"""
Painel local para rodar os downloads sem digitar comandos.

  Dois cliques em Painel.bat   (ou: python painel.py)

Abre http://127.0.0.1:8765 no navegador. O painel só aceita conexões do próprio computador.
Por baixo ele roda os mesmos scripts (vsco_dl.py / vsco_search_dl.py), um de cada vez (dois ao
mesmo tempo dobrariam o ritmo de requisições), e mostra a saída deles ao vivo.

  - Pasta de destino: a mesma de pasta_destino.py; trocar no painel vale na hora, inclusive para a
    execução em andamento (a partir do próximo perfil).
  - Parar: envia Ctrl+Break ao script, que encerra como num Ctrl+C (o curl é interrompido, arquivos
    parciais são apagados e o resumo é impresso). Se não responder em 15 s, é encerrado à força.
  - Lista: roda um item por vez; um bloqueio confirmado (código 3) interrompe a lista inteira.
  - Pesquisa (e lista de pesquisas): ao terminar sem erro, espera INTERVALO_REPETICAO segundos e roda
    tudo de novo, indefinidamente. Qualquer item que termine com código diferente de 0 encerra a repetição.

Opções: --porta N (padrão 8765), --sem-navegador
"""
import codecs
import http.server
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser

import pasta_destino
from registro_perfis import ARQUIVO_PADRAO as REGISTRO

AQUI = os.path.dirname(os.path.abspath(__file__))
HTML = os.path.join(AQUI, "painel.html")
PORTA_PADRAO = 8765
TOKEN = secrets.token_urlsafe(16)  # exigido nos POSTs: outro site aberto no navegador não consegue comandar o painel
SAIDA_BLOQUEIO = 3  # o mesmo de vsco_dl.SAIDA_BLOQUEIO
INTERVALO_REPETICAO = 10  # segundos entre uma rodada de pesquisa e a próxima
NAO_SAO_LISTAS = {"perfis_acessados.txt", "pasta_destino.txt", "vsco_sessao.txt"}
porta = PORTA_PADRAO


# ---------------------------------------------------------------- log ao vivo

class Log:
    """Saída dos scripts, linha a linha. O \\r das barras de progresso reescreve a linha atual."""

    MAX = 4000

    def __init__(self):
        self._lock = threading.Lock()
        self.linhas, self.base, self.atual, self._cr = [], 0, "", False

    def escrever(self, texto):
        with self._lock:
            if self._cr:
                texto, self._cr = "\r" + texto, False
            if texto.endswith("\r"):  # pode ser metade de um \r\n: espera o próximo pedaço
                texto, self._cr = texto[:-1], True
            for parte in re.split(r"(\r\n|\n|\r)", texto):
                if parte in ("\r\n", "\n"):
                    self.linhas.append(self.atual)
                    self.atual = ""
                elif parte == "\r":
                    self.atual = ""
                else:
                    self.atual += parte
            self._aparar()

    def linha(self, texto):
        """Mensagem do próprio painel, sempre numa linha nova."""
        with self._lock:
            if self.atual:
                self.linhas.append(self.atual)
                self.atual = ""
            self.linhas.extend(texto.split("\n"))
            self._aparar()

    def _aparar(self):
        excesso = len(self.linhas) - self.MAX
        if excesso > 0:
            del self.linhas[:excesso]
            self.base += excesso

    def ler(self, desde):
        with self._lock:
            fim = self.base + len(self.linhas)
            if not self.base <= desde <= fim:
                desde = self.base  # o navegador ficou para trás (ou o painel reiniciou): manda tudo
            return {"desde": desde, "fim": fim, "linhas": self.linhas[desde - self.base:], "atual": self.atual}


LOG = Log()


# ---------------------------------------------------------------- execução

