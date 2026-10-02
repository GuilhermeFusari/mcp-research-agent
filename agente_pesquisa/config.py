"""Configuração e caminhos do projeto."""

import tomllib
from pathlib import Path

# Raiz do projeto (uma pasta acima do pacote): config.toml, bin/, logs/ e qdrant_data/ ficam lá.
PASTA_PROJETO = Path(__file__).resolve().parent.parent
CONFIG_PADRAO = PASTA_PROJETO / "config.toml"


def carregar_config(caminho: Path | str | None = None) -> dict:
    """Lê o config.toml e devolve um dicionário."""
    with open(caminho or CONFIG_PADRAO, "rb") as f:
        return tomllib.load(f)
