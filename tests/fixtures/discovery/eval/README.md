# Discovery-quality evaluation fixtures (E36-T04)

Declarative, network-free scenarios that guard discovery ranking quality. Each
`*.json` file is one case: raw provider results plus the expected candidate
outcomes. `tests/test_discovery_quality_eval.py` runs every scenario through the
real discovery pipeline (legal/search via the fixture search backend, GitHub via
an injected transport) and scores candidate ranking, trust-tier/officialness
assignment, and rejection rationale.

## Scenario schema

| Field | Meaning |
|-------|---------|
| `id` | Stable scenario id (also the filename stem). |
| `kind` | `legal`, `search`, `github`, or `authors` — selects the pipeline and the candidate key (`host` for legal/search, `full_name` for github, OpenAlex `work_id` for authors). |
| `description`, `expected_behavior` | Human documentation of the case and what it guards. |
| `jurisdiction`, `topic`, `query`, `intent` | Command inputs for the pipeline. |
| `results` / `items` / `works` | Raw search results (`legal`/`search`), GitHub repo items (`github`), or OpenAlex work objects (`authors`). For `authors`, `paper` carries the seed source id, title, DOI, author ORCID, and author names. |
| `expected.candidates` | Per-candidate expectations keyed by host/full_name/work_id: `trust_tier`, `official_source`, `recommended_action`, `risk_flags_include`, `rationale_includes`. |
| `expected.outranks` | `[better, worse]` pairs; `better` must rank before `worse`. |
| `expected.rejected` | Candidates that must have `recommended_action: reject`. |

## Scenarios

- **legal-official-vs-secondary** — official federal sources outrank reputable
  secondary legal databases (kept supplemental), a superseded official page is
  retained as official provenance but rejected as current evidence, and a scraped
  mirror is rejected.
- **general-search-useful-and-rejected** — an official `.gov` source outranks
  generic pages, a useful page is retained for review, and a suspicious
  executable download and an official-source mirror are rejected with rationale.
- **paper-companion-repository** — a paper with known authors and a companion
  repository; the canonical repo outranks a fork/mirror and an archived,
  unlicensed copy.
- **author-publications** — an ORCID-author's topically related publication is
  proposed for review while an unrelated work is rejected as out of scope;
  identity ambiguity is never asserted and the seed paper is never re-proposed.

Add a scenario by dropping a new `*.json` here and extending the expected id set
in the test. Keep fixtures small and deterministic so the suite stays CI-fast.
