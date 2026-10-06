"""
Capacidade do computador: quantas linhas de execução (abas Linha 1, Linha 2...) o painel abre.

O painel mede a CPU e a RAM ao abrir e cria uma aba para cada linha que cabe nas duas (na CPU e na RAM):

  - CPU: uma linha por processador lógico. Medido: cada linha usa menos de 1% de um núcleo em média
    (o trabalho é quase todo esperar a rede), mas tem os seus próprios processos (Python, curl e, nos
    vídeos HLS, ffmpeg), com picos ao abrir cada um e ao antivírus conferir cada arquivo baixado.
    Uma linha por processador lógico deixa folga de sobra para esses picos e para o resto do Windows.
  - RAM: as linhas usam no máximo FRACAO_RAM da RAM livre no momento em que o painel abre (o resto
    fica para o Windows, o navegador e os outros programas), a MB_POR_LINHA cada. Medido por linha:
    script Python ~20 MB + curl ~11 MB + andamento no navegador até ~15 MB (4.000 linhas) = ~46 MB;
    MB_POR_LINHA soma a isso uma folga para o ffmpeg dos vídeos HLS.
  - Teto (MAXIMO_LINHAS): quem limita a velocidade é o site, não o computador. O ritmo (--rps) é um
    só para o computador inteiro e as linhas o dividem (ver coordenacao.py): mais abas que isso só
    deixariam cada uma mais lenta, sem baixar mais fotos por hora.
  - Piso: 1. O painel sempre abre com pelo menos uma linha.

Ex.: Core i5-3470 (4 processadores lógicos) com 4 GB de RAM e ~1,8 GB livres: CPU até 4, RAM até 5
-> 4 linhas.

A conta é feita uma vez, quando o painel abre. Para escolher na mão: python painel.py --linhas N
(ou Painel.bat --linhas N). Para só ver a conta: python capacidade.py
"""
import os
import re
import subprocess
import sys

import espaco_disco

MB = 1024 ** 2
MB_POR_LINHA = 80  # ~46 MB medidos + folga para o ffmpeg dos vídeos HLS
FRACAO_RAM = 0.25  # parte da RAM livre que as linhas podem usar
LINHAS_POR_PROCESSADOR = 1
MAXIMO_LINHAS = 8
MAXIMO_FORCADO = 32  # limite do --linhas


def processadores():
    """Processadores lógicos que este processo pode usar (núcleos x threads por núcleo)."""
    if hasattr(os, "process_cpu_count"):  # Python 3.13+: respeita a afinidade no Windows também
        n = os.process_cpu_count()
    elif hasattr(os, "sched_getaffinity"):
        n = len(os.sched_getaffinity(0))
    else:
        n = os.cpu_count()
    return max(1, n or 1)


def nome_cpu():
    """Nome do processador (ex.: "Intel(R) Core(TM) i5-3470 CPU @ 3.20GHz"), ou None."""
    try:
        if os.name == "nt":
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                return " ".join(winreg.QueryValueEx(k, "ProcessorNameString")[0].split())
        if os.path.exists("/proc/cpuinfo"):
            with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as f:
                for linha in f:
                    if linha.startswith("model name"):
                        return " ".join(linha.split(":", 1)[1].split())
        if sys.platform == "darwin":
            return subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True,
                                  text=True, timeout=5).stdout.strip() or None
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def memoria():
    """(total, livre) da RAM em bytes; None no que não der para medir."""
    try:
        if os.name == "nt":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            st = MEMORYSTATUSEX()
            st.dwLength = ctypes.sizeof(st)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return st.ullTotalPhys, st.ullAvailPhys
        elif os.path.exists("/proc/meminfo"):
            campos = {}
            with open("/proc/meminfo") as f:
                for linha in f:
                    m = re.match(r"(\w+):\s+(\d+) kB", linha)
                    if m:
                        campos[m.group(1)] = int(m.group(2)) * 1024
            return campos.get("MemTotal"), campos.get("MemAvailable")
        elif sys.platform == "darwin":
            total = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5)
            return int(total.stdout.strip()), None
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
        pass
    return None, None


def calcular(forcado=None):
    """Quantas linhas abrir, e o porquê. Devolve {"linhas", "automatico", "texto"}; `forcado` é o
    --linhas (None = pela capacidade)."""
    nucleos, cpu = processadores(), nome_cpu()
    total, livre = memoria()
    por_cpu = nucleos * LINHAS_POR_PROCESSADOR
    conta = livre if livre is not None else (total // 2 if total else None)  # sem a livre: metade da total
    por_ram = int(conta * FRACAO_RAM // (MB_POR_LINHA * MB)) if conta is not None else None
    candidatos = [("pela CPU", por_cpu), ("pela RAM", por_ram),
                  (f"pelo teto de {MAXIMO_LINHAS}: o ritmo do site é um só para todas", MAXIMO_LINHAS)]
    quem, n = min(((q, v) for q, v in candidatos if v is not None), key=lambda c: c[1])
    automatico = max(1, n)
    if not n:
        quem += "; pouca RAM livre, abre só o mínimo"

    def plural(k, palavra, sufixo="s"):
        return palavra + (sufixo if k != 1 else "")

    fmt = espaco_disco.formatar
    if total is None:
        ram = "não consegui medir"
    elif livre is None:
        ram = f"{fmt(total)} no total (a livre não deu para medir: contei a metade)"
    else:
        ram = f"{fmt(livre)} livres de {fmt(total)}"
    detalhe = (f"CPU: {cpu + ', ' if cpu else ''}{nucleos} {plural(nucleos, 'processador', 'es')} "
               f"{plural(nucleos, 'lógico')} -> até {por_cpu}; RAM: {ram}"
               + (f" -> até {por_ram}" if por_ram is not None else ""))
    if forcado:
        texto = (f"{forcado} {plural(forcado, 'linha')} de execução (escolhido com --linhas; pela capacidade "
                 f"deste computador seriam {automatico}). {detalhe}.")
    else:
        texto = f"{automatico} {plural(automatico, 'linha')} de execução para este computador (limitado {quem}). {detalhe}."
    return {"linhas": forcado or automatico, "automatico": automatico, "texto": texto}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(calcular()["texto"])
