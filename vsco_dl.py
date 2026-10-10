"""
Baixa todas as fotos/vídeos de uma galeria pública do VSCO (por padrão em baixa qualidade, ~300 px de largura).

Como o site funciona (e por que não precisamos rolar a página):
  1. GET https://vsco.co/<user>/gallery devolve o HTML com `window.__PRELOADED_STATE__`,
     um JSON contendo: o site_id do perfil, um token público anônimo (users.currentUser.tkn)
     e as primeiras 14 mídias + `nextCursor`. Essa visita também seta o cookie `vs_app_id`.
  2. O "scroll infinito" só chama
       GET /api/3.0/medias/profile?site_id=<id>&limit=<n>&cursor=<cursor>
     com `Authorization: Bearer <tkn>` + o cookie acima (sem o cookie o Cloudflare devolve 403).
     A resposta traz `media[]` e `next_cursor` (ausente na última página).
  3. Cada imagem tem `responsive_url` (im.vsco.co/...jpg). Sem o parâmetro `?w=` ela redireciona
     para img.vsco.co com o arquivo ORIGINAL (tamanho == image_meta.fileSize). Com `?w=<n>` o CDN
     redimensiona para a faixa de tamanho mais próxima (pode vir um pouco maior que o pedido, ex.:
     w=300 -> 321x480). Por padrão usamos w=300 (~25 KB por foto).
  4. Cada perfil baixado é gravado em perfis_acessados.txt (ver registro_perfis.py); perfis que já
     estão lá são pulados nas próximas execuções.
  5. Erros (fotos que falharam, erro ao listar, exceções) vão para erros.log com todos os detalhes
     (ver log_erros.py) e saem com código 1; o painel e os laços seguem para o próximo da fila. Só o
     bloqueio (código 3) e o disco cheio (código 4) param tudo.
     Perfil apagado ou inexistente (HTTP 404/410) não é erro: é pulado e sai com código 0. O aviso vai
     para o erros.log uma vez só, e o perfil (quando a API de mídias responde site_not_found) entra no
     registro como "apagado", para não ser mais tentado.
  6. Limite de espaço em disco (--espaco-minimo, padrão 2 GB; ver espaco_disco.py): o espaço livre
     do disco da pasta de destino é conferido antes e durante os downloads; abaixo do limite, o curl
     é encerrado, o arquivo pela metade é apagado e o script sai com código 4.
  7. Pasta sincronizada (Google Drive, OneDrive, Dropbox): o curl grava os .part e o status do lote
     numa pasta de trabalho no %TEMP% (_pasta_trabalho), e cada foto pronta vai na hora para o destino,
     já com a data da foto. O programa de sincronização só vê fotos completas. Se o destino estiver preso
     por outro programa, a foto pronta fica guardada e é tentada de novo sem segurar o download nem
     baixar de novo (_Entrega); continuando presa por PRESO_MAX_S, vai para o erros.log.

Cuidados com o firewall (Cloudflare) do VSCO:
  - Ritmo global (--rps): TODAS as requisições (página, API, fotos), de todas as threads e de todas
    as linhas de execução do computador (as abas do painel, ou dois terminais), passam por um único
    limitador (RITMO, ver coordenacao.py). O padrão é 1,5 requisição por segundo; 0 = sem limite.
  - Bloqueio: se vier a página "Sorry, you have been blocked" (ou um desafio/limite do Cloudflare),
    tudo pausa na hora (o curl em andamento é encerrado), nesta e nas outras linhas, por
    --pausa-bloqueio minutos (padrão 5) e então sai UMA requisição de teste. Se passar, foi uma
    recusa isolada e o download continua; se for recusada de novo, tudo para em todas as linhas
    (exceção Bloqueado, código de saída 3). Nada do perfil em andamento entra no registro, e os
    arquivos já baixados são pulados na próxima execução.
  - Conexão reaproveitada: as fotos de um perfil são baixadas por UM processo curl, em série, na
    mesma conexão (keep-alive), em vez de um processo e uma conexão nova por foto.

Uso:
  python vsco_dl.py isahevangelista
  python vsco_dl.py https://vsco.co/isahevangelista/gallery -o fotos --rps 1
  python vsco_dl.py isahevangelista --links-only     # só gera links.txt (p/ wget -i links.txt)
  python vsco_dl.py isahevangelista --original       # resolução original em vez da menor
  python vsco_dl.py isahevangelista --forcar         # baixa mesmo se já estiver no registro
  python vsco_dl.py isahevangelista --espaco-minimo 5   # para quando o disco tiver só 5 GB livres

Pasta de destino: sem -o (ou com -o relativo) os arquivos vão para dentro da pasta definida com
  python pasta_destino.py "D:\\Fotos VSCO"   (ver pasta_destino.py)
"""
import argparse
import errno
import itertools
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
from datetime import datetime, timedelta, timezone

import coordenacao
import dependencias
import espaco_disco
import log_erros
from pasta_destino import Destino
from registro_perfis import ARQUIVO_PADRAO, RegistroPerfis

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
API = "https://vsco.co/api/3.0/medias/profile"
PAGE_SIZE = 14  # o mesmo valor que o site usa; valores maiores levam 403 do Cloudflare
LARGURA_MINIMA = 300  # largura mínima pedida ao CDN; ele arredonda para a faixa dele (às vezes um pouco maior)
RPS_PADRAO = 1.5
SAIDA_BLOQUEIO = 3  # código de saída quando o Cloudflare bloqueia (p/ parar laços em PowerShell/bash)
SAIDA_SEM_ESPACO = espaco_disco.SAIDA_SEM_ESPACO  # 4: o disco de destino chegou ao limite de segurança

