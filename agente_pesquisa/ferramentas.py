"""Camada de entrada (adapter): expõe as fontes de dados como ferramentas MCP.

Não tem lógica de negócio. Cada ferramenta valida limites, chama UMA operação de
uma fonte (que devolve Resultado ou levanta ErroEsperado) e passa por _executar,
o caminho único que faz log, auditoria e conversão de erro.
"""

import asyncio
import importlib.util
import logging
import os
import threading
from collections.abc import Awaitable
from typing import Literal

# O modelo já está em cache (instalador/indexador): nunca acessar o Hugging Face.
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from mcp.server import MCPServer  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402

from agente_pesquisa import seguranca  # noqa: E402
from agente_pesquisa.config import carregar_config  # noqa: E402
from agente_pesquisa.contrato import ErroEsperado, Resultado  # noqa: E402
from agente_pesquisa.fontes import blast, pubtator, string_db, uniprot  # noqa: E402

# Logs vão para o stderr: o stdout é o canal do protocolo MCP (nunca use print aqui).
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("agente-pesquisa")

CONFIG = carregar_config()
CFG_SEG = CONFIG.get("seguranca", {})

# A busca em PDFs só liga na instalação completa (config.toml com [qdrant] e as
# dependências do extra [pdfs]). No pacote leve, só as ferramentas web existem.
PDFS_ATIVOS = "qdrant" in CONFIG and importlib.util.find_spec("langchain_qdrant") is not None

# Todas as ferramentas só leem: nada é criado, alterado ou apagado.
# Os PDFs são locais (mundo fechado); as demais fontes são APIs públicas.
SO_LEITURA_LOCAL = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
SO_LEITURA_WEB = ToolAnnotations(readOnlyHint=True, openWorldHint=True)

INSTRUCOES = (
    "Ferramentas de pesquisa em biologia molecular com fontes verificáveis: UniProt "
    "(proteínas), BLAST do EBI (sequências parecidas), STRING (interações) e PubMed via "
    "PubTator3 (literatura). Use-as quando a pergunta for sobre biologia, bioquímica ou "
    "biomedicina e pedir fatos que precisam ser atuais ou citáveis: função ou estrutura de "
    "uma proteína, identidade de uma sequência, parceiros de interação, evidências na "
    "literatura. Não use só porque um nome de gene aparece de passagem (ex.: em código ou "
    "nomes de variáveis). Cite UniProt IDs e PMIDs e deixe claro o que veio das "
    "ferramentas e o que é conhecimento geral."
)
if PDFS_ATIVOS:
    INSTRUCOES += (
        " Para perguntas científicas, verifique também a biblioteca local de PDFs do "
        "usuário (buscar_pdfs) e cite arquivo e página."
    )

mcp = MCPServer(name="agente-pesquisa", instructions=INSTRUCOES)


async def _executar(ferramenta: str, argumentos: dict, operacao: Awaitable[Resultado]) -> str:
    """Caminho único de toda ferramenta: log -> executa -> erro legível ou auditoria -> texto."""
    log.info("%s: %r", ferramenta, argumentos)
    try:
        resultado = await operacao
    except ErroEsperado as e:
        # ToolError = falha prevista: o Claude recebe a mensagem (is_error=True).
        raise ToolError(str(e)) from e
    seguranca.registrar(ferramenta, argumentos, resultado.texto, CFG_SEG, **resultado.auditoria)
    return resultado.texto