class Tarefa:
    """Uma execução por vez: uma lista de comandos rodados em sequência."""

    def __init__(self):
        self._lock = threading.Lock()
        self.thread = self.proc = None
        self.parar_agora = self.parar_depois = False
        self.titulo, self.item, self.inicio, self.fim, self.codigo = "", None, None, None, None
        self.repetir, self.rodada, self.proxima = False, 0, None
        self._acordar = threading.Event()  # interrompe a espera entre rodadas quando pedem para parar

    @property
    def rodando(self):
        return bool(self.thread and self.thread.is_alive())

    def iniciar(self, titulo, comandos, repetir=False):
        with self._lock:
            if self.rodando:
                raise ValueError("Já existe uma execução em andamento.")
            self.parar_agora = self.parar_depois = False
            self.titulo, self.item, self.codigo = titulo, None, None
            self.inicio, self.fim = time.time(), None
            self.repetir, self.rodada, self.proxima = repetir, 0, None
            self._acordar.clear()
            self.thread = threading.Thread(target=self._rodar, args=(comandos,), daemon=True)
            self.thread.start()

    def _rodar(self, comandos):
        LOG.linha(f"\n##### {self.titulo} — início {time.strftime('%d/%m %H:%M:%S')}")
        try:
            while True:
                self.rodada += 1
                if self.repetir:
                    LOG.linha(f"\n##### Rodada {self.rodada} — {time.strftime('%d/%m %H:%M:%S')}")
                erro = self._rodada(comandos)
                if not self.repetir or self.parar_agora or self.parar_depois:
                    break
                if erro is not None:
                    LOG.linha(f"*** Terminou com erro (código {erro}): repetição encerrada.")
                    self.codigo = erro
                    break
                LOG.linha(f">>> Próxima rodada em {INTERVALO_REPETICAO} s.")
                self.item, self.proxima = None, time.time() + INTERVALO_REPETICAO
                self._acordar.wait(INTERVALO_REPETICAO)
                self.proxima = None
                if self.parar_agora or self.parar_depois:
                    LOG.linha(">>> Parado a pedido antes da próxima rodada.")
                    break
        except Exception as ex:
            LOG.linha(f"*** Erro no painel: {ex}")
            self.codigo = -1
        finally:
            self.proc, self.proxima, self.fim = None, None, time.time()
            LOG.linha(f"##### Fim — {duracao(self.fim - self.inicio)}")

    def _rodada(self, comandos):
        """Roda a lista de comandos uma vez. Devolve o primeiro código de saída diferente de 0, ou None."""
        total, erro = len(comandos), None
        for i, (nome, argv) in enumerate(comandos, 1):
            if self.parar_agora or self.parar_depois:
                LOG.linha(f">>> Parado a pedido antes de: {nome}")
                break
            self.item = {"i": i, "n": total, "nome": nome}
            prefixo = f"[{i}/{total}] " if total > 1 else ""
            LOG.linha(f"\n===== {prefixo}{nome}")
            self.codigo = self._executar(argv)
            if self.codigo != 0 and erro is None:
                erro = self.codigo
            if self.codigo == SAIDA_BLOQUEIO:
                if total > 1:
                    LOG.linha("*** Bloqueio confirmado: a lista foi interrompida. "
                              f"Para retomar, comece do item {i}.")
                break
            if self.parar_agora:
                break
        return erro

    def _executar(self, argv):
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0  # p/ receber o Ctrl+Break sozinho
        proc = subprocess.Popen(argv, cwd=AQUI, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, env=env, creationflags=flags)
        self.proc = proc
        if self.parar_agora:  # o Parar chegou enquanto o processo nascia
            threading.Thread(target=self._interromper, args=(proc,), daemon=True).start()
        dec = codecs.getincrementaldecoder("utf-8")("replace")
        while True:
            dados = proc.stdout.read1(65536)
            if not dados:
                break
            LOG.escrever(dec.decode(dados))
        LOG.escrever(dec.decode(b"", final=True))
        return proc.wait()

    def parar(self, depois=False):
        if not self.rodando:
            return
        if depois:
            self.parar_depois = True
            self._acordar.set()
            LOG.linha(">>> Vai parar quando o item atual terminar.")
            return
        self.parar_agora = True
        self._acordar.set()
        proc = self.proc
        if proc and proc.poll() is None:
            threading.Thread(target=self._interromper, args=(proc,), daemon=True).start()

    def _interromper(self, proc):
        LOG.linha(">>> Parando...")
        try:
            proc.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
        except OSError:
            pass
        try:
            proc.wait(15)
        except subprocess.TimeoutExpired:
            LOG.linha(">>> Não respondeu em 15 s: encerrando à força.")
            if os.name == "nt":  # /T leva junto o curl filho
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
            else:
                proc.kill()


TAREFA = Tarefa()


def duracao(seg):
    seg = int(seg)
    h, resto = divmod(seg, 3600)
    m, s = divmod(resto, 60)
    return f"{h} h {m:02d} min" if h else f"{m} min {s:02d} s" if m else f"{s} s"


