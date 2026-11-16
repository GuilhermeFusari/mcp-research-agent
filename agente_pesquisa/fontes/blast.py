"""BLAST de proteínas pelo serviço REST do EBI (Job Dispatcher).

Complementa o Peptide Search do UniProt: lá a busca é exata; aqui encontra
sequências PARECIDAS (homólogas), com identidade e e-value de cada alinhamento.

Fluxo da API (assíncrona): POST /run -> jobId; GET /status/{jobId} até
FINISHED; GET /result/{jobId}/json. O EBI exige um e-mail de contato real.
"""

import asyncio
import re

import httpx

from agente_pesquisa.config import email_contato
from agente_pesquisa.contrato import ErroEsperado, Resultado
from agente_pesquisa.fontes.http import LimiteDeTaxa, requisitar
from agente_pesquisa.fontes.uniprot import AMINOACIDOS, limpar_sequencia

EBI_BLAST = "https://www.ebi.ac.uk/Tools/services/rest/ncbiblast"
PAGINA_JOB = "https://www.ebi.ac.uk/jdispatcher/sss/ncbiblast/summary?jobId={}"

# Nome amigável -> nome do banco no EBI.
BANCOS = {
    "swissprot": "uniprotkb_swissprot",  # ~570 mil proteínas revisadas: rápido e confiável
    "uniprot": "uniprotkb",              # >250 milhões: completo, porém bem mais lento
    "pdb": "pdb",                        # só proteínas com estrutura experimental
}

TIMEOUT = httpx.Timeout(30.0, connect=10.0)
TENTATIVAS = 3
LIMITE = LimiteDeTaxa(0.5)
INTERVALO_STATUS_S = 5
ESPERA_MAX_S = 240

MIN_TAM = 10
MAX_TAM = 10_000
# Na sequência de entrada aceitamos também X (desconhecido), B, Z, U e O.
LETRAS_VALIDAS = AMINOACIDOS | set("XBZUO*")


def _validar(sequencia: str) -> str:
    seq = limpar_sequencia(sequencia).rstrip("*")
    if not seq:
        raise ErroEsperado("Sequência vazia. Envie aminoácidos em código de 1 letra (FASTA aceito).")
    invalidas = sorted(set(seq) - LETRAS_VALIDAS)
    if invalidas:
        raise ErroEsperado(
            f"Caracteres que não são aminoácidos: {', '.join(invalidas)}. "
            "O BLAST aqui é de proteína (blastp); traduza DNA/RNA antes."
        )
    if len(seq) < MIN_TAM:
        raise ErroEsperado(
            f"Sequência curta demais para BLAST ({len(seq)} aa; mínimo {MIN_TAM}). "
            "Para peptídeos curtos, use buscar_proteina, que faz busca exata."
        )
    if len(seq) > MAX_TAM:
        raise ErroEsperado(f"Sequência longa demais ({len(seq)} aa; máximo {MAX_TAM}).")
    return seq


def _melhor_hsp(hit: dict) -> dict:
    hsps = hit.get("hit_hsps") or [{}]
    return min(hsps, key=lambda h: _num(h.get("hsp_expect"), 1e9))


def _num(valor, padrao: float = 0.0) -> float:
    try:
        return float(valor)
    except (TypeError, ValueError):
        return padrao


def _formatar_hit(i: int, hit: dict, tam_consulta: int) -> str:
    hsp = _melhor_hsp(hit)
    acc = hit.get("hit_acc") or hit.get("hit_id") or "?"
    desc = hit.get("hit_uni_de") or hit.get("hit_desc") or "(sem descrição)"
    organismo = hit.get("hit_uni_os") or "-"
    gene = hit.get("hit_uni_gn") or "-"
    ident = _num(hsp.get("hsp_identity"))
    alin = int(_num(hsp.get("hsp_align_len")))
    q_ini, q_fim = hsp.get("hsp_query_from", "?"), hsp.get("hsp_query_to", "?")
    cobertura = ""
    if isinstance(q_ini, int) and isinstance(q_fim, int) and tam_consulta:
        cobertura = f", cobre {100 * (q_fim - q_ini + 1) / tam_consulta:.0f}% da consulta"
    return (
        f"{i}. {desc} — {acc}\n"
        f"   Gene: {gene} | Organismo: {organismo} | Tamanho: {hit.get('hit_len', '?')} aa\n"
        f"   Identidade {ident:.1f}% em {alin} aa (consulta {q_ini}-{q_fim}{cobertura}) | "
        f"e-value {hsp.get('hsp_expect', '?')} | bit score {hsp.get('hsp_bit_score', '?')}"
    )