# O Cloudflare do vsco.co bloqueia o fingerprint TLS do Python (urllib/requests -> 403),
# mas aceita o curl. Por isso todas as requisições passam pelo curl.
dependencias.preparar_path()  # acha o curl/ffmpeg instalados pelo Painel.bat (ver dependencias.py)
CURL = shutil.which("curl") or sys.exit("curl não encontrado no PATH.")
COOKIE_JAR = os.path.join(tempfile.gettempdir(), f"vsco_cookies_{os.getpid()}.txt")
# Pasta de trabalho desta execução: o curl grava ali os .part e o status do lote (ver _pasta_trabalho).
PASTA_TRABALHO = os.path.join(tempfile.gettempdir(), f"vsco_lote_{os.getpid()}")
ESPACO_TRABALHO = espaco_disco.GB  # livre mínimo no disco da pasta temporária; abaixo, usa o destino
PRESO_MAX_S = 30  # por quanto tempo um arquivo pronto, com o destino preso por outro programa, é tentado
_LOTES = itertools.count()


# ---------------------------------------------------------------- bloqueio do Cloudflare

class Bloqueado(Exception):
    """O firewall do VSCO bloqueou o acesso. Tudo para; a retomada fica para a próxima execução."""


PAUSA_PADRAO_MIN = 5  # minutos parados antes da requisição de teste
MAX_PAUSAS = 3  # pausas por execução; depois disso o próximo bloqueio encerra direto
LIMITE_NEGADAS = 5  # rede de segurança caso o Cloudflare mude o texto da página de bloqueio

_parada = threading.Event()  # bloqueio confirmado: acorda e interrompe todas as threads
_motivo_parada = None
_estado = threading.Condition()  # protege as variáveis abaixo e avisa o fim de uma pausa
_pausado = False
_pausas_feitas = 0
_pausa_min = PAUSA_PADRAO_MIN
_negadas_seguidas = 0

_MARCAS_BLOQUEIO = re.compile(
    rb"you have been blocked|you are being rate limited|Attention Required! \| Cloudflare"
    rb"|cf-error-details|<title>Just a moment|challenge-platform", re.I)


def parar(motivo):
    global _motivo_parada
    with _estado:
        if not _parada.is_set():
            _motivo_parada = motivo
            _parada.set()
        _estado.notify_all()


def checar_parada():
    if not _parada.is_set():
        motivo = coordenacao.bloqueio_alheio()
        if motivo:
            parar(f"{motivo}\n    (confirmado por outra linha de execução; esta parou também, porque o IP é o mesmo)")
    if _parada.is_set():
        raise Bloqueado(_motivo_parada)


def pausado():
    return _pausado or coordenacao.pausa_alheia() is not None


_avisos_pausa = set()  # pausas de outras linhas já avisadas na tela (início e fim)


def aguardar_pausa():
    """Se há uma pausa de bloqueio em andamento, desta ou de outra linha de execução, espera ela
    terminar. Levanta Bloqueado se o bloqueio se confirmou."""
    alheia = None
    while True:
        with _estado:
            while _pausado and not _parada.is_set():
                _estado.wait(1)  # com prazo: no Windows um wait sem prazo não deixa o Ctrl+C passar
        checar_parada()
        p = coordenacao.pausa_alheia()
        if not p:
            break
        alheia = p
        with _estado:
            avisar = ("início", p["n"]) not in _avisos_pausa
            _avisos_pausa.add(("início", p["n"]))
        if avisar:
            _log(f"\n!!! Outra linha de execução viu a página de bloqueio: esta também está pausada. Às "
                 f"{datetime.fromtimestamp(p['ate']):%H:%M:%S} aquela faz UMA requisição de teste antes de decidir.")
        _parada.wait(1)
    if alheia:
        checar_parada()  # a pausa da outra linha pode ter terminado num bloqueio confirmado
        with _estado:
            avisar = ("fim", alheia["n"]) not in _avisos_pausa
            _avisos_pausa.add(("fim", alheia["n"]))
        if avisar:
            _log("    A pausa da outra linha terminou sem bloqueio confirmado. Retomando.")


def _descrever_bloqueio(code, corpo):
    """Texto curto da página de bloqueio: título/h1 + Ray ID (útil se for pedir desbloqueio)."""
    m = re.search(rb"<h1[^>]*>(.*?)</h1>", corpo, re.S) or re.search(rb"<title>(.*?)</title>", corpo, re.S)
    texto = re.sub(rb"<[^>]+>|\s+", b" ", m.group(1)).strip().decode("utf-8", "replace") if m else "página do Cloudflare"
    ray = re.search(rb"Ray ID:?\s*(?:<[^>]+>\s*)*([0-9a-f]{16})", corpo)
    return f'HTTP {code} "{texto}"' + (f" (Cloudflare Ray ID {ray.group(1).decode()})" if ray else "")


def _eh_bloqueio(code, corpo):
    return code in ("403", "429") and bool(_MARCAS_BLOQUEIO.search(corpo))


def checar_resposta(code, corpo=b"", url=None):
    """Chamada a cada resposta HTTP. Se for a página de bloqueio, desafio ou limite do Cloudflare
    (ou LIMITE_NEGADAS respostas 403/429 seguidas), pausa tudo e confirma o bloqueio (ver
    confirmar_bloqueio). Devolve True se o bloqueio NÃO se confirmou: a requisição deve ser repetida.
    Levanta Bloqueado se ele se confirmou."""
    global _negadas_seguidas
    with _estado:
        _negadas_seguidas = _negadas_seguidas + 1 if code in ("403", "429") else 0
        negadas = _negadas_seguidas
    if _eh_bloqueio(code, corpo):
        motivo = _descrever_bloqueio(code, corpo)
    elif negadas >= LIMITE_NEGADAS:
        motivo = f"{negadas} respostas HTTP 403/429 seguidas"
    else:
        return False
    confirmar_bloqueio(motivo + (f"\n    em {url}" if url else ""), url)
    return True


def _sondar(url):
    """A requisição de teste: um GET simples, fora do RITMO. Devolve (código, corpo)."""
    r = subprocess.run([CURL, "-sS", "-L", "--max-time", "60", "-A", UA, "-w", "\n%{http_code}", url],
                       capture_output=True)
    corpo, _, code = r.stdout.rpartition(b"\n")
    return code.decode(errors="replace").strip()[-3:], corpo


