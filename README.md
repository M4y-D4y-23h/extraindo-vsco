# Extraindo fotos do VSCO

Scripts em Python para baixar as fotos de galerias **públicas** do VSCO, tanto de um perfil
específico quanto dos perfis encontrados por uma pesquisa (a aba *Pessoas* de
`https://vsco.co/search/people/<termo>`).

Principais características:

- **Baixa qualidade por padrão**: cada foto vem com no mínimo 300 px de largura (~25 KB por foto;
  o CDN às vezes entrega um pouco mais, até ~480 px). Use `--original` para baixar a resolução cheia.
- **Registro de perfis acessados** (`perfis_acessados.txt`): perfis já baixados são pulados
  automaticamente nas próximas execuções, evitando downloads repetidos.
- **Ritmo global** (`--rps`, padrão 1,5 requisição/s): um único limite para todas as requisições
  (páginas, API e fotos), o que permite prever o tempo e rodar sem supervisão.
- **Retomada**: arquivos que já existem na pasta não são baixados de novo.
- **Pasta de destino padrão e trocável em execução** (`pasta_destino.py`): define onde tudo é salvo;
  o arquivo é relido antes de cada perfil, então dá para mudar a pasta com o script rodando.

---

## Requisitos

O **`Painel.bat` instala o que faltar e atualiza tudo a cada abertura** (ver abaixo). O projeto usa:

- Python 3.8+ (hoje só a biblioteca padrão; bibliotecas extras ficam no `requirements.txt`)
- `curl` **8.3 ou mais novo** (o script confere a versão)
- `ffmpeg` — opcional, só necessário para vídeos em HLS (`.m3u8`)

> Todas as requisições passam pelo `curl` porque o Cloudflare do vsco.co bloqueia o fingerprint
> TLS do Python (`urllib`/`requests` recebem 403).

### Instalação e atualização automática (`Painel.bat`)

Toda vez que o `Painel.bat` abre, antes do painel, ele usa o **winget** (Gerenciador de Pacotes do
Windows) e o `pip`:

1. **Python**: se não houver Python 3.8+, instala o pacote `Python.Python.3.14` do winget, só para o
   seu usuário. Se já houver, atualiza dentro da mesma versão (ex.: 3.14.6 → 3.14.7); se esse Python
   foi instalado para todos os usuários, a atualização pode pedir permissão de administrador.
2. **Programas** (`dependencias.py`, lista `PROGRAMAS`): `curl` (pacote `cURL.cURL`) e `ffmpeg`
   (pacote `Gyan.FFmpeg`). O que falta, ou está abaixo da versão mínima, é instalado; o que veio do
   winget é atualizado. Um programa que não veio do winget e atende à versão mínima (ex.: o `curl` do
   Windows) é usado como está.
3. **Bibliotecas Python** (`requirements.txt`): `pip install --upgrade -r requirements.txt`. Hoje o
   arquivo não lista nenhuma.

Se faltar algo obrigatório (Python 3.8+ ou `curl` 8.3+), a janela mostra o motivo e o painel não abre.
O `ffmpeg` é opcional: se não der para instalar, o painel abre e só os vídeos HLS falham. Sem internet
(ou sem winget), nada é instalado nem atualizado, mas o painel abre se o necessário já estiver instalado.

Um programa recém-instalado pelo winget só entra no PATH das janelas abertas depois. Por isso os
scripts procuram o `curl`/`ffmpeg` também no PATH gravado no Windows e nas pastas do winget, e usam o
primeiro que atende à versão mínima.

**Para acrescentar uma dependência no futuro:**

- biblioteca Python: uma linha no `requirements.txt` (ex.: `requests>=2.32`);
- programa externo: uma linha em `PROGRAMAS` no `dependencias.py`, com o comando, o id do pacote no
  winget, para que serve e a versão mínima (ex.: `Programa("curl", "cURL.cURL", "...", minimo=(8, 3))`).

Para só instalar/atualizar, sem abrir o painel: `python dependencias.py`.

---

## Arquivos

