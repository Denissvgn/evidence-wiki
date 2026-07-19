# Run Controller Artifact

The run controller artifact records one autonomous research run as workspace
data. It lets a PM agent, orchestrator, or follow-up worker inspect the current
phase, resume from the last durable checkpoint, and explain why a run stopped
without relying on chat history.

This document defines the `run_state` artifact schema version 1.0 and the
deterministic `scripts/run_controller.py` commands that update it.

## Artifact Locations

- Active run state: `runs/<run_id>/run-state.json`
- Append-only event stream: `runs/<run_id>/events.jsonl`

`run-state.json` is the current snapshot. `events.jsonl` is the audit trail:
each line is one JSON object, appended in chronological order. Future writers
may regenerate `run-state.json` from the event stream, but readers should use
the snapshot for the current phase.

The aggregate status and report tools read the snapshot directly:

- `scripts/workspace_status.py --format json --run-id <run_id>` adds a
  top-level `run_controller` block with current state, allowed next states,
  candidate counts, coverage counts, budget state, failure count, and terminal
  verdict. Without `--run-id`, status selects the newest non-terminal run, or
  the newest terminal run when no active run exists.
- `scripts/run_report.py --run-id <run_id> --format json` uses
  `workspace_baseline.run_report_baseline_path` from the run-state snapshot
  when `--baseline` is omitted, and includes run transitions plus final verdict
  in the report.

Run artifacts are workspace data and must never include provider tokens or secrets.
Store only identifiers, counters, workspace-relative paths, state names, and
human-readable non-secret reasons.

## `run_state` Schema

Top-level `run_state` fields:

| Field | Meaning |
|-------|---------|
| `schema_version` | Artifact schema version. `schema_version`: `"1.0"` |
| `run_id` | Stable identifier used in `runs/<run_id>/`. |
| `started_at` | UTC timestamp for run creation. |
| `updated_at` | UTC timestamp for the last state snapshot update. |
| `last_heartbeat_at` | UTC timestamp for the last explicit `heartbeat`, or `null` before the first heartbeat. |
| `agent_id` | PM or orchestrator actor currently owning the run. |
| `handoff` | Optional correlation block from upstream work, or `null`. |
| `state` | Current state object with `current`, `entered_at`, `allowed_next_states`, and `blocking_reason`. |
| `state_history` | Ordered transition summaries from run creation through the current state. |
| `workspace_baseline` | Run-start checkpoint references, such as status/report baseline paths and generated timestamps. |
| `question_counts` | Summary counts from the question backlog. |
| `source_counts` | Summary counts from source inventory, normalization, and source requests. |
| `candidate_counts` | Summary counts from discovery candidates. |
| `coverage_counts` | Summary counts for answerability coverage. Until coverage manifests exist, use zeroes and `unknown` for unclassified items. |
| `budget_state` | Per-run budget counters, remaining capacity, stop flag, and `stop_reasons` from the workspace status contract. Includes question/source-request/release budgets plus harmonized discovery/acquisition counters for downloads, GitHub archive bytes, OpenAlex/arXiv requests, contracted web downloads, and manual URL deliveries. |
| `budget_overrides` | Mapping of explicit supervisor-approved run-budget overrides. `manual_url_deliveries` records `previous_limit`, `new_limit`, `override_reason`, `approved_by`, `recorded_at`, and `agent_id`. |
| `failure_records` | Recoverable or terminal failure records, ordered by time. Empty list when no failures are recorded. |
| `recovery_history` | Ordered ownership recovery records from `adopt` or `abandon`, including previous owner, threshold, stale age, and reason when present. |
| `final_verdict` | `complete`, `blocked_on_sources`, `no_ship`, `failed`, or `null` while the run is active. |

The `state` object is intentionally self-contained:

```json
{
  "current": "planned",
  "entered_at": "2026-06-28T12:05:00Z",
  "allowed_next_states": ["discovering", "failed"],
  "blocking_reason": null
}
```

`blocking_reason` is `null` while the run can continue. For
`blocked_on_sources`, `no_ship`, or `failed`, it carries the durable reason a
future PM agent should read before choosing a recovery path.

Manual URL delivery and contracted web download budgets are enforced when a
runner supplies `--manual-url-deliveries-this-run` or
`--web-downloads-this-run` to `transition` or `finish`. Manual URL deliveries
are bounded by `run.max_manual_url_deliveries_per_run`; web downloads are
bounded by `run.max_web_downloads_per_run`, which inherits the manual URL limit
when unset. If either count is above its limit, the command refuses with
`BUDGET_EXCEEDED` until a supervisor records an explicit override:

```bash
python3 scripts/run_controller.py override-manual-url-budget --run-id RUN_ID --agent-id pm \
  --new-limit 18 --override-reason "Additional official captures approved." --approved-by supervisor
```

The override is written to `runs/<run_id>/run-state.json` under
`budget_overrides.manual_url_deliveries` and to `runs/<run_id>/events.jsonl` as a
`budget_override` event. The same override is surfaced in budget state as both
`manual_url_deliveries_override` and `web_downloads_override`.
`workspace_status.py --run-id RUN_ID --format json` surfaces the same object in
`run_controller.budget_overrides`.

## Liveness and Recovery

Active runs are considered stale when neither `last_heartbeat_at` nor the latest
event timestamp is within `run.stale_run_threshold_hours` (default 4 hours).
Terminal runs are never reported stale. `workspace_status.py --run-id RUN_ID
--format json` surfaces `run_controller.last_heartbeat_at`,
`last_event_at`, `liveness_at`, `stale_threshold_hours`, `stale_age_hours`, and
`stale`.