def confirmar_bloqueio(motivo, url):
    """Uma recusa isolada acontece às vezes sem o site estar bloqueando de fato. Então, antes de
    desistir: pausa TODAS as threads, e as outras linhas de execução, por `_pausa_min` minutos (o curl
    em andamento é encerrado) e faz UMA requisição de teste na mesma URL. Se ela passar, retoma; se
    for recusada de novo, para tudo (as outras linhas também, ver coordenacao.avisar_bloqueio).

    Só a primeira thread (de todas as linhas) que vê o bloqueio conduz a pausa; as outras esperam o
    resultado. Sem pausa configurada (0) ou depois de MAX_PAUSAS pausas, o bloqueio encerra direto."""
    global _pausado, _pausas_feitas, _negadas_seguidas
    with _estado:
        if _parada.is_set():
            raise Bloqueado(_motivo_parada)
        if _pausado:  # outra thread já está conduzindo a pausa
            dono = False
        elif not _pausa_min or _pausas_feitas >= MAX_PAUSAS or not url:
            dono = None
        elif not coordenacao.iniciar_pausa(_pausa_min):  # outra linha de execução já está conduzindo
            dono = False
        else:
            dono, _pausado = True, True
            _pausas_feitas += 1
    if dono is None:
        if _pausa_min and _pausas_feitas >= MAX_PAUSAS:
            motivo += f"\n    (limite de {MAX_PAUSAS} pausas por execução atingido)"
        parar(motivo)
        coordenacao.avisar_bloqueio(motivo)
        raise Bloqueado(motivo)
    if not dono:
        aguardar_pausa()
        return

    try:
        volta = datetime.now() + timedelta(minutes=_pausa_min)
        _log(f"\n!!! Página de bloqueio do Cloudflare: {motivo}\n"
             f"    Todas as requisições estão pausadas por {_pausa_min:g} min (pausa {_pausas_feitas} de "
             f"{MAX_PAUSAS}); às {volta:%H:%M:%S} faço UMA requisição de teste antes de decidir.")
        fim = time.monotonic() + _pausa_min * 60
        while time.monotonic() < fim:  # em fatias de 1 s: deixa o Ctrl+C interromper
            if _parada.wait(min(1, max(0, fim - time.monotonic()))):
                checar_parada()
        code, corpo = _sondar(url)
        RITMO.contar()
        if code in ("403", "429"):
            detalhe = _descrever_bloqueio(code, corpo) if _eh_bloqueio(code, corpo) else f"HTTP {code}"
            motivo += f"\n    confirmado: o teste após {_pausa_min:g} min de pausa também foi recusado ({detalhe})"
            parar(motivo)
            coordenacao.avisar_bloqueio(motivo)
            raise Bloqueado(motivo)
        _log(f"    Teste passou (HTTP {code}): foi uma recusa isolada. Retomando.")
    finally:
        coordenacao.encerrar_pausa()
        with _estado:
            _pausado = False
            _negadas_seguidas = 0
            _estado.notify_all()


def mensagem_bloqueio(ex):
    rps = f"{RITMO.rps:g}" if RITMO.rps else "sem limite"
    return (f"\n*** BLOQUEADO pelo firewall do VSCO: {ex}\n"
            f"    Todas as requisições foram interrompidas para não prolongar o bloqueio.\n"
            f"    O perfil em andamento NÃO foi registrado: na próxima execução os arquivos já baixados\n"
            f"    são pulados e o resto é retomado. Espere o bloqueio passar e rode de novo, de\n"
            f"    preferência com um --rps menor (atual: {rps}).")


# ---------------------------------------------------------------- ritmo global

class Ritmo:
    """Limite global de requisições por segundo, compartilhado por todas as threads e por todas as
    linhas de execução do computador (as vagas ficam em coordenacao.py).

    Cada requisição reserva uma "vaga" de início; as vagas ficam espaçadas de 1/rps segundos.
    Um lote do curl reserva várias vagas seguidas de uma vez e o próprio curl as respeita com
    --rate, então a soma de tudo (listagem + fotos, de todas as linhas) nunca passa de `rps`."""

    def __init__(self, rps=0):
        self.rps = rps

    def reservar(self, n=1):
        """Reserva n inícios de requisição consecutivos e espera até o primeiro. Devolve o fim da
        reserva (para liberar). Durante uma pausa de bloqueio, espera ela terminar antes de reservar."""
        while True:
            aguardar_pausa()
            if not self.rps:
                return None
            pausas = (_pausas_feitas, coordenacao.pausas())
            vaga = coordenacao.reservar(n / self.rps, teto=(PAGE_SIZE + 1) / self.rps)
            if not vaga:  # outra linha está no meio de um lote grande: ela vai dividi-lo em instantes
                _parada.wait(0.2)
                continue
            inicio, fim = vaga
            if inicio > time.time():
                _parada.wait(max(0, inicio - time.time()))
            aguardar_pausa()
            if (_pausas_feitas, coordenacao.pausas()) == pausas:
                return fim
            # houve uma pausa enquanto esperava: a vaga ficou para trás, reserva outra

    def liberar(self, fim):
        """Devolve as vagas que sobraram de um lote interrompido (pausa, ou a vez da outra linha)."""
        if self.rps and fim:
            coordenacao.liberar(fim, 1 / self.rps)

    def contar(self):
        """Conta uma requisição feita por fora (a de teste da pausa): empurra a próxima vaga."""
        if self.rps:
            coordenacao.empurrar(1 / self.rps)

    def opcao_curl(self):
        """--rate do curl equivalente (ele só aceita inteiros por unidade de tempo)."""
        return ["--rate", f"{max(1, round(self.rps * 3600))}/h"] if self.rps else []

    def estimar(self, n):
        """Tempo mínimo para n requisições neste ritmo (ex.: "~1 min 05 s a 1.5 req/s")."""
        return f"~{duracao(n / self.rps)} a {self.rps:g} req/s" if self.rps else ""


RITMO = Ritmo(RPS_PADRAO)


def duracao(seg):
    seg = int(round(seg))
    h, resto = divmod(seg, 3600)
    m, s = divmod(resto, 60)
    return f"{h} h {m:02d} min" if h else f"{m} min {s:02d} s" if m else f"{s} s"


# ---------------------------------------------------------------- requisições

