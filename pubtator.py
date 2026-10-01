"""Cliente do PubTator3 (NCBI): entidades, busca de artigos, leitura e relações."""

import asyncio
import re
import time
from collections import Counter

import httpx

PUBTATOR_API = "https://www.ncbi.nlm.nih.gov/research/pubtator3-api"

TIMEOUT = httpx.Timeout(30.0, connect=10.0)
TENTATIVAS = 3
# O NCBI pede no máximo 3 requisições por segundo.
INTERVALO_MIN_S = 0.35

TIPOS_ENTIDADE = ("gene", "disease", "chemical", "variant")
TIPOS_RELACAO = (
    "treat", "cause", "cotreat", "convert", "compare", "interact", "associate",
    "positive_correlate", "negative_correlate", "prevent", "inhibit", "stimulate",
    "drug_interact",
)

# Seções que respondem "o que o artigo descobriu". Métodos, figuras, tabelas
# (viram texto ilegível) e referências custam contexto e acrescentam pouco.
SECOES_UTEIS = {"TITLE", "ABSTRACT", "RESULTS", "DISCUSS", "CONCL"}
# Revisões não têm RESULTS/DISCUSS: o corpo inteiro vem marcado como INTRO.
SECOES_ACHADOS = {"RESULTS", "DISCUSS"}
MAX_CARACTERES = 20_000

# Para estes tipos o PubTator dá um nome normalizado ("Dox" -> "Doxorubicin").
# Para espécies e linhagens o "name" é um código numérico, então usa-se o texto.
TIPOS_COM_NOME = {"Gene", "Chemical", "Disease"}
MAX_ENTIDADES = 15

_trava = asyncio.Lock()
_ultima_requisicao = 0.0


class ErroPubTator(Exception):
    """Erro "esperado" (API fora, nada encontrado...), com mensagem legível."""


async def _aguardar_vez() -> None:
    """Espaça as requisições, inclusive as feitas em paralelo pelo Claude."""
    global _ultima_requisicao
    async with _trava:
        espera = _ultima_requisicao + INTERVALO_MIN_S - time.monotonic()
        if espera > 0:
            await asyncio.sleep(espera)
        _ultima_requisicao = time.monotonic()


async def _get(client: httpx.AsyncClient, caminho: str, params: dict) -> httpx.Response:
    """GET respeitando o limite de taxa e tentando de novo em falhas temporárias."""
    for tentativa in range(1, TENTATIVAS + 1):
        await _aguardar_vez()
        try:
            r = await client.get(f"{PUBTATOR_API}{caminho}", params=params)
            if r.status_code not in (429, 500, 502, 503, 504):
                return r
            motivo = f"HTTP {r.status_code}"
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            motivo = type(e).__name__
        if tentativa < TENTATIVAS:
            await asyncio.sleep(2 * tentativa)

    raise ErroPubTator(
        f"O PubTator não respondeu após {TENTATIVAS} tentativas ({motivo}). "
        "Provavelmente é instabilidade do NCBI; tente de novo em alguns minutos."
    )


