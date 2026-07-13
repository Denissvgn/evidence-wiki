# Domain Guidance Generator

Project-local domain guidance is the lightweight option for a new research scope when no reusable domain pack clearly matches. It gives agents extraction and filing rules for the first research cycles without asking the user to design a reusable pack up front.

The generator is deterministic. `research-init` infers the domain guidance content, writes it into a setup profile, and `evidence-wiki init` renders a Markdown guidance document during workspace creation when the profile uses `domain_guidance.mode: project_local`.

## When To Use Local Guidance

Use project-local guidance when:

- the research scope has domain-specific extraction needs,
- no existing domain pack clearly matches,
- the guidance is useful for this workspace but not yet proven reusable,
- the user has not requested a reusable domain pack,
- the first ingest cycle needs concrete filing and claim rules.

Use `domain_guidance.mode: none` when the generic taxonomy is enough. Use `domain_guidance.mode: deferred` when the domain cannot be judged safely before sources are inspected. Use `domain_guidance.mode: domain_pack` only when a reusable pack is already available and clearly matches.

## Setup Profile Fields

Add the guidance under `workspace_init.domain_guidance`:

```yaml
domain_guidance:
  mode: project_local
  path: docs/project-domain-guidance.md
  scope: Research workflow for a specific project domain.
  rationale: No reusable domain pack matches this scope yet.
  source_priorities:
    - Official documentation before secondary commentary.
    - Primary datasets before benchmark summaries.
  extraction_targets:
    - important domain entity
    - important metric or status
  claim_types:
    - domain_specific_claim
    - factual
  filing_rules:
    - File source summaries under wiki/sources.
    - File unresolved evidence gaps under wiki/questions.
  output_scaffolds:
    - domain brief
    - decision checklist
  promotion_notes:
    - Promote only if these rules repeat across multiple workspaces.
```

`path` is optional and defaults to `docs/project-domain-guidance.md`. It must be workspace-relative and cannot contain `..`, URLs, or absolute paths.

## Generated Document

The generated Markdown document includes:

- project scope and domain decision rationale,
- source priorities,
- extraction targets,
- claim types,
- filing rules,
- output scaffolds,
- promotion notes,
- setup-profile assumptions and skipped decisions,
- guardrails for keeping guidance local.

The initializer records the generated path in `log.md`. The document is copied only into the created workspace. It does not create `domain-packs/` and does not modify reusable domain-pack files.

## Promotion To A Domain Pack

Keep guidance local until at least one of these is true:

- multiple workspaces need the same extraction targets and filing rules,
- domain-specific claim types need reusable lint or frontmatter rules,
- scaffolds are stable enough to share,
- source-priority rules are broadly applicable,
- the user explicitly asks for reusable guidance.

A promoted domain pack should include:

- `README.md` with scope and application instructions,
- `research.overlay.yml` with partial config to merge into `research.yml`,
- `taxonomy.md` with page placement, extraction targets, and filing rules,
- `claims.md` with structured claim categories and examples,
- optional scaffold files under `scaffolds/`,
- any source-priority guidance that agents should reuse.

Use `docs/prototype-domain-pack-proposal.md` and `domain-packs/llm-research/`
as reference examples when packaging reusable guidance.

## Guardrails

- Do not copy pilot data or prototype content into generated guidance.
- Do not mutate raw source evidence while generating guidance.
- Do not create a reusable domain pack unless the user requests one.
- Keep local guidance concise enough for the first ingest cycle.
- Revisit local guidance after source notes reveal repeated patterns.