# códigos de saída do curl mais comuns, para o log de erros
_ERROS_CURL = {"6": "não achou o endereço (DNS)", "7": "não conectou", "28": "tempo esgotado",
               "35": "falha no TLS", "52": "resposta vazia", "56": "conexão interrompida"}


def descrever_curl(codigo):
    codigo = str(codigo)
    return f"curl código {codigo}" + (f" ({_ERROS_CURL[codigo]})" if codigo in _ERROS_CURL else "")


def _resumir_corpo(corpo):
    """Trecho da resposta de erro para o log: o <title> de uma página HTML ou o começo de um JSON/texto."""
    m = re.search(rb"<title[^>]*>(.*?)</title>", corpo, re.S | re.I)
    if m:
        return "página: " + " ".join(m.group(1).decode("utf-8", "replace").split())
    texto = " ".join(corpo[:2000].decode("utf-8", "replace").split())
    if "<html" in texto.lower():
        return "página HTML sem título"
    return texto[:300] + ("…" if len(texto) > 300 else "")


class ErroHTTP(RuntimeError):
    """Resposta HTTP de erro (ou falha do curl) depois das tentativas. A mensagem continua sendo
    "HTTP <código> em <url> ..."; os atributos vão para o log de erros (log_erros.py)."""

    def __init__(self, code, url, curl_codigo, curl_saida, corpo):
        self.code, self.url, self.curl_codigo, self.curl_saida = code or "?", url, curl_codigo, curl_saida
        self.resposta = _resumir_corpo(corpo)
        super().__init__(f"HTTP {self.code} em {url} {curl_saida}".rstrip())

    def campos_log(self):
        curl = f"{descrever_curl(self.curl_codigo)}: {self.curl_saida}" if self.curl_codigo else None
        return {"url": self.url, "http": self.code, "curl": curl, "resposta": self.resposta}


def titulo_erro_perfil(ex, padrao):
    """Título do log para um erro ao abrir/listar um perfil: 404/410 = perfil apagado ou inexistente."""
    code = getattr(ex, "code", None)
    if code in ("404", "410"):
        return f"perfil apagado ou inexistente (HTTP {code})"
    if isinstance(ex, LookupError):
        return "perfil não encontrado na página"
    return padrao


def perfil_apagado(ex):
    """O erro diz que o perfil foi apagado ou não existe (HTTP 404/410)? Isso não é falha da execução:
    o perfil é pulado, vai para o erros.log uma vez só e a execução segue sem erro (código 0)."""
    return getattr(ex, "code", None) in ("404", "410")


def registrar_apagado(ex):
    """Pode ir para o registro como "apagado" (e não ser mais tentado)? Só com a resposta site_not_found
    da API de mídias: um 404 genérico (ex.: a API mudou de endereço) marcaria todos os perfis."""
    return perfil_apagado(ex) and "site_not_found" in getattr(ex, "resposta", "")


def pular_apagado(ex, username, site_id=None, registro=None, depois="", **campos):
    """Perfil apagado ou inexistente: grava no registro como "apagado" (com `registro` e site_id, se
    registrar_apagado), avisa no erros.log uma vez só e na tela. `depois` completa o "o que foi feito"."""
    gravar = registro is not None and site_id and registrar_apagado(ex)
    acao = (("perfil pulado e gravado no registro como apagado (não será tentado de novo)" if gravar
             else "perfil pulado") + depois + ". Não conta como erro; este aviso não se repete no log")
    novo = log_erros.registrar(f"{titulo_erro_perfil(ex, 'perfil apagado ou inexistente')}: {username}", ex=ex,
                               uma_vez=True, **campos, perfil=username, site_id=site_id, acao=acao)
    if gravar:
        registro.registrar(site_id, username, "apagado")
    # sem a palavra "erro": o painel pinta essas linhas de vermelho, e isto é só um aviso (amarelo, "pulado")
    _log(f"\n  {username} pulado: perfil apagado ou inexistente (HTTP {ex.code})"
         + ("; gravado no registro, não será tentado de novo" if gravar else "")
         + (" (anotado no log, uma vez só)" if novo else " (já anotado no log antes)"))


def fetch(url, headers=None, retries=3):
    """GET via curl (página/API), respeitando o RITMO. Retorna o corpo em bytes."""
    cmd = [CURL, "-sS", "-L", "--compressed", "--max-time", "120", "-A", UA,
           "-b", COOKIE_JAR, "-c", COOKIE_JAR, "-w", "\n%{http_code}"]
    for k, v in (headers or {}).items():
        cmd += ["-H", f"{k}: {v}"]
    cmd.append(url)
    attempt = 0
    while True:
        RITMO.reservar()
        r = subprocess.run(cmd, capture_output=True)
        corpo, _, code = r.stdout.rpartition(b"\n")
        code = code.decode(errors="replace").strip()[-3:]
        if checar_resposta(code, corpo, url):
            continue  # bloqueio não confirmado após a pausa: repete sem gastar tentativa
        if r.returncode == 0 and code == "200":
            return corpo
        if attempt < retries - 1 and (r.returncode != 0 or code in ("403", "429", "500", "502", "503", "504")):
            _parada.wait(2 ** attempt * 2)
            checar_parada()
            attempt += 1
            continue
        raise ErroHTTP(code, url, r.returncode, r.stderr.decode(errors="replace").strip(), corpo)


def load_profile(username):
    html = fetch(f"https://vsco.co/{username}/gallery").decode("utf-8")
    m = re.search(r"window\.__PRELOADED_STATE__\s*=\s*(\{.*?\})\s*</script>", html, re.S)
    if not m:
        raise LookupError("não encontrei __PRELOADED_STATE__ no HTML (perfil inexistente ou layout mudou)")
    state = json.loads(re.sub(r":\s*undefined\b", ":null", m.group(1)))
    site = state["sites"]["siteByUsername"].get(username, {}).get("site")
    if not site:
        raise LookupError(f"perfil '{username}' não encontrado")
    return site["id"], state["users"]["currentUser"]["tkn"]


def _log(msg):
    print(msg, file=sys.stderr)


