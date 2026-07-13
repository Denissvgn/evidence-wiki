# Workspace Init Profile

A workspace init profile is the reviewable handoff between `research-init` and the `evidence-wiki init` initializer. Agents write the profile before creating files so a maintainer can inspect inferred setup decisions, run a dry preview, and replay initialization deterministically.

The profile is YAML or JSON. The canonical root key is `workspace_init`.
When a profile is used to create a workspace, the initializer also renders an init report in the created workspace. The default path is `docs/workspace-init-report.md`.

## Minimal Shape

```yaml
workspace_init:
  schema_version: "0.1"
  target_path: ../my-research-workspace
  project:
    name: my-research-workspace
    description: Research workspace for a specific topic.
    owner_goal: Build a source-grounded knowledge base for decisions.
    language: en
  domain_guidance:
    mode: none
    rationale: Generic starter taxonomy is sufficient for the first setup pass.
  domain_pack:
    enabled: false
  raw:
    immutable: true
    source_roots:
      - raw/papers
      - raw/links
  claim_strictness: structured_claims
  ingest:
    claim_extraction: true
  outputs:
    supported_formats:
      - markdown
      - json
  integrations:
    git:
      snapshot_user_edits: explicit
    codebase_analysis:
      enabled: false
      provider: none
      command: null
      output_dir: sources/code_wikis
      read_only: true
      install_hooks: false
      background_sync: false
      untrusted_input: null
  assumptions:
    - Generic wiki taxonomy is sufficient for the first setup pass.
  skipped_decisions:
    - No network fetching during initialization.
    - No git hooks or background automation during initialization.
  next_actions:
    - Run smoke validation from the created workspace root.
```

## Required Fields

- `schema_version`: supported profile schema version. The current version is `"0.1"`.
- `target_path`: target workspace path. Explicit `--target` overrides it.
  Treat both values as trusted operator inputs. When a reviewed setup bundle
  should be confined to one filesystem area, run the initializer with
  `--scope-root PATH` so the profile file and effective target must resolve
  under that root.
- `project.name`: short stable project identifier.
- `project.description`: one-sentence research scope.
- `project.owner_goal`: practical outcome the workspace should support.
- `project.language`: default language for generated project text.
- `domain_guidance` or `domain_pack`: domain decision made before file writes.
- `raw.source_roots`: non-empty list of immutable evidence roots to create and
  scan. Roots must use portable `/` separators, remain workspace-relative, and
  be unique under case-insensitive comparison. Drive/UNC paths, reserved Windows
  names, traversal, case collisions, and ancestor/descendant overlaps are
  rejected before any target write.
- `claim_strictness`: one of `none`, `source_notes`, or `structured_claims`.
- `ingest.claim_extraction`: boolean matching the claim strictness decision.
- `outputs.supported_formats`: non-empty list containing only `markdown`,
  `marp`, `csv`, `json`, or the compatibility format
  `presentation_outline`.
- `integrations.git.snapshot_user_edits`: must be `explicit`.
- `integrations.codebase_analysis`: optional read-only repository evidence adapter settings. Keep disabled unless repositories are research sources.
- `assumptions`: non-empty list of inferred defaults or unresolved operating assumptions.
- `skipped_decisions`: non-empty list of decisions intentionally deferred or skipped during initialization.

## Init Report Fields

Profile-driven initialization renders a Markdown setup report at `docs/workspace-init-report.md` by default. Override that path with:

```yaml
workspace_init:
  init_report:
    path: docs/setup/workspace-init-report.md
```

The report path must be workspace-relative. Do not use absolute paths, URLs, or `..` traversal.

The following optional fields add audit detail to the generated report:

- `questions_asked`: non-empty list of setup questions asked before initialization.
- `inferred_answers`: mapping from stable decision names to strings or non-empty string lists.
- `initial_sources`: non-empty list of supplied source paths, URLs, folders, repositories, or notes that should be routed after initialization.
- `existing_wiki_path`: string path to an existing wiki or notes collection that needs migration review.
- `validation.commands`: non-empty list of validation commands the agent should run after workspace creation.
- `validation.results`: list of mappings with `command`, `status`, and `summary`. Supported statuses are `pending`, `passed`, `failed`, `blocked`, and `not_run`.
- `next_actions`: non-empty list of recommended follow-up steps.

The initializer records validation commands and supplied results; it does not run those commands. Agents should update the generated report after running smoke validation, inventory dry-runs, normalization dry-runs, or lint checks.

## Handoff Correlation

The optional `handoff` block carries upstream correlation identifiers from an external orchestrator or parent agent into the created workspace:

