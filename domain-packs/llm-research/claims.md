# LLM Research Claim Types

Use these claim types for structured evidence about LLM research systems, methods, benchmarks, limitations, costs, and implementation availability.

Claims can be represented as dedicated `wiki/claims/` pages with `type: claim`, or embedded in another page's frontmatter under `claims`.
Dedicated claim pages get the configured `claim_type` allowed-value checks from `research.overlay.yml`. Embedded claims should follow the same shape, but the current reusable linter only applies generic structured-claim checks to embedded records.

## Common Claim Page Frontmatter

```yaml
---
type: claim
created: YYYY-MM-DD
updated: YYYY-MM-DD
source_ids:
  - paper:source-id
claim_type: benchmark_result
subject: System or method name
predicate: reports
object: Short evidence statement
metric: optional metric name
value: 0
unit: optional unit
scope: Benchmark, dataset, task, version, or evaluation setting
summary: One-sentence claim summary.
---
```

Common required fields:

- `claim_type`: one of the domain claim categories below.
- `subject`: system, method, benchmark, dataset, artifact, or source being described.
- `predicate`: relationship such as `reports`, `outperforms`, `uses`, `requires`, `lacks`, `limits`, or `releases`.
- `object`: concise claim object or evidence statement.
- `source_ids`: source IDs that directly support the claim.
- `scope`: setting where the claim applies.

Common optional fields:

- `metric`: metric, cost dimension, resource, artifact, or property name.
- `value`: scalar numeric, string, or boolean value when the source reports one.
- `unit`: unit for `value`, such as `percent`, `tasks`, `tokens`, `USD`, `GPU-hours`, `seconds`, or `available`.

Do not create claims from interpretation alone. If a field is missing from the source, omit it or record the gap in the body text.

## Claim Categories

| Claim Type | Use For | Category-Specific Fields |
|------------|---------|--------------------------|
| `benchmark_result` | A reported score, success rate, win rate, rank, or measured outcome on a benchmark, task, or dataset. | `metric`, `value`, `unit`, `scope`, benchmark/task name, baseline when applicable. |
| `ablation_result` | A comparison showing the effect of removing, adding, or changing a component, method, tool, memory strategy, or model. | `metric`, `value`, `unit`, `scope`, changed component, baseline condition. |
| `architectural_claim` | Evidence about system components, control flow, model roles, interfaces, or architecture design. | `scope`, affected component, evidence location. |
| `method_claim` | Evidence about a reusable method, orchestration pattern, state/memory strategy, training loop, search procedure, or evaluation workflow. | `scope`, method name, system context when applicable. |
| `limitation` | Stated or observed failure mode, scope limit, evaluation weakness, safety issue, reproducibility barrier, or missing evidence. | `scope`, affected system/method/benchmark, severity when known. |
| `cost_runtime_claim` | Reported cost, runtime, token use, sample count, compute, latency, throughput, or resource requirement. | `metric`, `value`, `unit`, `scope`, hardware/model setting when known. |
| `availability_claim` | Evidence about released code, model weights, datasets, documentation, licenses, installation instructions, or missing artifacts. | `metric`, `value`, `unit`, `scope`, repository or artifact reference. |
| `factual` | Source-grounded fact that does not fit the more specific categories. | `scope` and enough context to avoid overgeneralization. |

## Examples

### Benchmark Result

```yaml
---
type: claim
created: YYYY-MM-DD
updated: YYYY-MM-DD
source_ids:
  - paper:source-id
claim_type: benchmark_result
subject: Example system
predicate: reports
object: Example system reaches a reported task success score on ExampleBench.
metric: task_success
value: 42
unit: percent
scope: ExampleBench, reported paper setting
summary: Example system reports a 42 percent task success score on ExampleBench.
---
```

### Method Or Architecture Claim

```yaml
---
type: claim
created: YYYY-MM-DD
updated: YYYY-MM-DD
source_ids:
  - paper:source-id
claim_type: method_claim
subject: File-based state handoff
predicate: supports
object: Repeated agent handoffs through persistent project files.
scope: Example system architecture section
summary: The source describes file-based state handoff as part of the system workflow.
---
```

### Limitation

```yaml
---
type: claim
created: YYYY-MM-DD
updated: YYYY-MM-DD
source_ids:
  - paper:source-id
claim_type: limitation
subject: Example system
predicate: lacks
object: Evaluation does not include independent replication.
scope: Reported evaluation limitations
summary: The source states that independent replication is missing.
---
```

### Cost Or Runtime

```yaml
---
type: claim
created: YYYY-MM-DD
updated: YYYY-MM-DD
source_ids:
  - paper:source-id
claim_type: cost_runtime_claim
subject: Example system
predicate: reports
object: Example system uses a reported compute budget for one evaluation run.
metric: compute
value: 12
unit: GPU-hours
scope: Example evaluation run
summary: The source reports a 12 GPU-hour compute budget for one evaluation run.
---
```

### Availability

```yaml
---
type: claim
created: YYYY-MM-DD
updated: YYYY-MM-DD
source_ids:
  - paper:source-id
claim_type: availability_claim
subject: Example system repository
predicate: releases
object: Source code is available in a public repository.
metric: code_release
value: true
unit: available
scope: Repository link cited by the paper
summary: The cited source links to a public code repository.
---
```

## Body Structure

Use only the sections needed for review.

- Evidence: source note, normalized record section, figure, table, or repository reference.
- Interpretation Boundary: what the source supports and what it does not.
- Related Pages: systems, methods, benchmarks, datasets, synthesis, or questions.
- Open Questions: missing scope, missing artifact, unclear baseline, or replication gap.
