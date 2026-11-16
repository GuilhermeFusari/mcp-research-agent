"""O servidor sobe sem config.toml (modo leve) e liga os PDFs só na instalação completa."""

import importlib
import importlib.util

import pytest

FERRAMENTAS_WEB = {
    "buscar_proteina", "buscar_similares", "buscar_interacoes",
    "buscar_entidade", "buscar_artigos", "ler_artigo", "buscar_relacoes",
}


def _recarregar(monkeypatch, pasta, config_toml=None, tem_deps=False):
    monkeypatch.setenv("AGENTE_PESQUISA_HOME", str(pasta))
    if config_toml is not None:
        (pasta / "config.toml").write_text(config_toml, encoding="utf-8")
    original = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util, "find_spec",
        lambda nome, *a: object() if (nome == "langchain_qdrant" and tem_deps) else
        (None if nome == "langchain_qdrant" else original(nome, *a)),
    )
    from agente_pesquisa import config, seguranca
    importlib.reload(config)
    importlib.reload(seguranca)
    from agente_pesquisa import ferramentas
    return importlib.reload(ferramentas)


async def _nomes(mod):
    return {t.name for t in await mod.mcp.list_tools()}


async def test_modo_leve_sem_config(monkeypatch, tmp_path):
    mod = _recarregar(monkeypatch, tmp_path)
    assert not mod.PDFS_ATIVOS
    assert await _nomes(mod) == FERRAMENTAS_WEB
    assert "buscar_pdfs" not in mod.INSTRUCOES


async def test_pdfs_com_instalacao_completa(monkeypatch, tmp_path):
    mod = _recarregar(monkeypatch, tmp_path, '[qdrant]\nurl = "http://127.0.0.1:6333"\ncolecao = "pdfs"\n', True)
    assert mod.PDFS_ATIVOS
    assert await _nomes(mod) == FERRAMENTAS_WEB | {"buscar_pdfs", "listar_pdfs"}


async def test_config_sem_deps_nao_liga_pdfs(monkeypatch, tmp_path):
    mod = _recarregar(monkeypatch, tmp_path, '[qdrant]\nurl = "x"\ncolecao = "pdfs"\n', False)
    assert not mod.PDFS_ATIVOS


async def test_auditoria_vai_para_pasta_de_dados(monkeypatch, tmp_path):
    mod = _recarregar(monkeypatch, tmp_path)
    mod.seguranca.registrar("teste", {"a": 1}, "saida", {})
    assert (tmp_path / "logs" / "auditoria.jsonl").exists()


@pytest.fixture(autouse=True)
def _restaurar(monkeypatch):
    yield
    monkeypatch.undo()
    from agente_pesquisa import config, ferramentas, seguranca
    importlib.reload(config)
    importlib.reload(seguranca)
    importlib.reload(ferramentas)
