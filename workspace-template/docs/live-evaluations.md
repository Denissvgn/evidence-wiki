# Live Evaluations

Live dogfood runs are optional publication checks. They need explicit operator
approval because they may require network access, provider credentials, and
bounded downloads. Unit tests and offline fixtures remain the default CI path.

## Common Rules

- Get explicit operator approval before any live command.
- Keep budget knobs small: `max_downloads_per_run`,
  `max_academic_provider_requests_per_run`, `max_web_downloads_per_run`,
  `max_manual_url_deliveries_per_run`, and GitHub archive byte limits.
- Keep `OPENALEX_API_KEY` and `GITHUB_TOKEN` in the environment only.
- End by writing `runs/<run_id>/evaluation/` with
  `scripts/publication_readiness.py --format json bundle --run-id <run_id>`.
- safe cleanup deletes only scratch workspaces or operator-approved temporary
  downloads.

## Academic Provider-Backed Run

Use this when evaluating OpenAlex, arXiv, and optional GitHub evidence for a
paper-backed research question. Expected artifacts include discovered candidates,
provider deliveries under `raw/papers/`, normalized records, and
`runs/<run_id>/evaluation/`.

### Academic Acquisition Regression Scenario

The LLM-provider academic evaluation profile includes a mocked-provider fixture
and an optional live dogfood run. It must exercise a bounded non-confirmation
case where a method name has no resolvable provider record, preserve the
`unconfirmed` claim-probe limitation, reject any fabricated citation, and include
a product-spec web source for the hardware pillar so product evidence is not
forced into paper-only acquisition.

## Official Web And Product Run

Use this for official legal, regulatory, administrative, vendor, or product-spec
evidence. Manual delivery or an approved fetch agent should save HTML under
`raw/web/` with provenance sidecars and coverage manifests for high-stakes
facets.

### Official-Source Regression Scenario

The official-source evaluation profile must end missing current-figure evidence
as `blocked_on_sources` with facet-level source requests. It must reject or mark
as supplemental lower-trust legal/gestoria candidates and other advisory
candidates when official candidates exist, and it must never present stale 2023-2025 fee amounts as current facts.

## Mixed-Domain Run

Use this when a question needs academic papers, code/release evidence, product
specs, and official web evidence in one run. Discover candidates by evidence
path, select candidates, run `source_requests.py plan-fetch`, acquire only
selected sources within budget, normalize, verify, export, and bundle the
publication readiness inputs.

## Generic No-Pack Run

Use this when evaluating the starter's universal-domain claim without a domain
pack. The scenario must rely on base evidence policies only and end with one of
three explicit verdicts: ship-grade, clean `blocked_on_sources`, or a documented
domain-pack policy gap. Do not treat missing policy vocabulary as an exception;
record the gap and create a follow-up domain-pack policy task.

## Standards Registry Run

Use this when evaluating ISO/Open Data metadata, EU product requirements and
harmonised-standard references, NIST guidance/publication handoff, and GOV.UK
geospatial register entries. The pre-live regression net is
`tests/fixtures/standards-registry-workspace/`; it is offline, fixture-backed,
and must show `citations[].standards` in exports plus `no_ship` for withdrawn,
superseded, wrong-edition, stale, guidance-only, or missing-OJEU evidence.

## Production-Readiness Gate

A production-ready claim requires dated reports for:

- the academic provider-backed run,
- the official web/product run,
- the generic no-domain-pack run.
- the standards registry run.

Each report must show contracted-provider-only acquisition or record unapproved
manual/network steps as blockers. A run cannot satisfy the gate when it ships an
unsupported high-stakes claim, hides a manual fetch, fabricates a citation, or
omits publication-readiness bundle evidence.

Live dogfood commands may require network access and should never run without
explicit operator approval.
