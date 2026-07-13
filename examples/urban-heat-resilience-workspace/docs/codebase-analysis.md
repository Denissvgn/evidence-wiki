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
    provider: agent-wiki-cli
    command: llm-wiki context --src-dir raw/code/example --budget 12000 --format json
    output_dir: sources/code_wikis
    read_only: true
    install_hooks: false
    background_sync: false
```

The output directory must be under `sources/`. Generated architecture output is source material, not maintained wiki content. Promote findings into `wiki/` only through normal source notes, claims, system pages, or synthesis pages with `source_ids`.

## Inventory Behavior

When enabled, `scripts/source_inventory.py` emits `kind:codebase_architecture` records for:

- GitHub repository URLs in raw link files;
- local repository directories under code roots such as `raw/code`;
- source archives such as `.zip`, `.tar.gz`, or `.tgz`.

Each record includes `metadata.codebase_source_type` and a generated `metadata.codebase_output_dir` under `sources/code_wikis/`. Local repository files are suppressed as separate raw records so the repository is treated as one evidence unit.

## Adapter Artifacts

Run adapter commands manually and save the output under the record's `metadata.codebase_output_dir`. The normalizer looks for files such as:

- `context.json`
- `extract.json`
- `summary.json`
- `context.md`
- `extract-summary.md`
- `summary.md`

For `agent-wiki-cli` / `python-wiki-llm`, prefer read-only commands:

```bash
llm-wiki extract --src-dir raw/code/example --format json > sources/code_wikis/<safe-source-id>/extract.json
llm-wiki context --src-dir raw/code/example --budget 12000 --format json > sources/code_wikis/<safe-source-id>/context.json
```

Use `llm-wiki bootstrap` only when a generated architecture wiki is useful, and write it to the generated source directory, not to the maintained `wiki/`:

```bash
llm-wiki bootstrap --src-dir raw/code/example --out-dir sources/code_wikis/<safe-source-id>/wiki
```

Do not install hooks, auto-add generated files, auto-commit, or run background sync by default.

## Normalization

`scripts/normalize_sources.py` reads local artifacts and writes normalized records under `sources/normalized/`:

- artifact found: `extraction_method: codebase_context`, `fetch_status:  artifact_recorded`, and `codebase_artifact_paths` list the local files read;
- artifact missing: `extraction_method: codebase_stub`, `status: stubbed`, and `parse_warnings` explains where output should be saved.

The generated normalized source can then be cited from a maintained source note or synthesis page through its `source_id`.