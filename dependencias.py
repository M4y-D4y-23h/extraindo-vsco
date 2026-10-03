"""
Instala o que falta e atualiza as dependências do projeto. O Painel.bat roda isto toda vez, antes de
abrir o painel (o Python em si o Painel.bat instala/atualiza antes, porque este script precisa dele).

  python dependencias.py

  - Programas externos (PROGRAMAS abaixo): instalados/atualizados pelo winget (Gerenciador de
    Pacotes do Windows). Se o programa já existe mas não veio do winget (ex.: o curl do Windows),
    ele é usado como está, desde que atenda à versão mínima; senão, a versão do winget é instalada.
  - Bibliotecas Python (requirements.txt): instaladas/atualizadas pelo pip. Hoje não há nenhuma.

Para acrescentar uma dependência no futuro:
  - biblioteca Python: uma linha em requirements.txt (ex.: requests>=2.32);
  - programa externo: uma entrada em PROGRAMAS (comando, id no winget, para que serve, versão mínima).

Sem internet (ou sem winget), nada é instalado nem atualizado, mas o painel abre se o que é
obrigatório já estiver instalado.

Código de saída: 0 = pronto; 1 = falta algo obrigatório (o Painel.bat não abre o painel).

Os scripts chamam preparar_path() ao iniciar: um programa recém-instalado pelo winget só entra no
PATH das janelas abertas depois da instalação, e o curl do Windows (se for antigo) vem antes dele no PATH.
"""
import os
import re
import shutil
import subprocess
import sys

AQUI = os.path.dirname(os.path.abspath(__file__))
REQUISITOS = os.path.join(AQUI, "requirements.txt")


class Programa:
    def __init__(self, comando, winget, para, minimo=None, obrigatorio=True, opcao_versao="--version"):
        self.comando, self.winget, self.para = comando, winget, para
        self.minimo, self.obrigatorio, self.opcao_versao = minimo, obrigatorio, opcao_versao


PROGRAMAS = [
    Programa("curl", "cURL.cURL", "todas as requisições ao VSCO", minimo=(8, 3)),  # o mesmo de vsco_dl._checar_curl
    Programa("ffmpeg", "Gyan.FFmpeg", "os vídeos em HLS (.m3u8)", obrigatorio=False, opcao_versao="-version"),
]

WINGET_OPCOES = ["--exact", "--silent", "--disable-interactivity",
                 "--accept-package-agreements", "--accept-source-agreements"]
# Códigos de saída do winget que não são falha: 0x8A15002B = nenhuma atualização disponível;
# 0x8A150014 = pacote não encontrado entre os instalados (o programa não veio do winget).
WINGET_SEM_ERRO = {0, 0x8A15002B, 0x8A150014}


# ---------------------------------------------------------------- localizar programas

