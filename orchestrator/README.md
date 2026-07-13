# Orchestrator Skills

Skills in this directory are for the **external orchestrator role** — a PM,
planner, or parent agent that creates and manages research workspaces *from the
outside*, through the `evidence-wiki` package CLI and the per-workspace scripts.

This is deliberately separate from `workspace-template/skills/`, whose
`research-*` playbooks are copied into every created workspace for the
**inside-the-workspace research agent** (initialization, retrieval, the run loop,
acquisition, synthesis, and so on). Orchestrator skills are *not* copied into a
workspace; they ship with the package for the agent that sits above one or more
workspaces.

## Contents

- `skills/research-orchestrate.md` — end-to-end playbook for deploying a
  workspace, seeding questions and evidence, driving or delegating the run loop,
  routing blocked sources to a fetch agent, and collecting cited results. It is
  the executable companion to the machine contract in
  `workspace-template/docs/orchestrator-handoff.md` (the contract stays
  canonical for schemas and error codes).

## Discovery

An agent that has only the installed package can locate this skill without a
source checkout:

```bash
evidence-wiki orchestrator-guide            # print the resolved skill path
evidence-wiki orchestrator-guide --print    # print the skill content
evidence-wiki orchestrator-guide --format json
```
