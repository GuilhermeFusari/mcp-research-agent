"""Requisições HTTP resilientes, compartilhadas pelos clientes de API (retry + limite de taxa)."""

import asyncio
import time

import httpx

from agente_pesquisa.contrato import ErroEsperado

# Falhas que costumam ser temporárias: vale tentar de novo.
STATUS_RETENTAVEIS = (429, 500, 502, 503, 504)


class LimiteDeTaxa:
    """Garante um intervalo mínimo entre requisições, inclusive as feitas em paralelo."""

    def __init__(self, intervalo_s: float):
        self.intervalo_s = intervalo_s
        self._trava = asyncio.Lock()
        self._ultima = 0.0

    async def aguardar(self) -> None:
        async with self._trava:
            espera = self._ultima + self.intervalo_s - time.monotonic()
            if espera > 0:
                await asyncio.sleep(espera)
            self._ultima = time.monotonic()


async def requisitar(
    client: httpx.AsyncClient,
    metodo: str,
    url: str,
    *,
    servico: str,
    tentativas: int,
    limite: LimiteDeTaxa | None = None,
    **kwargs,
) -> httpx.Response:
    """Faz a requisição tentando de novo em falhas temporárias, com espera crescente (backoff)."""
    for tentativa in range(1, tentativas + 1):
        if limite:
            await limite.aguardar()
        try:
            r = await client.request(metodo, url, **kwargs)
            if r.status_code not in STATUS_RETENTAVEIS:
                return r
            motivo = f"HTTP {r.status_code}"
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            motivo = type(e).__name__
        if tentativa < tentativas:
            await asyncio.sleep(2 * tentativa)

    raise ErroEsperado(
        f"O serviço {servico} não respondeu após {tentativas} tentativas ({motivo}). "
        "Provavelmente é instabilidade do lado deles; tente de novo em alguns minutos."
    )
