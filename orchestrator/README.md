# Orchestrator Skills

Skills in this directory are for the **external orchestrator role** — a PM,
planner, or parent agent that creates and manages research workspaces *from the
outside*, through the `evidence-wiki` package CLI and the per-workspace scripts.

This is deliberately separate from `workspace-template/skills/`, whose
`research-*` playbooks are copied into every created workspace for the
**inside-the-workspace research agent**. Orchestrator skills are *not* copied
into a workspace; they ship with the package for the agent that sits above one
or more workspaces.

## Contents

- `skills/research-orchestrate.md` — end-to-end playbook for deploying a
  workspace, seeding questions, and driving the durable work-order protocol
  through research, discovery, candidate review, acquisition, verification,
  and export. It is the executable companion to the machine contract in
  `workspace-template/docs/orchestrator-handoff.md` (the contract stays
  canonical for schemas and error codes).

## Managed Run

The installed package can execute the protocol through Codex or Claude Code:

```bash
evidence-wiki orchestrate run --target /path/to/workspace --runner codex --agent-id pm-agent
evidence-wiki orchestrate resume --target /path/to/workspace \
  --orchestration-id ORCH_ID --runner codex --agent-id pm-agent
```

Managed Codex execution requires Codex CLI 0.138 or newer. User-local npm,
pnpm, and bun launchers are supported by granting only their resolved native
runtime tree read-only; install the runner outside the writable research
workspace. Managed Claude execution is unavailable on native Windows because the required per-path
isolation is not available there; use macOS, Linux, WSL2, or a container.
Managed Claude also requires `bubblewrap` plus `socat` on Linux/WSL2, or
`sandbox-exec` plus `touch` on macOS.
EvidenceWiki checks runner isolation and runtime visibility before launching a worker and
fails with `RUNNER_ISOLATION_UNAVAILABLE` when it cannot protect the host-owned
`runs/orchestrations/<orchestration_id>/` tree.

The semantic baseline covers bounded **tripwire-protected controls**: workspace
contract/instruction files, `scripts/`, `skills/`, `docs/`, and the current
parent session. `.git/`, `.codex/`, `.claude/`, `.agents/`, `.venv/`, and
`venv/` are preventively read-only to the runner but are not post-action
tripwire snapshots. The durable host guard root is likewise preventive-only
and lives outside the parent session at
`runs/orchestration-guards/<orchestration_id>.json`.

Only one managed host may drive a parent session at a time. A competing managed
process receives `ORCHESTRATION_ALREADY_RUNNING` before a worker is launched.
An external host using `start` / `next` / `submit` / `status` must provide
equivalent session-wide coordination around the command-level protocol.

A retained running attempt with the current lease fails before worker launch
with `ORCHESTRATION_LEASE_ACTIVE`; wait for expiry and resume the same action
so its lease can be renewed. Malformed and already-expired absolute leases fail
with `ORCHESTRATION_LEASE_INVALID` and `ORCHESTRATION_LEASE_EXPIRED`. The
effective runner timeout never exceeds the lease's remaining lifetime.

Recovery uses the most advanced valid checkpoint: keep the orchestration ID and
call `resume`. The exact order is an accepted canonical result, an identical
clean staged result, and then the same persisted action in a fresh worker only
when neither checkpoint exists. The worker checks persisted postconditions
before making new changes, so an already-materialized action is not duplicated.
Bounded `attempts/<attempt_id>.json` records retain execution status, while
private `.host-results/<action_id>.json` envelopes bridge a crash between
validation and canonical controller submission. Generated run reports belong
under `runs/run-reports/`; old `docs/run-reports/` files remain historical
read-only inputs.

Never hand-write a work result or edit the parent control tree. If the semantic
tripwire reports `CONTROL_ARTIFACT_TAMPERED`, inspect the exact paths, the
bounded attempt record, and any never-submittable `quarantine/` entry. The host
does not restore changed files. Its durable
`runs/orchestration-guards/<orchestration_id>.json` marker makes a later
managed resume fail with `CONTROL_REPAIR_REQUIRED` before any controller or
worker command. After restoring the issued state, pass
`--acknowledge-control-repair`; acknowledgement requires the controller-owned
tripwire-protected baseline to match its saved pre-action fingerprint or fails
with `CONTROL_REPAIR_MISMATCH`. If no trustworthy pre-action baseline survives,
it fails with `CONTROL_REPAIR_BASELINE_MISSING`; preserve the old session for
inspection and start a new orchestration. The flag does not accept quarantine
or bypass the controller's trusted-input fingerprint. Start a new session for
intentional static-control changes.

Discovery and candidate review may update only candidate metadata. The
controller preserves their no-fetch boundary with a bounded content digest of
the configured raw roots (at most 10,000 files and 2 GiB) and the exact record
count and content digest of `sources/manifest.jsonl` (at most 32 MiB); the
evidence tree and manifest must remain unchanged until acquisition.

Workers must not start daemons, hooks, background jobs, or detached
subprocesses, and all action processes must finish before result submission.
Process-group cleanup is not a security boundary for a hostile process tree;
run untrusted agents inside an operator-controlled container or VM.

For any other agent harness, use `orchestrate start`, `next`, `submit`, and
`status` directly. Work orders are persisted under
`runs/orchestrations/<orchestration_id>/`; worker claims are accepted only
after the controller verifies the corresponding workspace artifacts.

Provider authorization remains workspace policy. The orchestrator never turns
on discovery or acquisition and never widens `research.yml` allow-lists.
Those allow-lists are enforced by provider scripts rather than a host egress
firewall, so managed and external workers are trusted workspace writers. Put
untrusted or prompt-injected agents behind an operator-controlled network
sandbox/proxy.

## Discovery

An agent that has only the installed package can locate this skill without a
source checkout:

```bash
evidence-wiki orchestrator-guide            # print the resolved skill path
evidence-wiki orchestrator-guide --print    # print the skill content
evidence-wiki orchestrator-guide --format json
```
