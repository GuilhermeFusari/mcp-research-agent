"""Banco vetorial Qdrant como processo sidecar: liga o bin/qdrant.exe quando preciso e conecta."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from agente_pesquisa.config import PASTA_PROJETO

# Importação pesada fica dentro da função: o servidor MCP precisa iniciar rápido.
if TYPE_CHECKING:
    from qdrant_client import QdrantClient

log = logging.getLogger("agente-pesquisa")

EXE_QDRANT = PASTA_PROJETO / "bin" / "qdrant.exe"
DICA_CONEXAO = "Veja logs/qdrant.log ou rode o instalar.bat de novo."


def criar_cliente(config: dict) -> QdrantClient:
    """Conecta no Qdrant, ligando o executável antes se for o caso."""
    from qdrant_client import QdrantClient

    garantir_qdrant(config)
    return QdrantClient(url=config["qdrant"]["url"])


def _qdrant_respondendo(url: str) -> bool:
    import httpx

    try:
        return httpx.get(url, timeout=1.0).status_code == 200
    except httpx.HTTPError:
        return False


def garantir_qdrant(config: dict) -> None:
    """Liga o qdrant.exe em segundo plano se ele não estiver rodando.

    No Windows o processo costuma encerrar junto com quem o iniciou (job object),
    então isto deve ser chamado antes de cada uso, não só na inicialização.
    """
    url = config["qdrant"]["url"]
    if _qdrant_respondendo(url):
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