def iter_media(site_id, token, username, log=_log):
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Referer": f"https://vsco.co/{username}/gallery",
    }
    cursor, page = None, 0
    while True:
        q = {"site_id": site_id, "limit": PAGE_SIZE}
        if cursor:
            q["cursor"] = cursor
        data = json.loads(fetch(f"{API}?{urllib.parse.urlencode(q)}", headers))
        page += 1
        items = data.get("media", [])
        if log:
            log(f"  página {page}: {len(items)} itens")
        yield from items
        cursor = data.get("next_cursor")
        if not cursor or not items:
            break


def to_url(u):
    if not u:
        return None
    u = u.split("?")[0]
    u = u if u.startswith("http") else "https://" + u.lstrip("/")
    # alguns arquivos têm nome com emoji/acentos (ex.: "🗻.jpg"); a URL precisa ir percent-encoded
    p = urllib.parse.urlsplit(u)
    return urllib.parse.urlunsplit(p._replace(path=urllib.parse.quote(urllib.parse.unquote(p.path))))


def media_entry(item, largura=LARGURA_MINIMA):
    """Normaliza um item da API em (id, url, ext, timestamp_ms, is_hls).

    `largura` pede a foto redimensionada pelo CDN; None/0 = arquivo original."""
    kind = item.get("type")
    obj = item.get(kind) or {}
    mid = obj.get("_id") or obj.get("id")
    ts = obj.get("capture_date_ms") or obj.get("upload_date") or obj.get("created_date") or 0
    if kind == "image" and not obj.get("is_video"):
        url = to_url(obj.get("responsive_url"))
        ext = os.path.splitext(urllib.parse.urlparse(url).path)[1] or ".jpg"
        if url and largura:
            url += f"?w={largura}"
        return mid, url, ext, ts, False
    # vídeos: VSCO costuma expor mp4 (video_url) e/ou HLS (playback_url .m3u8)
    for key in ("video_url", "playback_url", "hls_url"):
        if obj.get(key):
            url = to_url(obj[key])
            return mid, url, ".mp4", ts, url.endswith(".m3u8")
    return mid, None, None, ts, False


def collect_entries(site_id, token, username, limit=None, largura=LARGURA_MINIMA, log=_log):
    """Percorre a paginação e devolve a lista de mídias (sem duplicatas). `log=None` silencia."""
    entries, seen = [], set()
    for item in iter_media(site_id, token, username, log):
        e = media_entry(item, largura)
        if e[0] in seen:
            continue
        seen.add(e[0])
        if e[1]:
            entries.append(e)
        elif log:
            log(f"  aviso: não achei URL para {item.get('type')} {e[0]}")
        if limit and len(entries) >= limit:
            break
    return entries


# ---------------------------------------------------------------- downloads

def media_path(entry, outdir):
    mid, _, ext, ts, _ = entry
    date = datetime.fromtimestamp(ts / 1000, timezone.utc).strftime("%Y-%m-%d_%H%M%S") if ts else "sem-data"
    return os.path.join(outdir, f"{date}_{mid}{ext}")


def _pasta_trabalho(outdir):
    """Onde o curl grava os .part e o arquivo de status: a pasta temporária do sistema, fora da pasta
    de destino. Assim um programa de sincronização (Google Drive, OneDrive, Dropbox) só vê cada foto
    pronta, nunca um arquivo pela metade nem o status que muda a cada foto. Se o disco da pasta
    temporária tiver menos de ESPACO_TRABALHO livre, usa a própria pasta de destino (como antes)."""
    try:
        os.makedirs(PASTA_TRABALHO, exist_ok=True)
        if shutil.disk_usage(PASTA_TRABALHO).free >= ESPACO_TRABALHO:
            return PASTA_TRABALHO
    except OSError:
        pass
    return outdir


def _apagar(caminho):
    """Apaga o arquivo, se existir. Não espera nem levanta se ele estiver em uso: o que sobra na pasta
    de trabalho sai no fim da execução, e um .part que sobra no destino é sobrescrito na próxima."""
    try:
        os.remove(caminho)
    except OSError:
        pass


def _mover(tmp, path):
    """Põe o arquivo pronto `tmp` no destino `path`. No mesmo disco é só trocar o nome (instantâneo).
    Em outro disco (ex.: %TEMP% no C: e destino no G: do Google Drive) copia para <path>.part e troca
    o nome logo em seguida: uma foto pela metade nunca fica com o nome final (a retomada a pularia)."""
    try:
        os.replace(tmp, path)
        return
    except OSError as ex:
        if ex.errno != errno.EXDEV:
            raise
    parcial = path + ".part"
    try:
        shutil.copy2(tmp, parcial)  # copy2 leva junto a data da foto
        os.replace(parcial, path)
    except BaseException:
        _apagar(parcial)
        raise
    _apagar(tmp)


class _Entrega:
    """Leva cada arquivo pronto da pasta de trabalho para o destino e conta o "ok" no progresso.

    Nunca segura o download: se o destino estiver preso por outro programa (o Google Drive subindo
    um arquivo, um antivírus), o arquivo pronto fica guardado e é tentado de novo a cada volta do
    laço do lote (tentar_presos), por até PRESO_MAX_S segundos, sem ser baixado de novo. Só no fim
    do perfil (esperar) a execução aguarda os que ainda estiverem presos."""

    def __init__(self, progresso):
        self.progresso = progresso
        self.presos = {}  # path -> (entry, tmp, desde)

    def entregar(self, entry, tmp, path):
        ts = entry[3]
        if ts:  # antes de mover: a foto já chega ao destino com a data certa (o Drive sobe uma vez só)
            try:
                os.utime(tmp, (ts / 1000, ts / 1000))
            except OSError:
                pass
        self._tentar(entry, tmp, path, time.monotonic())

    def _tentar(self, entry, tmp, path, desde):
        try:
            _mover(tmp, path)
        except PermissionError as ex:
            if time.monotonic() - desde < PRESO_MAX_S:
                self.presos[path] = (entry, tmp, desde)
                return
            erro = f"baixou, mas o destino continuou em uso por outro programa por {PRESO_MAX_S} s ({ex})"
        except OSError as ex:
            erro = f"baixou, mas não deu para gravar no destino ({ex})"
        else:
            self.progresso("ok")
            return
        _apagar(tmp)
        self.progresso(f"erro ({erro})", entry[1])

    def tentar_presos(self):
        for path, (entry, tmp, desde) in list(self.presos.items()):
            del self.presos[path]
            self._tentar(entry, tmp, path, desde)

    def esperar(self):
        while self.presos:
            time.sleep(0.2)
            self.tentar_presos()


