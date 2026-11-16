---
name: protein-research
description: Answer molecular biology questions with verifiable sources (UniProt, BLAST, STRING, PubMed). Use when the user asks what a protein does, what an amino acid sequence is, which proteins interact with a target, or what the literature says about a gene, disease or drug. Requires the mcp-research-agent MCP server.
---

# Protein research

This skill tells you how to use the **mcp-research-agent** MCP server to answer
protein, sequence, interaction and literature questions with data that can be
cited, instead of answering from memory.

## Setup (once)

The tools come from the MCP server. If tools such as `buscar_proteina` are not
available, help the user install it. It needs [uv](https://docs.astral.sh/uv/)
and an email address. EBI requires an email for BLAST jobs.

Claude Code:

```bash
claude mcp add research-agent -e AGENTE_PESQUISA_EMAIL=you@example.com -- \
  uvx --from git+https://github.com/GuilhermeFusari/mcp-research-agent mcp-research-agent
```

Claude Desktop, Cursor and other clients that use an `mcpServers` JSON config:

```json
{
  "mcpServers": {
    "research-agent": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/GuilhermeFusari/mcp-research-agent", "mcp-research-agent"],
      "env": { "AGENTE_PESQUISA_EMAIL": "you@example.com" }
    }
  }
}
```

Restart the client after adding it.

## Tools

The tool names are in Portuguese. Their outputs are in Portuguese as well, so
translate to the user's language when you answer.

| Tool | Use it for |
|---|---|
| `buscar_proteina` | UniProt lookup by name, gene, accession, or **exact** sequence match. Returns function, organism, localization, PDB and AlphaFold links. |
| `buscar_similares` | BLAST (blastp at EBI). Finds **similar** sequences (homologs), with identity, coverage and e-value. Takes 20 s to a few minutes. |
| `buscar_interacoes` | STRING protein-protein interactions. One protein returns its partners; several proteins return the network between them. |
| `buscar_entidade` | Turns a name into a PubTator ID (`p53` becomes `@GENE_TP53`). Needed before `buscar_relacoes`. |
| `buscar_artigos` | Searches PubMed (via PubTator3), 10 results per page. |
| `ler_artigo` | Reads a paper by PMID: the abstract, or results and discussion for PMC Open Access papers. |
| `buscar_relacoes` | Gene, disease and chemical relations mined from the whole literature, with paper counts. |
| `buscar_pdfs` / `listar_pdfs` | Only present in the full local install: semantic search over the user's own PDFs. |

## When to use them

Use the tools when the answer depends on facts that should be current or
citable, such as a protein's function, a sequence's identity, interaction
partners, or published evidence. Do **not** call them just because a gene name
shows up in passing, for example in code, file names or variable names.

## Workflows

**"What is this protein?"**
1. Call `buscar_proteina` with the name or accession. Pass `organismo` when the
   user cares about one species.
2. Prefer Swiss-Prot (reviewed) entries. Cite the accession (e.g. P62993).

**"What is this sequence?"**
1. Call `buscar_proteina` with the sequence. This is an exact match, so it is
   fast and ideal for peptides.
2. If nothing is found, or the user wants homologs, call `buscar_similares`.
   Start with `banco="swissprot"`, and use `"uniprot"` only if Swiss-Prot has
   no hits. Tell the user the job can take a while.
3. Interpret the hits honestly. High identity over most of the query plus a low
   e-value suggests homology. Below about 25-30% identity is the twilight zone,
   so say so.

**"What does X interact with?"**
1. Call `buscar_interacoes` with `["X"]`. To check whether several proteins
   form a network, pass all of them.
2. Report the score and the evidence channels. Evidence from experiments and
   curated databases is stronger than text mining. A STRING interaction can be
   functional association, not physical binding, so do not claim direct
   binding unless the experimental channel supports it.
3. For the strongest pairs, back them up with literature if the user needs
   proof: `buscar_artigos` with both names.

**"What does the literature say about…?"**
1. Call `buscar_entidade` to get precise IDs, and check that the top candidate
   is the right one (`CAT` can be catalase or cataract).
2. Call `buscar_artigos` with the IDs or free text, then `ler_artigo` on the 2-4
   most relevant PMIDs. Start with the abstract and request full text only
   when needed.
3. For "what treats, causes or inhibits X", use `buscar_relacoes`. Many papers
   behind a relation means consolidated evidence. Few papers means a weak
   signal or an extraction error.

## Rules for the answer

- Cite UniProt accessions, PMIDs and STRING scores next to the claims they support.
- Separate what came from the tools from general knowledge.
- Never say you read a full paper when only the abstract came back.
- If a tool returns an error, read the message. It usually says what to change
  (organism, score threshold, sequence length, missing email).
