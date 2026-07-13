# Coverage Manifest Contract

Coverage manifests are per-question, machine-readable answerability contracts.
They describe which facets of a question must be grounded before a high-stakes
answer can be treated as complete. The manifest is evidence state, not prose
memory, so it lives under `sources/coverage/<slug>.yml` beside the other
source-pipeline artifacts.

Use `sources/coverage/<slug>.yml`, where `<slug>` equals the question page slug
without `.md`. This keeps `wiki/questions/` focused on the human-readable task
record and keeps evaluator-owned state under `sources/`. Existing workspaces
that do not yet declare `sources.coverage_dir` should be interpreted as using
the default `sources/coverage`.

`coverage_manifest` schema version `1.0` is declarative only. It defines the
artifact shape and allowed identifiers. `scripts/coverage_manifest.py`
initializes, validates, updates, and evaluates this artifact without using an
LLM. Resolver gating, status, lint, export propagation, and domain-pack
templates are separate contracts layered on top of this file.

## Required Shape

Each manifest is a YAML mapping:

```yaml
schema_version: "1.0"
question_slug: minimal-social-security-fee
created_at: "2026-06-29T00:00:00Z"
updated_at: "2026-06-29T00:00:00Z"
coverage_profile: legal-current-figure
coverage_verdict: pending
required_facets:
  - facet_id: current-reduced-fee-amount
    description: Current reduced social-security fee amount for new autonomos.
    required: true
    evidence_path: legal_current_figure
    source_policy: official_primary
    freshness_policy: current_legal_figure
    identity_policy: official_domain_match
    min_sources: 1
    accepted_artifact_kinds: []
    accepted_source_ids: []
    blocking_request_ids:
      - req-current-reduced-fee
    facet_verdict: blocked
optional_facets: []
```

Equivalent contract metadata appears in `evidence-wiki contract`:

```json
{
  "schema_version": "1.0",
  "artifact_schemas": {
    "coverage_manifest": "1.0"
  },
  "policy_vocabularies": {
    "evidence_paths": [
      "academic_method_existence",
      "github_implementation",
      "legal_current_figure",
      "official_guidance",
      "product_requirement_profile",
      "standards_registry_reference",
      "vendor_product_spec"
    ],
    "artifact_kinds": [
      "release_metadata",
      "repository_metadata",
      "source_archive"
    ],
    "source_policy": [
      "academic_indexed",
      "canonical_repository",
      "domain_pack_allowed",
      "manual_review_required",
      "official_primary",
      "official_standards_registry",
      "official_vendor",
      "openalex_or_arxiv",
      "primary_or_official",
      "standards_body_primary"
    ],
    "freshness_policy": [
      "current_legal_figure",
      "current_product_spec",
      "current_product_requirement",
      "current_standard_reference",
      "manual_review",
      "no_staleness_check",
      "publication_identity",
      "release_snapshot"
    ],
    "identity_policy": [
      "citation_id_resolves",
      "none",
      "official_domain_match",
      "origin_url_matches_candidate",
      "registry_entry_matches_product_requirement",
      "repo_ref_resolves",
      "standard_designation_matches_registry"
    ]
  }
}
```

## CLI

Use `scripts/coverage_manifest.py` from a workspace root, or pass
`--project-root` explicitly:

```bash
python3 scripts/coverage_manifest.py init --slug minimal-social-security-fee --coverage-profile legal-current-figure
python3 scripts/coverage_manifest.py init --slug turboquant-existence --template coverage-template.yml
python3 scripts/coverage_manifest.py init --slug turboquant-existence --template domain-packs/llm-research/coverage-templates/academic-method-feasibility.yml
python3 scripts/coverage_manifest.py init --slug turboquant-existence --template domain-packs/llm-research/coverage-templates/academic-negative-claim-probe.yml
python3 scripts/coverage_manifest.py show --slug minimal-social-security-fee --format json
python3 scripts/coverage_manifest.py validate --slug minimal-social-security-fee --format json
python3 scripts/coverage_manifest.py set-facet --slug minimal-social-security-fee \
  --facet-id current-reduced-fee-amount \
  --accepted-source-id web:seg-social-cuota-reducida-2026 \
  --clear-blocking-request-ids
python3 scripts/coverage_manifest.py evaluate --slug minimal-social-security-fee --format json
```

