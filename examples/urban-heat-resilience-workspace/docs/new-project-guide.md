# New Research Project Guide

Use this guide to create a production research workspace from the reusable starter. The goal is to make the first workspace decisions explicit before agents ingest sources or create maintained wiki pages.

## Fast Path: Agent-Assisted Initialization

The preferred setup path is to ask an agent to use `skills/research-init.md`.
The agent should ask at most three setup questions:

1. Target workspace path or short project name.
2. Research scope and desired outcome.
3. Optional starting sources, URLs, folders, repositories, or existing wiki.

The agent should infer the remaining setup decisions, write a reviewable setup profile, configure the workspace, and run validation. The setup profile and generated `docs/workspace-init-report.md` are the audit surface for what the agent inferred, which questions it asked, which checks ran, and what remains to do. This keeps user preparation minimal while preserving a reviewable `research.yml`.

Agent-assisted initialization should infer:

- project name, description, owner goal, and language;
- relevant raw source roots;
- default source lifecycle policy;
- generic wiki taxonomy or matching domain pack;
- claim strictness;
- output formats;
- optional codebase-analysis settings;
- validation and next research steps.

Use `evidence-wiki init` as the preferred deterministic creation path. From a source checkout, use `scripts/init_research_workspace.py` directly. The manual sections below are the fallback checklist and the audit surface for reviewing what the agent prepared.

## 1. Create The Workspace

Preferred installed command:

```bash
evidence-wiki init \
  --target ../my-research-workspace \
  --project-name my-research-workspace \
  --project-description "Research workspace for a specific topic"
```

Use `--profile <path>` when an agent has prepared a setup profile. See `docs/workspace-initialization.md` for CLI usage and `docs/workspace-init-profile.md` for the profile schema. Review the profile or run `--dry-run` before writing workspace files when the inferred decisions need human confirmation. Profile-driven initialization also writes `docs/workspace-init-report.md` unless the profile sets a different workspace-relative `init_report.path`.

Manual fallback:

1. Copy the starter directory to the new project location.
2. Keep the copied directory as the workspace root.
3. Initialize git if the workspace is not already tracked.
4. Read `AGENTS.md`, `workspace-system.yml`, `research.yml`, `index.md`, and
   `log.md`.
5. Leave `raw/` empty until the source policy is defined.

Recommended first commit:

```bash
git add .
git commit -m "init research workspace"
```

Do not copy pilot data, prototype wiki pages, or domain-specific assumptions unless this project intentionally continues that research scope.

Keep `workspace-system.yml` at the workspace root. It records the reusable starter version and compatible `research.yml` contract so later initialization, smoke validation, and migration tooling can identify the workspace system without running code.

## 2. Define The Project Contract

Edit `research.yml` before running automation. Treat it as the public contract for agents, scripts, and human operators.

Required project decisions:

| Decision | Where | Guidance |
|----------|-------|----------|
| Project identity | `project.name`, `project.description` | Use a short stable name and a one-sentence scope. |
| Research goal | `project.owner_goal` | State the practical question the workspace must answer. |
| Working language | `project.language` | Choose the default language for generated notes and outputs. |
| Raw source roots | `raw.source_roots` | List every immutable source directory that inventory should scan. |
| Raw immutability | `raw.immutable` | Keep `true` for production evidence unless the user explicitly wants source cleanup. |
| Source status policy | `sources.default_status`, `sources.lifecycle_statuses` | Decide how sources move from discovery to integration or rejection. |
| Wiki taxonomy | `wiki.required_dirs`, `wiki.allowed_page_types`, `taxonomy` | Keep the generic taxonomy unless the project has repeated page types that need dedicated directories. |
| Claim strictness | `ingest.claim_extraction`, `lint.validate_claims`, `wiki.frontmatter_type_rules` | Use structured claims for evidence that may conflict, expire, or drive decisions. |
| Output types | `outputs.default_dir`, `outputs.supported_formats` | Define where reusable reports, tables, decks, exports, or decision artifacts belong. |
| Integrations | `integrations` | Enable only the tools the workspace will actually use. |
| Codebase analysis | `integrations.codebase_analysis` | Enable only when repositories are research sources. Keep generated codebase output under `sources/`. |

