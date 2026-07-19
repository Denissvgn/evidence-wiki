# Parent Orchestration

EvidenceWiki separates one bounded research run from the longer-lived process
that may need to discover and acquire evidence before the backlog can finish.
The parent orchestration session is durable workspace data: it issues one
bounded work order at a time, verifies workspace artifacts after a worker
returns, and may reference several immutable child `run_id` records.

## Command Surface

Use the package-managed Codex or Claude runner for a complete loop:

```bash
evidence-wiki orchestrate run \
  --target /path/to/workspace \
  --runner codex \
  --model MODEL_ID \
  --agent-id pm-agent

evidence-wiki orchestrate resume \
  --target /path/to/workspace \
  --orchestration-id orch-20260720T120000Z-example \
  --runner codex \
  --agent-id pm-agent
```

`run` creates a session when no orchestration ID is supplied. `resume` requires
an existing ID and continues its pending work order. Managed runs default to at
most 12 work orders, 1,800 seconds per worker, and 7,200 seconds overall.
Reaching a bound pauses the session without declaring failure; `resume` starts
a fresh bounded window for the same durable session.

Any agent harness can drive the same model-neutral protocol:

```bash
evidence-wiki orchestrate start --target PATH --agent-id pm-agent --format json
evidence-wiki orchestrate next --target PATH --orchestration-id ORCH_ID --format json
evidence-wiki orchestrate submit --target PATH --orchestration-id ORCH_ID \
  --action-id ACTION_ID --result-file result.json --format json
evidence-wiki orchestrate status --target PATH --orchestration-id ORCH_ID --format json
```

`next` is idempotent and returns the existing pending work order. `submit`
accepts the result only for that action, validates its schema, and checks the
real question, request, candidate, source, run, verification, and export
artifacts required by the order. A worker's `completed` claim is not sufficient
by itself.

## Artifacts

One session is stored below:

```text
runs/orchestrations/<orchestration_id>/
  session.json
  events.jsonl
  work-orders/<action_id>.json
  work-results/<action_id>.json
  answers.json
```

The `orchestration_session`, `orchestration_work_order`, and
`orchestration_result` schemas are version `1.0`. Query their supported versions
with `evidence-wiki contract`.

A work order contains:

- `orchestration_id`, `action_id`, issuance time, and lease;
- a phase and workspace skill handler;
- the active child `run_id`, when the phase belongs to a bounded run;
- scoped question, source-request, and candidate IDs;
- effective discovery/acquisition provider allow-lists and remaining budgets;
- workspace-relative input artifacts and deterministic completion checks.

A result contains the matching action ID, `completed`, `blocked`, or `failed`, a
short summary, and workspace-relative artifact paths. Session artifacts must
not contain provider credentials, absolute host paths, source instructions,
chat transcripts, or unbounded runner output.

Result outcomes describe the work order, not the child run's research verdict:

- `completed` means the requested bounded action was carried out and its
  postconditions are ready for deterministic verification. A research action
  that creates structured source requests and ends its child run as
  `blocked_on_sources` must return `completed`; that is the expected handoff to
  parent discovery/acquisition.
- `blocked` means the work order itself cannot make progress. It terminates the
  parent session as `blocked_on_sources` and therefore must not be used for the
  normal research-to-source-request transition.
- `failed` means the work order or tooling failed and terminates the parent as
  `failed`.

## Routing And Run Boundaries

The controller prioritizes health failures, an existing pending action,
actionable questions, selected acquisition routes, candidate review, enabled
discovery, verification, and export in that order. It declares
`blocked_on_sources` only when open source requests remain and no configured
provider route can progress them.

`blocked_on_sources`, `no_ship`, and `failed` remain terminal child-run states.
The parent never reopens those records. For example, an initial research run
against an empty workspace can block and create a source request; the parent
then retains that run, starts another child run for discovery and acquisition,
and eventually sends the reopened question to research again.

The parent declares `complete` only after fresh publication-readiness evaluation
returns `ship`, then writes `answers.json` through the deterministic answer
exporter. `workspace_status.py` remains read-only and exposes only an additive
summary of the newest parent session.

## Provider And Runner Boundaries

An orchestration session never enables discovery or acquisition. Its effective
provider set is restricted to the explicit phase allow-lists in `research.yml`.
A domain-pack recommendation, installed runner, available API key, prompt, or
candidate URL cannot widen those permissions.

The Codex and Claude adapters launch fixed argument vectors without a shell,
request schema-constrained final output, cap time and captured output, and run
with the workspace as their working directory. They do not add dangerous
permission or sandbox bypass flags. Provider credentials may be injected into
the process environment by the operator, but are never copied into work orders,
results, logs, or error messages.

The allow-lists are application authorization enforced by the deterministic
provider scripts; they are not a host-level domain firewall. A managed worker
is therefore a trusted workspace writer and must use only the scoped skill and
provider commands in its work order. For an untrusted or prompt-injected agent,
add an operator-controlled network proxy/sandbox or drive the protocol from a
host that executes provider calls itself. Direct protocol workers must likewise
not edit `runs/orchestrations/` control artifacts.

Managed orchestration is intentionally not exposed through the current MCP
server. MCP remains a read/append-only integration surface; starting workers
and acquiring remote evidence require the package CLI and its explicit policy
checks.

## Recovery

Session writes and action submission are lock-protected. A leased action can be
recovered after its lease expires; a runner crash leaves the same action pending
for `resume`. Re-submitting an identical accepted result is idempotent, while a
different result for the same completed action is a conflict. Inspect
`events.jsonl`, the child run records, and `orchestrate status` before recovering
a stale session.

Managed runners snapshot the workspace contract, scripts, skills, and parent
control tree around every action. `CONTROL_ARTIFACT_UNSAFE` refuses a snapshot
that is unbounded or contains links/special files. `CONTROL_ARTIFACT_TAMPERED`
means a worker changed protected inputs; the host restores the parent session,
does not submit the result, and leaves the action resumable. Configuration or
script changes are deliberately left visible for operator review.
