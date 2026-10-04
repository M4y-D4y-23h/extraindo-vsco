"""
Pesquisa perfis no VSCO (mesmo resultado da aba https://vsco.co/search/people/<termo>) e baixa
todas as fotos dos N primeiros perfis que tenham mídia. Perfis vazios são pulados.

Como a aba de pesquisa funciona:
  - A página é Next.js (App Router). Cada card é um <article class="creator-module__*__card"> com
    <a href="https://vsco.co/<username>"> e até 3 <img> de prévia (im.vsco.co). Perfis sem fotos
    mostram só um ícone placeholder no lugar das imagens.
  - O scroll infinito da página chama uma Server Action (POST /search/people/<termo>, header
    Next-Action), cujo id muda a cada deploy — frágil para reproduzir fora do navegador.
  - O mesmo resultado, na mesma ordem, vem da API pública
        GET /api/2.0/search/grids?query=<termo>&page=<n>&size=20
    (mesmo token Bearer + cookie usados na galeria), que devolve siteId e siteSubDomain.
    É essa que usamos.
  - "Tem mídia?" é decidido pela própria API de galeria (/api/3.0/medias/profile): se a primeira
    página vem vazia (ou o perfil é privado/inacessível), o perfil é pulado.

Listagem antecipada:
  Enquanto as fotos de um perfil baixam, uma thread já lista as mídias do próximo perfil novo
  (no máximo um perfil à frente). As duas dividem o mesmo ritmo global (--rps): o download cede
  uma vaga a cada PAGE_SIZE fotos, então a listagem só aproveita a folga e o total nunca passa
  do limite. Quando a listagem termina, o resto do perfil sai de um único processo curl.

Registro de perfis (perfis_acessados.txt, ver registro_perfis.py):
  - Antes de listar as mídias de um perfil, o script consulta o registro; se o site_id já está lá,
    o perfil é pulado sem nenhuma requisição extra e NÃO conta para o -n (a busca segue até achar
    N perfis novos).
  - Perfis baixados sem falhas e perfis vazios entram no registro. Perfis com erro de rede ou com
    alguma foto que falhou não entram, para serem tentados de novo na próxima execução.
  - Se vier a página de bloqueio do Cloudflare, tudo pausa (--pausa-bloqueio, padrão 5 min) e sai
    uma requisição de teste. Se ela for recusada também, tudo para (código de saída 3); o perfil em
    andamento não entra no registro e é retomado na próxima execução.
  - As fotos vêm com ~300 px de largura (LARGURA_MINIMA); use --original para o arquivo cheio.
  - Limite de espaço em disco (--espaco-minimo, padrão 2 GB; ver espaco_disco.py): quando o disco da
    pasta de destino chega ao limite, tudo para (código de saída 4), inclusive a repetição.

Uso:
  python vsco_search_dl.py isa                    # 10 perfis com mídia -> ./busca_isa/<username>/
  python vsco_search_dl.py isa -n 25 -o saida --rps 1
  python vsco_search_dl.py isa --links-only       # só lista/gera links.txt de cada perfil
  python vsco_search_dl.py isa --forcar           # ignora o registro e baixa de novo
  python vsco_search_dl.py isa --uma-vez          # roda uma vez só, sem repetir

Repetição: ao terminar uma rodada, espera --intervalo segundos (padrão 10) e roda de novo,
  indefinidamente. Só o bloqueio (código 3), o disco no limite (código 4) e o Ctrl+C encerram. Os
  outros erros (perfil apagado ou com erro ao listar, foto que falhou, erro de autenticação, exceção)
  vão para erros.log com todos os detalhes (ver log_erros.py): o perfil com erro é pulado e a
  pesquisa segue para o próximo.
  Com --uma-vez, uma rodada com erro sai com código 1.

Pasta de destino: sem -o (ou com -o relativo) tudo vai para dentro da pasta definida com
  python pasta_destino.py "D:\\Fotos VSCO"
e ela é relida antes de cada perfil, então dá para trocar durante a execução (ver pasta_destino.py).
"""
import argparse
import json
import os
import queue
import re
import sys
import threading
import time
import traceback
import urllib.parse

import espaco_disco
import log_erros
from pasta_destino import Destino
from registro_perfis import ARQUIVO_PADRAO, RegistroPerfis
from vsco_dl import (LARGURA_MINIMA, RITMO, Bloqueado, add_rede_args, collect_entries, download_all,
                     aplicar_rede_args, fetch, load_profile, rodar, titulo_erro_perfil)

