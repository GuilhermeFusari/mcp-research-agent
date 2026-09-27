"""
Peças compartilhadas do RAG: configuração, modelo de embeddings e conexão
com o Qdrant.

POR QUE UM MÓDULO COMPARTILHADO?
O indexador (indexar.py) e a busca (servidor.py) PRECISAM usar exatamente o
mesmo modelo e as mesmas configurações. Se o indexador gerar vetores com o
modelo A e a busca usar o modelo B, os números não são comparáveis e a
busca devolve lixo, sem dar erro nenhum. Centralizar aqui evita isso.
"""

import logging
import os
import tomllib
from pathlib import Path

# Desliga a telemetria do Hugging Face (de onde o modelo é baixado).
# Precisa vir ANTES de importar as bibliotecas que usam o Hugging Face.
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

from langchain_huggingface import HuggingFaceEmbeddings  # noqa: E402
from qdrant_client import QdrantClient  # noqa: E402

log = logging.getLogger("agente-pesquisa")

# Caminho do config relativo a ESTE arquivo, não à pasta de onde você roda
# o comando. O Claude Desktop inicia o servidor de outra pasta, e um caminho
# relativo simples ("config.toml") quebraria lá.
PASTA_PROJETO = Path(__file__).resolve().parent
CONFIG_PADRAO = PASTA_PROJETO / "config.toml"


def carregar_config(caminho: Path | str | None = None) -> dict:
    """Lê o config.toml e devolve um dicionário."""
    with open(caminho or CONFIG_PADRAO, "rb") as f:  # tomllib exige modo binário
        return tomllib.load(f)


def criar_embeddings(config: dict) -> HuggingFaceEmbeddings:
    """Carrega o modelo de embeddings (baixa na 1ª vez, ~1 GB; depois usa o cache).

    SOBRE O MODELO E5: ele foi treinado com prefixos que dizem o PAPEL de cada
    texto: "passage: " para os trechos guardados e "query: " para as perguntas.
    Sem os prefixos ele funciona, mas pior. O LangChain permite configurar
    um prefixo para documentos (encode_kwargs) e outro para buscas
    (query_encode_kwargs).

    normalize_embeddings=True deixa todo vetor com comprimento 1. Assim a
    similaridade de cosseno (usada no Qdrant) mede só a DIREÇÃO do vetor,
    ou seja, o significado, e não o tamanho do texto.
    """
    # Usa a GPU se houver PyTorch com CUDA; senão, CPU. Mesmo código nos dois casos.
    import torch

    dispositivo = "cuda" if torch.cuda.is_available() else "cpu"
    log.info("Embeddings: modelo=%s dispositivo=%s", config["embeddings"]["modelo"], dispositivo)

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


# ---------------------------------------------------------------------------
# Qdrant como processo "sidecar" (sem Docker)
# ---------------------------------------------------------------------------

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

    PADRÃO SIDECAR: o programa principal (indexador ou servidor MCP) sobe um
    processo auxiliar e conversa com ele por HTTP, como se fosse o Docker.

    CICLO DE VIDA: no Windows, o Qdrant costuma ser encerrado junto com o
    programa que o ligou (o sistema agrupa pai e filhos num "job"). Isso é
    bom, porque não sobra processo gastando memória depois. Mas significa
    que ele pode sumir a qualquer momento (ex.: o indexador ligou e terminou).
    Por isso esta função é chamada ANTES DE CADA USO: se ele estiver no ar,
    custa uma checagem de ~1 ms; se não estiver, liga de novo.

    No modo "docker" não fazemos nada: quem liga é o Docker Desktop.
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
    # O Qdrant é configurado por variáveis de ambiente (QDRANT__SECAO__CHAVE).
    env = dict(
        os.environ,
        QDRANT__SERVICE__HOST="127.0.0.1",          # só este PC acessa (como no Docker)
        QDRANT__SERVICE__HTTP_PORT=str(porta),
        QDRANT__SERVICE__GRPC_PORT=str(porta + 1),
        QDRANT__STORAGE__STORAGE_PATH=str(PASTA_PROJETO / "qdrant_data"),
        QDRANT__TELEMETRY_DISABLED="true",         # não envia estatísticas de uso
    )
    pasta_logs = PASTA_PROJETO / "logs"
    pasta_logs.mkdir(exist_ok=True)

    log.info("Ligando o Qdrant em segundo plano...")
    # Flags do Windows:
    #   CREATE_NO_WINDOW          sem janela preta aparecendo
    #   CREATE_NEW_PROCESS_GROUP  um Ctrl+C no terminal não chega ao Qdrant
    #                             (ele encerra de forma limpa com o processo pai)
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    with open(pasta_logs / "qdrant.log", "ab") as saida:
        subprocess.Popen(
            [str(EXE_QDRANT)], cwd=EXE_QDRANT.parent, env=env,
            stdin=subprocess.DEVNULL, stdout=saida, stderr=saida, creationflags=flags,
        )

    # Espera ficar pronto (normalmente 1-3 s).
    for _ in range(40):
        if _qdrant_respondendo(url):
            return
        time.sleep(0.5)
    raise RuntimeError("O Qdrant não iniciou em 20 s. Veja logs/qdrant.log.")
