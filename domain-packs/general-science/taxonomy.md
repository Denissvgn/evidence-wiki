# General Science Taxonomy

Domain taxonomy for source-grounded scientific literature reviews, evidence
maps, methods comparisons, dataset inventories, and reproducibility analysis.

Use this taxonomy with the reusable workspace page types from `research.yml`.
It defines where an ingesting agent should file scientific evidence without
changing core scripts.

## Page Placement

| Directory | Page Type | Use For |
|-----------|-----------|---------|
| `wiki/sources/` | `source` | One source note per paper, preprint, report, dataset card, protocol, or normalized source record. |
| `wiki/methods/` | `method` | Experimental protocols, analysis workflows, measurement methods, instruments, simulations, models, and statistical methods. |
| `wiki/datasets/` | `dataset` | Published datasets, corpora, benchmark collections, observation series, measurement tables, and derived data products. |
| `wiki/claims/` | `claim` | Structured evidence statements, empirical results, methodological claims, dataset claims, limitations, and reproducibility claims. |
| `wiki/synthesis/` | `synthesis` | Literature maps, evidence tables, method comparisons, dataset inventories, replication maps, and research agendas. |
| `wiki/questions/` | `question` | Evidence gaps, unresolved hypotheses, missing datasets, replication needs, and source-acquisition plans. |
| `wiki/decisions/` | `decision` | Accepted or rejected decisions about scope, inclusion criteria, taxonomy, synthesis priorities, and evidence standards. |

Inherited generic directories:

- `wiki/entities/`: researchers, organizations, labs, instruments, field sites,
  projects, and named collaborations when they recur across sources.
- `wiki/concepts/`: definitions, mechanisms, variables, phenomena, constructs,
  metrics, and theoretical distinctions.
- `wiki/systems/`: concrete platforms, instruments, experimental systems,
  simulation systems, or software systems when a source studies an implemented
  system.
- `wiki/benchmarks/`: evaluation protocols, challenge sets, reference tasks,
  scoring procedures, and validation suites.
- `wiki/outputs/`: reusable exports such as evidence maps, literature-review
  drafts, CSV tables, reports, and presentation outlines.

## Extraction Targets

Extract these targets from source notes and normalized records when evidence is
available:

- research question or hypothesis: explicit question, stated objective, or
  investigated mechanism;
- study design: experimental, observational, simulation, survey, review,
  meta-analysis, benchmark, or methods paper;
- population, system, or scope: species, cohort, material, environment,
  domain, time period, geography, benchmark, or applicability boundary;
- methods: protocol, instrument, model, algorithm, intervention, measurement,
  statistical test, data-processing workflow, or analysis pipeline;
- datasets: source data, sample size, variables, inclusion criteria, collection
  process, access conditions, and license;
- results: reported effect, association, metric, estimate, uncertainty,
  benchmark score, qualitative finding, or negative result;
- limitations: stated caveats, threats to validity, missing controls, bias,
  confounding, measurement error, and external-validity boundaries;
- reproducibility evidence: code availability, data availability, protocol
  detail, preregistration, materials, environment, and replication status.

Only extract a target when the source supports it. Mark missing targets as gaps
instead of filling them from inference.

## Filing Rules

- File source summaries in `sources`; do not turn a source note into a method,
  dataset, claim, or synthesis page.
- File reusable protocols, instruments, models, and analysis workflows in
  `methods`.
- File reusable data resources in `datasets`, even when a paper is the first
  place the data is described.
- File definitions, variables, mechanisms, metrics, and theoretical constructs
  in `concepts` when they need standalone explanation.
- File empirical findings as `claims` linked to source IDs; do not create claims
  from interpretation alone.
- File benchmark definitions in `benchmarks`; file benchmark observations or
  scores as `claims` linked to the benchmark and source.
- File cross-source comparisons and evidence maps in `synthesis`; require
  multiple source notes unless a single-source exception is explicit.
- File unresolved evidence needs in `questions`, especially when source notes
  lack datasets, methods detail, or reproducibility artifacts.

## Recommended Synthesis Outputs

Use these as early synthesis targets when enough source notes exist:

- evidence map,
- methods comparison matrix,
- dataset inventory,
- hypothesis and mechanism map,
- limitations and validity matrix,
- reproducibility and artifact availability table,
- open research agenda.
