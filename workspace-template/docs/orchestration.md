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

Managed execution is fail-closed when the runner cannot enforce the workspace
boundary required by the host. Codex managed runs require Codex CLI 0.138 or
newer so EvidenceWiki can select the named `evidence_wiki_worker` custom
permission profile with `-c default_permissions="evidence_wiki_worker"`.
Claude managed runs are not available from native Windows because Claude Code
does not currently provide the required per-path isolation there. Use macOS,
Linux, WSL2, or a container for managed Claude execution, or drive the
model-neutral protocol from an external host that owns the control artifacts.
A managed Claude preflight also requires `bubblewrap` and `socat` on
Linux/WSL2, or the built-in `sandbox-exec` and `touch` tools on macOS. Missing
primitives fail before a worker is launched.
A failed capability check returns `RUNNER_ISOLATION_UNAVAILABLE` before a worker
starts.

For every managed runner, EvidenceWiki pins the interpreter that launched the
package as `EVIDENCE_WIKI_PYTHON`; workers must use that exact value with `-B`
for workspace scripts instead of resolving `python`, `python3`, or `py` from
`PATH`. The Codex adapter disables login-shell environment rewriting, supplies
a minimal deterministic `PATH`, and adds read-only permission rules for only
the selected virtual environment and its external Python runtime. Its
preflight executes that interpreter inside a stricter read-only variant of the
worker profile and imports the required PyYAML and pypdf packages before it
initializes the TLS context. An unreadable macOS framework, Windows base
installation, missing dependency, or unavailable interpreter therefore fails
before a new parent is created by `run` and before a worker is launched by
`resume`. Poppler is not a managed-run prerequisite: pypdf is the portable
default PDF backend. A workspace that explicitly selects the Poppler
compatibility backend must provide `pdftotext` on the worker-visible `PATH`.

### Harness Support Levels

EvidenceWiki distinguishes three integration levels:

- **Managed adapters:** Codex and Claude Code are the registered runners for
  package-owned `run` and `resume` execution. The host performs runner-specific
  capability checks, fixed invocation, isolation, result decoding, and crash
  recovery.
- **External protocol:** OpenCode, Pi, Aider, Gemini CLI, and other harnesses
  can be integrated by a host that drives the model-neutral commands below.
  These harnesses are not package-managed runners.
- **Instruction compatibility:** any worker can receive the canonical
  `AGENTS.md`, the selected workspace skill, and the persisted work order.
  `CLAUDE.md` is a discovery pointer to `AGENTS.md`, not a separate contract.

The external protocol is:

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

An external-protocol host owns OS- or container-level process isolation and
must prevent user configuration, plugins, and MCP servers from becoming
unreviewed worker inputs. It also owns bounded structured-result extraction,
single-driver coordination, and crash replay of the same pending action. For
OpenCode and Pi, extract the work result from the harness's JSON event stream,
validate it against the published result schema, and pass only that bounded
document to `submit`; never treat a transcript as a result.

Attempt contract `1.0` readers accept bounded lowercase runner IDs so retained
history remains readable if the registry changes, but this starter writes only
`codex` or `claude`. A future package release must require a workspace upgrade
before it begins writing another managed runner ID. Runner-specific control
roots such as `.opencode/` or `.pi/` are likewise added only with an adapter
whose isolation contract has been implemented and verified.

Only one package-managed `run` or `resume` process may drive a parent session
at a time. The host holds a session-scoped lock for the complete managed window;
a concurrent managed driver exits with `ORCHESTRATION_ALREADY_RUNNING` before
launching a worker. That window lock is
`runs/orchestrations/<orchestration_id>/.locks/managed-host.lock` and is owned
by the host layer. It is complementary to the per-invocation driver lock below
rather than a substitute for it: the window lock spans a whole managed run or
resume, the driver lock spans one controller call.

