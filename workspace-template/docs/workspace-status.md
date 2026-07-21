# Workspace Status

`scripts/workspace_status.py` is the single aggregate status surface for a research workspace. One invocation combines smoke validation, the question backlog, source pipeline coverage, and lint health into one versioned document, plus a machine-checkable readiness verdict. Orchestrators and parent agents should poll this script instead of invoking and merging the individual check scripts.

The script is read-only by default. `--append-log` optionally appends one summary entry to `log.md`.

## Usage

```bash
python3 scripts/workspace_status.py --format text
python3 scripts/workspace_status.py --format json
python3 scripts/workspace_status.py --format json --run-id run-2026-06-29T010203Z
python3 scripts/workspace_status.py --check-complete --format json
python3 scripts/workspace_status.py --check-complete --format json \
  --questions-processed-this-run 3 \
  --source-requests-opened-this-run 1 \
  --releases-this-run 2 \
  --discovery-results-this-run 8 \
  --acquisition-downloads-this-run 1 \
  --github-archive-bytes-this-run 2048 \
  --academic-provider-requests-this-run 4 \
  --web-downloads-this-run 1 \
  --manual-url-deliveries-this-run 1
python3 scripts/workspace_status.py --format text --append-log
```

## Output Schema

The JSON document has a fixed top-level shape. The current `schema_version` is `"1.0"`.

| Field | Type | Meaning |
|-------|------|---------|
| `schema_version` | string | Status document schema version. Bumped on breaking shape changes. |
| `generated_at` | string | UTC timestamp (`YYYY-MM-DDTHH:MM:SSZ`) of report generation. |
| `project` | mapping | Project identity from `research.yml`. |
| `contract` | mapping | Starter and contract versions from `workspace-system.yml`. |
| `run` | mapping | Per-run budgets for unattended loops. |
| `run_controller` | mapping | PM run-controller summary when `runs/<run_id>/run-state.json` exists or `--run-id` is supplied. |
| `orchestration` | mapping | Read-only summary of the newest active parent orchestration, or newest terminal parent when none is active. |
| `smoke` | mapping | Smoke validation summary. |
| `questions` | mapping | Question backlog counts. |
| `intake` | mapping | Question-intake cap and recent-rate signals. |
| `candidates` | mapping | Candidate lifecycle counts from `sources/discovery/candidates.jsonl`. |
| `sources` | mapping | Source pipeline coverage. |
| `lint` | mapping | Lint issue counts by severity. |
| `readiness` | mapping | Verdict plus reasons. |
| `workspace_health` | mapping | Shared health status, material-validity flags, stable finding codes, affected artifacts, and remediation. |

## Shared Workspace Health

Doctor, smoke, status, lint, and publication readiness call the same
dependency-light evaluator before applying their surface-specific checks. Its
states are:

- `healthy`: the required contract is present and no shared finding applies;
- `degraded`: the workspace remains usable, but an optional capability such as
  `pdftotext` is unavailable;
- `invalid`: the workspace root or core YAML contract cannot be read; and
- `publication_blocked`: required starter content is incomplete, so local
  diagnostics may continue but publication must not.

Every finding contains `code`, `severity`, `artifacts`, `remediation`, and
`readiness_effect`. Text output prints the same stable codes exposed by JSON.
Required failures take precedence over optional degradation.

### `project`

| Field | Type | Meaning |
|-------|------|---------|
| `name`, `description`, `owner_goal`, `language` | string or null | Values from `research.yml` `project`. |
| `handoff` | mapping or null | Optional upstream correlation block (`task_id`, `requested_by`, `chain_run_id`) persisted from the setup profile. See [orchestrator-handoff.md](orchestrator-handoff.md). |
| `handoff_signature` | string or null | Optional `hmac-sha256:<hex>` signature stored beside `project.handoff`; never contains the secret. |
| `handoff_signature_status` | string | `verified`, `invalid`, `unsigned`, or `unconfigured`, based on the current `EVIDENCE_WIKI_HANDOFF_SECRET` or `.research-handoff-secret` sidecar. Status reporting is non-fatal; strict refusal happens in `export_answers.py`. |