async def _consultar(caminho: str, params: dict, erro_400: str | None = None):
    """Faz a chamada e devolve o JSON, convertendo falhas de rede em ErroPubTator."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            r = await _get(client, caminho, params)
            if r.status_code == 400 and erro_400:
                raise ErroPubTator(erro_400)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as e:
            raise ErroPubTator(f"Falha ao acessar o PubTator: {type(e).__name__} {e}") from e


def _legivel(entidade_id: str) -> str:
    """'@DISEASE_Breast_Neoplasms' -> 'Breast Neoplasms'."""
    partes = entidade_id.split("_", 1)
    return partes[1].replace("_", " ") if len(partes) == 2 else entidade_id


def _limpar(texto: str) -> str:
    """Colapsa espaços e quebras de linha repetidos."""
    return " ".join(texto.split())


async def buscar_entidades(texto: str, tipo: str | None = None, max_resultados: int = 5) -> str:
    """Lista candidatos a ID do PubTator (ex.: @GENE_TP53) para um nome."""
    params = {"query": texto, "limit": max_resultados}
    if tipo:
        params["concept"] = tipo
    candidatos = await _consultar("/entity/autocomplete/", params)
    if not candidatos:
        raise ErroPubTator(
            f"Nenhuma entidade reconhecida para '{texto}'. O PubTator só reconhece genes, "
            "doenças, químicos e variantes; para outros temas (ex.: 'gut microbiome'), "
            "use buscar_artigos com o texto livre."
        )

    linhas = []
    for c in candidatos:
        como = re.sub(r"</?m>", "", c.get("match") or "")
        banco = f"{c.get('db')} {c.get('db_id')}" if c.get("db_id") else "-"
        linhas.append(f"- {c['_id']} | {c.get('biotype')} | {c.get('name')} ({banco}) | {como}")

    return (
        f"Candidatos do PubTator para '{texto}'"
        + (f" (tipo: {tipo})" if tipo else "")
        + ". O autocomplete casa por prefixo e sinônimo, então confira se o primeiro é "
        "mesmo o que se procura:\n\n" + "\n".join(linhas)
    )


async def buscar_artigos(consulta: str, pagina: int = 1) -> str:
    """Busca artigos no PubTator (10 por página), mais relevantes primeiro."""
    dados = await _consultar("/search/", {"text": consulta, "page": pagina})
    artigos = dados.get("results", [])
    if not artigos:
        raise ErroPubTator(
            f"Nenhum artigo encontrado para '{consulta}'"
            + (f" na página {pagina}" if pagina > 1 else "")
            + ". Tente termos em inglês, mais gerais, ou o ID da entidade (buscar_entidade)."
        )

    linhas = []
    for a in artigos:
        ano = (a.get("date") or "")[:4] or "s/d"
        pmc = f" [{a['pmcid']}]" if a.get("pmcid") else ""
        linhas.append(f"- PMID {a['pmid']} ({ano}, {a.get('journal', '-')}): {_limpar(a.get('title', ''))}{pmc}")

    cab = (
        f"{dados.get('count', '?')} artigo(s) no PubMed para '{consulta}' "
        f"(página {dados.get('current', pagina)} de {dados.get('total_pages', '?')}). "
        "Artigos marcados com [PMC...] costumam ter texto completo disponível.\n\n"
    )
    return cab + "\n".join(linhas)


async def ler_artigo(pmid: str | int, texto_completo: bool = False) -> str:
    """Lê um artigo: seções úteis + resumo das entidades anotadas pelo PubTator."""
    pmid = re.sub(r"\D", "", str(pmid))
    if not pmid:
        raise ErroPubTator("PMID inválido: informe só o número (ex.: 29355051).")

    params = {"pmids": pmid}
    if texto_completo:
        params["full"] = "true"
    dados = await _consultar(
        "/publications/export/biocjson", params,
        erro_400=f"PMID {pmid} não encontrado no PubTator.",
    )
    documentos = dados.get("PubTator3") or []
    if not documentos:
        raise ErroPubTator(f"PMID {pmid} não encontrado no PubTator.")
    doc = documentos[0]
    passages = doc.get("passages", [])

    def secao(p: dict) -> str:
        infons = p.get("infons", {})
        return (infons.get("section_type") or infons.get("type") or "").upper()

    secoes = set(SECOES_UTEIS)
    if texto_completo and not {secao(p) for p in passages} & SECOES_ACHADOS:
        secoes.add("INTRO")

    pedacos, contagem, total, cortado = [], Counter(), 0, False
    for p in passages:
        rotulo = secao(p)
        if rotulo not in secoes:
            continue
        for a in p.get("annotations", []):
            info = a.get("infons", {})
            tipo_ent = info.get("type", "?")
            nome = info.get("name") if tipo_ent in TIPOS_COM_NOME else None
            contagem[f"{tipo_ent}: {nome or a.get('text', '').lower()}"] += 1

        texto = _limpar(p.get("text", ""))
        if cortado or total + len(texto) > MAX_CARACTERES:
            cortado = True
            continue
        pedacos.append(f"[{rotulo}] {texto}")
        total += len(texto)

    autores = doc.get("authors") or []
    autores_txt = ", ".join(autores[:3]) + (" et al." if len(autores) > 3 else "")
    ano = (doc.get("date") or "")[:4] or "s/d"
    cab = (
        f"PMID {pmid} | {doc.get('journal', '-')}, {ano} | {autores_txt or '-'}\n"
        f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
    )

    avisos = []
    if contagem:
        itens = ", ".join(f"{k} ({n}×)" for k, n in contagem.most_common(MAX_ENTIDADES))
        avisos.append(f"[ENTIDADES anotadas pelo PubTator] {itens}")
    if cortado:
        avisos.append(
            f"[AVISO] Texto cortado em {MAX_CARACTERES} caracteres para economizar contexto; "
            "o restante das seções foi omitido."
        )
    if texto_completo and len(passages) <= 2:
        avisos.append(
            "[AVISO] Texto completo indisponível (artigo fora do PMC Open Access). "
            "Apenas título e resumo foram retornados."
        )

    return "\n\n".join([cab, *pedacos, *avisos])


async def buscar_relacoes(
    entidade: str,
    tipo_relacao: str | None = None,
    tipo_alvo: str | None = None,
    max_resultados: int = 15,
) -> str:
    """Relações extraídas da literatura (ex.: químicos que tratam uma doença)."""
    entidade = entidade.strip()
    if not entidade.startswith("@"):
        raise ErroPubTator(
            f"'{entidade}' não é um ID do PubTator. Use buscar_entidade primeiro para "
            "obter o ID (ex.: @CHEMICAL_Doxorubicin, @GENE_TP53)."
        )

    params = {"e1": entidade}
    if tipo_relacao:
        params["type"] = tipo_relacao
    if tipo_alvo:
        params["e2"] = tipo_alvo
    relacoes = await _consultar("/relations", params)
    if not relacoes:
        msg = (
            f"Nenhuma relação encontrada para {entidade}"
            + (f" do tipo '{tipo_relacao}'" if tipo_relacao else "")
            + (f" com {tipo_alvo}" if tipo_alvo else "")
            + "."
        )
        if tipo_relacao:
            # Cada par de tipos usa só alguns rótulos (químico-gene não tem "inhibit",
            # e sim "negative_correlate"): mostrar os que existem evita tentativas às cegas.
            params.pop("type")
            existentes = Counter(r["type"] for r in await _consultar("/relations", params))
            if existentes:
                tipos = ", ".join(f"{t} ({n})" for t, n in existentes.most_common())
                msg += f" Tipos de relação disponíveis para esse filtro: {tipos}."
        raise ErroPubTator(msg + " Confira o ID com buscar_entidade ou afrouxe os filtros.")

    linhas = []
    for r in relacoes[:max_resultados]:
        outro = r["target"] if r["source"] == entidade else r["source"]
        linhas.append(
            f"- {_legivel(r['source'])} —{r['type']}→ {_legivel(r['target'])} "
            f"| {r['publications']} artigo(s) | outro lado: {outro}"
        )

    cab = (
        f"{len(relacoes)} relação(ões) de {entidade} na literatura; mostrando as "
        f"{len(linhas)} com mais artigos. Foram extraídas por IA: poucas publicações "
        "indicam evidência fraca ou erro de extração.\n\n"
    )
    rodape = (
        "\n\nPara ver os artigos de uma relação, use buscar_artigos com "
        f"'relations:<tipo>|{entidade}|<outro lado>'."
    )
    return cab + "\n".join(linhas) + rodape


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8")

    async def _demo():
        print(await buscar_entidades("p53"), end="\n\n")
        print(await buscar_artigos("@GENE_TP53 AND breast cancer"), end="\n\n")
        print((await ler_artigo(29355051))[:1500], end="\n\n")
        print(await buscar_relacoes("@CHEMICAL_Doxorubicin", "treat", "disease", 5))

    asyncio.run(_demo())