SEARCH_API = "https://vsco.co/api/2.0/search/grids"
SEARCH_PAGE = "https://vsco.co/search/people/"
SEARCH_PAGE_SIZE = 20
# Token da SUA sessão logada no vsco.co (ver README, "Erro de autenticação na pesquisa").
# Não vai para o git (.gitignore). Também pode vir de --token ou da variável VSCO_TOKEN.
ARQUIVO_SESSAO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vsco_sessao.txt")
# páginas que não são perfis, nos links da página de pesquisa
_NAO_PERFIS = {"search", "user", "feed", "about", "store", "api", "spaces", "journal", "discover",
               "static", "subscribe", "login", "signup", "account", "settings", "help", "legal", "careers"}


class ErroAutenticacao(Exception):
    """A API de pesquisa recusou o token (HTTP 401/403 que não é a página de bloqueio do Cloudflare)."""


def ler_token_sessao(args):
    """Token Bearer da sessão logada do usuário: --token, VSCO_TOKEN ou vsco_sessao.txt (nessa ordem)."""
    token = args.token or os.environ.get("VSCO_TOKEN")
    if not token and os.path.exists(ARQUIVO_SESSAO):
        with open(ARQUIVO_SESSAO, encoding="utf-8-sig") as f:
            linhas = [l.strip() for l in f if l.strip() and not l.lstrip().startswith("#")]
        token = linhas[0] if linhas else None
    if token:
        token = re.sub(r"^(authorization:\s*)?bearer\s+", "", token.strip(), flags=re.I)  # aceita colar o header inteiro
    return token or None


def iter_search(query, token):
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Referer": f"{SEARCH_PAGE}{urllib.parse.quote(query)}",
    }
    page = 0
    while True:
        q = urllib.parse.urlencode({"query": query, "page": page, "size": SEARCH_PAGE_SIZE})
        url = f"{SEARCH_API}?{q}"
        try:
            # 1 tentativa só: repetir um 401/403 de login não adianta e ainda somaria recusas seguidas,
            # o que dispara a pausa de bloqueio (LIMITE_NEGADAS). A página do Cloudflare vira Bloqueado lá dentro.
            corpo = fetch(url, headers, retries=1)
        except RuntimeError as ex:
            if re.match(r"HTTP (401|403)\b", str(ex)):
                raise ErroAutenticacao(str(ex)) from None
            corpo = fetch(url, headers)  # erro de rede/5xx: agora sim, com as tentativas normais
        results = json.loads(corpo).get("results", [])
        if not results:
            return
        yield from results
        page += 1


def iter_search_html(query):
    """Alternativa sem a API: lê os links de perfil da própria página https://vsco.co/search/people/<termo>
    (os cards são <a href="https://vsco.co/<username>">). Só vem a primeira leva de resultados, porque o
    resto é carregado pelo scroll infinito. O site_id de cada perfil sai da página da galeria dele."""
    html = fetch(f"{SEARCH_PAGE}{urllib.parse.quote(query)}").decode("utf-8", "replace")
    vistos = set()
    for username in re.findall(r'href="(?:https://vsco\.co)?/([A-Za-z0-9_.-]+)/?(?:gallery)?"', html):
        if username.lower() in _NAO_PERFIS or username in vistos or re.search(r"\.[a-z]{2,4}$", username):
            continue
        vistos.add(username)
        try:
            site_id, _ = load_profile(username)
        except (LookupError, RuntimeError, ValueError):
            continue  # link que não é perfil (ou perfil que sumiu)
        yield {"siteSubDomain": username, "siteId": site_id}


def iter_resultados(args, token_anonimo):
    """Resultados da pesquisa, tentando nesta ordem:
      1. API com o token da sessão logada do usuário (se ele configurou um);
      2. API com o token anônimo (o que o script sempre usou);
      3. HTML da página de pesquisa (primeira leva de resultados)."""
    token_sessao = ler_token_sessao(args)
    if token_sessao:
        print("Pesquisa: usando o token da sua sessão logada", file=sys.stderr)
        try:
            yield from iter_search(args.termo, token_sessao)
            return
        except ErroAutenticacao as ex:
            print(f"  aviso: o token da sessão foi recusado ({ex}); ele pode ter expirado.\n"
                  f"  Copie um novo (ver README) para {ARQUIVO_SESSAO}. Tentando sem ele...", file=sys.stderr)
    try:
        yield from iter_search(args.termo, token_anonimo)
        return
    except ErroAutenticacao as ex:
        print(f"  aviso: a API de pesquisa exigiu login ({ex}).\n"
              f"  Lendo a página de pesquisa direto (só a primeira leva de perfis). Para a pesquisa\n"
              f"  completa, salve o token da sua sessão em {ARQUIVO_SESSAO} (ver README).", file=sys.stderr)
    achou = False
    for r in iter_search_html(args.termo):
        achou = True
        yield r
    if not achou:
        raise ErroAutenticacao(
            "a pesquisa do VSCO exige login e a página de pesquisa não trouxe perfis.\n"
            f"  Salve o token da sua sessão logada em {ARQUIVO_SESSAO} (ver README, "
            "\"Erro de autenticação na pesquisa\") e rode de novo.")


