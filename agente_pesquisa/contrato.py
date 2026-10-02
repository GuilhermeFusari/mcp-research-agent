"""Contrato comum a todas as fontes de dados (UniProt, PubTator, PDFs locais).

Toda operação pública de uma fonte devolve um Resultado ou levanta ErroEsperado.
Assim a camada MCP (ferramentas.py) trata todas as fontes do mesmo jeito.
"""

from dataclasses import dataclass, field


class ErroEsperado(Exception):
    """Falha prevista (serviço fora, nada encontrado...), com mensagem legível para o Claude."""


@dataclass
class Resultado:
    """Texto que vai para o Claude + dados extras que vão só para o log de auditoria."""

    texto: str
    auditoria: dict = field(default_factory=dict)
