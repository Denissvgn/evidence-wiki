# Domain Pack Proposal Template

Use this template when a research workspace needs reusable domain guidance that
does not belong in the generic workspace core. The goal is to define an
optional pack that can be applied to future workspaces without copying project
content, private notes, or one-off assumptions into the starter.

Proposed pack name: `example-domain`.

Intended scope: describe the research domain, decision context, source types,
and recurring outputs the pack should support. Keep the scope narrow enough that
the pack has clear extraction targets and source-priority rules.

## Core Workspace Versus Domain Pack

The reusable workspace core should stay domain-neutral:

- `raw/` stores immutable source evidence.
- `sources/` stores generated manifests, normalized records, and source cards.
- `wiki/` stores maintained knowledge pages.
- `index.md` remains the navigation surface.
- `log.md` remains the append-only operation history.
- Scripts handle inventory, normalization, lint, local query, snapshots, and
  deterministic health checks.
- Generic skills handle ingest, query, lint, synthesis, scouting, and question
  tracking.

The domain pack should own:

- Domain taxonomy and recommended page placement.
- Domain-specific extraction targets.
- Source priority and conflict-resolution guidance.
- Structured claim types for facts that are likely to change or conflict.
- Scaffolds for decisions, briefs, tables, reports, and presentations.
- Filing rules that help agents promote source evidence into maintained wiki
  pages consistently.

## Assumptions To Move Into The Pack

Use this section to make domain assumptions explicit and optional. Good
assumptions are specific enough to guide agents, but not so specific that they
encode one workspace's private history.

Examples:

- The research subject has recurring source types such as official guidance,
  standards, reports, datasets, implementation repositories, or policy briefs.
- Important facts are often scoped by date, region, version, methodology, or
  operating context.
- Source reliability depends on publisher authority, recency, and evidence
  traceability.
- Decisions need structured rationale, not just prose summaries.
- Outputs need repeatable formats such as decision records, comparison tables,
  evidence maps, or briefings.

## Proposed Pack Files

A later implementation can create `domain-packs/example-domain/` with:

- `README.md`: purpose, scope, and application instructions.
- `research.overlay.yml`: domain taxonomy, claim rules, and recommended outputs
  to merge into a configured workspace.
- `taxonomy.md`: page placement, extraction targets, filing rules, and scope
  handling.
- `claims.md`: structured claim types for important domain evidence.
- `source-priorities.md`: source ranking and conflict-resolution guidance.
- `scaffolds/decision-record.md`: decision record scaffold.
- `scaffolds/output-brief.md`: reusable brief or report scaffold.
- `scaffolds/comparison-table.md`: comparison table scaffold.
- `scaffolds/presentation-outline.md`: presentation outline scaffold.

The pack should extend a configured workspace. It should not duplicate core
scripts, raw data, generated source records, or generic skills.

## Page Taxonomy And Migration Mapping

Prefer the generic workspace directories when they are sufficient:

| Source area | Proposed directory | Page type | Notes |
|-------------|--------------------|-----------|-------|
| Source notes | `wiki/sources/` | `source` | Summaries of normalized source records. |
| Organizations, people, tools, or programs | `wiki/entities/` | `entity` | Named things that recur across sources. |
| Definitions and reusable ideas | `wiki/concepts/` | `concept` | Concepts, terms, categories, and distinctions. |
| Techniques or workflows | `wiki/methods/` | `method` | Methods, processes, algorithms, or operating practices. |
| Implementations or products | `wiki/systems/` | `system` | Concrete systems, products, or architectures. |
| Evaluation protocols | `wiki/benchmarks/` | `benchmark` | Benchmarks, scoring protocols, or readiness checks. |
| Data resources | `wiki/datasets/` | `dataset` | Datasets, corpora, tables, and data cards. |
| Evidence statements | `wiki/claims/` | `claim` | Structured source-grounded facts. |
| Cross-source analysis | `wiki/synthesis/` | `synthesis` | Maps, comparisons, reviews, and summaries. |
| Open investigations | `wiki/questions/` | `question` | Research gaps and planned follow-up work. |
| Decisions | `wiki/decisions/` | `decision` | Decision records and rationale. |
| Reusable artifacts | `wiki/outputs/` | `output` | Reports, briefs, tables, decks, and exports. |

Recommended default: start with generic directories, then add optional
domain-specific directories only when a migration inventory shows repeated page
types that do not fit cleanly under the existing taxonomy.

## Proposed Overlay Shape

The eventual `research.overlay.yml` should remain partial and merge into the
base `research.yml`:

