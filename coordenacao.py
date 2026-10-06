"""
Coordenação entre linhas de execução: as abas Linha 1 e Linha 2 do painel, ou dois terminais
rodando os scripts ao mesmo tempo.

Cada linha é um processo à parte (vsco_dl.py / vsco_search_dl.py), mas todas saem do mesmo
computador e do mesmo IP: para o Cloudflare do vsco.co elas são uma execução só. Por isso, o que já
valia entre as threads de um processo passa a valer entre os processos:

  - Ritmo (--rps): um limite só para o computador inteiro. As linhas dividem as mesmas vagas: duas
    linhas a 1,5 req/s somam 1,5 req/s, e não 3 (ver vsco_dl.Ritmo).
  - Vez: enquanto houver outra linha ativa, os lotes de fotos saem em fatias de PAGE_SIZE e as vagas
    se alternam entre as linhas. Um lote grande que já estava rodando quando a outra linha pediu
    vaga é interrompido e dividido (ver outra_esperando): a outra espera no máximo uma fatia.
  - Pausa de bloqueio: quando uma linha vê a página de bloqueio, todas param. A que viu conduz a
    pausa e a requisição de teste; as outras esperam o resultado.
  - Bloqueio confirmado: todas as linhas em andamento param com o código 3.
  - Perfil em andamento: duas linhas nunca baixam o mesmo perfil ao mesmo tempo (reservar_perfil).
    A que chega depois pula o perfil; quando a outra terminar, ele estará no registro.
  - Arquivos compartilhados (registro de perfis, log de erros): gravados um bloco por vez
    (exclusivo), para os de uma linha nunca se misturarem com os da outra.

O estado fica num JSON pequeno na pasta temporária do usuário (ESTADO), lido e gravado sob uma trava
de arquivo (msvcrt no Windows, fcntl nos outros sistemas). Cada processo dá sinal de vida a cada
SINAL_S segundos; o que um processo encerrado à força deixou para trás (vagas, pausa, perfis) deixa
de valer ATIVA_S segundos depois. Se a pasta temporária não puder ser usada, o estado fica só na
memória e cada processo segue sozinho, como antes.
"""
import contextlib
import copy
import json
import os
import sys
import tempfile
import threading
import time

PASTA = os.path.join(tempfile.gettempdir(), "vsco_linhas")
ESTADO = os.path.join(PASTA, "estado.json")
TRAVA = os.path.join(PASTA, "estado.lock")
SINAL_S = 5  # intervalo do sinal de vida de cada processo
ATIVA_S = 20  # sem sinal de vida há mais que isso: o processo foi encerrado (ou travou)
ESPERA_S = 2  # um pedido de vaga recusado há menos que isso = a linha está esperando a vez
PAUSA_TESTE_S = 120  # folga depois do fim de uma pausa, para a requisição de teste (curl --max-time 60)
ESPERA_TRAVA_S = 10  # a trava só é segurada por instantes; mais que isso = algo deu errado: segue sem ela
EU = str(os.getpid())
INICIO = time.time()

_lock = threading.RLock()
_memoria = {}  # o estado, quando a pasta temporária não pode ser usada
_fd = None
_dentro = 0  # exclusivo() aninhado na mesma thread: a trava de arquivo é pega uma vez só
_travado = False  # se a trava de arquivo está com este processo agora
_sem_arquivo = False
_cache = [0.0, {}]  # [quando, estado] da última leitura (ver _consultar)
_iniciado = _saindo = False

if os.name == "nt":
    import msvcrt

    def _tentar_travar(fd):
        os.lseek(fd, 0, os.SEEK_SET)  # msvcrt.locking trava a partir da posição atual
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _soltar(fd):
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _tentar_travar(fd):
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _soltar(fd):
        fcntl.flock(fd, fcntl.LOCK_UN)


def _travar(fd):
    """Espera a trava de arquivo (o sistema a solta sozinho se o processo que a tem morrer). Devolve
    False se ela não veio em ESPERA_TRAVA_S: aí segue sem ela, em vez de travar a linha inteira."""
    limite = time.monotonic() + ESPERA_TRAVA_S
    while True:
        try:
            _tentar_travar(fd)
            return True
        except OSError:
            if time.monotonic() > limite:
                print(f"  aviso: a trava de {TRAVA} não veio em {ESPERA_TRAVA_S} s; seguindo sem ela.",
                      file=sys.stderr)
                return False
            time.sleep(0.005)


def _abrir():
    global _fd, _sem_arquivo
    if _fd is None and not _sem_arquivo:
        try:
            os.makedirs(PASTA, exist_ok=True)
            _fd = os.open(TRAVA, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0))
        except OSError as ex:
            _sem_arquivo = True
            print(f"  aviso: não consegui usar {PASTA} para coordenar as linhas de execução ({ex}); "
                  "esta linha segue sozinha (sem dividir o ritmo com outra).", file=sys.stderr)
    return _fd


