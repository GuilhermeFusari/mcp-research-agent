"""Indexador incremental: lê os PDFs das pastas do config.toml e guarda no Qdrant.

Uso: python indexar.py [--reindexar] [--config CAMINHO] [--sim]
"""

import argparse
import hashlib
import logging
import sys
import uuid
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import pymupdf
from langchain_core.documents import Document
from langchain_qdrant import QdrantVectorStore
from langchain_text_splitters import RecursiveCharacterTextSplitter
from qdrant_client import models

from agente_pesquisa.biblioteca import qdrant
from agente_pesquisa.biblioteca.embeddings import criar_embeddings
from agente_pesquisa.config import PASTA_PROJETO, carregar_config

log = logging.getLogger("agente-pesquisa")

# Página com menos texto que isso é tratada como escaneada (vai para o OCR).
MIN_CARACTERES_PAGINA = 20

ARQUIVO_ACEITE = PASTA_PROJETO / ".aviso_aceito"

TITULO_AVISO = "Agente de Pesquisa: aviso de privacidade"

AVISO = """A indexação é 100% local: seus PDFs, os vetores e o banco ficam neste PC.

PORÉM, quando o Claude usa a ferramenta de busca, os TRECHOS encontrados (até ~15 por pergunta) são enviados ao Claude, como qualquer texto que você colasse numa conversa. A segurança é a mesma de usar o app do Claude.

Proteções ativas (ajuste em config.toml):
  • só as pastas listadas em [indexacao] são lidas
  • PDFs "confidenciais" nunca são enviados
  • CPF, e-mail, telefone etc. são mascarados antes do envio
  • todo envio é registrado em logs/auditoria.jsonl

Regra prática: se você não colaria um arquivo no Claude, não deixe ele nas pastas indexadas (ou marque-o como confidencial).

Você entendeu e quer continuar?"""


def _perguntar_janela() -> bool | None:
    """Mostra o aviso numa janela com botões Sim/Não."""
    try:
        import tkinter as tk
        from tkinter import messagebox

        raiz = tk.Tk()
        raiz.withdraw()
        raiz.attributes("-topmost", True)
        resposta = messagebox.askyesno(TITULO_AVISO, AVISO, icon="warning",
                                       default="no", parent=raiz)
        raiz.destroy()
        return resposta
    except Exception:
        return None


def confirmar_aviso(aceitar: bool) -> None:
    """Exige um aceite explícito antes da 1ª indexação. O aceite fica salvo num arquivo."""
    if ARQUIVO_ACEITE.exists():
        return

    aceito = True if aceitar else _perguntar_janela()
    if aceito is None:
        print(f"\n===== {TITULO_AVISO} =====\n{AVISO}\n")
        aceito = input("[s/N]: ").strip().lower() == "s"

    if not aceito:
        print("Cancelado. Nada foi indexado.")
        sys.exit(0)
    ARQUIVO_ACEITE.write_text(datetime.now().isoformat(timespec="seconds"), encoding="utf-8")


def descobrir_pdfs(pastas: list[str], excluir: list[str]) -> list[Path]:
    """Varre as pastas (e subpastas) da allowlist atrás de PDFs."""
    excluir = [t.lower() for t in excluir]
    encontrados = []
    for pasta in pastas:
        p = Path(pasta).expanduser()
        if not p.is_dir():
            log.warning("Pasta não existe, pulando: %s", p)
            continue
        for arq in p.rglob("*.pdf"):
            if any(t in str(arq).lower() for t in excluir):
                log.info("Excluído pelo filtro: %s", arq.name)
                continue
            encontrados.append(arq.resolve())
    return encontrados


def hash_arquivo(caminho: Path) -> str:
    """SHA-256 do conteúdo. Muda se o arquivo mudar, mesmo que o nome continue igual."""
    h = hashlib.sha256()
    with open(caminho, "rb") as f:
        for bloco in iter(lambda: f.read(1024 * 1024), b""):
            h.update(bloco)
    return h.hexdigest()


