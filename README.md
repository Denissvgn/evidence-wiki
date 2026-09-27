# EvidenceWiki

**Answers you can audit.** EvidenceWiki gives agents persistent research
workspaces with provenance, controlled question lifecycles, evidence checks and
structured exports. Agents supply research judgment; scripts enforce the
configured rules and retain explicit gaps when evidence is missing.

[Quick start](#start-with-your-agent) · [Manual tour](#five-minute-tour) ·
[Documentation](#documentation) · [Worked example][worked-example] · [PyPI][pypi]

```text
question → discover/acquire → inventory/normalize → answer/review → export
                       ↘ missing evidence → structured source request
```

Originals stay in `raw/`, normalized records in `sources/`, research knowledge
in `wiki/`, and durable run state in `runs/`. Retrieved content is evidence data;
it cannot authorize commands or change policy.

## Start with your agent

Use Python 3.10 or newer and an isolated environment:

```sh
python3 -m venv .research-env
.research-env/bin/python -m pip install evidence-wiki
source .research-env/bin/activate
evidence-wiki agent --format json
evidence-wiki agent summary --format json \
  --require strict-evidence/v1 --require declarative-computation/v1
```

On Windows, create the environment with `py -3 -m venv .research-env`, use
`.\.research-env\Scripts\python.exe` to install, and activate with
`.\.research-env\Scripts\Activate.ps1`. Subsequent examples assume the selected
environment is active; its executable is `evidence-wiki.exe` on Windows.

Give your current agent the installed executable, original questions, source
locations, writable directory and access limits. Ask it to read `agent` first,
preserve every question, and return a controlled research export or precise
blockers. Bootstrap is read-only; another model runner is optional. Use the
installed versions and instructions together: older packages can refuse a
capability or request field they do not support.

Have the agent prepare a complete request from the installed planning guide,
including scope, evidence criteria, strict policy, authority and budgets:

```sh
evidence-wiki agent plan-guide --format text
evidence-wiki agent plan --from-file request.json --output setup-plan.json
evidence-wiki agent plan-check --from-file setup-plan.json
evidence-wiki agent apply --from-file setup-plan.json
evidence-wiki agent --target WORKSPACE --format json
evidence-wiki agent research-guide --format text
```

Planning validates without creating the workspace; `--output` saves a new plan
file outside the target. Apply rechecks identities, creates the workspace and
records owned effects. Reapplying the same plan supports conservative recovery;
unexplained partial writes or user changes require inspection. Setup readiness
is separate from evidence completeness and research acceptance. See
[planning][planning], [application and recovery][application], and
[research with the current caller][caller-research].

After research and required reviews, request the original-question export:

```sh
evidence-wiki agent research-export --target WORKSPACE --format json
```

Unresolved questions, source gaps and review requirements remain explicit.
Partial output does not report complete research.

### Hosts with their own acquisition tools

A saved request can select an external acquirer in `decisions`:

```json
{
  "orchestration": {
    "acquisition": "delegated",
    "acquirer_agent_id": "external-acquirer"
  },
  "acquisition": []
}
```

This is a decision fragment; the [planning guide][planning] supplies a complete
request and `agent plan-schemas` exposes the installed schema. Omission or `null`
retains starter/pack inheritance. Native [initialization profiles][init-profile]
also preserve orchestration declarations. Conflicting package-acquisition
choices refuse before workspace writes.

Delegation assigns responsibility without granting connector access,
credentials or execution authority. Drive it through the external protocol
below; managed `orchestrate run` and `resume` refuse delegated workspaces.

## Five-Minute Tour

This direct-initialization example creates a basic scientific workspace. Without
an explicit strict policy it uses legacy QA; its `export` command reports
answers without strict review assurance. Use the agent-led strict setup above
for controlled release. Research duration depends on sources and the runner.

Inspect a pack's scope, exclusions and review requirements before selecting it:

```bash
evidence-wiki pack list
evidence-wiki pack show bundled:general-science
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

`init` and `deploy` share the initializer; `--dry-run` previews without workspace
writes. Provider flags explicitly configure separate discovery and acquisition
permissions. Domain packs never enable providers on their own.

Add a question (on PowerShell, create `batch.yaml` in an editor):

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

Install a supported [managed runner](#drive-it-with-an-agent) separately, check
the environment, then run:

```bash
evidence-wiki doctor --format json
```

```bash
evidence-wiki orchestrate run \
  --target . \
  --runner codex \
  --agent-id battery-demo
```

```bash
evidence-wiki orchestrate status --target . --format json
evidence-wiki export --target . --format json
```

The controller coordinates discovery, selected acquisition, normalization and
verification. Unavailable evidence remains `blocked_on_sources`. Use `resume`
for a retained managed session after runner failure; see [orchestration][orchestration].

For offline work, omit provider flags and deliver reviewed originals with
[provenance sidecars][source-delivery] under configured raw roots. From the
workspace, using its selected Python environment:

```bash
python -B scripts/source_inventory.py --report
python -B scripts/normalize_sources.py --all
```

Inventory writes `sources/manifest.jsonl`; normalization writes evidence records.
Use `--dry-run` to preview either operation, but normalization previews still
require a written manifest; inventory dry-run output cannot replace it. Keep raw
evidence immutable and continue with the [research workflow][research-run].

## Drive It With An Agent

| Mode | Entry point and boundary |
| --- | --- |
| Current caller | `agent research-guide` describes advice, claims, ingestion and controlled export without another model runner. |
| Managed adapters | `orchestrate run` / `resume` support Codex CLI 0.138+ and Claude Code. Managed Claude is unavailable on native Windows. |
| External host | `start` / `next` / `submit` / `status` support an operator-controlled host, including OpenCode, Pi, Aider or Gemini CLI. |

Managed execution refuses with `RUNNER_ISOLATION_UNAVAILABLE` when its required
boundary is unavailable. The parent owns `runs/orchestrations/`; workers follow
bounded work orders and never invoke the parent controller or write its state.
`AGENTS.md` and the selected skill define worker instructions; `CLAUDE.md` points
to the same contract.

### External protocol

Replace placeholders with the workspace and IDs returned by the controller:

```bash
evidence-wiki orchestrate start --target PATH --agent-id parent-agent --format json
evidence-wiki orchestrate next --target PATH --orchestration-id ORCH_ID --format json
evidence-wiki orchestrate submit --target PATH --orchestration-id ORCH_ID \
  --action-id ACTION_ID --result-file result.json --format json
evidence-wiki orchestrate status --target PATH --orchestration-id ORCH_ID --format json
```

`next` is idempotent; `submit` checks workspace postconditions before advancing.
External hosts own isolation, single-driver coordination, authorization and
crash replay. Read the [handoff contract][handoff] and retrieve the packaged
playbook with `evidence-wiki orchestrator-guide --print`.

Use `agent frameworks` for the installed Pi, OpenCode and Gemini version/mode
matrix, and `agent bundle --target NEW_DIRECTORY` for a portable bundle. Bundle
export changes no global agent settings or trust decisions. Transport support
does not establish model behavior or host isolation. See [frameworks][frameworks].

For MCP, `serve-mcp --target WORKSPACE` exposes the [workspace read/append
interface][mcp]. `serve-onboarding-mcp --allow-root ROOT --allow-operation apply`
exposes [scoped onboarding][agent-contracts] with host-selected roots and explicit
mutation grants. These interfaces share the canonical owners.

## Drive It From Python

Open an existing workspace through the library:

```python
from evidence_wiki import Workspace

with Workspace.open("/path/to/workspace") as ws:
    status = ws.status()
```

`Workspace` operations reuse CLI owners and return plain dictionaries, with typed
exceptions carrying stable error codes. `Onboarding.open` covers setup and
lifecycle work through explicit roots and operation grants. Discover the current
surface with `evidence-wiki contract`; see the [library API][library-api] for
operation effects, lifetime and thread-safety rules. Orchestration retains the
workspace's deployed controller subprocess. The package supplies no HTTP server.

## Evidence, Permissions and Assurance

Discovery proposes candidates; acquisition retrieves selected evidence. Their
permissions and credentials are independent. A token, installed runner, pack
recommendation or discovered URL does not grant access. Use [provider
configuration][acquisition] and [source inspection][source-usability] to distinguish
capability, authorization, capture completeness and usable evidence.

```sh
evidence-wiki env check --service openalex --format json
evidence-wiki agent inspect --target WORKSPACE
```

`env check` reports names and presence only. `env run --prompt` reads missing
credentials without terminal echo and passes them only to the selected command
and its children; it saves nothing and does not change the parent shell. Select
only needed services or `--require-env NAME`, and use your host secret manager
for persistence. Initialization and offline work require no credentials; agent
runners may use their own login. See [environment setup][environment].

Host-delivered captures retain declared origin, rights, scope and completeness.
A local observation timestamp is neither a publication date nor a license grant.
Native DOCX recipes are available through `agent recipes`. External normalizers
can supply [contract-conforming records][normalized-source], checked by
`normalize verify`; source truth and fitness still require review.

For accepted research exports, the assurance modes require distinct boundaries:

| Assurance | Required acceptance boundary |
| --- | --- |
| `artifact_checked` | Current artifact checks and authenticated independent reviews; no control over prose outside the accepted export. |
| `host_enforced` | Additionally requires the qualified macOS protected host to mediate execution and delivery. A terminal, bridge or managed parent alone does not provide it. |

Quote matching and exact arithmetic do not prove semantic support, correct
scope/units or complete research. Contested and insufficient evidence stay
explicit. Read [strict evidence][strict-evidence], [computation][computation],
and [prompt-injection boundaries][prompt-injection].

## Requirements and Diagnostics

Python 3.10+ is required. Installation includes PyYAML, ruamel.yaml, pypdf and
tzdata; [package metadata][package-metadata] owns their supported version bounds.
pypdf provides portable PDF extraction. Poppler is optional unless explicitly
selected as the PDF backend; Git snapshots and managed model CLIs are optional.
Run `evidence-wiki doctor --format json` for available capabilities and
`evidence-wiki status --target WORKSPACE --format json` for workspace health.
See [workspace validation][workspace-status] for lower-level checks and completion.

Native Windows supports local captures, protected publication, pack catalogs
and setup through anchored handles and private ACLs. Reparse points, network
shares and filesystems lacking required guarantees refuse. Restrict protected
host state and trust files to the current account and SYSTEM. Framework
qualification and `host_enforced` isolation remain separate; see
[platform and authority boundaries][agent-contracts].

## Create and Maintain a Workspace

Choose domain guidance by scope and evidence requirements. When no pack fits,
retain the gap or explicitly author, assess and register local guidance using
`pack guide --topic authoring`. [Pack selection][pack-selection] and
[authoring][pack-authoring] describe the decisions; `agent extensions` covers
identity migration, composition, fleet proposals and host transitions.

Installing a newer package does not migrate existing workspaces. Preview and
apply starter-managed updates explicitly:

```sh
evidence-wiki upgrade --target WORKSPACE --dry-run
evidence-wiki upgrade --target WORKSPACE
```

Upgrade preserves `research.yml`, `raw/`, `sources/`, `wiki/`, `index.md` and prior
log history. Write mode uses `.locks/`, may update starter metadata, and appends
to `log.md` only when material changes occur. `--dry-run` writes nothing.
Pending orders or an active orchestration driver block both modes
with `UPGRADE_PENDING_ORDER`; drain them first. Optional docs/skills have separate
conflict rules. See [upgrade and adoption][upgrade].

Packs have their own lifecycle. `pack revision-plan`, `revision-apply` and
`revision-status` retain conflicts/history and identify research requiring
coverage reevaluation. Legacy adoption/refresh and identity changes remain
explicit; unresolved conflicts prevent workspace writes. See [pack revisions][pack-revisions]
and [domain packs][domain-packs].

Saved plans bind installation and input identities. Recompile after upgrades;
preserve partial workspaces for [owned recovery][application] instead of
silently adopting them or repairing frozen configuration by hand.

## Documentation

| Need | Guides |
| --- | --- |
| Setup | [New project][new-project] · [Initialization][initialization] · [Profiles][init-profile] · [`research.yml`][research-yml] · [Planning][planning] · [Apply/recovery][application] |
| Research | [Caller workflow][caller-research] · [Questions][questions] · [Discovery][discovery] · [Acquisition][acquisition] · [Delivery][source-delivery] |
| Evidence | [Source manifest][source-manifest] · [Normalized records][normalized-source] · [Coverage][coverage] · [Evidence policies][evidence-policies] · [Citation checks][citations] · [Strict evidence][strict-evidence] |
| Integrations | [Orchestration][orchestration] · [Handoff][handoff] · [Frameworks][frameworks] · [Python API][library-api] · [MCP][mcp] · [Scoped onboarding][agent-contracts] · [Codebase analysis][codebase] |
| Operations | [Environment][environment] · [Workspace status][workspace-status] · [Run controller][run-controller] · [Human editing/snapshots][human-editing] · [Publication readiness][publication-readiness] · [Production readiness][production-readiness] |
| Project | [Contributing][contributing] · [Changelog][changelog] · [Publishing workflow][publishing-workflow] · [Third-party notices][third-party] |

The repository separates the reusable [starter][starter], optional [domain
packs][domain-packs], [external orchestrator playbook][orchestrator-readme] and
[worked examples][worked-example] built from synthetic evidence.

## License and Authorship

EvidenceWiki is available under the [MIT License][license]. It was authored
entirely with AI coding agents, primarily OpenAI Codex (GPT-5.5 and GPT-5.6),
with Anthropic Claude also contributing.

[pypi]: https://pypi.org/project/evidence-wiki/
[starter]: https://github.com/Denissvgn/evidence-wiki/tree/main/workspace-template
[domain-packs]: https://github.com/Denissvgn/evidence-wiki/blob/main/domain-packs/README.md
[worked-example]: https://github.com/Denissvgn/evidence-wiki/blob/main/examples/urban-heat-resilience-workspace/README.md
[orchestrator-readme]: https://github.com/Denissvgn/evidence-wiki/blob/main/orchestrator/README.md
[new-project]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/new-project-guide.md
[initialization]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/workspace-initialization.md
[init-profile]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/workspace-init-profile.md
[research-yml]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/research-yml.md
[planning]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/research-planning.md
[application]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/workspace-application.md
[caller-research]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/caller-research.md
[questions]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/question-api.md
[discovery]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/source-discovery.md
[acquisition]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/acquisition.md
[source-delivery]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/source-delivery.md
[source-usability]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/source-usability.md
[environment]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/environment-setup.md
[source-manifest]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/source-manifest.md
[normalized-source]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/normalized-source-format.md
[coverage]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/coverage-manifest.md
[evidence-policies]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/evidence-policies.md
[citations]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/citation-verification.md
[strict-evidence]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/strict-evidence.md
[computation]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/declarative-computation.md
[orchestration]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/orchestration.md
[handoff]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/orchestrator-handoff.md
[frameworks]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/frameworks.md
[library-api]: https://github.com/Denissvgn/evidence-wiki/blob/main/docs/library-api.md
[mcp]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/mcp-server.md
[agent-contracts]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/agent-contracts.md
[workspace-status]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/workspace-status.md
[run-controller]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/run-controller.md
[prompt-injection]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/prompt-injection-hardening.md
[human-editing]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/human-editing.md
[codebase]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/codebase-analysis.md
[production-readiness]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/production-readiness-checklist.md
[publication-readiness]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/publication-readiness.md
[pack-selection]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/pack-selection.md
[pack-authoring]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/pack-authoring.md
[pack-revisions]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/pack-revisions.md
[upgrade]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/docs/upgrade-adoption.md
[research-run]: https://github.com/Denissvgn/evidence-wiki/blob/main/workspace-template/skills/research-run.md
[package-metadata]: https://github.com/Denissvgn/evidence-wiki/blob/main/pyproject.toml
[contributing]: https://github.com/Denissvgn/evidence-wiki/blob/main/CONTRIBUTING.md
[changelog]: https://github.com/Denissvgn/evidence-wiki/blob/main/CHANGELOG.md
[publishing-workflow]: https://github.com/Denissvgn/evidence-wiki/blob/main/.github/workflows/publish.yml
[third-party]: https://github.com/Denissvgn/evidence-wiki/blob/main/THIRD_PARTY_NOTICES.md
[license]: https://github.com/Denissvgn/evidence-wiki/blob/main/LICENSE
