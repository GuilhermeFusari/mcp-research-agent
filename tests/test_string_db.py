from urllib.parse import parse_qs

import pytest
import respx

from agente_pesquisa.contrato import ErroEsperado
from agente_pesquisa.fontes import string_db

API = string_db.STRING_API


def _ids(*nomes):
    return [
        {"queryItem": n, "stringId": f"9606.ENSP{i:011d}", "preferredName": n.upper(),
         "taxonName": "Homo sapiens"}
        for i, n in enumerate(nomes)
    ]


def _aresta(a, b, score, **canais):
    return {"preferredName_A": a, "preferredName_B": b, "score": score, **canais}


@respx.mock
async def test_parceiros_de_uma_proteina():
    respx.post(f"{API}/get_string_ids").respond(200, json=_ids("GRB2"))
    parceiros = respx.post(f"{API}/interaction_partners").respond(200, json=[
        _aresta("GRB2", "SOS1", 0.999, escore=0.99, dscore=0.9, tscore=0.95),
        _aresta("GRB2", "SHC1", 0.998, escore=0.98, tscore=0.9),
    ])

    r = await string_db.buscar_interacoes(["GRB2"], max_resultados=5)

    form = parse_qs(parceiros.calls[0].request.content.decode())
    assert form["required_score"] == ["700"] and form["limit"] == ["5"]
    assert form["caller_identity"] == ["mcp-research-agent"]
    assert "parceiros de GRB2 -> GRB2" in r.texto
    assert "GRB2 — SOS1 | score 0.999 | evidência: experimentos 0.99, mineração de texto 0.95" in r.texto
    assert "string-db.org/network/9606.ENSP" in r.texto


@respx.mock
async def test_rede_remove_arestas_duplicadas():
    respx.post(f"{API}/get_string_ids").respond(200, json=_ids("GRB2", "SOS1"))
    respx.post(f"{API}/network").respond(200, json=[
        _aresta("GRB2", "SOS1", 0.999), _aresta("SOS1", "GRB2", 0.999),
    ])
    r = await string_db.buscar_interacoes(["GRB2", "SOS1", "NAOEXISTE"])
    assert r.texto.count("score 0.999") == 1
    assert "rede entre" in r.texto
    assert "Não encontradas no STRING: NAOEXISTE" in r.texto


@respx.mock
async def test_nenhuma_proteina():
    respx.post(f"{API}/get_string_ids").respond(200, json=[])
    with pytest.raises(ErroEsperado, match="Nenhuma das proteínas"):
        await string_db.buscar_interacoes(["XYZ123"])


@respx.mock
async def test_sem_interacoes_sugere_score_menor():
    respx.post(f"{API}/get_string_ids").respond(200, json=_ids("GRB2"))
    respx.post(f"{API}/interaction_partners").respond(200, json=[])
    with pytest.raises(ErroEsperado, match="score_minimo menor"):
        await string_db.buscar_interacoes(["GRB2"], score_minimo=0.99)


@pytest.mark.parametrize("entrada, taxon", [
    (None, 9606), ("", 9606), ("human", 9606), ("Mus musculus", 10090), ("10116", 10116), (7955, 7955),
])
def test_resolver_organismo(entrada, taxon):
    assert string_db.resolver_organismo(entrada) == taxon


def test_organismo_desconhecido():
    with pytest.raises(ErroEsperado, match="não reconhecido"):
        string_db.resolver_organismo("dragão")


async def test_lista_vazia():
    with pytest.raises(ErroEsperado, match="ao menos uma"):
        await string_db.buscar_interacoes(["  "])
