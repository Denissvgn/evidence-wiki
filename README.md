# EvidenceWiki

**Answers you can audit.** This project builds *verifiable autonomous research*
workspaces: any LLM agent can drive the research loop, but every answer must
cite normalized source records with provenance, every state transition is owned
by a deterministic script, and a question that lacks required evidence blocks
with a structured source request instead of degrading into a weakly-cited
answer.

It ships as reusable tooling: a configurable research wiki starter,
deterministic scripts for inventory/normalization/linting and the question
lifecycle, optional domain packs, and tests that prove a minimally described
research project can be initialized and validated without hand-editing
`research.yml`.

## Why It Is Different

Most research agents produce a one-shot report that cites whatever they
happened to retrieve. This system takes the opposite bet:

- **A persistent workspace, not a disposable report.** Research accumulates in
  a versioned wiki where every claim cites a source ID that resolves to a
  normalized record with provenance. Answers are regenerable, lintable, and
  exportable as structured JSON.
- **Evidence gates, not vibes.** High-stakes questions carry coverage
  manifests with facet-typed requirements (for example `official_guidance` or
  `standards_registry_reference`). A failing facet blocks the question with a
  structured source request; it never silently downgrades the answer. See
  `workspace-template/docs/coverage-manifest.md`.
- **Blocked, not fabricated.** "No such paper exists" claims require a
  `claim_probe` record: provider, query, result counts, and an explicit
  limitation statement — bounded non-confirmation instead of invented
  citations.
- **Multi-agent-safe by construction.** Question claiming, per-run budgets,
  stale-claim recovery, and deterministic `blocked → reopen` transitions make
  unattended fleets coordinable. LLM skills do the judgment; scripts own every
  state transition, which is what makes the behavior testable.
- **Source content is data, never instructions.** Acquisition fails closed on
  TLS/DNS/redirect/media-type/size violations, provenance-incomplete
  deliveries are quarantined, and provenance URLs are never auto-fetched. See
  `workspace-template/docs/prompt-injection-hardening.md`.

## Five-Minute Tour

The goal: start with an empty source directory and let an agent produce a cited,
exportable answer from evidence that it discovers and acquires during the run.
Install the package and create a provider-enabled scientific workspace:

```bash
python3 -m pip install evidence-wiki
evidence-wiki deploy \
  --target solid-state-batteries \
  --project-name solid-state-batteries \
  --project-description "Survey of solid-state battery electrolyte research" \
  --domain-pack general-science \
  --discovery-provider arxiv \
  --discovery-provider openalex \
  --acquisition-provider arxiv \
  --acquisition-provider openalex
cd solid-state-batteries
```

The repeated provider flags are explicit network authorization. The
`general-science` pack recommends these providers but does not enable them by
itself. arXiv needs no credential; OpenAlex can use `OPENALEX_API_KEY` from the
process environment for authenticated quotas. Never put credentials in
`research.yml`.

Seed the backlog with a question (schema in
`workspace-template/docs/question-api.md`):

```bash
cat > batch.yaml <<'EOF'
schema_version: "1.0"
questions:
  - question: "Which solid electrolyte families report room-temperature ionic conductivity above 1 mS/cm?"
    id: electrolyte-conductivity
    priority: high
EOF
evidence-wiki questions add --target . --from-file batch.yaml
```

Run the managed orchestrator with an installed agent CLI:

```bash
evidence-wiki orchestrate run \
  --target . \
  --runner codex \
  --agent-id battery-demo
```

Use `--runner claude` to run the same work-order protocol through Claude Code.
Pass `--model MODEL_ID` only when an explicit runner-specific model override is
needed; otherwise the runner's safe configured default is used.
The orchestrator does not contain a battery-specific workflow. It first gives
the research agent the empty workspace; when the question blocks, it retains
that immutable run, discovers request-linked academic candidates, asks the
agent to select them, acquires the selected evidence, inventories and
normalizes it, fulfills the request, reopens the question, and starts a later
bounded research run. It declares completion only after deterministic
verification passes.