def _checar_curl():
    """O lote usa -w '%output{}' (curl >= 8.3) para saber, arquivo a arquivo, como cada download terminou."""
    r = subprocess.run([CURL, "--version"], capture_output=True, text=True)
    m = re.match(r"curl (\d+)\.(\d+)", r.stdout)
    if not m or (int(m.group(1)), int(m.group(2))) < (8, 3):
        sys.exit(f"É preciso curl 8.3 ou mais novo (encontrado: {r.stdout.splitlines()[0] if r.stdout else CURL}).")


def _baixar_fatia(fatia, outdir, trabalho, entrega, progresso, motivos):
    """Baixa `fatia` [(entry, path)] com UM processo curl: em série, na mesma conexão, no ritmo do
    RITMO (vagas já reservadas por quem chama). O curl grava em `trabalho` (ver _pasta_trabalho) e
    cada arquivo pronto vai na hora para o destino (`entrega`). O resultado de cada arquivo é lido ao
    vivo, então um bloqueio (aqui, em outra thread ou em outra linha) mata o curl na hora. O espaço
    livre do disco de `outdir` também é conferido a cada volta: abaixo do limite (espaco_disco), o curl
    é encerrado e sai SemEspaco. Um lote maior que PAGE_SIZE também é interrompido quando outra linha
    de execução pede vez.

    Devolve (retentar, repetir): `retentar` são falhas transitórias, para outra rodada com espera
    (o motivo de cada uma fica em `motivos[path]`, para o log de erros); `repetir` são os itens
    interrompidos (pausa de bloqueio que não se confirmou, ou a vez da outra linha), para baixar
    logo em seguida."""
    por_nome = {os.path.basename(path) + ".part": (entry, path) for entry, path in fatia}
    config = "".join(f'url = "{entry[1]}"\noutput = "{nome}"\n' for nome, (entry, _) in por_nome.items())
    nome_status = f".curl_status_{os.getpid()}_{next(_LOTES)}.tmp"  # um por lote: nunca sobra linha de outro
    status = os.path.join(trabalho, nome_status)
    # %output{>>arq} grava e fecha o arquivo a cada transferência (stdout/stderr em pipe só chegam no fim)
    cmd = [CURL, "-sS", "-L", "--max-time", "120", "-A", UA, *RITMO.opcao_curl(), "-K", "-",
           "-w", f"%output{{>>{nome_status}}}%{{filename_effective}}\t%{{http_code}}\t%{{exitcode}}\n"]
    retentar, repetir, lido, resto, interrompido = [], [], 0, b"", False
    with tempfile.TemporaryFile() as erros:  # arquivo, não pipe: um pipe cheio travaria o curl
        proc = subprocess.Popen(cmd, cwd=trabalho, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=erros)
        try:
            proc.stdin.write(config.encode())
            proc.stdin.close()
            while True:
                terminou = proc.poll() is not None
                if os.path.exists(status):
                    with open(status, "rb") as f:
                        f.seek(lido)
                        novo = f.read()
                    lido += len(novo)
                    *linhas, resto = (resto + novo).split(b"\n")
                    for linha in linhas:
                        nome, code, exitcode = linha.decode(errors="replace").strip().split("\t")
                        entry, path = por_nome.pop(nome)
                        tmp = os.path.join(trabalho, nome)
                        if code == "200" and exitcode == "0":
                            checar_resposta(code)
                            entrega.entregar(entry, tmp, path)
                            continue
                        corpo = b""
                        if os.path.exists(tmp):
                            with open(tmp, "rb") as f:
                                corpo = f.read(256 * 1024)
                            _apagar(tmp)
                        negada = code in ("403", "429")
                        if negada:  # possível bloqueio: nenhuma requisição a mais antes de decidir
                            proc.kill()
                            proc.wait()
                        # página de bloqueio: pausa e testa; Bloqueado se confirmar
                        if checar_resposta(code, corpo, entry[1]):
                            repetir.append((entry, path))
                        elif exitcode != "0" or code in ("429", "500", "502", "503", "504"):
                            motivos[path] = f"HTTP {code}" + (f", {descrever_curl(exitcode)}" if exitcode != "0" else "")
                            retentar.append((entry, path))
                        else:
                            progresso(f"erro (HTTP {code})", entry[1])
                        if negada:
                            interrompido = True  # o resto da fatia volta para a fila
                            break
                entrega.tentar_presos()
                if interrompido or terminou:
                    break
                # disco no limite: SemEspaco; o finally encerra o curl e apaga só o arquivo pela metade
                # (os que já terminaram foram finalizados acima)
                espaco_disco.checar(outdir)
                _parada.wait(0.2)
                checar_parada()  # outra thread confirmou um bloqueio
                if pausado():  # outra thread/linha viu um bloqueio: encerra o curl e espera a pausa lá fora
                    interrompido = True
                    break
                if len(fatia) > PAGE_SIZE and coordenacao.outra_esperando():
                    _log(f"\n  >>> Outra linha de execução pediu vez: o resto deste perfil sai em fatias de "
                         f"{PAGE_SIZE}, alternando com as outras linhas.")
                    interrompido = True
                    break
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            for nome in por_nome:  # o que estava em andamento quando o curl foi interrompido
                _apagar(os.path.join(trabalho, nome))
            _apagar(status)
        if proc.returncode and not lido and not interrompido:
            erros.seek(0)
            raise RuntimeError(f"curl falhou (código {proc.returncode}): {erros.read().decode(errors='replace').strip()}")
    if interrompido:
        return retentar, repetir + list(por_nome.values())
    for _, path in por_nome.values():
        motivos[path] = f"o curl terminou sem baixar ({descrever_curl(proc.returncode)})"
    return retentar + list(por_nome.values()), []  # sem linha de status = curl morreu antes: tenta de novo


