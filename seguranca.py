"""
Camada de segurança (DLP: Data Loss Prevention).

Tudo o que as ferramentas devolvem vai para o Claude, ou seja, sai da máquina.
Este módulo decide O QUE pode sair e registra o que saiu. São 4 defesas
em camadas ("defesa em profundidade": se uma falhar, as outras ainda protegem):

  1. MINIMIZAÇÃO    poucos trechos, de tamanho limitado (feito em busca_pdfs.py)
  2. CLASSIFICAÇÃO  documentos confidenciais nunca são devolvidos
  3. MASCARAMENTO   CPF, e-mail, telefone etc. viram [CPF], [EMAIL]...
  4. AUDITORIA      log local de cada chamada: o que foi pedido e o que saiu
"""

import hashlib
import json
import re
from datetime import datetime

import rag

# ---------------------------------------------------------------------------
# 2) Classificação
# ---------------------------------------------------------------------------

# Níveis em ordem crescente de sensibilidade. Comparar números é mais
# simples que comparar nomes ("interno" < "confidencial"?).
NIVEIS = {"publico": 0, "interno": 1, "confidencial": 2}


def nivel_do_caminho(caminho: str, cfg_seg: dict) -> int:
    """Classifica um PDF pelo caminho, usando as regras do config.

    POR QUE CLASSIFICAR NA HORA DA BUSCA E NÃO NA INDEXAÇÃO?
    Se o nível fosse gravado no índice, mudar uma regra exigiria reindexar
    tudo. Calculando na busca, a política muda na hora: editou o config,
    reiniciou o servidor, valeu.
    """
    c = caminho.lower()
    if any(t.lower() in c for t in cfg_seg.get("confidencial_se_contem", [])):
        return NIVEIS["confidencial"]
    if any(t.lower() in c for t in cfg_seg.get("publico_se_contem", [])):
        return NIVEIS["publico"]
    return NIVEIS[cfg_seg.get("nivel_padrao", "interno")]


def pode_enviar(caminho: str, cfg_seg: dict) -> bool:
    """True se o documento está dentro do nível máximo permitido para envio."""
    maximo = NIVEIS[cfg_seg.get("nivel_maximo_enviado", "interno")]
    return nivel_do_caminho(caminho, cfg_seg) <= maximo


# ---------------------------------------------------------------------------
# 3) Mascaramento de dados pessoais
# ---------------------------------------------------------------------------

def _cpf_valido(numeros: str) -> bool:
    """Confere os 2 dígitos verificadores do CPF.

    POR QUE VALIDAR? Artigos científicos estão cheios de números de 11
    dígitos que não são CPF. Sem essa checagem, mascararíamos dados
    científicos à toa (falso positivo). Com ela, só ~1 em 100 números
    aleatórios passa.
    """
    if len(numeros) != 11 or numeros == numeros[0] * 11:
        return False
    for tam in (9, 10):
        soma = sum(int(numeros[i]) * (tam + 1 - i) for i in range(tam))
        digito = (soma * 10) % 11 % 10
        if digito != int(numeros[tam]):
            return False
    return True


def _luhn_valido(numeros: str) -> bool:
    """Algoritmo de Luhn: o dígito verificador de todo cartão de crédito."""
    total = 0
    for i, d in enumerate(reversed(numeros)):
        n = int(d)
        if i % 2 == 1:
            n = n * 2 - 9 if n * 2 > 9 else n * 2
        total += n
    return total % 10 == 0


# (nome, regex, validador opcional). A ordem importa: padrões mais
# específicos primeiro, para um CNPJ não ser "comido" por outra regra.
PADROES = [
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), None),
    ("CNPJ", re.compile(r"\b\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\b"), None),
    ("CPF", re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b"),
     lambda s: _cpf_valido(re.sub(r"\D", "", s))),
    # Telefone só com DDD entre parênteses ou +55. Sem isso, intervalos de
    # anos como "2019-2020" seriam confundidos com telefone.
    ("TELEFONE", re.compile(r"(?:\+55\s?\d{2}|\(\d{2}\))\s?9?\d{4}-?\d{4}\b"), None),
    ("CARTAO", re.compile(r"\b(?:\d[ -]?){12,18}\d\b"),
     lambda s: _luhn_valido(re.sub(r"\D", "", s))),
]


def mascarar(texto: str) -> tuple[str, dict[str, int]]:
    """Troca dados pessoais por marcadores. Devolve (texto, {tipo: quantidade})."""
    contagem: dict[str, int] = {}

    for nome, regex, validador in PADROES:
        def substituir(m: re.Match, nome=nome, validador=validador) -> str:
            if validador and not validador(m.group()):
                return m.group()  # parecia, mas não passou na validação: mantém
            contagem[nome] = contagem.get(nome, 0) + 1
            return f"[{nome}]"

        texto = regex.sub(substituir, texto)
    return texto, contagem


# ---------------------------------------------------------------------------
# 4) Auditoria
# ---------------------------------------------------------------------------

PASTA_LOGS = rag.PASTA_PROJETO / "logs"


def registrar(ferramenta: str, argumentos: dict, saida: str, cfg_seg: dict, **extra) -> None:
    """Acrescenta uma linha no log de auditoria (logs/auditoria.jsonl).

    JSONL = um objeto JSON por linha. Vantagens: acrescentar é barato
    (não precisa reler o arquivo), e dá para abrir no Excel/pandas depois.

    Por padrão NÃO salvamos o texto enviado, só o hash e o tamanho. Se o log
    guardasse o texto, ele viraria mais uma cópia dos dados sensíveis. O hash
    ainda permite provar depois "foi exatamente este conteúdo que saiu".
    """
    if not cfg_seg.get("auditoria", True):
        return
    registro = {
        "quando": datetime.now().isoformat(timespec="seconds"),
        "ferramenta": ferramenta,
        "argumentos": argumentos,
        "caracteres_enviados": len(saida),
        "sha256_saida": hashlib.sha256(saida.encode("utf-8")).hexdigest(),
        **extra,
    }
    if cfg_seg.get("auditoria_salvar_texto", False):
        registro["saida"] = saida

    PASTA_LOGS.mkdir(exist_ok=True)
    with open(PASTA_LOGS / "auditoria.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(registro, ensure_ascii=False) + "\n")
