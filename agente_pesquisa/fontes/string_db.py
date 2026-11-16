"""Interações proteína-proteína pelo STRING (string-db.org).

Uma proteína -> seus parceiros mais confiáveis.
Várias proteínas -> a rede de interações entre elas.
Cada interação vem com o score combinado (0-1) e os canais de evidência.
"""

import httpx

from agente_pesquisa.contrato import ErroEsperado, Resultado
from agente_pesquisa.fontes.http import LimiteDeTaxa, requisitar

STRING_API = "https://string-db.org/api/json"
CHAMADOR = "mcp-research-agent"

TIMEOUT = httpx.Timeout(30.0, connect=10.0)
TENTATIVAS = 3
# O STRING pede no máximo ~1 requisição por segundo.
LIMITE = LimiteDeTaxa(1.0)

MAX_PROTEINAS = 50

# STRING trabalha com o ID de táxon do NCBI. Nomes comuns -> táxon.
ORGANISMOS = {
    "human": 9606, "humano": 9606, "homo sapiens": 9606,
    "mouse": 10090, "camundongo": 10090, "mus musculus": 10090,
    "rat": 10116, "rato": 10116, "rattus norvegicus": 10116,
    "zebrafish": 7955, "danio rerio": 7955,
    "fly": 7227, "drosophila": 7227, "drosophila melanogaster": 7227,
    "worm": 6239, "c. elegans": 6239, "caenorhabditis elegans": 6239,
    "yeast": 4932, "levedura": 4932, "saccharomyces cerevisiae": 4932,
    "e. coli": 511145, "escherichia coli": 511145,
    "arabidopsis": 3702, "arabidopsis thaliana": 3702,
}

# Canais de evidência do STRING (campo -> nome legível).
CANAIS = {
    "escore": "experimentos",
    "dscore": "bancos curados",
    "tscore": "mineração de texto",
    "ascore": "coexpressão",
    "nscore": "vizinhança gênica",
    "fscore": "fusão gênica",
    "pscore": "co-ocorrência filogenética",
}


def resolver_organismo(organismo: str | int | None) -> int:
    """Aceita nome comum, nome científico ou ID de táxon. Padrão: humano."""
    if organismo is None or str(organismo).strip() == "":
        return 9606
    texto = str(organismo).strip().lower()
    if texto.isdigit():
        return int(texto)
    if texto in ORGANISMOS:
        return ORGANISMOS[texto]
    raise ErroEsperado(
        f"Organismo '{organismo}' não reconhecido. Use o ID de táxon do NCBI "
        "(ex.: 9606 humano, 10090 camundongo) ou um destes nomes: "
        + ", ".join(sorted({k for k in ORGANISMOS if " " in k or "." in k})) + "."
    )


def _evidencias(i: dict) -> str:
    canais = [(nome, float(i.get(campo) or 0)) for campo, nome in CANAIS.items()]
    canais = [(n, v) for n, v in canais if v > 0]
    canais.sort(key=lambda c: -c[1])
    return ", ".join(f"{n} {v:.2f}" for n, v in canais[:3]) or "-"


async def _get(client: httpx.AsyncClient, metodo: str, params: dict):
    r = await requisitar(
        client, "POST", f"{STRING_API}/{metodo}",
        servico="STRING", tentativas=TENTATIVAS, limite=LIMITE,
        data={**params, "caller_identity": CHAMADOR},
    )
    if r.status_code == 400:
        raise ErroEsperado(f"O STRING recusou o pedido: {r.text.strip()[:300]}")
    r.raise_for_status()
    return r.json()


async def buscar_interacoes(
    proteinas: list[str],
    organismo: str | int | None = None,
    max_resultados: int = 15,
    score_minimo: float = 0.7,
) -> Resultado:
    """Parceiros de uma proteína, ou a rede entre várias."""
    nomes = [p.strip() for p in proteinas if p and p.strip()]
    if not nomes:
        raise ErroEsperado("Informe ao menos uma proteína ou gene (ex.: ['GRB2']).")
    if len(nomes) > MAX_PROTEINAS:
        raise ErroEsperado(f"No máximo {MAX_PROTEINAS} proteínas por consulta.")
    taxon = resolver_organismo(organismo)
    score_minimo = max(0.15, min(score_minimo, 0.999))

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            # 1) Mapeia os nomes para IDs do STRING (evita ambiguidade e mostra o que foi achado).
            mapa = await _get(client, "get_string_ids", {
                "identifiers": "\r".join(nomes), "species": taxon, "limit": 1, "echo_query": 1,
            })
            if not mapa:
                raise ErroEsperado(
                    f"Nenhuma das proteínas ({', '.join(nomes)}) foi encontrada no STRING "
                    f"para o táxon {taxon}. Confira o nome do gene e o organismo."
                )
            ids = [m["stringId"] for m in mapa]
            achados = {m.get("queryItem", "?"): m.get("preferredName", "?") for m in mapa}
            faltando = [n for n in nomes if n not in achados]

            params = {
                "identifiers": "\r".join(ids), "species": taxon,
                "required_score": int(score_minimo * 1000),
            }
            if len(ids) == 1:
                params["limit"] = max_resultados
                interacoes = await _get(client, "interaction_partners", params)
            else:
                interacoes = await _get(client, "network", params)
        except httpx.HTTPError as e:
            raise ErroEsperado(f"Falha ao acessar o STRING: {type(e).__name__} {e}") from e

    especie = mapa[0].get("taxonName") or f"táxon {taxon}"
    mapeamento = "; ".join(f"{q} -> {p}" for q, p in achados.items())
    if not interacoes:
        raise ErroEsperado(
            f"Nenhuma interação com score >= {score_minimo:.2f} para {mapeamento} ({especie}). "
            "Tente um score_minimo menor (0.4 = confiança média)."
        )

    interacoes = sorted(interacoes, key=lambda i: -float(i.get("score") or 0))
    # A rede pode trazer a mesma aresta duas vezes (A-B e B-A).
    vistas, linhas = set(), []
    for i in interacoes:
        par = frozenset((i.get("preferredName_A"), i.get("preferredName_B")))
        if par in vistas:
            continue
        vistas.add(par)
        linhas.append(
            f"- {i.get('preferredName_A')} — {i.get('preferredName_B')} | "
            f"score {float(i.get('score') or 0):.3f} | evidência: {_evidencias(i)}"
        )
        if len(linhas) >= max_resultados:
            break

    modo = "parceiros de" if len(ids) == 1 else "rede entre"
    cab = (
        f"STRING ({especie}): {modo} {mapeamento}. {len(linhas)} interação(ões) com score "
        f">= {score_minimo:.2f}, da mais para a menos confiável. O score combina evidências "
        "de experimentos, bancos curados, mineração de texto e predições: 'experimentos' e "
        "'bancos curados' pesam mais que 'mineração de texto', e interação funcional não "
        "significa necessariamente contato físico.\n"
    )
    if faltando:
        cab += f"Não encontradas no STRING: {', '.join(faltando)}.\n"
    link = f"https://string-db.org/network/{ids[0]}" if len(ids) == 1 else "https://string-db.org"
    return Resultado(
        cab + "\n" + "\n".join(linhas) + f"\n\nVer no STRING: {link}",
        auditoria={"taxon": taxon, "string_ids": ids},
    )