def _rodar_ffmpeg(cmd, tmp, outdir):
    """Roda o ffmpeg conferindo o espaço livre a cada segundo. Se o disco chegar ao limite (ou vier
    Ctrl+C), o ffmpeg é encerrado e o arquivo pela metade é apagado. Devolve o código de saída."""
    proc = subprocess.Popen(cmd)
    try:
        while True:
            try:
                return proc.wait(1)
            except subprocess.TimeoutExpired:
                espaco_disco.checar(outdir)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
            _apagar(tmp)


def download_all(entries, outdir, links_only=False, ceder_vez=None, contexto=None):
    """Grava links.txt e baixa tudo no ritmo global. Retorna (ok, pulados, falhas).

    ceder_vez: função opcional; enquanto devolver True, o lote é dividido em fatias de PAGE_SIZE
    fotos para que outra thread (ex.: a listagem do próximo perfil) consiga vagas no RITMO entre
    uma fatia e outra. O mesmo vale enquanto houver outra linha de execução ativa. Sem nenhuma das
    duas coisas, o perfil inteiro sai de um único processo curl.
    contexto: campos para o log de erros (perfil, site_id...), onde vão as mídias que falharam.
    Levanta espaco_disco.SemEspaco se o disco de `outdir` chegar ao limite de segurança."""
    os.makedirs(outdir, exist_ok=True)
    links_path = os.path.join(outdir, "links.txt")
    with open(links_path, "w", encoding="utf-8") as f:
        f.writelines(e[1] + "\n" for e in entries)
    _log(f"{len(entries)} mídias encontradas. Links em {links_path}")
    if links_only:
        return 0, 0, 0

    alvos = [(e, media_path(e, outdir)) for e in entries]
    skipped = len(alvos)
    # uma leitura da pasta em vez de uma consulta por foto (no G: do Google Drive cada consulta é lenta)
    with os.scandir(outdir) as it:
        existentes = {a.name for a in it if a.is_file() and a.stat().st_size > 0}
    alvos = [(e, p) for e, p in alvos if os.path.basename(p) not in existentes]
    skipped -= len(alvos)
    if alvos:
        estimativa = f", tempo estimado {RITMO.estimar(len(alvos))}" if RITMO.rps else ""
        _log(f"  {len(alvos)} a baixar ({skipped} já existem){estimativa}")
    contagem = {"ok": 0, "falhas": 0}
    falhas, motivos = [], {}

    def progresso(status, info=None):
        if status == "ok":
            contagem["ok"] += 1
        else:
            contagem["falhas"] += 1
            falhas.append(f"{info}  ->  {status}")
            _log(f"\n  {status}: {info}")
        feitos = skipped + contagem["ok"] + contagem["falhas"]
        resta = f"  resta {RITMO.estimar(len(entries) - feitos)}" if RITMO.rps and feitos < len(entries) else ""
        print(f"\r  [{feitos}/{len(entries)}] ok={contagem['ok']} pulados={skipped} falhas={contagem['falhas']}{resta}   ",
              end="", file=sys.stderr, flush=True)

    hls = [a for a in alvos if a[0][4]]
    diretos = [a for a in alvos if not a[0][4]]
    trabalho = _pasta_trabalho(outdir) if alvos else outdir
    entrega = _Entrega(progresso)
    if diretos:
        _checar_curl()
    for rodada in range(3):  # erros de rede/5xx voltam para uma nova rodada, com espera crescente
        if not diretos:
            break
        if rodada:
            _log(f"\n  {len(diretos)} com erro temporário; nova tentativa em {10 * rodada} s")
            _parada.wait(10 * rodada)
        pendentes, diretos = diretos, []
        while pendentes:
            dividir = (ceder_vez and ceder_vez()) or coordenacao.outras_linhas()
            n = PAGE_SIZE if dividir else len(pendentes)
            fatia, pendentes = pendentes[:n], pendentes[n:]
            espaco_disco.checar(outdir)
            fim = RITMO.reservar(len(fatia))  # durante uma pausa de bloqueio, espera aqui
            retentar, repetir = _baixar_fatia(fatia, outdir, trabalho, entrega, progresso, motivos)
            if repetir:
                RITMO.liberar(fim)  # o curl foi interrompido: as vagas que sobraram voltam
                if coordenacao.outra_esperando():
                    _parada.wait(0.5)  # a próxima vaga é de quem pediu primeiro
            diretos += retentar
            pendentes = repetir + pendentes
    for entry, path in diretos:
        progresso(f"erro (falhou após 3 rodadas; última: {motivos.get(path, '?')})", entry[1])

    for entry, path in hls:
        if not shutil.which("ffmpeg"):
            progresso("erro (vídeo HLS precisa de ffmpeg)", entry[1])
            continue
        espaco_disco.checar(outdir)
        RITMO.reservar()
        tmp = os.path.join(trabalho, os.path.basename(path) + ".part.mp4")
        codigo = _rodar_ffmpeg(["ffmpeg", "-loglevel", "error", "-y", "-i", entry[1], "-c", "copy", tmp],
                               tmp, outdir)
        if codigo == 0:
            entrega.entregar(entry, tmp, path)
        else:
            progresso(f"erro (ffmpeg {codigo})", entry[1])
    entrega.esperar()  # só os que ainda estão presos por outro programa (normalmente nenhum)
    if alvos:
        print(file=sys.stderr)
    if falhas:
        log_erros.registrar(f"{len(falhas)} de {len(entries)} mídias não baixaram", **(contexto or {}),
                            pasta=outdir, falhas="\n".join(falhas),
                            acao="o perfil não entrou no registro: na próxima execução só as mídias que "
                                 "faltam são baixadas")
    return contagem["ok"], skipped, contagem["falhas"]


# ---------------------------------------------------------------- linha de comando

def rps_arg(texto):
    v = float(texto.replace(",", "."))
    if v < 0:
        raise argparse.ArgumentTypeError("precisa ser >= 0")
    return v