def main():
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Pesquisa perfis no VSCO e baixa as fotos dos que têm mídia.")
    ap.add_argument("termo", help="termo de pesquisa (ex.: isa)")
    ap.add_argument("-n", "--perfis", type=int, default=10, help="quantos perfis COM mídia baixar (padrão 10)")
    ap.add_argument("-o", "--out", help="pasta base (padrão: <pasta de pasta_destino.py>/busca_<termo>; "
                                        "relativa = dentro da pasta padrão)")
    add_rede_args(ap)
    espaco_disco.add_args(ap)
    ap.add_argument("--links-only", action="store_true", help="só gera links.txt de cada perfil, sem baixar")
    ap.add_argument("--original", action="store_true", help="baixa a resolução original (padrão: ~300 px)")
    ap.add_argument("--forcar", action="store_true", help="não pula perfis que já estão no registro")
    ap.add_argument("--registro", default=ARQUIVO_PADRAO, help="arquivo de perfis acessados (padrão: %(default)s)")
    ap.add_argument("--token", help="token da sua sessão logada no vsco.co para a pesquisa "
                                    "(padrão: variável VSCO_TOKEN ou o arquivo vsco_sessao.txt)")
    ap.add_argument("--intervalo", type=float, default=10,
                    help="segundos de espera entre uma rodada e a próxima (padrão 10)")
    ap.add_argument("--uma-vez", action="store_true", help="roda uma vez só, sem repetir")
    args = ap.parse_args()
    if args.intervalo < 0:
        ap.error("--intervalo precisa ser >= 0")
    aplicar_rede_args(args)
    espaco_disco.aplicar_args(args)
    with RegistroPerfis(args.registro) as registro:
        print(f"Registro: {len(registro)} perfis já acessados em {args.registro}", file=sys.stderr)
        if RITMO.rps:
            print(f"Ritmo: até {RITMO.rps:g} req/s (~{round(RITMO.rps * 3600)} por hora)", file=sys.stderr)
        rodada = 1
        while True:
            if not args.uma_vez:
                print(f"\n##### Rodada {rodada} — {time.strftime('%d/%m %H:%M:%S')}", file=sys.stderr)
            acao = "rodada encerrada" + ("" if args.uma_vez else f"; nova rodada em {args.intervalo:g} s")
            try:
                ok = pesquisar(args, registro)
            except (Bloqueado, espaco_disco.SemEspaco):
                raise  # código 3 / 4: para tudo, inclusive a repetição
            except ErroAutenticacao as ex:
                print(f"\nErro de autenticação na pesquisa: {ex}", file=sys.stderr)
                log_erros.registrar(f'erro de autenticação na pesquisa "{args.termo}"', ex=ex,
                                    pesquisa=args.termo, acao=acao)
                ok = False
            except Exception as ex:
                traceback.print_exc()
                log_erros.registrar(f'erro inesperado na pesquisa "{args.termo}"', ex=ex, pilha=True,
                                    pesquisa=args.termo, acao=acao)
                ok = False
            if args.uma_vez:
                if not ok:
                    sys.exit(f"\nA pesquisa terminou com erro (detalhes em {log_erros.ARQUIVO}).")
                break
            if not ok:
                print(f"\nA rodada teve erros (detalhes em {log_erros.ARQUIVO}); a repetição continua.",
                      file=sys.stderr)
            print(f"\nPróxima rodada em {args.intervalo:g} s (Ctrl+C para parar).", file=sys.stderr)
            time.sleep(args.intervalo)
            rodada += 1


def listar_perfis(args, registro, token, largura, fila, listando, encerrar):
    """Produtor (thread): percorre a pesquisa e lista as mídias dos perfis novos, no máximo um perfil
    à frente do download. Só manda eventos para `fila`: quem imprime e grava no registro é o consumidor.
    `encerrar` é setado quando a rodada acaba (inclusive por erro): o produtor para no próximo perfil."""
    produzidos, seen = 0, set()
    try:
        for r in iter_resultados(args, token):
            if encerrar.is_set():
                return
            username, site_id = r.get("siteSubDomain"), r.get("siteId")
            if not username or not site_id or site_id in seen:
                continue
            seen.add(site_id)
            if site_id in registro and not args.forcar:
                fila.put(("conhecido",))
                continue
            avisos = []
            try:
                entries = collect_entries(site_id, token, username, largura=largura, log=avisos.append)
            except Bloqueado:
                raise
            except Exception as ex:
                fila.put(("erro", username, site_id, ex))
                continue
            if not entries:
                fila.put(("vazio", username, site_id))
                continue
            produzidos += 1
            mais = produzidos < args.perfis
            listando.clear()
            fila.put(("perfil", username, site_id, entries, avisos, mais))
            if not mais:
                break
            # = fila.join() (espera o consumidor pegar este perfil antes de listar o próximo), mas
            # desiste se a rodada acabou com erro: aí ninguém mais vai pegar nada da fila
            while fila.unfinished_tasks and not encerrar.wait(0.2):
                pass
            if encerrar.is_set():
                return
        fila.put(("fim",))
    except BaseException as ex:
        fila.put(("excecao", ex))
    finally:
        listando.clear()