Managed runner isolation is fail-closed. Codex requires Codex CLI 0.138 or
newer so EvidenceWiki can apply its named `evidence_wiki_worker` permission
profile. Managed Claude execution is unavailable on native Windows; use macOS,
Linux, WSL2, a container, or the external `start` / `next` / `submit` protocol
there. If the required boundary cannot be enforced, the host returns
`RUNNER_ISOLATION_UNAVAILABLE` before starting a worker. The parent owns
`runs/orchestrations/<orchestration_id>/`, and workers must never write that
tree or invoke `evidence-wiki orchestrate` themselves.

The semantic baseline is the set of **tripwire-protected controls**: the
workspace contract and instruction files (`research.yml`,
`workspace-system.yml`, `AGENTS.md`, `CLAUDE.md`, `README.md`, and
`.gitignore`), plus bounded snapshots of `scripts/`, `skills/`, `docs/`, and
the current parent session. Snapshotting is capped at 10,000 entries and 32 MiB
of regular-file content. The runner also makes `.git/`, `.codex/`,
`.claude/`, `.agents/`, `.venv/`, and `venv/` read-only preventively, but those
potentially large roots are not post-action tripwire snapshots. The durable
host guard root is likewise preventive-only and lives outside the parent tree at
`runs/orchestration-guards/<orchestration_id>.json` and is read-only to the
worker.

Only one managed host may drive a parent session at a time. A competing managed
drive for the same session fails before launching a worker with
`ORCHESTRATION_ALREADY_RUNNING`. Hosts built on the external
`start` / `next` / `submit` / `status` protocol must provide equivalent
session-wide coordination; the command-level locks do not replace that host
ownership boundary.

Managed Claude additionally requires `bubblewrap` and `socat` on Linux/WSL2,
or the built-in `sandbox-exec` and `touch` tools on macOS. These are checked
before a worker is launched. A retained running attempt with the same lease
fails with `ORCHESTRATION_LEASE_ACTIVE`; wait for expiry and resume so the
controller can renew the same action. An invalid or already-expired absolute
lease fails before worker launch with `ORCHESTRATION_LEASE_INVALID` or
`ORCHESTRATION_LEASE_EXPIRED`. The worker timeout is capped by the lease's
remaining lifetime, even when the configured action timeout is longer.

Workers must not start daemons, hooks, background jobs, or detached
subprocesses; every process started for an action must finish inside that
action. The managed host cleans up the runner's process group, but this is not
a hostile-process-tree containment guarantee. Put an untrusted agent in an
operator-controlled container or VM with its own process and network limits.

If a runner exits after writing valid research artifacts, keep the durable
session. The `orchestrate resume` recovery order is: an accepted canonical result,
an identical clean staged result, then the same persisted action in a fresh
worker only when neither checkpoint exists. The worker checks its
postconditions before doing more work, and the controller still verifies the
artifacts before advancing. Host-owned
`attempts/<attempt_id>.json` records describe bounded execution state;
`.host-results/<action_id>.json` is a private submission checkpoint, not a
canonical result.

Do not fabricate a work-result, edit `session.json`, or redeploy over the
workspace. `CONTROL_ARTIFACT_TAMPERED` records bounded path diagnostics, marks
the attempt, writes
`runs/orchestration-guards/<orchestration_id>.json`, and, when a validated
worker result exists, retains it under `quarantine/` without submitting it.
EvidenceWiki does not automatically restore or roll changes back. A later
managed resume fails with `CONTROL_REPAIR_REQUIRED` before any controller
command or worker starts. After inspection and restoration, resume with
`--acknowledge-control-repair`. Acknowledgement succeeds only when all
tripwire-protected controls match the pre-action fingerprint; otherwise it
fails with
`CONTROL_REPAIR_MISMATCH`. The flag does not accept quarantine or bypass the
controller's per-action trusted-input fingerprint. Start a new session for an
intentional static-control change. If a retained tampered attempt has no
durable baseline, acknowledgement fails closed with
`CONTROL_REPAIR_BASELINE_MISSING`; preserve that session for inspection and
start a new orchestration from reviewed workspace state. See
`workspace-template/docs/orchestration.md` for the upgrade and recovery steps.

