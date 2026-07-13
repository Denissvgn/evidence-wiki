# Optional Codebase Analysis

Codebase analysis lets a research workspace cite repositories, source archives, or local implementation snapshots as source evidence. It is opt-in and artifact-based: scripts inventory repository evidence and normalize existing local adapter output, but they do not clone repositories, fetch network content, run adapters, install hooks, or start background sync.

## When To Enable

Enable `integrations.codebase_analysis` when the research question depends on:

- implementation availability or reproducibility;
- software architecture, module boundaries, or dependency evidence;
- repository comparison across papers, systems, or benchmarks;
- code-understanding evidence that should be cited like any other source.

Keep it disabled for projects that only need papers, web pages, datasets, or human notes.

## Configuration

Disabled default:

```yaml
integrations:
  codebase_analysis:
    enabled: false
    provider: none
    command: null
    output_dir: sources/code_wikis
    read_only: true
    install_hooks: false
    background_sync: false
    untrusted_input: null
```

Enabled example:

```yaml
raw:
  source_roots:
    - raw/links
    - raw/code
integrations:
  codebase_analysis:
    enabled: true
    provider: external-artifact
    command: null
    output_dir: sources/code_wikis
    read_only: true
    install_hooks: false
    background_sync: false
    untrusted_input: acknowledged
```

The output directory must be under `sources/`. Generated architecture output is source material, not maintained wiki content. Promote findings into `wiki/` only through normal source notes, claims, system pages, synthesis pages, or decision pages with `source_ids`.

`command` is a backward-compatible display-only field and should remain `null`.
No shipped product path interprets or executes it.

The code root `raw/code/` is an untrusted-input boundary. Set `untrusted_input: acknowledged` only after choosing an adapter safe for untrusted input; `scripts/lint.py` reports a LOW `codebase_untrusted_input` finding when enabled codebase analysis lacks that acknowledgement.

## Inventory Behavior

When enabled, `scripts/source_inventory.py` emits `kind:codebase_architecture` records for:

- GitHub repository URLs in raw link files;
- local repository directories under code roots such as `raw/code`;
- source archives such as `.zip`, `.tar.gz`, or `.tgz`.

Each record includes `metadata.codebase_source_type` and a generated `metadata.codebase_output_dir` under `sources/code_wikis/`. Local repository files are suppressed as separate raw records so the repository is treated as one evidence unit.

## Adapter Artifacts

A separately authorized acquisition worker may produce an inert artifact and
deposit it under the record's `metadata.codebase_output_dir`. The research
product only reads the deposited files: it does not clone or unpack a
repository, execute an adapter, load repository plugins, install hooks, or
start background work. The normalizer looks for files such as:

- `context.json`
- `extract.json`
- `summary.json`
- `context.md`
- `extract-summary.md`
- `summary.md`

Every publication-grade deposit includes `artifact-manifest.json`. The manifest
binds the expected source ID to a bounded file list with byte counts and SHA-256
checksums. It also records a structured `invocation.argv` list and explicitly
states `executed_by: external_worker`, `plugins_enabled: false`,
`hooks_enabled: false`, and `network_access: false`. These are retained,
self-asserted producer provenance; they do not turn artifact metadata into
repository identity evidence.

Example manifest:

```json
{
  "schema_version": "1",
  "artifact_kind": "codebase_evidence",
  "source_id": "codebase:example-0123456789",
  "generated_at": "2026-07-11T00:00:00Z",
  "producer": {"name": "authorized-worker", "version": "1.0"},
  "invocation": {
    "argv": ["external-analyzer", "analyze", "--input", "snapshot.zip"],
    "executed_by": "external_worker",
    "plugins_enabled": false,
    "hooks_enabled": false,
    "network_access": false
  },
  "files": [
    {"path": "context.json", "size_bytes": 123, "sha256": "sha256:<64 lowercase hex>"}
  ]
}
```

Malformed manifests, checksum drift, hidden/executable files, symlinks, more
than 128 supported files, more than 512 filesystem entries, or more than 16 MiB
of deposited artifacts remain invalid stub evidence. Code archives larger than
64 MiB and local snapshots with more than 10,000 files are review-required at
inventory time.

## External Producer Example (Outside Product Scope)

The following `agent-wiki-cli` / `llm-wiki` v1.2.x commands are examples for a
separately authorized external producer. They are not evidence-wiki commands,
are not invoked by any shipped script, and do not advertise a revision-bound
adapter execution capability in this product.

The supported command baseline below is `agent-wiki-cli` / `llm-wiki` v1.2.x.

```bash
llm-wiki extract --src-dir raw/code/example --summary --read-only --output sources/code_wikis/<safe-source-id>/extract.json
llm-wiki context --src-dir raw/code/example --budget 12000 --format json --focus all --read-only --output sources/code_wikis/<safe-source-id>/context.json
```

`extract` always emits JSON and does not accept `--format`. Prefer its explicit
`--output` option over shell redirection so the same command shape works on
Windows, macOS, and Ubuntu.

An external producer may use `llm-wiki bootstrap` when a generated architecture
wiki is useful, but must write it to the generated source directory, not to the
maintained `wiki/`:

```bash
llm-wiki bootstrap --src-dir raw/code/example --wiki-dir sources/code_wikis/<safe-source-id>/wiki --depth full --format json --source-adapter
```

`bootstrap` uses `--wiki-dir`, not `--out-dir`. `--source-adapter` keeps its
writes inside that generated wiki directory instead of updating agent
instruction files elsewhere in the workspace.

The external producer must not install hooks, auto-add generated files,
auto-commit, load project plugins, or run background sync.

## Normalization

`scripts/normalize_sources.py` reads local artifacts and writes normalized records under `sources/normalized/`:

- validated artifact found: `extraction_method: codebase_context`, `fetch_status: artifact_recorded`, `codebase_intake_status: validated`, and checksum-bound `codebase_artifact_paths` list the local files read;
- legacy artifact without a manifest: retained as `codebase_intake_status: legacy_unbound` and reported by lint; it is not publication-grade provenance;
- artifact missing: `extraction_method: codebase_stub`, `status: stubbed`, and `parse_warnings` explains where output should be saved.

The generated normalized source can then be cited from a maintained source note,
claim, synthesis, or decision through its `source_id`. Keep navigation explicit
in both directions:

1. the source note links to the normalized record;
2. every maintained claim, synthesis, or decision links to the source note;
3. the source note links back to each maintained interpretation.

Query JSON exposes the computed relationship as
`evidence_links.normalized_paths`, `evidence_links.maintained_paths`, and
`evidence_links.backlinks`. Lint reports `codebase_evidence_link_missing` when a
validated artifact lacks either navigation direction.

## Product Scope

EvidenceWiki neither clones repositories nor launches `python-wiki-llm` or
another adapter. Adding product-side cloning, archive extraction, subprocess
adapters, plugin loading, hook installation, or revision-bound execution would
expand the product's trust boundary and requires explicit design, security, and
cross-platform review.
