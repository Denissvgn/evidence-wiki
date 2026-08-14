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
- `pdf_extractor`: PDF text backend. `pypdf` is the portable canonical default
  installed with EvidenceWiki. `poppler` selects the optional `pdftotext`
  compatibility backend and requires the Poppler system package on `PATH`.
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

Default type-specific rules require source notes to cite at least one source ID, decisions to include a configured `status`, and claims to include structured claim fields such as `subject`, `predicate`, `object`, and `source_ids`. Question task records allow resolver-managed fields such as `answer_page`, `blocked_reason`, `resolution_reason`, `claimed_by`, `claimed_at`, `confidence`, `evidence_strength`, `coverage_required`, and `coverage_manifest`, plus the review fields `human_review_required`, `human_review_status`, `human_review_requested_at`, `human_review_approved`, `human_review_policies`, `approved_by`, and `approved_at`.

The question `status` values include `human_review`, which
`scripts/question_resolve.py answer --require-coverage` writes when a coverage
policy requires manual sign-off. The per-policy `human_reviews` list is
intentionally undeclared: it is a list of mappings, and `field_types` has no
type for that shape. Lint validates only declared fields, so an undeclared
field is accepted rather than reported.

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
- `max_academic_provider_requests_per_run`: maximum arXiv/OpenAlex academic-discovery transport attempts one run may reserve, including retries and zero-result/error calls (default 25). Acquisition calls use the separate download budget.
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

### `review`

Optional. Defines how far one pending human review escalates. The whole section
may be omitted; every key then falls back to the default below, which reproduces
the behavior of workspaces that predate the section. Adding the section does not
change `compatible_research_yml_contract`.

- `escalation_scope`: `workspace` (default) or `question`.
  - `workspace`: a question in `human_review` contributes to the
    `attention_required` readiness verdict, so orchestration refuses to operate
    over the workspace until the review is recorded.
  - `question`: a pending review parks only its own question. The question is
    still excluded from scheduling (`human_review` is not an actionable status),
    but `scripts/workspace_status.py` reports it under `questions_awaiting_review`
    rather than flipping the verdict.
- `max_pending_review_hours`: age after which lint reports a question that has
  sat in `human_review` too long (default 168). Must be a positive integer, or
  `null` to disable the age finding. The finding applies under
  `escalation_scope: question` only; under `workspace` scope the pending review
  already blocks the workspace.

The age finding is the rot guard for the scoped mode. It is HIGH on purpose:
HIGH lint findings flip the readiness verdict to `attention_required`, so a
review queue nobody works re-freezes the workspace through the existing
lint-to-verdict path rather than sitting unnoticed behind a scoped escalation.
Lint measures the age from `human_review_requested_at`, which the answer
transition stamps when it parks a question.

Under either scope, a question in `human_review` also keeps drawing the MEDIUM
`question_human_review_pending` finding. That is expected, not a defect: it is
how a pending review stays visible to anyone reading lint output directly.
MEDIUM findings do not change the readiness verdict, but they do count towards
`readiness.operational_debt.warning_count`, so a workspace with parked questions
reports non-zero operational warnings while reviews are outstanding.

Unlike `run`, an invalid `review` value is not silently replaced by its default:
scripts reject the workspace config, because defaulting a misconfigured review
scope back to `workspace` would silently freeze the workspace the operator was
configuring. Omit a key to accept its default, or use an `x-` prefix for
experimental keys.

```yaml
review:
  escalation_scope: question
  max_pending_review_hours: 168
```

### `normalization`

Optional. Maps manifest source kinds to an external normalizer, so evidence this
package cannot extract — structured API payloads, instrument output — can produce
normalized records through the normal pipeline instead of being written around it. The
whole section may be omitted; no adapter then exists and normalization behaves exactly
as it did before the section. Adding the section does not change
`compatible_research_yml_contract`.

```yaml
normalization:
  adapters:
    - kinds: [structured_data]
      provider: command
      command: ["autoseller-normalize", "--format", "json"]
      name: autoseller-normalize
      version: "1.4.0"
      timeout_seconds: 120
```

