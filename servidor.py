"""Servidor MCP do agente de pesquisa."""

import asyncio
import logging
import os
from typing import Literal

# O modelo já está em cache (instalador/indexador): nunca acessar o Hugging Face.
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from mcp.server import MCPServer  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402

import busca_pdfs  # noqa: E402
import pubtator  # noqa: E402
import rag  # noqa: E402
import seguranca  # noqa: E402
import uniprot  # noqa: E402

# Logs vão para o stderr: o stdout é o canal do protocolo MCP (nunca use print aqui).
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("agente-pesquisa")

CFG_SEG = rag.carregar_config().get("seguranca", {})

# Todas as ferramentas só leem: nada é criado, alterado ou apagado.
# Os PDFs são locais (mundo fechado); UniProt e PubTator são APIs públicas.
SO_LEITURA_LOCAL = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
SO_LEITURA_WEB = ToolAnnotations(readOnlyHint=True, openWorldHint=True)

mcp = MCPServer(
    name="agente-pesquisa",
    instructions=(
        "Assistente de pesquisa científica com fontes verificáveis. REGRAS: "
        "(1) Sempre que a conversa mencionar uma proteína, peptídeo, gene ou "
        "sequência de aminoácidos, consulte buscar_proteina ANTES de responder, "
        "mesmo que você já conheça o assunto: os dados do UniProt são mais "
        "atuais e citáveis. "
        "(2) Para perguntas científicas ou técnicas, use também buscar_pdfs para "
        "verificar se a biblioteca local do usuário tem algo relevante, e cite "
        "arquivo e página. "
        "(3) Quando a pergunta mencionar literatura, estudos, evidências ou relações "
        "entre genes, doenças e fármacos (o que trata, causa, inibe...), consulte o "
        "PubMed com buscar_artigos, ler_artigo e buscar_relacoes ANTES de responder, "
        "mesmo que você já conheça o assunto, e cite os PMIDs. "
        "(4) Deixe claro o que veio das ferramentas e o que é conhecimento geral."
    ),
)


@mcp.tool(annotations=SO_LEITURA_WEB)
async def buscar_proteina(
    consulta: str,
    tipo: Literal["auto", "nome", "sequencia"] = "auto",
    organismo: str | None = None,
    max_resultados: int = 5,
) -> str:
    """Busca proteínas e peptídeos no UniProt e retorna função, organismo,
    localização celular e estruturas (PDB/AlphaFold).

    Use SEMPRE que o usuário mencionar uma proteína, peptídeo, gene ou
    sequência, mesmo sem pedir explicitamente, e mesmo que você já conheça
    o assunto. Prefira os dados daqui ao seu conhecimento prévio.

    Aceita:
    - nome de proteína ou gene (ex.: "insulin", "TP53", "hemoglobin beta");
    - ID do UniProt (ex.: "P01308");
    - sequência de aminoácidos em código de 1 letra, com ou sem cabeçalho FASTA.
      Sequências são buscadas por CORRESPONDÊNCIA EXATA: retorna as proteínas
      que contêm exatamente aquele trecho (útil para identificar peptídeos).

    Args:
        consulta: nome, gene, ID ou sequência.
        tipo: "auto" detecta sozinho. Use "nome" ou "sequencia" quando for
            ambíguo (ex.: "CAT" pode ser o gene da catalase ou um tripeptídeo).
        organismo: filtro opcional, nome científico ou comum (ex.: "Homo sapiens", "human").
        max_resultados: quantas proteínas retornar (1 a 25).
    """
    max_resultados = max(1, min(max_resultados, 25))
    log.info("buscar_proteina: consulta=%r tipo=%s organismo=%s", consulta, tipo, organismo)

    try:
        saida = await uniprot.buscar(consulta, tipo, organismo, max_resultados)
        seguranca.registrar(
            "buscar_proteina",
            {"consulta": consulta, "tipo": tipo, "organismo": organismo},
            saida, CFG_SEG,
        )
        return saida
    except uniprot.ErroUniProt as e:
        raise ToolError(str(e)) from e