```yaml
workspace_init:
  handoff:
    task_id: chain-task-0042
    requested_by: planner-agent
    chain_run_id: run-2026-06-09-a
```

- Allowed keys: `task_id`, `requested_by`, `chain_run_id`. Unknown keys are rejected.
- Each present key must be a non-empty string; an empty `handoff` mapping is rejected.
- At least one key is required when the block is present. The block itself is optional; profiles without it behave exactly as before.

Accepted values are persisted verbatim into the created workspace's `research.yml` under `project.handoff` and surfaced by `scripts/workspace_status.py`, so status reports and exported results stay correlated with the upstream task. See `docs/orchestrator-handoff.md` for the end-to-end machine contract.

## Seed Questions

The optional `questions` list seeds the question task backlog during initialization. Each entry becomes an `open` question page under the configured wiki questions directory (`wiki/questions` by default) and a row in the `index.md` Questions section.

```yaml
workspace_init:
  questions:
    - id: scaling-laws
      question: How do scaling laws affect emergent abilities?
      priority: high
      origin: parent_agent
    - question: What evaluation benchmarks matter for reasoning?
```

Each item is a mapping with these keys:

- `question` (or `text`): required question text.
- `id`: optional stable slug source. Defaults to a slug derived from the question text. Slugs are made unique within the questions directory.
- `priority`: optional `high`, `medium`, or `low`. Defaults to `medium`.
- `origin`: optional source of the question. Defaults to `parent_agent`.

Seeded questions start with `status: open` and an empty `source_ids` list, so a parent agent can assign work before any evidence exists. Requires `wiki.required_dirs` to include `questions`. Work the backlog with the `research-answer` skill and `scripts/question_status.py`.

## Domain Guidance

Use `domain_guidance.mode` to make the domain decision explicit:

- `none`: start with the generic starter taxonomy.
- `domain_pack`: apply a reusable domain pack selected by `domain_pack.name` or `domain_pack.path`.
- `project_local`: render project-local guidance from this profile during workspace creation.
- `deferred`: domain guidance was considered and intentionally deferred.

When `domain_pack.enabled` is `false`, do not set `name` or `path`. When a pack is enabled, choose exactly one of `name` or `path`.
If the selected pack declares `domain_pack.recommended_acquisition`, the init
report names those provider IDs for planner/fetch-agent routing. This is
advisory only; acquisition remains disabled unless
`integrations.acquisition.enabled` is explicitly true in the setup profile or
`research_yml` overrides.

Example reusable pack selection:

```yaml
workspace_init:
  schema_version: "0.1"
  target_path: ../llm-research-workspace
  project:
    name: llm-research-workspace
    description: Research workspace for LLM research systems.
    owner_goal: Build a source-grounded map of LLM research methods.
    language: en
  domain_guidance:
    mode: domain_pack
    rationale: The LLM research domain pack matches the research scope.
  domain_pack:
    enabled: true
    name: llm-research
  raw:
    source_roots:
      - raw/papers
      - raw/links
  claim_strictness: structured_claims
  ingest:
    claim_extraction: true
  outputs:
    supported_formats:
      - markdown
      - json
  integrations:
    git:
      snapshot_user_edits: explicit
    acquisition:
      enabled: false
      providers: []
      target_root: raw/papers
      max_downloads_per_run: 10
      require_license_check: true
  assumptions:
    - Papers and curated links are enough for the first ingest cycle.
  skipped_decisions:
    - No codebase-analysis adapter during initial setup.
```

Example project-local guidance when no reusable pack matches:

```yaml
workspace_init:
  schema_version: "0.1"
  target_path: ../governance-research-workspace
  project:
    name: governance-research-workspace
    description: Research workspace for benchmark governance evidence.
    owner_goal: Compare governance policies across benchmark programs.
    language: en
  domain_guidance:
    mode: project_local
    path: docs/project-domain-guidance.md
    scope: Benchmark governance, dataset revision, and dispute handling evidence.
    rationale: No reusable domain pack matches this narrow governance scope.
    source_priorities:
      - Official benchmark documentation before secondary commentary.
      - Repository release notes before informal summaries.
    extraction_targets:
      - benchmark governance process
      - dataset revision policy
      - evaluation dispute handling
    claim_types:
      - governance_policy_claim
      - benchmark_revision_claim
    filing_rules:
      - File benchmark programs under wiki/benchmarks.
      - File unresolved governance gaps under wiki/questions.
    output_scaffolds:
      - benchmark governance brief
      - revision risk register
    promotion_notes:
      - Promote only if multiple future projects reuse these governance rules.
  domain_pack:
    enabled: false
  raw:
    source_roots:
      - raw/links
      - raw/other
  claim_strictness: structured_claims
  ingest:
    claim_extraction: true
  outputs:
    supported_formats:
      - markdown
      - json
  integrations:
    git:
      snapshot_user_edits: explicit
  assumptions:
    - Public benchmark documentation is enough for the first source cycle.
  skipped_decisions:
    - No reusable domain pack during initialization.
```

