# Production Readiness Checklist

Use this checklist when deciding whether a research workspace is ready for sustained use beyond a pilot. Run it after the workspace has been initialized, smoke-validated, and exercised through at least one source cycle.

This checklist does not replace deterministic checks. `smoke_validate_workspace.py` verifies initialization structure, `source_inventory.py` and `normalize_sources.py` verify the source pipeline, and `lint.py` validates maintained wiki health. Production readiness adds the human operating bar: evidence policy, validation status, version-control practice, and human-editing safety.

Doctor, smoke, status, lint, and publication readiness share one additive
workspace-health contract. A `healthy` workspace may proceed; `degraded`
remains usable with the named optional capability unavailable; `invalid` exits
as unreadable; and `publication_blocked` permits diagnostics but forbids a
publication verdict. Retain the stable finding codes, affected artifacts, and
remediation from either JSON or text output rather than paraphrasing them.

## Readiness Levels

| Level | Meaning | Minimum gate |
|-------|---------|--------------|
| `initialized` | The workspace has been created and structurally validated. | Smoke validation passes. |
| `pilot-ready` | The first source cycle can run end to end. | Inventory, normalization dry run, source-note workflow, and lint are understood. |
| `production-ready` | The workspace can support sustained use and handoff. | This checklist is satisfied or every exception is documented with an owner and disposition. |

## Minimum Production Readiness Commands

Run from the research workspace root:

```bash
python3 scripts/smoke_validate_workspace.py --format text
python3 scripts/source_inventory.py --report
python3 scripts/normalize_sources.py --all --dry-run
python3 scripts/lint.py --format text
```

When reusable system scripts, fixtures, or tests changed, also run from the repository root:

```bash
python3 -B -m unittest discover -s tests -p 'test_*.py'
python3 -m ruff check
python3 -B workspace-template/scripts/source_inventory.py --project-root tests/fixtures/arxiv-source-project --report
python3 -B workspace-template/scripts/normalize_sources.py --project-root tests/fixtures/arxiv-source-project --all --dry-run
python3 -B workspace-template/scripts/lint.py --project-root tests/fixtures/minimal-project --format text
python3 -B workspace-template/scripts/lint.py --project-root tests/fixtures/dataview-index-project --format text
```

The fixture inventory command writes a temporary or fixture manifest before the
normalization dry run. `normalize_sources.py --all --dry-run` reads
`sources/manifest.jsonl`; it does not consume inventory dry-run stdout.

## Provider Credential Safety

Provider credentials must come from operator-managed environment injection for
the run. Treat readable repository-root or workspace-root `.env` files as
development-only; rotate `OPENALEX_API_KEY` or `GITHUB_TOKEN` if a key may
have been exposed in a workspace, report, shell history, or shared checkout.

## Scale Benchmark And Practical Limits

Run the repository-root benchmark when a workspace is being considered for
sustained unattended use, or after changing reusable source, lint, status, or
retrieval code:

```bash
python3 -B tools/scale_benchmark.py --format json
EVIDENCE_WIKI_RUN_SCALE=1 python3 -B -m unittest tests.test_scale_smoke
```

The default benchmark generates 1,000 synthetic link sources and 2,000 maintained wiki pages, then measures inventory, normalization, lint, workspace status, persistent FTS index build, and indexed query. The synthetic sources are offline link records, so the run does not need network access, Poppler, or optional package extras.

Measured in this checkout on 2026-06-13 with Python 3.14:

| Stage | Wall time |
|-------|-----------|
| Inventory | 0.012 s |
| Normalization | 0.718 s |
| Lint | 3.537 s |
| Workspace status | 4.547 s |
| Persistent FTS index build | 1.529 s |
| Indexed query | 0.090 s |
| Total benchmark | 10.478 s |

The measured run indexed 3,000 documents, returned 10 indexed query results,
and reported no HIGH or MEDIUM lint findings. The remaining LOW findings are
expected synthetic source-note advisories because the benchmark normalizes link
records without curating `wiki/sources/` notes.

