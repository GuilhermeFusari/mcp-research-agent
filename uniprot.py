"""
Cliente do UniProt: toda a lógica de "falar com a API" fica aqui.

POR QUE SEPARAR DO SERVIDOR?
O servidor MCP (servidor.py) só "anuncia" as ferramentas para o Claude.
A lógica de negócio fica neste módulo, que não sabe nada de MCP. Vantagens:
  - dá para testar este arquivo sozinho, sem Claude e sem Inspector;
  - se um dia trocar MCP por uma API web ou um app, este código não muda.
"""

import asyncio
import re

import httpx

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

# API REST oficial do UniProt (pública, sem cadastro).
UNIPROT_API = "https://rest.uniprot.org/uniprotkb"

# Serviço de Peptide Search. É ASSÍNCRONO: você envia um "job", recebe um
# endereço e fica consultando esse endereço até o resultado ficar pronto.
PEPTIDE_API = "https://peptidesearch.uniprot.org/asyncrest"

# Campos que pedimos ao UniProt. Pedir só o necessário deixa a resposta
# menor e mais rápida (e gasta menos contexto do Claude).
CAMPOS = ",".join([
    "accession",             # ID único (ex.: P01308)
    "protein_name",          # nome recomendado
    "gene_names",            # genes (ex.: INS)
    "organism_name",         # organismo
    "length",                # tamanho da sequência (aminoácidos)
    "cc_function",           # texto sobre a função
    "cc_subcellular_location",  # onde fica na célula
    "xref_pdb",              # estruturas experimentais no PDB
])

# Os 20 aminoácidos padrão (código de 1 letra). Deixamos de fora U
# (selenocisteína) e O (pirrolisina), que são raros: assim palavras como
# "INSULIN" e "CYTOCHROME" não são confundidas com sequências.
AMINOACIDOS = set("ACDEFGHIKLMNPQRSTVWY")
NUCLEOTIDEOS = set("ACGTUN")  # N = base indefinida

# Formato oficial dos IDs (accessions) do UniProt, ex.: P01308, A0A023GPI8.
# Fonte: https://www.uniprot.org/help/accession_numbers
RE_ACCESSION = re.compile(r"[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9]([A-Z][A-Z0-9]{2}[0-9]){1,2}")

# Limites da heurística do modo "auto" (explicados em detectar_tipo).
MIN_TAM_SEQUENCIA = 8
# Tamanho mínimo aceito na busca por peptídeo, mesmo com tipo="sequencia".
# Peptídeos muito curtos aparecem em milhões de proteínas: o job não termina
# a tempo e o resultado não serviria para nada. Melhor recusar na hora.
MIN_TAM_PEPTIDEO = 5
MIN_TAM_NUCLEOTIDEO = 12

# Timeout: nunca deixar a ferramenta travar para sempre se a API cair.
# connect = tempo para abrir a conexão; o outro valor vale para ler a resposta.
TIMEOUT = httpx.Timeout(20.0, connect=10.0)
# Quantas vezes tentar uma requisição que falhou por motivo TEMPORÁRIO.
TENTATIVAS = 2
# Tempo máximo esperando o job do Peptide Search terminar.
PEPTIDE_ESPERA_MAX_S = 60


class ErroUniProt(Exception):
    """Erro "esperado" (API fora, nada encontrado...), com mensagem legível."""


async def _requisicao(client: httpx.AsyncClient, metodo: str, url: str, **kwargs) -> httpx.Response:
    """Faz a requisição HTTP tentando de novo em falhas temporárias.

    POR QUE ISSO EXISTE? APIs públicas oscilam: às vezes demoram, às vezes
    respondem 429 (muitas requisições) ou 5xx (erro no servidor delas).
    Esses erros costumam sumir sozinhos, então vale tentar de novo após uma
    pausa ("backoff"). Já um 4xx comum (ex.: 400 = pedido mal formado) não
    melhora tentando de novo, então devolvemos a resposta na hora.
    """
    for tentativa in range(1, TENTATIVAS + 1):
        try:
            r = await client.request(metodo, url, **kwargs)
            if r.status_code not in (429, 500, 502, 503, 504):
                return r
            motivo = f"HTTP {r.status_code}"
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            # str() de um timeout costuma vir vazio; usamos o nome da classe.
            motivo = type(e).__name__
        if tentativa < TENTATIVAS:
            await asyncio.sleep(2 * tentativa)  # espera 2 s, 4 s, ...

    servico = "Peptide Search" if "peptidesearch" in url else "UniProt"
    raise ErroUniProt(
        f"O serviço {servico} não respondeu após {TENTATIVAS} tentativas ({motivo}). "
        "Provavelmente é instabilidade do lado deles; tente de novo em alguns minutos."
    )