### `contract`

| Field | Type | Meaning |
|-------|------|---------|
| `starter_version` | string or null | Starter version recorded in `workspace-system.yml`. |
| `schema_version` | string or null | Workspace metadata schema version. |
| `compatible_research_yml_contract` | string or null | `research.yml` contract version this workspace was created against. |

### `run`

Budget values from the `research.yml` `run` block; absent or invalid values fall back to the documented defaults so loop implementations can always read them here. Wall-clock and token budgets belong to the orchestrator, not the workspace.

| Field | Type | Meaning |
|-------|------|---------|
| `max_questions_per_run` | integer | Maximum questions one unattended run should resolve (default 25). |
| `max_source_requests_per_run` | integer | Maximum source requests one run should open (default 10). |
| `max_releases_per_run` | integer | Maximum successful claim releases one run should perform before stopping (default 3 x `max_questions_per_run`; template default 75). |
| `max_discovery_results_per_run` | integer | Maximum discovery candidate/result records one run should propose (default 50). |
| `max_academic_provider_requests_per_run` | integer | Maximum OpenAlex/arXiv provider requests one run should make (default 25). |
| `max_web_downloads_per_run` | integer | Maximum contracted `web get` downloads one run should count; defaults to `max_manual_url_deliveries_per_run` when unset. |
| `max_manual_url_deliveries_per_run` | integer | Maximum manual URL/file deliveries one run should count (default 10). |
| `max_acquisition_downloads_per_run` | integer | Maximum provider-backed downloads one run should count, derived from `integrations.acquisition.max_downloads_per_run` (default 10). |
| `max_github_archive_bytes_per_run` | integer | Maximum GitHub archive bytes one run should count, derived from `integrations.acquisition.github.max_archive_bytes` (default 104857600). |
| `max_open_questions_total` | integer | Maximum currently `open` questions allowed after one intake batch (default 250). |
| `max_intake_per_hour` | integer | Maximum newly created questions accepted through intake in one rolling hour (default 25). |
| `max_mcp_intake_batch_questions` | integer | Maximum `questions[]` items accepted in one MCP `intake_questions` call (default 100). |
| `claim_staleness_hours` | integer | Age after which an `in_progress` claim is reported stale by lint (default 24). |
| `stale_run_threshold_hours` | integer | Age after which an active run with no heartbeat or event is reported stale (default 4). |

### `run_controller`

`run_controller` is additive to the readiness verdict. It reports durable PM
run state from `runs/<run_id>/run-state.json` but does not change
`readiness.verdict` or `--check-complete` exit codes. `no_ship` and `failed`
are controller states, not workspace readiness verdicts.

When `--run-id` is supplied, status reads that exact run and reports
`selection: "explicit"`. Without `--run-id`, status selects the newest
non-terminal run by `updated_at`; if no active run exists, it selects the
newest terminal run. If no run-state files exist, the block is:

```json
{"present": false, "selection": "none"}
```

Malformed discovered run-state files are fatal and return the stable
`RUN_STATE_INVALID` JSON error envelope instead of being skipped.