def _path_do_registro():
    """PATH gravado no registro (sistema + usuário), onde o winget e os instaladores acrescentam pastas.
    O PATH deste processo é o de quando a janela foi aberta e não enxerga essas mudanças."""
    if os.name != "nt":
        return []
    import winreg
    pastas = []
    for raiz, chave in ((winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
                        (winreg.HKEY_CURRENT_USER, "Environment")):
        try:
            with winreg.OpenKey(raiz, chave) as k:
                valor = winreg.QueryValueEx(k, "Path")[0]
        except OSError:
            continue
        pastas += winreg.ExpandEnvironmentStrings(valor).split(os.pathsep)
    return pastas


def _pastas():
    """Onde procurar: PATH atual, PATH do registro e as pastas de atalhos do winget (programas portáteis)."""
    pastas = os.environ.get("PATH", "").split(os.pathsep) + _path_do_registro()
    if os.name == "nt":
        pastas += [os.path.expandvars(p) for p in (r"%LOCALAPPDATA%\Microsoft\WinGet\Links",
                                                   r"%ProgramFiles%\WinGet\Links",
                                                   r"%ProgramFiles(x86)%\WinGet\Links")]
    return [p for p in pastas if p.strip()]


def _real(caminho):
    return os.path.normcase(os.path.realpath(caminho))


def versao(exe, opcao):
    """(texto da versão, (maior, menor)) a partir da 1ª linha de `exe opcao`; ("", None) se não rodar."""
    try:
        r = subprocess.run([exe, opcao], capture_output=True, text=True, errors="replace", timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return "", None
    linhas = (r.stdout or r.stderr).strip().splitlines()
    m = re.search(r"(\d+)\.(\d+)[\w.]*", linhas[0]) if linhas else None
    if not m:
        return (linhas[0][:60] if linhas else ""), None
    return m.group(0), (int(m.group(1)), int(m.group(2)))


def localizar(prog):
    """Caminho do primeiro `prog.comando` que atende à versão mínima, ou None."""
    vistos = set()
    for pasta in _pastas():
        exe = shutil.which(prog.comando, path=pasta)
        if not exe or _real(exe) in vistos:
            continue
        vistos.add(_real(exe))
        if prog.minimo is None or (versao(exe, prog.opcao_versao)[1] or (0,)) >= prog.minimo:
            return exe
    return None


def preparar_path():
    """Põe na frente do PATH deste processo (e dos que ele abrir) a pasta de cada programa de PROGRAMAS,
    quando o PATH atual acharia outro (mais antigo que o mínimo) ou nenhum."""
    for prog in PROGRAMAS:
        exe = localizar(prog)
        atual = shutil.which(prog.comando)
        if exe and not (atual and _real(atual) == _real(exe)):
            # pasta real do executável: em WinGet\Links fica só um atalho para ele
            os.environ["PATH"] = os.path.dirname(os.path.realpath(exe)) + os.pathsep + os.environ.get("PATH", "")


# ---------------------------------------------------------------- instalar / atualizar

def _winget(winget, acao, pacote, *extra):
    """Roda `winget <acao>` mostrando a saída dele na janela. Devolve o código de saída (sem sinal)."""
    r = subprocess.run([winget, acao, *WINGET_OPCOES, *extra, "--id", pacote])
    return r.returncode & 0xFFFFFFFF


def programa(prog, winget):
    """Instala/atualiza um programa. Devolve False só se ele é obrigatório e continua faltando."""
    nome = f"[{prog.comando}]"
    exige = f" (precisa de {prog.minimo[0]}.{prog.minimo[1]} ou mais novo)" if prog.minimo else ""
    exe = localizar(prog)
    if winget:
        if exe:
            print(f"\n{nome} procurando atualização (winget {prog.winget})...", flush=True)
            rc = _winget(winget, "upgrade", prog.winget)
        else:
            print(f"\n{nome} não encontrado{exige}: instalando (winget {prog.winget})...", flush=True)
            rc = _winget(winget, "install", prog.winget, "--scope", "user")
        if rc not in WINGET_SEM_ERRO:
            print(f"{nome} o winget terminou com erro 0x{rc:08X}.")
        exe = localizar(prog)
    if exe:
        print(f"{nome} ok: {versao(exe, prog.opcao_versao)[0] or '?'} — {exe}")
        return True
    if prog.obrigatorio:
        print(f"{nome} FALTA o {prog.comando}{exige}, necessário para {prog.para}.")
        return False
    print(f"{nome} ausente (opcional): sem ele, {prog.para} falham.")
    return True


def bibliotecas():
    """pip install --upgrade -r requirements.txt, se houver algum pacote listado. Devolve False se faltar algum."""
    try:
        with open(REQUISITOS, encoding="utf-8-sig") as f:
            pacotes = [l for l in (l.strip() for l in f) if l and not l.startswith("#")]
    except FileNotFoundError:
        pacotes = []
    nome = "[bibliotecas Python]"
    if not pacotes:
        print(f"\n{nome} nenhuma no requirements.txt.")
        return True
    print(f"\n{nome} instalando/atualizando o requirements.txt (pip)...", flush=True)
    pip = [sys.executable, "-m", "pip"]
    if subprocess.run(pip + ["--version"], capture_output=True).returncode != 0:
        subprocess.run([sys.executable, "-m", "ensurepip", "--upgrade"])
    # sem internet, o que já está instalado passa ("Requirement already satisfied") e só o que falta dá erro
    r = subprocess.run(pip + ["install", "--upgrade", "--disable-pip-version-check", "-r", REQUISITOS])
    if r.returncode == 0:
        print(f"{nome} ok")
        return True
    print(f"{nome} FALTA biblioteca do requirements.txt (veja a saída do pip acima).")
    return False


def main():
    print(f"[Python] ok: {sys.version.split()[0]} — {sys.executable}")
    try:
        import tkinter  # noqa: F401  (botão "Escolher pasta…" do painel)
        print("[tkinter] ok")
    except ImportError:
        print('[tkinter] ausente (opcional): o botão "Escolher pasta…" do painel não funciona. '
              "Ele vem no instalador do python.org (opção tcl/tk).")
    winget = shutil.which("winget") if os.name == "nt" else None
    if not winget:
        print("\nwinget não encontrado: só confiro o que já está instalado (nada é instalado nem atualizado).")
    ok = all([programa(p, winget) for p in PROGRAMAS])  # lista: roda todos mesmo se um faltar
    ok = bibliotecas() and ok
    if not ok:
        print("\nFalta dependência obrigatória (veja acima). Rode o Painel.bat de novo depois de resolver.")
        return 1
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
