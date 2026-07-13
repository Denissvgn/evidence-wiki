# research-lint

Generic playbook for running deterministic workspace health checks and turning lint output into prioritized maintenance work.

## Use When

Use this skill when the user asks to validate the workspace, review wiki health, check source coverage, inspect frontmatter or links, verify claim consistency, or prepare a project for ingest, synthesis, publishing, or handoff.

Inputs:

- `research.yml`
- `scripts/lint.py`
- configured `wiki/` root and wiki pages
- `sources.manifest_path`
- `sources.normalized_dir`
- `index.md`
- `log.md`

## Operating Rules

- Read `research.yml` before assuming enabled lint checks, severity labels, wiki directories, source manifest paths, or normalized source paths.
- Delegate deterministic checks to `scripts/lint.py`; do not replace the script with manual inspection.
- Use JSON output for agent interpretation and text output for quick human review.
- Treat lint output as evidence about workspace health, not as permission to make unrelated rewrites.
- Preserve raw sources. Never fix lint issues by editing raw evidence files.
- Ask before changing source lifecycle decisions, resolving claim conflicts, or performing broad multi-page cleanup.

## Commands

Run from the project root.

Primary agent-readable check:

```bash
python3 scripts/lint.py --format json
```

Human-readable check:

```bash
python3 scripts/lint.py --format text
```

Append a dated lint checkpoint to `log.md`:

```bash
python3 scripts/lint.py --format json --append-log
```

Use `--append-log` only for meaningful checkpoints, not every exploratory run.

## Interpreting Results

Read these JSON fields first:

- `issues`: concrete findings with severity, category, files, and recommendations.
- `recommendations`: high-level next actions.
- `stats.issue_counts`: severity counts.
- `stats.sources_*`: manifest, normalization, source-note, and integration coverage.
- `stats.claim_*`: structured claim validation and conflict counts.
- `source_coverage`: per-source lifecycle status when source coverage is enabled.

Severity guidance:

- `HIGH`: blocks trust or correctness. Address before relying on outputs.
- `MEDIUM`: important consistency, evidence, or workflow risk. Fix soon or explain why it is deferred.
- `LOW`: cleanup, discoverability, or coverage improvement. Batch when useful, but escalate if repeated broadly.

Category guidance:

- `structure` and `raw_sources`: align configured directories and filesystem.
- `frontmatter`: repair required fields, field types, and allowed values.
- `broken_link`, `index`, and `orphan`: restore navigation and discoverability.
- `source_manifest` and `source_status`: repair manifest shape or status values.
- `source_missing_normalized`: run or rerun normalization.
- `normalized_missing_source_note`: create source notes before integration.
- `integrated_missing_citation`: cite integrated sources from broader wiki pages or lower their status.
- `normalized_orphan` and `source_note_unknown_source`: reconcile stale or unknown source IDs.
- `claim_invalid`, `claim_conflict`, and `claim_near_duplicate`: review structured claims against evidence before changing conclusions.

## Fix Policy

Safe fixes when the target is obvious:

- add missing index rows or Dataview-compatible metadata,
- fill clearly missing frontmatter required by `research.yml`,
- fix broken Markdown links with a single clear target,
- update `updated` dates for pages changed in the current task,
- add source IDs that are directly supported by the page evidence.

Review before fixing:

- claim conflicts or contradictory values,
- source lifecycle changes such as `rejected`, `superseded`, or `integrated`,
- broad multi-page rewrites,
- deleting normalized records or source notes,
- changing configured lint behavior in `research.yml`.

Do not fix:

- raw source files,
- evidence content without checking the normalized record or source note,
- substantive research disagreements by choosing one side without review.

## Reporting

When reporting lint results to the user:

- lead with blocking `HIGH` issues,
- summarize `MEDIUM` and repeated `LOW` patterns,
- mention source coverage counts and claim conflicts when present,
- name affected files and source IDs,
- separate fixes already made from remaining risks,
- note when lint passes cleanly.

If the lint run is a meaningful project checkpoint, append it to `log.md` with `--append-log`. The generated entry records page counts, manifest counts, severity counts, source coverage, and recommendations.

## Completion Checklist

- `scripts/lint.py --format json` was run from the project root.
- Results were interpreted by severity and category.
- Deterministic fixes were applied only where appropriate.
- Source coverage and claim issues were reviewed when enabled.
- Remaining risks or deferred fixes were reported.
- `log.md` was updated with `--append-log` when this was a checkpoint.
