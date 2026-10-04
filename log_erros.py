"""
Log de erros (erros.log, ao lado dos scripts).

Só entram os erros, um bloco por erro, com tudo o que ajuda a entender o que houve: data/hora,
perfil, URL, código HTTP, saída do curl, trecho da resposta do site, o que a execução fez depois
e o comando que estava rodando. Erros inesperados levam também o traceback do Python.

Esses erros não param mais a execução: o perfil é pulado e o painel/pesquisa segue para o próximo
da fila. A exceção é o bloqueio do Cloudflare (código 3), que para tudo e não entra aqui.

Exemplo:

[2026-10-04 03:12:08 -0300] perfil apagado ou inexistente (HTTP 404): vdvdvdvdvdvdgdgg
    perfil:          vdvdvdvdvdvdgdgg
    erro:            ErroHTTP: HTTP 404 em https://vsco.co/vdvdvdvdvdvdgdgg/gallery
    url:             https://vsco.co/vdvdvdvdvdvdgdgg/gallery
    http:            404
    resposta:        página: ...
    o que foi feito: perfil pulado (não entrou no registro)
    comando:         vsco_dl.py --rps 1.5 --pausa-bloqueio 5 -- vdvdvdvdvdvdgdgg

Cada bloco é gravado de uma vez e salvo na hora. Se não der para gravar (disco cheio, arquivo
aberto/travado), o aviso vai para a tela e a execução continua.
"""
import os
import subprocess
import sys
import threading
import traceback
from datetime import datetime

ARQUIVO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "erros.log")
_lock = threading.Lock()


def formatar_comando(argv):
    """Linha de comando legível, sem o caminho do Python e com o --token escondido."""
    args, esconder = [], False
    for a in argv:
        if esconder:
            a, esconder = "***", False
        elif a == "--token":
            esconder = True
        elif a.startswith("--token="):
            a = "--token=***"
        args.append(a)
    if args and os.path.isabs(args[0]):
        args[0] = os.path.basename(args[0])
    return subprocess.list2cmdline(args)


def tamanho():
    """Tamanho atual do log (0 se ainda não existe): o painel usa para saber se o script registrou algo."""
    try:
        return os.path.getsize(ARQUIVO)
    except OSError:
        return 0


def registrar(titulo, *, ex=None, acao=None, pilha=False, comando=None, **campos):
    """Acrescenta um erro ao log.

    campos: informações do erro (perfil, site_id, url...), na ordem dada; None/"" são omitidos.
    ex: a exceção: tipo e mensagem e, se ela tiver campos_log() (ver vsco_dl.ErroHTTP), o código
        HTTP, a URL, a saída do curl e um trecho da resposta.
    acao: o que a execução fez depois do erro.
    pilha: inclui o traceback (para erros inesperados).
    comando: a linha de comando que falhou (padrão: a deste processo)."""
    if ex is not None:
        campos["erro"] = f"{type(ex).__name__}: {ex}"
        if hasattr(ex, "campos_log"):
            campos.update(ex.campos_log())
    campos["o que foi feito"] = acao
    campos["comando"] = comando or formatar_comando(sys.argv)
    if pilha and ex is not None:
        campos["traceback"] = "".join(traceback.format_exception(type(ex), ex, ex.__traceback__)).rstrip()
    campos = {k: str(v).strip() for k, v in campos.items() if v not in (None, "")}
    largura = max(len(k) for k in campos) + 1
    linhas = [f"[{datetime.now().astimezone():%Y-%m-%d %H:%M:%S %z}] {titulo}"]
    for nome, valor in campos.items():
        primeira, *resto = valor.splitlines() or [""]
        linhas.append(f"    {nome + ':':<{largura}} {primeira}")
        linhas.extend(" " * (largura + 5) + l for l in resto)
    bloco = "\n".join(linhas) + "\n\n"
    try:
        with _lock, open(ARQUIVO, "a", encoding="utf-8") as f:
            f.write(bloco)
    except OSError as erro:
        print(f"  aviso: não consegui gravar em {ARQUIVO} ({erro})", file=sys.stderr)