| Arquivo | O que faz |
|---|---|
| `vsco_dl.py` | Baixa a galeria de **um** perfil. Também é a biblioteca base usada pelo script de busca. |
| `vsco_search_dl.py` | Pesquisa um termo e baixa as fotos dos N primeiros perfis **novos** que tenham mídia. |
| `registro_perfis.py` | Lê/grava o registro de perfis acessados (`perfis_acessados.txt`). |
| `Painel.bat` / `painel.py` / `painel.html` | Painel no navegador para rodar tudo sem digitar comandos. |
| `dependencias.py` | Instala/atualiza as dependências (rodado pelo `Painel.bat`). Tem a lista dos programas externos. |
| `requirements.txt` | Bibliotecas Python do projeto, instaladas/atualizadas pelo `pip` (hoje nenhuma). |
| `pasta_destino.py` | Mostra/define a pasta padrão onde os downloads são salvos (`pasta_destino.txt`). |
| `pasta_destino.txt` | Criado por `pasta_destino.py`. Uma linha com a pasta base (pode ser editado no Bloco de Notas). |
| `perfis_acessados.txt` | Criado automaticamente na primeira execução. Histórico de perfis já processados. |
| `vsco_sessao.txt` | Opcional, criado por você. Token da sua sessão logada, usado só na pesquisa (ver *Erro de autenticação na pesquisa*).

---

## Uso

### Painel (o jeito mais simples)

Dê **dois cliques em `Painel.bat`** (ou rode `python painel.py`). O `Painel.bat` primeiro instala/atualiza
as dependências (ver *Requisitos*); o `python painel.py` abre direto. O navegador abre em
`http://127.0.0.1:8765` com tudo numa tela só:

- **Onde salvar**: mostra/troca a pasta padrão (digitando ou pelo botão *Escolher pasta…*) e abre a
  pasta no Explorer. Trocar durante um download vale a partir do próximo perfil.
- **O que baixar**: abas *Um perfil*, *Pesquisa* e *Lista* (carrega um `.txt` da pasta do projeto, como
  `variacoes_isabela.txt`, e roda um item por vez; dá para começar de um item específico).
  As *Opções avançadas* têm ritmo, pausa no bloqueio, subpasta, resolução original, `--forcar` e só links.
- **Iniciar / Parar agora / Parar após o item atual**, e o **andamento ao vivo** com a mesma saída dos scripts.

Detalhes:

- Deixe a janela preta do `Painel.bat` aberta; fechar ela encerra o painel (e o download em andamento).
- Só roda uma execução por vez: duas ao mesmo tempo dobrariam o ritmo de requisições.
- Numa lista, um bloqueio confirmado (código 3) interrompe a lista inteira e o painel diz de qual item retomar.
- *Pesquisa* e *Lista* de pesquisas se repetem sozinhas: ao terminar sem erro, o painel espera 10 s e roda
  tudo de novo, indefinidamente. Qualquer item que termine com erro encerra a repetição. Para parar antes:
  *Parar agora* ou *Parar após a rodada atual* / *Parar após o item atual*.
- O painel só aceita conexões do próprio computador. Se a porta 8765 estiver ocupada, ele usa a seguinte.

Os comandos abaixo continuam funcionando do mesmo jeito para quem preferir o terminal.

### Pasta de destino

```powershell
python pasta_destino.py                       # mostra a pasta padrão atual
python pasta_destino.py "D:\Fotos VSCO"       # define a pasta padrão (cria se não existir)
python pasta_destino.py --limpar              # remove: volta a salvar na pasta atual do terminal
```

| Como chama | Onde salva |
|---|---|
| sem `-o` | `<pasta padrão>\<username>` ou `<pasta padrão>\busca_<termo>\<username>` |
| `-o fotos` (relativo) | `<pasta padrão>\fotos\...` |
| `-o E:\outra` (absoluto) | `E:\outra\...`, fixo naquela execução (ignora a pasta padrão) |
| sem pasta padrão definida | pasta atual do terminal (como antes) |

**Trocar durante a execução**: com o script rodando, abra outro terminal e rode
`python pasta_destino.py "E:\Nova pasta"` (ou edite `pasta_destino.txt`). A pasta é relida antes de
cada perfil: o perfil em andamento termina na pasta antiga e o próximo já vai para a nova, com o aviso
`>>> Pasta de destino mudou`. Nos laços `foreach`, cada chamada também lê a pasta na hora. Se a pasta
nova não puder ser criada (disco desconectado, caminho inválido), o script avisa e continua na anterior.

