"""Peças compartilhadas do RAG: configuração, modelo de embeddings e conexão com o Qdrant."""

import logging
import os
import tomllib
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

from langchain_huggingface import HuggingFaceEmbeddings  # noqa: E402
from qdrant_client import QdrantClient  # noqa: E402

log = logging.getLogger("agente-pesquisa")

PASTA_PROJETO = Path(__file__).resolve().parent
CONFIG_PADRAO = PASTA_PROJETO / "config.toml"


def carregar_config(caminho: Path | str | None = None) -> dict:
    """Lê o config.toml e devolve um dicionário."""
    with open(caminho or CONFIG_PADRAO, "rb") as f:
        return tomllib.load(f)


def criar_embeddings(config: dict) -> HuggingFaceEmbeddings:
    """Carrega o modelo de embeddings (baixa na 1ª vez, ~1 GB; depois usa o cache)."""
    import torch

    dispositivo = "cuda" if torch.cuda.is_available() else "cpu"
    log.info("Embeddings: modelo=%s dispositivo=%s", config["embeddings"]["modelo"], dispositivo)

    # Modelos E5 foram treinados com os prefixos "passage: " (documentos) e "query: " (buscas).
    return HuggingFaceEmbeddings(
        model_name=config["embeddings"]["modelo"],
        model_kwargs={"device": dispositivo},
        encode_kwargs={"prompt": "passage: ", "normalize_embeddings": True},
        query_encode_kwargs={"prompt": "query: ", "normalize_embeddings": True},
    )


def criar_cliente_qdrant(config: dict) -> QdrantClient:
    """Conecta no Qdrant, ligando o executável antes se for o caso."""
    garantir_qdrant(config)
    return QdrantClient(url=config["qdrant"]["url"])


EXE_QDRANT = PASTA_PROJETO / "bin" / "qdrant.exe"


def dica_conexao(config: dict) -> str:
    """Mensagem de ajuda certa para cada modo, usada quando a conexão falha."""
    if config["qdrant"].get("modo", "docker") == "executavel":
        return "Veja logs/qdrant.log ou rode o instalar.bat de novo."
    return "O Docker Desktop está aberto e o container rodando? (docker compose up -d)"


def _qdrant_respondendo(url: str) -> bool:
    import httpx

    try:
        return httpx.get(url, timeout=1.0).status_code == 200
    except httpx.HTTPError:
        return False


def garantir_qdrant(config: dict) -> None:
    """No modo "executavel", liga o qdrant.exe em segundo plano se ele não estiver rodando.

    No Windows o processo costuma encerrar junto com quem o iniciou (job object),
    então isto deve ser chamado antes de cada uso, não só na inicialização.
    """
    cfg = config["qdrant"]
    url = cfg["url"]
    if cfg.get("modo", "docker") != "executavel" or _qdrant_respondendo(url):
        return

    if not EXE_QDRANT.exists():
        raise RuntimeError(f"{EXE_QDRANT} não encontrado. Rode o instalar.bat de novo.")

    import subprocess
    import time
    from urllib.parse import urlparse

    porta = urlparse(url).port or 6333
    env = dict(
        os.environ,
        QDRANT__SERVICE__HOST="127.0.0.1",
        QDRANT__SERVICE__HTTP_PORT=str(porta),
        QDRANT__SERVICE__GRPC_PORT=str(porta + 1),
        QDRANT__STORAGE__STORAGE_PATH=str(PASTA_PROJETO / "qdrant_data"),
        QDRANT__TELEMETRY_DISABLED="true",
    )
    pasta_logs = PASTA_PROJETO / "logs"
    pasta_logs.mkdir(exist_ok=True)

    log.info("Ligando o Qdrant em segundo plano...")
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    with open(pasta_logs / "qdrant.log", "ab") as saida:
        subprocess.Popen(
            [str(EXE_QDRANT)], cwd=EXE_QDRANT.parent, env=env,
            stdin=subprocess.DEVNULL, stdout=saida, stderr=saida, creationflags=flags,
        )

    for _ in range(40):
        if _qdrant_respondendo(url):
            return
        time.sleep(0.5)
    raise RuntimeError("O Qdrant não iniciou em 20 s. Veja logs/qdrant.log.")
