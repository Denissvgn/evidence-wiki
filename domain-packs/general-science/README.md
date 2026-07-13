# General Science Domain Pack

Domain pack for broad scientific literature workspaces that compare papers,
datasets, methods, instruments, models, hypotheses, and reproducibility
evidence across one or more research fields.

This pack extends the reusable research workspace with general scientific
research guidance. It does not add scripts, fetch sources, copy raw data, or
replace core workspace tools.

## Files

- `research.overlay.yml`: configuration fragment to merge into a workspace
  `research.yml`.
- `taxonomy.md`: page placement, extraction targets, filing rules, and
  synthesis outputs for general scientific evidence.
- `scaffolds/source-paper.md`: source-note scaffold for papers, preprints, and
  technical reports.
- `scaffolds/method.md`: page scaffold for reusable scientific methods,
  protocols, instruments, and analysis workflows.
- `scaffolds/dataset.md`: page scaffold for datasets, corpora, measurements,
  and data products.
- `claims.md`: structured claim categories, required fields, and examples for
  scientific evidence.

## Applying The Pack

1. Start from a configured research workspace.
2. Deep-merge `research.overlay.yml` into that workspace's `research.yml` so
   base wiki rules are preserved.
3. Use `taxonomy.md` to decide where papers, methods, datasets, evidence
   claims, hypotheses, synthesis notes, questions, and decisions belong.
4. Use `claims.md` to write structured claim pages and embedded claims.
5. Keep reusable workspace scripts unchanged.

The overlay is intentionally partial. A workspace must still provide the base
configuration sections required by the reusable template.

## Acquisition Guidance

This pack recommends `arxiv` and `openalex` as acquisition providers when a
workspace has explicitly enabled acquisition. The recommendation is advisory:
initialization reports surface it for planners and fetch agents, but
`integrations.acquisition.enabled` remains `false` by default.
