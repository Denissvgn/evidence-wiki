# `research.yml` Configuration Contract

`research.yml` is the template's public configuration interface. Scripts and agent instructions should read it for structure, source lifecycle states, page types, and validation behavior instead of hardcoding those choices.

## Top-Level Sections

### `project`

Describes the research project for humans and agents.

- `name`: short project identifier.
- `description`: one-sentence project description.
- `owner_goal`: practical reason the wiki exists.
- `language`: default language for generated project text.

### `raw`

Defines where immutable source material lives.

- `immutable`: when `true`, agents and scripts must not rewrite files under raw source roots.
- `source_roots`: directories scanned by source inventory tooling.

### `sources`

Defines generated source metadata locations and lifecycle states.

- `manifest_path`: JSONL manifest written by inventory tooling.
- `normalized_dir`: Markdown records generated from raw sources.
- `cards_dir`: compact source cards or summaries.
- `default_status`: lifecycle state assigned to newly discovered sources.
- `lifecycle_statuses`: allowed states for source records.

The default source lifecycle is:

- `discovered`: found in raw sources but not processed.
- `normalized`: converted into an agent-readable record.
- `noted`: represented by a wiki source note.
- `integrated`: cited or synthesized into broader wiki pages.
- `superseded`: replaced by a newer source or version.
- `rejected`: intentionally excluded from further processing.

### `wiki`

Defines the maintained knowledge layer.

- `root`: wiki directory path.
- `required_dirs`: wiki subdirectories expected by lint tooling.
- `allowed_page_types`: valid values for page frontmatter `type`.
- `frontmatter_required`: fields required on maintained wiki pages.
- `frontmatter_type_rules`: optional page-type-specific frontmatter rules.
- `date_format`: expected date format for `created` and `updated`.
- `link_style`: preferred internal link style.

`frontmatter_type_rules` supports these keys per page type:

- `required_fields`: additional fields required for that page type.
- `field_types`: field type checks. Supported types are `string`, `string_list`, and `scalar`.
- `non_empty_fields`: fields that must not be empty when present.
- `allowed_values`: allowed scalar values for specific fields.

Default type-specific rules require source notes to cite at least one source ID, decisions to include a configured `status`, and claims to include structured claim fields such as `subject`, `predicate`, `object`, and `source_ids`.

Structured claims can be represented as dedicated pages under `wiki/claims/` with `type: claim`, or as embedded frontmatter records on another wiki page:

```yaml
claims:
  - subject: File-as-Bus
    predicate: improves
    object: long-horizon ML research engineering performance
    value: 10.54
    unit: points
    scope: AiScientist paper benchmark setting
    source_ids:
      - paper:2604.13018v1
```

Embedded claim records require `subject`, `predicate`, `object`, and `source_ids`. If an embedded claim omits `source_ids`, linting inherits the containing page's `source_ids`. Claim contradiction checks compare only structured records with the same normalized `subject`, `predicate`, `unit`, and `scope`; missing scope only matches missing scope. The linter does not extract or compare numbers from prose.

## Standard Wiki Directories

The default wiki taxonomy is defined by `wiki.required_dirs` and mirrored by directories under `wiki/`. Scripts must read this configuration instead of hardcoding folder names.

| Directory | Page Type | Purpose |
|-----------|-----------|---------|
| `sources` | `source` | Source notes that summarize and cite normalized source records. |
| `entities` | `entity` | Organizations, people, projects, tools, labs, or other named things. |
| `concepts` | `concept` | Reusable ideas, definitions, taxonomies, and conceptual distinctions. |
| `methods` | `method` | Techniques, workflows, algorithms, or research methods. |
| `systems` | `system` | Concrete systems, implementations, products, or architectures. |
| `benchmarks` | `benchmark` | Evaluation suites, tasks, leaderboards, and scoring protocols. |
| `datasets` | `dataset` | Datasets, corpora, test sets, and generated data resources. |
| `claims` | `claim` | Structured evidence statements extracted from sources. |
| `synthesis` | `synthesis` | Cross-source maps, comparisons, literature reviews, and summaries. |
| `questions` | `question` | Open questions, research gaps, and planned investigations. |
| `decisions` | `decision` | Decision records about project direction or implementation choices. |
| `outputs` | `output` | Reusable generated artifacts such as reports, decks, tables, and exports. |

Domain packs may add, remove, or rename wiki directories by updating `research.yml` and the filesystem together. Scripts should tolerate unknown additional page types and nested configuration keys when the top-level contract remains stable.

### `taxonomy`

Defines generic classification values used during ingestion and synthesis.
Domain packs may extend these lists without changing script code.

- `entity_types`: kinds of entities worth extracting.
- `concept_types`: kinds of concepts worth extracting.
- `claim_types`: kinds of structured claims supported by default.

### `ingest`

Defines default behavior for source ingestion workflows.

- `source_note_required`: require a source note before broader integration.
- `claim_extraction`: extract important evidence as structured claims.
- `ask_before_large_wiki_update`: pause before broad multi-page edits.
- `large_update_page_threshold`: page count that qualifies as a large update.
- `update_log`: append ingest operations to the activity log when available.

### `lint`

Defines validation behavior for future lint tooling.

- `validate_structure`: check configured directories.
- `validate_frontmatter`: check required fields and allowed types.
- `validate_links`: check internal Markdown links.
- `validate_source_coverage`: compare manifest, normalized records, and notes.
- `validate_claims`: validate structured claim pages or embedded claims.
- `dataview_aware`: account for Dataview-generated index sections.
- `severity_levels`: allowed issue severities.

### `outputs`

Defines where reusable outputs belong and which formats are expected.

- `default_dir`: default path for generated reusable outputs.
- `supported_formats`: output formats the template expects agents to support.

### `integrations`

Defines optional external tooling behavior.

- `obsidian.enabled`: whether Obsidian-specific behavior is active.
- `obsidian.dataview`: Dataview support status.
- `git.snapshot_user_edits`: how agents should handle user edit snapshots.
- `codebase_analysis.enabled`: whether optional codebase analysis is active.
- `codebase_analysis.provider`: adapter name such as `agent-wiki-cli`, or `none` when disabled.
- `codebase_analysis.command`: command used by the adapter when enabled.
- `codebase_analysis.output_dir`: generated output area for architecture wiki artifacts or codebase-analysis records.
- `codebase_analysis.read_only`: keep `true` during initialization.
- `codebase_analysis.install_hooks`: must remain `false` during initialization.
- `codebase_analysis.background_sync`: must remain `false` during initialization.

Codebase analysis is optional. Treat generated architecture output as source material under `sources/`, not as maintained research wiki pages, unless a human explicitly promotes the evidence into `wiki/`.

Default disabled shape:

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

When enabled, `provider` must name the adapter and `output_dir` must stay under `sources/`. Initialization and smoke validation refuse paths under `wiki/` or `raw/`, git hooks, auto-commit, auto-add, background agents, and background sync. The template records adapter commands for users or agents to run explicitly; inventory and normalization never execute those commands.

## Extension Rules

- Keep the top-level section names stable.
- Add domain-specific values by extending lists or adding nested keys.
- Do not rename configured directories without also updating the filesystem.
- Scripts should tolerate unknown nested keys for forward compatibility.
