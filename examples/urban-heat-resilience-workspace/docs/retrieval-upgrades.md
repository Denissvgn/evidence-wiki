# Retrieval Upgrade Notes

`scripts/query_index.py` is the default local retrieval layer for the research workspace. It is intentionally dependency-light: by default it can index Markdown files from the configured `wiki.root` and `sources.normalized_dir`, rank keyword matches in memory, and return paths, headings, source IDs, and snippets.

SQLite FTS5 is the first retrieval upgrade. It adds a generated persistent index for faster repeated lexical search and prefix recall without adding runtime dependencies beyond stdlib `sqlite3` and the existing PyYAML dependency. The in-memory path remains the fallback when the SQLite index is absent, unreadable, or unsupported by the local SQLite build.

## Baseline: `query_index.py`

Use the baseline when:

- the workspace has small or medium Markdown corpora;
- agents need cheap local search without a setup step;
- reproducibility matters more than semantic ranking;
- no persistent index or external service should be required.

Strengths:

- no new runtime service;
- deterministic enough for agent workflows;
- respects configured wiki and normalized-source paths;
- supports wiki-first and normalized-source scoped search.

Limits:

- each invocation rebuilds the in-memory index;
- matching is lexical, so aliases and paraphrases may be missed;
- ranking is useful but not a replacement for reading source notes and pages.

## Upgrade Path: SQLite FTS

SQLite full-text search is the implemented upgrade when keyword search is still the right retrieval model, but repeated indexing becomes inefficient.

Build the generated local index explicitly:

```bash
python3 scripts/query_index.py build-index --project-root . --scope all
```

Query commands keep the existing contract:

```bash
python3 scripts/query_index.py --project-root . --scope all --limit 10 --format json "query terms"
```

By default the generated database is written to `.research-cache/query-index.sqlite3` under the project root. Use `--index-path` on both `build-index` and query mode to choose another path.

Use SQLite FTS when:

- the workspace has hundreds or thousands of Markdown files;
- repeated queries happen during one workflow;
- agents need fast prefix, phrase, or column-scoped search;
- the project should still avoid an external search service.

Implemented shape:

- stores document path, scope, kind, title, headings, source IDs, body text, and a content hash in a generated SQLite FTS5 database;
- ranks matches with BM25-style column weights that keep titles and source IDs stronger than body text;
- supports prefix queries, which improves recall for partial terms such as `retriev generat`;
- preserves wiki/normalized scope filtering and the JSON/text output shape;
- keeps `query_index.py` usable as the PyYAML-only fallback when the SQLite index is absent or unavailable.

Cautions:

- do not make the SQLite database authoritative; Markdown remains source of truth;
- avoid committing generated indexes unless the project explicitly decides to;
- rebuild when wiki pages or normalized records change.

## Upgrade Path: qmd or MCP Search

qmd or MCP-style search remains a future path. It can be useful when a project already has a local search tooling layer that agents can call consistently across workspaces.

Use qmd or MCP search when:

- retrieval should be shared across multiple tools or agents;
- the project already runs an MCP server or query daemon;
- search needs to cover files outside the research workspace;
- operators want one search interface for several repositories or corpora.

Expected implementation shape:

- document the server or CLI setup separately from the research workspace contract;
- map returned results back to workspace-relative paths;
- preserve the same evidence policy: maintained wiki pages first, normalized records second, raw files only when surfaced by maintained evidence.

Cautions:

- do not require qmd or MCP search for the MVP workspace;
- keep `research.yml` paths as the source of workspace boundaries;
- avoid hidden network dependencies unless the project explicitly allows them.

## Upgrade Path: Embeddings and Vector Search

Embeddings remain a future path. They are useful when agents miss relevant material because the question uses different wording than the source pages.

Use vector search when:

- semantic recall matters more than exact keyword matching;
- the corpus has many aliases, paraphrases, or cross-domain terms;
- users often ask conceptual questions that do not share source vocabulary;
- the project has explicit choices for embedding model, storage, and refresh cadence.

Expected implementation shape:

- chunk wiki pages and normalized records with stable chunk IDs based on path and heading;
- store vectors in a local vector database, SQLite extension, or generated artifact directory;
- include source path, heading, line or chunk range, source IDs, and content hash in each record;
- combine vector search with keyword search for source IDs, benchmark names, exact terms, and page titles.

Cautions:

- embeddings introduce model and version dependencies;
- semantic matches must still be verified by reading the source page or normalized record;
- private or sensitive sources require explicit embedding-storage policy;
- regenerated normalized records should invalidate stale chunks.

## Decision Guide

Choose the smallest retrieval layer that solves the observed problem.

| Situation | Recommended path |
|-----------|------------------|
| Small workspace or early pilot | Keep `query_index.py`. |
| Keyword search is useful but repeated indexing is slow | Build the SQLite FTS index. |
| Retrieval must be shared across tools or repositories | Add qmd or MCP search. |
| Relevant pages are missed because wording differs | Add embeddings plus keyword fallback. |

Do not add an upgrade only because it is technically available. Add it when the current baseline causes a concrete retrieval failure, speed problem, or operator workflow problem.

## Compatibility Rules

- Keep `query_index.py` available as the minimum local fallback.
- Keep Markdown files as the source of truth.
- Preserve workspace-relative paths in search results.
- Return enough metadata for agents to cite maintained wiki pages and `source_ids`.
- Do not weaken the evidence boundary: source notes and maintained wiki pages remain preferred over raw files.
