# General Science Claim Types

Use these claim types for structured evidence about scientific hypotheses,
methods, datasets, empirical results, limitations, and reproducibility.

Claims can be represented as dedicated `wiki/claims/` pages with `type: claim`,
or embedded in another page's frontmatter under `claims`. Dedicated claim pages
get the configured `claim_type` allowed-value checks from
`research.overlay.yml`.

## Common Claim Page Frontmatter

```yaml
---
type: claim
created: YYYY-MM-DD
updated: YYYY-MM-DD
source_ids:
  - paper:source-id
claim_type: empirical_result
subject: Study, method, dataset, variable, or phenomenon
predicate: reports
object: Short evidence statement
metric: optional metric name
value: 0
unit: optional unit
scope: Population, system, setting, dataset, or evaluation condition
summary: One-sentence claim summary.
---
```

Common required fields:

- `claim_type`: one of the domain claim categories below.
- `subject`: study, method, dataset, variable, phenomenon, system, or source
  being described.
- `predicate`: relationship such as `reports`, `supports`, `contradicts`,
  `uses`, `measures`, `limits`, or `releases`.
- `object`: concise claim object or evidence statement.
- `source_ids`: source IDs that directly support the claim.
- `scope`: setting where the claim applies.

Common optional fields:

- `metric`: metric, variable, endpoint, property, resource, or artifact name.
- `value`: scalar numeric, string, or boolean value when the source reports one.
- `unit`: unit for `value`, such as `percent`, `samples`, `p-value`, `kg`,
  `seconds`, `available`, or a domain-specific measurement unit.

Do not create claims from interpretation alone. If a field is missing from the
source, omit it or record the gap in the body text.

## Claim Categories

| Claim Type | Use For | Category-Specific Fields |
|------------|---------|--------------------------|
| `empirical_result` | A reported effect, association, estimate, benchmark score, measurement, or qualitative finding. | `metric`, `value`, `unit`, `scope`, uncertainty, comparison group when applicable. |
| `hypothesis_claim` | A stated hypothesis, proposed mechanism, theoretical expectation, or tested research question. | `scope`, mechanism, variables, predicted direction when stated. |
| `method_claim` | Evidence about a reusable method, protocol, instrument, simulation, model, workflow, or analysis procedure. | `scope`, method name, assumptions, inputs, outputs, validation evidence. |
| `dataset_claim` | Evidence about a dataset, corpus, observation series, data quality, access condition, license, or sampling process. | `metric`, `value`, `unit`, scope, sample criteria, access status. |
| `limitation` | Stated or observed validity threat, missing control, bias, confounding, measurement error, or scope limit. | `scope`, affected method/result/dataset, severity when known. |
| `reproducibility_claim` | Evidence about released data, code, protocols, preregistration, materials, environment, or independent replication. | `metric`, `value`, `unit`, artifact reference, replication status. |
| `factual` | Source-grounded fact that does not fit the more specific categories. | `scope` and enough context to avoid overgeneralization. |

## Examples

### Empirical Result

```yaml
---
type: claim
created: YYYY-MM-DD
updated: YYYY-MM-DD
source_ids:
  - paper:source-id
claim_type: empirical_result
subject: Example intervention
predicate: reports
object: Example intervention changes the measured endpoint in the reported study.
metric: endpoint_change
value: 12
unit: percent
scope: Reported study population and measurement window
summary: The source reports a 12 percent endpoint change for the example intervention.
---
```

### Method Claim

```yaml
---
type: claim
created: YYYY-MM-DD
updated: YYYY-MM-DD
source_ids:
  - paper:source-id
claim_type: method_claim
subject: Example protocol
predicate: measures
object: The protocol measures the target variable using the stated instrument.
scope: Methods section of the cited source
summary: The source describes the protocol used to measure the target variable.
---
```

### Reproducibility Claim

```yaml
---
type: claim
created: YYYY-MM-DD
updated: YYYY-MM-DD
source_ids:
  - paper:source-id
claim_type: reproducibility_claim
subject: Example study artifact
predicate: releases
object: Study code is available in a public repository.
metric: code_release
value: true
unit: available
scope: Artifact availability statement
summary: The cited source links to a public code repository.
---
```

## Body Structure

Use only the sections needed for review.

- Evidence: source note, normalized record section, figure, table, appendix,
  dataset card, protocol, or repository reference.
- Interpretation Boundary: what the source supports and what it does not.
- Related Pages: methods, datasets, concepts, benchmarks, synthesis, or
  questions.
- Open Questions: missing scope, missing data, unclear method detail, uncertain
  artifact access, or replication gap.