`init` creates `sources/coverage/<slug>.yml` for an existing question page.
`--coverage-profile NAME` records the profile label only; it does not populate
facets. Use `--template PATH` when the manifest should be initialized with
required and optional facets in the same command.
Relative `--template` paths are resolved from `--project-root`, so initialized
workspaces can pass generated domain-pack paths such as
`domain-packs/llm-research/coverage-templates/academic-method-feasibility.yml`.
A template file is a declarative YAML mapping with `coverage_profile`,
`required_facets`, and `optional_facets`; template facets define policies and
minimum counts, not source URLs. Missing `accepted_source_ids`,
`blocking_request_ids`, `accepted_artifact_kinds`, and `facet_verdict` values
default to empty lists and `pending`. A template may include `claim_probe`
metadata for bounded negative probes; it is copied into the manifest, but it
does not satisfy `min_sources`.

`accepted_artifact_kinds` is optional and is interpreted by the evidence-policy
helper, not by the min-source coverage evaluator. For `github_implementation`
facets, omitting it means source-code evidence must be a captured GitHub source
archive. A facet can explicitly permit metadata-only repository evidence:

```yaml
accepted_artifact_kinds:
  - repository_metadata
  - release_metadata
```

## Domain-Pack Templates

Domain packs may declare reusable templates under
`domain_pack.coverage_templates` in `research.overlay.yml`:

```yaml
domain_pack:
  coverage_templates:
    official-current-figure: coverage-templates/official-current-figure.yml
```

Workspace initialization prefixes these pack-local paths in `research.yml`, for
example `domain-packs/legal-regulatory/coverage-templates/official-current-figure.yml`.
The domain-pack validator checks that each declared path stays inside the pack,
exists, and conforms to the coverage-template schema. Templates remain
guidance-only: they define coverage profile, facets, evidence paths, and policy
identifiers, but never source URLs, accepted source IDs, or fetched evidence.

Initial reusable templates:

- `llm-research`: `academic-method-feasibility`,
  `academic-negative-claim-probe`, and `vendor-product-spec`.
- `legal-regulatory`: `official-current-figure`.
- `standards-compliance`: `official-standard-reference`,
  `standards-current-version`, `eu-product-requirement-profile`, and
  `uk-geospatial-standard-register-entry`.

`set-facet` updates only the named facet. Accepted source IDs must exist in
`sources/manifest.jsonl`; blocking request IDs must exist in
`sources/source-requests.jsonl` and, when the request record carries
`question_slugs`, must reference the manifest question slug.

`evaluate` is deterministic: required facets pass only when they have at least
`min_sources` accepted source IDs, no blocking request IDs, and no accepted
source is marked unusable evidence by the local policy layer. Required facets
otherwise block. Optional facets use the existing count rule, except optional
facets with `min_sources: 0` and no blockers evaluate to `not_applicable`. The
top-level `coverage_verdict` is `pass` only when every required facet passes,
`blocked` when any required facet blocks, and `pending` when no required facets
exist. An unconfirmed `claim_probe` never changes this lifecycle: a required
method or artifact existence facet with no accepted source remains `blocked`.

JSON evaluation results and status/export summaries include `policy_results`
per facet when accepted sources are present. These results expose local policy
diagnostics such as unusable official error pages, stale currentness metadata,
mismatched origins, or missing repository refs. The evidence-usability gate uses these diagnostics to
block required facets only when accepted sources are explicitly unusable
evidence; broader policy-enforcement gates remain separate evidence-policy work.
Policy results are not written back into the coverage manifest file; the manifest
stores only verdict fields and the accepted/blocking IDs.

## Resolver Gate

High-stakes answers can require this manifest before the question lifecycle moves
to `answered`:

```bash
python3 scripts/question_resolve.py answer \
  --slug minimal-social-security-fee \
  --agent-id agent-a \
  --answer-page wiki/synthesis/minimal-social-security-fee.md \
  --source-id web:seg-social-cuota-reducida-2026 \
  --require-coverage \
  --format json
```

`--require-coverage` is the active gate. Without it, `question_resolve.py
answer` keeps the normal source-id-only behavior so non-high-stakes and ad hoc
questions can still be resolved. `--coverage-manifest PATH` only selects a
workspace-relative manifest path under `sources.coverage_dir`; by itself it does
not enforce coverage. When omitted, the resolver uses
`sources/coverage/<slug>.yml`.

