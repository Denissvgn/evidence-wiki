# LLM Research Domain Pack

Domain pack for research workspaces focused on autonomous research systems, LLM-based research agents, evaluation methods, benchmarks, datasets, and implementation availability.

This pack extends the reusable research workspace with domain-specific configuration. It does not duplicate the workspace starter, copy raw data, or replace core scripts.

## Files

- `research.overlay.yml`: configuration fragment to merge into a workspace `research.yml`.
- `taxonomy.md`: page placement, extraction targets, and filing rules for LLM research evidence.
- `scaffolds/source-paper.md`: source-note scaffold for academic papers and technical reports during `research-ingest`.
- `scaffolds/system.md`: page scaffold for concrete LLM research systems and implementations.
- `scaffolds/method.md`: page scaffold for reusable LLM research techniques and patterns.
- `claims.md`: structured claim categories, required fields, and examples for LLM research evidence.
- `coverage-templates/academic-negative-claim-probe.yml`: answerability template for bounded arXiv/OpenAlex checks of unconfirmed named methods or artifacts.
- `coverage-templates/academic-method-feasibility.yml`: answerability template for named academic methods or artifacts.
- `coverage-templates/vendor-product-spec.yml`: answerability template for official vendor product specifications.

## Applying The Pack

1. Start from a configured research workspace.
2. Deep-merge `research.overlay.yml` into that workspace's `research.yml` so base wiki rules are preserved.
3. Use `taxonomy.md` to decide where source notes, systems, methods, benchmark results, datasets, claims, synthesis pages, questions, and decisions belong.
4. Use `claims.md` to write structured claim pages and embedded claims.
5. Use the coverage templates to seed answerability manifests for academic method and vendor product-spec questions.
6. Keep the reusable workspace scripts unchanged.

The overlay is intentionally partial. A workspace must still provide the base configuration sections required by the reusable template.
