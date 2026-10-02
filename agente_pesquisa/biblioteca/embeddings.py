"""Modelo de embeddings (texto -> vetor), carregado localmente."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

# Importações pesadas ficam dentro da função: o servidor MCP precisa iniciar rápido.
if TYPE_CHECKING:
    from langchain_huggingface import HuggingFaceEmbeddings

log = logging.getLogger("agente-pesquisa")


def criar_embeddings(config: dict) -> HuggingFaceEmbeddings:
    """Carrega o modelo de embeddings (baixa na 1ª vez, ~1 GB; depois usa o cache)."""
    import torch
    from langchain_huggingface import HuggingFaceEmbeddings

    dispositivo = "cuda" if torch.cuda.is_available() else "cpu"
    log.info("Embeddings: modelo=%s dispositivo=%s", config["embeddings"]["modelo"], dispositivo)

    # Modelos E5 foram treinados com os prefixos "passage: " (documentos) e "query: " (buscas).
    return HuggingFaceEmbeddings(
        model_name=config["embeddings"]["modelo"],
        model_kwargs={"device": dispositivo},
        encode_kwargs={"prompt": "passage: ", "normalize_embeddings": True},
        query_encode_kwargs={"prompt": "query: ", "normalize_embeddings": True},
    )