@mcp.tool(annotations=SO_LEITURA_WEB)
async def buscar_proteina(
    consulta: str,
    tipo: Literal["auto", "nome", "sequencia"] = "auto",
    organismo: str | None = None,
    max_resultados: int = 5,
) -> str:
    """Busca proteínas e peptídeos no UniProt e retorna função, organismo,
    localização celular e estruturas (PDB/AlphaFold).

    Use quando a pergunta depender de dados de uma proteína (função, organismo,
    localização, estrutura) ou de identificar uma sequência. Prefira os dados
    daqui ao conhecimento prévio: são atuais e citáveis.

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
    return await _executar(
        "buscar_proteina", {"consulta": consulta, "tipo": tipo, "organismo": organismo},
        uniprot.buscar(consulta, tipo, organismo, max_resultados),
    )


def _busca():
    """Importa a busca em PDFs só quando ela está ativa (dependências pesadas)."""
    from agente_pesquisa.biblioteca import busca

    return busca


if PDFS_ATIVOS:
    @mcp.tool(annotations=SO_LEITURA_LOCAL)
    async def buscar_pdfs(pergunta: str, k: int = 5, arquivo: str | None = None) -> str:
        """Busca semântica nos PDFs científicos locais do usuário (artigos, livros,
        apostilas). Encontra trechos pelo SIGNIFICADO, não só por palavra exata,
        e funciona entre idiomas (pergunta em português acha texto em inglês).

        Use em perguntas científicas ou técnicas, mesmo sem o usuário mencionar
        os PDFs: a biblioteca pessoal dele pode ter material relevante. Cada
        trecho vem com arquivo e página: cite-os na resposta.

        Args:
            pergunta: o que procurar, em linguagem natural (ex.: "mecanismo de
                ação de peptídeos antimicrobianos").
            k: quantos trechos retornar (1 a 15). Aumente para temas amplos.
            arquivo: opcional, restringe a busca a PDFs cujo nome contém este texto.
        """
        k = max(1, min(k, 15))
        # A busca roda o modelo na CPU: em outra thread, o servidor não trava enquanto isso.
        return await _executar(
            "buscar_pdfs", {"pergunta": pergunta, "k": k, "arquivo": arquivo},
            asyncio.to_thread(_busca().buscar, pergunta, k, arquivo),
        )


    @mcp.tool(annotations=SO_LEITURA_LOCAL)
    async def listar_pdfs() -> str:
        """Lista os nomes dos PDFs indexados na biblioteca local do usuário.
        Útil para saber o que existe antes de buscar, ou para achar o nome de
        um arquivo e usá-lo no filtro 'arquivo' de buscar_pdfs."""
        return await _executar("listar_pdfs", {}, asyncio.to_thread(_busca().listar_arquivos))


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
    return await _executar(
        "buscar_entidade", {"nome": nome, "tipo": tipo}, pubtator.buscar_entidades(nome, tipo)
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
    return await _executar(
        "buscar_artigos", {"consulta": consulta, "pagina": pagina},
        pubtator.buscar_artigos(consulta, pagina),
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
    return await _executar(
        "ler_artigo", {"pmid": pmid, "texto_completo": texto_completo},
        pubtator.ler_artigo(pmid, texto_completo),
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
    return await _executar(
        "buscar_relacoes",
        {"entidade": entidade, "tipo_relacao": tipo_relacao, "tipo_alvo": tipo_alvo,
         "max_resultados": max_resultados},
        pubtator.buscar_relacoes(entidade, tipo_relacao, tipo_alvo, max_resultados),
    )


@mcp.tool(annotations=SO_LEITURA_WEB)
async def buscar_similares(
    sequencia: str,
    banco: Literal["swissprot", "uniprot", "pdb"] = "swissprot",
    max_resultados: int = 10,
    evalue_max: float = 1e-3,
) -> str:
    """BLAST de proteína (blastp, serviço do EBI): encontra proteínas com
    sequência PARECIDA, não só idêntica, com identidade, cobertura e e-value.

    Use para identificar uma sequência desconhecida, achar homólogos em outros
    organismos ou quando buscar_proteina não encontrou correspondência exata.
    Leva de 20 s a alguns minutos (o job roda na fila do EBI).

    Args:
        sequencia: aminoácidos em código de 1 letra, com ou sem cabeçalho FASTA
            (mínimo 10 aa).
        banco: "swissprot" (revisadas, rápido, padrão), "uniprot" (tudo, lento)
            ou "pdb" (só proteínas com estrutura experimental).
        max_resultados: quantos alinhamentos retornar (1 a 50).
        evalue_max: corte de e-value; menor = mais rigoroso.
    """
    max_resultados = max(1, min(max_resultados, 50))
    return await _executar(
        "buscar_similares",
        {"tamanho": len(sequencia), "banco": banco, "max_resultados": max_resultados,
         "evalue_max": evalue_max},
        blast.buscar_similares(sequencia, banco, max_resultados, evalue_max),
    )


@mcp.tool(annotations=SO_LEITURA_WEB)
async def buscar_interacoes(
    proteinas: list[str],
    organismo: str | None = None,
    max_resultados: int = 15,
    score_minimo: float = 0.7,
) -> str:
    """Interações proteína-proteína do STRING, com score de confiança e os
    canais de evidência (experimentos, bancos curados, mineração de texto...).

    - Uma proteína: lista os parceiros mais confiáveis (ex.: ["GRB2"]).
    - Várias: mostra a rede de interações entre elas (ex.: ["GRB2", "SOS1", "FGFR2"]).

    Use quando perguntarem com quem uma proteína interage, em que complexo ou
    via ela atua, ou se duas proteínas se ligam. Interação no STRING pode ser
    funcional, não necessariamente contato físico: confira o canal de evidência.

    Args:
        proteinas: nomes de gene/proteína ou IDs do UniProt (até 50).
        organismo: nome ("human", "mouse", "yeast"...) ou ID de táxon do NCBI.
            Padrão: humano (9606).
        max_resultados: quantas interações mostrar (1 a 100).
        score_minimo: confiança mínima de 0 a 1 (0.4 média, 0.7 alta, 0.9 muito alta).
    """
    max_resultados = max(1, min(max_resultados, 100))
    return await _executar(
        "buscar_interacoes",
        {"proteinas": proteinas, "organismo": organismo, "max_resultados": max_resultados,
         "score_minimo": score_minimo},
        string_db.buscar_interacoes(proteinas, organismo, max_resultados, score_minimo),
    )


def _pre_carregar() -> None:
    """Carrega modelo e banco em segundo plano, para a 1ª busca não esperar ~1 min."""
    try:
        _busca()._pronto()
        log.info("Busca em PDFs pronta (pré-carregada).")
    except Exception as e:
        log.info("Pré-carregamento adiado: %s", e)


def main() -> None:
    """Inicia o servidor (stdio) e pré-carrega a busca em PDFs em segundo plano."""
    if PDFS_ATIVOS:
        threading.Thread(target=_pre_carregar, daemon=True).start()
    else:
        log.info("Modo leve: busca em PDFs desativada (sem config.toml com [qdrant]).")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
