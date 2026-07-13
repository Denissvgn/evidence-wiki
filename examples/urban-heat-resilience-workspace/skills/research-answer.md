# research-answer

Generic playbook for working the open question backlog and reporting progress back to the agent or human that requested the research.

## Use When

Use this skill when the workspace has question task records to resolve: after initialization seeded questions, after `research-questions` intake added new questions, after `research-scout` opened gaps, or when a parent agent asks for the status of previously assigned questions.

Inputs:

- `research.yml`
- `index.md`
- configured `wiki/` root, especially the questions directory
- `scripts/question_status.py`
- `scripts/lint.py`
- wiki pages, source notes, and synthesis pages
- `sources.manifest_path` and `sources.normalized_dir`
- `log.md`

## Operating Rules

- Read `research.yml` before assuming wiki directories, page types, lifecycle states, or the questions directory.
- Treat question pages as task records with a lifecycle, not as static notes.
- Answer from maintained wiki knowledge and normalized source records. Do not invent conclusions, metrics, citations, or dates.
- Update a question page's `status` only when the evidence supports the new state.
- For high-stakes or supervisor-designated answerability questions, keep `sources/coverage/<slug>.yml` current and require it to pass before marking the question answered; successful gated answers record `coverage_required: true` and `coverage_manifest: sources/coverage/<slug>.yml`.
- Allowed `status` values come from `wiki.frontmatter_type_rules.question`: `open`, `in_progress`, `answered`, `blocked`, `deferred`, `rejected`.
- Never delete a question. Mark it `rejected` or `deferred` with a short reason instead.
- Do not fabricate an `answer_page` link. An `answered` question must point to a real wiki page.

## Backlog Workflow

1. Scan the backlog deterministically:

```bash
python3 scripts/question_status.py --format text
```

   Use `--format json` when you need structured fields, or `--status open` to focus on a single lifecycle state.

2. Pick the highest-priority actionable question (`open` or `in_progress`). Prefer `high` over `medium` over `low`; break ties by the order reported.

3. Set the question to `in_progress` and bump `updated` while you work on it, so concurrent agents do not duplicate the effort.

4. Answer the question using the `research-query` skill. Start from `index.md` and maintained wiki pages, then follow `source_ids` into source notes and normalized records. Run `scripts/query_index.py` (the shipped local retrieval baseline) to surface relevant pages and records; use raw repository search only as a last resort when the index returns nothing.

5. Resolve the question into one of these outcomes:

   - Answerable now: write or update the answer page (a `synthesis`, `output`, or other configured page type that fits), then set the question `status: answered` and set `answer_page` to a relative link to that page. For high-stakes questions, first update coverage facets, confirm `sources/coverage/<slug>.yml` reports `coverage_verdict: pass`, and record `coverage_required: true` plus `coverage_manifest: sources/coverage/<slug>.yml`.
   - Not answerable from current evidence: set `status: blocked` and record `blocked_reason` plus the sources needed. Open or update a `research-scout` candidate when a new source is required.
   - Out of scope or superseded: set `status: rejected` or `deferred` with a short reason in the body.

6. Repeat for the next actionable question until the backlog is cleared or the requested batch is done.

## Resolving A Question Page

When marking a question `answered`:

```yaml
---
type: question
created: YYYY-MM-DD
updated: YYYY-MM-DD
status: answered
priority: high
origin: parent_agent
source_ids:
  - source:id
answer_page: ../synthesis/example-answer.md
question: The original question text.
---
```

- Keep the original `question` text intact for traceability.
- Add the `source_ids` that ground the answer.
- For high-stakes answers, require a passing coverage manifest and persist `coverage_required: true` plus `coverage_manifest: sources/coverage/<slug>.yml` for status, lint, and export.
- Point `answer_page` at an existing wiki page, relative to the question page.
- Record the answer detail on the linked page, not in the question frontmatter.

When marking a question `blocked`:

```yaml
---
type: question
status: blocked
blocked_reason: Needs the 2024 benchmark results; none ingested yet.
source_ids: []
---
```

## Reporting Back

Report progress in two complementary ways.

1. Deterministic snapshot for the parent agent or human:

```bash
python3 scripts/question_status.py --format json
```

   This is the machine-readable hand-back. It lists totals, counts by status and priority, the actionable backlog, blocked reasons, and answered pages.

2. Narrative summary in the response and, when appropriate, an `answer` entry in `log.md`:

```text
## [YYYY-MM-DD] answer | Question backlog progress

- Answered: 2 (wiki/questions/scaling-laws.md -> wiki/synthesis/scaling.md, ...)
- Blocked: 1 (needs 2024 benchmark source)
- Still open: 3 (P1=1 P2=2)
- Next actions: ingest source:id, then re-run research-answer
```

Make the evidence boundary clear: separate what was answered from current knowledge, what is blocked pending new sources, and what remains open.

## Completion Checklist

- `research.yml` was read before choosing paths or lifecycle states.
- The backlog was scanned with `scripts/question_status.py`.
- Each handled question moved to a valid lifecycle state with the required fields for that state.
- Answered questions link an existing `answer_page` and cite `source_ids`.
- Blocked questions record a `blocked_reason` and a route to acquire evidence.
- `python3 scripts/lint.py --format text` passes the question checks.
- Progress was reported back via the status script and a narrative summary, with a `log.md` entry when maintained pages changed.