def montar(cfg):
    """Formulário do painel -> (título, [(nome, argv)], repetir). Pesquisas se repetem (ver Tarefa)."""
    modo = cfg.get("modo")
    rps = float(str(cfg.get("rps", 1.5)).replace(",", "."))
    pausa = float(str(cfg.get("pausa", 5)).replace(",", "."))
    n = int(cfg.get("n") or 10)
    if rps < 0 or pausa < 0 or n < 1:
        raise ValueError("Ritmo e pausa precisam ser >= 0 e perfis >= 1.")
    comuns = ["--rps", f"{rps:g}", "--pausa-bloqueio", f"{pausa:g}"]
    comuns += [op for op, marcado in (("--original", cfg.get("original")), ("--forcar", cfg.get("forcar")),
                                      ("--links-only", cfg.get("links"))) if marcado]
    subpasta = (cfg.get("subpasta") or "").strip().strip('"')
    py = [sys.executable, "-u"]

    def busca(termo):
        # --uma-vez: quem repete é o painel, para valer também numa lista e o Parar cortar a espera
        return f'pesquisa "{termo}"', py + ["vsco_search_dl.py", "--uma-vez", "-n", str(n), *comuns,
                                            *(["-o", subpasta] if subpasta else []), "--", termo]

    def perfil(alvo):
        m = re.search(r"vsco\.co/([^/?#]+)", alvo)
        username = m.group(1) if m else alvo.strip("/")
        # a subpasta guarda as pastas dos perfis (no vsco_dl o -o é a pasta do próprio perfil)
        saida = ["-o", os.path.join(subpasta, username)] if subpasta else []
        return f"perfil {username}", py + ["vsco_dl.py", *comuns, *saida, "--", alvo]

    if modo in ("perfil", "pesquisa"):
        alvo = (cfg.get("alvo") or "").strip()
        if not alvo:
            raise ValueError("Informe o perfil." if modo == "perfil" else "Informe o termo de pesquisa.")
        cmd = perfil(alvo) if modo == "perfil" else busca(alvo)
        return cmd[0], [cmd], modo == "pesquisa"
    if modo == "lista":
        itens = [l.strip() for l in (cfg.get("lista") or "").splitlines()]
        itens = [l for l in itens if l and not l.startswith("#")]
        inicio = int(cfg.get("inicio") or 1)
        if not itens:
            raise ValueError("A lista está vazia.")
        if not 1 <= inicio <= len(itens):
            raise ValueError(f"'Começar do item' precisa estar entre 1 e {len(itens)}.")
        fazer = busca if cfg.get("como") == "pesquisa" else perfil
        itens = itens[inicio - 1:]
        como = "pesquisas" if fazer is busca else "perfis"
        return (f"lista: {len(itens)} {como} (a partir do item {inicio})", [fazer(i) for i in itens],
                fazer is busca)
    raise ValueError("Modo inválido.")


# ---------------------------------------------------------------- dados para a tela

_cache_registro = [None, 0]


def contar_registro():
    """Perfis no registro (linhas, sem o cabeçalho). Só reconta quando o arquivo muda."""
    try:
        st = os.stat(REGISTRO)
    except FileNotFoundError:
        return 0
    chave = (st.st_mtime_ns, st.st_size)
    if _cache_registro[0] != chave:
        total = 0
        with open(REGISTRO, "rb") as f:
            for bloco in iter(lambda: f.read(1 << 20), b""):
                total += bloco.count(b"\n")
        _cache_registro[:] = [chave, max(0, total - 1)]
    return _cache_registro[1]


def listas():
    saida = []
    for nome in sorted(os.listdir(AQUI)):
        caminho = os.path.join(AQUI, nome)
        if nome.lower().endswith(".txt") and nome not in NAO_SAO_LISTAS and os.path.isfile(caminho):
            with open(caminho, encoding="utf-8-sig", errors="replace") as f:
                itens = sum(1 for l in f if l.strip() and not l.startswith("#"))
            saida.append({"nome": nome, "itens": itens})
    return saida


def ler_lista(nome):
    if nome not in {l["nome"] for l in listas()}:
        raise ValueError("Lista não encontrada.")
    with open(os.path.join(AQUI, nome), encoding="utf-8-sig", errors="replace") as f:
        return f.read()


def pasta_efetiva():
    return pasta_destino.ler() or AQUI


def estado(desde):
    t = TAREFA
    return {
        "rodando": t.rodando,
        "titulo": t.titulo,
        "item": t.item if t.rodando else None,
        "parando": t.rodando and (t.parar_agora or t.parar_depois),
        "decorrido": duracao((t.fim or time.time()) - t.inicio) if t.inicio else "",
        "codigo": None if t.rodando else t.codigo,
        "repetir": t.rodando and t.repetir,
        "rodada": t.rodada,
        "espera": max(0, round(t.proxima - time.time())) if t.rodando and t.proxima else None,
        "pasta": pasta_destino.ler(),
        "pasta_efetiva": pasta_efetiva(),
        "registro": contar_registro(),
        "log": LOG.ler(desde),
    }


