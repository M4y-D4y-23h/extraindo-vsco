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

Uso:
  python vsco_search_dl.py isa                    # 10 perfis com mídia -> ./busca_isa/<username>/
  python vsco_search_dl.py isa -n 25 -o saida --rps 1
  python vsco_search_dl.py isa --links-only       # só lista/gera links.txt de cada perfil
  python vsco_search_dl.py isa --forcar           # ignora o registro e baixa de novo

Pasta de destino: sem -o (ou com -o relativo) tudo vai para dentro da pasta definida com
  python pasta_destino.py "D:\\Fotos VSCO"
e ela é relida antes de cada perfil, então dá para trocar durante a execução (ver pasta_destino.py).
"""
import argparse
import json
import queue
import sys
import threading
import urllib.parse

from pasta_destino import Destino
from registro_perfis import ARQUIVO_PADRAO, RegistroPerfis
from vsco_dl import (LARGURA_MINIMA, RITMO, Bloqueado, add_rede_args, collect_entries, download_all,
                     aplicar_rede_args, fetch, load_profile, rodar)

SEARCH_API = "https://vsco.co/api/2.0/search/grids"
SEARCH_PAGE_SIZE = 20


def iter_search(query, token):
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Referer": f"https://vsco.co/search/people/{urllib.parse.quote(query)}",
    }
    page = 0
    while True:
        q = urllib.parse.urlencode({"query": query, "page": page, "size": SEARCH_PAGE_SIZE})
        results = json.loads(fetch(f"{SEARCH_API}?{q}", headers)).get("results", [])
        if not results:
            return
        yield from results
        page += 1


def main():
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Pesquisa perfis no VSCO e baixa as fotos dos que têm mídia.")
    ap.add_argument("termo", help="termo de pesquisa (ex.: isa)")
    ap.add_argument("-n", "--perfis", type=int, default=10, help="quantos perfis COM mídia baixar (padrão 10)")
    ap.add_argument("-o", "--out", help="pasta base (padrão: <pasta de pasta_destino.py>/busca_<termo>; "
                                        "relativa = dentro da pasta padrão)")
    add_rede_args(ap)
    ap.add_argument("--links-only", action="store_true", help="só gera links.txt de cada perfil, sem baixar")
    ap.add_argument("--original", action="store_true", help="baixa a resolução original (padrão: ~300 px)")
    ap.add_argument("--forcar", action="store_true", help="não pula perfis que já estão no registro")
    ap.add_argument("--registro", default=ARQUIVO_PADRAO, help="arquivo de perfis acessados (padrão: %(default)s)")
    args = ap.parse_args()
    aplicar_rede_args(args)
    with RegistroPerfis(args.registro) as registro:
        print(f"Registro: {len(registro)} perfis já acessados em {args.registro}", file=sys.stderr)
        if RITMO.rps:
            print(f"Ritmo: até {RITMO.rps:g} req/s (~{round(RITMO.rps * 3600)} por hora)", file=sys.stderr)
        pesquisar(args, registro)


def listar_perfis(args, registro, token, largura, fila, listando):
    """Produtor (thread): percorre a pesquisa e lista as mídias dos perfis novos, no máximo um perfil
    à frente do download. Só manda eventos para `fila`: quem imprime e grava no registro é o consumidor."""
    produzidos, seen = 0, set()
    try:
        for r in iter_search(args.termo, token):
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
                fila.put(("erro", username, ex))
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
            fila.join()  # espera o consumidor pegar este perfil antes de listar o próximo
        fila.put(("fim",))
    except BaseException as ex:
        fila.put(("excecao", ex))
    finally:
        listando.clear()


def pesquisar(args, registro):
    largura = None if args.original else LARGURA_MINIMA
    # relida antes de cada perfil: pasta_destino.py pode trocar a pasta durante a execução
    destino = Destino(args.out or f"busca_{args.termo}")
    print(f"Destino: {destino.pasta()}", file=sys.stderr)
    # Qualquer galeria pública serve para obter o cookie vs_app_id e o token anônimo.
    _, token = load_profile("vsco")

    fila = queue.Queue()
    listando = threading.Event()  # setado enquanto o produtor lista o próximo perfil
    threading.Thread(target=listar_perfis, args=(args, registro, token, largura, fila, listando),
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
                raise evento[1]
            if tipo == "conhecido":
                known += 1
            elif tipo == "erro":
                print(f"\n  {evento[1]} pulado: erro ao listar ({evento[2]})", file=sys.stderr)
                errors.append(evento[1])
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
                                                   ceder_vez=listando.is_set)
                if not args.links_only and not failed:
                    registro.registrar(site_id, username, "baixado", len(entries))
                done.append((username, len(entries), ok, skipped, failed))
    finally:
        print(f"\n===== Resumo ({destino.atual}) =====", file=sys.stderr)
        for username, total, ok, skipped, failed in done:
            print(f"  {username:<30} {total:>5} mídias  ok={ok} pulados={skipped} falhas={failed}", file=sys.stderr)
        print(f"  perfis já no registro pulados: {known}", file=sys.stderr)
        print(f"  perfis vazios pulados: {len(empty)} {empty}", file=sys.stderr)
        if errors:
            print(f"  perfis com erro pulados: {len(errors)} {errors}", file=sys.stderr)
    if len(done) < args.perfis:
        print(f"  aviso: a pesquisa acabou com só {len(done)} perfis com mídia.", file=sys.stderr)


if __name__ == "__main__":
    rodar(main)