For `project_local`, the initializer renders a Markdown guidance document at `domain_guidance.path` or `docs/project-domain-guidance.md` by default. The path must be workspace-relative. `extraction_targets`, `source_priorities`, `claim_types`, `output_scaffolds`, and `filing_rules` are required non-empty lists. `promotion_notes` is optional.

## `research.yml` Overrides

Supported top-level config sections are merged into `research.yml`:

- `raw`
- `sources`
- `wiki`
- `taxonomy`
- `ingest`
- `run`
- `lint`
- `outputs`
- `integrations`

The same sections may also be nested under `research_yml`. Dictionaries are
deep-merged; list values replace the starter list. Core `project`, `raw`,
`ingest`, and `outputs` mappings reject unknown keys; an experimental extension
key in those mappings must use an explicit `x-` prefix. Unknown setup-profile
top-level keys are always refused. Other namespaced integration and domain-pack
configuration remains additive, but its owning provider or pack must validate
the nested contract before use.

`integrations.acquisition` is optional and default-disabled. When present,
`enabled` must be boolean, `providers` must list supported provider IDs
(`arxiv`, `openalex`, `github`, `web`), `target_root` must stay under `raw/`,
`max_downloads_per_run` must be a positive integer, and
`require_license_check` must be boolean. Initialization rejects hook,
auto-fetch, auto-download, auto-commit, auto-add, background-agent, and
background-sync keys.

The `run` section carries per-run budgets for unattended loops, run liveness, and intake containment (`max_questions_per_run`, `max_source_requests_per_run`, `max_releases_per_run`, `max_open_questions_total`, `max_intake_per_hour`, `max_mcp_intake_batch_questions`, `claim_staleness_hours`, `stale_run_threshold_hours`); each value must be a positive integer when present. If a setup profile overrides `max_questions_per_run` without `max_releases_per_run`, initialization derives the release cap as 3 x `max_questions_per_run`. Wall-clock and token budgets belong to the orchestrator, not the workspace.

Project identity values from `project` become the final `research.yml.project` values unless explicit CLI flags override them.

## Optional Codebase Analysis

Enable codebase analysis only when repositories, source archives, or local implementation snapshots are part of the research evidence:

```yaml
workspace_init:
  raw:
    source_roots:
      - raw/papers
      - raw/links
      - raw/code
  integrations:
    git:
      snapshot_user_edits: explicit
    codebase_analysis:
      enabled: true
      provider: agent-wiki-cli
      command: llm-wiki context --src-dir raw/code/example --budget 12000 --format json
      output_dir: sources/code_wikis
      read_only: true
      install_hooks: false
      background_sync: false
      untrusted_input: acknowledged
```

The initializer creates the configured `output_dir` when the integration is enabled. It records the command but does not run it. Set `untrusted_input: acknowledged` only after selecting an adapter safe for untrusted input. Generated architecture artifacts remain under `sources/` and become source evidence; maintained interpretation still belongs in `wiki/` after citation and review.

## Safety Rules

Profile validation runs before dry-run output or file writes. The initializer refuses profiles that are ambiguous or unsafe:

- unsupported `schema_version`,
- missing required fields,
- malformed lists or mappings,
- domain guidance that disagrees with domain-pack selection,
- project-local guidance combined with a reusable domain pack,
- empty source roots or output formats,
- empty or malformed project-local guidance lists,
- absolute paths, URLs, or `..` traversal in configured workspace paths,
- git hook, auto-commit, background automation, or auto-sync settings.
- codebase-analysis output outside `sources/`, disabled read-only mode, hooks, auto-add, auto-commit, background agents, or background sync.

The script also refuses to initialize a workspace inside the reusable starter root and refuses non-empty targets unless `--force` is supplied.

## Review Workflow

1. Write the profile from the user's minimal answers and inferred defaults.
2. Review `assumptions` and `skipped_decisions` before file writes.
3. Preview the plan:

    ```bash
    evidence-wiki init --profile /path/to/workspace-init.yml --scope-root /path/to/reviewed-root --dry-run
    ```

4. Initialize the workspace:

    ```bash
    evidence-wiki init --profile /path/to/workspace-init.yml --scope-root /path/to/reviewed-root
    ```

5. Run smoke validation from the created workspace root before inventory, normalization, or broader lint checks.