_dialogo = threading.Lock()


def escolher_pasta():
    """Abre a janela nativa 'Selecionar pasta' do Windows. Devolve '' se cancelar."""
    if not _dialogo.acquire(blocking=False):
        raise ValueError("A janela de escolher pasta já está aberta.")
    try:
        import tkinter
        from tkinter import filedialog
        raiz = tkinter.Tk()
        raiz.withdraw()
        raiz.attributes("-topmost", True)  # senão abre atrás do navegador
        try:
            return filedialog.askdirectory(parent=raiz, initialdir=pasta_efetiva(), mustexist=False,
                                           title="Pasta onde salvar as fotos")
        finally:
            raiz.destroy()
    finally:
        _dialogo.release()


# ---------------------------------------------------------------- HTTP

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _enviar(self, corpo, tipo, code=200):
        self.send_response(code)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(corpo)

    def _json(self, obj, code=200):
        self._enviar(json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8", code)

    def _local(self):
        # recusa nomes de host estranhos (protege contra DNS rebinding)
        return self.headers.get("Host", "") in (f"127.0.0.1:{porta}", f"localhost:{porta}")

    def do_GET(self):
        if not self._local():
            return self._json({"erro": "proibido"}, 403)
        url = urllib.parse.urlsplit(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        try:
            if url.path == "/":
                with open(HTML, encoding="utf-8") as f:
                    self._enviar(f.read().replace("__TOKEN__", TOKEN).encode(), "text/html; charset=utf-8")
            elif url.path == "/api/estado":
                self._json(estado(int(q.get("desde", 0))))
            elif url.path == "/api/listas":
                self._json(listas())
            elif url.path == "/api/lista":
                self._json({"texto": ler_lista(q.get("nome", ""))})
            else:
                self._json({"erro": "não encontrado"}, 404)
        except (ValueError, OSError) as ex:
            self._json({"erro": str(ex)}, 400)

    def do_POST(self):
        if not self._local() or self.headers.get("X-Token") != TOKEN:
            return self._json({"erro": "proibido"}, 403)
        try:
            tamanho = int(self.headers.get("Content-Length") or 0)
            cfg = json.loads(self.rfile.read(tamanho) or b"{}")
            if self.path == "/api/pasta":
                caminho = (cfg.get("caminho") or "").strip()
                if not caminho:
                    raise ValueError("Informe uma pasta.")
                pasta = pasta_destino.definir(caminho)
                LOG.linha(f">>> Pasta padrão definida: {pasta}")
                self._json({"pasta": pasta})
            elif self.path == "/api/escolher-pasta":
                escolhida = escolher_pasta()
                if escolhida:
                    escolhida = pasta_destino.definir(escolhida)
                    LOG.linha(f">>> Pasta padrão definida: {escolhida}")
                self._json({"pasta": escolhida})
            elif self.path == "/api/abrir-pasta":
                pasta = pasta_efetiva()
                os.makedirs(pasta, exist_ok=True)
                os.startfile(pasta)
                self._json({"ok": True})
            elif self.path == "/api/iniciar":
                titulo, comandos, repetir = montar(cfg)
                TAREFA.iniciar(titulo, comandos, repetir)
                self._json({"ok": True})
            elif self.path == "/api/parar":
                TAREFA.parar(depois=bool(cfg.get("depois")))
                self._json({"ok": True})
            else:
                self._json({"erro": "não encontrado"}, 404)
        except (ValueError, OSError) as ex:
            self._json({"erro": str(ex)}, 400)


def main():
    global porta
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = sys.argv[1:]
    inicial = int(args[args.index("--porta") + 1]) if "--porta" in args else PORTA_PADRAO
    for porta in range(inicial, inicial + 20):  # se a porta estiver ocupada, tenta as seguintes
        try:
            servidor = http.server.ThreadingHTTPServer(("127.0.0.1", porta), Handler)
            break
        except OSError:
            continue
    else:
        sys.exit(f"Nenhuma porta livre entre {inicial} e {inicial + 19}.")
    servidor.daemon_threads = True
    url = f"http://127.0.0.1:{porta}/"
    print(f"Painel aberto em {url}\nDeixe esta janela aberta enquanto usa o painel. Para sair: Ctrl+C.")
    if "--sem-navegador" not in args:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if TAREFA.rodando:
            print("Parando a execução em andamento...")
            TAREFA.parar()
            TAREFA.thread.join(20)
        servidor.server_close()


if __name__ == "__main__":
    main()