# ---------------------------------------------------------------------------
# 1) Roteador: descobrir o que o usuário mandou
# ---------------------------------------------------------------------------

def limpar_sequencia(texto: str) -> str:
    """Remove cabeçalho FASTA (linhas com '>'), espaços, quebras de linha e números.

    Sequências copiadas de artigos ou bancos costumam vir quebradas em linhas,
    com números de posição ou no formato FASTA:
        >sp|P01308|INS_HUMAN Insulin
        MALWMRLLPL LALLALWGPD ...
    """
    linhas = [l for l in texto.splitlines() if not l.strip().startswith(">")]
    return re.sub(r"[\s\d]", "", "".join(linhas)).upper()


def detectar_tipo(consulta: str) -> str:
    """Retorna 'nome', 'peptideo' ou 'nucleotideo'.

    HEURÍSTICA (e seus limites):
    - Começa com '>'  -> é FASTA, com certeza é sequência.
    - Só letras de nucleotídeo e longo -> DNA/RNA.
    - Só letras de aminoácido, >= 8 caracteres e em MAIÚSCULAS -> proteína/peptídeo.
    - Qualquer outra coisa (dígitos, hífens, letras como B/J/O/U/X) -> nome.

    O corte em 8 existe porque nomes curtos como 'CAT' ou 'INS' também são
    "sequências válidas". Palavras longas que só usam letras de aminoácido
    ainda podem enganar a heurística. Por isso a ferramenta aceita tipo
    explícito, e o Claude (que entende o contexto) pode passar o tipo certo.
    """
    texto = consulta.strip()
    if texto.startswith(">"):
        seq = limpar_sequencia(texto)
        return "nucleotideo" if set(seq) <= NUCLEOTIDEOS else "peptideo"

    # Juntamos sem espaços porque sequências coladas às vezes vêm em blocos
    # ("MALWMRLLPL LALLALWGPD").
    seq = "".join(texto.split()).upper()

    # Dígitos ou símbolos -> ID ou nome (P01308, IL-6, "TNF alpha 2").
    if not seq.isalpha():
        return "nome"
    if len(seq) >= MIN_TAM_NUCLEOTIDEO and set(seq) <= NUCLEOTIDEOS:
        return "nucleotideo"
    if len(seq) >= MIN_TAM_SEQUENCIA and set(seq) <= AMINOACIDOS:
        # Sequências quase sempre vêm em MAIÚSCULAS. Palavras comuns vêm em
        # minúsculas, e algumas só usam letras de aminoácido ("defensin"!).
        # Minúsculas só contam como sequência se forem longas demais para
        # ser uma palavra (>= 30 letras).
        if texto.isupper() or len(seq) >= 30:
            return "peptideo"
    return "nome"


# ---------------------------------------------------------------------------
# 2) Formatação: transformar o JSON do UniProt em texto enxuto
# ---------------------------------------------------------------------------

def _formatar_entrada(e: dict) -> str:
    """Converte uma entrada JSON do UniProt num bloco de texto curto.

    POR QUE NÃO DEVOLVER O JSON CRU? Ele é enorme e cheio de campos que não
    interessam. Texto resumido ocupa menos contexto do Claude e é mais fácil
    de ele usar na resposta.
    """
    acc = e.get("primaryAccession", "?")

    desc = e.get("proteinDescription", {})
    nome = (
        desc.get("recommendedName", {}).get("fullName", {}).get("value")
        or next(iter(desc.get("submissionNames", [])), {}).get("fullName", {}).get("value")
        or "(sem nome)"
    )
    genes = ", ".join(
        g["geneName"]["value"] for g in e.get("genes", []) if "geneName" in g
    ) or "-"
    organismo = e.get("organism", {}).get("scientificName", "-")
    tamanho = e.get("sequence", {}).get("length", "?")
    # entryType vem como "UniProtKB reviewed (Swiss-Prot)" ou "... unreviewed (TrEMBL)".
    revisada = "Swiss-Prot (revisada)" if "Swiss-Prot" in e.get("entryType", "") else "TrEMBL (não revisada)"

    # "comments" é uma lista de blocos de tipos diferentes; filtramos pelo tipo.
    # Uma proteína pode ter VÁRIOS blocos do mesmo tipo (ex.: um por isoforma),
    # então acumulamos todos. dict.fromkeys remove repetidos mantendo a ordem.
    funcoes, locais = [], []
    for c in e.get("comments", []):
        if c.get("commentType") == "FUNCTION":
            funcoes += [t["value"] for t in c.get("texts", [])]
        elif c.get("commentType") == "SUBCELLULAR LOCATION":
            locais += [
                s["location"]["value"]
                for s in c.get("subcellularLocations", [])
                if "location" in s
            ]
    funcao = " ".join(dict.fromkeys(funcoes)) or "-"
    # "; " porque um local pode ter vírgula dentro ("Nucleus, PML body").
    localizacao = "; ".join(dict.fromkeys(locais)) or "-"

    pdbs = [x["id"] for x in e.get("uniProtKBCrossReferences", []) if x.get("database") == "PDB"]
    estrutura = (
        f"{len(pdbs)} estrutura(s) no PDB (ex.: {', '.join(pdbs[:5])})" if pdbs
        else "sem estrutura experimental no PDB"
    )

    return (
        f"### {nome} — {acc}\n"
        f"- Genes: {genes}\n"
        f"- Organismo: {organismo}\n"
        f"- Tamanho: {tamanho} aa | Entrada: {revisada}\n"
        f"- Função: {funcao}\n"
        f"- Localização celular: {localizacao}\n"
        f"- Estrutura: {estrutura}; modelo previsto: https://alphafold.ebi.ac.uk/entry/{acc}\n"
        f"- Página: https://www.uniprot.org/uniprotkb/{acc}"
    )