Discovery and candidate review are metadata-only phases. At issuance and
submission, the controller compares a bounded SHA-256 content snapshot of the
configured raw roots (at most 10,000 files and 2 GiB) and both the record count
and exact content digest of the evidence manifest (`sources/manifest.jsonl`, at
most 32 MiB). The candidate store may change; the raw evidence tree and
evidence manifest may not change until acquisition.

Generated run reports now belong under `runs/run-reports/`, which remains a
worker-writable output surface. Reports already present under
`docs/run-reports/` remain historical, read-only inputs.

For a session created by 0.2.0, upgrade the package and managed workspace
scripts before replaying its pending action:

```bash
python -m pip install --upgrade evidence-wiki==0.2.1
evidence-wiki upgrade --target . --dry-run
evidence-wiki upgrade --target .
evidence-wiki orchestrate resume \
  --target . \
  --orchestration-id ORCH_ID \
  --runner codex \
  --agent-id ORIGINAL_AGENT_ID
```

Inspect the durable parent session and export the answer:

```bash
evidence-wiki orchestrate status --target . --format json
evidence-wiki export --target . --format json
```

The export contains question state, the answer summary and page, confidence,
evidence strength, and citations resolved to provenance-tracked manifest and
normalized records. If no allowed provider can satisfy a request, orchestration
stops as `blocked_on_sources` with machine-readable remediation instead of
inventing an answer.

### Local-Files-Only Alternative

Discovery and acquisition are optional. For a local-only workspace, omit all
provider flags, deliver reviewed files with provenance sidecars under the
configured `raw/` roots, then run:

```bash
python3 scripts/source_inventory.py --report
python3 scripts/normalize_sources.py --all
```

Inventory and normalization process files that are already present. They never
search the internet or download evidence. Point an agent at
`skills/research-run.md` after normalization, or use the model-neutral
`orchestrate start` / `next` / `submit` protocol from an external harness.

## Drive It With An Agent

The repository is the substrate; the intelligence comes from whatever agent
runs it. Every created workspace ships an `AGENTS.md` operating contract (with
a `CLAUDE.md` pointer for Claude-style agents) and executable skill playbooks
under `skills/` — `research-init`, `research-ingest`, `research-answer`,
`research-run`, and friends. The system is developed and exercised end to end
with two harness families, and exposes a third integration surface:

- **Claude Code** — reads `CLAUDE.md`/`AGENTS.md` and the workspace skills
  directly. This is the pairing used for the project's live evaluation runs.
- **Codex-style `AGENTS.md` agents** — the same contract files drive
  Codex-like harnesses; the project's offline regression runs use this
  pairing.
- **Any MCP client** — `evidence-wiki serve-mcp` exposes status, retrieval,
  question intake, answer export, and source-request listing over stdio
  without changing the canonical CLI/script contracts.

### How This Project Was Built

EvidenceWiki was planned, written, and tested entirely with AI coding agents.
Most of the work was done with OpenAI Codex using GPT-5.5 and GPT-5.6, with
Anthropic Claude also used for parts of the project. No code in this repository
was manually authored by a human.

A concrete end-to-end pairing, matching the tour above:

1. Deploy with reviewed discovery and acquisition provider allow-lists.
2. Seed questions yourself or let an agent submit the validated batch.
3. Run `evidence-wiki orchestrate run --runner codex|claude`. Each worker gets
   one persisted, bounded work order and the relevant workspace skill.
4. Inspect `evidence-wiki orchestrate status` and collect
   `evidence-wiki export --target PATH`.

### Agent-Assisted Setup

For the minimal-preparation path, ask an agent to use `workspace-template/skills/research-init.md`. The agent should ask at most three setup questions:

1. Target workspace path or short project name.
2. Research scope and desired outcome.
3. Optional starting sources, URLs, folders, repositories, or existing wiki.

The agent then writes a reviewable setup profile and runs the initializer with `--profile`. Profile-driven initialization writes `docs/workspace-init-report.md` in the created workspace so the inferred decisions, skipped decisions, validation commands, and next actions are visible without reading the conversation.

Example profile flow:

```bash
evidence-wiki init \
  --profile /path/to/workspace-init.yml \
  --dry-run

evidence-wiki init \
  --profile /path/to/workspace-init.yml
```

Profile schema details are in `workspace-template/docs/workspace-init-profile.md`.

### Orchestrating From a Parent Agent

A PM, planner, or parent agent can use the persisted orchestration protocol
without depending on either built-in runner:

```bash
evidence-wiki orchestrate start --target PATH --agent-id parent-agent --format json
evidence-wiki orchestrate next --target PATH --orchestration-id ORCH_ID --format json
evidence-wiki orchestrate submit --target PATH --orchestration-id ORCH_ID \
  --action-id ACTION_ID --result-file result.json --format json
evidence-wiki orchestrate status --target PATH --orchestration-id ORCH_ID --format json
```

`next` is idempotent: after a crash it returns the same unfinished work order.
`submit` treats the worker result as a claim and verifies the actual workspace
artifacts before advancing. A terminal `blocked_on_sources` child run is never
reopened; the parent session can retain it and start a later run after evidence
acquisition.

The `research-orchestrate` skill in `orchestrator/skills/` documents this
protocol for external agents. The machine contract remains
`workspace-template/docs/orchestrator-handoff.md`.

These orchestrator skills ship with the package but are never copied into a
created workspace. Locate the playbook without a source checkout:

```bash
evidence-wiki orchestrator-guide            # print the resolved skill path
evidence-wiki orchestrator-guide --print    # print the skill content
evidence-wiki orchestrator-guide --format json
```

## What This Repository Contains

- `workspace-template/`: reusable starter copied into each new research workspace.
- `domain-packs/`: optional reusable domain guidance, including `llm-research`,
  `general-science`, and `legal-regulatory`.
- `examples/`: public-safe worked research workspaces built from synthetic evidence.
- `tests/`: regression tests and small, rights-inventoried synthetic fixtures for initialization, inventory, normalization, linting, and end-to-end workspace bootstrap.

The template separates three evidence layers:

- `raw/`: immutable original source material.
- `sources/`: generated manifests, normalized source records, cards, and optional codebase-analysis artifacts.
- `wiki/`: maintained research knowledge that cites source IDs.

## Requirements

Required:

- Python 3.10 or newer.
- PyYAML, importable as `yaml`.

Optional:

- Poppler `pdftotext` for PDF-only normalization.
- Codex CLI 0.138 or newer, or Claude Code on macOS, Linux, WSL2, or in a
  container, for
  `evidence-wiki orchestrate run`; the
  model-neutral `start` / `next` / `submit` / `status` protocol does not require
  either built-in runner.
- `agent-wiki-cli` / `llm-wiki` for manually generated codebase-analysis artifacts. The repository does not install or run this adapter automatically.
- Git, for normal version-control workflows and optional user-edit snapshots.

Quick dependency check:

```bash
evidence-wiki doctor --format json
```

From inside an initialized workspace, the same preflight is available without
the package entry point:

```bash
python3 scripts/doctor.py --format json
```

The doctor report is machine-readable and explains degraded optional
capabilities. For example, if Poppler is missing, it reports that PDF normalization degrades to stubs for PDF-only records.

## Workspace Commands

Install the package from PyPI:

```bash
python3 -m pip install evidence-wiki
```

Create a new research workspace from explicit fields:

```bash
evidence-wiki init \
  --target ../my-research-workspace \
  --project-name my-research-workspace \
  --project-description "Research workspace for a specific topic" \
  --owner-goal "Build a source-grounded knowledge base for decisions"
```