The resolver evaluates the selected manifest in memory before writing the
question page, so stale stored verdicts cannot bypass or falsely block the gate.
On success it records `coverage_required: true` and
`coverage_manifest: sources/coverage/<slug>.yml` in the question frontmatter so
status, lint, and export can identify covered high-stakes answers after the
resolver command has finished.
Facet policy results can still require human review. `manual_review` can pass
coverage when the source is otherwise acceptable, but publication readiness
stays `no_ship` until `question_resolve.py approve` records the reviewer,
timestamp, and approval state.
It refuses the answer with:

- `COVERAGE_REQUIRED` when `--require-coverage` is set but the selected manifest
  is missing.
- `COVERAGE_MANIFEST_INVALID` when the manifest path is unsafe, outside
  `sources.coverage_dir`, malformed, for another slug, or violates this schema.
- `COVERAGE_BLOCKED` when evaluation does not produce `coverage_verdict: pass`.

JSON errors include `manifest_path` and, for blocked coverage, the evaluated
`coverage_verdict` plus failed required facet ids.

## Status, Lint, and Export

`scripts/workspace_status.py --format json` includes a top-level `coverage`
mapping with `manifests_total`, `required_questions`, `passed`, `blocked`,
`pending`, `missing`, `invalid`, `coverage_verdicts`, and
`required_question_counts`. Manifest-level counts evaluate every
`sources/coverage/*.yml` file, including gate-blocked manifests that exist
before successful answer frontmatter is written. `coverage_verdicts` maps each
manifest slug to its evaluated verdict. The nested `required_question_counts`
object preserves the answered/human-review question view for questions that
carry `coverage_required: true`.

`scripts/lint.py` reports HIGH findings for answered questions that carry
`coverage_required: true` but do not have passing required coverage:

- `question_coverage_missing`: the selected manifest is absent.
- `question_coverage_blocked`: the selected manifest evaluates to a non-pass
  verdict such as `blocked` or `pending`.
- `question_coverage_invalid`: the selected manifest path is unsafe, malformed,
  for another question slug, or otherwise violates this schema.

`scripts/export_answers.py --format json` includes coverage fields on every
question record: `coverage_required`, `coverage_manifest`, `coverage_status`,
`coverage_verdict`, `coverage_facets`, `failed_facets`,
`linked_source_requests`, `missing_source_request_ids`, and
`unconfirmed_claims`. Facet records preserve `claim_probe` metadata when
present.

## Top-Level Fields

| Field | Type | Meaning |
|-------|------|---------|
| `schema_version` | string | Coverage manifest schema version. Current value: `"1.0"`. |
| `question_slug` | string | Question page slug; must match the file name in `sources/coverage/<slug>.yml`. |
| `created_at` | string | UTC creation timestamp, formatted like `2026-06-29T00:00:00Z`. |
| `updated_at` | string | UTC update timestamp, formatted like `2026-06-29T00:00:00Z`. |
| `coverage_profile` | string | Short profile name chosen by the workspace or future domain-pack template. |
| `required_facets` | list | Facets that must pass before the question has complete coverage. |
| `optional_facets` | list | Supplemental facets that can enrich export but do not block the top-level verdict. |
| `coverage_verdict` | string | One of `pending`, `pass`, or `blocked`. |

## Facet Fields

Every item in `required_facets` and `optional_facets` has the same shape:

| Field | Type | Meaning |
|-------|------|---------|
| `facet_id` | string | Stable identifier unique within the manifest. |
| `description` | string | Human-readable statement of the facet being checked. |
| `required` | boolean | `true` for required facets, `false` for optional facets. |
| `evidence_path` | string | Evidence family used to interpret source and identity policies. |
| `source_policy` | string | Required authority level for sources that can satisfy the facet. |
| `freshness_policy` | string | Source-currentness or publication-identity requirement. |
| `identity_policy` | string | Identifier or origin check that prevents fabricated evidence. |
| `min_sources` | integer | Minimum accepted source count. `0` is allowed only when the facet can be explicitly out of scope or blocked. |
| `accepted_source_ids` | list | Manifest source IDs already accepted for this facet. |
| `blocking_request_ids` | list | Source-request IDs that must be fulfilled before the facet can pass. |
| `facet_verdict` | string | One of `pending`, `pass`, `blocked`, or `not_applicable`. |
| `claim_probe` | mapping | Optional bounded negative-probe metadata for unconfirmed academic method or artifact existence claims. |

### Claim Probe Fields

`claim_probe` is for bounded provider probes of a method, paper, dataset,
benchmark, or artifact existence claim. It records that configured providers did
not confirm the claim during this run. It is not evidence of global
nonexistence and cannot make a required facet pass.

