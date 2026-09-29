"""
Pasta de destino padrão dos downloads (pasta_destino.txt, ao lado dos scripts).

  - Sem -o, ou com -o relativo, os downloads vão para dentro desta pasta.
    Ex.: pasta = D:\\Fotos VSCO  ->  D:\\Fotos VSCO\\busca_isabela\\<username>\\
  - Com -o absoluto (ex.: -o E:\\outra), a pasta fica fixa naquela execução e este arquivo é ignorado.
  - Sem o arquivo, vale a pasta de onde o script é executado (comportamento antigo).

Mudar durante uma execução: o arquivo é relido antes de CADA perfil. Rode este script (ou edite o
.txt) em outro terminal e o próximo perfil já vai para a pasta nova; o perfil em andamento termina
onde começou. Se a pasta nova não puder ser criada (ex.: pendrive desconectado), o script avisa e
continua na anterior.

Uso:
  python pasta_destino.py                      # mostra a pasta atual
  python pasta_destino.py "D:\\Fotos VSCO"      # define a pasta padrão (cria se não existir)
  python pasta_destino.py --limpar             # apaga a definição (volta para a pasta atual do terminal)
"""
import os
import sys
import time

_AQUI = os.path.dirname(os.path.abspath(__file__))
ARQUIVO = os.path.join(_AQUI, "pasta_destino.txt")


def _normalizar(caminho, relativo_a):
    c = os.path.expandvars(os.path.expanduser(caminho.strip().strip('"').strip("'")))
    return os.path.normpath(os.path.join(relativo_a, c))  # join ignora relativo_a se c for absoluto


def ler():
    """Pasta definida em pasta_destino.txt (absoluta), ou None. Caminho relativo = relativo aos scripts."""
    try:
        with open(ARQUIVO, "rb") as f:
            dados = f.read()
    except FileNotFoundError:
        return None
    try:
        texto = dados.decode("utf-8-sig")  # -sig: o Bloco de Notas às vezes grava BOM
    except UnicodeDecodeError:
        texto = dados.decode("mbcs" if os.name == "nt" else "latin-1", errors="replace")  # salvo como ANSI
    for linha in texto.splitlines():
        linha = linha.strip()
        if linha and not linha.startswith("#"):
            return _normalizar(linha, _AQUI)
    return None


def definir(caminho):
    """Grava a pasta padrão (criando-a). A troca é atômica: uma execução em andamento nunca lê o
    arquivo pela metade."""
    pasta = _normalizar(caminho, os.getcwd())
    os.makedirs(pasta, exist_ok=True)
    tmp = ARQUIVO + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("# Pasta base dos downloads. Relida antes de cada perfil: pode ser alterada durante a execução.\n")
        f.write(pasta + "\n")
    for tentativa in range(10):  # no Windows o replace falha se outro processo estiver lendo naquele instante
        try:
            os.replace(tmp, ARQUIVO)
            return pasta
        except PermissionError:
            if tentativa == 9:
                raise
            time.sleep(0.1)


class Destino:
    """Resolve a pasta de saída de uma execução, relendo pasta_destino.txt a cada chamada.

    `out` é o -o da linha de comando (ou o nome padrão, ex.: "busca_isabela"): se for absoluto, é
    usado como está e nunca muda; se for relativo, fica dentro da pasta de pasta_destino.txt."""

    def __init__(self, out="", log=None):
        self.out = out or ""
        self.fixa = os.path.isabs(os.path.expandvars(os.path.expanduser(self.out)))
        self.log = log or (lambda msg: print(msg, file=sys.stderr))
        self.atual = None

    def _base(self):
        if self.fixa:
            return _normalizar(self.out, os.getcwd())
        return _normalizar(self.out or ".", ler() or os.getcwd())

    def pasta(self, *sub):
        """Pasta para o próximo perfil (ex.: destino.pasta(username)). Na primeira chamada, um erro
        ao criar a pasta é levantado; depois disso, avisa e continua na pasta anterior."""
        base = self._base()
        if base != self.atual:
            try:
                os.makedirs(base, exist_ok=True)
            except OSError as ex:
                if self.atual is None:
                    raise
                self.log(f"\n  aviso: não consegui usar a nova pasta {base} ({ex}); continuo em {self.atual}")
                return os.path.join(self.atual, *sub)
            if self.atual is not None:
                self.log(f"\n>>> Pasta de destino mudou: {self.atual}\n                         -> {base}")
            self.atual = base
        return os.path.join(self.atual, *sub)


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = sys.argv[1:]
    if args == ["--limpar"]:
        if os.path.exists(ARQUIVO):
            os.remove(ARQUIVO)
        print("Pasta padrão removida: os downloads vão para a pasta de onde o script for executado.")
    elif len(args) == 1 and not args[0].startswith("-"):
        print(f"Pasta padrão definida: {definir(args[0])}")
        print("Execuções em andamento passam a usá-la a partir do próximo perfil.")
    elif not args:
        pasta = ler()
        print(f"Pasta padrão: {pasta}" if pasta else
              f"Nenhuma pasta padrão definida: os downloads vão para a pasta atual do terminal ({os.getcwd()}).")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
