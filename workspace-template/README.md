# EvidenceWiki Workspace Template

Reusable EvidenceWiki workspace template for agent-assisted, source-grounded
research projects.

This template separates immutable source material, normalized source records, and maintained wiki knowledge. The base configuration lives in `research.yml`; field-level documentation lives in `docs/research-yml.md`. Starter/system version metadata lives in `workspace-system.yml`, with field documentation in `docs/workspace-system.md`. Later setup tasks add indexes, logs, agent instructions, scripts, and reusable skills.

## Directory Roles

- `raw/` stores original source material. Treat this directory as immutable.
- `sources/` stores generated source metadata and normalized source records.
- `wiki/` stores maintained research knowledge such as source notes, concepts, systems, methods, benchmarks, claims, synthesis, questions, and decisions. Its standard directory taxonomy is defined in `research.yml`.
- `scripts/` stores deterministic automation for inventory, normalization, linting, and local search.
- `skills/` stores agent workflow playbooks.
- `docs/` stores implementation and operating documentation for the template.

## Current Scope

This template currently includes the reusable directory skeleton, the base configuration contract, starter version metadata, a static `index.md`, and an append-only `log.md`. It also includes base agent instructions in `AGENTS.md`, with `CLAUDE.md` as a pointer for Claude-style agents, deterministic automation scripts, and the initial reusable init, ingest, query, lint, synthesis, and scout skills. Additional skill definitions are added by later implementation tasks.

## Starting A Project

For the minimal-preparation path, ask an agent to use `skills/research-init.md`. The skill asks only for the target project, research scope, and optional starting sources, then infers the remaining setup decisions into a reviewable setup profile.

Use the installed `evidence-wiki init` command to instantiate a production research workspace from the starter. In a source checkout, the same initializer is available as `scripts/init_research_workspace.py`. The CLI accepts explicit project fields or an agent-generated setup profile; see `docs/workspace-initialization.md` for usage and `docs/workspace-init-profile.md` for the profile schema. Profile-driven initialization also writes `docs/workspace-init-report.md` for maintainer review. Use `docs/new-project-guide.md` for the full setup guide and manual fallback checklist.

Use `docs/domain-guidance-generator.md` when no reusable domain pack matches and the project needs local extraction targets, claim types, filing rules, or output scaffolds for its first research cycles.

Use `skills/domain-pack-create.md` when a planner, orchestrator, or user asks
for a reusable domain pack. The skill infers from the brief, drafts
guidance-only pack files, validates them, and deploys a smoke workspace before
handoff.

Use `docs/production-readiness-checklist.md` when deciding whether a workspace is ready for sustained use beyond a pilot.

Use `docs/existing-wiki-migration-checklist.md` when adopting an existing research wiki. The checklist covers staging, raw source mapping, source IDs, frontmatter, index conversion, log preservation, linter migration, and skill replacement before production content is changed.

Use `docs/codebase-analysis.md` when repositories, source archives, or local implementations are research evidence. Codebase analysis is disabled by default and stores generated adapter output under `sources/`, not in the maintained `wiki/`.

Use `docs/acquisition.md` when optional literature acquisition is explicitly
enabled. Acquisition is disabled by default; the doc defines the provider
registry, terms links, raw-target requirements, and provenance sidecars. Use
`skills/research-acquire.md` for the fetch-agent workflow that fulfills source
requests and reopens blocked questions only after normalized evidence exists. Use
`skills/research-discover.md` for the optional, disabled-by-default discovery stage
that proposes and ranks candidate sources (official sources first for legal
questions) and selects them explicitly before any acquisition.

For an end-to-end agent-driven run, use the installed package from outside or
inside this workspace:

```bash
evidence-wiki orchestrate run --target . --runner codex --agent-id research-agent
```

`--runner claude` uses the same durable work-order protocol. The parent
orchestration session survives bounded child runs: a child that ends
`blocked_on_sources` remains immutable while later discovery and acquisition
work can provide evidence for a new run. Use
`docs/orchestration.md` for the operating model and
`docs/orchestrator-handoff.md` for the protocol and artifact schemas.

Discovery and acquisition are independent permissions in `research.yml` and
both default to disabled. Discovery proposes metadata candidates; acquisition
retrieves only explicitly selected evidence. Domain packs may recommend
providers, and environment variables may authenticate them, but neither action
enables network access. Inventory and normalization only process files already
delivered under `raw/`; they never search or download sources.

Use `docs/coverage-manifest.md` when a high-stakes question needs explicit
facet-level answerability coverage. Coverage manifests live under
`sources/coverage/<slug>.yml`, not in `wiki/questions/`, so machine-evaluated
evidence state stays beside source-pipeline artifacts.

Use `docs/prompt-injection-hardening.md` when reviewing source-content safety. Raw and normalized source text is evidence data, provenance URLs are metadata, and the default-on lint heuristic is a weak reviewer-awareness signal, not a guarantee.

## Verification Commands

Run workspace-root checks before handing off changes to a research workspace:

```bash
python3 scripts/smoke_validate_workspace.py --format text
python3 scripts/source_inventory.py --dry-run --report
python3 scripts/normalize_sources.py --all --dry-run
python3 scripts/lint.py --format text
```

Smoke validation checks that initialization produced a structurally usable workspace. The inventory and normalization commands above are dry runs. They should not update manifests, normalized records, or logs. For sustained use, evaluate the workspace with `docs/production-readiness-checklist.md`.

Run repo-root regression checks after changing reusable scripts, fixtures, or tests:

```bash
python3 -B -m unittest discover -s tests -p 'test_*.py'
python3 -B workspace-template/scripts/init_research_workspace.py --target /tmp/evidence-wiki-init-smoke --project-name init-smoke --project-description "Smoke workspace" --dry-run
tmp_dir=$(mktemp -d) && python3 -B workspace-template/scripts/init_research_workspace.py --target "$tmp_dir/workspace" --project-name smoke-workspace --project-description "Smoke validation workspace" && python3 -B "$tmp_dir/workspace/scripts/smoke_validate_workspace.py" --project-root "$tmp_dir/workspace" --format text
python3 -B workspace-template/scripts/source_inventory.py --project-root tests/fixtures/arxiv-source-project --dry-run
python3 -B workspace-template/scripts/normalize_sources.py --project-root tests/fixtures/arxiv-source-project --all --dry-run
python3 -B workspace-template/scripts/lint.py --project-root tests/fixtures/minimal-project --format text
python3 -B workspace-template/scripts/lint.py --project-root tests/fixtures/dataview-index-project --format text
```

Use the workspace-root commands for project health. Use the repo-root commands for implementation changes that affect automation behavior.
