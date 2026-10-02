"""Cliente da API do UniProt: busca por nome/gene/ID e por sequência (Peptide Search)."""

import asyncio
import re

import httpx

from agente_pesquisa.contrato import ErroEsperado, Resultado
from agente_pesquisa.fontes.http import requisitar

UNIPROT_API = "https://rest.uniprot.org/uniprotkb"

PEPTIDE_API = "https://peptidesearch.uniprot.org/asyncrest"

CAMPOS = ",".join([
    "accession",
    "protein_name",
    "gene_names",
    "organism_name",
    "length",
    "cc_function",
    "cc_subcellular_location",
    "xref_pdb",
])

# Sem U/O (raros) de propósito: evita que palavras como "INSULIN" pareçam sequências.
AMINOACIDOS = set("ACDEFGHIKLMNPQRSTVWY")
NUCLEOTIDEOS = set("ACGTUN")

RE_ACCESSION = re.compile(r"[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9]([A-Z][A-Z0-9]{2}[0-9]){1,2}")

MIN_TAM_SEQUENCIA = 8
MIN_TAM_PEPTIDEO = 5
MIN_TAM_NUCLEOTIDEO = 12

TIMEOUT = httpx.Timeout(20.0, connect=10.0)
TENTATIVAS = 2
PEPTIDE_ESPERA_MAX_S = 60


def limpar_sequencia(texto: str) -> str:
    """Remove cabeçalho FASTA (linhas com '>'), espaços, quebras de linha e números."""
    linhas = [l for l in texto.splitlines() if not l.strip().startswith(">")]
    return re.sub(r"[\s\d]", "", "".join(linhas)).upper()


def detectar_tipo(consulta: str) -> str:
    """Retorna 'nome', 'peptideo' ou 'nucleotideo'."""
    texto = consulta.strip()
    if texto.startswith(">"):
        seq = limpar_sequencia(texto)
        return "nucleotideo" if set(seq) <= NUCLEOTIDEOS else "peptideo"

    seq = "".join(texto.split()).upper()

    if not seq.isalpha():
        return "nome"
    if len(seq) >= MIN_TAM_NUCLEOTIDEO and set(seq) <= NUCLEOTIDEOS:
        return "nucleotideo"
    if len(seq) >= MIN_TAM_SEQUENCIA and set(seq) <= AMINOACIDOS:
        # Palavras em minúsculas podem usar só letras de aminoácido ("defensin").
        if texto.isupper() or len(seq) >= 30:
            return "peptideo"
    return "nome"


def _formatar_entrada(e: dict) -> str:
    """Converte uma entrada JSON do UniProt num bloco de texto curto."""
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
    revisada = "Swiss-Prot (revisada)" if "Swiss-Prot" in e.get("entryType", "") else "TrEMBL (não revisada)"

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


async def buscar_por_nome(
    client: httpx.AsyncClient, consulta: str, organismo: str | None, max_resultados: int
) -> str:
    """Busca textual (nome, gene, ID) no UniProtKB."""
    termos = []
    if RE_ACCESSION.fullmatch(consulta.upper()):
        termos.append(f"accession:{consulta.upper()}")
    elif len(consulta.split()) == 1:
        termos.append(f"gene_exact:{consulta}")
    termos.append(consulta)

    filtro_org = f' AND (organism_name:"{organismo}")' if organismo else ""
    resultados = []
    # Entradas revisadas (Swiss-Prot) primeiro; do termo mais específico ao mais amplo.
    for filtro_rev in (" AND (reviewed:true)", ""):
        for termo in termos:
            params = {
                "query": f"({termo}){filtro_org}{filtro_rev}",
                "fields": CAMPOS,
                "format": "json",
                "size": max_resultados,
            }
            r = await requisitar(
                client, "GET", f"{UNIPROT_API}/search",
                servico="UniProt", tentativas=TENTATIVAS, params=params,
            )
            r.raise_for_status()
            resultados = r.json().get("results", [])
            if resultados:
                break
        if resultados:
            break

    if not resultados:
        raise ErroEsperado(f"Nenhuma proteína encontrada para '{consulta}'.")

    cab = f"Resultados do UniProt para '{consulta}' (busca por nome/gene/ID):\n\n"
    return cab + "\n\n".join(_formatar_entrada(e) for e in resultados)