async def _esperar(client: httpx.AsyncClient, job: str) -> None:
    espera = 0
    while True:
        r = await requisitar(
            client, "GET", f"{EBI_BLAST}/status/{job}",
            servico="BLAST (EBI)", tentativas=TENTATIVAS, limite=LIMITE,
        )
        r.raise_for_status()
        status = r.text.strip().upper()
        if status == "FINISHED":
            return
        if status in ("ERROR", "FAILURE", "NOT_FOUND"):
            raise ErroEsperado(f"O BLAST do EBI terminou com status {status}. Veja {PAGINA_JOB.format(job)}")
        if espera >= ESPERA_MAX_S:
            raise ErroEsperado(
                f"O BLAST ainda está rodando após {ESPERA_MAX_S} s (fila do EBI cheia ou banco "
                f"grande). O resultado vai aparecer em {PAGINA_JOB.format(job)} . "
                "Dica: o banco 'swissprot' é bem mais rápido que 'uniprot'."
            )
        await asyncio.sleep(INTERVALO_STATUS_S)
        espera += INTERVALO_STATUS_S


async def buscar_similares(
    sequencia: str, banco: str = "swissprot", max_resultados: int = 10, evalue_max: float = 1e-3
) -> Resultado:
    """Roda um blastp e devolve as proteínas mais parecidas com a sequência."""
    if banco not in BANCOS:
        raise ErroEsperado(f"Banco '{banco}' desconhecido. Opções: {', '.join(BANCOS)}.")
    email = email_contato()
    if not email:
        raise ErroEsperado(
            "O BLAST do EBI exige um e-mail de contato. Defina a variável de ambiente "
            "AGENTE_PESQUISA_EMAIL (ou [contato] email no config.toml) e reinicie o servidor."
        )
    seq = _validar(sequencia)

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            r = await requisitar(
                client, "POST", f"{EBI_BLAST}/run",
                servico="BLAST (EBI)", tentativas=TENTATIVAS, limite=LIMITE,
                data={
                    "email": email, "program": "blastp", "stype": "protein",
                    "sequence": seq, "database": BANCOS[banco],
                    "exp": str(evalue_max),
                    "alignments": str(max_resultados), "scores": str(max_resultados),
                },
            )
            if r.status_code == 400:
                raise ErroEsperado(f"O EBI recusou o pedido de BLAST: {r.text.strip()[:300]}")
            r.raise_for_status()
            job = r.text.strip()
            if not re.fullmatch(r"[\w.-]+", job):
                raise ErroEsperado("Resposta inesperada do EBI ao iniciar o BLAST.")

            await _esperar(client, job)

            r = await requisitar(
                client, "GET", f"{EBI_BLAST}/result/{job}/json",
                servico="BLAST (EBI)", tentativas=TENTATIVAS, limite=LIMITE,
            )
            r.raise_for_status()
            dados = r.json()
        except httpx.HTTPError as e:
            raise ErroEsperado(f"Falha ao acessar o BLAST do EBI: {type(e).__name__} {e}") from e

    hits = dados.get("hits") or []
    if not hits:
        raise ErroEsperado(
            f"Nenhuma proteína parecida no banco '{banco}' com e-value <= {evalue_max:g}. "
            "Tente o banco 'uniprot' (maior) ou um e-value mais alto."
        )

    hits = hits[:max_resultados]
    cab = (
        f"BLAST (blastp, banco {banco}) da sequência de {len(seq)} aa: "
        f"{len(hits)} proteína(s) mais parecidas, da melhor para a pior. "
        "Identidade alta (>~40%) em boa parte da sequência e e-value baixo sugerem homologia; "
        "abaixo de ~25-30% de identidade é a 'zona crepuscular', interprete com cautela.\n"
        f"Job: {PAGINA_JOB.format(job)}\n\n"
    )
    return Resultado(
        cab + "\n\n".join(_formatar_hit(i, h, len(seq)) for i, h in enumerate(hits, start=1)),
        auditoria={"job_ebi": job, "banco": banco, "tamanho_consulta": len(seq)},
    )