| Field | Type | Meaning |
|-------|------|---------|
| `present` | boolean | `true` when a run snapshot was selected. |
| `selection` | string | `none`, `explicit`, `newest_active`, or `newest_terminal`. |
| `run_id` | string | Selected run id. Present only when `present` is true. |
| `started_at` | string | Run creation timestamp. |
| `state` | string | Current run-controller state, including terminal states such as `blocked_on_sources`, `no_ship`, and `failed`. |
| `terminal` | boolean | `true` when the run state is terminal. |
| `final_verdict` | string or null | Terminal verdict from the controller, or null while active. |
| `blocking_reason` | string or null | Durable terminal/blocking reason from the run state. |
| `updated_at` | string | Last run-state update timestamp. |
| `last_heartbeat_at` | string or null | Last explicit run heartbeat. |
| `last_event_at` | string or null | Latest timestamp in `runs/<run_id>/events.jsonl`. |
| `liveness_at` | string or null | Latest heartbeat, event, or update timestamp used for stale-run detection. |
| `stale_threshold_hours` | integer | Threshold applied to this run. |
| `stale_age_hours` | number or null | Age of `liveness_at` in hours. |
| `stale` | boolean | `true` only for non-terminal runs older than the stale threshold. |
| `allowed_next_states` | list | Allowed next controller states for the selected run. |
| `candidate_counts`, `coverage_counts`, `budget_state`, `budget_overrides` | mapping | Snapshot counters and explicit budget overrides copied from `run-state.json`. |
| `failure_count` | integer | Count of recorded failure records. |
| `run_state_path` | string | Workspace-relative path to the selected run-state file. |

### `orchestration`

`orchestration` is an additive, read-only view of durable parent state under
`runs/orchestrations/`. Status selects the newest non-terminal session by
`updated_at`, otherwise the newest terminal session. It never issues, leases, or
submits work. With no readable session it returns:

```json
{"present": false, "selection": "none"}
```

When present, the block includes `orchestration_id`, `status`, `phase`,
`terminal`, `verdict`, `pause_reason`, `pending_action_id`,
`pending_submission_action_id`, bounded `recovery` metadata, `active_run_id`,
`child_run_ids`, action counters, lifecycle timestamps, and the
workspace-relative `session_path`. It also reports an `attempts` summary with a
hard limit of 1,000 inspected directory entries and 64 KiB per record. The
summary contains the valid `orchestration_attempt` version 1.0 record count,
invalid-record count, truncation flag, and only the latest valid attempt's safe
identifiers, runner, phase, status, timestamps, and error code. It never exposes
a result digest, work-order fingerprint, prompt, transcript, diagnostic,
secret, or absolute path. Use
`evidence-wiki orchestrate status` for the complete version 1.0 session
artifact.

`control_repair` reports whether the durable managed-host marker at
`runs/orchestration-guards/<orchestration_id>.json` is present,
required, acknowledged, or invalid, plus its bounded reason, timestamps, and
attempt IDs. It never exposes the retained
`expected_control_fingerprint` for the tripwire-protected controls. The guard is
outside the parent-session tree and is a preventive-only read-only root. A
required marker makes the managed host stop with `CONTROL_REPAIR_REQUIRED` and
makes controller `next` and `submit` stop with
`ORCHESTRATION_CONTROL_REPAIR_REQUIRED`; an acknowledgement whose
tripwire-protected controls do not match fails with
`CONTROL_REPAIR_MISMATCH`. An invalid marker is also fail-closed even if the
parent session itself still says `active`.

The tripwire-protected controls are the bounded workspace contract and
instructions, `scripts/`, `skills/`, `docs/`, and current parent-session tree.
The sandbox separately keeps `.git/`, `.codex/`, `.claude/`, `.agents/`,
`.venv/`, `venv/`, and `runs/orchestration-guards/` preventive-only and
read-only; their contents are not recursively hashed into the semantic
tripwire. If a retained `control_tampered` attempt exists without its durable
guard baseline, managed acknowledgement fails with
`CONTROL_REPAIR_BASELINE_MISSING`; status does not invent a replacement
fingerprint.

Attempt status is durable metadata, not proof that a process is still alive.
When a fresh worker is needed, the host caps its timeout to the work order's
remaining absolute lease. It reports `ORCHESTRATION_LEASE_INVALID` for a
malformed expiry, `ORCHESTRATION_LEASE_EXPIRED` when no lease time remains, and
`ORCHESTRATION_LEASE_ACTIVE` when a retained `running` attempt still owns the
same lease attempt. Wait for expiry and resume the same action so the controller
can increment the lease attempt. A managed work order must never leave a
daemon, background job, or detached process; status cannot establish process
containment, so untrusted process trees require an operator-controlled
container or equivalent lifecycle boundary.