- `adapters`: list of adapter declarations. Absent or empty means no adapter.
- `kinds`: non-empty list of manifest source kinds this adapter normalizes. One kind
  resolves to exactly one adapter, so two adapters may not claim the same kind.
  Membership is open — a domain pack may declare its own namespaced kinds — but kinds
  `normalize_sources.py` extracts itself (`paper`, `pdf`, `repo_link`, `web_link`,
  `html`, `table`, `codebase_architecture`) are refused, because adapters exist to fill
  a gap rather than to override a built-in extractor from one config line.
- `provider`: `command` (the only value today). Required rather than defaulted, so
  `research.yml` states plainly that the workspace executes something.
- `command`: argv list, for example `["tool", "--flag", "value"]`. A command string is
  refused: splitting one into arguments would guess at the operator's quoting, and an
  argv list says exactly what runs with no shell in the path.
- `name`, `version`: the adapter's declared identity. Both are required and are matched
  against what the adapter reports, so a record's stated producer can be checked
  against what this workspace authorized. `version` is a string; quote it so a value
  like `"1.40"` keeps its exact form.
- `timeout_seconds`: optional, default 120, between 1 and 3600. An adapter that never
  returns would otherwise hang normalization.

**Security stance.** Configuring an adapter authorizes this package to execute that
command during normalization — the same explicit-reviewed-config model as
`integrations.retrieval.command`. The package grants the adapter nothing: no network,
no credentials, no writes of its own. Whatever the adapter reaches, it reaches on its
own authority. This is deliberately unlike `integrations.codebase_analysis`, whose
configured command this package records for a human to run but never executes itself;
that boundary is unchanged.

`scripts/doctor.py` reports what a workspace is authorized to execute under its
`normalization_adapters` check: every configured adapter's kinds, command, declared
identity, and timeout, or an explicit "no external normalizer adapters are configured"
when the section is absent. Doctor only reads the declaration — it never runs the
command — so it is safe to use as the audit step before an unattended run.

An invalid `normalization` value is not silently replaced by a default: scripts reject
the workspace config, because quietly ignoring a misconfigured adapter would let a
workspace believe its structured evidence had been normalized when nothing ran.
`normalize_sources.py` reports the failure through the shared error envelope with
`error_code: CONFIG_INVALID`, and doctor reports the same problem as a degraded check.
Omit a key to accept its default, or use an `x-` prefix for experimental keys.

See [normalized-source-format.md](normalized-source-format.md) for the record contract
adapter output must satisfy.

**Structured-view sidecars need no configuration.** An adapter may return an optional
`structured` key, whose content becomes the record's uncapped structured-view sidecar and
is what anchor-form grounding resolves pointers against; native CSV/TSV records emit one
from the parse the normalizer already runs. Neither is switched on here or anywhere else.
The feature is artifact-driven and wholly additive: a workspace whose sources produce no
sidecars, and whose questions carry no anchor-form grounding entries, behaves exactly as
it did before sidecars existed.

### `orchestration`

Optional. Declares who acquires evidence for this workspace. The whole section may be
omitted; every key then falls back to the default below, which reproduces the behavior of
workspaces that predate the section. Adding the section does not change
`compatible_research_yml_contract`.

```yaml
orchestration:
  acquisition: delegated
  acquirer_agent_id: autoseller-orchestrator
  max_attempts_per_request: 2
```

- `acquisition`: `providers` (default) or `delegated`.
  - `providers`: the orchestration controller issues an acquisition work order only when
    an enabled `integrations.acquisition` provider can fetch the source. A workspace with
    no enabled provider never reaches the acquisition phase and terminates
    `blocked_on_sources` once its open requests have no route.
  - `delegated`: an external acquirer fulfils source requests. The controller issues
    acquisition work orders scoped to open request ids and addressed to
    `acquirer_agent_id`; the acquirer delivers evidence, fulfils the request, and reopens
    the blocked question **while that order is pending**, so the mutation is verified as a
    postcondition instead of happening between actions where no work order accounts for
    it. See [orchestration.md](orchestration.md).
