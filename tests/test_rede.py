"""Testes contra as APIs reais. Pulados por padrão (CI sem rede).

Rode na sua máquina antes de publicar uma versão:
    AGENTE_PESQUISA_EMAIL=voce@exemplo.com RODAR_REDE=1 pytest tests/test_rede.py -v
(No PowerShell: $env:RODAR_REDE=1; $env:AGENTE_PESQUISA_EMAIL="voce@exemplo.com"; pytest tests/test_rede.py -v)
"""

import os

import pytest

from agente_pesquisa.fontes import blast, pubtator, string_db, uniprot

pytestmark = pytest.mark.skipif(not os.environ.get("RODAR_REDE"), reason="defina RODAR_REDE=1")

# SH3 N-terminal do GRB2 humano (P62993).
GRB2_SH3 = "MEAIAKYDFKATADDELSFKRGDILKVLNEECDQNWYKAELNGKDGFIPKNYIEMKPH"


async def test_uniprot_real():
    r = await uniprot.buscar("GRB2", organismo="human", max_resultados=1)
    assert "P62993" in r.texto


async def test_string_parceiros_real():
    r = await string_db.buscar_interacoes(["GRB2"], max_resultados=5)
    assert "SOS1" in r.texto or "SHC1" in r.texto, r.texto
    assert "evidência: -" not in r.texto, "canais de evidência não foram lidos"


async def test_string_rede_real():
    r = await string_db.buscar_interacoes(["GRB2", "SOS1", "FGFR2"], score_minimo=0.4)
    assert "GRB2 — SOS1" in r.texto or "SOS1 — GRB2" in r.texto, r.texto


@pytest.mark.skipif(not os.environ.get("AGENTE_PESQUISA_EMAIL"), reason="BLAST exige e-mail")
async def test_blast_real():
    r = await blast.buscar_similares(GRB2_SH3, max_resultados=3)
    assert "P62993" in r.texto, r.texto
    assert "Identidade 0.0%" not in r.texto, "campos do HSP não foram lidos"


async def test_pubtator_real():
    r = await pubtator.buscar_artigos("@GENE_GRB2")
    assert "PMID" in r.texto