@mcp.tool(annotations=SO_LEITURA_LOCAL)
async def buscar_pdfs(pergunta: str, k: int = 5, arquivo: str | None = None) -> str:
    """Busca semântica nos PDFs científicos locais do usuário (artigos, livros,
    apostilas). Encontra trechos pelo SIGNIFICADO, não só por palavra exata,
    e funciona entre idiomas (pergunta em português acha texto em inglês).

    Use SEMPRE que o usuário fizer uma pergunta científica ou técnica, mesmo
    sem mencionar os PDFs: a biblioteca pessoal dele pode ter material
    relevante. Cada trecho vem com arquivo e página: cite-os na resposta.

    Args:
        pergunta: o que procurar, em linguagem natural (ex.: "mecanismo de
            ação de peptídeos antimicrobianos").
        k: quantos trechos retornar (1 a 15). Aumente para temas amplos.
        arquivo: opcional, restringe a busca a PDFs cujo nome contém este texto.
    """
    k = max(1, min(k, 15))
    log.info("buscar_pdfs: pergunta=%r k=%d arquivo=%s", pergunta, k, arquivo)
    try:
        saida, detalhes = await asyncio.to_thread(busca_pdfs.buscar, pergunta, k, arquivo)
    except busca_pdfs.ErroBusca as e:
        raise ToolError(str(e)) from e
    seguranca.registrar(
        "buscar_pdfs", {"pergunta": pergunta, "k": k, "arquivo": arquivo},
        saida, CFG_SEG, **detalhes,
    )
    return saida


@mcp.tool(annotations=SO_LEITURA_LOCAL)
async def listar_pdfs() -> str:
    """Lista os nomes dos PDFs indexados na biblioteca local do usuário.
    Útil para saber o que existe antes de buscar, ou para achar o nome de
    um arquivo e usá-lo no filtro 'arquivo' de buscar_pdfs."""
    try:
        saida, detalhes = await asyncio.to_thread(busca_pdfs.listar_arquivos)
    except busca_pdfs.ErroBusca as e:
        raise ToolError(str(e)) from e
    seguranca.registrar("listar_pdfs", {}, saida, CFG_SEG, **detalhes)
    return saida


async def _pubtator(ferramenta: str, coro, argumentos: dict) -> str:
    """Executa uma chamada ao PubTator com log, auditoria e erro legível."""
    log.info("%s: %r", ferramenta, argumentos)
    try:
        saida = await coro
    except pubtator.ErroPubTator as e:
        raise ToolError(str(e)) from e
    seguranca.registrar(ferramenta, argumentos, saida, CFG_SEG)
    return saida


@mcp.tool(annotations=SO_LEITURA_WEB)
async def buscar_entidade(
    nome: str,
    tipo: Literal["gene", "disease", "chemical", "variant"] | None = None,
) -> str:
    """Converte o nome de um gene, doença, químico ou variante no ID do PubTator
    (ex.: "p53" -> @GENE_TP53, "doxorubicin" -> @CHEMICAL_Doxorubicin).

    Use antes de buscar_relacoes (que exige o ID) e para buscas de artigos mais
    precisas: o ID encontra todos os sinônimos ("p53", "TP53", "tumor protein p53").

    O autocomplete casa por prefixo e sinônimo, então confira o candidato:
    "aging" devolve "Aging Premature", e "CAT" pode ser o gene da catalase ou
    catarata. Passe `tipo` quando souber o que procura.

    Args:
        nome: nome em inglês (ex.: "p53", "breast cancer", "metformin").
        tipo: filtro opcional pelo tipo da entidade.
    """
    return await _pubtator(
        "buscar_entidade", pubtator.buscar_entidades(nome, tipo), {"nome": nome, "tipo": tipo}
    )


@mcp.tool(annotations=SO_LEITURA_WEB)
async def buscar_artigos(consulta: str, pagina: int = 1) -> str:
    """Busca artigos científicos no PubMed (mais de 36 milhões) pelo PubTator3,
    10 por página, mais relevantes primeiro. Retorna PMID, ano, revista e título.

    Use quando a pergunta pedir evidência publicada ou estado da arte. Depois,
    abra os mais relevantes com ler_artigo e cite os PMIDs.

    A consulta aceita (funciona melhor em inglês):
    - texto livre: "gut microbiome depression";
    - IDs de entidade (via buscar_entidade), combináveis com AND/OR:
      "@GENE_TP53 AND breast cancer";
    - relações: "relations:treat|@CHEMICAL_Doxorubicin|@DISEASE_Breast_Neoplasms"
      ou "relations:ANY|@CHEMICAL_Doxorubicin|DISEASE".

    Nomes curtos que também são palavras comuns geram ruído (o gene CAT traz
    artigos sobre gatos): acrescente contexto, ex.: "@GENE_CAT AND oxidative stress".

    Args:
        consulta: o que buscar, num dos formatos acima.
        pagina: página de resultados (começa em 1).
    """
    pagina = max(1, pagina)
    return await _pubtator(
        "buscar_artigos", pubtator.buscar_artigos(consulta, pagina),
        {"consulta": consulta, "pagina": pagina},
    )