- `acquirer_agent_id`: required under `acquisition: delegated`, refused otherwise. Same
  shape as `--agent-id`: a non-empty string of at most 160 characters with no control
  characters.
- `max_attempts_per_request`: recorded attempt failures that retire one source request
  within a session (default 2, maximum 10). Read under delegation only. There is
  deliberately no unlimited value: an unbounded retry budget would let one failing request
  keep a session alive forever. Attempts are counted per session, so a new session gets a
  fresh look at every request — that is the supported way to retry after fixing a
  host-side cause, rather than editing the append-only attempt audit.

**Delegation is not a provider grant.** It authorizes no network access, no credentials,
and no fetching by this package; whatever the acquirer reaches, it reaches on its own
authority. `integrations.acquisition.providers` must stay empty under delegation — the two
are mutually exclusive, and declaring both is refused when the session starts rather than
resolved silently in favor of one.

Delegated acquisition is an external-protocol mode. Drive it with
`evidence-wiki orchestrate start/next/submit`; the package-managed `orchestrate run` and
`resume` runners refuse a delegated workspace, because a delegated acquisition order is
addressed to the host's own connectors and no managed worker can execute it.

`scripts/doctor.py` reports the posture under its `acquisition_mode` check — the mode, the
declared acquirer, and the attempts budget — so an auditor can see who acquires for this
workspace without opening `research.yml`. `scripts/workspace_status.py` carries
`acquisition_mode` and `acquirer_agent_id` on its parent-session summary, and the MCP
`workspace_status` tool passes them through. Lint's delegation-specific findings are listed
under `validate_source_requests` below.

An invalid `orchestration` value is not silently replaced by a default: scripts reject the
workspace config. Defaulting to `providers` would leave a workspace that believes it
delegates unable to issue any acquisition order at all, and defaulting to `delegated`
would address work orders to an acquirer nobody declared — both failures are silent.
Delegated-only keys under `acquisition: providers` are refused for the same reason: they
state an intent the workspace will not act on. Omit a key to accept its default, or use an
`x-` prefix for experimental keys.

### `domain_pack`

Optional. A domain pack's own configuration, merged into a workspace's `research.yml` from
the pack overlay (`research.overlay.yml`) at `evidence-wiki init` through the generic
deep-merge — declaring `request_kinds` needs no extra wiring beyond that merge. This section
documents `request_kinds` and `policy_rules`; `policy_vocabularies` and `coverage_templates`
are documented in [evidence-policies.md](evidence-policies.md) and
[coverage-manifest.md](coverage-manifest.md).

`request_kinds` declares source-request kinds specific to the pack's domain, namespaced
exactly like pack evidence policies so one pack can never define kinds in another's
namespace:

```yaml
domain_pack:
  name: market-data
  request_kinds:
    - id: pack:market-data/supplier_quote
      label: Supplier quote
      description: Live SKU price + shipping + MOQ from a named supplier, ≤ 48h old.
```

- A list of mappings, each requiring non-empty `id`, `label`, and `description` strings.
- `id` must be namespaced `pack:<pack-name>/<kind-id>` — the same convention pack evidence
  policies use — and the pack-name segment must equal this pack's own `domain_pack.name`. A
  pack cannot declare kinds in another pack's namespace.
- The built-in kinds (`paper`, `dataset`, `web`, `code`, `structured_data`, `other`) are
  reserved and cannot be redeclared.
- Declarations are validated in two places that read the same definition and can never
  disagree: `evidence-wiki pack validate` checks a pack's `request_kinds` before it ships,
  and `scripts/source_requests.py add --kind` validates against the merged workspace config
  at request time.

A workspace's valid kind set is always built-ins plus whatever its active pack declares. An
operator opening a request against an undeclared or malformed kind sees one of two stable
error codes:

- `REQUEST_KIND_INVALID`: the kind id is malformed, or a pack kind was written without its
  reserved `pack:` prefix. Writing the bare `<pack-name>/<kind-id>` form is a common first
  mistake, so when that bare form matches a declared kind, the message names the exact
  prefixed id to use instead.
- `REQUEST_KIND_UNDECLARED`: the id is well-formed and namespaced correctly, but this
  workspace's active pack does not declare it — declare it under `request_kinds`, or use a
  built-in kind.