Preview before writing files:

```bash
evidence-wiki init \
  --target ../my-research-workspace \
  --project-name my-research-workspace \
  --project-description "Research workspace for a specific topic" \
  --dry-run
```

Create a workspace with the LLM research domain pack:

```bash
evidence-wiki deploy \
  --target ../llm-research-workspace \
  --project-name llm-research-workspace \
  --project-description "Research workspace for LLM research systems" \
  --domain-pack llm-research
```

Refresh an existing workspace's starter-managed tooling (`scripts/`) to the
installed package version after upgrading `evidence-wiki`:

```bash
evidence-wiki upgrade --target ../my-research-workspace --dry-run   # preview
evidence-wiki upgrade --target ../my-research-workspace             # apply
```

`upgrade` overwrites only starter-managed files. Add `--include skills` or
`--include docs` to refresh those optional reusable directories; if an overlapping
optional file has local edits, upgrade refuses unless `--force-optional` is set.
Forced optional replacements preserve the displaced file under `.replaced/<path>`.
It never touches `research.yml`, `raw/`, `sources/`, `wiki/`, `index.md`, or your
`log.md` content.

For source-checkout development, run tests from the repository root:

```bash
python3 -B -m unittest discover -s tests -p 'test_*.py'
```

The equivalent source-checkout initializer command is:

```bash
python3 workspace-template/scripts/init_research_workspace.py \
  --target ../my-research-workspace \
  --project-name my-research-workspace \
  --project-description "Research workspace for a specific topic" \
  --owner-goal "Build a source-grounded knowledge base for decisions"
```

## Worked Example

See `examples/urban-heat-resilience-workspace/` for a complete public-safe
workspace. It uses synthetic urban heat resilience evidence, reserved
`example.org` URLs, normalized source records, source notes, claims, synthesis,
questions, decisions, and an example output without private pilot data or
machine-specific paths.

## Validate A Created Workspace

Run these commands from the created workspace root:

```bash
python3 scripts/doctor.py --format json
python3 scripts/smoke_validate_workspace.py --format text
python3 scripts/source_inventory.py --report
python3 scripts/normalize_sources.py --all --dry-run
python3 scripts/lint.py --format text
```

Doctor should be `ok` or only `degraded` for optional capabilities before
running unattended workflows. Smoke validation should pass before inventory,
normalization, or broader lint checks. `source_inventory.py --report` writes
`sources/manifest.jsonl` so the following normalization dry run has real
manifest records to inspect. normalize_sources.py --all --dry-run reads `sources/manifest.jsonl` and previews generated normalized records without writing them.

For one aggregate machine-readable health and progress document (including a
completion verdict for orchestrators and parent agents), run:

```bash
python3 scripts/workspace_status.py --format json
python3 scripts/workspace_status.py --check-complete --format json
```

From outside a workspace, the installed package exposes the same status and
export contracts plus contract negotiation, fleet status, and domain-pack
validation:

```bash
evidence-wiki status --target ../my-research-workspace --format json --no-cache
evidence-wiki export --target ../my-research-workspace --format json
evidence-wiki contract
evidence-wiki fleet-status --target ../my-research-workspace --format json
evidence-wiki pack validate --path general-science --format json
```

`evidence-wiki status` forwards to the copied `workspace_status.py` contract;
`evidence-wiki export` is the concise alias for `evidence-wiki questions
export`. Both retain the workspace script schema and exit behavior.

To inject question batches into a running workspace and export structured
answers with citations (schemas in
`workspace-template/docs/question-api.md`), run:

```bash
python3 scripts/intake_questions.py --from-file batch.yaml --dry-run
python3 scripts/intake_questions.py --from-file batch.yaml --format json
python3 scripts/export_answers.py --format json
```

The same operations are available from outside the workspace via
`evidence-wiki questions add|export --target PATH`.

For MCP-speaking orchestrators, the optional stdio server exposes status,
question status, retrieval, question intake, answer export, and source-request
listing without changing the canonical CLI/script contracts:

