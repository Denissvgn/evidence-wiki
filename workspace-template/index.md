# Research Wiki Index

Static catalog for maintained wiki pages.

Update this file when adding or changing wiki pages. Future scripts and agents should derive index sections from `research.yml` instead of hardcoding wiki directory names.

## Sources

Source notes that summarize and cite normalized source records.

| Page | Summary | Updated | Source IDs |
|------|---------|---------|------------|
| (none yet) | | | |

## Entities

Organizations, people, projects, tools, labs, or other named things.

| Page | Summary | Updated | Source IDs |
|------|---------|---------|------------|
| (none yet) | | | |

## Concepts

Reusable ideas, definitions, taxonomies, and conceptual distinctions.

| Page | Summary | Updated | Source IDs |
|------|---------|---------|------------|
| (none yet) | | | |

## Methods

Techniques, workflows, algorithms, or research methods.

| Page | Summary | Updated | Source IDs |
|------|---------|---------|------------|
| (none yet) | | | |

## Systems

Concrete systems, implementations, products, or architectures.

| Page | Summary | Updated | Source IDs |
|------|---------|---------|------------|
| (none yet) | | | |

## Benchmarks

Evaluation suites, tasks, leaderboards, and scoring protocols.

| Page | Summary | Updated | Source IDs |
|------|---------|---------|------------|
| (none yet) | | | |

## Datasets

Datasets, corpora, test sets, and generated data resources.

| Page | Summary | Updated | Source IDs |
|------|---------|---------|------------|
| (none yet) | | | |

## Claims

Structured evidence statements extracted from sources.

| Page | Summary | Updated | Source IDs |
|------|---------|---------|------------|
| (none yet) | | | |

## Synthesis

Cross-source maps, comparisons, literature reviews, and summaries.

| Page | Summary | Updated | Source IDs |
|------|---------|---------|------------|
| (none yet) | | | |

## Questions

Open questions, research gaps, and planned investigations. Question pages are task records with a lifecycle (`open`, `in_progress`, `answered`, `blocked`, `deferred`, `rejected`). Use `scripts/question_status.py` to scan the backlog and the `research-answer` skill to work it. Unattended runs use the `research-run` skill: claims via `scripts/question_claim.py`, budgets from the `research.yml` `run` block, and a `scripts/run_report.py` report per run; `research-verify` optionally records answer confidence before export. Fetch agents use `research-acquire` to fulfill source requests and reopen blocked questions only after normalized evidence exists. The optional `research-discover` skill runs the disabled-by-default discovery stage before acquisition: it proposes and ranks candidate sources (official sources first for legal questions), and a reviewer selects candidates explicitly before any fetch.

| Page | Summary | Updated | Source IDs |
|------|---------|---------|------------|
| (none yet) | | | |

## Decisions

Decision records about project direction or implementation choices.

| Page | Summary | Updated | Source IDs |
|------|---------|---------|------------|
| (none yet) | | | |

## Outputs

Reusable generated artifacts such as reports, decks, tables, and exports.

| Page | Summary | Updated | Source IDs |
|------|---------|---------|------------|
| (none yet) | | | |