@lru_cache(maxsize=1)
def _leitor_ocr(idiomas: tuple[str, ...]):
    """Carrega o EasyOCR uma vez só (lru_cache), e só se aparecer página escaneada."""
    import easyocr
    import torch

    log.info("Carregando OCR (idiomas: %s)...", ", ".join(idiomas))
    return easyocr.Reader(list(idiomas), gpu=torch.cuda.is_available(), verbose=False)


def extrair_texto_ocr(pagina: pymupdf.Page, cfg_ocr: dict) -> str | None:
    """Lê o texto de uma página que é só imagem (PDF escaneado)."""
    if not cfg_ocr.get("ativo", True):
        return None
    import numpy as np

    pix = pagina.get_pixmap(dpi=cfg_ocr.get("dpi", 200))
    imagem = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, pix.n)
    leitor = _leitor_ocr(tuple(cfg_ocr.get("idiomas", ["pt", "en"])))
    blocos = leitor.readtext(imagem, detail=0, paragraph=True)
    return "\n\n".join(blocos)


def extrair_paginas(caminho: Path, hash_pdf: str, cfg_ocr: dict) -> tuple[list[Document], int, int]:
    """Lê o PDF e devolve (um Document por página, nº de páginas sem texto, nº com OCR)."""
    documentos, sem_texto, com_ocr = [], 0, 0
    with pymupdf.open(caminho) as pdf:
        titulo = (pdf.metadata or {}).get("title") or caminho.stem
        for num, pagina in enumerate(pdf, start=1):
            texto = pagina.get_text()
            usou_ocr = False
            if len(texto.strip()) < MIN_CARACTERES_PAGINA:
                log.info("  %s: OCR na página %d/%d...", caminho.name, num, pdf.page_count)
                texto = extrair_texto_ocr(pagina, cfg_ocr) or ""
                usou_ocr = bool(texto.strip())
                if not usou_ocr:
                    sem_texto += 1
                    continue
                com_ocr += 1
            documentos.append(Document(
                page_content=texto,
                metadata={
                    "arquivo": caminho.name,
                    "caminho": str(caminho),
                    "titulo": titulo,
                    "pagina": num,
                    "ocr": usou_ocr,
                    "hash": hash_pdf,
                },
            ))
    return documentos, sem_texto, com_ocr


def filtro(campo: str, valor: str) -> models.Filter:
    """Monta o filtro 'metadata.<campo> == valor' no formato do Qdrant."""
    return models.Filter(must=[
        models.FieldCondition(key=f"metadata.{campo}", match=models.MatchValue(value=valor))
    ])


def preparar_colecao(client, nome: str, dimensao: int) -> None:
    """Cria a coleção (a "tabela" do Qdrant) se ainda não existir."""
    if client.collection_exists(nome):
        return
    log.info("Criando coleção '%s' (vetores de %d dimensões)", nome, dimensao)
    client.create_collection(
        collection_name=nome,
        vectors_config=models.VectorParams(size=dimensao, distance=models.Distance.COSINE),
    )
    for campo in ("hash", "caminho"):
        client.create_payload_index(nome, f"metadata.{campo}", models.PayloadSchemaType.KEYWORD)


def caminhos_indexados(client, nome: str) -> set[str]:
    """Todos os caminhos de arquivo que existem hoje no índice."""
    caminhos, offset = set(), None
    while True:
        pontos, offset = client.scroll(
            nome, limit=1000, offset=offset,
            with_payload=["metadata.caminho"], with_vectors=False,
        )
        caminhos.update(p.payload["metadata"]["caminho"] for p in pontos)
        if offset is None:
            return caminhos


