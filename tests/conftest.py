import pytest

from agente_pesquisa.fontes import blast, pubtator, string_db
from agente_pesquisa.fontes.http import LimiteDeTaxa


@pytest.fixture(autouse=True)
def sem_espera(monkeypatch):
    """Testes não esperam limite de taxa nem polling."""
    for mod in (blast, pubtator, string_db):
        monkeypatch.setattr(mod, "LIMITE", LimiteDeTaxa(0))
    monkeypatch.setattr(blast, "INTERVALO_STATUS_S", 0)


@pytest.fixture
def email(monkeypatch):
    monkeypatch.setenv("AGENTE_PESQUISA_EMAIL", "teste@exemplo.com")