```bash
evidence-wiki serve-mcp --target /path/to/workspace
```

See `workspace-template/docs/mcp-server.md` for the tool list, handshake,
and read/append-only boundary.

To preview inventory records before writing the manifest, run:

```bash
python3 scripts/source_inventory.py --dry-run --report
```

## Add Sources

### Enable Source Providers

Discovery and acquisition are separate permissions:

| Phase | Provider | Purpose | Runtime configuration |
|---|---|---|---|
| Discovery | `arxiv` | Search arXiv metadata and propose paper candidates. | No credential. Metadata only; no download. |
| Discovery | `openalex` | Search the OpenAlex scholarly index and propose paper candidates. | Optional `OPENALEX_API_KEY` in the environment. Index metadata is not itself evidence. |
| Discovery | `github` | Search repository metadata and propose code candidates. | Optional `GITHUB_TOKEN` in the environment. Never clones during discovery. |
| Discovery | `search` | Run a configured general-search backend. | A reviewed fixture, argv command, or HTTP backend in the setup profile; backend secrets stay in its environment. |
| Discovery | `standards` | Propose bounded registry candidates. | Registry-specific configuration; it does not grant rights to standards text. |
| Acquisition | `arxiv` | Download selected PDF and source-bundle evidence. | No credential; per-paper copyright and license still apply. |
| Acquisition | `openalex` | Resolve works, enrich records, and download selected open-access PDFs. | Optional `OPENALEX_API_KEY`; unavailable or uncertain OA routes fail closed. |
| Acquisition | `github` | Capture selected metadata, releases, or bounded source archives. | Optional `GITHUB_TOKEN`; repository license remains authoritative. |
| Acquisition | `web` | Capture one explicitly selected HTTPS resource. | A non-empty domain allow-list and byte limit; license cannot be inferred. |

The three controls are independent:

1. `integrations.discovery` authorizes candidate metadata lookup.
2. `integrations.acquisition` authorizes retrieval of selected evidence.
3. Environment credentials authenticate an already-authorized provider.

A token, installed runner, domain-pack recommendation, or discovered URL never
grants provider permission. The orchestrator uses only the configured
allow-lists and never edits them during a run. arXiv is primarily a preprint
host and OpenAlex is a scholarly index; neither provider alone establishes that
a work was peer reviewed.

Repeated initializer flags are the concise opt-in. When a flag is present for a
phase, it replaces that phase's profile allow-list and sets `enabled: true`:

```bash
evidence-wiki deploy \
  --target literature-workspace \
  --project-name literature-workspace \
  --project-description "Agent-acquired literature review" \
  --discovery-provider arxiv \
  --discovery-provider openalex \
  --acquisition-provider arxiv \
  --acquisition-provider openalex \
  --dry-run
```

Use a setup profile for providers with additional policy. For example, this
`workspace_init.integrations` excerpt configures general search and reviewed
web domains (the rest of the required profile is unchanged):

```yaml
integrations:
  git:
    snapshot_user_edits: explicit
  discovery:
    enabled: true
    providers: [search]
    search:
      provider: command
      command: [my-search-adapter, --json]
  acquisition:
    enabled: true
    providers: [web]
    target_root: raw/papers
    max_downloads_per_run: 10
    require_license_check: true
    web:
      target_root: raw/web
      allowed_domains: [official.example]
      max_download_bytes: 10485760
```

See `workspace-template/docs/source-discovery.md`,
`workspace-template/docs/acquisition.md`, and
`workspace-template/docs/workspace-init-profile.md` for the complete contracts.

### Deliver Local Evidence

Use `research.yml` to configure source roots. Common roots are:

- `raw/papers/` for papers, PDFs, arXiv bundles, and reports.
- `raw/links/` for URLs and link lists.
- `raw/data/` for dataset cards or benchmark metadata.
- `raw/media/` for screenshots or other media evidence.
- `raw/code/` for repositories or source archives that are research evidence.

