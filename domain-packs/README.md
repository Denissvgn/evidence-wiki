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
fetch routes. They may separately declare `domain_pack.recommended_discovery`
provider IDs to identify useful candidate-metadata routes. Recommendations are
report-only: they do not enable either integration and never authorize network
access.

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

Pack overlays may also declare optional `domain_pack.request_kinds` as a list
of source-request kind declarations, each requiring `id`, `label`, and
`description`. A declared id must be namespaced `pack:<pack-name>/<kind-id>` —
the same convention pack evidence policies use — and its namespace segment
must equal the pack's own `domain_pack.name`, so one pack can never declare
kinds in another pack's namespace. Built-in kinds (`paper`, `dataset`, `web`,
`code`, `structured_data`, `other`) are reserved and cannot be redeclared.
`evidence-wiki pack validate` checks id shape, namespace, and uniqueness
before a pack ships; `source_requests.py add --kind` accepts a declared kind
once the pack is installed in a workspace and refuses an undeclared or
malformed one.

```yaml
domain_pack:
  name: market-data
  request_kinds:
    - id: pack:market-data/supplier_quote
      label: Supplier quote
      description: Live SKU price + shipping + MOQ from a named supplier, ≤ 48h old.
```

## Policy Rules

Pack overlays may also declare optional `domain_pack.policy_rules`, which say how
the pack's own evidence policies are decided instead of falling to
`manual_review`. Declare the vocabulary definition text first — the sentence a
reviewer would act on — then add a rule only for the part of that sentence a
machine can settle from data the workspace already holds: the source's
structured-view sidecar, its delivery provenance, or the question's frontmatter.
A rule is data, never code. A pack names primitives from a closed set —
`max_age`, `equals`, `numeric_range`, `regex`, and `one_of_provenance`, composed
with `all_of` and `any_of` — and the package evaluates them.

```yaml
domain_pack:
  name: market-data
  policy_vocabularies:
    freshness_policy:
      pack:market-data/quote-48h: A supplier quote must be at most 48 hours old.
  policy_rules:
    pack:market-data/quote-48h:
      all_of:
        - max_age: {field: provenance/retrieved_at, hours: 48}
```

Keep judgement calls manual. `pack:general-science/study-recency` asks a reviewer
to confirm that study dates, dataset releases, and follow-up literature are recent
enough *for the scientific question*, and no threshold is right across studies.
It ships with no rule, and it should stay that way: writing one would replace a
reviewer's judgement with a number nobody agreed on. The same applies to any
definition asking whether evidence is adequate, appropriate, comparable, or
representative. Where a policy is partly mechanical, add the rule and set
`manual_review_required: true` — the checks run and the answer still routes to a
human, which narrows what the reviewer must inspect without pretending the
judgement was made.

`evidence-wiki pack validate` reports a `policy_rules` check before the pack
ships, and a rule-backed policy that does not set `manual_review_required` counts
as deterministic, so a required coverage facet may use it without the pack
declaring `domain_pack.human_gated: true`. Rule syntax, field references, and
verdicts are documented in
`workspace-template/docs/evidence-policies.md`.

## Reference Packs

- `llm-research`: guidance for LLM systems, autonomous research agents,
  benchmarks, datasets, and implementation availability.
- `general-science`: guidance for broad scientific literature reviews,
  methods comparisons, dataset inventories, evidence maps, and reproducibility
  analysis. Recommends `arxiv` and `openalex` for both discovery and acquisition
  when a workspace explicitly enables each phase.
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
uses valid `recommended_discovery` and `recommended_acquisition` providers when present, validates declared
`coverage_templates` and `policy_rules`, matches the starter `research.yml`
contract, deep-merges with the starter configuration, and smoke-validates after
initialization in a temporary workspace under `/tmp`.