Keep top-level `research.yml` sections stable. Add domain-specific behavior by extending lists, adding nested keys, or applying a domain pack overlay.

If the agent inferred these values, review the init report instead of re-answering every setup question. If validation results were pending at creation time, update the report after smoke validation, inventory dry-run, normalization dry-run, and lint.

## 3. Choose Source Roots

Source roots are the evidence boundary for inventory and normalization. Define them before adding files.

Common root patterns:

- `raw/papers/` for PDFs, arXiv bundles, technical reports, and source code archives.
- `raw/links/` for curated URLs, saved web references, or link lists.
- `raw/datasets/` for dataset cards, metadata, or downloaded benchmark descriptions.
- `raw/notes/` for user-provided source material that should be preserved as evidence.
- `raw/code/` for code repositories or source archives that are part of the research evidence.

Production rules:

- Treat raw files as immutable once committed.
- Prefer adding newer versions as new files instead of overwriting old ones.
- Keep source filenames stable enough for inventory to produce repeatable IDs.
- Record manual exclusions as `rejected` sources instead of deleting evidence.

## 4. Set The Wiki Taxonomy

The default wiki taxonomy supports source notes, entities, concepts, methods, systems, benchmarks, datasets, claims, synthesis, questions, decisions, and outputs. Start with the default taxonomy unless the project has a clear, repeated need for extra page types.

Add a directory only when all of these are true:

- the page type appears often,
- the page type does not fit an existing directory,
- agents need different extraction or lint rules for it,
- `research.yml` and the filesystem can be updated together.

Use domain packs for domain-specific taxonomy. For example, the `llm-research` pack adds LLM research extraction guidance, while a `climate-policy` pack could add jurisdiction-specific regulatory rules without changing the reusable core.

If no domain pack matches, the agent can use `domain_guidance.mode:project_local` in the setup profile to generate lightweight project-local guidance during initialization. Keep it local unless the user requests a reusable pack or the same extraction targets, source priorities, claim types, output scaffolds, and filing rules repeat across workspaces. See `docs/domain-guidance-generator.md` for the workflow.

## 5. Define Source Status Policy

Use lifecycle states to make source progress visible. The default policy is:

- `discovered`: found in raw inputs but not processed.
- `normalized`: converted into an agent-readable record.
- `noted`: represented by a maintained wiki source note.
- `integrated`: cited or synthesized into broader wiki pages.
- `superseded`: replaced by a newer source or version.
- `rejected`: intentionally excluded from further processing.

Adjust the list only if the project needs a materially different workflow.
Avoid status names that mix workflow state with quality judgment. For example, prefer `rejected` plus a reason over a vague state such as `bad`.

## 6. Choose Claim Strictness

Structured claims are required when evidence is likely to conflict, change over time, or support important decisions. Examples include numeric results, deadlines, legal or fiscal facts, benchmark scores, availability statements, eligibility rules, and safety claims.

Recommended defaults:

- Keep `ingest.claim_extraction` enabled.
- Keep `lint.validate_claims` enabled.
- Require `source_ids` for source notes and claim pages.
- Use embedded claims for local page evidence.
- Use dedicated `wiki/claims/` pages for reusable or high-risk evidence.

Use lower strictness only for exploratory projects where the wiki is clearly temporary. Raise strictness for regulated, financial, legal, medical, safety, or publication-quality research.

## 7. Configure Outputs

Outputs are reusable artifacts produced from the maintained wiki, not raw scratch notes. Decide which output types the workspace should support before agents start producing deliverables.

Typical output types:

- Markdown reports and literature reviews.
- CSV or Markdown tables.
- Decision records.
- Presentation outlines.
- Export bundles for downstream tooling.
- Domain-specific checklists, briefs, or plans.

Keep generated outputs under `outputs.default_dir`, usually `wiki/outputs/`.
Add specialized output directories only when they become first-class maintained knowledge.

## 8. Optional Codebase Analysis

Enable codebase analysis only when repositories are part of the research scope, such as implementation availability, architecture comparison, or code-understanding evidence.

Recommended policy:

- keep `integrations.codebase_analysis.enabled` false by default;
- use read-only codebase inventory and context commands when possible;
- store generated architecture output under `sources/`, for example `sources/code_wikis/<source_id>/`;
- cite codebase analysis through source notes and `source_ids`;
- do not merge generated architecture pages directly into the maintained research `wiki/`;
- do not install hooks or background sync during workspace initialization.

For `agent-wiki-cli` / `python-wiki-llm`, prefer read-only `llm-wiki extract` and `llm-wiki context` output as normalized source evidence. Use `llm-wiki bootstrap` only into a generated source directory when a separate architecture wiki is useful.

Example disabled default:

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
```

Example enabled adapter contract:

```yaml
raw:
  source_roots:
    - raw/links
    - raw/code
integrations:
  codebase_analysis:
    enabled: true
    provider: agent-wiki-cli
    command: llm-wiki context --src-dir raw/code/example --budget 12000 --format json
    output_dir: sources/code_wikis
    read_only: true
    install_hooks: false
    background_sync: false
```

Run adapter commands explicitly and save their output under the manifest record's generated `metadata.codebase_output_dir`. The normalizer reads local artifacts such as `context.json` or `context.md`; if no artifact exists, it creates a stub and records the missing artifact warning.

## 9. Bootstrap The Workspace

When using `evidence-wiki init` or `scripts/init_research_workspace.py`, these bootstrap steps are handled during creation. For manual setup or review, confirm the resulting workspace has:

1. Confirm every configured raw root and wiki directory exists.
2. Update `index.md` so it names the project and links the main wiki areas.
3. Append a `setup` entry to `log.md` with the configuration decisions.
4. If using a domain pack, deep-merge its `research.overlay.yml` into the workspace `research.yml` and keep the pack files as reference guidance.
5. Run the workspace health checks.

Workspace-root checks:

```bash
python3 scripts/smoke_validate_workspace.py --format text
python3 scripts/source_inventory.py --dry-run --report
python3 scripts/normalize_sources.py --all --dry-run
python3 scripts/lint.py --format text
```

Smoke validation should pass before the broader health checks. The first inventory and normalization runs should be dry runs. Review the planned source IDs, parse warnings, and output paths before writing generated records.

## 10. Run The First Research Cycle

Use this sequence for the first batch:

1. Add raw sources under the configured source roots.
2. Run inventory in dry-run report mode.
3. Fix unexpected source grouping or filename issues.
4. Run inventory without `--dry-run` when the manifest shape is acceptable.
5. Run normalization as a dry run.
6. Normalize the selected batch.
7. Create source notes under `wiki/sources/`.
8. Extract entities, concepts, methods, systems, benchmarks, datasets, claims, questions, and synthesis pages according to the configured taxonomy.
9. Update `index.md`.
10. Append `inventory`, `normalize`, or `ingest` entries to `log.md`.
11. Run lint and fix structural issues before expanding the batch.

Prefer small batches until source IDs, source notes, and taxonomy placement are stable.

## 11. Snapshot Human Edits

Human edit snapshots are explicit. Before broad agent operations in a git workspace, run:

```bash
python3 scripts/snapshot_user_edits.py
```

Create a snapshot commit only when the user asks for it:

```bash
python3 scripts/snapshot_user_edits.py --commit --message "snapshot: user edits before ingest"
```

Do not install hooks that automatically commit user edits. Keep snapshot timing visible in `log.md` when it matters for the research history.

## Production Handoff Checklist

Use `docs/production-readiness-checklist.md` as the authoritative production readiness gate. The workspace is not production-ready just because it was created successfully; it should also have a reviewed source policy, validation status, version-control practice, and human-editing workflow.

At minimum, confirm:

- `research.yml` reflects the project goal, source roots, taxonomy, source status policy, claim strictness, outputs, and integrations.
- Raw source roots exist and raw immutability is understood.
- `index.md` and `log.md` are project-specific.
- Optional domain-pack guidance is applied or intentionally skipped.
- Optional codebase analysis is disabled or routed to generated source artifacts under `sources/`.
- Smoke validation passes.
- Inventory, normalization dry run, and lint complete without unexpected structural issues.
- The first source note can be traced to a manifest source ID and normalized record.
- Git history contains the starter setup and any intentional configuration changes.

For migrated production content, also use `docs/existing-wiki-migration-checklist.md` before changing production paths.
