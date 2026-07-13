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

Query mode caps the effective result limit at 100. Larger `--limit` values are
accepted for CLI compatibility, but the lexical path, SQLite FTS path, provider
request payload, and MCP wrapper all use the capped limit.

By default the generated database is written to `.research-cache/query-index.sqlite3` under the project root. Use `--index-path` on both `build-index` and query mode to choose another workspace-relative path.
Concurrent builders serialize through a cross-process lock. Each build uses a
unique same-directory temporary SQLite database, closes SQLite before replacement
or cleanup on POSIX and Windows, and atomically publishes only a complete index.
Failed builds retain the prior complete database and remove their own temporary
database and SQLite sidecars.

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
- records a corpus fingerprint and build scope so query mode can detect a stale or scope-mismatched index;
- keeps `query_index.py` usable as the PyYAML-only fallback when the SQLite index is absent or unavailable.

Stale-index safety: query mode verifies the index before trusting it. If the
indexed files changed since `build-index` ran, or the index was built for a
narrower scope than the query needs, it prints a short `note:` to stderr and
falls back to the always-correct in-memory scan instead of returning outdated or
partial results. Rebuild with `build-index` to restore the fast path.

The SQLite FTS database is a generated cache, not source of truth. Markdown
files remain authoritative, and query mode treats the index as a fast path that
can disappear or change. A rebuild, removal, or replacement can happen between
evaluate_index and query_fts_index after the freshness check has passed. If that
race makes the index unreadable, query mode catches the SQLite error, prints a
short `note:`, and falls back to the in-memory scan.

Unprocessed-evidence signal: every query also reports how many discovered
sources are not yet normalized and therefore not searchable, as a footer note in
text mode and `unnormalized_source_count` / `unnormalized_source_ids` in JSON.
This tells the answering agent when the pipeline still has raw evidence to
normalize before the index is complete.

Citation-graph signal: normalized source records may carry
`references_source_ids` when local BibTeX entries match existing manifest records
by arXiv ID or DOI. JSON query results include `related_source_ids`, the one-hop
union of sources cited by the result's `source_ids` and sources that cite them.
This lets agents discover adjacent evidence without re-reading every raw
bibliography. Text output stays focused on ranked matches and does not print the
graph field.

Cautions:

- do not make the SQLite database authoritative; Markdown remains source of truth;
- avoid committing generated indexes unless the project explicitly decides to;
- rebuild after wiki pages or normalized records change to keep the fast path; a stale index is detected and bypassed, not silently trusted.

## Upgrade Path: Pluggable Retrieval Providers

An optional command-based retrieval provider interface supports semantic
or embedding-backed engines. No embedding model, vector store, daemon, or
network client ships with the template. The local lexical engine remains the
default and fallback.

Default disabled shape:

```yaml
integrations:
  retrieval:
    provider: lexical
    command: null
    timeout_seconds: 30
```

To use an external engine, set `provider` to a non-`lexical` name and provide a
command. String commands are parsed with `shlex.split`; list form is preferred
when paths or arguments may contain spaces.

```yaml
integrations:
  retrieval:
    provider: local-semantic
    command:
      - python3
      - tools/semantic_retriever.py
      - search
    timeout_seconds: 30
```

`query_index.py` sends one JSON request on stdin:

```json
{
  "schema_version": "1",
  "query": "paraphrased question text",
  "scope": "all",
  "limit": 10,
  "project_root": "/absolute/workspace/path",
  "corpus_roots": [
    {"scope": "wiki", "path": "wiki"},
    {"scope": "normalized", "path": "sources/normalized"}
  ],
  "documents": [
    {
      "path": "wiki/concepts/example.md",
      "scope": "wiki",
      "kind": "concept",
      "title": "Example",
      "headings": ["Definition"],
      "source_ids": ["paper:example"]
    }
  ]
}
```

The `limit` field is the capped effective limit, never greater than 100.

The provider returns ranked JSON on stdout:

```json
{
  "schema_version": "1",
  "results": [
    {
      "path": "wiki/concepts/example.md",
      "score": 12.5,
      "snippet": "Optional provider snippet",
      "matched_headings": ["Definition"]
    }
  ]
}
```

`path` must be a workspace-relative Markdown document that appears in the query
corpus; `score` must be numeric. `snippet` and `matched_headings` are optional.
The template hydrates provider results from the local document metadata, so
`scope`, `kind`, `title`, and `source_ids` remain trusted local values.

If the provider command exits non-zero, times out, emits invalid JSON, returns
an unsafe path, or names a document outside the query corpus, `query_index.py`
prints a warning to stderr and falls back to the lexical engine. Successful
empty provider results are treated as a real answer, not as a fallback trigger.
JSON output includes `engine: lexical` for lexical results and
`engine: <provider>` for provider results, both at the top level and per result.

## Upgrade Path: Opt-In Semantic Hybrid Ranking

Production hardening adds a provider-neutral semantic subconfiguration that
keeps lexical retrieval as the baseline and merges optional semantic recall into
the final ranking:

```yaml
integrations:
  retrieval:
    provider: lexical
    semantic:
      enabled: true
      provider: local-semantic
      transport: command
      command:
        - python3
        - tools/semantic_retriever.py
        - search
      timeout_seconds: 30
      cache_dir: .research-cache/semantic-retrieval
```

`transport` may be `command` or `http`. Command transport uses the same stdin
JSON request and stdout JSON response shape as the pluggable provider interface.
HTTP transport sends that request as a JSON `POST` to `endpoint`. Both transports
are operator-managed: the template ships no embedding model, vector store,
commercial API client, or credentials.

When semantic retrieval is enabled and usable, `query_index.py` first computes
the lexical or SQLite FTS result set, then hydrates semantic provider results
from the same local corpus and ranks the union as `engine: hybrid`. Each result
keeps local `scope`, `kind`, `title`, and `source_ids`; hybrid results also carry
`lexical_score` and `semantic_score` for audit. Provider failures warn and leave
the lexical path in control.

Generated semantic artifacts must stay under `.research-cache/`. The bundled
query path records a small `last-query.json` audit artifact in the configured
semantic cache directory, but Markdown pages and normalized source records remain
the source of truth.

Semantic retrieval is a recall aid only. It does not replace reading matched
pages, citation verification, coverage manifests, grounding quotes, currentness
checks, or publication readiness gates.

## Optional MCP and qmd Search

The starter now ships a stdio MCP server (`evidence-wiki serve-mcp --target
PATH`) that exposes the existing `query_index.py` JSON contract as the
`query_index` MCP tool. This is an integration surface over the same local
lexical, FTS, or configured-provider retrieval behavior; it does not replace
`query_index.py` as the canonical contract.

qmd or a separate MCP-style search service can still be useful when a project
already has a local search tooling layer that agents call consistently across
workspaces.

Use qmd or MCP search when:

- retrieval should be shared across multiple tools or agents;
- the project uses the packaged MCP server or already runs another MCP server/query daemon;
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