@mcp.tool(annotations=SO_LEITURA_WEB)
async def ler_artigo(pmid: str, texto_completo: bool = False) -> str:
    """Lê um artigo do PubMed pelo PMID: título, resumo e, com texto_completo,
    resultados e discussão (métodos, figuras, tabelas e referências são
    omitidos para economizar contexto). Termina com as entidades que o PubTator
    anotou no texto (genes, doenças, químicos, espécies), úteis para ver de
    relance do que o artigo trata e se o estudo foi em humanos ou animais.

    Comece pelo resumo; peça o texto completo só quando precisar de detalhes.
    O texto completo só existe para artigos do PMC Open Access: quando não
    houver, a saída avisa. Não diga que leu o artigo inteiro se só veio o resumo.

    Args:
        pmid: o PMID do artigo (ex.: "29355051").
        texto_completo: True para incluir resultados e discussão.
    """
    return await _pubtator(
        "ler_artigo", pubtator.ler_artigo(pmid, texto_completo),
        {"pmid": pmid, "texto_completo": texto_completo},
    )


@mcp.tool(annotations=SO_LEITURA_WEB)
async def buscar_relacoes(
    entidade: str,
    tipo_relacao: Literal[
        "treat", "cause", "cotreat", "convert", "compare", "interact", "associate",
        "positive_correlate", "negative_correlate", "prevent", "inhibit",
        "stimulate", "drug_interact",
    ] | None = None,
    tipo_alvo: Literal["gene", "disease", "chemical", "variant"] | None = None,
    max_resultados: int = 15,
) -> str:
    """Relações entre entidades extraídas de toda a literatura do PubMed, com o
    número de artigos que sustentam cada uma. Ex.: que doenças a doxorrubicina
    trata, que químicos inibem um gene, que genes se associam a uma doença.
    Útil para levantar hipóteses (ex.: reposicionamento de fármacos).

    Cada par de tipos usa só alguns rótulos. Entre químico e gene não existe
    "inhibit": um inibidor aparece como "negative_correlate" (e um ativador como
    "positive_correlate"). Na dúvida, omita tipo_relacao e filtre só por tipo_alvo.

    As relações foram extraídas por IA: muitas publicações indicam evidência
    consolidada; poucas indicam indício fraco ou erro de extração. Para ver os
    artigos de uma relação, use buscar_artigos com a sintaxe "relations:...".

    Args:
        entidade: ID do PubTator (ex.: "@CHEMICAL_Doxorubicin"); obtenha com buscar_entidade.
        tipo_relacao: filtro opcional pelo tipo de relação.
        tipo_alvo: filtro opcional pelo tipo da outra entidade.
        max_resultados: quantas relações mostrar (1 a 50).
    """
    max_resultados = max(1, min(max_resultados, 50))
    return await _pubtator(
        "buscar_relacoes",
        pubtator.buscar_relacoes(entidade, tipo_relacao, tipo_alvo, max_resultados),
        {"entidade": entidade, "tipo_relacao": tipo_relacao, "tipo_alvo": tipo_alvo,
         "max_resultados": max_resultados},
    )


def _pre_carregar() -> None:
    """Carrega modelo e banco em segundo plano, para a 1ª busca não esperar ~1 min."""
    try:
        busca_pdfs._pronto()
        log.info("Busca em PDFs pronta (pré-carregada).")
    except Exception as e:
        log.info("Pré-carregamento adiado: %s", e)


if __name__ == "__main__":
    import threading

    threading.Thread(target=_pre_carregar, daemon=True).start()
    mcp.run(transport="stdio")
