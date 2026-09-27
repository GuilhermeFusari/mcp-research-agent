# Agente de Pesquisa (MCP)

Servidor MCP para o Claude Desktop com duas capacidades:

- **UniProt**: consulta proteínas e peptídeos por nome, gene, ID ou sequência (função, localização, estruturas PDB/AlphaFold).
- **Seus PDFs**: busca semântica (por significado, em PT e EN) nos PDFs do seu computador, com citação de arquivo e página.

## ⚠️ Privacidade: leia antes

A indexação é **100% local**: PDFs, vetores e banco ficam no seu PC.
Porém, quando o Claude usa a busca, os **trechos encontrados** (até ~15 por pergunta) são enviados ao Claude, como qualquer texto que você colasse numa conversa. **A segurança é a mesma de usar o app do Claude.**

> Se você não colaria um arquivo no Claude, não deixe ele nas pastas indexadas.

Proteções incluídas (configuráveis em `config.toml`):

- só as pastas listadas são lidas, e nomes como "boleto", "extrato" e "contrato" são ignorados;
- caminhos com "confidencial", "sigiloso" etc. **nunca** são enviados, nem o nome do arquivo;
- CPF, CNPJ, e-mail, telefone e cartão são mascarados antes do envio;
- todo envio fica registrado localmente em `logs/auditoria.jsonl`.

## Requisitos (Windows)

1. [Python 3.12+](https://www.python.org/downloads/) (marque **"Add python.exe to PATH"** na instalação)
2. [Claude Desktop](https://claude.ai/download)

Não precisa de Docker: o instalador baixa o banco vetorial (Qdrant) como um executável,
confere o hash oficial dele, e o programa liga e desliga o banco sozinho, em segundo plano.

## Instalação

1. Baixe/clone esta pasta para um lugar fixo (ex.: `C:\Projetos\agente-pesquisa`). **Não mova depois**: o Claude Desktop guarda o caminho.
2. Dê **duplo clique em `instalar.bat`**. A 1ª vez leva ~10 minutos (baixa ~1,5 GB entre bibliotecas e modelo).
3. Reinicie o Claude Desktop pelo **ícone da bandeja** (perto do relógio) > Sair, e abra de novo.

## Uso

Converse normalmente no Claude Desktop, por exemplo:

- *"O que é a TP53?"*
- *"O que meus PDFs dizem sobre peptídeos antimicrobianos?"*
- *"Quais PDFs eu tenho indexados?"*

**Adicionou ou alterou PDFs?** Reindexe (só processa o que mudou):

```
.venv\Scripts\python.exe indexar.py
```

## Configuração

Tudo em `config.toml` (criado pelo instalador a partir de `config.exemplo.toml`):

| Seção | O que ajustar |
|---|---|
| `[indexacao]` | pastas indexadas e termos de exclusão |
| `[chunking]` | tamanho dos trechos |
| `[seguranca]` | classificação, mascaramento, auditoria |
| `[ocr]` | liga/desliga OCR, idiomas, resolução |
| `[qdrant]` | `modo = "executavel"` (padrão) ou `"docker"` (se preferir `docker compose up -d`) |

Mudou `[seguranca]`? Basta reiniciar o Claude Desktop. Mudou pastas? Rode o `indexar.py`.

## Problemas comuns

| Sintoma | Solução |
|---|---|
| Busca nos PDFs dá erro de conexão | Veja `logs\qdrant.log`; rode o `instalar.bat` de novo |
| 1ª busca demora ~30 s | Normal: carrega o modelo e liga o banco. As seguintes levam ~1 s |
| Indexação parece travada | PDFs escaneados passam por OCR (~1 min/página na CPU); o log mostra a página atual |
| Ferramentas não aparecem no Claude | Reinicie o Claude pela bandeja; confira em Configurações > Desenvolvedor |
| "O índice ainda não existe" | Rode `indexar.py` |
| Aviso "página(s) sem texto nem com OCR" | Normal para páginas só com figuras ou em branco |

**Desinstalar do Claude:** `.venv\Scripts\python.exe registrar_claude.py --remover`

## Estrutura

| Arquivo | Papel |
|---|---|
| `servidor.py` | servidor MCP (expõe as ferramentas ao Claude) |
| `uniprot.py` | cliente da API do UniProt |
| `busca_pdfs.py` | busca semântica no Qdrant |
| `indexar.py` | lê PDFs → chunks → embeddings → Qdrant |
| `rag.py` | config, modelo e conexão compartilhados |
| `seguranca.py` | classificação, mascaramento e auditoria |
| `registrar_claude.py` | registra o servidor no Claude Desktop |
| `instalar.ps1` / `.bat` | instalador |