See [source-delivery.md](source-delivery.md) for how a request's `kind` is recorded and
carried through fulfilment.

`policy_rules` declares how this pack's own evidence policies are decided, so that a
namespaced policy no longer falls to `manual_review` merely because the pack had no
vocabulary in which to express the check:

```yaml
domain_pack:
  name: market-data
  policy_vocabularies:
    freshness_policy:
      pack:market-data/quote-48h: A supplier quote must be at most 48 hours old.
  policy_rules:
    pack:market-data/quote-48h:
      all_of:
        - max_age: {field: provenance/retrieved_at, hours: 48}
```

- A mapping of `pack:<pack-name>/<policy-id>` to one rule declaration. The pack-name
  segment must equal this pack's own `domain_pack.name`, exactly as for `request_kinds`,
  so one pack can never decide another's policies.
- Every key must already appear under `domain_pack.policy_vocabularies.source_policy`,
  `.freshness_policy`, or `.identity_policy`. `evidence_paths` entries carry no rules: an
  evidence path names which facet must be covered, and the coverage manifest resolves that
  structurally before any policy runs.
- A rule declares exactly one of `all_of` or `any_of` — each a non-empty list of
  primitives, nesting at most three deep — plus an optional `manual_review_required`
  boolean defaulting to `false`. Any other key is refused rather than ignored.
- The primitives are `max_age`, `equals`, `numeric_range`, `regex`, and
  `one_of_provenance`. A pack *declares* them and never ships code that runs; there is no
  expression language and no callable. See [evidence-policies.md](evidence-policies.md)
  for each primitive's arguments, the field-reference syntax, and what each outcome means
  for a facet verdict.
- `max_age`, `equals`, `numeric_range`, and `regex` may declare
  `when_absent: fail | manual_review` only for their primary `record/...` field.
  Omission defaults to fail. Manual review applies only to a missing terminal member
  under fully resolved mapping parents in a valid hash-bound structured view; missing
  parents, arrays, corrupt evidence, provenance, and question operands remain hard
  failures. Null and blank are present, never conditional absence, and retain each
  primitive's ordinary comparison/parsing semantics. A required facet using a rule that
  can take this path requires `domain_pack.human_gated: true`.
- Declarations are validated in one module that both consumers read, so they can never
  disagree about what a pack declared: `evidence-wiki pack validate` reports a
  `policy_rules` check before the pack ships, and evaluation refuses a malformed block at
  answer time with `CONFIG_INVALID` rather than quietly treating the pack as declaring no
  rules at all.

### `lint`

Defines validation behavior for future lint tooling.

- `validate_structure`: check configured directories.
- `validate_frontmatter`: check required fields and allowed types.
- `validate_links`: check internal Markdown links.
- `validate_source_coverage`: compare manifest, normalized records, and notes. A manifest source with no normalized record emits LOW `source_missing_normalized`. A record that names a producing tool other than this package's is additionally held to the published record contract: it is accepted exactly as a native record when it conforms, and emits MEDIUM `normalized_record_contract_violation` naming the first breach when it does not. MEDIUM rather than HIGH, so one malformed record cannot freeze orchestration across the workspace; the gates that decide whether a record can support an answer fail closed independently. A record that names no producing tool is left alone — it predates the contract rather than claims it — while `normalize_verify.py` checks every record on demand. See [normalized-source-format.md](normalized-source-format.md).
  This pass also reports a structured-view sidecar with no normalized record beside it as
  LOW `normalized_orphan` — the same finding key as an orphaned record, because both name
  a file in `sources/normalized/` that nothing will ever read. The `normalized_orphans`
  stat still counts orphaned **records** only, so a host tracking that number sees no
  change from sidecars; read the findings, not the counter, if you need both.
