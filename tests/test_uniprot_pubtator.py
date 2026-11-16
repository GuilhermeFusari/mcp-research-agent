import pytest
import respx

from agente_pesquisa.contrato import ErroEsperado
from agente_pesquisa.fontes import pubtator, uniprot


@pytest.mark.parametrize("consulta, tipo", [
    ("insulin", "nome"),
    ("TP53", "nome"),
    ("defensin", "nome"),
    ("MEAIAKYDFK", "peptideo"),
    ("ATGCATGCATGCATGC", "nucleotideo"),
    (">sp|P62993\nMEAIAKYDFKATADDE", "peptideo"),
])
def test_detectar_tipo(consulta, tipo):
    assert uniprot.detectar_tipo(consulta) == tipo


@respx.mock
async def test_busca_por_nome_formata_entrada():
    respx.get(f"{uniprot.UNIPROT_API}/search").respond(200, json={"results": [{
        "primaryAccession": "P62993", "entryType": "UniProtKB reviewed (Swiss-Prot)",
        "proteinDescription": {"recommendedName": {"fullName": {"value": "Growth factor receptor-bound protein 2"}}},
        "genes": [{"geneName": {"value": "GRB2"}}],
        "organism": {"scientificName": "Homo sapiens"},
        "sequence": {"length": 217},
        "comments": [{"commentType": "FUNCTION", "texts": [{"value": "Adapter protein."}]}],
        "uniProtKBCrossReferences": [{"database": "PDB", "id": "1GRI"}],
    }]})
    r = await uniprot.buscar("GRB2")
    assert "Growth factor receptor-bound protein 2 — P62993" in r.texto
    assert "Swiss-Prot (revisada)" in r.texto and "1GRI" in r.texto


async def test_recusa_dna():
    with pytest.raises(ErroEsperado, match="DNA/RNA"):
        await uniprot.buscar("ATGCATGCATGCATGC")


@respx.mock
async def test_buscar_artigos():
    respx.get(f"{pubtator.PUBTATOR_API}/search/").respond(200, json={
        "count": 1, "current": 1, "total_pages": 1,
        "results": [{"pmid": 123, "date": "2020-01-01", "journal": "Nature",
                     "title": "GRB2  dimers", "pmcid": "PMC1"}],
    })
    r = await pubtator.buscar_artigos("GRB2")
    assert "PMID 123 (2020, Nature): GRB2 dimers [PMC1]" in r.texto


async def test_relacoes_exige_id():
    with pytest.raises(ErroEsperado, match="não é um ID"):
        await pubtator.buscar_relacoes("doxorubicin")