# ---------------------------------------------------------------------------
# 3) Buscas
# ---------------------------------------------------------------------------

async def buscar_por_nome(
    client: httpx.AsyncClient, consulta: str, organismo: str | None, max_resultados: int
) -> str:
    """Busca textual (nome, gene, ID) no UniProtKB.

    Tenta do mais específico para o mais amplo e para no primeiro que achar:
      1. ID exato (se parece um accession, ex.: P01308)
      2. Gene exato (se é uma palavra só, ex.: TP53)
      3. Texto livre (qualquer campo)
    Tudo isso primeiro só entre entradas revisadas (Swiss-Prot, curadas por
    especialistas); se nada aparecer, repete incluindo as automáticas (TrEMBL).

    POR QUE? A busca textual ordena por "relevância", e aí "TP53" traz
    primeiro a MDM2, que só MENCIONA a p53 na descrição.
    """
    termos = []
    if RE_ACCESSION.fullmatch(consulta.upper()):
        termos.append(f"accession:{consulta.upper()}")
    elif len(consulta.split()) == 1:
        termos.append(f"gene_exact:{consulta}")
    termos.append(consulta)

    filtro_org = f' AND (organism_name:"{organismo}")' if organismo else ""
    resultados = []
    # Laço externo = revisadas primeiro. Assim uma entrada curada no texto
    # livre ganha de uma entrada automática que por acaso tem gene "insulin".
    for filtro_rev in (" AND (reviewed:true)", ""):
        for termo in termos:
            params = {
                "query": f"({termo}){filtro_org}{filtro_rev}",
                "fields": CAMPOS,
                "format": "json",
                "size": max_resultados,
            }
            r = await _requisicao(client, "GET", f"{UNIPROT_API}/search", params=params)
            r.raise_for_status()  # 4xx que sobrou vira exceção (tratada em buscar)
            resultados = r.json().get("results", [])
            if resultados:
                break
        if resultados:
            break

    if not resultados:
        raise ErroUniProt(f"Nenhuma proteína encontrada para '{consulta}'.")

    cab = f"Resultados do UniProt para '{consulta}' (busca por nome/gene/ID):\n\n"
    return cab + "\n\n".join(_formatar_entrada(e) for e in resultados)