Recommended workspace partitioning point: start splitting by domain, time period,
source collection, or subquestion once a workspace exceeds roughly 2,000 production sources or 5,000 maintained wiki pages, or earlier if local lint, status, or indexed-query timings no longer fit the runner's operational budget. Treat these numbers as local-run guidance, not a hosted CI service-level objective.

Save meaningful validation results in `log.md` or under `wiki/outputs/` when they support handoff or audit.

## Discovery Quality Evaluation

When the optional discovery stage is in use, run the discovery-quality regression
guard after changing any candidate ranking, trust-tier, or rejection logic:

```bash
python3 -B -m unittest tests.test_discovery_quality_eval
```

The declarative scenarios under `tests/fixtures/discovery/eval/` are
network-free and deterministic. They run real discovery pipelines through fixture
backends and assert the expected ranking behavior:

| Scenario | Guards |
|----------|--------|
| `legal-official-vs-secondary` | Official statute/regulation/court sources rank `official_primary` and outrank reputable secondary legal databases (kept as supplemental); a superseded official page is flagged and downgraded to review; a scraped mirror is rejected. |
| `general-search-useful-and-rejected` | An official `.gov` source outranks generic pages; a useful page is retained for review; a suspicious executable download and a mirror duplicating the official source are rejected with rationale. |
| `paper-companion-repository` | The canonical companion repository outranks a fork/mirror and an archived, unlicensed copy; ownership is never asserted from search, so every repo stays `review`. |
| `author-publications` | An ORCID-author's topically related publication is proposed for review while an unrelated work is rejected as out of scope; the seed paper is never re-proposed and identity ambiguity is never asserted. |

Each fixture documents its own `expected_behavior`.

## 1. Identity And Configuration

- [ ] `workspace-system.yml` is present, readable, and records starter version metadata.
- [ ] `research.yml` has project-specific `project.name`, `project.description`, `project.owner_goal`, and `project.language` values.
- [ ] `research.yml` top-level sections match the documented configuration contract in `docs/research-yml.md`.
- [ ] A domain pack or project-local guidance is applied when the research scope needs domain-specific extraction rules.
- [ ] `python3 scripts/smoke_validate_workspace.py --format text` passes.
- [ ] Setup decisions are recorded in `log.md` or, when available, in `docs/workspace-init-report.md`.

## 2. Source Immutability And Evidence Boundaries

- [ ] `raw.immutable` is `true`, or the exception is documented and approved.
- [ ] Every configured `raw.source_roots` directory exists and has a clear purpose.
- [ ] Raw source mapping is documented well enough that a maintainer can tell where papers, links, datasets, media, code, and notes belong.
- [ ] Scripts and agents do not rewrite, reorganize, or delete raw evidence unless a task explicitly authorizes source cleanup.
- [ ] New source versions are added as new evidence instead of overwriting old files.
- [ ] Missing, excluded, or untrusted evidence is recorded as a gap or rejected source rather than silently removed.
- [ ] Agents follow `docs/prompt-injection-hardening.md`: source content is evidence data, instruction-like text is quoted rather than obeyed, and provenance URLs are not auto-fetched.

## 3. Manifest Coverage And Source Lifecycle

- [ ] `python3 scripts/source_inventory.py --report` has been reviewed.
- [ ] `sources/manifest.jsonl` is committed, or the project has a documented policy for regenerating it on demand.
- [ ] Unknown, ambiguous, unpaired, and review-required inventory records have been triaged.
- [ ] Source lifecycle statuses in `research.yml` are meaningful for this project and understood by maintainers.
- [ ] Production-scope raw sources have a visible lifecycle state such as `discovered`, `normalized`, `noted`, `integrated`, `deferred`, `superseded`, or `rejected`.
- [ ] Deferred sources and source coverage gaps have owners, dispositions, or follow-up questions.

## 4. Normalized-Source Policy

