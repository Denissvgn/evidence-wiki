# Obsidian Page Templates

The `.obsidian/templates/` directory contains optional page scaffolds for manual wiki editing. The workspace works without Obsidian; these files are plain Markdown snippets that can also be copied from any text editor.

## Available Templates

| Template | Default destination | Page type |
|----------|---------------------|-----------|
| `source-note.md` | `wiki/sources/` | `source` |
| `concept.md` | `wiki/concepts/` | `concept` |
| `decision.md` | `wiki/decisions/` | `decision` |
| `claim.md` | `wiki/claims/` | `claim` |
| `synthesis.md` | `wiki/synthesis/` | `synthesis` |

## Use Workflow

1. Copy the relevant template into the matching wiki directory.
2. Rename the copied file with a stable, lowercase, hyphenated page slug.
3. Replace placeholder dates with `YYYY-MM-DD` values.
4. Replace placeholder source IDs with real IDs from `sources/manifest.jsonl` or normalized source records.
5. Fill the summary and evidence sections with source-grounded notes.
6. Add the page to `index.md`, unless Dataview coverage is configured later.
7. Run `python3 scripts/lint.py --format text` from the workspace root.

## Placeholder Rules

- `source-note.md` must keep `type: source` and a non-empty `source_ids` list.
- `claim.md` must keep `type: claim`, `claim_type`, `subject`, `predicate`, `object`, `scope`, and a non-empty `source_ids` list.
- `decision.md` must use a configured status such as `proposed`, `accepted`, `rejected`, or `superseded`.
- `concept.md` and `synthesis.md` may start with `source_ids: []`, but add real source IDs as soon as evidence is cited.

Do not leave `source:id`, `YYYY-MM-DD`, or generic placeholder text in a maintained wiki page. The templates are starting points; copied pages should still be edited into project-specific, evidence-backed notes before they are treated as durable research knowledge.

## Relationship To Domain Packs

Domain packs may include richer source or page scaffolds for a specific research area. Use those domain-specific scaffolds when they apply. Use the Obsidian templates for quick generic page creation and for page types that do not have a domain-specific scaffold.

## Plugin-Free Copy Workflow

The Obsidian Templates core plugin is optional. From the workspace root, a plain
terminal or file manager can copy the same scaffold. Quote the path and preserve
the template's exact lowercase filename:

```bash
cp .obsidian/templates/concept.md "wiki/concepts/café-cooling.md"
```

```powershell
Copy-Item -LiteralPath '.obsidian\templates\concept.md' -Destination 'wiki\concepts\café-cooling.md'
```

Open the copied file in any text editor, replace every placeholder, and add an
exact-case standard Markdown link to `index.md`. The destination may contain
reviewed Unicode, but create names in NFC where the platform permits it and do
not create another path that differs only by case or normalization. Markdown
links use `/`, so the portable index target is
`wiki/concepts/café-cooling.md` on every operating system.

## Template And Frontmatter Check

For each template included in a manual usability check:

1. record the template's workspace-relative source and destination paths;
2. parse the YAML between the opening and closing `---` delimiters;
3. confirm `type`, `created`, `updated`, `source_ids`, and `summary`, plus the
   page-type-specific fields documented above;
4. replace `YYYY-MM-DD`, `source:id`, generic summaries, titles, and body
   prompts before treating the page as maintained content;
5. add the exact-case standard Markdown link to `index.md` and follow it in a
   plain renderer with Obsidian closed;
6. run `python3 scripts/lint.py --format text`, then repeat the navigation check
   in Obsidian Restricted Mode before enabling any optional plugin;
7. when the result matters for a handoff, retain the observed result,
   user-visible failure, remediation, retest, tool versions, artifact hashes,
   and useful screenshots in `log.md` or under `wiki/outputs/`.

A copied template that works only through an Obsidian plugin, case-folded path,
or normalization alias fails the check.
