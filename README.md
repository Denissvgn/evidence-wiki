# EvidenceWiki

**Answers you can audit.** EvidenceWiki creates persistent research workspaces
where agents investigate questions and deterministic scripts enforce
provenance, lifecycle state, and export validation. A validated research
outcome is either a cited, auditable answer or a structured request for missing
evidence.

[Quick start](#five-minute-tour) · [Documentation](#documentation) ·
[Worked example][worked-example] · [PyPI][pypi] · [Contributing][contributing]

### How This Project Was Built

EvidenceWiki was planned, written, and tested entirely with AI coding agents.
Most of the work was done with OpenAI Codex using GPT-5.5 and GPT-5.6, with
Anthropic Claude also used for parts of the project. No code in this repository
was manually authored by a human.

## Why EvidenceWiki

- **Traceable answers.** Every citation resolves through a stable source ID to
  a normalized record and its provenance-tracked original.
- **Evidence-aware failure.** Configured coverage requirements block weakly
  supported answers and produce machine-readable source requests.
- **Deterministic control.** Scripts own critical question-lifecycle
  transitions, validation, and export; agents supply research judgment.
- **Reusable workspaces.** The starter, domain packs, agent skills, and
  orchestration protocol work across research domains and agent harnesses.

```text
question → discover/acquire → inventory/normalize → answer/verify → export
                       ↘ missing evidence → structured source request
```

The workspace keeps original evidence in `raw/`, generated evidence records in
`sources/`, and maintained research knowledge in `wiki/`. Source content is
treated as data, never as agent instructions; see [prompt-injection
hardening][prompt-injection].

## Five-Minute Tour

These commands set up the workflow; research time varies with the question,
providers, and agent runner. The examples use a POSIX-compatible shell; on
Windows, create `batch.yaml` in an editor or adapt that one heredoc step for
PowerShell. Python 3.10 or newer is required.

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

`init` and `deploy` invoke the same workspace initializer. The repeated
provider flags explicitly authorize network-backed discovery and acquisition;
a domain pack never enables providers by itself. arXiv needs no credential,
while OpenAlex can use `OPENALEX_API_KEY` from the process environment. See
[workspace initialization][workspace-initialization], [source
discovery][source-discovery], and [acquisition][acquisition] for the full
contracts.

Add a question using the [question API][question-api]:

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

Codex CLI 0.138 or newer must already be installed for the managed Codex
adapter. Check the environment before launching it:

```bash
evidence-wiki doctor --format json
```

Run the managed orchestrator:

```bash
evidence-wiki orchestrate run \
  --target . \
  --runner codex \
  --agent-id battery-demo
```

Use `--runner claude` for the managed Claude Code adapter. Then inspect the
durable parent session and export the answer:

```bash
evidence-wiki orchestrate status --target . --format json
evidence-wiki export --target . --format json
```

The orchestrator can discover candidate sources, ask an agent to select them,
acquire and normalize the selected evidence, reopen a blocked question, and
verify the final artifacts. If allowed providers cannot satisfy the request,
the session ends as `blocked_on_sources` instead of inventing an answer. The
[orchestration guide][orchestration] covers execution, recovery, and security
boundaries.

### Local-files-only alternative

Discovery and acquisition are optional. Omit provider flags, deliver reviewed
files with provenance sidecars under the configured `raw/` roots, then run:

```bash
python3 scripts/source_inventory.py --report
python3 scripts/normalize_sources.py --all
```

Inventory and normalization process only files already present. Continue with
the [research-run skill][research-run], or use the external protocol described
below. The [source-delivery contract][source-delivery] defines provenance
sidecars and atomic delivery.

## Drive It With An Agent

EvidenceWiki supports agent harnesses at three levels:

- **Managed adapters:** Codex and Claude Code are the registered runners for
  package-owned `run` and `resume` execution.
- **External protocol:** OpenCode, Pi, Aider, Gemini CLI, and other harnesses
  can drive `start`, `next`, `submit`, and `status` from an operator-controlled
  host. They are not package-managed runners.
- **Instruction compatibility:** any worker can follow the workspace
  `AGENTS.md`, selected skill, and bounded work order. `CLAUDE.md` points
  Claude-style agents to the same contract.

Managed Codex execution requires Codex CLI 0.138 or newer. Managed Claude
execution is unavailable on native Windows; use macOS, Linux, WSL2, a
container, or the external protocol. If the required isolation boundary cannot
be enforced, the host returns `RUNNER_ISOLATION_UNAVAILABLE` before starting a
worker. The parent exclusively owns `runs/orchestrations/`; workers never write
that tree or invoke the parent controller. Use `resume` for a retained session
after a runner failure. See [parent orchestration][orchestration] for
isolation, leases, tamper recovery, and upgrade rules.

### External protocol

A PM, planner, or custom host can drive the model-neutral protocol directly:

```bash
evidence-wiki orchestrate start --target PATH --agent-id parent-agent --format json
evidence-wiki orchestrate next --target PATH --orchestration-id ORCH_ID --format json
evidence-wiki orchestrate submit --target PATH --orchestration-id ORCH_ID \
  --action-id ACTION_ID --result-file result.json --format json
evidence-wiki orchestrate status --target PATH --orchestration-id ORCH_ID --format json
```

`next` is idempotent, and `submit` verifies workspace postconditions before
advancing. External hosts must provide process isolation, single-driver
coordination, and crash replay; see the [orchestrator handoff
contract][orchestrator-handoff]. The packaged [research-orchestrate
playbook][research-orchestrate] lives under `orchestrator/skills/` and can be
located without a source checkout:

```bash
evidence-wiki orchestrator-guide
evidence-wiki orchestrator-guide --print
```

For MCP clients, an optional stdio server exposes status, retrieval, question
intake, answer export, and source-request listing:

```bash
evidence-wiki serve-mcp --target /path/to/workspace
```

See the [MCP server contract][mcp-server] for its tool list and read/append-only
boundary.

## Drive It From Python

A host that embeds EvidenceWiki — an ASGI service, a scheduler, a batch worker —
can call the package in-process instead of spawning the CLI per operation:

```python
from evidence_wiki import Workspace

with Workspace.open("/path/to/workspace") as ws:
    report = ws.coverage.evaluate("electrolyte-conductivity")
```

Twenty-six operations return the same documents the matching `--format json`
commands print, and refuse with typed exceptions carrying the same stable error
codes. Both doors render from one seam per operation, so they cannot disagree.
Orchestration keeps a subprocess to the workspace's own deployed controller,
which is version-matched to the session state it owns. The package ships no HTTP
server; hosts build their own. See the [library API][library-api] for the full
surface, the error families, thread-safety guarantees, and a worked embedding
example.

## Requirements and Diagnostics

Required:

- Python 3.10 or newer.
- PyYAML 6.0 or newer, ruamel.yaml 0.19.1 or newer within the 0.19 series, and
  pypdf 6.14 or newer within major version 6. All are installed with
  `evidence-wiki`; ruamel.yaml preserves live YAML comments and quoting during
  pack refresh, while the portable pypdf backend requires no separate PDF tool.

Optional capabilities include Codex CLI or Claude Code for managed runs, Git
for snapshots, and the Poppler compatibility backend for explicitly configured
`pdftotext` extraction. Platform installation is covered by [workspace
initialization][workspace-initialization]; managed-runner sandbox requirements
are covered by [parent orchestration][orchestration].

Check dependencies and optional capabilities from any directory:

```bash
evidence-wiki doctor --format json
```

An initialized workspace includes the same preflight:

```bash
python3 scripts/doctor.py --format json
```

Missing pypdf is a required failure. Missing Poppler is informational unless
the workspace explicitly selects the Poppler compatibility backend.

## Create and Maintain a Workspace

Create a generic workspace from explicit fields:

```bash
evidence-wiki init \
  --target ../my-research-workspace \
  --project-name my-research-workspace \
  --project-description "Research workspace for a specific topic" \
  --owner-goal "Build a source-grounded knowledge base for decisions"
```

Add `--dry-run` to preview without writing files. For minimal-preparation,
agent-assisted setup, ask an agent to follow the [research-init
skill][research-init]; it can prepare a reviewable [workspace init
profile][workspace-init-profile].

After upgrading the package, preview and apply starter-managed script updates:

```bash
evidence-wiki upgrade --target ../my-research-workspace --dry-run
evidence-wiki upgrade --target ../my-research-workspace
```

Write-mode `upgrade` refreshes only starter-managed tooling, may update
`workspace-system.yml`, uses `.locks/`, and conditionally appends one audit
entry to `log.md` when it applies material changes. It preserves prior log
history, `research.yml`, `raw/`, `sources/`, `wiki/`, `index.md`, and other user
data. `--dry-run` writes nothing. Optional skills and docs have additional
conflict rules documented in [workspace initialization][workspace-initialization].

Domain packs have a separate, explicit lifecycle. Preview and apply a new
revision of the already-installed pack with:

```bash
evidence-wiki pack refresh \
  --target ../my-research-workspace \
  --path general-science \
  --dry-run
evidence-wiki pack refresh \
  --target ../my-research-workspace \
  --path general-science
```

An older workspace whose pack predates lifecycle state must first run
`evidence-wiki pack adopt --target ../my-research-workspace --dry-run`, review
the result, and repeat without `--dry-run`. Refresh never switches pack names,
and an unresolved local/pack conflict produces zero writes. See [domain
packs][domain-packs] for adoption, path-specific conflict resolution, and
transaction recovery.

## Validate A Created Workspace

For manual or operator-level validation, the copied workspace exposes its
lower-level checks directly. Run these commands from the workspace root:

```bash
python3 scripts/doctor.py --format json
python3 scripts/smoke_validate_workspace.py --format text
python3 scripts/source_inventory.py --report
python3 scripts/normalize_sources.py --all --dry-run
python3 scripts/normalize_verify.py --format text
python3 scripts/lint.py --format text
```

`source_inventory.py --report` writes `sources/manifest.jsonl`, so
normalize_sources.py --all --dry-run reads `sources/manifest.jsonl` and can
preview normalized records without writing them. For aggregate health and a
machine-readable completion verdict, run:

```bash
python3 scripts/workspace_status.py --format json
python3 scripts/workspace_status.py --check-complete --format json
```

Question intake and structured answer export are also available inside a
workspace:

```bash
python3 scripts/intake_questions.py --from-file batch.yaml --dry-run
python3 scripts/intake_questions.py --from-file batch.yaml --format json
python3 scripts/export_answers.py --format json
```

The installed equivalents are `evidence-wiki status`, `evidence-wiki
questions add`, and `evidence-wiki export`; see [workspace status][workspace-status]
and the [question API][question-api].

To preview inventory records without writing the manifest:

```bash
python3 scripts/source_inventory.py --dry-run --report
```

## Evidence and Provider Permissions

Discovery and acquisition are separate permissions. Discovery providers
(`arxiv`, `openalex`, `github`, `search`, and `standards`) propose metadata;
candidates are not evidence until selected, acquired into `raw/`, and recorded
with provenance. Acquisition providers (`arxiv`, `openalex`, `github`, and
allow-listed `web`) retrieve selected evidence under configured limits.

Three controls remain independent:

1. `integrations.discovery` authorizes candidate lookup.
2. `integrations.acquisition` authorizes retrieval.
3. Environment credentials authenticate an already-authorized provider.

A token, installed runner, domain-pack recommendation, or discovered URL never
grants provider permission. See [source discovery][source-discovery],
[acquisition][acquisition], and the [workspace init
profile][workspace-init-profile] for provider configuration. For reviewed
local evidence, follow the [source-delivery contract][source-delivery], keep
raw files immutable, then inventory and normalize them.

Evidence is not limited to the source kinds this package extracts. [Normalized
records][normalized-source] are a versioned public contract, so an external
normalizer can supply records for evidence the package does not read itself —
structured API payloads, instrument output — and those records count on exactly
the same terms as records the package wrote. The terms are enforced, not assumed:
`evidence-wiki normalize verify` checks a record against the contract and names
each breach with a stable code, and lint accepts an externally written record only
when it conforms.

## Repository Layout

- [`workspace-template/`][workspace-template] is copied into each research
  workspace and contains its scripts, skills, and operator documentation.
- [`domain-packs/`][domain-packs] contains optional, reusable domain guidance.
- [`examples/`][examples] includes a [complete public-safe
  workspace][worked-example] built from synthetic evidence.
- [`orchestrator/`][orchestrator-readme] contains the external parent-agent
  playbook.
- [`tests/`][tests] contains regression tests and synthetic fixtures with
  documented usage rights.

## Documentation

- **Start a workspace:** [new project guide][new-project], [workspace
  initialization][workspace-initialization], [setup profile
  schema][workspace-init-profile], [`research.yml` configuration][research-yml],
  [domain packs][domain-packs], and the [worked example][worked-example].
- **Research and evidence:** [question API][question-api], [source
  discovery][source-discovery], [acquisition][acquisition], [source
  delivery][source-delivery], [source manifest][source-manifest], [normalized
  records][normalized-source], [coverage manifests][coverage-manifest],
  [evidence policies][evidence-policies], and [citation
  verification][citation-verification].
- **Agents and integrations:** [parent orchestration][orchestration],
  [orchestrator handoff][orchestrator-handoff], [workspace
  status][workspace-status], [run controller][run-controller], [library
  API][library-api], [MCP server][mcp-server], and [orchestrator
  playbooks][orchestrator-readme].
- **Safety and operations:** [prompt-injection hardening][prompt-injection],
  [human editing and snapshots][human-editing], [codebase
  analysis][codebase-analysis], [production readiness][production-readiness],
  and [publication readiness][publication-readiness].
- **Project development:** [architecture index][architecture],
  [contributing][contributing], [changelog][changelog], [release
  process][releasing], [third-party notices][third-party], and [license][license].

Development setup, repository boundaries, style rules, and the full verification
suite are documented in [CONTRIBUTING.md][contributing].

## License

EvidenceWiki is available under the [MIT License][license].

[pypi]: https://pypi.org/project/evidence-wiki/
[workspace-template]: https://github.com/Denissvgn/evidence-wiki/tree/main/workspace-template
[domain-packs]: https://github.com/Denissvgn/evidence-wiki/blob/main/domain-packs/README.md
[examples]: https://github.com/Denissvgn/evidence-wiki/tree/main/examples
[worked-example]: https://github.com/Denissvgn/evidence-wiki/blob/main/examples/urban-heat-resilience-workspace/README.md
[orchestrator-readme]: https://github.com/Denissvgn/evidence-wiki/blob/main/orchestrator/README.md
[tests]: https://github.com/Denissvgn/evidence-wiki/tree/main/tests
[new-project]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/new-project-guide.md
[workspace-initialization]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/workspace-initialization.md
[workspace-init-profile]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/workspace-init-profile.md
[research-yml]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/research-yml.md
[question-api]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/question-api.md
[source-discovery]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/source-discovery.md
[acquisition]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/acquisition.md
[source-delivery]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/source-delivery.md
[source-manifest]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/source-manifest.md
[normalized-source]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/normalized-source-format.md
[coverage-manifest]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/coverage-manifest.md
[evidence-policies]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/evidence-policies.md
[citation-verification]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/citation-verification.md
[orchestration]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/orchestration.md
[orchestrator-handoff]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/orchestrator-handoff.md
[workspace-status]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/workspace-status.md
[run-controller]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/run-controller.md
[library-api]: https://github.com/Denissvgn/evidence-wiki/blob/main/docs/library-api.md
[mcp-server]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/mcp-server.md
[prompt-injection]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/prompt-injection-hardening.md
[human-editing]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/human-editing.md
[codebase-analysis]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/codebase-analysis.md
[production-readiness]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/production-readiness-checklist.md
[publication-readiness]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/publication-readiness.md
[research-init]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/skills/research-init.md
[research-run]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/skills/research-run.md
[research-orchestrate]: https://github.com/Denissvgn/evidence-wiki/blob/main/orchestrator/skills/research-orchestrate.md
[architecture]: https://github.com/Denissvgn/evidence-wiki/blob/main/docs/llm_wiki/index.md
[contributing]: https://github.com/Denissvgn/evidence-wiki/blob/main/CONTRIBUTING.md
[changelog]: https://github.com/Denissvgn/evidence-wiki/blob/main/CHANGELOG.md
[releasing]: https://github.com/Denissvgn/evidence-wiki/blob/main/RELEASING.md
[third-party]: https://github.com/Denissvgn/evidence-wiki/blob/main/THIRD_PARTY_NOTICES.md
[license]: https://github.com/Denissvgn/evidence-wiki/blob/main/LICENSE
