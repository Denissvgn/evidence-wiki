# research-synthesis

Generic playbook for creating durable cross-source synthesis from maintained workspace knowledge.

## Use When

Use this skill when the user asks for a cross-source comparison, taxonomy, literature map, evidence map, recurring summary, open-question page, or decision-support synthesis.

Inputs:

- `research.yml`
- `index.md`
- existing synthesis, question, concept, method, system, benchmark, dataset, claim, and source-note pages when configured
- `sources.normalized_dir`
- `sources.manifest_path`
- structured claim pages or embedded claims
- `log.md`

## Operating Rules

- Read `research.yml` before choosing directories, page types, source paths, or output locations.
- Start from maintained wiki pages and source notes before reading normalized records.
- Use normalized records only for missing detail, evidence paths, parse warnings, or source-specific nuance.
- Preserve disagreement, uncertainty, limitations, and evidence gaps.
- Do not invent source conclusions, metrics, citations, or relationships.
- Do not create synthesis from a single source unless the user explicitly asks or the page states why that source is uniquely sufficient.
- Link claims back to source IDs, source notes, or structured claim pages.

## Synthesis Workflow

1. Frame the synthesis question, audience, and output type.
2. Read `index.md` and search existing synthesis or question pages to avoid duplicating maintained work.
3. Gather at least two relevant source notes by default.
4. Read related configured page types, such as concepts, methods, systems, benchmarks, datasets, claims, and questions when they exist.
5. Follow `source_ids` to normalized records only when source notes or wiki pages lack enough detail.
6. Compare sources for agreement, disagreement, scope differences, methods, evidence quality, limitations, and unresolved gaps.
7. Decide whether to update an existing page or create a new configured synthesis, question, or output page.
8. Update index metadata or static rows and append a `synthesis` log entry when the work changes maintained wiki content.

## Page Types And Frontmatter

Use configured page types and directories from `research.yml`. Prefer a `synthesis` page when the result combines or compares multiple sources and that type exists in the configured taxonomy. Use a `question` page when the main result is an unresolved gap or research plan. Use an `output` page when the result is a reusable artifact such as a table, export, or deck outline.

Default synthesis frontmatter:

```yaml
---
type: synthesis
created: YYYY-MM-DD
updated: YYYY-MM-DD
source_ids:
  - source:id
summary: One-sentence synthesis summary.
---
```

For a single-source exception, include a short section explaining why one source is sufficient or why the user requested single-source synthesis.

## Content Structure

Use only the sections needed for the work.

- Summary: short answer or synthesis result.
- Evidence Map: source notes, source IDs, and what each contributes.
- Comparison: differences by method, context, scope, result, cost, risk, or other configured taxonomy dimensions.
- Agreements: points supported by multiple sources.
- Disagreements: conflicts, competing interpretations, or incompatible scopes.
- Limitations: weak evidence, parse warnings, missing source notes, or stale data.
- Open Questions: follow-up research tasks or unresolved claims.
- Next Sources: source candidates that would reduce uncertainty.

Write synthesis as maintained knowledge, not as a transcript of every source.
Keep claims concise and traceable.

## Evidence And Claims

- Cite `source_ids` on the page frontmatter and near important claims.
- Link to source notes or related wiki pages with relative Markdown links.
- If `ingest.claim_extraction` is true and structured claims are useful, create or update claim pages or embedded claims using the configured claim schema.
- Do not resolve claim conflicts by choosing one value without reviewing the cited evidence.
- Represent scope differences explicitly instead of treating them as direct contradictions.

## Index, Log, And Follow-Up

- Update `index.md` when static index tables are used.
- If the workspace uses Dataview or another generated index, ensure frontmatter includes the metadata those queries need.
- Append a concise `synthesis` entry to `log.md` when maintained pages change.
- After broad synthesis work, run `python3 scripts/lint.py --format json` or use the `research-lint` playbook.

Log entry shape:

```text
## [YYYY-MM-DD] synthesis | Cross-source synthesis

- Pages: wiki/synthesis/example.md
- Source IDs: source:id, source:id-2
- Claims: created=0 updated=0 reviewed=0
- Gaps: missing source on example topic
```

## Completion Checklist

- Existing index and synthesis/question pages were checked first.
- At least two source notes support the synthesis, or a single-source exception is explicitly justified.
- Claims and conclusions cite source IDs and evidence pages.
- Agreements, disagreements, limitations, and gaps are visible.
- New or changed pages have valid frontmatter and relative links.
- Index metadata or static rows were updated.
- `log.md` has a synthesis entry when maintained pages changed.
- Lint was run or recommended after broad updates.