- `validate_claims`: validate structured claim pages or embedded claims.
- `validate_provenance`: require license provenance on automated deliveries (manifest records whose `provenance.retrieved_by` is set; MEDIUM `provenance_missing_license`).
- `validate_source_requests`: check the source-request artifact — blocked questions should reference an open or fulfilled request (LOW `question_blocked_no_request`) and fulfilled requests must point at existing manifest sources (MEDIUM `request_fulfilled_missing_source`); malformed request lines are reported (MEDIUM `source_request_invalid`). Under `orchestration.acquisition: delegated` this pass also reports a fulfilment no delegated acquisition work order ever scoped (LOW `delegated_fulfilment_unattributed`), an attempt-audit event naming a request the store no longer has (LOW `source_request_attempt_orphaned`), an unreadable attempt audit (MEDIUM `source_request_attempt_audit_invalid`), and an attempt audit approaching the controller's bounded read guard, after which delegated acquisition stops verifying (LOW `source_request_attempt_audit_large`). All four are delegation-specific: a workspace acquiring through its own providers never sees them.
- `validate_output_license_status`: require reusable output pages under `outputs.default_dir` to cite fetched sources with concrete license metadata. If an output page cites a manifest source whose `provenance.retrieved_by` is set and whose `provenance.license` is missing, null, or empty, lint reports LOW `output_license_missing`.
- `validate_questions`: validate question task records, including answered/blocked consistency, coverage manifests, and claim hygiene — answered questions with `coverage_required: true` but missing, blocked, or invalid coverage emit HIGH `question_coverage_missing`, `question_coverage_blocked`, or `question_coverage_invalid`; `in_progress` questions without `claimed_by`/`claimed_at` emit MEDIUM `question_claim_missing`, and claims older than `run.claim_staleness_hours` emit LOW `question_claim_stale`. Questions in `human_review` emit MEDIUM `question_human_review_pending`; under `review.escalation_scope: question` they additionally emit HIGH `question_human_review_stale` once `human_review_requested_at` is older than `review.max_pending_review_hours`, or MEDIUM `question_human_review_undated` when that timestamp is missing or unparseable. This pass also counts grounding by evidence form across every answered question — `grounding_entries_quote` and `grounding_entries_anchor` under `stats`, and a `- grounding: quote=N anchor=M` line in the `log.md` entry — so a workspace can measure its own migration from quoted prose to structured anchors. The counters are unconditional and need no configuration; they read zero in a workspace with no grounding at all.
- `detect_prompt_injection_patterns`: default-on weak reviewer-awareness heuristic. When enabled, lint scans normalized Markdown records, question pages, and parsed manifest `provenance.notes` values for instruction-like phrases, structural prompt-injection shapes, and large base64-like blobs after Unicode/zero-width normalization. It reports LOW `source_prompt_injection_pattern` findings and never reads raw files, opens provenance sidecars, or fetches provenance URLs.
- `dataview_aware`: account for Dataview-generated index sections.
- `min_rendered_coverage_ratio`: optional number in `[0, 1]`. Unset by default, which disables the check. When set, any normalized record whose `rendered_coverage.ratio` falls below it emits LOW `normalized_low_rendered_coverage`, and the count appears as the `normalized_low_rendered_coverage` stat. This is a visibility control, not a gate: capping a long series is a legitimate rendering choice, and the un-rendered part stays citable by containment even though it cannot be quoted. Records that declare no `rendered_coverage` block are never reported — declaring nothing is not declaring zero. A value that is not a number in range disables the check rather than failing the run. See [normalized-source-format.md](normalized-source-format.md).
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
- `acquisition.providers`: enabled provider IDs. Built-in IDs are `arxiv`, `openalex`, `github`, and `web`. The list also accepts the ID of any acquisition provider supplied by an installed third-party distribution (see [provider-registration.md](provider-registration.md)); listing an ID here is what *enables* it, exactly as for a built-in.
- `acquisition.target_root`: raw evidence directory for downloaded or delivered papers. Defaults to `raw/papers`.
- `acquisition.max_downloads_per_run`: positive per-run download budget.
- `acquisition.require_license_check`: whether acquisition workflows must surface license status before handoff.
- `acquisition.github.target_root` (optional): raw evidence directory for GitHub downloads. Must stay under `raw/`. Defaults to `raw/code`. Add it to `raw.source_roots` so inventory records captured archives.
- `acquisition.github.max_archive_bytes` (optional): positive byte ceiling for GitHub source archives. Defaults to 104857600 (100 MiB).
- `acquisition.web.target_root` (optional): raw evidence directory for contracted web downloads. Must stay under `raw/`. Defaults to `raw/web`.
- `acquisition.web.allowed_domains`: required non-empty domain allow-list for `web get` when the `web` provider is enabled.
- `acquisition.web.max_download_bytes` (optional): positive byte ceiling for one web response. Defaults to 10485760 (10 MiB).
- `discovery.enabled`: whether optional source discovery is active. `true` requires a non-empty concrete provider allow-list.
- `discovery.providers`: explicit network authorization. Built-in IDs are `arxiv`, `openalex`, `github`, `search`, `standards`, `standards:iso-open-data`, `standards:eu-product-requirements`, `standards:uk-geospatial-register`, and `standards:nist`. The list also accepts the ID of any discovery provider supplied by an installed third-party distribution (see [provider-registration.md](provider-registration.md)).
- `discovery.candidate_store_path`: workspace-relative JSONL path for proposed source candidates. Defaults to `sources/discovery/candidates.jsonl`.
- `discovery.search`: required backend configuration when `search` is allowed. Its `provider` is `fixture`, `command`, or `http`; provider-specific command, fixture, endpoint, and environment-secret settings stay in this nested block.
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
domain-pack `recommended_discovery` values are advisory and do not enable
discovery. `legal`, `authors`, and `companions` are strategies rather than
network permissions: legal execution requires `search`, publication expansion
requires `openalex`, and companion network phases require `github` and/or
`search`. Those three old strategy IDs remain readable for one compatibility
release with LOW deprecation findings, but never authorize transport.