`workspace_status.py` only observes these artifacts. One managed host at a time
may drive a session; a second returns `ORCHESTRATION_ALREADY_RUNNING`. External
protocol hosts must coordinate a single writer and not interleave `next` or
`submit` with an active managed host. Status polling itself remains read-only.

### `smoke`

| Field | Type | Meaning |
|-------|------|---------|
| `ok` | boolean | Smoke validation passed. |
| `issues` | integer | Total smoke issues. |
| `by_severity` | mapping | Issue counts keyed by severity. |
| `error` | string or null | Set when smoke validation could not run at all. |

### `questions`

Counts reuse the collection logic of `scripts/question_status.py`.

| Field | Type | Meaning |
|-------|------|---------|
| `total` | integer | All question task records. |
| `by_status` | mapping | Counts keyed by lifecycle status. |
| `by_priority` | mapping | Counts keyed by priority. |
| `actionable` | integer | `open` plus `in_progress` questions. |
| `blocked` | integer | `blocked` questions. |
| `answered` | integer | `answered` questions. |
| `claimed` | integer | `in_progress` questions with a non-empty `claimed_by` holder. |
| `actionable_slugs` | list | Slugs of actionable questions. |
| `blocked_slugs` | list | Slugs of blocked questions. |
| `claimed_slugs` | list | Slugs of currently claimed `in_progress` questions. |
| `stale_claim_slugs` | list | Claimed question slugs whose `claimed_at` is missing, unparseable, or older than `run.claim_staleness_hours`. |
| `blocked_questions_with_requests` | integer | Blocked questions whose `blocking_request_ids` all resolve to open source requests linked to the same question slug. |
| `blocked_questions_missing_requests` | integer | Blocked questions with no `blocking_request_ids`, missing request IDs, non-open requests, or request records not linked to the question slug. |
| `blocked_slugs_missing_requests` | list | Blocked question slugs that need request-link repair before `blocked_on_sources` is clean. |
| `missing_blocking_request_ids` | list | `blocking_request_ids` values that are absent from `sources/source-requests.jsonl`. |
| `blocked_request_link_errors` | list | Per-link diagnostics with `slug`, `request_id`, and `problem` (`empty`, `missing`, `not_open`, `not_linked`, or `request_artifact_unreadable`). |
| `blocked_open_request_ids` | list | Valid linked open request IDs that explain clean blocked questions. |

The aggregate claim fields are derived from the same question records as
`question_status.py --format json`. They let an orchestrator identify active
and stale claims from one status poll without parsing individual Markdown pages.
Malformed `in_progress` records without `claimed_by` are not counted as active
claims; lint still reports those as claim hygiene issues.

### `coverage`

Coverage counts summarize every manifest file under `sources.coverage_dir`, so a
gate-blocked manifest is visible even when `question_resolve.py answer
--require-coverage` refused before `coverage_required: true` could be written.
The `coverage_verdicts` map names each evaluated manifest by slug. The
`required_question_counts` nested block preserves the answered/human-review
question view for records that already carry `coverage_required: true`.

| Field | Type | Meaning |
|-------|------|---------|
| `manifests_total` | integer | Count of present `*.yml` coverage manifests. |
| `required_questions` | integer | Answered or human-review question records with `coverage_required: true`. |
| `passed` | integer | Coverage manifests that evaluate to `coverage_verdict: pass`. |
| `blocked` | integer | Coverage manifests that evaluate to `coverage_verdict: blocked`. |
| `pending` | integer | Coverage manifests that evaluate to `coverage_verdict: pending`. |
| `missing` | integer | Required answered questions whose selected manifest is absent. |
| `invalid` | integer | Coverage manifests that are unsafe, malformed, for another slug, or schema-invalid. |
| `coverage_verdicts` | object | Per-manifest verdicts keyed by coverage manifest slug. |
| `required_question_counts` | object | Compatibility counters for answered or human-review questions with `coverage_required: true`: `total`, `passed`, `blocked`, `pending`, `missing`, and `invalid`. |

