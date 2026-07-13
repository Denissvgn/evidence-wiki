# research-questions

Generic playbook for intaking new research questions into the workspace backlog at any point in the project lifecycle.

## Use When

Use this skill when a parent agent or human hands the workspace one or more new questions to investigate, whether at initialization, after the first source cycle, or mid-project as priorities shift. It turns free-form question requests into well-formed question task records that `research-answer` can work.

Inputs:

- `research.yml`
- `index.md`
- configured `wiki/` root, especially the questions directory
- `scripts/question_status.py`
- `scripts/lint.py`
- `log.md`
- user- or parent-supplied questions, with optional priority and origin

## Operating Rules

- Read `research.yml` before assuming the questions directory, page types, or required frontmatter.
- Each question becomes one question page under the configured questions directory.
- Do not answer the questions here. Intake only records them as `open` tasks.
- Do not duplicate an existing open question. Check the backlog first and link or update the existing record instead.
- Keep the question text faithful to the request. Do not silently narrow or broaden scope.

## Intake Workflow

1. Review the existing backlog to avoid duplicates:

```bash
python3 scripts/question_status.py --format text
```

2. For each incoming question, normalize it into:

   - a concise one-line question,
   - a `priority` of `high`, `medium`, or `low` (default `medium` when unstated),
   - an `origin` describing who asked (for example `parent_agent`, `human`, `scout`, or `ingest`).

3. Derive a stable slug from a short identifier or the question text. Keep slugs unique within the questions directory.

4. Create one question page per new question under the configured questions directory using the frontmatter below.

5. Update `index.md` question rows or Dataview-compatible metadata so the new tasks are discoverable.

6. Append an `intake` entry to `log.md` summarizing how many questions were added and their origin.

7. Hand off to `research-answer` to work the updated backlog when the requester wants answers now.

## Question Page Frontmatter

```yaml
---
type: question
created: YYYY-MM-DD
updated: YYYY-MM-DD
status: open
priority: medium
origin: parent_agent
source_ids: []
question: The original question text.
summary: One-line restatement for the index.
---
```

Body shape:

```markdown
# The original question text.

## Task

- Status: open
- Priority: medium
- Origin: parent_agent

Context or constraints supplied with the request.

## Answer

_Not yet answered._
```

Open questions intentionally start with an empty `source_ids` list. The `question` frontmatter rule does not require evidence until the question is answered.

## Log Entry Shape

```text
## [YYYY-MM-DD] intake | New research questions

- Added: 3 open questions (origin: parent_agent)
- Priorities: P-high=1 P-medium=2
- Next action: run research-answer to work the backlog
```

## Completion Checklist

- `research.yml` was read before choosing paths or frontmatter.
- Existing open questions were checked to avoid duplicates.
- Each new question is a single `open` page with valid frontmatter and a unique slug.
- `index.md` and `log.md` reflect the new tasks.
- `python3 scripts/lint.py --format text` passes after intake.
- No questions were answered during intake; answering is handed to `research-answer`.
