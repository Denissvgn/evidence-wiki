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