```yaml
domain_pack:
  name: example-domain
  version: 0.1.0
  description: Domain guidance for a reusable research scope.
  compatible_research_yml_contract: "0.1"
  taxonomy_doc: taxonomy.md
  claims_doc: claims.md
  source_priorities_doc: source-priorities.md
  scaffolds:
    decision_record: scaffolds/decision-record.md
    output_brief: scaffolds/output-brief.md
    comparison_table: scaffolds/comparison-table.md
    presentation_outline: scaffolds/presentation-outline.md
  applicable_source_types:
    - official_guidance
    - standard_or_specification
    - research_report
    - dataset_card
    - implementation_repository
    - benchmark_page
    - expert_commentary
  recommended_synthesis_outputs:
    - evidence_map
    - comparison_matrix
    - decision_register
    - gap_analysis
    - implementation_readiness_brief
```

## Extraction Rules

When ingesting sources, extract fields that affect interpretation and reuse:

- scope, applicability, and version;
- publication date, last-updated date, effective date, and expiration date when
  available;
- publisher, author, authority, or maintainer;
- eligibility rules, inclusion criteria, and exclusions;
- metrics, thresholds, measurements, scores, and confidence statements;
- deadlines, review windows, release dates, and renewal periods;
- required inputs, dependencies, documents, or implementation steps;
- risks, limitations, uncertainty, and caveats;
- related systems, datasets, methods, and benchmark links;
- output candidates such as decision records, comparison tables, evidence maps,
  or briefing pages.

Do not infer missing values. Mark missing dates, unclear scope, and weak source
authority as evidence gaps.

## Source Priority Rules

Define a source ranking that fits the domain. A generic default order:

1. Primary official publications, maintained specifications, or source datasets.
2. Direct project documentation, release notes, and repository evidence.
3. Peer-reviewed papers, technical reports, and benchmark documentation.
4. Major institutional reports or standards-body guidance.
5. Practitioner writeups and expert commentary with clear citations.
6. Blogs, newsletters, and commercial summaries as secondary context only.

When sources disagree, prefer the more authoritative and more recent source
within the same scope. If two high-authority sources conflict, create a question
page or decision note instead of silently choosing one.

## Structured Claim Types

Use structured claims instead of broad numeric or keyword matching. Proposed
claim types:

| Claim Type | Use For |
|------------|---------|
| `metric_claim` | Scores, measurements, rates, counts, or other quantitative facts. |
| `deadline_claim` | Release dates, review windows, renewal periods, and effective dates. |
| `eligibility_claim` | Requirements, exclusions, applicant categories, and input constraints. |
| `requirement_claim` | Mandatory steps, dependencies, documents, or operating conditions. |
| `availability_claim` | Implementation availability, repository status, releases, or access limits. |
| `source_authority_claim` | Publisher, maintainer, authority, scope, and freshness of a source. |
| `limitation_claim` | Caveats, uncertainty, constraints, and known gaps. |
| `recommendation` | Source-grounded recommendations or decision criteria. |
| `factual` | Source-grounded facts that do not fit the more specific categories. |

Example:

```yaml
---
type: claim
created: YYYY-MM-DD
updated: YYYY-MM-DD
source_ids:
  - source:id
claim_type: metric_claim
subject: example-system
predicate: reports
object: Example benchmark score under the documented evaluation setup.
metric: example_score
value: 0.82
unit: score
scope: example benchmark, documented version required
summary: Example metric claim with explicit scope and source evidence.
---
```

Each claim should include scope, source IDs, and date or version information
when available. Missing scope, date, or version should be explicit gaps.

## Output Scaffolds

The pack should provide scaffolds for common reusable outputs:

- Decision record: context, options, source evidence, chosen path, risks,
  reversibility, and review date.
- Output brief: summary, key evidence, caveats, open questions, and next
  actions.
- Comparison table: dimensions, source IDs, findings, confidence, and gaps.
- Evidence map: source clusters, claims, disagreements, and synthesis status.
- Presentation outline: audience, thesis, key evidence, slide sequence,
  decisions, and appendix sources.

Generated artifacts should live under `wiki/outputs/` unless a future migration
explicitly adds dedicated output directories.

## Implementation Phases

1. Draft the domain pack files without touching production workspace content.
2. Inventory existing pages and map each page to the target workspace taxonomy.
3. Define source IDs and normalize raw sources before rewriting wiki pages.
4. Convert high-risk prose facts into structured claim pages or embedded claims.
5. Recreate domain-specific agent guidance as pack documentation consumed by
   generic skills.
6. Run lint on a copied workspace before any production migration.

## Non-Goals

- Do not migrate workspace content in this proposal.
- Do not add hardcoded domain behavior to reusable scripts.
- Do not make domain assumptions part of the core workspace.
- Do not treat weak secondary sources as authoritative for high-impact claims.

The target outcome is that domain-specific behavior can be rebuilt from
domain-neutral workspace mechanics plus an optional reusable domain pack.