async def buscar_por_peptideo(
    client: httpx.AsyncClient, sequencia: str, organismo: str | None, max_resultados: int
) -> str:
    """Encontra proteínas que CONTÊM exatamente a sequência (Peptide Search).

    Fluxo assíncrono do serviço:
      1. POST com a sequência -> resposta 202 com o endereço do job (header Location)
      2. GET no endereço:
           303 = ainda processando (tente de novo depois)
           200 = pronto; o corpo é a lista de IDs separados por vírgula
      3. Com os IDs em mãos, buscamos os detalhes no UniProt.
    """
    if len(sequencia) < MIN_TAM_PEPTIDEO:
        raise ErroUniProt(
            f"Sequência curta demais ({len(sequencia)} aa). Peptídeos com menos de "
            f"{MIN_TAM_PEPTIDEO} aminoácidos aparecem em milhões de proteínas, então a "
            "busca não é informativa. Use uma sequência maior ou busque por nome/gene."
        )

    # lEQi=on trata Leucina e Isoleucina como iguais (massa idêntica, útil em
    # espectrometria de massas). Deixamos off para correspondência exata.
    r = await _requisicao(
        client, "POST", f"{PEPTIDE_API}/",
        data={"peps": sequencia, "lEQi": "off", "spOnly": "off"},
    )
    if r.status_code != 202 or "Location" not in r.headers:
        raise ErroUniProt(f"Peptide Search recusou o pedido (HTTP {r.status_code}).")

    # O serviço devolve o endereço em http://; forçamos https.
    url_job = r.headers["Location"].replace("http://", "https://", 1)

    # Polling: consulta a cada 2 s até ficar pronto ou estourar o tempo.
    # asyncio.sleep (e não time.sleep) não bloqueia o servidor enquanto espera.
    espera = 0
    while True:
        r = await _requisicao(client, "GET", url_job, follow_redirects=False)
        if r.status_code == 200:
            break
        if r.status_code != 303:
            raise ErroUniProt(f"Erro no job do Peptide Search (HTTP {r.status_code}).")
        if espera >= PEPTIDE_ESPERA_MAX_S:
            raise ErroUniProt("Peptide Search demorou demais. Tente novamente mais tarde.")
        await asyncio.sleep(2)
        espera += 2

    ids = [i.strip() for i in r.text.split(",") if i.strip()]
    if not ids:
        raise ErroUniProt(
            f"Nenhuma proteína do UniProt contém exatamente a sequência {sequencia}. "
            "O Peptide Search só encontra correspondências exatas. Para sequências "
            "parecidas (não idênticas), seria preciso BLAST (ainda não implementado)."
        )

    # Busca detalhes. Se tem filtro de organismo, filtramos via query;
    # senão pegamos os primeiros IDs direto (endpoint /accessions).
    if organismo:
        consulta_ids = " OR ".join(f"accession:{i}" for i in ids[:200])
        params = {
            "query": f"({consulta_ids}) AND (organism_name:\"{organismo}\")",
            "fields": CAMPOS, "format": "json", "size": max_resultados,
        }
        r = await _requisicao(client, "GET", f"{UNIPROT_API}/search", params=params)
    else:
        params = {"accessions": ",".join(ids[:max_resultados]), "fields": CAMPOS, "format": "json"}
        r = await _requisicao(client, "GET", f"{UNIPROT_API}/accessions", params=params)
    r.raise_for_status()
    resultados = r.json().get("results", [])

    if not resultados:
        # Só acontece com filtro de organismo: a sequência existe, mas não nele.
        raise ErroUniProt(
            f"A sequência aparece em {len(ids)} proteína(s), mas nenhuma de '{organismo}'. "
            "Pode ser que nesse organismo a sequência tenha alguma diferença "
            "(a busca é exata). Tente sem o filtro de organismo."
        )

    cab = (
        f"A sequência {sequencia} ({len(sequencia)} aa) aparece exatamente em "
        f"{len(ids)} proteína(s) do UniProt. Mostrando {len(resultados)}:\n\n"
    )
    return cab + "\n\n".join(_formatar_entrada(e) for e in resultados)


# ---------------------------------------------------------------------------
# 4) Ponto de entrada único (é isto que o servidor MCP chama)
# ---------------------------------------------------------------------------

async def buscar(
    consulta: str, tipo: str = "auto", organismo: str | None = None, max_resultados: int = 5
) -> str:
    """Decide o tipo de busca e executa."""
    if tipo == "auto":
        tipo = detectar_tipo(consulta)

    if tipo == "nucleotideo":
        raise ErroUniProt(
            "Isso parece uma sequência de DNA/RNA. O UniProt é um banco de proteínas; "
            "traduza para aminoácidos primeiro (ou use tipo='sequencia' se for mesmo proteína)."
        )

    # Um único cliente HTTP por chamada, fechado automaticamente pelo "async with".
    # Ele reaproveita a conexão entre as várias requisições (polling, detalhes).
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            if tipo in ("peptideo", "sequencia"):
                return await buscar_por_peptideo(
                    client, limpar_sequencia(consulta), organismo, max_resultados
                )
            return await buscar_por_nome(client, consulta.strip(), organismo, max_resultados)
        except httpx.HTTPError as e:
            # Erros de rede/HTTP viram ErroUniProt para o servidor tratar de um jeito só.
            raise ErroUniProt(f"Falha ao acessar o UniProt: {type(e).__name__} {e}") from e


# Permite testar este arquivo sozinho:  python uniprot.py insulin
if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8")  # acentos corretos no terminal do Windows
    termo = " ".join(sys.argv[1:]) or "insulin"
    print(asyncio.run(buscar(termo)))
