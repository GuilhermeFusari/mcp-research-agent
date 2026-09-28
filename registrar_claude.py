"""Registra (ou atualiza) o servidor MCP no Claude Desktop."""

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

NOME_SERVIDOR = "agente-pesquisa"
PASTA_PROJETO = Path(__file__).resolve().parent


def arquivos_config() -> list[Path]:
    """Onde o Claude Desktop guarda o config no Windows."""
    candidatos = [Path(os.environ["APPDATA"]) / "Claude" / "claude_desktop_config.json"]
    pacotes = Path(os.environ["LOCALAPPDATA"]) / "Packages"
    if pacotes.is_dir():
        candidatos += pacotes.glob("Claude_*/LocalCache/Roaming/Claude/claude_desktop_config.json")

    existentes = [c for c in candidatos if c.exists()]
    return existentes or candidatos[:1]


def atualizar(arquivo: Path, remover: bool) -> None:
    config = {}
    if arquivo.exists():
        texto = arquivo.read_text(encoding="utf-8-sig").strip()
        if texto:
            try:
                config = json.loads(texto)
            except json.JSONDecodeError as e:
                sys.exit(f"ERRO: {arquivo} não é um JSON válido ({e}). Corrija ou apague e rode de novo.")
        backup = arquivo.with_name(f"{arquivo.name}.{datetime.now():%Y%m%d-%H%M%S}.bak")
        shutil.copy2(arquivo, backup)
        print(f"Backup: {backup}")

    servidores = config.setdefault("mcpServers", {})
    if remover:
        servidores.pop(NOME_SERVIDOR, None)
    else:
        servidores[NOME_SERVIDOR] = {
            "command": sys.executable,
            "args": [str(PASTA_PROJETO / "servidor.py")],
        }

    arquivo.parent.mkdir(parents=True, exist_ok=True)
    arquivo.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"{'Removido de' if remover else 'Registrado em'}: {arquivo}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remover", action="store_true", help="remove o servidor do Claude Desktop")
    parser.add_argument("--arquivo", type=Path, help="usa este arquivo em vez de detectar (para testes)")
    args = parser.parse_args()

    if "venv" not in sys.executable.lower() and not args.remover:
        print("AVISO: você não está usando o Python do .venv. O Claude Desktop pode não "
              "achar as bibliotecas. Rode com .venv\\Scripts\\python.exe registrar_claude.py")

    for arquivo in ([args.arquivo] if args.arquivo else arquivos_config()):
        atualizar(arquivo, args.remover)
    print("Feche o Claude Desktop pelo ícone da bandeja (perto do relógio) e abra de novo.")


if __name__ == "__main__":
    main()
