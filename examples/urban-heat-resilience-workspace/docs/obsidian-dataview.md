# Obsidian Dataview Guidance

Obsidian and Dataview are optional viewing tools. The research workspace works with plain Markdown, static `index.md` tables, scripts, and agent workflows without either plugin.

## Setup

To use Dataview, install and enable the Dataview community plugin in Obsidian.
Do not commit local plugin state, installed plugin files, or machine-specific Obsidian settings. Keep shared guidance in Markdown documentation.

The default `research.yml` marks Dataview as optional:

```yaml
integrations:
  obsidian:
    enabled: false
    dataview: optional
```

This means Dataview may be used for browsing, but workspace automation must not depend on it.

## Index Coverage

The linter treats pages as discoverable when they are covered by either:

- static Markdown links in `index.md`; or
- Dataview code blocks in `index.md` with quoted `FROM "wiki/<dir>"` paths.

Static index tables are the required portable baseline. Dataview sections may
supplement static rows when a human wants dynamic Obsidian views, but must not
replace them.

Use fenced `dataview` blocks and quoted folder paths. The current lint behavior recognizes folder coverage from examples like:

````markdown
```dataview
TABLE summary, updated, source_ids
FROM "wiki/sources"
SORT updated DESC
```
````

Every discoverable wiki directory must have static rows in `index.md`. A matching
Dataview section may provide an additional view. Dataview examples placed
outside `index.md` are useful for browsing, but they do not count as portable
index coverage.

## Generic Directory Queries

Use these examples as optional replacements or companions for static sections in `index.md`.

### Sources

```dataview
TABLE summary, updated, source_ids
FROM "wiki/sources"
SORT updated DESC
```

### Entities

```dataview
TABLE summary, updated, source_ids
FROM "wiki/entities"
SORT updated DESC
```

### Concepts

```dataview
TABLE summary, updated, source_ids
FROM "wiki/concepts"
SORT updated DESC
```

### Methods

```dataview
TABLE summary, updated, source_ids
FROM "wiki/methods"
SORT updated DESC
```

### Systems

```dataview
TABLE summary, updated, source_ids
FROM "wiki/systems"
SORT updated DESC
```

### Benchmarks

```dataview
TABLE summary, updated, source_ids
FROM "wiki/benchmarks"
SORT updated DESC
```

### Datasets

```dataview
TABLE summary, updated, source_ids
FROM "wiki/datasets"
SORT updated DESC
```

### Claims

```dataview
TABLE claim_type, subject, predicate, object, scope, updated, source_ids
FROM "wiki/claims"
SORT updated DESC
```

### Synthesis

```dataview
TABLE summary, updated, source_ids
FROM "wiki/synthesis"
SORT updated DESC
```

### Questions

```dataview
TABLE summary, updated, source_ids
FROM "wiki/questions"
SORT updated DESC
```

### Decisions

```dataview
TABLE status, summary, updated, source_ids
FROM "wiki/decisions"
SORT updated DESC
```

### Outputs

```dataview
TABLE summary, updated, source_ids
FROM "wiki/outputs"
SORT updated DESC
```

## Frontmatter Expectations

Dataview tables are only useful when pages keep consistent frontmatter. The default fields are:

```yaml
---
type: concept
created: YYYY-MM-DD
updated: YYYY-MM-DD
source_ids: []
summary: Short summary.
---
```

Some page types have additional configured fields:

- decisions should include `status`;
- claims should include `claim_type`, `subject`, `predicate`, `object`, and `scope`;
- source notes should include at least one real source ID.

Run lint after adding or changing pages:

```bash
python3 scripts/lint.py --format text
```

## Static Index Compatibility

Do not remove static rows. Dataview sections may supplement the same directories,
but the maintained workspace must remain complete in GitHub,
terminals, simple editors, and agents that do not evaluate Dataview. A Dataview
query is never the only route to a maintained page or essential conclusion.

## Comparative Usability Checklist

Run this checklist when evaluating plain-Markdown and Obsidian usability.
Record the OS, tool versions, observed result, failure, remediation, retest,
and useful screenshot identifiers in `log.md` or under `wiki/outputs/` when
the result matters for a handoff.

| Surface | Check | Pass condition |
|---------|-------|----------------|
| Plain Markdown | Open `index.md` in a text editor and a renderer with Obsidian closed. Follow every static Markdown link. | Every maintained page is reachable and its body, evidence, and caveats are readable without plugin evaluation. |
| Frontmatter | Inspect raw YAML and the rendered page for each page type. | YAML parses, required fields remain present, and a renderer that hides frontmatter does not hide the page's substantive content. |
| Obsidian core | Open the workspace folder as a vault with Restricted Mode on and all community plugins disabled. | Static links resolve, headings and tables render, and no essential navigation or content is missing. |
| Dataview disabled | Leave `dataview` fences unevaluated or disable the plugin. | The fences may remain visible, but the static index still provides equivalent page discovery. |
| Dataview enabled | Enable the reviewed Dataview version and run each documented query. | Query results agree with the static index; an empty or erroneous query is a visible failure, not permission to delete static coverage. |
| Templates | Create one page by copying a file from `.obsidian/templates/` without the Templates core plugin. | The page can be completed, indexed, and linted as plain Markdown. |
| Casing | Compare every link component with the on-disk spelling. | Links do not rely on Windows/macOS case folding and no two paths differ only by case. |
| Unicode | Create and follow a reviewed NFC name such as `café-cooling.md` under a path containing spaces. | The same visible link reaches the same file in the plain renderer and Obsidian; no case- or normalization-only alias is introduced. |

## Link, Casing, And Unicode Rules

- Use standard relative Markdown links for required navigation. Wikilinks may be
  convenient in Obsidian, but they must not replace the static link from
  `index.md`.
- Write `/` separators in Markdown and configuration. Match every directory and
  filename component exactly as stored; a link that works only because a host
  folds case fails the lane.
- Quote shell paths that contain spaces or Unicode. Create Unicode filenames in
  NFC where the platform permits it, and reject names that collide after case
  folding or Unicode normalization.
- When moving a page or changing only its case, use an intermediate distinct
  name, update all links, run lint, and review the version-control diff before
  removing the old path.

## Optional-Plugin Failure Behavior

If Obsidian, its Templates core plugin, or Dataview is missing, disabled,
incompatible, or reports an error, keep working from `index.md` and copy the
[plain Markdown templates](obsidian-templates.md) manually. Do not install a
plugin automatically, enable community plugins without operator review, or
change workspace content merely to suppress a plugin error. Capture the
user-visible failure and remediation, then retest both the plugin-disabled
baseline and (when required) the reviewed plugin-enabled lane.