> Se um perfil foi interrompido pela metade (Ctrl+C, bloqueio) e você trocar a pasta antes de retomá-lo,
> ele recomeça do zero na pasta nova: a retomada só enxerga os arquivos da pasta atual.

### Um perfil

```powershell
python vsco_dl.py isahevangelista                     # -> ./isahevangelista/
python vsco_dl.py https://vsco.co/isahevangelista/gallery -o fotos --rps 1
python vsco_dl.py isahevangelista --original          # resolução original
python vsco_dl.py isahevangelista --forcar            # baixa mesmo já estando no registro
python vsco_dl.py isahevangelista --links-only        # só gera links.txt, sem baixar
```

### Pesquisa de perfis

```powershell
python vsco_search_dl.py isabela                      # 10 perfis novos -> ./busca_isabela/<username>/
python vsco_search_dl.py isabela -n 25 -o saida --rps 1
python vsco_search_dl.py isabela --forcar             # ignora o registro
python vsco_search_dl.py isabela --links-only         # só lista/gera links.txt de cada perfil
python vsco_search_dl.py isabela --token "SEU_TOKEN"  # pesquisa com a sua sessão logada (ver abaixo)
python vsco_search_dl.py isabela --intervalo 30       # repete a cada 30 s em vez de 10 s
python vsco_search_dl.py isabela --uma-vez            # roda uma vez só, sem repetir
```

**Repetição automática**: ao terminar uma rodada sem erro, o script espera `--intervalo` segundos
(padrão 10) e roda a pesquisa de novo, com os mesmos valores, indefinidamente. Qualquer erro encerra a
repetição: perfil com erro ao listar, foto que falhou, erro de autenticação, bloqueio ou qualquer exceção.
`Ctrl+C` também encerra.

### Erro de autenticação na pesquisa

A pesquisa usa a API `/api/2.0/search/grids`. Se o VSCO passar a exigir login nela (HTTP 401/403 sem
a página do Cloudflare), o script tenta, nesta ordem:

1. **A sua sessão logada**, se você configurou um token (`--token`, variável `VSCO_TOKEN` ou o arquivo
   `vsco_sessao.txt`). É a alternativa completa: a pesquisa volta a paginar normalmente.
2. **O token anônimo**, como sempre fez.
3. **A própria página** `https://vsco.co/search/people/<termo>`: lê os links dos perfis no HTML. Só vem
   a primeira leva de resultados (o resto a página carrega com o scroll), então o `-n` pode não ser atingido.


**Como pegar o token da sua sessão** (a sua conta, no seu navegador):

1. Entre no `https://vsco.co` com a sua conta e abra `https://vsco.co/search/people/isabela`.
2. Aperte **F12** → aba **Rede** (*Network*) → digite `search` no filtro e recarregue a página (F5).
3. Clique numa requisição para `api/2.0/search/...` (ou outra para `vsco.co/api/...`) e, em
   **Cabeçalhos da solicitação**, copie o valor de `Authorization` (o texto depois de `Bearer`).
4. Crie `vsco_sessao.txt` ao lado dos scripts e cole o token numa linha (pode colar a linha
   `Authorization: Bearer ...` inteira, o script limpa).

O painel e os laços `foreach` passam a usar o arquivo automaticamente. Cuidados:

- O token dá acesso à sua conta: **não compartilhe** o arquivo. Ele está no `.gitignore`.
- Ele expira. Quando o script avisar *"o token da sessão foi recusado"*, copie um novo.
- O token só é usado na pesquisa; a listagem e o download das fotos continuam com o token anônimo e
  no mesmo ritmo (`--rps`). Um bloqueio do Cloudflare (código 3) **não** é problema de login: espere e
  rode de novo com um `--rps` menor.

### Usando o arquivo de variações