def add_rede_args(ap):
    ap.add_argument("--rps", type=rps_arg, default=RPS_PADRAO,
                    help="limite global de requisições por segundo (padrão %(default)s; 0 = sem limite)")
    ap.add_argument("--pausa-bloqueio", type=rps_arg, default=PAUSA_PADRAO_MIN, metavar="MIN",
                    help="ao ver a página de bloqueio, pausa tudo por MIN minutos e faz 1 requisição de "
                         f"teste antes de desistir (padrão %(default)s; até {MAX_PAUSAS} pausas por "
                         "execução; 0 = para no primeiro bloqueio)")


def aplicar_rede_args(args):
    global _pausa_min
    RITMO.rps = args.rps
    _pausa_min = args.pausa_bloqueio


def main():
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Baixa a galeria de um perfil VSCO (baixa qualidade, ~300 px, por padrão).")
    ap.add_argument("perfil", help="username ou URL (ex.: isahevangelista ou https://vsco.co/isahevangelista/gallery)")
    ap.add_argument("-o", "--out", help="pasta de saída (padrão: <pasta de pasta_destino.py>/<username>; "
                                        "relativa = dentro da pasta padrão)")
    add_rede_args(ap)
    espaco_disco.add_args(ap)
    ap.add_argument("--links-only", action="store_true", help="só grava links.txt, sem baixar")
    ap.add_argument("--original", action="store_true", help="baixa a resolução original (padrão: ~300 px)")
    ap.add_argument("--forcar", action="store_true", help="baixa mesmo que o perfil já esteja no registro")
    ap.add_argument("--registro", default=ARQUIVO_PADRAO, help="arquivo de perfis acessados (padrão: %(default)s)")
    args = ap.parse_args()
    aplicar_rede_args(args)
    espaco_disco.aplicar_args(args)

    m = re.search(r"vsco\.co/([^/?#]+)", args.perfil)
    username = m.group(1) if m else args.perfil.strip("/")
    outdir = Destino(args.out or username).pasta()
    _log(f"Disco: {espaco_disco.resumo(outdir)}")
    espaco_disco.checar(outdir)  # já abaixo do limite: nem abre o perfil

    try:
        site_id, token = load_profile(username)
    except (LookupError, RuntimeError) as ex:
        if perfil_apagado(ex):  # ErroHTTP 404: sem site_id não dá para gravar no registro; sai com 0
            pular_apagado(ex, username)
            return
        log_erros.registrar(f"{titulo_erro_perfil(ex, 'erro ao abrir o perfil')}: {username}", ex=ex,
                            perfil=username, acao="perfil pulado (não entrou no registro)")
        sys.exit(f"Erro: {ex}\n  (registrado em {log_erros.ARQUIVO})")
    # outra linha de execução baixando o mesmo perfil agora: pula (quando ela terminar, estará no registro)
    if not coordenacao.reservar_perfil(site_id):
        _log(f"Perfil {username} já está sendo baixado por outra linha de execução; pulado.")
        return
    with RegistroPerfis(args.registro) as registro:
        if site_id in registro and not args.forcar:
            _log(f"Perfil {username} já está em {args.registro}; pulado (use --forcar para baixar de novo).")
            return
        _log(f"Perfil {username} (site_id={site_id}) — listando mídias...")
        try:
            entries = collect_entries(site_id, token, username, largura=None if args.original else LARGURA_MINIMA)
        except (RuntimeError, ValueError) as ex:  # ValueError = JSON inválido na resposta da API
            if perfil_apagado(ex):  # HTTP 404 da API: vai para o registro (ver registrar_apagado); sai com 0
                pular_apagado(ex, username, site_id, None if args.links_only else registro)
                coordenacao.soltar_perfil(site_id)  # depois de registrar, como abaixo
                return
            log_erros.registrar(f"{titulo_erro_perfil(ex, 'erro ao listar as mídias')}: {username}", ex=ex,
                                perfil=username, site_id=site_id, acao="perfil pulado (não entrou no registro)")
            sys.exit(f"Erro ao listar as mídias: {ex}\n  (registrado em {log_erros.ARQUIVO})")
        ok, skipped, failed = download_all(entries, outdir, args.links_only,
                                           contexto={"perfil": username, "site_id": site_id})
        # só registra quando nada falhou: assim uma próxima execução ainda completa o que faltou
        if not args.links_only and not failed:
            registro.registrar(site_id, username, "baixado" if entries else "vazio", len(entries))
        coordenacao.soltar_perfil(site_id)  # depois de registrar: a outra linha já o encontra no registro
    if failed:
        sys.exit(f"Concluído em {outdir}, mas {failed} mídia(s) falharam (detalhes em {log_erros.ARQUIVO}).")
    _log(f"Concluído em {outdir}")


def cleanup():
    coordenacao.sair()  # solta as vagas e os perfis desta linha (encerrada à força, eles expiram sozinhos)
    if os.path.exists(COOKIE_JAR):
        os.remove(COOKIE_JAR)
    shutil.rmtree(PASTA_TRABALHO, ignore_errors=True)


def rodar(main_fn):
    """Executa o main tratando o bloqueio (mensagem clara e código de saída SAIDA_BLOQUEIO), o disco
    no limite de segurança (SAIDA_SEM_ESPACO) e os erros inesperados (traceback na tela e no log de
    erros, código 1)."""
    if hasattr(signal, "SIGBREAK"):  # Ctrl+Break (é o que o painel.py envia para parar) = Ctrl+C
        signal.signal(signal.SIGBREAK, signal.default_int_handler)
    try:
        coordenacao.iniciar()  # esta é uma linha de execução: divide o ritmo e o bloqueio com as outras
        main_fn()
    except Bloqueado as ex:
        _log(mensagem_bloqueio(ex))
        sys.exit(SAIDA_BLOQUEIO)
    except espaco_disco.SemEspaco as ex:
        _log(espaco_disco.mensagem(ex))
        sys.exit(SAIDA_SEM_ESPACO)
    except KeyboardInterrupt:
        _log("\nInterrompido. Rode de novo para continuar de onde parou.")
        sys.exit(130)
    except Exception as ex:
        traceback.print_exc()
        log_erros.registrar("erro inesperado", ex=ex, pilha=True, acao="script encerrado com código 1")
        sys.exit(f"  (registrado em {log_erros.ARQUIVO})")
    finally:
        cleanup()


if __name__ == "__main__":
    rodar(main)