The single-driver rule is now enforced by the workspace instead of being
promised by the host. `start`, `next`, and `submit` each hold
`runs/orchestrations/<orchestration_id>/.locks/session.lock` for the duration of
the call, and a second driver that finds it held is refused immediately with
`ORCHESTRATION_DRIVER_BUSY` and exit code `5`. It has an exit code of its own,
rather than sharing the general refusal exit `2`, because contention is the one
refusal a host is supposed to retry unchanged; a shell-only caller that can read
nothing but `$?` must be able to tell "come back in a moment" from "this will
never succeed as issued". The refusal is
`recoverable`, and `details.holder` names who the call lost to: `agent_id` (the
holder's `--agent-id`, or `null` when that command supplied none), `pid`,
`hostname`, `command`, and `acquired_at`. The holder block is a diagnostic hint
written by a peer, never an authorization claim, and it is `null` when it cannot
be read — a driver refused in the instant between the winner acquiring the lock
and publishing its identity legitimately sees nothing, and the refusal still
renders. A refused call writes nothing: no session document, no appended event.
The loser therefore retries against exactly the state it observed.

`ORCHESTRATION_DRIVER_BUSY` replaces `LOCK_UNAVAILABLE` for contention only.
`LOCK_UNAVAILABLE` still means what it always meant — no lock backend could be
established on this filesystem at all — and the two stay distinguishable because
retrying fixes one and never fixes the other.

Refusal rather than queueing is the default because queueing is what made
interleaved drivers invisible: two hosts whose calls each finished quickly both
succeeded, serially and silently, and the damage surfaced later as inconsistent
session state. A host that wants the bounded wait back asks for it.
`--driver-wait-seconds SECONDS` on `start`, `next`, and `submit` waits up to
SECONDS for the holder to release before refusing; the default `0` refuses
immediately. Negative, infinite, and non-numeric values are rejected at the CLI
boundary rather than clamped.

`status` takes no lock and never blocks, so a session another driver is
mutating can always be polled.

An external protocol host must still not interleave `next` or `submit` with an
active managed host for the same session. The driver lock is scoped to one
invocation, so it cannot see the gap *between* a managed host's own controller
calls; the workspace refuses a simultaneous second driver, not a second driver
that takes its turn between two of the first driver's calls.

Two `start` calls racing on the same explicit `--orchestration-id` may surface
either `ORCHESTRATION_EXISTS` — the loser arrived after the winner committed the
session — or `ORCHESTRATION_DRIVER_BUSY`, when the two were genuinely
simultaneous. Both are final for that call and carry the same remediation: pick
another id, or poll `status` for the session that now exists. A caller must
branch on the pair, never on which of the two it happened to receive.

A holder that dies releases the session lock, because the shared workspace lock
helper prefers `fcntl` and `msvcrt`, and those learn of process death from the
OS. On a filesystem that offers neither, the helper falls back to an
exclusive-create lock file with no owner-death notification, and there a driver
killed mid-call can block its successors for up to about two minutes before the
lock is treated as abandoned. That latency belongs to the exclusive-create
fallback alone; it never applies where a native advisory lock is available.

## Artifacts

One session is stored below:

```text
runs/orchestrations/<orchestration_id>/
  session.json
  events.jsonl
  work-orders/<action_id>.json
  work-results/<action_id>.json
  trusted-inputs/<action_id>.json
  trusted-inputs/<action_id>-scope-baseline.json
  attempts/<attempt_id>.json
  .host-results/<action_id>.json
  quarantine/<attempt_id>.json
  .locks/managed-host.lock
  .locks/session.lock
  answers.json
runs/orchestration-guards/<orchestration_id>.json
```

The `orchestration_session`, `orchestration_work_order`,
`orchestration_result`, and `orchestration_attempt` schemas are version `1.0`.
Query their supported versions with `evidence-wiki contract`. The existing
`artifact_schemas` mapping remains the compact version-negotiation surface.
The same response publishes complete, self-contained Draft 2020-12 JSON Schema
documents for all four orchestration artifacts under
`artifact_schema_documents`, so external protocol hosts can validate durable
controller output without importing EvidenceWiki Python modules.

`work-results/` contains only controller-accepted results. `trusted-inputs/`
contains the controller's static-input fingerprint for each pending action and,
for non-verification actions, an exact protected scope baseline referenced by
the public work order. The sidecar stores pre-action question, request,
candidate, manifest, raw, and normalized-evidence fingerprints; it is an
internal controller artifact, not a fifth published protocol schema. It is
capped at 8 MiB, while the persisted public work order is capped at 256 KiB.
`attempts/` contains bounded host execution metadata without prompts,
transcripts, diagnostics, secrets, or absolute paths. `.host-results/` is a
private, transient submission stage used after a validated worker response;
it is removed after the identical canonical result is observed.
`quarantine/` retains a validated result that was not submitted because the
post-run control tripwire detected drift. Quarantined output is never treated
as an accepted work result. It is created only when the runner produced a
schema-valid result before the tripwire detected drift. The corresponding file
under `runs/orchestration-guards/` is the durable repair gate shared by the
managed host and workspace controller; it lives outside the guarded parent tree
so replacing that tree cannot erase the repair requirement.
The private `.locks/managed-host.lock` serializes managed drivers for a whole
run or resume window; `.locks/session.lock` serializes one controller
invocation and is what refuses a second driver with
`ORCHESTRATION_DRIVER_BUSY`. Both are lock machinery rather than session
content: the `.locks/` subtree is excluded from the managed-run control
snapshot and from the semantic tripwire fingerprint, and its transient
artifacts — the exclusive-create fallback's lock file, the holder sidecar a
crashed driver leaves behind — are therefore never reported as control drift.
Excluded is not uninspected: a symlink, special file, or hard link planted under
`.locks/` is still refused as an unsafe control artifact.

The repair guard is deliberately not stored below the parent-session tree.
`runs/orchestration-guards/<orchestration_id>.json` is a preventive-only,
host-owned control file: a worker cannot erase the repair gate by replacing
`runs/orchestrations/<orchestration_id>/`, and the guard itself is not included
in the semantic tripwire whose expected fingerprint it retains.

A work order contains:

- `orchestration_id`, `action_id`, issuance time, and lease;
- a phase and workspace skill handler;
- the active child `run_id`, when the phase belongs to a bounded run;
- scoped question, source-request, and candidate IDs;
- effective discovery/acquisition provider allow-lists and remaining budgets;
- workspace-relative input artifacts and deterministic completion checks.

Research, discovery, candidate-review, and acquisition orders contain exactly
one `controller_integrity_baseline` completion check. It carries only the
workspace-relative protected-sidecar path, SHA-256 fingerprint, and bounded
field/entry counts. The controller validates and hydrates the private baseline
in memory before replay or submission; workers neither receive its embedded
maps nor own the artifact.

A result contains the matching action ID, `completed`, `blocked`, or `failed`, a
short summary, and workspace-relative artifact paths. Session artifacts must
not contain provider credentials, absolute host paths, source instructions,
chat transcripts, or unbounded runner output.

For package-managed runners, the host removes otherwise safe artifact-list
references below `runs/orchestrations/` before validating and submitting the
result. Those paths describe host state rather than worker output, and the
controller verifies completion from deterministic workspace postconditions.
This canonicalization does not authorize control-tree writes: the post-action
snapshot still rejects any creation, mutation, rename, relink, deletion, or
metadata change below the parent session. Direct `submit` results remain strict
and reject every `runs/orchestrations/` artifact reference.

Result outcomes describe the work order, not the child run's research verdict:

- `completed` means the requested bounded action was carried out and its
  postconditions are ready for deterministic verification. A research action
  that creates structured source requests and ends its child run as
  `blocked_on_sources` must return `completed`; that is the expected handoff to
  parent discovery/acquisition.
- `blocked` means the current bounded attempt cannot complete. The controller
  classifies the durable post-action state instead of trusting the summary. A
  non-acquisition action, or an acquisition action whose scoped candidate
  remains `selected`, pauses the parent with the same pending action; `resume`
  replays that action. A candidate-specific acquisition failure must make the
  one canonical `selected` → `failed` transition and append its audit event.
  That completes only the failed route action and returns the parent to
  planning so another retained or discoverable route can be tried.
- `failed` means execution is unrecoverable and terminates the parent as
  `failed`. A repairable dependency, local tool, or transient provider failure
  is `blocked`, not `failed`, and must leave the acquisition candidate
  `selected`.

A worker's `blocked` result never directly assigns terminal `blocked_on_sources`.
Only the controller's route exhaustion decision on the next routing pass may do
so, after artifact-derived routing proves that every permitted route for the
open source requests is exhausted.

## Routing And Run Boundaries

The controller prioritizes health failures, an existing pending action,
actionable questions, selected acquisition routes, candidate review, enabled
discovery, verification, and export in that order. It declares
`blocked_on_sources` only when open source requests remain and no configured
provider route can progress them. A failed candidate route therefore does not
terminate the parent by itself: the controller first considers another selected
candidate, reviewable candidates, and an enabled composable discovery route.

Discovery and candidate-review work orders make their no-fetch boundary
content-based. At issue time the controller records a SHA-256 fingerprint of
the configured immutable raw roots, bounded to 10,000 files and 2 GiB, plus the
exact digest of `sources/manifest.jsonl`, bounded to 32 MiB. Submission
recomputes both values, so unchanged counts, sizes, or timestamps cannot hide a
raw-evidence or evidence-manifest rewrite. The candidate side is checked from
the exact configured candidate-store path: discovery must add a
request-scoped candidate, and review must select a scoped candidate with an
enabled acquisition route. These phases may update candidate records and their
audit trail, but they may not fetch evidence or alter existing raw or manifest
content.

All mutating phases also compare exact pre-action records. Research may change
only scoped question files and append open requests linked only to those
questions. Discovery may append only request-scoped candidates from enabled
providers. Review may change only scoped candidate IDs. Acquisition may fulfill
only scoped requests, transition only scoped candidates/questions, preserve all
existing raw and normalized evidence, and create only outputs attributable to
new fulfilled source IDs. An unchanged pre-existing source is reusable only
when both its manifest record and normalized output exactly match the protected
baseline.

A **delegated** acquisition action is bounded the same way, with three
differences that follow from having no candidate store: it may not change
candidate records at all, a scoped request it did not fulfil must be
byte-identical (its failure lives in the attempt audit, not in the request), and
it may append to that append-only audit but never rewrite an event that existed
when the order was issued. Question files are mutable only for questions whose
*every* blocking request was fulfilled. Pre-existing evidence is reusable on the
same terms as above, correlated by `provenance.request_id` alone.

`blocked_on_sources`, `no_ship`, and `failed` remain terminal child-run states.
The parent never reopens those records. For example, an initial research run
against an empty workspace can block and create a source request; the parent
then retains that run, starts another child run for discovery and acquisition,
and eventually sends the reopened question to research again.

Under `research.yml` `review.escalation_scope: question` (see
`docs/research-yml.md`), a question awaiting human review no longer flips the
workspace verdict, so the controller keeps issuing work for the remaining
questions instead of refusing the workspace. When every remaining question
awaits review, the readiness verdict is `in_progress` with an empty actionable
scope; rather than issue a work order scoped to no questions, the parent
terminates `no_ship`. The terminal reason begins with the stable prefix
`All remaining questions await human review`, and the `session_finished` event
carries `questions_awaiting_review` and `question_slugs` in its `data`, so a
host can branch on the count without parsing prose. The host records the
outstanding reviews and starts a new session. Under the default `workspace`
scope the pending review still produces `attention_required`, and `next`,
`submit`, and replay still refuse with `ORCHESTRATION_WORKSPACE_UNSAFE`.

Scoping does not disable that refusal for anything else. `next`, `submit`, and
replay still raise `ORCHESTRATION_WORKSPACE_UNSAFE` for smoke failure, HIGH lint
findings, a blocked question without a linked open request, and the filesystem
integrity guards over `raw/`, question files, and normalized evidence. That
matters for the scoped mode specifically: `review.max_pending_review_hours`
makes lint report a review nobody works as a HIGH finding, which returns the
verdict to `attention_required` and re-freezes the workspace through the
ordinary path. A scoped review queue cannot rot unnoticed.

`research.yml` is a trusted static input of an orchestration session. Changing
`review:` while an action is pending is refused with
`ORCHESTRATION_TRUSTED_INPUT_CHANGED`, naming `research.yml` in
`details.changed_paths`. This is deliberate fail-closed behavior, not a
limitation to work around: a session must not have its escalation semantics
changed underneath a work order it already issued. Set the escalation scope
before `orchestrate start`; a changed scope takes effect for the next session.

The parent declares `complete` only after fresh publication-readiness evaluation
returns `ship`, then writes `answers.json` through the deterministic answer
exporter. `workspace_status.py` remains read-only and exposes only an additive
summary of the newest parent session.

## Delegated Acquisition

A workspace that disables its own acquisition providers has no route to the
acquisition phase: the controller has nobody to address the work to, so open
source requests terminate the session `blocked_on_sources` however good the
host's own fetching is. `research.yml` can name that host instead:

```yaml
orchestration:
  acquisition: delegated          # default: providers
  acquirer_agent_id: autoseller-orchestrator
  max_attempts_per_request: 2     # optional, default 2, maximum 10
```

The mode is resolved once, at `orchestrate start`, and frozen into the session
alongside `provider_policy`. Declaring it together with an enabled
`integrations.acquisition` is refused there — exactly one of them acquires. A
session whose declaration changes while it is running is refused with
`ORCHESTRATION_DELEGATION_CHANGED`; under a pending action the trusted-static-input
guard answers first with `ORCHESTRATION_TRUSTED_INPUT_CHANGED`. Delegation is not
a provider grant: the effective provider policy stays empty and the package still
fetches nothing.

**Routing.** Under the `blocked_on_sources` verdict the controller skips the
candidate and discovery arms entirely and issues one acquisition work order
scoped to every open request still worth attempting, addressed to
`assigned_agent_id`, with `acquisition_mode: delegated`, an empty `candidate_ids`
scope, and the `research-acquire-delegated` skill. Batching is deliberate: a host
with parallel connectors should not be forced through one protocol round trip per
request.

**Execution.** The acquirer works *inside the pending order* — deliver under
`docs/source-delivery.md` with `request_id` stamped in the sidecar, inventory,
normalize, `source_requests.py fulfill`, `question_resolve.py reopen` — and
records anything it could not obtain with
`source_requests.py record-attempt-failure`. See
[../skills/research-acquire-delegated.md](../skills/research-acquire-delegated.md)
for the full sequence. Managed `orchestrate run` and `resume` refuse a delegated
workspace with `RUNNER_DELEGATED_ACQUISITION_UNSUPPORTED`: the order is addressed
to the host's own connectors, which no managed worker can be.

**Result semantics.** Every scoped request must end the action with a fulfilment
**or** a recorded attempt failure naming that action; a request with neither
fails the postconditions. A **partial** batch is therefore `completed`, not
degraded — that is the ordinary shape of a delegated action. `blocked` means the
attempt changed nothing durable at all and `resume` replays the same order;
unlike the provider path, no partial delivery is tolerated, because a delegate
holding delivered evidence can simply fulfil it. `failed` remains reserved for an
unrecoverable condition, never a throttled or unauthorized connector.

**Retry and exhaustion.** Attempts are counted from the durable audit and scoped
to the current session, so a new session gets a fresh look at every request —
that is the supported way to retry after fixing a host-side cause, rather than
editing an append-only audit. A request is retired when it reaches
`max_attempts_per_request` or when its most recent failure is a standing decision
(`not_authorized`, `robots_or_terms_blocked`, `license_or_terms_unknown`,
`manual_review_required`). When every open request is retired the session
terminates `blocked_on_sources` with a reason beginning
`Delegated acquisition exhausted its attempts for every open source request`, and
the `session_finished` event carries `exhausted_requests` mapping each request id
to its last failure code, so a host branches on data rather than prose.

**Fulfilment stays inside the protocol.** While a delegated session is live,
`source_requests.py fulfill`, `record-attempt-failure`, and
`question_resolve.py reopen` are refused for anything no pending work order
scopes — `SOURCE_REQUEST_FULFILL_DELEGATED` and `QUESTION_REOPEN_DELEGATED`. A
request is sanctioned by a pending delegated acquisition order that scopes it; a
question by any pending order that scopes its slug, since research orders
legitimately mutate their own questions. An identical re-fulfil is a no-op and
stays allowed. Without the `orchestration:` section, or with every session
terminal, nothing is gated and these commands behave exactly as before.

## Provider And Runner Boundaries

An orchestration session never enables discovery or acquisition. Its effective
provider set is restricted to the explicit phase allow-lists in `research.yml`.
A domain-pack recommendation, installed runner, available API key, prompt, or
candidate URL cannot widen those permissions.

The Codex and Claude adapters launch fixed argument vectors without a shell,
request schema-constrained final output, cap time and captured output, and run
with the workspace as their working directory. They do not add dangerous
permission or sandbox bypass flags. When recovery actually needs a fresh
worker, the host lazily verifies before launch that the selected runner can
enforce its permission profile and keep
`runs/orchestrations/<orchestration_id>/` host-owned. Canonical-result and clean
staged-result recovery do not require a runner capability check. Provider
credentials may be injected into the process environment by the operator, but
are never copied into work orders, results, logs, or error messages.

Managed work orders must not start daemons, hooks, background jobs, or detached
subprocesses; every process started for an action must finish within that
action. The host cleans up the runner's initial process group, but that cleanup
is not containment for hostile processes that deliberately escape or detach.
A deployment that executes untrusted process trees must add an
operator-controlled container or equivalent lifecycle boundary.

The Codex adapter ignores user configuration and rules for the worker process,
uses strict configuration, disables web search, and does not use the legacy
`--sandbox` selector. Its named profile makes scoped workspace output writable
while keeping the workspace contract and instructions (`research.yml`,
`workspace-system.yml`, `AGENTS.md`, `CLAUDE.md`, `README.md`, and `.gitignore`),
managed tooling and guidance (`scripts/`, `skills/`, and all of `docs/`), parent state
(`runs/orchestrations/`), repository and agent configuration, and local virtual
environments read-only. New generated reports are written to
`runs/run-reports/`, outside the trusted documentation tree; reports left under
`docs/run-reports/` by earlier starters remain historical read-only inputs. The
bounded temporary roots remain writable. The Claude
adapter supplies equivalent host-owned sandbox
settings with fail-if-unavailable behavior, OS-level write denials, Edit-tool
denials for the complete documentation tree, and WebFetch/WebSearch disabled.
Network access is enabled only for a
discovery or acquisition work order whose persisted provider policy permits it.
For those provider-enabled Codex actions on Linux/WSL2, the profile preserves
the lexical `/etc` path read-only because Codex's bubblewrap `:minimal`
filesystem does not materialize the resolver symlink or the NSS and CA
configuration needed by normal HTTPS clients. If `/etc` is itself a symlink,
its canonical directory target is also granted read-only. If
`/etc/resolv.conf` resolves outside that directory, its canonical target must
be under a bounded supported system resolver directory and receives one exact
read-only grant. Offline actions receive none of these grants; an unexpected
resolver target fails closed with `RUNNER_ISOLATION_UNAVAILABLE`.

The Codex preflight resolves the selected launcher before session creation.
For an official npm, pnpm, or bun launcher (including a Windows npm shim), it
locates the matching platform package and grants only the canonical native
target tree containing `bin/`, `codex-resources/`, and `codex-path/` as an
absolute read-only profile rule. Direct native/IDE layouts use their bounded
runtime tree. The grant is identical in the enforcement probe and every worker
action; it never widens to the operator's home, `CODEX_HOME`, or an npm prefix.
A missing/malformed platform package or a runtime that overlaps the writable
workspace fails with `RUNNER_ISOLATION_UNAVAILABLE` before a parent session is
created. The official launcher remains the executed command so its supported
runtime environment and signal handling are preserved, except on native
Windows when `PATH` resolves an npm `.cmd`/`.bat`/`.ps1` shim: the host resolves
and executes the validated packaged `codex.exe` directly to retain
`shell=False` and avoid command-shell injection semantics.

The same Codex preflight resolves the exact `sys.executable` used by the
EvidenceWiki host without resolving away a workspace `.venv`/`venv` launcher.
The launcher stays protected by the workspace profile while any external base
runtime (including `Python.framework` on macOS) receives only a read-only
grant. Login shells are disabled and the worker shell inherits only the core
environment plus the pinned interpreter and system-tool path. The final probe
runs that exact interpreter in isolated, no-bytecode mode inside a read-only
workspace profile, verifies the environment binding, imports the required
PyYAML and pypdf packages, and initializes the TLS context. A developer-tools
shim, dependency shadowing, or inaccessible framework therefore cannot reach
the model action.

The host calls the semantic baseline the **tripwire-protected controls**. Its
bounded snapshot covers the workspace contract and instructions
(`research.yml`, `workspace-system.yml`, `AGENTS.md`, `CLAUDE.md`, `README.md`,
and `.gitignore`), `scripts/`, `skills/`, `docs/`, and the current
`runs/orchestrations/<orchestration_id>/` parent session. The sandbox also keeps
`.git/`, `.codex/`, `.claude/`, `.agents/`, `.venv/`, `venv/`, and
`runs/orchestration-guards/` read-only, but those are **preventive-only
read-only roots**: they are not recursively content-hashed into the bounded
tripwire. Preventive-only does not mean worker-writable; it distinguishes a
sandbox denial from the host's independent before/after semantic check.

The allow-lists are application authorization enforced by the deterministic
provider scripts; they are not a host-level domain firewall. A managed worker
is therefore a trusted workspace writer and must use only the scoped skill and
provider commands in its work order. For an untrusted or prompt-injected agent,
add an operator-controlled network proxy/sandbox or drive the protocol from a
host that executes provider calls itself. Direct protocol workers must likewise
not edit `runs/orchestrations/` control artifacts.

The append-only academic provider-call ledger under the active child run is
restart-stable workspace accounting enforced by those protected scripts. New
runs carry a versioned marker that binds them to their pre-created empty ledger.
An active legacy run without that marker has an unknown prior call count and
fails discovery and readiness closed; preserve it and start a fresh run rather
than creating an empty ledger by hand. A missing or corrupt marked ledger also
fails closed. Completed legacy runs remain inspectable. The ledger is not a
host-level network quota against a malicious process that bypasses the provider
commands. Deployments that need that stronger boundary must put provider
traffic behind an operator-controlled proxy or execute provider calls in the
host, in addition to the workspace ledger.

The parent owns every file below
`runs/orchestrations/<orchestration_id>/`. A worker must not invoke
`evidence-wiki orchestrate`, create a work-result file, or otherwise mutate that
tree. It receives all required orchestration data in the persisted work order
and returns its result through the runner's structured-output channel. Child
research state under `runs/<run_id>/` remains a separate workspace surface and
may be updated only through the scoped deterministic scripts named by the
workspace skill.

Managed orchestration is intentionally not exposed through the current MCP
server. MCP remains a read/append-only integration surface; starting workers
and acquiring remote evidence require the package CLI and its explicit policy
checks.

## Recovery

Session writes and action submission are lock-protected. A leased action can be
recovered after its lease expires; a runner crash leaves the same action pending
for `resume`. Re-submitting an identical accepted result is idempotent, while a
different result for the same completed action is a conflict.

A host-side runner failure is resumable because no result was accepted. A
schema-valid `blocked` result normally pauses with the action still pending;
`resume` replays that same action after the dependency, tool, provider, or other
temporary condition is repaired. The bounded exception is an acquisition
candidate already transitioned from `selected` to `failed` with the exact new
canonical audit event: the controller retains that result as a completed route
attempt and continues planning instead of replaying it.

By contrast, a schema-valid worker result with `outcome: failed` is an accepted
terminal result: it records the failure, completes the pending action, and
closes the parent session as `failed`. `resume` never reopens that session.
After repairing an unrecoverable environment failure, preserve the failed
session for audit. Review any partial worker outputs, then start a fresh parent without the old ID:

```bash
evidence-wiki orchestrate run \
  --target . \
  --runner codex \
  --agent-id AGENT_ID \
  --model MODEL_ID
```

Before launching a fresh worker, the host caps its timeout to the smallest of
the managed action limit, the work-order budget, the lease duration, and the
remaining time until the absolute lease expiry. A malformed expiry fails with
`ORCHESTRATION_LEASE_INVALID`; an already expired lease fails with
`ORCHESTRATION_LEASE_EXPIRED` before worker launch. If a retained `running`
attempt still owns the same lease attempt, replay fails with
`ORCHESTRATION_LEASE_ACTIVE`. Wait for expiry and resume: the controller renews
the lease by incrementing its attempt while retaining the same action ID.

Recovery uses the least-mutating valid checkpoint in this exact order:
accepted canonical result; clean validated host-staged result; replay the same persisted action.
The first checkpoint is finalized, the second is submitted without launching a
worker, and replay occurs only when neither checkpoint exists. Recovery never
issues a replacement action. No recovery path requires a human-authored result document.
The worker first checks the recorded postconditions against current workspace
artifacts. When an earlier attempt already wrote a valid answer, request,
candidate selection, or acquired source before it exited, the worker reports
the existing artifacts as `completed` without repeating that work. The
controller then verifies the real artifacts before advancing. Pre-0.2.1
pending actions are bound to a controller-owned trusted-input fingerprint on
their first successful `next --resume`, before a managed worker starts. That
static-input migration cannot reconstruct a missing semantic pre-action
baseline: a legacy pending research, discovery, candidate-review, or acquisition
action without its scoped question, candidate, selection, or evidence baseline
fails closed and must be preserved while a fresh parent session starts from
reviewed workspace state. A direct result submission is refused until all
required bindings exist. Do not hand-write
`runs/orchestrations/<orchestration_id>/work-results/*.json`, edit
`session.json`, or use `deploy --force` as recovery.

Managed runners compare the tripwire-protected controls around every action.
Timestamp-only filesystem drift is not a control change. Path membership, file
type, mode, or content changes are control changes and are reported with
bounded, workspace-relative paths. `CONTROL_ARTIFACT_UNSAFE` refuses an
unbounded snapshot or links/special files. `CONTROL_ARTIFACT_TAMPERED` means the
tripwire observed a semantic change; no result is submitted and the action
remains pending. When the runner returned a valid structured result before the
drift was detected, that unsubmitted result is retained under `quarantine/`;
otherwise no quarantine file is invented. The attempt is marked
`control_tampered`, and the host persists
`runs/orchestration-guards/<orchestration_id>.json` with status `required`. A
later managed resume stops with `CONTROL_REPAIR_REQUIRED` before any controller
or worker command. Direct protocol `next` and `submit` calls independently stop
with `ORCHESTRATION_CONTROL_REPAIR_REQUIRED` while the same marker is required.
EvidenceWiki does not automatically restore or roll back any changed file.
Inspect the named paths and quarantine, restore the issued control state, then
explicitly acknowledge that review:

```bash
evidence-wiki orchestrate resume \
  --target . \
  --orchestration-id ORCH_ID \
  --runner codex \
  --agent-id ORIGINAL_AGENT_ID \
  --acknowledge-control-repair
```

The acknowledgement clears the host gate only after every tripwire-protected
control matches the `expected_control_fingerprint` retained in
`runs/orchestration-guards/<orchestration_id>.json`; otherwise it fails with
`CONTROL_REPAIR_MISMATCH`. If a legacy or damaged session retains a
`control_tampered` attempt but no durable guard with that pre-action baseline,
acknowledgement fails with `CONTROL_REPAIR_BASELINE_MISSING`; preserve the
session for inspection and start a new session from reviewed workspace state.
Acknowledgement does not accept the quarantined result or bypass controller
verification. The controller also checks the action's trusted-input
fingerprint. If a static control change was intentional, start a new
orchestration session from that state. Do not use the flag before inspecting
the reported paths.

After upgrading the package, refresh an existing workspace's managed scripts
and inspect the retained phase before deciding whether it is safe to resume.
Preview both steps first:

```bash
python -m pip install --upgrade evidence-wiki==0.3.0
evidence-wiki --version
evidence-wiki upgrade --target . --dry-run
evidence-wiki upgrade --target .
evidence-wiki orchestrate status --target . --orchestration-id ORCH_ID --format json
```

If status shows a pending legacy action and resume reports
`ORCHESTRATION_RESEARCH_BASELINE_UNAVAILABLE`,
`ORCHESTRATION_DISCOVERY_BASELINE_UNAVAILABLE`,
`ORCHESTRATION_CANDIDATE_REVIEW_BASELINE_UNAVAILABLE`, or
`ORCHESTRATION_ACQUISITION_BASELINE_UNAVAILABLE`, preserve that session for
audit and start a new orchestration. Otherwise resume the upgraded session:

For current actions, `ORCHESTRATION_INTEGRITY_BASELINE_CHANGED` means the
protected scope sidecar is missing or no longer has its issued digest, while
`ORCHESTRATION_INTEGRITY_BASELINE_INVALID` means its identity, shape, or summary
is inconsistent. Restore the exact controller-owned artifact or preserve the
session and start a fresh one; never infer a replacement from post-action state.
`ORCHESTRATION_SCOPE_EXCEEDED` means the 8 MiB sidecar or 256 KiB public order
bound was exceeded and the action scope/history must be reduced.

```bash
evidence-wiki orchestrate resume \
  --target . \
  --orchestration-id ORCH_ID \
  --runner codex \
  --agent-id ORIGINAL_AGENT_ID \
  --model MODEL_ID
```

To refresh the optional worker guidance as well, review it separately:

```bash
evidence-wiki upgrade --target . --include skills --include docs --dry-run
evidence-wiki upgrade --target . --include skills --include docs
```

Optional-file conflicts stop before replacement. Use `--force-optional` only
after reviewing the local edits and the backup behavior under `.replaced/`.