Allowed shape:

```yaml
claim_probe:
  claim_type: method_or_artifact_existence
  claim_text: TurboQuant exists as a published scholarly method or artifact.
  claim_verdict: unconfirmed
  limitation: not found in configured providers for this bounded run; not a global nonexistence claim
  bounded_provider_results:
    - provider: arxiv
      query: TurboQuant
      max_results: 5
      result_count: 0
      exact_match_count: 0
      network_io_executed: true
    - provider: openalex
      query: TurboQuant
      max_results: 5
      result_count: 1
      exact_match_count: 0
      network_io_executed: true
```

Validation is deliberately narrow: `claim_type` must be
`method_or_artifact_existence`, `claim_verdict` must be `unconfirmed`, providers
must be exactly `arxiv` and `openalex`, and every `exact_match_count` must be
zero. The `limitation` string must be exactly
`not found in configured providers for this bounded run; not a global nonexistence claim`.
If the facet later accepts a source, remove the unconfirmed `claim_probe`.

## Verdict Values

`coverage_verdict` values:

- `pending`: coverage has not been evaluated, or some facets are still open.
- `pass`: every required facet has `facet_verdict: pass`.
- `blocked`: at least one required facet is blocked by missing, stale, or invalid evidence.

`facet_verdict` values:

- `pending`: the facet is defined but not yet evaluated.
- `pass`: the facet has enough accepted evidence under its policies.
- `blocked`: the facet lacks acceptable evidence, usually with linked source requests.
- `not_applicable`: the facet is optional or explicitly out of scope for this question.

## Evidence Paths

Version 1.0 defines these initial `evidence_path` values:

| Value | Purpose |
|-------|---------|
| `legal_current_figure` | Current legal, tax, fee, threshold, deadline, or benefit figures from official sources. |
| `academic_method_existence` | A named academic method or artifact exists in a resolvable scholarly index. |
| `github_implementation` | A code implementation is tied to a canonical repository and ref. |
| `official_guidance` | Official operational, safety, response, standards-body, or best-practice guidance from a public authority or recognized response body. |
| `standards_registry_reference` | A standards registry record identifies designation, edition, currentness, authority, or replacement status. |
| `product_requirement_profile` | Product-compliance guidance, harmonised-standard linkage, OJEU/legal-act metadata, or equivalent requirement profile. |
| `vendor_product_spec` | A product capability is grounded in an official vendor source. |

## Policy Identifiers

The full version 1.0 policy vocabulary is defined in
[Evidence Policy Vocabulary](evidence-policies.md). The same allowed values are
published by `evidence-wiki contract` under `policy_vocabularies`.

`source_policy` values:

- `official_primary`
- `primary_or_official`
- `academic_indexed`
- `openalex_or_arxiv`
- `canonical_repository`
- `official_vendor`
- `official_standards_registry`
- `standards_body_primary`
- `domain_pack_allowed`
- `manual_review_required`

`freshness_policy` values:

- `current_legal_figure`
- `current_product_spec`
- `current_standard_reference`
- `current_product_requirement`
- `publication_identity`
- `release_snapshot`
- `no_staleness_check`
- `manual_review`

`identity_policy` values:

- `citation_id_resolves`
- `origin_url_matches_candidate`
- `repo_ref_resolves`
- `official_domain_match`
- `standard_designation_matches_registry`
- `registry_entry_matches_product_requirement`
- `none`

`accepted_artifact_kinds` values:

- `source_archive`
- `repository_metadata`
- `release_metadata`

## Example Coverage Profiles

- Legal current fee: a local fee answer can require a
  `legal_current_figure` facet with `official_primary`,
  `current_legal_figure`, and `official_domain_match`.
- Academic method existence: the TurboQuant probe can require
  `academic_method_existence` with `openalex_or_arxiv`,
  `publication_identity`, and `citation_id_resolves`. If no real record
  resolves, the facet should be `blocked`, not fabricated. A bounded
  `claim_probe` may record arXiv/OpenAlex non-confirmation, but it must keep the
  limitation that this is not a global nonexistence claim.
- GitHub implementation: implementation evidence can require
  `github_implementation` with a canonical repository and resolving ref.
- Vendor product spec: product hardware or service claims can require
  `vendor_product_spec` from an official vendor origin.

Fixtures for all four paths live in
`tests/fixtures/coverage-manifests/`.