def main() -> None:
    parser = argparse.ArgumentParser(description="Indexa PDFs locais no Qdrant.")
    parser.add_argument("--config", help="caminho de outro config.toml (útil para testes)")
    parser.add_argument("--sim", action="store_true", help="aceita o aviso de privacidade sem perguntar")
    parser.add_argument("--reindexar", action="store_true",
                        help="reprocessa todos os PDFs, mesmo os já indexados")
    args = parser.parse_args()

    confirmar_aviso(args.sim)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    for barulhento in ("httpx", "sentence_transformers", "transformers"):
        logging.getLogger(barulhento).setLevel(logging.WARNING)

    config = carregar_config(args.config)
    colecao = config["qdrant"]["colecao"]

    try:
        client = qdrant.criar_cliente(config)
        client.get_collections()
    except Exception as e:
        log.error("Não consegui conectar no Qdrant em %s (%s). %s", config["qdrant"]["url"], e,
                  qdrant.DICA_CONEXAO)
        sys.exit(1)

    pdfs = descobrir_pdfs(config["indexacao"]["pastas"], config["indexacao"]["excluir_se_contem"])
    log.info("%d PDF(s) encontrados nas pastas permitidas.", len(pdfs))

    embeddings = criar_embeddings(config)
    preparar_colecao(client, colecao, dimensao=len(embeddings.embed_query("teste")))

    store = QdrantVectorStore(client=client, collection_name=colecao, embedding=embeddings)

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=config["chunking"]["tamanho"],
        chunk_overlap=config["chunking"]["sobreposicao"],
        separators=["\n\n", "\n", ". ", " ", ""],
    )

    atuais = {str(p) for p in pdfs}
    for caminho in caminhos_indexados(client, colecao) - atuais:
        log.info("Removendo do índice (arquivo sumiu): %s", Path(caminho).name)
        client.delete(colecao, points_selector=filtro("caminho", caminho))

    novos = pulados = falhas = total_chunks = 0
    hashes_nesta_rodada = set()

    for i, pdf in enumerate(pdfs, start=1):
        prefixo = f"[{i}/{len(pdfs)}] {pdf.name}"
        try:
            h = hash_arquivo(pdf)

            ja_indexado = not args.reindexar and client.count(colecao, count_filter=filtro("hash", h)).count
            if h in hashes_nesta_rodada or ja_indexado:
                pulados += 1
                hashes_nesta_rodada.add(h)
                continue
            hashes_nesta_rodada.add(h)

            # Mesmo caminho com hash novo = arquivo alterado: remove a versão antiga.
            client.delete(colecao, points_selector=filtro("caminho", str(pdf)))

            paginas, sem_texto, com_ocr = extrair_paginas(pdf, h, config.get("ocr", {}))
            if com_ocr:
                log.info("%s: %d página(s) lidas por OCR", prefixo, com_ocr)
            if sem_texto:
                log.warning("%s: %d página(s) sem texto nem com OCR", prefixo, sem_texto)
            if not paginas:
                log.warning("%s: nenhum texto extraído, pulando.", prefixo)
                falhas += 1
                continue

            chunks = splitter.split_documents(paginas)
            for n, c in enumerate(chunks):
                c.metadata["chunk"] = n

            # IDs determinísticos: reexecuções sobrescrevem em vez de duplicar.
            ids = [str(uuid.uuid5(uuid.NAMESPACE_URL, f"{h}:{n}")) for n in range(len(chunks))]
            store.add_documents(chunks, ids=ids)

            novos += 1
            total_chunks += len(chunks)
            log.info("%s: %d páginas -> %d chunks", prefixo, len(paginas), len(chunks))

        except Exception as e:
            falhas += 1
            log.error("%s: falhou (%s: %s)", prefixo, type(e).__name__, e)

    total = client.count(colecao).count
    log.info("Pronto. Novos: %d | Já indexados/duplicados: %d | Falhas: %d | "
             "Chunks adicionados: %d | Total no índice: %d",
             novos, pulados, falhas, total_chunks, total)


if __name__ == "__main__":
    main()
