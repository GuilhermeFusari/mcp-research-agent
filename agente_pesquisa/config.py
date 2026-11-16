"""Configuração e caminhos do projeto.

Dois modos de uso:
- Instalação completa no Windows (instalar.bat): config.toml, bin/, logs/ e
  qdrant_data/ ficam na raiz do repositório, como sempre.
- Pacote leve (uvx / pip install): não há config.toml obrigatório. Os dados
  (logs de auditoria, config opcional) ficam em ~/.agente-pesquisa, ou na pasta
  indicada pela variável de ambiente AGENTE_PESQUISA_HOME.
"""

import os
import tomllib
from pathlib import Path

# Raiz do repositório (uma pasta acima do pacote).
PASTA_PROJETO = Path(__file__).resolve().parent.parent


def _pasta_dados() -> Path:
    if os.environ.get("AGENTE_PESQUISA_HOME"):
        return Path(os.environ["AGENTE_PESQUISA_HOME"]).expanduser()
    if (PASTA_PROJETO / "config.toml").exists():
        return PASTA_PROJETO
    return Path.home() / ".agente-pesquisa"


PASTA_DADOS = _pasta_dados()
CONFIG_PADRAO = PASTA_DADOS / "config.toml"


def carregar_config(caminho: Path | str | None = None) -> dict:
    """Lê o config.toml e devolve um dicionário.

    Com caminho explícito, o arquivo é obrigatório (erro se não existir).
    Sem caminho, a ausência do config.toml padrão devolve {} (modo leve).
    """
    if caminho is None:
        if not CONFIG_PADRAO.exists():
            return {}
        caminho = CONFIG_PADRAO
    with open(caminho, "rb") as f:
        return tomllib.load(f)


def email_contato(config: dict | None = None) -> str | None:
    """E-mail exigido por algumas APIs (ex.: BLAST do EBI). Variável de ambiente tem prioridade."""
    if os.environ.get("AGENTE_PESQUISA_EMAIL"):
        return os.environ["AGENTE_PESQUISA_EMAIL"].strip()
    cfg = config if config is not None else carregar_config()
    return (cfg.get("contato", {}).get("email") or "").strip() or None