@contextlib.contextmanager
def exclusivo():
    """Trava entre threads e entre processos. Usada aqui para o estado e, por fora, para gravar os
    arquivos que todas as linhas compartilham (registro, log de erros) um bloco por vez.
    Devolve False se só há a trava entre threads (pasta temporária inutilizável)."""
    global _dentro, _travado
    with _lock:
        fd = _abrir()
        if not _dentro:
            _travado = fd is not None and _travar(fd)
        _dentro += 1
        try:
            yield fd is not None
        finally:
            _dentro -= 1
            if not _dentro and _travado:
                _travado = False
                try:
                    _soltar(fd)
                except OSError:
                    pass


def _tentar(fn):
    """No Windows, um antivírus ou o indexador às vezes está com o arquivo aberto naquele instante."""
    for tentativa in range(50):
        try:
            return fn()
        except FileNotFoundError:
            raise
        except OSError:
            if tentativa == 49:
                raise
            time.sleep(0.02)


def _ler():
    """O estado gravado; None se o arquivo não pôde ser lido (aí nada é gravado por cima)."""
    def ler():
        with open(ESTADO, "rb") as f:
            return f.read()
    try:
        dados = _tentar(ler)
    except FileNotFoundError:
        return {}
    except OSError:
        return None
    try:
        st = json.loads(dados or b"{}")
    except ValueError:
        return {}  # ficou pela metade (processo encerrado à força enquanto gravava)
    return st if isinstance(st, dict) else {}


def _gravar(texto):
    def gravar():
        with open(ESTADO, "w", encoding="utf-8") as f:
            f.write(texto)
    try:
        _tentar(gravar)
    except OSError:
        pass  # fica para a próxima alteração


def _ativos(st, agora):
    return {pid for pid, t in st.get("linhas", {}).items() if agora - t <= ATIVA_S}


def _limpar(st, agora):
    """Esquece o que processos encerrados à força deixaram para trás."""
    ativos = _ativos(st, agora) | {EU}
    st["linhas"] = {pid: t for pid, t in st.get("linhas", {}).items() if pid in ativos}
    st["esperando"] = {pid: t for pid, t in st.get("esperando", {}).items()
                       if pid in ativos and agora - t <= ESPERA_S}
    st["perfis"] = {sid: pid for sid, pid in st.get("perfis", {}).items() if pid in ativos}
    p = st.get("pausa")
    if p and (p["pid"] not in ativos or agora > p["ate"] + PAUSA_TESTE_S):
        del st["pausa"]
    if st.get("dono") not in ativos and st.get("prox", 0) > agora:
        st["prox"] = agora  # vagas reservadas por um processo que morreu: ficam livres


@contextlib.contextmanager
def _estado():
    """O estado compartilhado, sob a trava. O que o bloco alterar é gravado."""
    with exclusivo() as arquivo:
        st = _ler() if arquivo else _memoria
        gravar = arquivo and st is not None
        if st is None:  # não deu para ler: trabalha sobre a última cópia, sem gravar
            st = copy.deepcopy(_cache[1])
        antes = json.dumps(st, sort_keys=True)
        _limpar(st, time.time())
        yield st
        depois = json.dumps(st, sort_keys=True)
        if depois != antes:
            _cache[0] = 0.0
            if gravar:
                _gravar(depois)


def _consultar():
    """Estado só para leitura, lido há no máximo 0,2 s (as esperas em laço consultam várias vezes por segundo)."""
    with _lock:
        if time.monotonic() - _cache[0] > 0.2:
            with _estado() as st:
                _cache[:] = [time.monotonic(), copy.deepcopy(st)]
        return _cache[1]


# ---------------------------------------------------------------- linhas ativas

def iniciar():
    """Registra este processo como uma linha ativa e começa o sinal de vida (uma thread)."""
    global _iniciado
    if _iniciado:
        return
    _iniciado = True
    sinal()
    threading.Thread(target=_sinal_de_vida, daemon=True).start()


def _sinal_de_vida():
    while not _saindo:
        time.sleep(SINAL_S)
        if not _saindo:
            sinal()


def sinal():
    with _estado() as st:
        st["linhas"][EU] = time.time()


def sair():
    """Fim do processo: sai das linhas ativas, devolve as vagas que reservou e solta os perfis."""
    global _saindo
    _saindo = True
    with _estado() as st:
        agora = time.time()
        st["linhas"].pop(EU, None)
        st["esperando"].pop(EU, None)
        st["perfis"] = {sid: pid for sid, pid in st["perfis"].items() if pid != EU}
        if (st.get("pausa") or {}).get("pid") == EU:
            del st["pausa"]
        if st.get("dono") == EU and st.get("prox", 0) > agora:
            st["prox"] = agora


