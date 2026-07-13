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
- `source_roots`: canonical portable workspace-relative directories scanned by
  source inventory tooling. Values use `/`, may not contain drives, UNC roots,
  traversal, reserved components, duplicates, case collisions, or overlapping
  ancestor/descendant scan roots.

### `sources`

Defines generated source metadata locations and lifecycle states.

- `manifest_path`: JSONL manifest written by inventory tooling.
- `normalized_dir`: Markdown records generated from raw sources.
- `cards_dir`: compact source cards or summaries.
- `source_requests_path`: JSONL source-request artifact managed by `scripts/source_requests.py` (see `docs/source-delivery.md`).
- `sources.coverage_dir`: directory for per-question coverage manifests, defaulting to `sources/coverage` (see `docs/coverage-manifest.md`).
- `default_status`: lifecycle state assigned to newly discovered sources.
- `lifecycle_statuses`: allowed states for source records.

`default_status` must be one of the configured lifecycle values.
`manifest_path` and `source_requests_path` use the `.jsonl` extension so their
append/rewrite semantics are unambiguous.

The default source lifecycle is:

- `discovered`: found in raw sources but not processed.
- `normalized`: converted into an agent-readable record.
- `noted`: represented by a wiki source note.
- `integrated`: cited or synthesized into broader wiki pages.
- `deferred`: intentionally postponed for later review or ingestion.
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
- `field_types`: field type checks. Supported types are `string`, `string_list`, `scalar`, and `boolean`.
- `non_empty_fields`: fields that must not be empty when present.
- `allowed_values`: allowed scalar values for specific fields.

Default type-specific rules require source notes to cite at least one source ID, decisions to include a configured `status`, and claims to include structured claim fields such as `subject`, `predicate`, `object`, and `source_ids`. Question task records allow resolver-managed fields such as `answer_page`, `blocked_reason`, `resolution_reason`, `claimed_by`, `claimed_at`, `confidence`, `evidence_strength`, `coverage_required`, and `coverage_manifest`.

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

### `run`

Defines per-run budgets for unattended research loops (the `research-run` skill). Each value must be a positive integer when present; absent values fall back to the documented defaults, which `scripts/workspace_status.py` reports in its `run` section. Wall-clock and token budgets belong to the orchestrator, not the workspace.

- `max_questions_per_run`: maximum questions one unattended run should resolve (default 25).
- `max_source_requests_per_run`: maximum source requests one run should open (default 10).
- `max_releases_per_run`: maximum successful claim releases one run should perform before stopping (default 3 x `max_questions_per_run`; template default 75).
- `max_discovery_results_per_run`: maximum discovery candidate/result records one run should propose (default 50).
- `max_academic_provider_requests_per_run`: maximum OpenAlex/arXiv provider requests one run should make (default 25).
- `max_manual_url_deliveries_per_run`: maximum manual URL/file deliveries one run should count (default 10).
- `max_web_downloads_per_run`: maximum contracted `web get` downloads one run should count. Defaults to `max_manual_url_deliveries_per_run` when unset.
- `max_open_questions_total`: maximum currently `open` questions allowed after one intake batch (default 250).
- `max_intake_per_hour`: maximum newly created questions accepted through intake in one rolling hour (default 25).
- `max_mcp_intake_batch_questions`: maximum `questions[]` items accepted in one MCP `intake_questions` call (default 100).
- `claim_staleness_hours`: age after which lint reports an `in_progress` claim as stale (default 24).
- `stale_run_threshold_hours`: age after which `workspace_status.py` reports an active run with no heartbeat or event as stale (default 4).

`workspace_status.py` also reports `run.max_acquisition_downloads_per_run`
from `integrations.acquisition.max_downloads_per_run` and
`run.max_github_archive_bytes_per_run` from
`integrations.acquisition.github.max_archive_bytes`, plus
`run.max_web_downloads_per_run` from the run block/manual URL alias. Those
limits stay in the acquisition config when provider adapters enforce their own
download and byte guards; the run section is the orchestrator-facing status
view.

### `lint`

Defines validation behavior for future lint tooling.