Cada linha de `variacoes_isabela.txt` pode ser usada como termo de pesquisa ou como username direto.
Graças ao registro, perfis que aparecem em mais de uma pesquisa só são baixados uma vez.
O `if ($LASTEXITCODE -eq 3) { break }` encerra o laço inteiro quando vier um bloqueio, em vez de
seguir para a próxima variação e continuar batendo no site.

```powershell
# como termo de pesquisa (5 perfis novos por variação)
foreach ($v in Get-Content variacoes_isabela.txt) {
    python vsco_search_dl.py $v --uma-vez -n 5 -o busca_isabela
    if ($LASTEXITCODE -eq 3) { break }
}

# como username exato (variações que não existem são reportadas e puladas)
foreach ($v in Get-Content variacoes_isabela.txt) {
    python vsco_dl.py $v -o "perfis/$v"
    if ($LASTEXITCODE -eq 3) { break }
}
```

### Opções

| Opção | Script | Descrição |
|---|---|---|
| `-o, --out` | ambos | Pasta de saída (padrão: `<pasta padrão>/<username>` ou `<pasta padrão>/busca_<termo>`; relativa fica dentro da pasta padrão) |
| `--rps N` | ambos | Limite global de requisições por segundo (padrão 1.5; aceita `1,5`; `0` = sem limite) |
| `--pausa-bloqueio MIN` | ambos | Minutos de pausa antes da requisição de teste quando vier bloqueio (padrão 5; `0` = para no primeiro bloqueio) |
| `-n, --perfis` | busca | Quantos perfis **novos com mídia** baixar (padrão 10) |
| `--original` | ambos | Baixa a resolução original em vez de ~300 px |
| `--forcar` | ambos | Não pula perfis que já estão no registro |
| `--registro ARQ` | ambos | Usa outro arquivo de registro (padrão: `perfis_acessados.txt` ao lado dos scripts) |
| `--links-only` | ambos | Só gera `links.txt`, sem baixar (não grava no registro) |
| `--token TOKEN` | busca | Token da sua sessão logada para a pesquisa (padrão: `VSCO_TOKEN` ou `vsco_sessao.txt`) |
| `--intervalo S` | busca | Segundos de espera entre uma rodada e a próxima (padrão 10) |
| `--uma-vez` | busca | Roda uma vez só, sem a repetição automática |

### Códigos de saída

| Código | Significado |
|---|---|
| 0 | Terminou (pode ter havido falhas pontuais, listadas na saída) |
| 1 | Erro (perfil inexistente, perfil já no registro, curl ausente, pesquisa exigindo login etc.). Na pesquisa, também quando uma rodada teve perfil com erro ao listar ou foto que falhou (encerra a repetição) |
| 3 | **Bloqueado pelo Cloudflare** (confirmado pela requisição de teste): tudo foi interrompido; rode de novo mais tarde |

---

## Ritmo, bloqueio e conexões

### Ritmo global (`--rps`)

Todas as requisições passam por um único limitador, compartilhado entre as threads: página do perfil,
API de listagem, pesquisa e fotos. As requisições começam espaçadas de `1/rps` segundos, então o tempo
é previsível. Cada perfil mostra a estimativa antes de baixar e o tempo restante durante o download:

```
  98 a baixar (0 já existem), tempo estimado ~1 min 05 s a 1.5 req/s
  [40/98] ok=40 pulados=0 falhas=0  resta ~39 s a 1.5 req/s
```

| `--rps` | Requisições/hora | 100 fotos | 1.000 fotos | Noite (8 h) |
|---|---|---|---|---|
| 0.5 | 1.800 | ~3 min 30 s | ~36 min | ~13.500 fotos |
| 1.5 (padrão) | 5.400 | ~1 min 10 s | ~12 min | ~40.000 fotos |
| 3 | 10.800 | ~35 s | ~6 min | ~80.000 fotos |

Os valores já contam a listagem (1 requisição a cada 14 fotos). Cada foto conta como 1 requisição,
mas passa por um redirecionamento (`im.vsco.co` → `img.vsco.co`), então o Cloudflare vê 2 respostas
HTTP por foto.

### Bloqueio