- [ ] `python3 scripts/normalize_sources.py --all --dry-run` has been reviewed before writing or refreshing normalized records.
- [ ] Normalized records exist for production-scope sources that are ready for source notes.
- [ ] Parse warnings are preserved in normalized records and triaged before claims or synthesis depend on the affected content.
- [ ] PDF fallback, link stub, and optional codebase-analysis policies are documented for the project.
- [ ] The project has a clear rule for when `--force` regeneration is allowed.
- [ ] Maintained wiki pages cite source notes and `source_ids`; they do not treat normalized records as curated interpretation by themselves.
- [ ] The default-on prompt-injection pattern review is left enabled unless the project has an explicit opt-out reason; LOW findings are triaged as weak reviewer-awareness signals, not blocking verdicts by themselves.

## 5. Maintained Wiki And Source Notes

- [ ] Production-scope normalized sources have corresponding `wiki/sources/` notes or documented deferrals.
- [ ] Maintained wiki pages use configured frontmatter and cite relevant `source_ids`.
- [ ] `index.md` or Dataview sections cover maintained pages that should be discoverable.
- [ ] `log.md` is append-only and records meaningful inventory, normalization, ingest, lint, and synthesis checkpoints.
- [ ] Synthesis pages cite multiple source notes when they compare or combine evidence across sources.
- [ ] Open evidence gaps, unresolved contradictions, and deferred decisions are captured as questions, decisions, or documented lint dispositions.

## 6. Lint And Quality Gates

- [ ] `python3 scripts/lint.py --format text` or JSON output reports no HIGH or MEDIUM issues.
- [ ] Remaining LOW issues are documented with an owner, disposition, or planned follow-up.
- [ ] Structured claim conflicts are reviewed against source evidence before being deferred or resolved.
- [ ] Broken links, missing frontmatter, invalid source IDs, and source coverage gaps are treated as blockers unless explicitly deferred.
- [ ] Regression tests are run from the repository root when reusable scripts, fixtures, or tests changed.
- [ ] Validation summaries are appended to `log.md` or saved under `wiki/outputs/` when they matter for handoff.

## 7. Backup And Version Control

- [ ] The workspace is tracked in git or has an equivalent backup and change history policy.
- [ ] The worktree is clean or all uncommitted changes are understood before broad agent operations.
- [ ] Raw, source, and wiki changes are committed in reviewable batches.
- [ ] Migrated production content has a read-only backup or clone before production paths are changed.
- [ ] Starter version provenance is retained in `workspace-system.yml`.
- [ ] No git hook performs automatic commits, broad auto-adds, or nested commits.

## 8. Human Editing And Operations

- [ ] `integrations.git.snapshot_user_edits` is set to `explicit`, or an equivalent human-editing policy is documented.
- [ ] Agents use `python3 scripts/snapshot_user_edits.py` before broad operations when the workspace is under git.
- [ ] Obsidian and Dataview are documented as optional interfaces if used.
- [ ] `AGENTS.md` and project/domain guidance reflect the current workflow.
- [ ] Maintainers know who owns source inventory, normalization issues, lint issues, and source-note integration.
- [ ] The project has an escalation policy for contradictions, uncertain evidence, or claims that affect important decisions.

## 9. Adoption Or Migration Cutover

For existing wiki migrations, use `docs/existing-wiki-migration-checklist.md` as the migration-specific companion to this checklist.

- [ ] Migration planning happens in a staging workspace before production content changes.
- [ ] Source ID mapping from old citations to workspace manifest IDs is documented.
- [ ] Existing log or history is preserved or archived and linked from the new workspace.
- [ ] A human has reviewed the staging wiki and remaining lint/source coverage issues.
- [ ] A final backup exists before production paths are changed.
- [ ] The cutover commit or handoff note records source mapping and validation results.

## Readiness Decision

The workspace is production-ready only when every required item is checked or has an explicit exception with an owner, reason, and follow-up date. Record the decision in `log.md` and, when useful, save the completed checklist under `wiki/outputs/`.