async def buscar_por_peptideo(
    client: httpx.AsyncClient, sequencia: str, organismo: str | None, max_resultados: int
) -> str:
    """Encontra proteínas que CONTÊM exatamente a sequência (Peptide Search)."""
    if len(sequencia) < MIN_TAM_PEPTIDEO:
        raise ErroEsperado(
            f"Sequência curta demais ({len(sequencia)} aa). Peptídeos com menos de "
            f"{MIN_TAM_PEPTIDEO} aminoácidos aparecem em milhões de proteínas, então a "
            "busca não é informativa. Use uma sequência maior ou busque por nome/gene."
        )

    r = await requisitar(
        client, "POST", f"{PEPTIDE_API}/",
        servico="Peptide Search", tentativas=TENTATIVAS,
        data={"peps": sequencia, "lEQi": "off", "spOnly": "off"},
    )
    if r.status_code != 202 or "Location" not in r.headers:
        raise ErroEsperado(f"Peptide Search recusou o pedido (HTTP {r.status_code}).")

    url_job = r.headers["Location"].replace("http://", "https://", 1)

    # Job assíncrono: 303 = ainda processando, 200 = pronto (lista de IDs).
    espera = 0
    while True:
        r = await requisitar(
            client, "GET", url_job,
            servico="Peptide Search", tentativas=TENTATIVAS, follow_redirects=False,
        )
        if r.status_code == 200:
            break
        if r.status_code != 303:
            raise ErroEsperado(f"Erro no job do Peptide Search (HTTP {r.status_code}).")
        if espera >= PEPTIDE_ESPERA_MAX_S:
            raise ErroEsperado("Peptide Search demorou demais. Tente novamente mais tarde.")
        await asyncio.sleep(2)
        espera += 2

    ids = [i.strip() for i in r.text.split(",") if i.strip()]
    if not ids:
        raise ErroEsperado(
            f"Nenhuma proteína do UniProt contém exatamente a sequência {sequencia}. "
            "O Peptide Search só encontra correspondências exatas. Para sequências "
            "parecidas (não idênticas), seria preciso BLAST (ainda não implementado)."
        )

    if organismo:
        consulta_ids = " OR ".join(f"accession:{i}" for i in ids[:200])
        params = {
            "query": f"({consulta_ids}) AND (organism_name:\"{organismo}\")",
            "fields": CAMPOS, "format": "json", "size": max_resultados,
        }
        r = await requisitar(
                client, "GET", f"{UNIPROT_API}/search",
                servico="UniProt", tentativas=TENTATIVAS, params=params,
            )
    else:
        params = {"accessions": ",".join(ids[:max_resultados]), "fields": CAMPOS, "format": "json"}
        r = await requisitar(
            client, "GET", f"{UNIPROT_API}/accessions",
            servico="UniProt", tentativas=TENTATIVAS, params=params,
        )
    r.raise_for_status()
    resultados = r.json().get("results", [])

    if not resultados:
        raise ErroEsperado(
            f"A sequência aparece em {len(ids)} proteína(s), mas nenhuma de '{organismo}'. "
            "Pode ser que nesse organismo a sequência tenha alguma diferença "
            "(a busca é exata). Tente sem o filtro de organismo."
        )

    cab = (
        f"A sequência {sequencia} ({len(sequencia)} aa) aparece exatamente em "
        f"{len(ids)} proteína(s) do UniProt. Mostrando {len(resultados)}:\n\n"
    )
    return cab + "\n\n".join(_formatar_entrada(e) for e in resultados)


async def buscar(
    consulta: str, tipo: str = "auto", organismo: str | None = None, max_resultados: int = 5
) -> Resultado:
    """Decide o tipo de busca (nome ou sequência) e executa."""
    if tipo == "auto":
        tipo = detectar_tipo(consulta)

    if tipo == "nucleotideo":
        raise ErroEsperado(
            "Isso parece uma sequência de DNA/RNA. O UniProt é um banco de proteínas; "
            "traduza para aminoácidos primeiro (ou use tipo='sequencia' se for mesmo proteína)."
        )

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            if tipo in ("peptideo", "sequencia"):
                return Resultado(await buscar_por_peptideo(
                    client, limpar_sequencia(consulta), organismo, max_resultados
                ))
            return Resultado(await buscar_por_nome(client, consulta.strip(), organismo, max_resultados))
        except httpx.HTTPError as e:
            raise ErroEsperado(f"Falha ao acessar o UniProt: {type(e).__name__} {e}") from e


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    termo = " ".join(sys.argv[1:]) or "insulin"
    print(asyncio.run(buscar(termo)).texto)
