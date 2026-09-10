# Optional market evidence

Select `kind: market_evidence` or `metadata.market_profile: market_evidence/v1`
on a manifest record to validate a delegated market slice. Ordinary source kinds
retain their existing contracts. The optional `capital-markets` pack provides
guidance and coverage templates; profile selection and host usage authority are
separate choices.

The host acquires a bounded response, resolves its requested scope, and sanitizes
it before deposit. This library reads delivered bytes without calling a provider,
following retrieval instructions, or running an external tool. Larger histories
stay in host storage; each delivery records a durable slice identifier.

```python
from evidence_wiki import Workspace

with Workspace.open("/path/to/workspace") as workspace:
    report = workspace.normalize.validate_market("market:observations")
    print(report["valid"], report.get("completeness"))
```

The matching command is
`evidence-wiki normalize market --target /path/to/workspace --source-id market:observations --format json`.
Invalid deliveries return `valid: false` and a stable reason; the CLI exits 1.
A structurally valid slice can have incomplete scope. Both forms retain a
separate authority result. An unsupported declared profile never falls back to
ordinary text normalization.

## Captured delivery

Deliver `market-record.json` and all declared artifacts beneath the source's
evidence root. Without a configured usage store, local staging uses
`sources/evidence/<safe-source-id>/`. Protected workspaces read the exact
host-deposited source revision and require the shared sanitization and usage
contracts before consumption.

The record is a JSON object with exactly these members:

| Member | Contract |
| --- | --- |
| `schema_version`, `profile` | `market-evidence/v1`, `market_evidence/v1` |
| `source_id` | Exact manifest source identity |
| `route` | `sec-company-concept` or `alpaca-stock-bars` |
| `request` | Route-specific requested slice, described below |
| `artifacts` | Complete list of `{path, content_hash, size_bytes, role}`; hashes use `sha256:` |
| `pages` | Ordered `{artifact: {path, content_hash}, request_token}` entries; the first token is null |
| `listings` | Dated issuer and listing identities |
| `provenance` | Provider, retrieval, scope, policy evidence, and adjustment lineage |

Every captured artifact other than the record must be declared and referenced.
Paths are bounded relative files. Duplicate JSON keys, excessive nesting,
incorrect hashes, unreferenced artifacts, non-finite numbers, and exceeded bounds
are refused. Limits include 16 pages, 4,096 examined observations, and 64 listing
intervals; the shared artifact closure also imposes file and total byte limits.

Each listing has `issuer_id`, `listing_id`, `provider_id`, `venue`, `symbol`,
`issuer_name`, `valid_from`, nullable `valid_to`, three-letter `currency`,
`status` (`active`, `inactive`, `delisted`, or `unknown`), and `status_at`.
Times use timezone-qualified timestamps. Reused tickers remain separate
identities. Conflicting or overlapping matches remain explicit gaps.

`provenance` contains `provider`, `retrieved_at`, `policy_reference`,
`durable_slice_id`, inert `retrieval_instructions`, `conflicts`, `excluded`,
`universe`, `corporate_action_coverage`, and `corporate_actions`.
`policy_reference` is null or `{policy_id, policy_revision, artifact}`.
`universe` contains `as_of`, `coverage`, `listing_ids`, and nullable `artifact`;
coverage is `complete`, `partial`, `survivors-only`, `unknown`, or `not-applicable`.
A complete universe requires a captured reference. Corporate-action coverage
is `complete`, `partial`, `unknown`, or `not-applicable`. Each action binds `listing_id`,
`type` (`split`, `dividend`, or `spin-off`), `effective_at`, `ratio`, `amount`,
`currency`, and `artifact`; inapplicable numeric fields are null. These references
preserve attributable host evidence and do not authenticate the provider.

## Filing slice

The SEC company-concept route accepts one captured response. `request` contains
`issuer_id`, a ten-digit `cik`, `taxonomy`, `tag`, `units`, `periods` (objects
with nullable `start` and required `end` dates), and `accessions`. The response's
CIK, taxonomy and concept must match. Requested units, periods, and filing
accessions remain separate observations. Missing combinations and conflicting
facts are gaps; numeric values retain exact decimal strings with scale zero.

SEC separates company-concept facts by reporting unit. Calendar frames are not
a substitute for a company's fiscal period. The adapter therefore preserves
fiscal year, fiscal period, form, accession, filing date, and optional frame
instead of collapsing them to one latest value. [SEC API documentation](https://www.sec.gov/search-filings/edgar-application-programming-interfaces).

A filing date has day precision. Normalization records `available_at: null` for
each fact; it does not invent an intraday availability timestamp. Historical
reconstruction requires the shared independently attested availability contract.

## Price slice

`request` contains `symbols`, `start`, `end`, `timeframe`, `feed`, `delay`,
`currency`, `adjustment`, `asof`, `session`, `calendar`, `timezone`, and
`expected_bars`. Each expected bar specifies `listing_id`, `symbol`, `start`, and
`end`. The host supplies explicit expected intervals and dated universe evidence;
the library does not infer a complete exchange calendar from a timeframe name.

Each captured Alpaca response uses its `bars` mapping. Delivered OHLC values,
volume, trade count, and optional volume-weighted price retain decimal precision.
Bars must match the requested intervals and identities. Unfinished bars, missing
members, incompatible currencies, ambiguous mappings, unknown delay, and missing
corporate-action coverage remain gaps. Report fields retain the requested feed,
session, calendar, timezone, and raw or adjusted basis.

Alpaca distinguishes symbol-mapping `asof` from price adjustment and feed. This
profile accepts a mapping date or `-`; it does not interpret that value as data
availability. Pages follow the response's `next_page_token` until explicit
completion, even when a response contains fewer observations than requested.
[Alpaca historical-bars API](https://docs.alpaca.markets/us/reference/stockbars).

## Consumption and replay

Normalization writes the validation report into `market_evidence` frontmatter
and its `data` mapping into the hash-bound structured sidecar. Ground exact
values with pointers such as `/observations/0/close` or `/observations/0/value`.
Verification re-parses the originals and compares the report and scalar bytes.
Current protected search and snapshot qualification also recheck that binding.

Completeness is evidence about the declared slice, not independent proof that
the host chose an adequate universe or supplied authentic provider data. A
checksum proves byte integrity; issuer identity, entitlement, and evaluation
authority require their own evidence. Coverage refuses unresolved slice gaps.

Market records require current host usage authority for consumption. A policy
reference alone grants no retrieval, training, or export permission. Delayed
data may be authorized for research while training and redistribution remain
denied. Shared transitive revocation checks still apply to derived evidence.

Temporal evaluation uses the common source revision, measurement interval,
publication/availability claims, cutoff, and authority checkpoint. It requires
the complete market slice to fit those claims, including finished bars, dated
universe and adjustment information, and filing dates. Frozen snapshot
qualification replays the original bytes offline; current use separately checks
live permission. These shared mechanisms also support nonfinancial research
without this optional profile.
