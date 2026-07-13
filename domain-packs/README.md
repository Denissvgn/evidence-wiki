# Domain Packs

Domain packs are reusable, guidance-only overlays for research workspaces. A
pack lives in its own directory and must include `research.overlay.yml`; any
docs or scaffolds declared by that overlay must also live inside the pack.

## Create A Pack

Use `workspace-template/skills/domain-pack-create.md` when a planner or
orchestrator asks for reusable guidance for a fresh research domain. The skill
turns an orchestrator brief into pack-local `README.md`, `taxonomy.md`,
`claims.md`, `research.overlay.yml`, and optional scaffolds, then requires
validator and smoke-workspace checks before handoff.

Domain packs stay guidance-only. Do not add scripts, raw evidence, generated
source records, or project-specific workspace content to a pack.

Pack overlays may declare optional
`domain_pack.recommended_acquisition` provider IDs to help planners choose
fetch routes. The validator accepts only `arxiv` and `openalex` in this field
for now. Recommendations are report-only; they do not enable
`integrations.acquisition`.

Pack overlays may also declare optional `domain_pack.coverage_templates` as a
mapping from stable template slug to pack-local YAML file. Coverage templates
seed per-question answerability manifests with required facets and policies; they
must not include source URLs, raw evidence, generated records, or workspace-
specific answers.

```yaml
domain_pack:
  coverage_templates:
    official-current-figure: coverage-templates/official-current-figure.yml
```

## Reference Packs

- `llm-research`: guidance for LLM systems, autonomous research agents,
  benchmarks, datasets, and implementation availability.
- `general-science`: guidance for broad scientific literature reviews,
  methods comparisons, dataset inventories, evidence maps, and reproducibility
  analysis. Recommends `arxiv` and `openalex` as acquisition routes when a
  workspace explicitly enables acquisition.
- `legal-regulatory`: guidance for official-source legal, tax, regulatory,
  administrative, and public-policy research. Includes an
  `official-current-figure` coverage template for current figures from official
  primary sources.
- `standards-compliance`: guidance for standards registries, standards-body
  references, EU product requirements, and UK geospatial register evidence.
  Includes templates for exact standard references, current-version checks,
  EU product-requirement profiles, and GOV.UK geospatial register entries.

## Validate A Pack

Validate a packaged pack by name:

```bash
evidence-wiki pack validate --path llm-research
```

Validate a pack from a source checkout:

```bash
python3 tools/validate_domain_pack.py --path domain-packs/llm-research
```

The validator emits JSON. A valid pack returns exit code `0` with `ok: true`.
An invalid pack returns exit code `1` with `ok: false` and failed checks. Fatal
input errors, such as a missing pack path, use the shared JSON error envelope on
stderr and return exit code `2`.

Validation checks that `research.overlay.yml` parses, declares required
`domain_pack` metadata, references existing pack-local docs and scaffolds,
uses valid `recommended_acquisition` providers when present, validates declared
`coverage_templates`, matches the starter `research.yml` contract, deep-merges
with the starter configuration, and smoke-validates after initialization in a
temporary workspace under `/tmp`.