### `intake`

Recent intake counts are derived from timestamped `log.md` entries written by
`scripts/intake_questions.py`; legacy intake entries without `Created at` are
ignored for the rolling-hour window.

| Field | Type | Meaning |
|-------|------|---------|
| `open_questions_total` | integer | Count of question records with status exactly `open`. |
| `batches_last_hour` | integer | Timestamped intake batches in the last rolling hour. |
| `questions_created_last_hour` | integer | Created question pages from timestamped intake batches in the last rolling hour. |
| `window_seconds` | integer | Rolling window size; currently `3600`. |
| `last_intake_at` | string or null | Most recent timestamped intake batch time. |

### `candidates`

Candidate counts are derived from `sources/discovery/candidates.jsonl`, using
the same default candidate policy fields and lifecycle status rules as
`scripts/discover_sources.py candidates list`. This section is additive
observability for candidate review and acquisition planning. It does not change
`readiness.verdict`, `--check-complete` exit codes, or the controller snapshot
under `run_controller.candidate_counts`.

Malformed JSONL lines and non-object records are skipped and counted in
`invalid_records`; valid candidate records are still reported. `fetched` means
the candidate lifecycle record explicitly has `status: fetched`. Status does
not infer fetched candidates from raw files, fulfilled source requests, or the
source manifest.

| Field | Type | Meaning |
|-------|------|---------|
| `store_exists` | boolean | `true` when the durable candidate store exists. |
| `candidates_path` | string | Workspace-relative path to the candidate store. |
| `total` | integer | Valid candidate records counted. |
| `invalid_records` | integer | Candidate-store lines skipped because they are malformed JSON or not objects. |
| `by_status` | mapping | Counts for `new`, `selected`, `rejected`, and `fetched`; missing or unknown status is treated as `new`. |
| `by_selection_status` | mapping | Counts keyed by OEH `selection_status` (`pending`, `selected`, `rejected`, `duplicate`, `obsolete`, `needs_manual_review`). |
| `by_evidence_path` | mapping | Counts keyed by candidate `evidence_path`, after legacy records receive the default policy for their `source_type`. |
| `by_trust_tier` | mapping | Counts keyed by candidate `trust_tier`, or `unknown` when absent. |
| `by_recommended_action` | mapping | Counts keyed by discovery `recommended_action`, or `unknown` when absent. |
| `by_fetch_status` | mapping | Counts keyed by OEH `fetch_status` (`not_planned`, `pending_manual_delivery`, `planned`, `fetched`, `failed`, `not_fetchable`). |
| `by_fetched_status` | mapping | `fetched` versus `not_fetched` lifecycle counts. |
| `official_candidates`, `aggregator_candidates`, `linked_to_source_requests` | integer | Official-source evaluation counts for proving official preference, aggregator rejection, and durable source-request linkage. |
| `selection` | mapping | Selected-candidate counts, split into `selected_with_request` and `selected_without_request`; legacy `selected_request_id` is accepted. |
| `rejections` | mapping | Rejected-candidate totals, reason presence counts, and `by_reason` counts. |
| `error` | string or null | Set when the candidate store exists but could not be read. |

### `sources`

