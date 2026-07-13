# research-ingest

Generic playbook for moving normalized source records into maintained wiki knowledge.

## Use When

Use this skill when the user asks to ingest one or more sources into the wiki, create source notes, extract evidence, or update research pages from normalized records.

Inputs:

- `research.yml`
- `sources.manifest_path`
- `sources.normalized_dir`
- target `source_id` values or normalized source files
- existing `wiki/` pages
- `index.md`
- `log.md`

## Operating Rules

- Read `research.yml` before choosing directories, page types, lifecycle states, or ingest behavior.
- Ingest from normalized source records, not directly from raw files.
- Use raw paths only as evidence references named by normalized records.
- Preserve parse warnings and evidence gaps instead of smoothing them over.
- Keep every maintained wiki page source-grounded with `source_ids`.
- Do not invent claims, citations, metrics, dates, or source conclusions.
- If `ingest.ask_before_large_wiki_update` is true and the update would touch more than `ingest.large_update_page_threshold` wiki pages, pause and ask for confirmation.

## Step 1: Create Or Update Source Notes

Start with a source note before broader integration when `ingest.source_note_required` is true.

1. Locate each normalized record by `source_id` under `sources.normalized_dir`.
2. Read frontmatter, citation metadata, abstract or summary, outline, extracted text, links, raw source paths, and parse warnings.
3. Create or update a source note in the configured wiki source-note area (`wiki/sources/` by default).
4. Use frontmatter compatible with `research.yml`:

```yaml
---
type: source
created: YYYY-MM-DD
updated: YYYY-MM-DD
source_ids:
  - source:id
summary: One-sentence source summary.
---
```

5. Include concise sections appropriate to the source, such as:
   - citation or origin,
   - key findings,
   - methods or evidence basis,
   - limitations,
   - useful links,
   - parse warnings,
   - candidate pages to update next.
6. If a source is not worth ingesting, record the reason in the source note or manifest status instead of silently skipping it.

## Step 2: Integrate Across The Wiki

After source notes exist, integrate evidence into configured wiki page types.
Use `wiki.allowed_page_types` and `wiki.required_dirs` as the contract instead of assuming a domain taxonomy.

Typical integration targets:

- entities: organizations, people, products, projects, places, or named things.
- concepts: reusable definitions, categories, ideas, or distinctions.
- methods: workflows, techniques, algorithms, or procedures.
- systems: concrete tools, products, implementations, or architectures.
- benchmarks and datasets: evaluation assets and data resources.
- claims: structured evidence statements.
- synthesis: cross-source comparisons, maps, reviews, or summaries.
- questions: gaps, uncertainties, and follow-up research tasks.

For each created or updated page:

- keep required frontmatter fields valid,
- set `updated` to the current date,
- add all supporting `source_ids`,
- link to source notes or related wiki pages with relative Markdown links,
- separate sourced facts from interpretation,
- preserve disagreements and uncertainty.

If `ingest.claim_extraction` is true, write important evidence as structured claims either in `wiki/claims/` or embedded in page frontmatter:

```yaml
claims:
  - subject: Example subject
    predicate: reports
    object: Example evidence statement
    value: 10.54
    unit: points
    scope: Example evaluation setting
    source_ids:
      - source:id
```

Only create a claim when the normalized source supports the statement.

## Step 3: Resolve Open Questions When Evidence Arrives

Ingest is a natural point to advance the question backlog. New evidence may answer or unblock questions opened by initialization seeding, `research-scout`, or `research-questions`.

1. Scan the backlog for questions this source might affect:

```bash
python3 scripts/question_status.py --format text
```

2. For any `open`, `in_progress`, or `blocked` question the ingested evidence now answers, hand off to the `research-answer` skill to write the answer page and set `status: answered` with a real `answer_page` link and `source_ids`.
3. For a question that is still unanswered but newly advanced, update its `blocked_reason` or notes to reflect what remains.
4. Do not invent answers. Only resolve questions the ingested evidence actually supports.

## Index And Log Updates

Make the ingest discoverable and auditable.

- Update `index.md` when static index tables are used.
- If the workspace uses Dataview or another generated index, ensure page frontmatter contains the metadata needed by those queries.
- Append an `ingest` entry to `log.md` when `ingest.update_log` is true.

Log entry shape:

```text
## [YYYY-MM-DD] ingest | Source ingest

- Source IDs: source:id, source:id-2
- Source notes: wiki/sources/example.md
- Integrated pages: wiki/concepts/example.md
- Claims: created=0 updated=0
- Gaps: none
```

## Completion Checklist

- Normalized records were used as the ingest source.
- Source notes exist for ingested source IDs.
- Integrated pages cite the relevant source IDs.
- New or changed pages have valid frontmatter.
- Index metadata or static rows were updated.
- `log.md` has a concise ingest entry when configured.
- Remaining parse warnings, contradictions, or evidence gaps are visible.
