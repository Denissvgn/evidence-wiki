# LLM Research Taxonomy

Domain taxonomy for autonomous research systems, LLM research agents, evaluation methods, benchmarks, datasets, implementation evidence, and cross-source synthesis.

Use this taxonomy with the reusable workspace page types from `research.yml`. It defines where an ingesting agent should file evidence without changing core scripts.

## Page Placement

| Directory | Page Type | Use For |
|-----------|-----------|---------|
| `wiki/sources/` | `source` | One source note per paper, repository, technical report, benchmark document, dataset card, or other normalized source. |
| `wiki/systems/` | `system` | Concrete LLM research systems, agents, frameworks, repositories, prototypes, and named implementations. |
| `wiki/methods/` | `method` | Reusable techniques such as orchestration patterns, state/memory strategies, search procedures, training loops, evaluation workflows, and tool-use methods. |
| `wiki/benchmarks/` | `benchmark` | Evaluation suites, tasks, leaderboards, scoring protocols, challenge sets, and benchmark variants. |
| `wiki/datasets/` | `dataset` | Training, evaluation, benchmark, synthetic, human-annotated, and generated datasets. |
| `wiki/claims/` | `claim` | Structured evidence statements, benchmark results, ablations, architecture claims, limitations, cost/runtime claims, and availability claims. |
| `wiki/synthesis/` | `synthesis` | Cross-source maps, comparison tables, literature reviews, benchmark landscapes, implementation pattern summaries, and research agendas. |
| `wiki/questions/` | `question` | Open research questions, missing evidence, unresolved comparisons, replication gaps, and source-acquisition plans. |
| `wiki/decisions/` | `decision` | Accepted or rejected project decisions about taxonomy, ingestion scope, synthesis priorities, and implementation direction. |

Inherited generic directories:

- `wiki/entities/`: organizations, labs, authors, projects, products, tools, and repositories when they are important outside a single system page.
- `wiki/concepts/`: reusable definitions such as evaluation metrics, autonomy levels, agent roles, task classes, and implementation terminology.
- `wiki/outputs/`: reusable exports such as CSV tables, report drafts, deck outlines, and publication-ready summaries.

Do not create a separate `metrics` page type for this pack. File metrics as `concept` pages when they need standalone explanation, or as structured fields on benchmark, claim, and synthesis pages.

## Extraction Targets

Extract these targets from source notes and normalized records when evidence is available:

- architecture: major components, control flow, model roles, and interfaces;
- orchestration model: single-agent, multi-agent, hierarchical, recursive, marketplace, debate, planner-executor, or other coordination pattern;
- state and memory strategy: persistent state, scratchpads, file-based memory, context management, retrieval, traces, and handoff artifacts;
- tools: external tools, code execution, search, repository access, evaluation runners, data generators, and environment APIs;
- benchmarks: named evaluation suites, tasks, scoring protocols, and dataset dependencies;
- baselines: compared systems, human baselines, model baselines, ablations, and no-tool or no-memory variants;
- results: measured outcomes, task success, benchmark scores, error rates, cost, runtime, sample efficiency, or qualitative findings;
- limitations: failure modes, evaluation weaknesses, scope limits, safety concerns, reproducibility issues, and negative results;
- implementation availability: repository links, license, code release status, installation notes, missing artifacts, and reproducibility barriers.

Only extract a target when the source supports it. Mark missing targets as gaps instead of filling them from inference.

## Filing Rules

- File source summaries in `sources`; do not turn a source note into a system, method, or synthesis page.
- File named implementations in `systems`, even when a paper also introduces a method.
- File reusable procedures or patterns in `methods`, even when first observed in one system.
- File benchmark definitions in `benchmarks`; file benchmark observations or scores as `claims` linked to the benchmark and source.
- File datasets in `datasets` when they are reusable assets, not just examples mentioned in prose.
- File cross-source comparisons and maps in `synthesis`; require multiple   source notes unless a single-source exception is explicit.
- File unresolved evidence needs in `questions`, especially when source notes lack implementation, benchmark, or reproducibility details.
- File taxonomy and ingestion policy choices in `decisions` when they affect future project behavior.

## Recommended Synthesis Outputs

Use these as early synthesis targets when enough source notes exist:

- autonomous research systems map,
- orchestration patterns comparison,
- state and memory patterns comparison,
- benchmark landscape,
- implementation availability matrix,
- reproducibility and artifact gaps,
- open research agenda.