Cada resposta é conferida. Se vier a página de bloqueio do Cloudflare (*"Sorry, you have been
blocked"*, *"Attention Required!"*, desafio *"Just a moment..."* ou o limite *"You are being rate
limited"*), ou 5 respostas 403/429 seguidas (rede de segurança se a página mudar):

1. **Pausa**: todas as threads param na hora e o `curl` em andamento é encerrado. Nenhuma
   requisição sai durante a pausa (`--pausa-bloqueio`, padrão 5 min).
2. **Teste**: ao fim da pausa sai **uma** requisição, para a mesma URL que foi recusada.
3. **Decisão**:
   - se o teste passar, foi uma recusa isolada (acontece de vez em quando mesmo sem bloqueio de
     verdade): o download continua de onde parou, incluindo a foto recusada;
   - se o teste também for recusado, o bloqueio está confirmado: aparece a mensagem com o texto da
     página, o *Ray ID* e a URL, e o script sai com código **3**.

São no máximo 3 pausas por execução; depois disso, o próximo bloqueio encerra direto (sem pausa),
para uma execução noturna não ficar horas insistindo num site que bloqueia a toda hora. Com
`--pausa-bloqueio 0`, o script para já no primeiro bloqueio, sem teste.

```
!!! Página de bloqueio do Cloudflare: HTTP 403 "Sorry, you have been blocked" (Cloudflare Ray ID a4283b2b09becd97)
    em https://vsco.co/isahevangelista/gallery
    Todas as requisições estão pausadas por 5 min (pausa 1 de 3); às 01:31:41 faço UMA requisição de teste antes de decidir.
    Teste passou (HTTP 200): foi uma recusa isolada. Retomando.
```

```
*** BLOQUEADO pelo firewall do VSCO: HTTP 403 "Sorry, you have been blocked" (Cloudflare Ray ID a4283b2b09becd97)
    em https://vsco.co/isahevangelista/gallery
    confirmado: o teste após 5 min de pausa também foi recusado (HTTP 403 "Sorry, you have been blocked" (...))
    Todas as requisições foram interrompidas para não prolongar o bloqueio.
    ...
```

Quando o bloqueio se confirma, o perfil em andamento não entra no registro. Na próxima execução, as fotos que já estão na pasta são
puladas e só o resto é baixado. Erros que não são bloqueio (queda de rede, 5xx) continuam sendo
retentados, com espera.

### Conexões

O Cloudflare recusa o fingerprint TLS do Python, por isso tudo passa pelo `curl`. Antes, cada foto
abria um processo `curl` e uma conexão TLS nova. Agora as fotos de um perfil vão para **um único
processo** `curl`, em série, que reaproveita a conexão (keep-alive). O ritmo é aplicado pelo próprio
curl (`--rate`), e o resultado de cada arquivo é lido na hora, para poder interromper no bloqueio.
Resultado: 2 conexões por lote (uma para `im.vsco.co` e outra para `img.vsco.co`) em vez de 2 por foto.

### Listagem antecipada (só na pesquisa)

Enquanto um perfil baixa, `vsco_search_dl.py` já lista as mídias do próximo perfil novo (no máximo
um à frente). As duas tarefas dividem o **mesmo** limite: enquanto a listagem roda, o download
trabalha em fatias de 14 fotos e cede uma vaga entre elas. Quando a listagem termina, o resto do
perfil sai de um único `curl`. Assim a listagem só aproveita a folga e nunca passa do `--rps`. Com
ritmo baixo o ganho é pequeno, porque o limite já está ocupado. Com ritmo alto, a listagem (que
espera a resposta de cada página) deixa de atrasar o download.

---

## Como funciona

### 1. Acesso ao VSCO

1. `GET https://vsco.co/<user>/gallery` devolve o HTML com `window.__PRELOADED_STATE__`, um JSON
   com o `site_id` do perfil e um token público anônimo. A visita também seta o cookie `vs_app_id`.
2. A lista de mídias vem da mesma API usada pelo scroll infinito do site:
   `GET /api/3.0/medias/profile?site_id=<id>&limit=14&cursor=<cursor>` (com o token Bearer + cookie).
3. A pesquisa usa `GET /api/2.0/search/grids?query=<termo>&page=<n>&size=20`, que devolve os mesmos
   perfis, na mesma ordem, da aba *Pessoas* do site.

### 2. Qualidade das fotos

Cada foto tem uma `responsive_url` em `im.vsco.co`. Sem parâmetros ela redireciona para o arquivo
original; com `?w=<largura>` o CDN devolve uma versão redimensionada, arredondada para faixas fixas.
O script usa `?w=300` (constante `LARGURA_MINIMA` em `vsco_dl.py`): **300 px de largura é o mínimo**.
Como o CDN arredonda para as faixas dele, algumas fotos vêm um pouco maiores (até ~480 px de largura),
nunca menores. Para mudar o tamanho, basta alterar essa constante.

Medido em fotos reais:

| Pedido | Largura recebida | Média por foto |
|---|---|---|
| `?w=300` (padrão) | 300–480 px | ~25 KB |
| sem `?w` (`--original`) | resolução cheia (ex.: 1368×2048) | ~600 KB (1 foto medida) |

#### Espaço em disco estimado (padrão de 300 px)

| Fotos | Espaço aproximado |
|---|---|
| 100 | ~2,5 MB |
| 1.000 | ~25 MB |
| 10.000 | ~250 MB |
| 100.000 | ~2,5 GB |

Num perfil real de teste, 98 fotos ocuparam 2,39 MB (média de 24,9 KB). Vídeos não são
redimensionados e são baixados como vêm da API, então perfis com muitos vídeos ocupam bem mais.
A memória RAM praticamente não muda com o tamanho da foto: o `curl` grava cada arquivo direto no disco.

### 3. Registro de perfis (`perfis_acessados.txt`)

Arquivo texto, *append-only*, uma linha por perfil, separado por TAB:

```
# site_id	username	data_utc	status	midias
192595495	isahevangelista	2026-09-29T03:12:08Z	baixado	98
```

- **Chave**: o `site_id` (o username pode mudar, o `site_id` não).
- **Consulta**: antes de listar as mídias de um perfil o script verifica o registro. Se o perfil
  já está lá, é pulado sem nenhuma requisição extra e **não conta** para o `-n`: a pesquisa continua
  até encontrar N perfis novos.
- **O que entra no registro**:
  - `baixado` — perfil com todas as fotos baixadas sem falha;
  - `vazio` — perfil sem nenhuma mídia.
- **O que não entra**: perfis com erro de rede ou com alguma foto que falhou (para serem tentados de
  novo na próxima execução) e execuções com `--links-only`.
- Cada linha é gravada e salva em disco na hora (`flush`), então interromper com Ctrl+C não perde o
  que já foi processado.
- Para baixar um perfil de novo: use `--forcar` ou apague a linha dele no arquivo.
- Se você apagar as pastas baixadas e quiser recomeçar do zero, apague também o
  `perfis_acessados.txt` (ele é recriado na próxima execução); senão os perfis continuam sendo pulados.

#### Escalabilidade de memória

O registro pode crescer para milhões de linhas, por isso:

- O arquivo é lido **linha a linha** (nunca carregado inteiro na memória).
- Cada `site_id` vira um hash de 64 bits (BLAKE2b) guardado numa **tabela hash compacta** —
  um `array('Q')` com endereçamento aberto — em vez de um `set` de strings do Python.

| Perfis no registro | `set` de strings (aprox.) | Tabela compacta (aprox.) |
|---|---|---|
| 100 mil | ~10 MB | ~2 MB |
| 1 milhão | ~100 MB | ~16–24 MB |
| 10 milhões | ~1 GB+ | ~160–256 MB |

A consulta continua O(1). Medido: 200 mil perfis carregados em ~0,7 s com ~22 bytes por perfil.
A chance de colisão entre hashes de 64 bits é desprezível (≈ 3 em 1 milhão com 10 milhões de perfis).

### 4. Organização dos arquivos baixados

```
busca_isabela/
  <username>/
    links.txt                                   # URLs de todas as mídias do perfil
    2023-05-07_175813_658ccaf62699447e1b70665f.jpg
    ...
```

O nome de cada arquivo é `<data de captura>_<id da mídia>.<ext>` e a data de modificação do arquivo
é ajustada para a data da foto.