def outras_linhas():
    """Quantas outras linhas (processos) estão ativas agora."""
    st = _consultar()
    return len(_ativos(st, time.time()) - {EU})


def outra_esperando():
    """True se outra linha está esperando vaga atrás de uma reserva grande desta (ver reservar)."""
    agora = time.time()
    return any(pid != EU and agora - t <= ESPERA_S for pid, t in _consultar().get("esperando", {}).items())


# ---------------------------------------------------------------- ritmo

def reservar(segundos, teto):
    """Reserva `segundos` de vagas a partir da próxima vaga livre (de todas as linhas). Devolve
    (início, fim) em time.time().

    Devolve None, sem reservar, se a próxima vaga livre está a mais de `teto` segundos por causa de
    outra linha (um lote grande dela, que começou quando esta linha ainda não existia): esta linha
    fica marcada como esperando, a outra divide o lote (outra_esperando) e quem chamou tenta de novo
    em instantes."""
    with _estado() as st:
        agora = time.time()
        st["linhas"][EU] = agora
        inicio = max(agora, st.get("prox", 0))
        if inicio - agora > teto and st.get("dono") != EU:
            st["esperando"][EU] = agora
            return None
        st["esperando"].pop(EU, None)
        st["prox"], st["dono"] = inicio + segundos, EU
        return inicio, inicio + segundos


def liberar(fim, folga):
    """Devolve as vagas que sobraram de uma reserva interrompida, se ninguém reservou depois dela.
    `folga` (1/rps) separa a próxima requisição da última que saiu."""
    with _estado() as st:
        if st.get("dono") == EU and abs(st.get("prox", 0) - fim) < 1e-3:
            st["prox"] = min(fim, time.time() + folga)


def empurrar(segundos):
    """Conta uma requisição feita fora das vagas (a de teste da pausa): empurra a próxima vaga."""
    with _estado() as st:
        st["prox"], st["dono"] = max(st.get("prox", 0), time.time()) + segundos, EU


# ---------------------------------------------------------------- pausa e bloqueio

def iniciar_pausa(minutos):
    """Esta linha vai conduzir uma pausa de bloqueio. Devolve False se outra linha já está pausando:
    aí é só esperar o resultado dela (pausa_alheia)."""
    with _estado() as st:
        p = st.get("pausa")
        if p and p["pid"] != EU:
            return False
        st["pausas"] = st.get("pausas", 0) + 1
        st["pausa"] = {"pid": EU, "ate": time.time() + minutos * 60, "n": st["pausas"]}
        return True


def encerrar_pausa():
    with _estado() as st:
        if (st.get("pausa") or {}).get("pid") == EU:
            del st["pausa"]


def pausa_alheia():
    """A pausa de bloqueio que outra linha está conduzindo agora ({"ate": time.time(), "n"}), ou None."""
    p = _consultar().get("pausa")
    return p if p and p["pid"] != EU else None


def pausas():
    """Quantas pausas de bloqueio (de qualquer linha) já começaram: muda quando começa uma nova."""
    return _consultar().get("pausas", 0)


def avisar_bloqueio(motivo):
    """Bloqueio confirmado por esta linha: as outras em andamento param também (bloqueio_alheio)."""
    with _estado() as st:
        st["bloqueio"] = {"pid": EU, "em": time.time(), "motivo": motivo}


def bloqueio_alheio():
    """Motivo do bloqueio que outra linha confirmou depois que este processo começou, ou None."""
    b = _consultar().get("bloqueio")
    return b["motivo"] if b and b["pid"] != EU and b["em"] >= INICIO else None


# ---------------------------------------------------------------- perfis em andamento

def reservar_perfil(site_id):
    """Marca o perfil como em andamento nesta linha. Devolve False se outra linha está com ele."""
    with _estado() as st:
        dono = st["perfis"].get(str(site_id))
        if dono and dono != EU:
            return False
        st["perfis"][str(site_id)] = EU
        st["linhas"][EU] = time.time()
        return True


def soltar_perfil(site_id):
    """Chamado depois de gravar o perfil no registro (ou de desistir dele)."""
    with _estado() as st:
        if st["perfis"].get(str(site_id)) == EU:
            del st["perfis"][str(site_id)]


def soltar_perfis():
    """Solta todos os perfis desta linha (fim de uma rodada, inclusive por erro)."""
    with _estado() as st:
        st["perfis"] = {sid: pid for sid, pid in st["perfis"].items() if pid != EU}