def pesquisar(args, registro):
    """Uma rodada. Devolve False se algum perfil deu erro ao listar ou teve foto que falhou."""
    largura = None if args.original else LARGURA_MINIMA
    # relida antes de cada perfil: pasta_destino.py pode trocar a pasta durante a execução
    destino = Destino(args.out or f"busca_{args.termo}")
    print(f"Destino: {destino.pasta()}", file=sys.stderr)
    print(f"Disco: {espaco_disco.resumo(destino.atual)}", file=sys.stderr)
    espaco_disco.checar(destino.atual)  # já abaixo do limite: a rodada nem começa
    # Qualquer galeria pública serve para obter o cookie vs_app_id e o token anônimo.
    _, token = load_profile("vsco")

    fila = queue.Queue()
    listando = threading.Event()  # setado enquanto o produtor lista o próximo perfil
    encerrar = threading.Event()  # setado quando esta rodada acaba: o produtor não continua sozinho
    threading.Thread(target=listar_perfis, args=(args, registro, token, largura, fila, listando, encerrar),
                     daemon=True).start()

    done, empty, errors, known = [], [], [], 0
    try:
        while True:
            try:
                evento = fila.get(timeout=1)  # com prazo: no Windows um get sem prazo segura o Ctrl+C
            except queue.Empty:
                continue
            tipo = evento[0]
            if tipo == "perfil" and evento[5]:
                listando.set()  # antes do task_done: o produtor só volta a listar depois disto
            fila.task_done()
            if tipo == "fim":
                break
            if tipo == "excecao":
                raise evento[1]  # ErroAutenticacao, Bloqueado etc.: quem trata é o main
            if tipo == "conhecido":
                known += 1
            elif tipo == "erro":
                _, username, site_id, ex = evento
                print(f"\n  {username} pulado: erro ao listar ({ex})", file=sys.stderr)
                log_erros.registrar(f"{titulo_erro_perfil(ex, 'erro ao listar as mídias')}: {username}", ex=ex,
                                    pesquisa=args.termo, perfil=username, site_id=site_id,
                                    acao="perfil pulado (não entrou no registro); a pesquisa seguiu para o próximo")
                errors.append(username)
            elif tipo == "vazio":
                _, username, site_id = evento
                print(f"\n  {username} pulado: perfil sem mídia", file=sys.stderr)
                empty.append(username)
                if not args.links_only:
                    registro.registrar(site_id, username, "vazio")
            else:
                _, username, site_id, entries, avisos, _ = evento
                print(f"\n[{len(done) + 1}/{args.perfis}] {username} (site_id={site_id})", file=sys.stderr)
                for aviso in avisos:
                    print(aviso, file=sys.stderr)
                ok, skipped, failed = download_all(entries, destino.pasta(username), args.links_only,
                                                   ceder_vez=listando.is_set,
                                                   contexto={"pesquisa": args.termo, "perfil": username,
                                                             "site_id": site_id})
                if not args.links_only and not failed:
                    registro.registrar(site_id, username, "baixado", len(entries))
                done.append((username, len(entries), ok, skipped, failed))
    finally:
        encerrar.set()
        print(f"\n===== Resumo ({destino.atual}) =====", file=sys.stderr)
        for username, total, ok, skipped, failed in done:
            print(f"  {username:<30} {total:>5} mídias  ok={ok} pulados={skipped} falhas={failed}", file=sys.stderr)
        print(f"  perfis já no registro pulados: {known}", file=sys.stderr)
        print(f"  perfis vazios pulados: {len(empty)} {empty}", file=sys.stderr)
        if errors:
            print(f"  perfis com erro pulados: {len(errors)} {errors}", file=sys.stderr)
    if len(done) < args.perfis:
        print(f"  aviso: a pesquisa acabou com só {len(done)} perfis com mídia.", file=sys.stderr)
    return not errors and not any(failed for *_, failed in done)


if __name__ == "__main__":
    rodar(main)