PM agents should send a heartbeat during long work:

```bash
python3 scripts/run_controller.py heartbeat --run-id RUN_ID --agent-id pm --format json
```

Recovery is explicit and threshold-gated. `adopt` transfers ownership of a
stale active run and records the previous owner; it refuses fresh runs with
`RUN_NOT_STALE` and refuses missing thresholds with
`RUN_ADOPT_THRESHOLD_REQUIRED`:

```bash
python3 scripts/run_controller.py adopt --run-id RUN_ID --agent-id pm-next --if-stale-hours 4 --format json
```

`abandon` marks a stale active run as `failed` with machine reason
`stale_run_abandoned`, appends `recovery_history`, and writes a `run_abandoned`
event:

```bash
python3 scripts/run_controller.py abandon --run-id RUN_ID --agent-id supervisor --if-stale-hours 4 --reason "No heartbeat." --format json
```

## State Machine

The controller records only the PM run phase. Domain-specific evidence policy
belongs in candidate, coverage, question, and source artifacts.

| State | Allowed next states |
|-------|---------------------|
| `initialized` | `planned`, `failed` |
| `planned` | `discovering`, `answering`, `no_ship`, `failed` |
| `discovering` | `candidates_ready`, `blocked_on_sources`, `failed` |
| `candidates_ready` | `fetch_planned`, `blocked_on_sources`, `failed` |
| `fetch_planned` | `fetching`, `blocked_on_sources`, `failed` |
| `fetching` | `evidence_ready`, `blocked_on_sources`, `failed` |
| `evidence_ready` | `answering`, `blocked_on_sources`, `failed` |
| `answering` | `verifying`, `blocked_on_sources`, `no_ship`, `failed` |
| `verifying` | `complete`, `blocked_on_sources`, `no_ship`, `failed` |
| `complete` | Terminal |
| `blocked_on_sources` | Terminal until a later run records a new `initialized` state. |
| `no_ship` | Terminal |
| `failed` | Terminal |

Terminal states do not transition inside the same run. A later attempt should
create a new `run_id` so the blocked or failed run remains auditable.

The direct `planned` → `answering` edge is intentionally narrow: it lets a
parent orchestration send an already answerable backlog to `research-run`
without pretending discovery occurred. All other forward paths remain
unchanged. A durable parent session under `runs/orchestrations/` may reference
several child runs; it never reopens a terminal child. See
[orchestration.md](orchestration.md).

`finish --final-verdict complete` has an additional fail-closed gate. While
holding the run-state lock, the controller invokes the publication-readiness
API fresh from workspace artifacts; it does not accept a caller-supplied or
cached verdict. Only `ship` permits the transition. Any open/actionable
question, required source request, coverage/citation/quote/license/currentness
or manual-review finding, failed live artifact, health blocker, or
contradictory verdict returns `RUN_COMPLETION_NOT_READY` with structured
`blocking_findings` and leaves both run state and events unchanged. A successful
completion event records the readiness verdict and evaluation time. Duplicate
finish remains refused because terminal runs are immutable. When evidence is
honestly unresolved, the legal `no_ship` transition remains available and does
not pretend the workspace is ship-ready.

Old terminal run directories can be archived with `scripts/workspace_gc.py`.
The command is dry-run by default, writes tarballs under `runs/archive/`, and
never modifies active runs, `raw/`, or `wiki/`. Apply mode revalidates terminal
state and retention age while holding the run lock before it archives anything.
Archive publication uses a unique same-directory temporary file and refuses to
overwrite an existing run archive:

```bash
python3 scripts/workspace_gc.py --older-than-days 30 --format json
python3 scripts/workspace_gc.py --older-than-days 30 --apply --format json
```

## Event Records

Each `events.jsonl` line uses schema version 1.0:

```json
{
  "schema_version": "1.0",
  "event_id": "evt-0002",
  "run_id": "run-2026-06-28T120000Z-sample",
  "occurred_at": "2026-06-28T12:05:00Z",
  "agent_id": "agent-pm",
  "event_type": "state_transition",
  "from_state": "initialized",
  "to_state": "planned",
  "message": "Initial plan accepted.",
  "data": {
    "allowed_next_states": ["discovering", "failed"]
  }
}
```

For state transitions, `to_state` must be one of the documented states and
`from_state` is either `null` for the first event or one of those states. Event
records may include additional non-secret metadata under `data`.

`run_controller.py event` validates event types against the shared vocabulary.
Use a documented type below, or use the namespaced custom escape hatch
`custom.<namespace>.<name>` for local operator notes.

| Event type | Purpose |
|------------|---------|
| `state_transition` | State machine transition written by `start`, `transition`, or `finish`. |
| `checkpoint` | Non-secret progress checkpoint from a PM or worker. |
| `delegation_failed` | Worker delegation failed and the PM must continue or stop. |
| `budget_override` | Supervisor-approved budget override. |
| `budget_divergence` | Runner-reported counters disagree with artifact-derived counters. |
| `heartbeat` | Active-run liveness marker. |
| `run_adopted` | Stale active run ownership transferred. |
| `run_abandoned` | Stale active run failed for recovery. |
| `source_request_opened`, `source_request_fulfilled` | Source request lifecycle marker. |
| `candidate_discovered`, `candidate_selected`, `candidate_rejected` | Discovery lifecycle marker. |
| `fetch_planned`, `fetch_failed`, `acquisition_completed` | Acquisition lifecycle marker. |
| `verification_failed` | Final verification found a blocking problem. |

Unknown un-namespaced event types are refused with `EVENT_TYPE_INVALID`.
