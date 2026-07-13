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

The goal: a cited, exportable answer with provenance, end to end. Install the
package and create a workspace:

```bash
python3 -m pip install evidence-wiki
evidence-wiki deploy \
  --target solid-state-batteries \
  --project-name solid-state-batteries \
  --project-description "Survey of solid-state battery electrolyte research"
cd solid-state-batteries
```

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
python3 scripts/intake_questions.py --from-file batch.yaml --format json
```

Deliver evidence (here: an HTML page saved next to its provenance sidecar, the
same contract fetch agents use), then inventory and normalize it:

```bash
python3 scripts/source_inventory.py --report
python3 scripts/normalize_sources.py
```

Now let an agent work the backlog — point it at `skills/research-run.md` in
the workspace (see [Drive It With An Agent](#drive-it-with-an-agent)). The
agent claims the question, retrieves evidence from the normalized records,
writes a cited answer page, and resolves the question through the same
deterministic lifecycle scripts a human would use:

```bash
python3 scripts/question_claim.py claim --slug electrolyte-conductivity --agent demo-agent
python3 scripts/question_resolve.py answer --slug electrolyte-conductivity \
  --agent-id demo-agent --answer-page wiki/synthesis/electrolyte-conductivity.md \
  --source-id raw:raw-web-sulfide-electrolyte-review-a3a566db55
```

Export structured answers with citations:

```bash
python3 scripts/export_answers.py --format json
```

Abridged real output — every citation resolves to a raw file and a normalized
record inside the workspace:

```json
{
  "questions": [
    {
      "slug": "electrolyte-conductivity",
      "question": "Which solid electrolyte families report room-temperature ionic conductivity above 1 mS/cm?",
      "status": "answered",
      "answer_page": "wiki/synthesis/electrolyte-conductivity.md",
      "answer_summary": "Sulfide families (argyrodite Li6PS5Cl, LGPS) exceed 1 mS/cm at room temperature; garnet oxides typically do not.",
      "citations": [
        {
          "source_id": "raw:raw-web-sulfide-electrolyte-review-a3a566db55",
          "raw_paths": ["raw/web/sulfide-electrolyte-review.html"],
          "normalized_record": "sources/normalized/raw--raw-web-sulfide-electrolyte-review-a3a566db55.md",
          "title": "Sulfide Solid Electrolytes: A Review"
        }
      ],
      "confidence": "high",
      "evidence_strength": "single_source"
    }
  ]
}
```

When required evidence is missing, the same export shows the question as
`blocked` with machine-readable `blocking_requests` describing exactly what a
fetch agent or human must deliver — that is the system refusing to guess.

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

A concrete end-to-end pairing with Claude Code, matching the tour above:

1. `python3 -m pip install evidence-wiki`, then ask the agent:
   *"Create a research workspace for solid-state battery electrolytes using
   the research-init skill from EvidenceWiki."* The agent asks at
   most three setup questions and runs the initializer with a reviewable
   profile.
2. Seed questions yourself (as above) or let the agent intake them.
3. Ask: *"Work the backlog: follow skills/research-run.md with agent id
   claude-demo."* The agent claims, researches, cites, and resolves inside
   the deterministic lifecycle — or blocks with source requests it cannot
   satisfy.
4. Collect `python3 scripts/export_answers.py --format json` (or
   `evidence-wiki export --target PATH` from outside the workspace).

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

A PM, planner, or parent agent that creates and manages workspaces from the
outside has a dedicated executable playbook: the `research-orchestrate` skill in
`orchestrator/skills/`. It walks the full lifecycle — preflight and contract
negotiation, profile-driven deploy with handoff correlation, question intake,
driving or delegating the unattended run loop, routing blocked sources to a fetch
agent, and collecting cited results — and delegates inside-workspace work to the
workspace's own `skills/research-*.md`. The machine contract it executes is
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
Optional workspace-side acquisition is disabled by default; when explicitly
enabled, provider terms, the `arxiv`/`openalex`/`github` registry, and provenance rules
are documented in `workspace-template/docs/acquisition.md`.
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

## Release Engineering (Maintainers)

See `RELEASING.md` for the local build, verification, and publication checklist.
CI tests supported Python versions on Ubuntu, macOS, and Windows and builds both
distribution formats; it does not publish them automatically. The root
`Containerfile` builds the current source into a small non-root runtime image.