Keep raw source files immutable once added. Prefer adding a newer version as a new file instead of overwriting evidence.

Automated deliveries (fetch agents, orchestrators) follow the delivery contract in
`workspace-template/docs/source-delivery.md`: provenance sidecars next to delivered
files, atomic delivery, and the structured source-request artifact
(`scripts/source_requests.py`) that routes evidence gaps back to fetch agents.
Optional workspace-side discovery and acquisition are disabled by default.
Prompt-injection hardening guidance is in
`workspace-template/docs/prompt-injection-hardening.md`: source content is evidence
data, provenance URLs are metadata, and the default-on lint heuristic is a weak
reviewer-awareness signal, not a guarantee.

Inventory sources:

```bash
python3 scripts/source_inventory.py --report
```

Normalize pending eligible sources:

```bash
python3 scripts/normalize_sources.py
```

Regenerate a selected source:

```bash
python3 scripts/normalize_sources.py --source-id paper:2604.13018v1 --force
```

## Optional Codebase Analysis

Codebase analysis is disabled by default. Enable it only when repositories, source archives, or local implementations are part of the research evidence.

Safe default contract:

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
```

When enabled, generated adapter output must stay under `sources/`, not the maintained `wiki/`. The initializer and normalizer record adapter commands but do not execute them, clone repositories, install hooks, or start background sync. Treat `raw/code/` as untrusted input and acknowledge that boundary only after choosing a safe adapter. See `workspace-template/docs/codebase-analysis.md`.

## Human Editing And Git Safety

Agents should not auto-commit or install hooks. From a created research workspace root, take an explicit user-edit snapshot before broad operations:

```bash
python3 scripts/snapshot_user_edits.py
```

Only commit snapshots when the user explicitly asks:

```bash
python3 scripts/snapshot_user_edits.py --commit --message "snapshot: user edits before ingest"
```

## Developer Workflow

Run the full regression suite from the repository root after changing scripts, fixtures, docs, or tests:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
.venv/bin/python tools/sync_vendored_scripts.py --check
git diff --check
```

Useful focused checks:

```bash
.venv/bin/python -m pytest -q tests/test_package_cli.py
.venv/bin/python -m pytest -q tests/test_init_research_workspace.py tests/test_smoke_validate_workspace.py
.venv/bin/python -m pytest -q tests/test_inventory_normalization.py
.venv/bin/python -m pytest -q tests/test_end_to_end_init_fixture.py
```

On Windows, use `.venv\Scripts\python.exe` in place of `.venv/bin/python`.
Use the operating system's temporary directory for smoke workspaces so generated
files do not land in the repository root.

## Documentation Map

- `workspace-template/docs/new-project-guide.md`: full project setup guide.
- `workspace-template/docs/workspace-initialization.md`: initialization CLI and setup profile workflow.
- `workspace-template/docs/workspace-init-profile.md`: setup profile schema.
- `workspace-template/docs/orchestrator-handoff.md`: machine contract for external orchestrators and parent agents.
- `workspace-template/docs/orchestration.md`: durable parent sessions, work orders, managed runners, and recovery.
- `workspace-template/docs/workspace-status.md`: aggregate status document schema and completion verdict.
- `workspace-template/docs/research-yml.md`: public configuration contract.
- `workspace-template/docs/source-manifest.md`: source inventory format.
- `workspace-template/docs/normalized-source-format.md`: normalized record format.
- `workspace-template/docs/coverage-manifest.md`: per-question answerability manifest schema.
- `workspace-template/docs/acquisition.md`: optional acquisition safety model and provider registry.
- `workspace-template/docs/source-discovery.md`: candidate-discovery contract and `source_candidate` schema (proposals before acquisition).
- `workspace-template/docs/codebase-analysis.md`: optional codebase evidence workflow.
- `workspace-template/docs/production-readiness-checklist.md`: sustained-use readiness review.

## Contributing

See `CONTRIBUTING.md` for local setup, test expectations, style rules, and pull
request guidance.