| Field | Type | Meaning |
|-------|------|---------|
| `manifest_exists` | boolean | `sources/manifest.jsonl` (or configured path) is present. |
| `manifest_records` | integer | Valid manifest records. |
| `invalid_records` | integer | Unparseable manifest lines. |
| `by_status` | mapping | Record counts keyed by lifecycle status. |
| `unnormalized` | integer | Normalizable sources without a normalized record yet (same signal `query_index.py` reports). |
| `needs_ocr` | integer | Normalized records flagged `needs_ocr: true` (likely scanned/image-only PDFs awaiting external OCR). |
| `requests_open` | integer | Open source requests in the source-request artifact (`scripts/source_requests.py`). |
| `requests_open_ids` | list | Sorted request ids of open source requests. |

### `lint`

| Field | Type | Meaning |
|-------|------|---------|
| `issue_counts` | mapping | Lint issue counts keyed by severity level. |
| `pages_checked` | integer | Wiki pages checked. |
| `error` | string or null | Set when lint checks could not run at all. |

### `readiness`

| Field | Type | Meaning |
|-------|------|---------|
| `verdict` | string | One of `complete`, `in_progress`, `blocked_on_sources`, `attention_required`. |
| `reasons` | list | Human-readable reasons naming the exact questions or checks behind the verdict. |
| `verdict_reasons` | list | Machine-readable reason objects with stable `code` values such as `blocked_on_linked_source_requests`, `blocked_request_link_missing`, and `actionable_questions_remaining`. |
| `budget_state` | mapping, optional | Present when a runner supplies at least one per-run counter flag or a run-controller snapshot is selected; gives used/remaining run budget, stop signal, and machine-readable stop reasons. |

`readiness.budget_state` is additive to the verdict. It never changes verdict
rules or `--check-complete` exit codes; it only tells a loop whether the
current run has exhausted one of its configured budgets. With `--run-id` or an
auto-selected run, counters are derived from workspace artifacts inside the run
window: terminal question page mtimes, source-request `created_at`, discovery
candidate timestamps, manifest provenance, and claim-release log entries. Legacy
counter flags are retained under `runner_reported`; disagreements are listed in
`counter_divergence`, while `should_stop` uses the artifact-derived values.
Stable request, candidate, and source identities are counted once even if a
worker restarts and replays an identical JSONL record. Persisted timestamps are
compared as UTC instants, so local display offsets and daylight-saving changes
cannot reset, inflate, or reorder a run budget.
Without a selected run, supplied counter flags remain the source of truth and
missing counterparts are treated as zero.

| Field | Type | Meaning |
|-------|------|---------|
| `questions_processed_this_run` | integer | Questions terminally processed by the current runner. |
| `questions_remaining_this_run` | integer | `max(0, run.max_questions_per_run - questions_processed_this_run)`. |
| `source_requests_opened_this_run` | integer | Source requests opened by the current runner. |
| `source_requests_remaining_this_run` | integer | `max(0, run.max_source_requests_per_run - source_requests_opened_this_run)`. |
| `releases_this_run` | integer | Successful claim releases reported by the runner for the current run. |
| `releases_remaining_this_run` | integer | `max(0, run.max_releases_per_run - releases_this_run)`. |
| `discovery_results_this_run`, `discovery_results_remaining_this_run` | integer | Discovery candidates/results used and remaining for the current run. |
| `acquisition_downloads_this_run`, `acquisition_downloads_remaining_this_run` | integer | Provider-backed downloads used and remaining for the current run. |
| `github_archive_bytes_this_run`, `github_archive_bytes_remaining_this_run` | integer | GitHub archive bytes used and remaining for the current run. |
| `academic_provider_requests_this_run`, `academic_provider_requests_remaining_this_run` | integer | OpenAlex/arXiv provider requests used and remaining for the current run. |
| `web_downloads_this_run`, `web_downloads_remaining_this_run` | integer | Contracted `web get` downloads used and remaining for the current run. |
| `manual_url_deliveries_this_run`, `manual_url_deliveries_remaining_this_run` | integer | Manual URL/file deliveries used and remaining for the current run. |
| `counter_source` | string, optional | `artifact_derived` when a run-controller window was used. |
| `runner_reported` | mapping, optional | Legacy CLI counters supplied by the current runner. |
| `counter_divergence` | list, optional | Per-counter differences between runner-reported and artifact-derived values. |
| `stop_reasons` | list | Stable reason codes for each exhausted budget: `questions_exhausted`, `source_requests_exhausted`, `releases_exhausted`, `discovery_results_exhausted`, `acquisition_downloads_exhausted`, `github_archive_bytes_exhausted`, `academic_provider_requests_exhausted`, `web_downloads_exhausted`, or `manual_url_deliveries_exhausted`. |
| `should_stop` | boolean | `true` when `stop_reasons` is non-empty; otherwise `false`. |

