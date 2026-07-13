# research-init

Generic playbook for creating or preparing a new research workspace with minimal user configuration.

## Use When

Use this skill when the user asks to start a new research project, apply the workspace system to a research scope, instantiate a template, prepare a domain pack, or reduce setup questions before research begins.

Inputs:

- target workspace path or project name,
- research scope and desired outcome,
- optional initial sources, URLs, folders, code repositories, or existing wiki,
- available domain packs,
- reusable starter files,
- `research.yml`,
- `index.md`,
- `log.md`,
- generic research skills.

## Operating Rules

- Ask the fewest questions needed to create a usable starting workspace.
- Ask at most three setup questions before doing local inspection and drafting a setup profile.
- Prefer inferred defaults over making the user choose internal settings.
- Make inferred decisions explicit in the init report.
- Keep the workspace domain-neutral unless a domain pack or project-local guidance is clearly needed.
- Do not copy pilot data or prototype content unless the user explicitly asks.
- Do not install git hooks, background agents, or auto-commit workflows during initialization.
- Do not fetch network resources unless the user requested it and the
  environment permits it.
- Treat code repositories as optional research sources, not as replacements for
  the maintained research wiki.

## Minimal Questions

If missing, ask only for:

1. Target workspace path or short project name.
2. Research scope and desired outcome.
3. Optional starting sources, URLs, folders, repositories, or existing wiki.

Do not ask the user to choose raw roots, page types, lifecycle statuses, claim rules, output formats, or integration settings unless the answer materially changes the setup and cannot be inferred safely.

## Inference Defaults

Use these defaults unless the user gives a conflicting requirement:

- `project.name`: slug from the target path or project title.
- `project.description`: one sentence derived from the stated scope.
- `project.owner_goal`: the user's desired outcome.
- `project.language`: the user's language when obvious, otherwise `en`.
- `raw.source_roots`: keep the starter defaults and create only the roots that are relevant to supplied sources.
- `raw.immutable`: `true`.
- `sources.lifecycle_statuses`: keep the default discovered to rejected flow.
- `wiki.required_dirs`: keep the generic taxonomy for the first workspace.
- `ingest.claim_extraction`: `true` when evidence may conflict, expire, or support decisions.
- `outputs.supported_formats`: keep Markdown, CSV, JSON, and presentation outline support unless the user names a narrower output set.
- `integrations.git.snapshot_user_edits`: explicit snapshots only.
- `integrations.codebase_analysis.enabled`: `false` unless the supplied sources include repositories, source archives, or implementation snapshots that are part of the research evidence.

## Workflow

### 1. Frame The Init Profile

Create a schema-compliant init profile before changing files. Use the shape in `docs/workspace-init-profile.md`. The profile should include:

- project identity and goal,
- inferred language,
- target path,
- initial source locations,
- optional existing wiki path,
- domain guidance or domain-pack decision,
- output expectations,
- claim strictness,
- integration decisions,
- optional codebase-analysis setting,
- questions asked and inferred answers,
- validation commands with pending results,
- assumptions, skipped decisions, and next actions.

Pass this profile to `evidence-wiki init` with `--profile`, or to `scripts/init_research_workspace.py` when working from a source checkout. Use `--dry-run` when the inferred decisions need review before writes. Keep the profile reviewable so future agents can replay or audit the setup.

### 2. Select Domain Guidance

Inspect available domain packs. Apply one only when its scope clearly matches the project.

Choose one profile mode:

- `none`: the generic taxonomy is enough for the first setup pass.
- `domain_pack`: a reusable domain pack clearly matches the scope.
- `project_local`: no pack matches, but the project needs lightweight local extraction and filing guidance.
- `deferred`: source inspection is needed before a domain decision is safe.

If no pack matches and local guidance is useful:

- keep the generic workspace configuration,
- set `domain_guidance.mode: project_local` in the setup profile,
- include concise extraction targets, source priorities, claim types, output scaffolds, filing rules, and promotion notes,
- do not create a reusable domain pack unless the user asks for a pack.

The initializer renders project-local guidance into the created workspace. See `docs/domain-guidance-generator.md` for the generated document workflow and promotion threshold.

### 3. Create The Workspace

Use `evidence-wiki init` to create the target workspace. The initializer refuses non-empty targets unless it is explicitly run with `--force`.

Example:

```bash
evidence-wiki init --profile /path/to/workspace-init.yml
```

For older starters without the script, follow the manual fallback in `docs/new-project-guide.md`:

1. copy the reusable starter to the target path,
2. initialize git if appropriate,
3. edit `research.yml` from the init profile,
4. update `index.md` and `log.md`,
5. keep raw sources immutable.

### 4. Prepare Sources

Route supplied inputs into source roots:

- papers and reports to `raw/papers` or `raw/pdf`,
- curated URLs to `raw/links`,
- datasets to `raw/data`,
- media or screenshots to `raw/media`,
- code repositories or source archives to `raw/code`,
- existing wiki content to a staging migration workflow, not directly into maintained pages.

Run inventory in dry-run report mode before writing manifests:

```bash
python3 scripts/source_inventory.py --dry-run --report
```

### 5. Optional Codebase Analysis

If the research scope includes software architecture, implementation availability, repository comparison, or codebase understanding, enable optional codebase analysis.

Use a codebase analysis adapter only as a source normalizer:

- run it read-only,
- write generated architecture context under configured source or generated source locations,
- cite it through source IDs,
- keep its generated architecture wiki separate from the maintained research `wiki/`,
- do not install its hooks or background automation by default.

For `agent-wiki-cli` / `python-wiki-llm`, prefer `llm-wiki extract` and `llm-wiki context` for read-only context. Use `llm-wiki bootstrap` only into a separate generated source directory such as `sources/code_wikis/<source_id>/`.
Record the adapter command in the setup profile if useful, but do not run it during initialization. Inventory and normalization will treat existing local artifacts as source evidence and will stub missing artifacts with a warning.

### 6. Validate Setup

Run workspace checks before research begins:

```bash
python3 scripts/smoke_validate_workspace.py --format text
python3 scripts/source_inventory.py --dry-run --report
python3 scripts/normalize_sources.py --all --dry-run
python3 scripts/lint.py --format text
```

Smoke validation must pass before inventory, normalization, or broader lint checks run. Treat failures as setup issues to fix before research begins.

### 7. Review And Update The Init Report

Profile-driven initialization writes a setup report by default:

```text
docs/workspace-init-report.md
```

Include:

- questions asked,
- inferred answers,
- selected domain guidance,
- source roots and supplied sources,
- output types,
- claim strictness,
- optional codebase-analysis decision,
- validation commands and results,
- next recommended research actions.

Before initialization, populate report fields in the setup profile: `questions_asked`, `inferred_answers`, `validation.commands`, pending `validation.results`, and `next_actions`. After running validation, update the generated report with passed, failed, blocked, or not-run results so the setup is auditable without reading the conversation.

## Completion Checklist

- No more than three setup questions were required.
- The workspace exists at the target path.
- `research.yml` reflects inferred setup decisions.
- `index.md` and `log.md` are project-specific.
- Domain guidance was applied, created locally, or intentionally skipped.
- Initial sources are mapped to raw roots or staged for migration.
- Codebase analysis is disabled unless the scope needs it.
- Hooks and background automation were not installed by default.
- Smoke validation passed, then dry-run inventory, dry-run normalization, and lint were run or clearly blocked.
- The init report records assumptions and next actions.
- Production readiness is evaluated later with `docs/production-readiness-checklist.md`; it is not required for initial workspace creation.
