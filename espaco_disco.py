"""
Limite de segurança de espaço em disco: os downloads param antes de o disco de destino encher.

Vale para qualquer disco onde a pasta de destino estiver: o do Windows (C:), outro HD, um pendrive,
um cartão de memória ou uma pasta de rede. Quem manda é sempre o disco da pasta onde o perfil está
sendo gravado naquele momento, então trocar a pasta durante a execução (pasta_destino.py) troca
também o disco vigiado.

  --espaco-minimo GB   espaço livre que nunca é usado (padrão 2; aceita 1,5; 0 = desliga)

O espaço livre é conferido:
  - no começo da execução (e de cada rodada da pesquisa): abaixo do limite, nada começa;
  - antes de cada perfil e de cada lote de fotos;
  - durante o download, várias vezes por segundo: o curl (ou o ffmpeg) é encerrado na hora e o
    arquivo pela metade é apagado.

Quando o espaço livre fica abaixo do limite, tudo para (exceção SemEspaco, código de saída 4, ver
vsco_dl.rodar): o perfil em andamento NÃO entra no registro, então na próxima execução as fotos que
já estão na pasta são puladas e só o resto é baixado. No painel, o código 4 para a lista e a repetição,
como o bloqueio (código 3): continuar só encheria o mesmo disco.

GB aqui é 1024³ bytes, a mesma conta do Explorer do Windows.
"""
import argparse
import os
import shutil

GB = 1024 ** 3
MB = 1024 ** 2
PADRAO_GB = 2  # padrão do --espaco-minimo e do painel
SAIDA_SEM_ESPACO = 4  # código de saída quando o espaço livre chega ao limite (painel e laços param)

_minimo = PADRAO_GB * GB  # bytes; 0 = desligado


class SemEspaco(Exception):
    """O espaço livre no disco de destino ficou abaixo do limite de segurança. Tudo para."""


def formatar(n):
    """Bytes em texto curto, com vírgula decimal (ex.: "1,98 GB", "350 MB"). Arredonda para baixo:
    1,999 GB livres aparece como 1,99 GB, nunca como 2,00 GB (o que pareceria acima de um limite de 2)."""
    if n >= GB:
        return f"{n * 100 // GB / 100:.2f}".replace(".", ",") + " GB"
    return f"{n // MB} MB"


def formatar_gb(gb):
    """Limite em GB como o usuário digitou (ex.: "2 GB", "1,5 GB")."""
    return f"{gb:.2f}".rstrip("0").rstrip(".").replace(".", ",") + " GB"


def _existente(caminho):
    """O próprio caminho ou a pasta acima mais próxima que já existe (a pasta pode ainda não ter
    sido criada; o espaço livre é o mesmo do disco dela)."""
    c = os.path.abspath(caminho)
    while not os.path.exists(c):
        pai = os.path.dirname(c)
        if pai == c:
            break
        c = pai
    return c


def disco(caminho):
    """Nome do disco do caminho, para as mensagens: "F:\\" no Windows (ou \\\\servidor\\pasta), o
    ponto de montagem nos outros sistemas."""
    c = _existente(caminho)
    if os.name == "nt":
        unidade = os.path.splitdrive(c)[0]
        return unidade + "\\" if unidade.endswith(":") else unidade or c
    while not os.path.ismount(c):
        c = os.path.dirname(c)
    return c


def uso(caminho):
    """(disco, livre, total) em bytes, ou None se o disco não estiver acessível (ex.: pendrive
    desconectado)."""
    try:
        u = shutil.disk_usage(_existente(caminho))
        return disco(caminho), u.free, u.total
    except OSError:
        return None


def minimo():
    """Limite atual em bytes (0 = desligado)."""
    return _minimo


def configurar(gb):
    global _minimo
    _minimo = int(gb * GB) if gb else 0


def falta(caminho, minimo_bytes=None):
    """Mensagem explicando que o disco de `caminho` está abaixo do limite, ou None se está tudo bem
    (ou o limite está desligado, ou o disco não está acessível: aí a gravação falha sozinha e o
    erro segue o caminho normal)."""
    limite = _minimo if minimo_bytes is None else minimo_bytes
    if not limite:
        return None
    u = uso(caminho)
    if not u or u[1] >= limite:
        return None
    local, livre, total = u
    return (f"restam {formatar(livre)} livres em {local} (de {formatar(total)}), "
            f"abaixo do limite de segurança de {formatar_gb(limite / GB)}")


def checar(caminho):
    """Levanta SemEspaco se o disco de `caminho` está abaixo do limite. Barato (uma chamada ao
    sistema), pode ser usado em laços de espera."""
    motivo = falta(caminho)
    if motivo:
        raise SemEspaco(motivo)


def resumo(caminho):
    """Linha informativa para o início da execução (ex.: "123,45 GB livres em F:\\ (para em 2 GB)")."""
    u = uso(caminho)
    if not u:
        return "não consegui ler o espaço livre do disco de destino"
    local, livre, total = u
    limite = f"para em {formatar_gb(_minimo / GB)}" if _minimo else "limite de segurança desligado"
    return f"{formatar(livre)} livres em {local} de {formatar(total)} ({limite})"


def mensagem(ex):
    return (f"\n*** ESPAÇO EM DISCO no limite de segurança: {ex}.\n"
            f"    Nada mais é baixado, para o disco não encher (um arquivo que estivesse pela metade foi\n"
            f"    apagado). O perfil em andamento NÃO foi registrado: na próxima execução os arquivos já\n"
            f"    baixados são pulados e o resto é retomado.\n"
            f"    Para continuar: libere espaço, troque a pasta de destino (no painel ou com\n"
            f"    python pasta_destino.py) ou diminua o limite (--espaco-minimo; 0 desliga) e rode de novo.")


def _gb_arg(texto):
    v = float(texto.replace(",", "."))
    if v < 0:
        raise argparse.ArgumentTypeError("precisa ser >= 0")
    return v


def add_args(ap):
    ap.add_argument("--espaco-minimo", type=_gb_arg, default=PADRAO_GB, metavar="GB",
                    help="para tudo (código 4) quando o espaço livre no disco da pasta de destino ficar "
                         "abaixo de GB gigabytes (padrão %(default)s; aceita 1,5; 0 = desliga)")


def aplicar_args(args):
    configurar(args.espaco_minimo)