### Status Cache

`workspace_status.py` writes a generated `.research-cache/workspace-status.json`
cache keyed by workspace-relative file paths, mtimes, sizes, and invocation
options. Corrupt or mismatched cache entries are ignored and replaced. Use
`--no-cache` to force a fresh status document. `--append-log` bypasses the cache
because it mutates `log.md`.

### Fleet Status

`scripts/fleet_status.py --target PATH --target OTHER --format json` aggregates
the same status contract across local workspaces. Each target reports path,
readiness verdict, budget state when present, active-run count, stale-run count,
and the selected `run_controller` block. Unreadable targets are returned as
per-target `WORKSPACE_UNREADABLE` errors without aborting the whole fleet
report. The packaged CLI exposes the same command as:

```bash
evidence-wiki fleet-status --target workspace-a --target workspace-b --format json
```

When actionable questions include held `in_progress` claims, readiness reasons
include claim-holder details in the form of question slug, holder, and
`claimed_at`. Stale claims get a separate reason that names the affected slugs
and points orchestrators to `scripts/question_claim.py claim --steal --if-older-than
HOURS` for explicit recovery.

## Verdict Rules

The rule set is fixed for schema version 1.0 and evaluated in this order:

1. `attention_required`: smoke validation failed or could not run, lint reported HIGH issues, lint could not run, or a blocked question lacks a valid linked open source request.
2. `complete`: no actionable questions, no blocked questions, smoke passed, no HIGH lint issues. An empty backlog is reported as complete with an explanatory reason so orchestrators can distinguish "done" from "not yet started".
3. `blocked_on_sources`: no actionable questions, but blocked questions remain and every blocked question has `blocking_request_ids` linked to open source requests. Deliver the requested evidence and reopen the blocked questions to proceed.
4. `in_progress`: actionable questions remain.

## Exit Codes

| Mode | Exit code | Meaning |
|------|-----------|---------|
| default | 0 | Status document produced (any verdict). |
| default | 2 | Shared health is `invalid`, or selected/discovered run-state is malformed. The JSON/text health report is still printed. |
| `--check-complete` | 0 | Verdict is `complete`. |
| `--check-complete` | 1 | Verdict is `in_progress`. |
| `--check-complete` | 3 | Verdict is `blocked_on_sources`. |
| `--check-complete` | 2 | Shared health is `invalid`, or selected/discovered run-state is malformed. |
| `--check-complete` | 4 | Verdict is `attention_required`. |

An orchestrator polling `--check-complete` can distinguish done, still-working, needs-sources, broken, and attention-required states by exit code alone. The document is printed in every non-error case, so the same invocation also provides the detail. Budget exhaustion is not a verdict; when counters are supplied, read `readiness.budget_state.should_stop` and `readiness.budget_state.stop_reasons` from the printed document on exit `1`.

## Official-Source Regression Replay

The official-source regression fixture is the dedicated replay workspace under
`tests/fixtures/official-source-regression-workspace`. The documented smoke path is:
inventory, coverage evaluation, answer export, workspace status, publication
readiness, then run report. Use `.venv/bin/python` for each command, and expect
`workspace_status.py --check-complete --format json` to return verdict
`blocked_on_sources` for the clean fixture.
