import httpx
import pytest
import respx

from agente_pesquisa.contrato import ErroEsperado
from agente_pesquisa.fontes import blast

SEQ = "MEAIAKYDFKATADDELSFKRGDILKVLNEECDQNWYKAELNGKDGFIPKNYIEMKPH"

RESULTADO = {
    "hits": [
        {
            "hit_acc": "P62993", "hit_uni_de": "Growth factor receptor-bound protein 2",
            "hit_uni_os": "Homo sapiens", "hit_uni_gn": "GRB2", "hit_len": 217,
            "hit_hsps": [
                {"hsp_identity": 100.0, "hsp_align_len": 58, "hsp_expect": 1e-35,
                 "hsp_bit_score": 120.5, "hsp_query_from": 1, "hsp_query_to": 58},
                {"hsp_identity": 40.0, "hsp_align_len": 20, "hsp_expect": 0.5,
                 "hsp_bit_score": 20.0, "hsp_query_from": 1, "hsp_query_to": 20},
            ],
        },
        {
            "hit_acc": "Q60631", "hit_uni_de": "Growth factor receptor-bound protein 2",
            "hit_uni_os": "Mus musculus", "hit_uni_gn": "Grb2", "hit_len": 217,
            "hit_hsps": [{"hsp_identity": 98.3, "hsp_align_len": 58, "hsp_expect": 2e-35,
                          "hsp_bit_score": 119.0, "hsp_query_from": 1, "hsp_query_to": 58}],
        },
    ]
}


@respx.mock
async def test_fluxo_completo(email):
    run = respx.post(f"{blast.EBI_BLAST}/run").respond(200, text="ncbiblast-R20261116-1")
    respx.get(f"{blast.EBI_BLAST}/status/ncbiblast-R20261116-1").mock(side_effect=[
        httpx.Response(200, text="RUNNING"), httpx.Response(200, text="FINISHED"),
    ])
    respx.get(f"{blast.EBI_BLAST}/result/ncbiblast-R20261116-1/json").respond(200, json=RESULTADO)

    r = await blast.buscar_similares(">GRB2\n" + SEQ, max_resultados=5)

    enviado = run.calls[0].request.content.decode()
    assert "program=blastp" in enviado and "uniprotkb_swissprot" in enviado
    assert "teste%40exemplo.com" in enviado
    assert "P62993" in r.texto and "Q60631" in r.texto
    # Usa o melhor HSP (menor e-value), não o primeiro qualquer.
    assert "Identidade 100.0% em 58 aa" in r.texto
    assert "cobre 100% da consulta" in r.texto
    assert r.auditoria["job_ebi"] == "ncbiblast-R20261116-1"


async def test_exige_email(monkeypatch):
    monkeypatch.delenv("AGENTE_PESQUISA_EMAIL", raising=False)
    monkeypatch.setattr(blast, "email_contato", lambda: None)
    with pytest.raises(ErroEsperado, match="e-mail"):
        await blast.buscar_similares(SEQ)


@pytest.mark.parametrize("seq, msg", [
    ("MEAIAK", "curta"),
    ("ATGCATGCATGC1234JJ", "não são aminoácidos"),
    ("", "vazia"),
])
async def test_valida_sequencia(email, seq, msg):
    with pytest.raises(ErroEsperado, match=msg):
        await blast.buscar_similares(seq)


async def test_banco_invalido(email):
    with pytest.raises(ErroEsperado, match="desconhecido"):
        await blast.buscar_similares(SEQ, banco="genbank")


@respx.mock
async def test_job_com_erro(email):
    respx.post(f"{blast.EBI_BLAST}/run").respond(200, text="job-1")
    respx.get(f"{blast.EBI_BLAST}/status/job-1").respond(200, text="FAILURE")
    with pytest.raises(ErroEsperado, match="FAILURE"):
        await blast.buscar_similares(SEQ)


@respx.mock
async def test_sem_hits(email):
    respx.post(f"{blast.EBI_BLAST}/run").respond(200, text="job-2")
    respx.get(f"{blast.EBI_BLAST}/status/job-2").respond(200, text="FINISHED")
    respx.get(f"{blast.EBI_BLAST}/result/job-2/json").respond(200, json={"hits": []})
    with pytest.raises(ErroEsperado, match="Nenhuma proteína parecida"):
        await blast.buscar_similares(SEQ)


@respx.mock
async def test_timeout_devolve_link(email, monkeypatch):
    monkeypatch.setattr(blast, "ESPERA_MAX_S", 0)
    respx.post(f"{blast.EBI_BLAST}/run").respond(200, text="job-3")
    respx.get(f"{blast.EBI_BLAST}/status/job-3").respond(200, text="RUNNING")
    with pytest.raises(ErroEsperado, match="jobId=job-3"):
        await blast.buscar_similares(SEQ)
