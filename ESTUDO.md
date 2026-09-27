# Roteiro de estudo

O objetivo não é decorar este projeto. É conseguir **refazer uma versão pequena dele sozinho**.
Cada nível tem: o conceito, onde ele aparece no código, e exercícios que **você** escreve.

**Método para todos os níveis:**
1. Leia o arquivo indicado (os comentários explicam o *porquê*).
2. Antes de mudar algo, **preveja** o que vai acontecer. Depois rode e confira.
3. Quebre de propósito (apague uma linha, troque um valor) e leia o erro.
4. Travou? Peça ao Claude uma **dica**, não a resposta.

Crie seus exercícios numa pasta `estudo/`, para não mexer no projeto principal.

---

## Nível 0: aquecimento em Python (se precisar)

Conceitos usados no projeto inteiro: função, `dict`, `list`, f-string, `for`, `try/except`, `import`.

- [ ] Escreva uma função `contar_aminoacidos(seq: str) -> dict` que devolve quantas vezes cada letra aparece.
      `contar_aminoacidos("GIVEQ")` → `{"G": 1, "I": 1, "V": 1, "E": 1, "Q": 1}`
- [ ] Faça a função ignorar espaços e aceitar minúsculas.
- [ ] Faça ela lançar um `ValueError` se aparecer uma letra que não é aminoácido (ex.: `"X"`).

---

## Nível 1: APIs web e JSON → `uniprot.py`

**Conceito:** uma API REST é um endereço que devolve dados (JSON) em vez de uma página.
O programa faz uma requisição HTTP, recebe o JSON e escolhe os pedaços que interessam.

**Veja com os próprios olhos:** abra no navegador
`https://rest.uniprot.org/uniprotkb/P04637.json`
É isso que o código recebe. O `_formatar_entrada` só navega nesse dicionário.

- [ ] No navegador, encontre no JSON onde está a massa da proteína.
- [ ] Num arquivo novo, use `httpx.get(...)` para baixar esse JSON e imprimir **só** o nome e a massa.
- [ ] Mude para receber o ID por `input()` e tratar o caso de ID inexistente (qual status HTTP volta?).
- [ ] **No projeto:** adicione a massa ao resultado de `_formatar_entrada`. Teste com `python uniprot.py TP53`.

**Para entender depois:** por que o código usa `async`/`await`? (Pista: o que acontece com o
servidor enquanto espera a resposta do UniProt, que pode demorar 30 segundos?)

---

## Nível 2: MCP → `servidor.py`

**Conceito central:** ferramenta MCP = **função Python + docstring + type hints**.
- os type hints viram o "formulário" que o Claude preenche (`inputSchema`);
- a docstring é o que o Claude lê para decidir **quando** usar a ferramenta;
- o servidor conversa por stdin/stdout. Por isso **nunca** use `print()` no servidor.

- [ ] Crie `estudo/meu_servidor.py` **do zero, sem copiar**, com uma ferramenta
      `massa_peptideo(sequencia: str) -> str` que soma as massas dos aminoácidos
      (tabela de massas: pesquise "amino acid residue masses"; some ~18 Da da água no final).
- [ ] Teste no Inspector:
      `npx @modelcontextprotocol/inspector .venv\Scripts\python.exe estudo\meu_servidor.py`
- [ ] Olhe no painel da direita a mensagem `tools/list`. Encontre sua docstring e seus type hints lá dentro.
- [ ] Mude a docstring para algo vago ("faz uma conta") e pense: o Claude saberia quando usar?
- [ ] Coloque um `print("oi")` dentro da ferramenta e veja o que quebra. Troque por `logging`.

**Se você fez este nível sem copiar, você entendeu MCP.** O resto do projeto é "o que a ferramenta faz por dentro".

---

## Nível 3: embeddings, o coração do RAG

**Conceito:** um modelo transforma texto em um vetor (lista de números).
Textos com significado parecido geram vetores próximos, mesmo com palavras diferentes ou em outro idioma.

- [ ] Script de ~15 linhas: carregue `SentenceTransformer("intfloat/multilingual-e5-base")`,
      gere os embeddings destas frases e imprima a similaridade entre todos os pares
      (`model.similarity(...)` ou produto escalar com NumPy):
  - "A insulina reduz a glicose no sangue"
  - "Insulin lowers blood sugar"
  - "O pâncreas produz hormônios"
  - "O Brasil ganhou o jogo de futebol"
- [ ] Antes de rodar, **preveja** qual par terá a maior similaridade e qual a menor.
- [ ] Imprima `len()` de um embedding. De onde vem o 768 que aparece no `indexar.py`?
- [ ] Teste com e sem o prefixo `"query: "` / `"passage: "`. Muda alguma coisa? (Leia `rag.py`.)

---

## Nível 4: o pipeline RAG → `indexar.py` e `busca_pdfs.py`

**Conceito:** indexar = PDF → texto → pedaços (chunks) → vetores → banco.
Buscar = pergunta → vetor → os k vetores mais próximos no banco → trechos.

- [ ] Use `pymupdf` para imprimir o texto da página 1 de um PDF seu.
- [ ] Passe esse texto pelo `RecursiveCharacterTextSplitter` com `chunk_size=300` e imprima os chunks.
      Onde ele cortou? Por que ali? (Leia os `separators` no `indexar.py`.)
- [ ] Mude `chunk_overlap` para 0 e para 150. O que muda nas bordas?
- [ ] **Experimento de verdade:** copie o projeto, mude `tamanho` no config para 300, reindexe numa
      coleção nova (`colecao = "teste300"`) e compare as respostas da mesma pergunta com as da coleção
      atual (1000). Qual dá trechos mais úteis? Esse é o trade-off precisão × contexto.
- [ ] Abra o painel do Qdrant (http://localhost:6333/dashboard) e explore a coleção: um ponto é vetor + payload.

---

## Nível 5 (opcional): o acabamento

Só depois dos níveis 1 a 4:

- `seguranca.py`: expressões regulares (regex) e por que validar o dígito do CPF evita falso positivo.
- `indexar.py`: como o hash do arquivo torna a indexação incremental.
- `docker-compose.yml`: o que é um container e por que o `127.0.0.1:` na porta importa.
- `instalar.ps1` / `registrar_claude.py`: automação. Leia por curiosidade, não é conceito central.

---

## Desafio final

Numa pasta vazia, **sem olhar o projeto**, construa um servidor MCP com **uma** ferramenta
que busca em 3 arquivos `.txt` usando embeddings. Não precisa de Qdrant: guarde os vetores numa
lista em memória e compare um por um.
São ~50 linhas. Se funcionar no Inspector, você domina a ideia do projeto inteiro.

## Referências

- MCP: https://modelcontextprotocol.io (conceitos e especificação)
- SDK Python do MCP: https://github.com/modelcontextprotocol/python-sdk
- Embeddings: https://sbert.net (documentação do sentence-transformers)
- Qdrant: https://qdrant.tech/documentation (quickstart)
- API do UniProt: https://www.uniprot.org/help/api
