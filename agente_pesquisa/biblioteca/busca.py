"""Busca semântica nos PDFs indexados (facade sobre Qdrant + embeddings + filtros de segurança)."""

import logging
from functools import lru_cache

from agente_pesquisa import seguranca
from agente_pesquisa.biblioteca import qdrant
from agente_pesquisa.biblioteca.embeddings import criar_embeddings
from agente_pesquisa.config import carregar_config
from agente_pesquisa.contrato import ErroEsperado, Resultado

log = logging.getLogger("agente-pesquisa")

MAX_CHARS_TRECHO = 1200


@lru_cache(maxsize=1)
def _config() -> dict:
    return carregar_config()


def _pronto():
    """Garante que o Qdrant está no ar e devolve os recursos (cliente, modelo...)."""
    try:
        qdrant.garantir_qdrant(_config())
    except Exception as e:
        raise ErroEsperado(f"Não consegui iniciar o banco vetorial. {qdrant.DICA_CONEXAO}") from e
    return _recursos()


@lru_cache(maxsize=1)
def _recursos():
    """Carrega config, modelo e conexão UMA vez e guarda (cache)."""
    config = _config()
    colecao = config["qdrant"]["colecao"]
    try:
        client = qdrant.criar_cliente(config)
        existe = client.collection_exists(colecao)
    except Exception as e:
        raise ErroEsperado(
            f"Não consegui conectar no banco vetorial (Qdrant). {qdrant.DICA_CONEXAO}"
        ) from e
    if not existe:
        raise ErroEsperado("O índice de PDFs ainda não existe. Rode primeiro: python indexar.py")

    from langchain_qdrant import QdrantVectorStore

    store = QdrantVectorStore(
        client=client, collection_name=colecao, embedding=criar_embeddings(config)
    )
    return client, colecao, store, config.get("seguranca", {})


def buscar(pergunta: str, k: int = 5, arquivo: str | None = None) -> Resultado:
    """Devolve os k trechos mais parecidos (em significado) com a pergunta."""
    from qdrant_client import models

    client, colecao, store, cfg_seg = _pronto()
    permitidos, bloqueados = _separar_por_classificacao(client, colecao, cfg_seg)

    condicoes_must, condicoes_must_not = [], []
    if bloqueados:
        condicoes_must_not.append(models.FieldCondition(
            key="metadata.caminho", match=models.MatchAny(any=sorted(bloqueados))
        ))

    if arquivo:
        nomes = {n for n in permitidos.values() if arquivo.lower() in n.lower()}
        if not nomes:
            raise ErroEsperado(f"Nenhum PDF indexado tem '{arquivo}' no nome. Use listar_pdfs para ver os nomes.")
        condicoes_must.append(models.FieldCondition(
            key="metadata.arquivo", match=models.MatchAny(any=sorted(nomes))
        ))

    filtro = models.Filter(must=condicoes_must, must_not=condicoes_must_not)

    resultados = store.similarity_search_with_score(pergunta, k=k, filter=filtro)
    if not resultados:
        raise ErroEsperado("Nenhum trecho encontrado" + (f" no arquivo '{arquivo}'." if arquivo else "."))

    blocos, fontes, mascaramentos = [], [], {}
    for i, (doc, score) in enumerate(resultados, start=1):
        m = doc.metadata
        texto = " ".join(doc.page_content.split())
        if len(texto) > MAX_CHARS_TRECHO:
            texto = texto[:MAX_CHARS_TRECHO] + "..."
        if cfg_seg.get("mascarar_dados_pessoais", True):
            texto, contagem = seguranca.mascarar(texto)
            for tipo, n in contagem.items():
                mascaramentos[tipo] = mascaramentos.get(tipo, 0) + n
        aviso_ocr = " [texto lido por OCR, pode conter erros]" if m.get("ocr") else ""
        blocos.append(
            f"[{i}] {m.get('arquivo')} — página {m.get('pagina')} "
            f"(similaridade {score:.2f}){aviso_ocr}\n{texto}"
        )
        fontes.append({"arquivo": m.get("arquivo"), "pagina": m.get("pagina")})

    saida = (
        f"{len(resultados)} trecho(s) mais relevantes nos PDFs locais para: '{pergunta}'\n\n"
        + "\n\n---\n\n".join(blocos)
    )
    return Resultado(saida, auditoria={
        "fontes": fontes, "mascaramentos": mascaramentos,
        "pdfs_bloqueados_por_classificacao": len(bloqueados),
    })


def _mapa_caminhos(client, colecao: str) -> dict[str, tuple[str, int]]:
    """{caminho: (nome do arquivo, nº de trechos)}, lendo só os metadados."""
    mapa: dict[str, tuple[str, int]] = {}
    offset = None
    while True:
        pontos, offset = client.scroll(
            colecao, limit=1000, offset=offset,
            with_payload=["metadata.arquivo", "metadata.caminho"], with_vectors=False,
        )
        for p in pontos:
            md = p.payload["metadata"]
            nome, n = mapa.get(md["caminho"], (md["arquivo"], 0))
            mapa[md["caminho"]] = (nome, n + 1)
        if offset is None:
            return mapa


def _separar_por_classificacao(client, colecao: str, cfg_seg: dict) -> tuple[dict[str, str], set[str]]:
    """Divide os PDFs em permitidos ({caminho: nome}) e bloqueados ({caminhos})."""
    permitidos, bloqueados = {}, set()
    for caminho, (nome, _) in _mapa_caminhos(client, colecao).items():
        if seguranca.pode_enviar(caminho, cfg_seg):
            permitidos[caminho] = nome
        else:
            bloqueados.add(caminho)
    return permitidos, bloqueados


def listar_arquivos() -> Resultado:
    """Lista os PDFs do índice (nome e nº de trechos), sem conteúdo."""
    client, colecao, _, cfg_seg = _pronto()
    mapa = _mapa_caminhos(client, colecao)
    visiveis = [(nome, n) for c, (nome, n) in mapa.items() if seguranca.pode_enviar(c, cfg_seg)]
    ocultos = len(mapa) - len(visiveis)

    linhas = [f"- {nome} ({n} trechos)" for nome, n in sorted(visiveis)]
    saida = f"{len(visiveis)} PDF(s) indexados:\n" + "\n".join(linhas)
    if ocultos:
        saida += f"\n\n({ocultos} PDF(s) ocultos por classificação de segurança.)"
    return Resultado(saida, auditoria={"pdfs_bloqueados_por_classificacao": ocultos})


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.WARNING)
    resultado = buscar(" ".join(sys.argv[1:]) or "peptídeos antimicrobianos")
    print(resultado.texto)
    print("\n[auditoria]", resultado.auditoria)
