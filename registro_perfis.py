"""
Registro persistente dos perfis já acessados (perfis_acessados.txt).

Formato do arquivo (texto, append-only, 1 perfil por linha, separado por TAB):
    site_id    username    data_utc    status    midias
Linhas começando com '#' são comentários. Linhas repetidas são inofensivas.

Por que não é só um set() de strings:
  O arquivo pode crescer para milhões de linhas. Um set de strings em Python gasta ~100 bytes
  por perfil; aqui cada perfil vira um hash de 64 bits guardado numa tabela hash compacta
  (array de inteiros de 8 bytes, endereçamento aberto), o que dá ~12–24 bytes por perfil.
  O arquivo é lido linha a linha (streaming), então nunca é carregado inteiro na memória.
  Ex.: 10 milhões de perfis ≈ 160–256 MB de RAM, contra ~1 GB+ com set de strings.
"""
import hashlib
import os
import threading
from array import array
from datetime import datetime, timezone

ARQUIVO_PADRAO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "perfis_acessados.txt")
CABECALHO = "# site_id\tusername\tdata_utc\tstatus\tmidias\n"


def _hash64(site_id):
    """Hash estável de 64 bits do site_id (nunca 0, que marca slot vazio)."""
    h = int.from_bytes(hashlib.blake2b(str(site_id).strip().encode(), digest_size=8).digest(), "little")
    return h or 1


class _HashSet64:
    """Conjunto de inteiros de 64 bits em um array('Q') com sondagem linear."""

    def __init__(self, capacidade=1024):
        self._tab = array("Q", bytes(8 * capacidade))
        self._mask = capacidade - 1
        self._n = 0

    def __len__(self):
        return self._n

    def _slot(self, h):
        i = h & self._mask
        tab = self._tab
        while tab[i] and tab[i] != h:
            i = (i + 1) & self._mask
        return i

    def __contains__(self, h):
        return self._tab[self._slot(h)] == h

    def add(self, h):
        i = self._slot(h)
        if self._tab[i] == h:
            return
        self._tab[i] = h
        self._n += 1
        if self._n * 3 > len(self._tab) * 2:  # carga > 2/3 -> dobra a tabela
            self._crescer()

    def _crescer(self):
        antiga = self._tab
        self._tab = array("Q", bytes(16 * len(antiga)))
        self._mask = len(self._tab) - 1
        for h in antiga:
            if h:
                self._tab[self._slot(h)] = h


class RegistroPerfis:
    """Consulta e grava perfis acessados. Seguro para uso entre threads."""

    def __init__(self, caminho=ARQUIVO_PADRAO):
        self.caminho = caminho
        self._ids = _HashSet64()
        self._lock = threading.Lock()
        if os.path.exists(caminho):
            with open(caminho, encoding="utf-8", errors="replace") as f:
                for linha in f:  # streaming: memória constante independente do tamanho do arquivo
                    if linha.startswith("#"):
                        continue
                    site_id = linha.split("\t", 1)[0].strip()
                    if site_id:
                        self._ids.add(_hash64(site_id))
        novo = not os.path.exists(caminho) or os.path.getsize(caminho) == 0
        self._f = open(caminho, "a", encoding="utf-8", newline="\n")
        if novo:
            self._f.write(CABECALHO)
            self._f.flush()

    def __len__(self):
        return len(self._ids)

    def __contains__(self, site_id):
        return _hash64(site_id) in self._ids

    def registrar(self, site_id, username, status, midias=0):
        data = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._lock:
            self._ids.add(_hash64(site_id))
            self._f.write(f"{site_id}\t{username}\t{data}\t{status}\t{midias}\n")
            self._f.flush()  # grava na hora: um Ctrl+C/queda não perde o que já foi baixado

    def fechar(self):
        self._f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.fechar()