- `validate_structure`: check configured directories.
- `validate_frontmatter`: check required fields and allowed types.
- `validate_links`: check internal Markdown links.
- `validate_source_coverage`: compare manifest, normalized records, and notes.
- `validate_claims`: validate structured claim pages or embedded claims.
- `validate_provenance`: require license provenance on automated deliveries (manifest records whose `provenance.retrieved_by` is set; MEDIUM `provenance_missing_license`).
- `validate_source_requests`: check the source-request artifact — blocked questions should reference an open or fulfilled request (LOW `question_blocked_no_request`) and fulfilled requests must point at existing manifest sources (MEDIUM `request_fulfilled_missing_source`); malformed request lines are reported (MEDIUM `source_request_invalid`).
- `validate_output_license_status`: require reusable output pages under `outputs.default_dir` to cite fetched sources with concrete license metadata. If an output page cites a manifest source whose `provenance.retrieved_by` is set and whose `provenance.license` is missing, null, or empty, lint reports LOW `output_license_missing`.
- `validate_questions`: validate question task records, including answered/blocked consistency, coverage manifests, and claim hygiene — answered questions with `coverage_required: true` but missing, blocked, or invalid coverage emit HIGH `question_coverage_missing`, `question_coverage_blocked`, or `question_coverage_invalid`; `in_progress` questions without `claimed_by`/`claimed_at` emit MEDIUM `question_claim_missing`, and claims older than `run.claim_staleness_hours` emit LOW `question_claim_stale`.
- `detect_prompt_injection_patterns`: default-on weak reviewer-awareness heuristic. When enabled, lint scans normalized Markdown records, question pages, and parsed manifest `provenance.notes` values for instruction-like phrases, structural prompt-injection shapes, and large base64-like blobs after Unicode/zero-width normalization. It reports LOW `source_prompt_injection_pattern` findings and never reads raw files, opens provenance sidecars, or fetches provenance URLs.
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
- `codebase_analysis.untrusted_input`: set to `acknowledged` when enabled only after choosing an adapter safe for untrusted input; missing acknowledgement produces a LOW lint finding.
- `acquisition.enabled`: whether explicit source acquisition is active.
- `acquisition.providers`: enabled provider IDs. Supported IDs are `arxiv`, `openalex`, `github`, and `web`.
- `acquisition.target_root`: raw evidence directory for downloaded or delivered papers. Defaults to `raw/papers`.
- `acquisition.max_downloads_per_run`: positive per-run download budget.
- `acquisition.require_license_check`: whether acquisition workflows must surface license status before handoff.
- `acquisition.github.target_root` (optional): raw evidence directory for GitHub downloads. Must stay under `raw/`. Defaults to `raw/code`. Add it to `raw.source_roots` so inventory records captured archives.
- `acquisition.github.max_archive_bytes` (optional): positive byte ceiling for GitHub source archives. Defaults to 104857600 (100 MiB).
- `acquisition.web.target_root` (optional): raw evidence directory for contracted web downloads. Must stay under `raw/`. Defaults to `raw/web`.
- `acquisition.web.allowed_domains`: required non-empty domain allow-list for `web get` when the `web` provider is enabled.
- `acquisition.web.max_download_bytes` (optional): positive byte ceiling for one web response. Defaults to 10485760 (10 MiB).
- `discovery.enabled`: whether optional source discovery is active.
- `discovery.providers`: enabled discovery provider families. Standards routes require `standards` or a route-specific value such as `standards:iso-open-data`.
- `discovery.candidate_store_path`: workspace-relative JSONL path for proposed source candidates. Defaults to `sources/discovery/candidates.jsonl`.
- `retrieval.provider`: retrieval engine name; `lexical` uses the bundled local engine.
- `retrieval.command`: optional external provider command. Use list form for safest argument handling.
- `retrieval.timeout_seconds`: provider timeout before `query_index.py` falls back to lexical retrieval.
- `retrieval.semantic.enabled`: opt-in hybrid semantic recall switch. Defaults to `false`.
- `retrieval.semantic.transport`: `command` or `http` provider transport.
- `retrieval.semantic.command`: command transport argv list or string.
- `retrieval.semantic.endpoint`: HTTP transport endpoint for JSON `POST` requests.
- `retrieval.semantic.cache_dir`: generated semantic cache directory under `.research-cache/`.

Codebase analysis is optional. Treat generated architecture output as source material under `sources/`, not as maintained research wiki pages, unless a human explicitly promotes the evidence into `wiki/`.
Acquisition is also optional and disabled by default. Domain packs may recommend
providers through `domain_pack.recommended_acquisition`, but initialization only
surfaces that recommendation in the init report; it never enables fetching.
Discovery is optional and disabled by default. New discovery families must be
explicitly listed in `integrations.discovery.providers` before their routes run;
domain-pack recommendations are advisory and do not enable discovery.

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
    untrusted_input: null
  acquisition:
    enabled: false
    providers: []
    target_root: raw/papers
    max_downloads_per_run: 10
    require_license_check: true
  discovery:
    enabled: false
    providers: []
    candidate_store_path: sources/discovery/candidates.jsonl
  retrieval:
    provider: lexical
    command: null
    timeout_seconds: 30
    semantic:
      enabled: false
      provider: null
      transport: command
      command: null
      endpoint: null
      timeout_seconds: 30
      cache_dir: .research-cache/semantic-retrieval
```

When codebase analysis is enabled, `provider` must name the adapter and
`output_dir` must stay under `sources/`. Initialization and smoke validation
refuse paths under `wiki/` or `raw/`, git hooks, auto-commit, auto-add,
background agents, and background sync. The template records adapter commands
for users or agents to run explicitly; inventory and normalization never
execute those commands. `raw/code/` is an untrusted-input boundary, so enabled
codebase analysis should include `untrusted_input: acknowledged` only after the
operator selects an adapter safe for untrusted input.

When acquisition is enabled, `providers` must list one or more supported
provider IDs and `target_root` must stay under `raw/`. Initialization and smoke
validation refuse hooks, auto-fetch/download flags, auto-commit/add settings,
background agents, and background sync. This repository records the contract
only; adding a provider recommendation to a domain pack does not add network
behavior. Provider terms, provenance requirements, and the acquisition safety
model are documented in [acquisition.md](acquisition.md).

External retrieval providers are optional. When `integrations.retrieval.provider`
is anything other than `lexical` and `command` is configured, `query_index.py`
sends the provider a JSON request containing the query, scope, limit, configured
corpus roots, and local document metadata. Provider results must return
workspace-relative paths from that corpus plus numeric scores. Invalid provider
responses warn and fall back to lexical retrieval; see `docs/retrieval-upgrades.md`
for the full contract.

Semantic retrieval is separate and best-effort. When
`integrations.retrieval.semantic.enabled: true`, the provider returns ranked
workspace-relative paths that are merged with lexical/FTS results as
`engine: hybrid`. Semantic artifacts must stay under `.research-cache/`, and
semantic ranking never replaces grounding, citation, coverage, or publication
readiness gates.

## Extension Rules

- Keep the top-level section names stable.
- Add domain-specific values by extending lists or adding nested keys.
- Do not rename configured directories without also updating the filesystem.
- Core profile mappings reject unknown fields. Experimental keys in strict core
  mappings use an explicit `x-` prefix; namespaced provider and domain-pack
  mappings remain additive and are validated by their owning component.