Legacy discovery migration is manual because `evidence-wiki upgrade` preserves
the existing `research.yml`:

- replace `legal` with `search`, and configure `integrations.discovery.search`;
- replace `authors` with `openalex`;
- replace `companions` with `github`, `search`, or both, configuring search when
  selected.

Keep `enabled: true` only when at least one concrete provider remains, then run
`python3 scripts/smoke_validate_workspace.py --format json`. A legacy strategy
may still be read for compatibility, but it does not satisfy the non-empty
provider gate and cannot authorize network I/O.

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

A provider ID supplied by an installed distribution is authorized by the same
list and validated by the same rules — the authorization semantics do not change
because the provider is third-party. Registration is packaging metadata and
decides only which IDs *exist*; `research.yml` remains the sole statement of
which of them may run. The two states are checked separately: an installed but
unlisted provider is available and refuses with `ACQUISITION_PROVIDER_DISABLED`,
while an ID listed here whose distribution is not installed in the running
environment is deploy drift on an authorization boundary and fails smoke
validation with `PROVIDER_NOT_REGISTERED`. Neither state can be declared away in
config: a provider cannot be brought into existence by writing it into
`research.yml`. `scripts/doctor.py` prints both states per provider, and
[provider-registration.md](provider-registration.md) documents the contract and
its failure modes in full.

When discovery is enabled, its provider list must also be non-empty. Unknown or
duplicate IDs are invalid. The repeated initializer flags
`--discovery-provider` and `--acquisition-provider` each replace that phase's
profile allow-list and set only that phase to `enabled: true`; one phase never
implicitly enables the other. The configured candidate-store path is shared by
discovery, candidate review, source-request planning, workspace status, and
orchestration.

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

## Machine Output

Every script configured by this file follows one output contract: under
`--format json`, stdout carries exactly one JSON document, diagnostics go to stderr,
and a fatal error — including an invalid section in this file — is the shared error
envelope on stderr with stdout left empty. See "Machine Output On stdout" in
[orchestrator-handoff.md](orchestrator-handoff.md).

## Extension Rules

- Keep the top-level section names stable.
- Add domain-specific values by extending lists or adding nested keys.
- Do not rename configured directories without also updating the filesystem.
- Core profile mappings reject unknown fields. Experimental keys in strict core
  mappings use an explicit `x-` prefix; namespaced provider and domain-pack
  mappings remain additive and are validated by their owning component.
