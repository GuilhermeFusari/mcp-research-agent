"""
Servidor MCP do agente de pesquisa.

O QUE É UM SERVIDOR MCP?
É um programa que "anuncia" ferramentas (nome, descrição, parâmetros) para um
cliente como o Claude Desktop. O Claude lê essas descrições, decide quando
chamar cada ferramenta, e o servidor executa e devolve o resultado.

COMO O CLAUDE DESKTOP CONVERSA COM ELE?
Via "stdio": o Claude Desktop abre este script como subprocesso e troca
mensagens JSON pela entrada/saída padrão (stdin/stdout).
CONSEQUÊNCIA IMPORTANTE: nunca use print() aqui! Qualquer coisa escrita no
stdout corrompe o protocolo. Para mensagens de debug, use logging (vai
para o stderr, que é um canal separado).
"""

import asyncio
import logging
import os
from typing import Literal

# O modelo de embeddings já foi baixado pelo indexador. Modo offline impede
# que o servidor tente falar com o Hugging Face a cada início (mais rápido,
# funciona sem internet e nada sai da máquina por esse caminho).
# Precisa vir ANTES de importar módulos que carregam o Hugging Face.
os.environ.setdefault("HF_HUB_OFFLINE", "1")

# No SDK mcp 2.x a classe se chama MCPServer (na 1.x era FastMCP).
from mcp.server import MCPServer  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

import busca_pdfs  # noqa: E402
import rag  # noqa: E402
import seguranca  # noqa: E402
import uniprot  # noqa: E402

# logging escreve no stderr por padrão -> seguro para o protocolo stdio.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("agente-pesquisa")

# Política de segurança (seção [seguranca] do config.toml), lida uma vez no início.
CFG_SEG = rag.carregar_config().get("seguranca", {})

# "instructions" é um texto que o cliente pode passar ao Claude explicando
# para que serve o servidor como um todo.
mcp = MCPServer(
    name="agente-pesquisa",
    # Instruções diretivas: sem elas, o Claude responde de memória sobre temas
    # que ele já conhece (ex.: p53) e não chama as ferramentas.
    instructions=(
        "Assistente de pesquisa científica com fontes verificáveis. REGRAS: "
        "(1) Sempre que a conversa mencionar uma proteína, peptídeo, gene ou "
        "sequência de aminoácidos, consulte buscar_proteina ANTES de responder, "
        "mesmo que você já conheça o assunto: os dados do UniProt são mais "
        "atuais e citáveis. "
        "(2) Para perguntas científicas ou técnicas, use também buscar_pdfs para "
        "verificar se a biblioteca local do usuário tem algo relevante, e cite "
        "arquivo e página. "
        "(3) Deixe claro o que veio das ferramentas e o que é conhecimento geral."
    ),
)


# O decorator @mcp.tool() registra a função como ferramenta. O SDK lê:
#   - o NOME da função             -> nome da ferramenta
#   - os TIPOS dos parâmetros      -> esquema JSON que o Claude precisa seguir
#   - a DOCSTRING                  -> descrição que o Claude lê para decidir
#                                     quando e como usar a ferramenta
# Ou seja: a docstring aqui não é só comentário, é "prompt" para o modelo.
# Vale caprichar nela.
@mcp.tool()
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
    # Validar entradas protege a API e o contexto do Claude de pedidos absurdos.
    max_resultados = max(1, min(max_resultados, 25))
    log.info("buscar_proteina: consulta=%r tipo=%s organismo=%s", consulta, tipo, organismo)

    try:
        saida = await uniprot.buscar(consulta, tipo, organismo, max_resultados)
        # Auditoria também aqui: a consulta sai da máquina (vai para o UniProt).
        seguranca.registrar(
            "buscar_proteina",
            {"consulta": consulta, "tipo": tipo, "organismo": organismo},
            saida, CFG_SEG,
        )
        return saida
    except uniprot.ErroUniProt as e:
        # ToolError = "falha esperada": o Claude recebe a mensagem e sabe que a
        # chamada falhou (is_error=True), podendo explicar ou tentar de novo.
        # Qualquer outra exceção vira um erro genérico sem detalhes.
        raise ToolError(str(e)) from e


@mcp.tool()
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
        # A busca usa CPU pesado (rodar o modelo). asyncio.to_thread roda em
        # outra thread, para o servidor continuar respondendo enquanto isso.
        saida, detalhes = await asyncio.to_thread(busca_pdfs.buscar, pergunta, k, arquivo)
    except busca_pdfs.ErroBusca as e:
        raise ToolError(str(e)) from e
    # AUDITORIA: registra o que está prestes a sair para o Claude.
    seguranca.registrar(
        "buscar_pdfs", {"pergunta": pergunta, "k": k, "arquivo": arquivo},
        saida, CFG_SEG, **detalhes,
    )
    return saida


@mcp.tool()
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


# Só roda quando executamos o arquivo diretamente (python servidor.py).
# transport="stdio" é o que o Claude Desktop e o Inspector usam.
if __name__ == "__main__":
    mcp.run(transport="stdio")
