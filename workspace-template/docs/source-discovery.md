# Source Discovery Contract

This document defines the source-discovery contract: how a research workspace
proposes *candidate* sources before anything is fetched, normalized, or treated
as evidence. Discovery is a reasoning stage that emits ranked, explained
candidate records; it does not download, scrape, clone, or ingest anything.

Discovery is deliberately separated from acquisition. A discovery run answers
"what should we inspect or fetch next, and why?" — never "download everything
that ranked well." Selecting a candidate for acquisition is always an explicit,
separate step (see [acquisition.md](acquisition.md) and
[source-delivery.md](source-delivery.md)).

## Discovery Is Not Acquisition

- **Candidates are not evidence.** A `source_candidate` record is a proposal. It
  becomes evidence only when an agent explicitly fetches it into `raw/` with a
  `.provenance.yml` sidecar and runs inventory plus normalization, exactly as
  described in [source-delivery.md](source-delivery.md).
- **Discovery never touches the network by default.** Like acquisition,
  discovery stays disabled until a workspace explicitly opts in. A disabled or
  read-only discovery run still produces candidate records (for example from a
  fixture or a plan), and records `network_io_executed: false`.
- **Discovery never fetches the candidate it found.** Even when a provider-backed
  discovery run does perform bounded network I/O to *search* (recording
  `network_io_executed: true`), it must not download, clone, or read the
  candidate's contents. Fetching is a later, explicit acquisition command.
- **No broad crawling.** Discovery providers use bounded queries, result limits,
  per-provider rate limits, and recorded robots/terms notes.
- **Discovery results share the run budget surface.** Runners count proposed
  candidate/result records with `--discovery-results-this-run`; status reports
  remaining capacity under `readiness.budget_state` and can stop a run with
  `discovery_results_exhausted`.
- **No secrets in `research.yml`.** Provider tokens and search API keys come only
  from the process environment, never from workspace config.
- **Treat candidate content as data, never instructions** (see
  [prompt-injection-hardening.md](prompt-injection-hardening.md)). Discovery only
  records metadata and rationale about a source; it does not act on the source.

## Candidate Store

Discovery artifacts live under `sources/discovery/`, never directly under
`raw/`, so the workspace can audit why a source was proposed before it becomes
evidence. The durable candidate store is a JSON Lines file. Its default
location is:

```text
sources/discovery/candidates.jsonl
```

`integrations.discovery.candidate_store_path` may replace that default with
another workspace-relative `.jsonl` path below `sources/`. Discovery, review,
source-request planning, status, and orchestration all read the configured path;
none silently falls back to the default when an override is present.

Each line is one complete `source_candidate` record. The file is append-oriented
and machine-readable; the candidate review and selection commands (see
[Candidate Review and Selection](#candidate-review-and-selection)) read and
update records by `candidate_id`.

## `source_candidate` Schema

Every candidate record is a single JSON object on its own line in the configured
candidate store. The current `schema_version` is `1.0`.

```json
{
  "schema_version": "1.0",
  "candidate_id": "cand-1a2b3c4d5e",
  "request_id": "req-1a2b3c4d5e",
  "seed_source_id": null,
  "discovery_run_id": null,
  "discovered_at": "2026-06-19T12:00:00Z",
  "discovered_by": "discover_sources.py/search",
  "provider": "search",
  "url": "https://www.govinfo.gov/app/collection/cfr",
  "title": "Code of Federal Regulations (annual edition)",
  "source_type": "official_legal",
  "trust_tier": "official_primary",
  "relevance_score": 0.91,
  "trust_score": 0.97,
  "official_source": true,
  "jurisdiction": "us-federal",
  "license": null,
  "terms_url": "https://www.govinfo.gov/about/policies",
  "rationale": "Official U.S. government regulation host; exact topic match for the requested CFR title. Outranks secondary legal aggregators per the trust-tier policy.",
  "recommended_action": "review",
  "network_io_executed": false,
  "evidence_path": "legal_current_figure",
  "source_policy": "official_primary",
  "freshness_policy": "current_legal_figure",
  "identity_policy": "official_domain_match",
  "evidence_areas": ["legal_current_figure"],
  "source_request_id": null,
  "selection_status": "pending",
  "fetch_status": "not_planned",
  "selected_for_request_id": null,
  "selected_at": null,
  "reasoning": {
    "matched_query_terms": ["code of federal regulations", "cfr", "annual edition"],
    "authority_reason": "govinfo.gov is the official U.S. Government Publishing Office host for the CFR; it is an official_primary regulator/publisher source for us-federal.",
    "freshness_reason": "Links to the current annual CFR edition; no superseded-edition risk flagged.",
    "scope_reason": "Collection landing page for the requested CFR title, directly in scope for the regulation lookup.",
    "risk_flags": []
  }
}
```

### Required Fields

| Field | Type | Meaning |
|-------|------|---------|
| `schema_version` | string | Candidate-record schema version. Currently `"1.0"`. Orchestrators pin to the major version and treat unknown fields as forward-compatible additions. |
| `candidate_id` | string | Stable unique identifier for this candidate (for example `cand-1a2b3c4d5e`). Later review and selection commands address candidates by this id. |
| `request_id` | string or null | Source-request origin when discovery answers a record in `sources/source-requests.jsonl`; otherwise `null`. Exactly one origin field (`request_id`, `seed_source_id`, or `discovery_run_id`) must be non-null. |
| `seed_source_id` | string or null | Manifest source origin when discovery expands from an existing source (for example `paper:2601.00001v1`); otherwise `null`. Exactly one origin field must be non-null. |
| `discovery_run_id` | string or null | Deterministic origin id for exploratory discovery that is not tied to a source request or seed source (for example `disc-1a2b3c4d5e`); otherwise `null`. Exactly one origin field must be non-null. |
| `discovered_at` | string | ISO 8601 UTC timestamp when the candidate was proposed (`YYYY-MM-DDTHH:MM:SSZ`). |
| `discovered_by` | string | Discovery agent or tool identifier, for example `discover_sources.py/search`. Marks which provider path produced the record. |
| `provider` | string | Candidate provenance/route attribution. Direct routes use a concrete provider such as `arxiv`, `openalex`, `github`, `search`, or scoped `standards:*`; compatibility records produced by composite routes may say `legal`, `authors`, or `companions`. Only the concrete IDs in `integrations.discovery.providers` authorize network access. |
| `url` | string | The candidate source URL (HTTP/HTTPS). This is a pointer for review, not a fetched artifact. |
| `title` | string | Human-readable title or name of the candidate source. |
| `source_type` | string | Kind of source, for example `paper`, `code_repository`, `dataset`, `project_page`, `supplemental_material`, `publisher_page`, `official_legal`, `standards_registry_entry`, `harmonised_standard_reference`, `product_requirement_guidance`, `geospatial_standard_register_entry`, or `web_page`. |
| `trust_tier` | string | Trust classification from the trust-tier policy. Official-source evaluation logs use `official_primary`, `official_secondary`, `academic_primary`, `vendor_primary`, `implementation_primary`, `aggregator`, `unknown`, or `rejected`; legacy discovery records with `primary_non_official`, `secondary_reputable`, `secondary_unknown`, or `unsafe_or_unusable` remain readable. The full policy and ranking rules are defined in the trust-tier section. |
| `relevance_score` | number | Topical-relevance score in the range `0.0`–`1.0`. Higher means a closer match to the request or seed. |
| `trust_score` | number | Trust score in the range `0.0`–`1.0` derived from authority, officialness, and risk signals — not from provider rank alone. |
| `official_source` | boolean or null | Whether the source is an official or primary authority for its `source_type`/`jurisdiction`. `true`/`false` when known; `null` when officialness is unknown and must be reviewed before download. |
| `jurisdiction` | string or null | Jurisdiction id when applicable (legal/regulatory candidates), for example `us-federal` or `us-ca`. `null` or omitted for non-jurisdictional sources. |
| `license` | string or null | License identifier or short statement when known; `null` is an explicit known-unknown, mirroring the acquisition sidecar convention. License uncertainty is surfaced, never guessed. |
| `terms_url` | string or null | URL to the source or provider terms when known, otherwise `null`. |
| `rationale` | string | Human-readable explanation of why this candidate was proposed, how it matched, and why its trust tier applies. |
| `recommended_action` | string | One of `fetch`, `review`, or `reject`. The discovery recommendation only; selecting a candidate for acquisition remains an explicit, separate step. |
| `network_io_executed` | boolean | Whether producing this candidate required network I/O. Disabled and read-only/plan-only runs record `false`; bounded provider-backed search records `true`. Either way, the candidate itself is never fetched during discovery. |
| `evidence_path` | string | Evidence-policy path used after selection/acquisition, reusing the [Evidence Policy Vocabulary](evidence-policies.md), for example `academic_method_existence`, `github_implementation`, `legal_current_figure`, `standards_registry_reference`, `product_requirement_profile`, or `vendor_product_spec`. |
| `source_policy` | string | Required source authority policy for the candidate if it is later accepted into a coverage facet. |
| `freshness_policy` | string | Currentness, publication identity, release snapshot, or manual-review freshness rule for the candidate. |
| `identity_policy` | string | Identifier, origin URL, repository ref, official domain, or no-op identity rule for the candidate. |
| `evidence_areas` | list | Stable evidence-area tags used for official-source evaluation summaries; readers default legacy records from `evidence_path`. |
| `source_request_id` | string or null | Durable source request this candidate is linked to, if any. This is the OEH alias for `selected_for_request_id`/`selected_request_id` and is used by status summaries. |
| `lifecycle_schema_version` | string | Candidate lifecycle contract version; current canonical records use `2.0`. |
| `lifecycle_state` | string | Authoritative state: `proposed`, `reviewed`, `selected`, `rejected`, `deferred`, `fetched`, `failed`, or `superseded`. |
| `status` | string | Coarse compatibility view (`new`, `selected`, `rejected`, or `fetched`) for older consumers. It is not the lifecycle authority. |
| `lifecycle_migration` | object | Present on the read view of legacy records; records the legacy status, mapped state, and `review_state_inferred: false`. |
| `selection_status` | string | One of `selected`, `rejected`, `duplicate`, `obsolete`, `needs_manual_review`, or `pending`. Rejected aggregators, obsolete pages, and duplicates remain in the log with a `rejection_reason`. |
| `fetch_status` | string | One of `not_planned`, `pending_manual_delivery`, `planned`, `fetched`, `failed`, or `not_fetchable`. Official legal/manual web candidates normally become `pending_manual_delivery` after selection. |
| `selected_for_request_id` | string or null | Canonical source-request id this candidate was selected for. `null` until `candidates select` links it to a request. Legacy `selected_request_id` is still accepted as a read alias. |
| `selected_at` | string or null | ISO 8601 UTC timestamp when this candidate was selected, or `null` while unselected. |
| `reasoning` | object | Structured decomposition of `rationale` into the auditable reasoning fields (`matched_query_terms`, `authority_reason`, `freshness_reason`, `scope_reason`, `risk_flags`) defined in the trust-tier policy below. Required for every candidate that carries a `trust_tier`. |

Optional `quality_gates` objects make expansion-specific checks explicit without
changing `schema_version`. Author-publication candidates may carry
`quality_gates.author_identity`; companion repository candidates may carry
`quality_gates.companion_repository` (`companion_repository`). Each gate records `status`,
`review_required`, and the machine-readable identity or origin signal that kept
the candidate at `recommended_action: review` rather than silently promoting it.

`relevance_score`, `trust_score`, `official_source`, `trust_tier`, and
`recommended_action` are the reasoning surface: a planner reading one record
must be able to tell where the candidate came from, why it was proposed, how
trustworthy it is, and whether it is safe to fetch — without rerunning
discovery.

The origin fields are part of that audit surface. Request-backed discovery sets
`request_id`; seed expansion sets `seed_source_id`; exploratory discovery sets a
deterministic `discovery_run_id`. Exactly one of those three fields is non-null
in every durable candidate.

Candidate policy fields are additive. Older candidate records that lack them are loaded
with defaults based on `source_type`: `paper` uses academic publication identity;
`code_repository` uses GitHub implementation/release identity; `official_legal`
uses current legal authority; `standards_registry_entry` and
`geospatial_standard_register_entry` use official registry/current-standard
defaults; `harmonised_standard_reference` uses registry/product-requirement
linkage defaults; `product_requirement_guidance` uses official guidance with
product-requirement currentness; `dataset`, `publisher_page`, and
`supplemental_material` use academic manual-review policies; `project_page` and
`web_page` use vendor/manual-review defaults.

Standards candidates may carry an additive `standards` object with registry
provider, standards body, designation, title, edition, status, registry URL,
replacement links, product or legal linkage, and terms/attribution metadata.
That object is registry metadata only. It is not proof that full standards text
was fetched, reviewed, or licensed for storage.

Academic paper candidates may also carry an additive `paper` object. This is
provider-neutral metadata used by acquisition planning, not evidence. It keeps
provider identity and fetchability explicit:

```json
{
  "paper": {
    "provider_ids": {"arxiv": "2601.00001v2", "openalex": "W260100001", "doi": "10.5555/example"},
    "title": "Example paper",
    "authors": ["Ada Lovelace"],
    "publication_year": 2026,
    "doi": "10.5555/example",
    "arxiv_id": "2601.00001v2",
    "open_access": true,
    "oa_status": "gold",
    "license": "cc-by",
    "landing_page_url": "https://example.org/paper",
    "pdf_url": "https://example.org/paper.pdf",
    "resolution_status": "resolved"
  },
  "provider_budget": {
    "provider": "openalex",
    "network_io_executed": true,
    "token_used": false,
    "max_results": 10
  }
}
```

`resolution_status` is one of `resolved`, `metadata_only`, or `uncertain`.
`paper.license: null` is an explicit known-unknown, matching provenance
sidecars. Discovery records `provider_budget` when a bounded provider run
produced the candidate so a planner can distinguish read-only metadata already
obtained from later acquisition network I/O.

### `recommended_action` Values

| Value | Meaning |
|-------|---------|
| `fetch` | High-confidence, in-policy candidate. Still requires an explicit acquisition step; discovery never fetches. |
| `review` | Needs human or agent review before download — typically unknown officialness, license uncertainty, or an ambiguous identity match. |
| `reject` | Should not be fetched — a mirror, scraped copy, lower-trust duplicate of an official source, or a terms-prohibited/unsafe source. Recorded with rationale rather than silently dropped. |

## Trust-Tier Policy

Discovery must make trust explicit. Every candidate is classified into exactly
one trust tier, and that tier — not the discovery provider's own ranking — is
the primary signal for whether a source is safe to fetch. The provider's rank is
an input feature, never the final authority.

### Trust Tiers

Tiers are ordered from most to least trustworthy. The `trust_tier` field carries
one of these five values, and `trust_score` should track the tier ordering
(higher score for higher tiers), adjusted by the reasoning signals below.

| Tier | Rank | Definition | Examples | Default action |
|------|------|------------|----------|----------------|
| `official_primary` | 1 (best) | Government, court, legislature, regulator, standards body, original publisher, or canonical project owner — the authoritative origin for the source. | `govinfo.gov` CFR, a court's official opinion portal, an ISO/IETF standard page, a publisher's DOI landing page, the canonical GitHub owner for a project. | `fetch` (or `review` when officialness is unconfirmed) |
| `primary_non_official` | 2 | Author website, repository, dataset host, publisher page, or project page — a primary source that is not the official authority for the topic. | An author's personal site or GitHub repo, a Zenodo/Figshare dataset, a lab project page. | `fetch` or `review` |
| `secondary_reputable` | 3 | Indexed databases, established aggregators, institutional pages, or reputable news/reporting sources. | Semantic Scholar, OpenAlex, a university library guide, a reputable legal database, established journalism. | `review` |
| `secondary_unknown` | 4 | Blogs, mirrors, scraped copies, forums, or low-context reposts of uncertain provenance. | A personal blog summarizing a law, a mirror of a paper, a forum thread, an SEO content farm. | `review` (often `reject` when it duplicates a higher tier) |
| `unsafe_or_unusable` | 5 (worst) | Suspicious, credential-gated, pirated, malicious, or terms-prohibited sources. | A pirated-paper host, a paywalled scrape, a malware-flagged domain, a terms-prohibited copy. | `reject` |

A candidate whose officialness cannot be determined is **not** silently promoted
to `official_primary`. It is classified by what is known, given `official_source:
null`, and recorded with `recommended_action: review`.

### Ranking Rules

When multiple candidates answer the same request or seed, they are ordered by the
following rules. Earlier rules dominate later ones.

1. **Official-first for legal/regulatory queries.** For legal or regulatory
   requests, `official_primary` sources (official gazette, legislature,
   regulator, court, or original publisher for the jurisdiction) rank ahead of
   **all** non-official results, regardless of how highly a search provider
   ranked a generic page. A high-ranked generic article never outranks the
   official statute, regulation, or opinion it merely describes.
2. **Exact identifier matches beat fuzzy text matches.** An exact DOI, arXiv id,
   or GitHub `owner/repo` match outranks a fuzzy title/text match. Identifier
   equality is strong evidence the candidate is the intended source; lexical
   similarity is not.
3. **Unknown officialness requires review before download.** A candidate with
   `official_source: null` must carry `recommended_action: review` and cannot be
   marked `fetch`. Officialness is confirmed by a human or a later check, not
   assumed from rank.
4. **Terms/license uncertainty is suggest-not-ingest.** A candidate with unknown
   or uncertain terms/license may be *suggested* (with a `risk_flag`) but must
   not be silently ingested into reusable outputs. Such candidates are capped at
   `recommended_action: review` until the terms/license question is resolved.

Within a tier, candidates are ordered by `relevance_score`, then by the freshness
and scope reasoning below, with duplicate URLs and lower-trust duplicates of an
official source collapsed (the lower-trust copy is recorded with
`recommended_action: reject` rather than dropped).

### Reasoning Fields

Trust and ranking decisions must be auditable, so `rationale` is backed by a
structured `reasoning` object on every candidate. All five fields are required:

| Field | Type | Meaning |
|-------|------|---------|
| `matched_query_terms` | array of strings | The request/seed terms (or identifiers) this candidate actually matched. Distinguishes an exact identifier match from a loose lexical one. |
| `authority_reason` | string | Why the candidate sits in its `trust_tier`: what makes the source official, primary, secondary, or unsafe. States *why officialness is unknown* when `official_source` is `null`. |
| `freshness_reason` | string | Why the candidate is current enough (or a note that it may be stale/superseded, which should also raise a `risk_flag`). |
| `scope_reason` | string | Why the candidate is in scope for the request or seed — the right document, jurisdiction, and granularity rather than an adjacent topic. |
| `risk_flags` | array of strings | Zero or more machine-readable risk markers, for example `unknown_officialness`, `license_uncertain`, `terms_uncertain`, `possible_mirror`, `superseded`, `credential_gated`, `terms_prohibited`, `suspicious_download`, `stale_source`, or `duplicate_of_official`. Discovery records may also carry source-delivery failure codes from the shared vocabulary when a prior capture attempt or provider metadata already exposed the delivery problem: `tls_verification_failed`, `http_error`, `javascript_required`, `official_error_page`, `not_found`, `content_too_sparse`, `license_or_terms_unknown`, `robots_or_terms_blocked`, and `manual_review_required`. An empty array means no flags. |

### Worked Example: Official Legal Source Beats A Generic Result

Consider a legal request for the text of a federal regulation. A general search
backend ranks a popular explainer blog first (high provider rank, high lexical
overlap) and the official government regulation host lower. The trust-tier policy
inverts that ordering:

- The blog is classified `secondary_unknown` (`official_source: false`), with
  `risk_flags: ["possible_mirror"]` and `authority_reason` noting it merely
  paraphrases the regulation. It is capped at `recommended_action: review` and,
  because it duplicates the official text, recorded as `reject` when the official
  source is available.
- The government host is classified `official_primary` (`official_source: true`)
  with an `authority_reason` tying the domain to the regulator, an exact
  `matched_query_terms` hit on the regulation identifier, and empty `risk_flags`.

By Ranking Rule 1 the `official_primary` regulation host outranks the higher
provider-ranked blog, and by Rule 2 its exact identifier match outranks the
blog's fuzzy text match — so the planner is handed the authoritative source
first, with a recorded reason it beat the generic result. Canonical machine-
readable examples for each tier, including this legal-versus-generic pair, are
exercised by the discovery contract tests.

## Discovery Command Surface

Discovery is driven by `scripts/discover_sources.py`. The command surface exists
so skills and orchestrators can issue bounded provider work consistently.
Discovery routes may append candidate metadata, and candidate lifecycle commands
may review or select those records, but no discovery command downloads, scrapes,
clones, or ingests candidate contents as evidence.

Eight bounded subcommands cover discovery providers, strategies, and
read-only jurisdiction inspection:

```bash
python3 scripts/discover_sources.py academic   --request-id ID [--run-id RUN_ID] --provider arxiv [--provider openalex] [--query TEXT] --max-results N --format json
python3 scripts/discover_sources.py search     --query TEXT [--intent paper|code|dataset|legal|web] [--request-id ID] [--domain-allow DOMAIN] [--domain-block DOMAIN] [--jurisdiction TEXT] [--execute] --max-results N
python3 scripts/discover_sources.py legal      --jurisdiction TEXT --topic TEXT --max-results N
python3 scripts/discover_sources.py github     --query TEXT --max-results N
python3 scripts/discover_sources.py authors    --source-id SOURCE_ID [--discover-publications] [--run-id RUN_ID] --max-results N
python3 scripts/discover_sources.py companions --source-id SOURCE_ID [--request-id ID] [--no-github] [--no-search] --max-results N
python3 scripts/discover_sources.py standards  iso-open-data | eu-product-requirements | uk-geospatial-register | nist [provider flags] --max-results N
python3 scripts/discover_sources.py jurisdictions  validate | list | show --jurisdiction ID
```

The `academic`, `search`, `github`, and `standards` routes use concrete provider
permissions. `legal`, `authors`, and `companions` are strategies: legal execution
requires `search`, author publication expansion requires `openalex`, and companion
network phases run only when `github` and/or `search` is enabled. The legacy
strategy IDs remain readable for one compatibility release but never authorize
network access. `jurisdictions` (see
[Jurisdiction Profiles](#jurisdiction-profiles))
and `candidates` (see [Candidate Review and Selection](#candidate-review-and-selection))
are offline subcommands: they validate profiles or review existing candidates and
never contact a provider, so they run even when discovery is disabled.

All commands accept the shared `--project-root` and `--format text|json` flags
and use the stable error envelope shared by the other workspace scripts
([orchestrator-handoff.md](orchestrator-handoff.md)). `--max-results` defaults to
a bounded value and must be a positive integer.

### Disabled By Default

Like acquisition, discovery is **disabled by default** and opts in through
`research.yml`:

```yaml
integrations:
  discovery:
    enabled: true
    providers:
      - arxiv
      - openalex
    candidate_store_path: sources/discovery/candidates.jsonl
```

A missing `integrations.discovery` block, or `enabled` left `false`, is the safe
default. In that state provider-backed discovery routes refuse with the
`DISCOVERY_DISABLED` error code and exit `2` without touching the network.
Offline `candidates` and `jurisdictions` commands remain available.

Every provider-backed route also requires an explicit entry in
`integrations.discovery.providers`; `enabled: true` with an empty list is invalid.
The candidate store is a workspace-relative JSONL path under `sources/` and all
discovery and candidate-lifecycle commands honor the configured value. Standards discovery accepts either `standards`
for all fixture-backed standards routes or a route-specific value such as
`standards:iso-open-data`. If the route is not listed, the command refuses with
`DISCOVERY_PROVIDER_DISABLED` before reading provider fixtures or touching the
network.

### Provider Transport Status

Every provider subcommand is implemented. The `academic` subcommand searches a
source request through explicitly enabled arXiv and/or OpenAlex providers,
deduplicates their metadata into provider-neutral paper candidates, and never
downloads evidence. The `github` subcommand performs
bounded GitHub repository search (see [GitHub Repository
Discovery](#github-repository-discovery) below) and records `network_io_executed:
true`. The `search` subcommand normalizes a configured backend's results into
candidates through a provider-neutral interface (see [General Search
Discovery](#general-search-discovery) below). The `legal` subcommand plans and
ranks official-source-first legal candidates (see [Legal Source
Discovery](#legal-source-discovery) below). The `authors` subcommand extracts an
author seed list and, with `--discover-publications`, proposes related works (see
[Author Extraction](#author-extraction) below). The `companions` subcommand
composes inline links, GitHub, and search into companion-artifact candidates (see
[Companion Artifact Discovery](#companion-artifact-discovery) below). The
`standards` subcommand proposes fixture-backed standards registry, product
requirement, and harmonised-standard candidates for ISO Open Data, EU product
requirements, UK geospatial register entries, and NIST publication references.
When discovery is disabled, provider-backed routes refuse with
`DISCOVERY_DISABLED` before any network access. In all cases the candidate a
command would propose is never fetched during discovery.

Fatal error codes for this command surface are `DEPENDENCY_MISSING`,
`CONFIG_MISSING`, `CONFIG_INVALID`, `VALUE_INVALID`, `DISCOVERY_DISABLED`,
`DISCOVERY_PROVIDER_DISABLED`, `NOT_IMPLEMENTED`, `DISCOVERY_NETWORK_ERROR`,
`DISCOVERY_RESPONSE_INVALID`, `SEARCH_PROVIDER_DISABLED`,
`SEARCH_PROVIDER_FAILED`, `GITHUB_AUTH_REQUIRED`, `GITHUB_RATE_LIMITED`,
`JURISDICTION_INVALID`, `JURISDICTION_UNKNOWN`, `REQUEST_UNKNOWN`,
`REQUEST_NOT_OPEN`, `SOURCE_UNKNOWN`, `DISCOVERY_RUN_STATE_INVALID`,
`DISCOVERY_RUN_RECOVERY_REQUIRED`, `DISCOVERY_RUN_ID_INVALID`,
`DISCOVERY_RUN_UNKNOWN`, `DISCOVERY_RUN_TERMINAL`,
`DISCOVERY_RUN_ID_REQUIRED`, `ACADEMIC_PROVIDER_ACCOUNTING_UNINITIALIZED`,
`ACADEMIC_PROVIDER_ACCOUNTING_INVALID`, `ACADEMIC_PROVIDER_REQUEST_LEDGER_INVALID`,
`ACADEMIC_PROVIDER_REQUEST_BUDGET_EXCEEDED`,
`ACADEMIC_PROVIDER_REQUEST_LEDGER_WRITE_FAILED`, and `WORKSPACE_UNREADABLE`.

## Request-Backed Academic Discovery

`academic` closes the gap between an open paper request and candidate review:

```bash
python3 scripts/discover_sources.py academic \
  --request-id req-paper-1234567890 \
  --run-id run-2026-07-20T202810Z-research \
  --provider arxiv \
  --provider openalex \
  --max-results 15 \
  --format json
```

The query defaults to the request's `query_or_identifier`; `--query` records an
explicit refinement. arXiv and OpenAlex are searched independently with bounded
limits. Matches are deduplicated by DOI, arXiv identity, OpenAlex identity, and
title/year fallback, and each candidate carries a provider-neutral `paper`
mapping consumed by `source_requests.py plan-fetch`. arXiv results are preprints
and OpenAlex results are index records; neither provider proves peer-review
status. `OPENALEX_API_KEY`, when used, is read only from the process environment
and is never written into candidates, reports, or URLs returned by the command.
Provider-backed managed actions pass their work order's exact `--run-id`, which
binds every transport attempt (including errors and retries) to that child
run's durable request budget. Omitting it is a manual compatibility convenience:
the command may infer the owner only when exactly one active run exists.
Run-bound discovery requires the versioned accounting marker written by
`run_controller.py start` and its pre-created empty ledger before the first
transport. An active run created by an older workspace version has no verifiable
zero-call baseline and therefore fails before network I/O with
`ACADEMIC_PROVIDER_ACCOUNTING_UNINITIALIZED`; preserve it and start a fresh run
rather than constructing accounting artifacts by hand. Discovery without any
selected or active run retains the local/manual compatibility behavior and does
not create a ledger implicitly.

Discovery ends after writing reviewable candidates. An agent must still review
and select candidates before acquisition can retrieve evidence.

## General Search Discovery

`discover_sources.py search --query TEXT` proposes candidate sources from a
configured general-search backend. It exists so a workspace can locate evidence
outside curated literature APIs while keeping discovery's "propose, don't fetch"
contract: results are normalized into `source_candidate` records for review, and
the command never downloads or ingests a result it proposes.

### Query planning is the default

`search` first **plans**: it expands the research need into a small, bounded set
of explained queries and emits them as JSON. Planning is read-only and contacts
no backend — a general search run is therefore explainable *before* any provider
is configured or any network I/O happens (planning even works with no provider
configured). The planned queries run only when the caller passes `--execute`.

The intent that selects the query templates is inferred (override with
`--intent`): a specific `--request-id` kind (`paper`/`code`/`dataset`) wins first,
then a `--jurisdiction` implies the `legal` intent, otherwise a general `web`
query is planned. Each planned query records its originating research need and
request, the generated query text, the expected source type, the inclusion and
exclusion domains, and a rationale for why the query is needed:

```json
{
  "mode": "plan",
  "research_need": "emissions reporting",
  "request_id": null,
  "intent": "legal",
  "jurisdiction": "us-federal",
  "network_io_executed": false,
  "planned_query_count": 7,
  "planned_queries": [
    {
      "query": "emissions reporting statute",
      "expected_source_type": "official_legal",
      "domain_allowlist": [],
      "domain_blocklist": [],
      "rationale": "Find the controlling statute or law text for jurisdiction us-federal. Prefer official government, legislature, regulator, court, or gazette sources over aggregators.",
      "prefer_official": true,
      "jurisdiction": "us-federal"
    }
  ]
}
```

The `legal` intent plans official-source-first queries using legal source-type
terms (`statute`, `code`, `regulation`, `administrative rule`, `agency guidance`,
`court opinion`, `official gazette`) and marks each query `prefer_official`.
Profile-driven enumeration of a jurisdiction's official domains is provided by the
dedicated [`legal` command](#legal-source-discovery); this general planner
provides the official-source-first terms and the explicit-before-execute boundary.

With `--execute`, each planned query is run through the configured provider and
the deduplicated candidates are aggregated, capped at `--max-results`, and
appended to `sources/discovery/candidates.jsonl`.

### Provider-neutral interface

The backend is provider-neutral and pluggable. Configure exactly one provider
under `integrations.discovery.search`; with none configured the command refuses
with `SEARCH_PROVIDER_DISABLED`. **No commercial search API is hard-coded**, and
an HTTP backend requires an explicit endpoint.

| Provider | Config | Behavior |
|----------|--------|----------|
| `fixture` | `fixture_path` (workspace-relative JSONL of raw results) | Reads local results; no network. Intended for tests and offline plans. |
| `command` | `command` (argv list or string; `{query}` is substituted, else the query is appended) | Runs a local search command that prints a JSON results array (or `{"results": [...]}`) to stdout. |
| `http` | `endpoint` (explicit http/https URL), optional `query_param` (default `q`), `params`, `results_path` (default `results`) | Issues one GET with the query, parsing a JSON results array. Records `network_io_executed: true`. |

```yaml
integrations:
  discovery:
    enabled: true
    providers: [search]
    search:
      provider: http
      endpoint: https://search.example.internal/api   # explicit; never a default vendor
      query_param: q
```

### Inputs and normalization

`search` accepts a `--query` (the research need) plus optional `--intent`,
`--request-id` (links candidates to a source request, otherwise a deterministic
`discovery_run_id` is used, mirroring `github`; an unknown id is still linked but
does not drive intent), `--domain-allow`/`--domain-block` (repeatable host
filters, including subdomains), `--jurisdiction` (passed through to each
candidate), and `--max-results`. During `--execute`, each raw backend result
(`title`, `url`, optional `snippet`, `published`, and optional provider hints
`official`, `source_type`, `trust_tier`, `license`, `terms_url`) becomes a
`source_candidate` with `provider: search`. Normalization filters by the domain
allow/block lists, collapses duplicate URLs, skips results without an http(s)
`url`, and caps the output at `--max-results`. Candidates are appended to
`sources/discovery/candidates.jsonl` idempotently.

### Trust ranking

Search results are **ranked by the trust-tier policy**, never by the provider's
own ordering. Provider rank is recorded (`search.provider_rank`) as a weak
relevance input only. Each result is classified by `classify_search_result`,
which derives `trust_tier`, `trust_score`, `relevance_score`, `official_source`,
`recommended_action`, and the five reasoning fields from policy signals:

- **Official-source signals (combination).** A result is `official_primary`
  when its host matches the optional `integrations.discovery.search.official_domains`
  allowlist, the host is on `--domain-allow` for an official/legal query, the host
  is under a conservative official TLD (`.gov`/`.mil`), or the provider result
  carries `official: true`. (The [`legal` command](#legal-source-discovery) feeds
  a jurisdiction profile's official domains into this allowlist.) A clean official
  candidate is `recommended_action: fetch`; it is still never fetched by discovery.
- **Relevance.** An exact query-phrase match in the title/snippet (Ranking Rule 2
  analog) beats fuzzy token overlap; provider position is a weak input only.
- **Freshness.** A `published` date older than a staleness threshold raises a
  `stale_source` risk flag and a freshness note; a recent date is a small
  `trust_score` signal.
- **Risk flags.** In addition to the documented set, search reasoning may emit
  `suspicious_download`, `stale_source`, and `duplicate_of_official`.

A cross-result pass (`apply_search_trust_rejection`) then rejects, with recorded
rationale rather than silently dropping:

- **Suspicious downloads.** A direct executable/installer/script download
  (`.exe`, `.msi`, `.dmg`, `.apk`, …) is `unsafe_or_unusable` with a
  `suspicious_download` flag. An archive (`.zip`, `.tar`, …) is suspicious only
  for `web_page`/`official_legal` results — a `dataset` or `code_repository`
  query legitimately resolves to an archive.
- **Mirrors and scraped copies.** Mirror/cache/scrape telltales in the host,
  path, or title raise a `possible_mirror` flag (or `unsafe_or_unusable` with
  `terms_prohibited` when the title signals an unauthorized copy).
- **Lower-trust duplicates of an official source.** When a run also contains an
  `official_primary` candidate, a non-official mirror or a high-title-overlap
  duplicate is marked `recommended_action: reject` with a `duplicate_of_official`
  flag, so the planner is handed the authoritative source first.

Any result whose officialness cannot be determined is **not** promoted to
`official_primary`: it is classified `secondary_unknown` with
`official_source: null` and `recommended_action: review` (Ranking Rule 3).
Candidates are ordered by trust tier, then known-official, then `trust_score`,
then `relevance_score` — so a higher-provider-ranked generic page never outranks
the official source it merely describes.

```yaml
integrations:
  discovery:
    enabled: true
    providers: [search]
    search:
      provider: http
      endpoint: https://search.example.internal/api
      official_domains:           # optional; raises these hosts to official_primary
        - govinfo.gov
        - regs.gov
```

Error codes specific to search are `SEARCH_PROVIDER_DISABLED` (no provider
configured) and `SEARCH_PROVIDER_FAILED` (the configured command or fixture could
not produce results); malformed backend output uses `DISCOVERY_RESPONSE_INVALID`
and HTTP transport failures use `DISCOVERY_NETWORK_ERROR`.

## Jurisdiction Profiles

Legal and regulatory research has a higher trust bar than general web discovery:
for laws, rules, agency guidance, and court opinions a workspace should prefer the
**official source for its chosen jurisdiction** and treat everything else as
supplemental. A *jurisdiction profile* records those official source roots so that
[legal discovery](#legal-source-discovery) and the
[search trust ranker](#trust-ranking-e33-t03) can recognize an official
domain instead of guessing.

Profiles live in a **workspace-local, user-editable file** — `sources/jurisdictions.yml`
by default, overridable with `integrations.discovery.jurisdictions_path` (pinned
under the workspace, like `sources.manifest_path`). This is intentional: the
template does **not** ship a stale universal legal database as code. Each
workspace curates the jurisdictions it actually researches and keeps them current.

```yaml
# sources/jurisdictions.yml
schema_version: "1.0"
jurisdiction_profiles:
  - jurisdiction_id: us-federal          # lowercase slug, unique within the file
    name: United States (Federal)
    country: US                          # ISO 3166-1 alpha-2
    official_domains:                    # bare hosts; subdomains match too
      - govinfo.gov
      - ecfr.gov
      - federalregister.gov
    legislature_urls: [https://www.congress.gov]
    regulator_urls: [https://www.federalregister.gov]
    court_urls: [https://www.supremecourt.gov]
    gazette_urls: [https://www.govinfo.gov/app/collection/fr]
    blocked_domains: []
    notes: "U.S. federal primary legal sources."
  - jurisdiction_id: us-ca               # a state-level (sub-national) profile
    name: California (State)
    country: US
    state_or_region: CA
    official_domains: [leginfo.legislature.ca.gov, oal.ca.gov, courts.ca.gov]
    legislature_urls: [https://leginfo.legislature.ca.gov]
    regulator_urls: [https://oal.ca.gov]
    court_urls: [https://courts.ca.gov]
```

### Profile fields

| Field | Type | Notes |
|-------|------|-------|
| `schema_version` | string (file-level) | Pinned to `1.0`; stamped onto every loaded profile. |
| `jurisdiction_id` | string | **Required.** Lowercase slug of letters, digits, and hyphens (for example `us-federal`, `us-ca`). Unique within the file; matches the `jurisdiction` field on candidate records. |
| `name` | string | **Required.** Human-readable label. |
| `country` | string | **Required.** ISO 3166-1 alpha-2 code; normalized to upper case (`us` → `US`). |
| `state_or_region` | string or null | Optional sub-national scope for state/province/region profiles. |
| `official_domains` | list of hosts | Bare domains (no scheme); normalized to lower case with a leading `www.` stripped. Host matching also matches subdomains (`www.govinfo.gov`, `api.ecfr.gov` match `govinfo.gov`/`ecfr.gov`). |
| `legislature_urls` | list of URLs | Entry points for statutes/codes. Each must be an `http(s)` URL. |
| `regulator_urls` | list of URLs | Agency/regulator roots (rules, guidance). |
| `court_urls` | list of URLs | Court opinion roots. |
| `gazette_urls` | list of URLs | Official gazette / legislative-notice roots. |
| `blocked_domains` | list of hosts | Hosts to exclude for this jurisdiction (normalized like `official_domains`). |
| `notes` | string or null | Optional free-text context. |

A profile is **invalid** unless it carries at least one official source root —
that is, at least one of `official_domains`, `legislature_urls`,
`regulator_urls`, `court_urls`, or `gazette_urls` is non-empty. This guarantees a
profile can actually anchor official-source-first discovery rather than naming a
jurisdiction with no authority to point at.

### Validating and inspecting profiles

The `jurisdictions` subcommand reads and validates the file. It is **offline**:
it parses a workspace-local YAML file and never contacts a provider, so — like
[`candidates`](#candidate-review-and-selection) — it runs before the discovery
gate and works even when `integrations.discovery.enabled` is `false`. Profiles
can therefore be curated and checked with acquisition fully disabled.

```bash
python3 scripts/discover_sources.py jurisdictions validate                 # check every profile
python3 scripts/discover_sources.py jurisdictions list                     # summary of each profile
python3 scripts/discover_sources.py jurisdictions show --jurisdiction us-ca # full profile
```

A missing file is **not** an error: `validate` and `list` report
`jurisdictions_path_exists: false` and `count: 0` so a workspace that has not yet
defined any jurisdiction is simply empty, not broken. A malformed file or any
schema violation fails with `JURISDICTION_INVALID` (the message names the
offending profile), and `show` for an id with no matching profile fails with
`JURISDICTION_UNKNOWN`. Both envelopes carry `network_io_executed: false`.

A user can therefore declare the legal jurisdiction and its official source roots,
confirm them with `validate`, and have those official domains ready *before* any
legal discovery run is planned or executed.

## Legal Source Discovery

`discover_sources.py legal --jurisdiction TEXT --topic TEXT --max-results N`
expands a legal/regulatory topic into an **official-source-first query plan** for
a chosen jurisdiction. It is the legal counterpart to the general `search`
planner, but it is *profile-driven*: it loads the matched
[jurisdiction profile](#jurisdiction-profiles) and threads that profile's official
domains and entry-point roots into every planned query, so the plan prefers the
authoritative source for the jurisdiction instead of guessing from a TLD.

By default `legal` is **read-only**: it produces an explained plan and records
`network_io_executed: false`, never contacting a search backend, so an
official-source-first plan is explainable *before* any provider is called. Adding
`--execute` runs the planned queries through the configured search backend and
**ranks the results by officialness** (see
[Legal candidate ranking](#legal-candidate-ranking-e34-t03) below).

`--jurisdiction` accepts either a profile `jurisdiction_id` (for example
`us-federal`) or its display `name` (for example `California (State)`). The plan
distinguishes the legal source categories below, emitting one explained query per
category (`legal_category`), all with `expected_source_type: official_legal` and
`prefer_official: true`:

| `legal_category` | Query term | Profile entry-point root |
|------------------|-----------|--------------------------|
| `statute` | `statute` | `legislature_urls` |
| `regulation` | `regulation` | `regulator_urls` |
| `agency_guidance` | `agency guidance` | `regulator_urls` |
| `court_opinion` | `court opinion` | `court_urls` |
| `official_form` | `official form` | `regulator_urls` |
| `gazette_notice` | `official gazette notice` | `gazette_urls` |

Each planned query carries the profile's `official_domains` as its
`domain_allowlist`, the profile's `blocked_domains` as its `domain_blocklist`, and
the category's `profile_roots` (the specific entry-point URLs from the profile, for
example `legislature_urls` for `statute`) so a reviewer has the exact official
landing pages to check.

```json
{
  "command": "legal",
  "mode": "plan",
  "topic": "emissions reporting",
  "jurisdiction": "us-federal",
  "jurisdiction_resolved": true,
  "official_domains": ["govinfo.gov", "ecfr.gov", "federalregister.gov"],
  "network_io_executed": false,
  "warnings": [],
  "planned_query_count": 6,
  "planned_queries": [
    {
      "query": "emissions reporting statute",
      "legal_category": "statute",
      "expected_source_type": "official_legal",
      "domain_allowlist": ["govinfo.gov", "ecfr.gov", "federalregister.gov"],
      "domain_blocklist": [],
      "profile_roots": ["https://www.congress.gov"],
      "prefer_official": true,
      "jurisdiction": "us-federal",
      "rationale": "Find the controlling statute or law text. Prefer official us-federal government, legislature, regulator, court, or gazette sources over aggregators."
    }
  ]
}
```

A missing or incomplete profile is a **warning, not an error** — the plan is still
produced so the topic is explainable, just without official-domain
prioritization:

- `no_jurisdiction_profile`: the `--jurisdiction` value matched no profile. The
  plan uses generic legal terms with an empty `official_domains` allowlist. Add a
  profile (see [Jurisdiction Profiles](#jurisdiction-profiles)) for
  official-source-first results.
- `no_official_domains`: a profile matched but declared no `official_domains`
  (only URL roots). Official-domain filtering will not apply; add `official_domains`
  for stronger official-source ranking.

### Legal candidate ranking

With `--execute`, each planned query runs through the configured search provider
(same provider-neutral backend as [general search](#provider-neutral-interface);
`--execute` with no provider configured refuses with `SEARCH_PROVIDER_DISABLED`).
Results are normalized to `source_candidate` records, aggregated, deduplicated, and
ranked with the [trust-tier policy](#trust-ranking-e33-t03) plus legal-specific
rules. Candidates retain strategy attribution (`provider: legal`) and record the
concrete search transport in their search metadata; `legal` itself is never network
authorization. They are written
to `sources/discovery/candidates.jsonl`; nothing is fetched.

A key difference from the general planner: the profile's official domains are used
as a **major trust signal, not a hard filter**. A host that matches an official
domain is raised to `official_primary`, but non-official results are *retained* and
ranked below — legal review needs to see the secondary sources, not have them
silently dropped. The legal rules are:

- **Official domain match is the major trust signal.** A host matching the
  jurisdiction profile's `official_domains` (or a `.gov`/`.mil` TLD) is
  `official_primary`. Official gazette, legislature, regulator, and court sources
  therefore outrank aggregators and blogs (the sort key ranks `official_primary`
  first). A clean official source is `recommended_action: fetch`.
- **Secondary legal databases are retained, not promoted.** Recognized reputable
  aggregators (Justia, FindLaw, CourtListener, Cornell LII, Westlaw, …) are kept as
  `secondary_reputable` with `official_source: false`. When an official source is
  available in the same run they are flagged `secondary_when_official_available` and
  marked supplemental-only (`recommended_action: review`) — never used as primary
  authority, but never dropped.
- **Mirrors and duplicates of an official source are rejected.** A non-official
  mirror/scraped copy, or a lower-trust duplicate (high title overlap) of an
  official source available in the run, is `recommended_action: reject` with a
  `possible_mirror`/`duplicate_of_official` flag. Recognized reputable secondary
  databases are exempt from this rejection.
- **Stale or superseded pages get risk flags.** A page whose title/snippet signals
  a repealed, superseded, or historical version raises a `superseded_or_historical`
  flag (distinct from the date-based `stale_source` flag) and downgrades an
  otherwise-clean official `fetch` to `review`.
- **Every candidate states its officialness.** The rationale records *why* a source
  is official (profile official-domain match) or why officialness is unknown, so the
  ranked list is safe to hand to a human or agent for review.

## GitHub Repository Discovery

`discover_sources.py github --query TEXT --max-results N` proposes GitHub
repository candidates for a paper title, project name, or free-form code query.
It is the first network-backed discovery provider, and it follows every rule in
[Discovery Is Not Acquisition](#discovery-is-not-acquisition):

- **Bounded search only.** The command issues a single GitHub repository search
  request (`GET /search/repositories`) with a small `per_page`. It never clones a
  repository, downloads an archive or release asset, or reads file contents.
  Repository contents become evidence only through a later, explicit acquisition
  step: `scripts/fetch_sources.py github repo-metadata | release-metadata |
  download-archive` for a selected repository (see
  [acquisition.md](acquisition.md)). Acquisition requires an explicit
  `--repo`/`--url` (and `--ref` for archives); it never reuses a discovery search
  result automatically.
- **`GITHUB_TOKEN` from the environment only.** When `GITHUB_TOKEN` is set, the
  adapter authenticates for GitHub's higher rate limit; otherwise it runs
  unauthenticated within GitHub's published unauthenticated limit. The token is
  never written to `research.yml`, the candidate store, logs, or command output.
  The JSON report exposes only `token_used: true|false`.
- **`network_io_executed: true`.** Because the search request touches the
  network, every github candidate records `network_io_executed: true`. The
  candidate is still only a proposal — discovery does not fetch it.

### Candidate fields

Each github candidate is a `source_candidate` with `provider: github`,
`source_type: code_repository`, and a provider-specific `github` block carrying:
repository `url` (the candidate `url`), `owner`/`repo`/`full_name`,
`default_branch`, `description`, detected SPDX `license_key` (also propagated to
the top-level `license`), `stars`/`forks` as **weak popularity metadata only**,
`archived` and `is_fork` flags, `pushed_at`, and a canonical
`latest_release_url` pointer (`{repo}/releases/latest`, unverified during
discovery).

When `github` runs with `--request-id`, candidates use that `request_id` origin
and set `discovery_run_id: null`. Exploratory runs without `--request-id` use a
deterministic `discovery_run_id` and set `request_id: null`.

### Trust reasoning

A code repository is a `primary_non_official` source. Discovery cannot confirm
canonical ownership from a search result, so every github candidate has
`official_source: null` and, by Ranking Rule 3, `recommended_action: review` —
never `fetch`. The reasoning signals are:

- **Owner / title match.** An exact `owner/repo` (or repo-name) match is an
  identifier match and outranks fuzzy lexical overlap (Ranking Rule 2). It raises
  `relevance_score` and is recorded in `reasoning.matched_query_terms`.
- **License presence.** A detected SPDX license (GitHub's `NOASSERTION` sentinel
  is treated as unknown) raises `trust_score`; a missing license adds a
  `license_uncertain` risk flag (Ranking Rule 4: suggest, not silently ingest).
- **Archived status.** An archived repository adds an `archived` risk flag and a
  staleness note in `freshness_reason`.
- **Forks.** A fork is a possible mirror, so it is lowered to `secondary_unknown`
  with a `possible_mirror` risk flag.

Candidates are ranked by trust tier first, then `trust_score`, then
`relevance_score`, so a well-licensed canonical repository outranks a fork even
when the provider returned the fork first. The ranked candidates are appended to
`sources/discovery/candidates.jsonl` idempotently (re-running the same query
does not duplicate records) and echoed in the command's JSON report.

## Author Extraction

`discover_sources.py authors --source-id SOURCE_ID --max-results N` reads a
**normalized paper source** already in the workspace and emits a bounded *author
seed list* — the read-only preparation path for author and publication expansion.
By default it is read-only and contacts no provider (`network_io_executed: false`).
With `--discover-publications` (see [Related Publication Discovery](#related-publication-discovery)
below) the extracted seeds drive a network-backed OpenAlex lookup that proposes
the authors' other works as candidates.

Author names come from the normalized record's `authors` frontmatter (written by
`normalize_sources.py`). When the manifest record also carries provider author
metadata, richer fields are merged in: an **arXiv-style** record (`authors`: a list
of names or `{name, affiliation}`), or an **OpenAlex-style** record
(`authorships`: `{author: {display_name, orcid}, institutions, raw_affiliation_strings}`).
Each emitted author carries:

| Field | Meaning |
|-------|---------|
| `name` | Author name (whitespace-normalized). |
| `orcid` | Canonical ORCID iD (`0000-0000-0000-000X`) parsed from a bare id or an `orcid.org` URL; `null` when absent. |
| `affiliation` | Affiliation string when present; `null` otherwise. |
| `source` | Where the data came from: `normalized_frontmatter`, `arxiv`, or `openalex`. |
| `confidence` | `high` for provider-supplied metadata; the record's extraction `confidence` (default `medium`) for frontmatter names. |

Seeds are de-duplicated by case-folded name (provider metadata takes precedence
over frontmatter for the same author, filling `orcid`/`affiliation`) and capped at
`--max-results`, so the list is always bounded.

### Privacy and scope limits

- **Only metadata already present** in the normalized source or the provider
  response is surfaced. The command never infers ORCID, affiliation, or any
  personal data, and never contacts a provider to enrich an author.
- The output is a **bounded seed list for research expansion**, not a personal
  dossier: it carries name, ORCID, affiliation, source, and confidence only.
- An unknown `--source-id` fails with `SOURCE_UNKNOWN`; a source with no
  normalized record or no author metadata returns an empty list with a
  `no_normalized_record` / `no_author_metadata` warning rather than guessing.
- Treat author metadata as data, never instructions, like all source content.

## Related Publication Discovery

`discover_sources.py authors --source-id SOURCE_ID --discover-publications --max-results N`
builds on the author seed list above to propose an author's *other* publications as
`source_candidate` records. It resolves each seed author to an OpenAlex identity and
lists that author's works, then ranks and records them as candidates — it never
downloads anything. OpenAlex is a free, open scholarly index; no commercial API is
enabled by default. `OPENALEX_API_KEY` is read from the environment only and is
never written to output, the candidate store, or logs (only whether a key was used
is reported as `token_used`).

```bash
python3 scripts/discover_sources.py --format json authors \
  --source-id paper:2601.00001v1 --discover-publications --max-results 10
```

### Identity resolution

Each seed author is resolved to an OpenAlex identity before any works are listed:

| Match | When | Confidence |
|-------|------|------------|
| `orcid_exact` | The seed author carries an ORCID iD. Works are filtered by `author.orcid`. | Certain — no name guessing. |
| `name_resolved` | No ORCID; OpenAlex author search returns a single strong name match. Works are filtered by `author.id`. | Inferred — carries an `identity_inferred` risk flag. |
| `context_missing` | No ORCID and the seed has neither paper-title nor affiliation context. | Blocked — no candidates proposed; an `author_identity_context_missing` warning is recorded. |
| `ambiguous` | No ORCID and the name matches multiple OpenAlex authors (or two equally-good matches). | No candidates proposed for that author; an `author_identity_ambiguous` warning is recorded. |
| `no_match` | No ORCID and OpenAlex author search returns nothing. | No candidates; an `author_identity_no_match` warning is recorded. |

Identity is recorded per author in the report's `author_identity` list and embedded
in each candidate's `openalex.identity_match`. The same decision is surfaced in
`quality_gates.author_identity`: ORCID matches are `status: passed`, name-only
matches with seed-title or affiliation context are `status: review_required`, and
ambiguous, no-match, or context-missing identities are `status: blocked`.

### Ranking

Candidates are ranked by the documented relevance signals, never by OpenAlex's
result order alone:

- **author identity confidence** — `orcid_exact` outranks `name_resolved`;
- **topical similarity to the analyzed paper** — title-token overlap with the seed
  paper's title (a work sharing no terms is treated as out of scope, see below);
- **publication year relevance** — recent works get a small freshness bonus;
- **citation/reference relation** — `cited_by_count` contributes a weak authority
  signal (direct citation-graph relation to the seed is not separately verified
  from the works listing and is noted as such);
- **open-access availability** — open-access works get a small bonus and a missing
  `not_open_access` risk flag when not.

Every publication candidate is `source_type: paper`, `trust_tier:
primary_non_official`, with `official_source: null` — a paper is a primary source,
but canonical publisher authority is not verified from the index, so each candidate
is `recommended_action: review` until an operator confirms it. Name-resolved
publication candidates also keep the `identity_inferred` risk flag and
`quality_gates.author_identity.review_required: true`.

### Out-of-scope rejection

A work by the author that shares **no analyzed-paper title terms** is recommended
for rejection (`recommended_action: reject`, `out_of_scope` risk flag) rather than
silently dropped: it is genuinely the author's work but unrelated to this paper's
research context, so the workspace records why it was set aside. The candidate is
still written to the store and can be selected on review. The seed paper itself
(matched by OpenAlex id, DOI, or exact title) is never proposed.

### Privacy and scope limits

- Discovery uses **only metadata already present** in the normalized source or
  returned by OpenAlex's author/works endpoints. It never infers ORCID,
  affiliation, or personal data, and never expands beyond the seed authors.
- Discovery is **bounded**: at most ten seed authors per run, a capped number of
  works per author, and `--max-results` candidates overall. It never paginates
  indefinitely or resembles crawling.
- `network_io_executed` is `true` for a discovery run; without
  `--discover-publications` the command stays read-only (`false`).

## Companion Artifact Discovery

`discover_sources.py companions --source-id SOURCE_ID --max-results N` expands a
single analyzed paper into its likely **companion artifacts**: the code, data, and
pages that travel with the work. It is a paper-centered composite of the other
providers rather than a new transport, and it proposes candidates for review —
it never fetches, clones, or executes anything.

Candidates carry one of five `source_type` values:

| `source_type` | Typical host | Base trust tier |
| --- | --- | --- |
| `code_repository` | github.com, gitlab.com, bitbucket.org | `primary_non_official` |
| `dataset` | zenodo.org, figshare.com, huggingface.co, osf.io, … | `primary_non_official` |
| `publisher_page` | doi.org (canonical landing page) | `official_primary` |
| `supplemental_material` | arxiv.org, biorxiv.org, … (preprint hosts) | `primary_non_official` |
| `project_page` | any other host | `primary_non_official` |

The command runs three composed phases and merges their output:

1. **Inline (no network, highest trust).** Links already present in the paper —
   the normalized record's `links` frontmatter and bare URLs in its body, plus
   landing-page URLs carried in OpenAlex-style provider metadata — are extracted
   first. These are `paper_inline` / `provider_metadata` candidates: the paper
   itself cites them, so they outrank anything found by search. The seed paper's
   own arXiv listing is filtered out so it is never proposed as its own companion.
2. **GitHub (`network`, skip with `--no-github`).** A bounded GitHub repository
   search (reusing the [GitHub provider](#github-repository-discovery)). A
   repository already cited inline collapses against its search hit (the inline
   origin wins the dedupe), so the same repo is never proposed twice.
3. **Search (`network`, skip with `--no-search`).** When a search provider is
   configured, bounded queries propose datasets and project pages. With no
   provider configured the phase is skipped with a `no_search_provider` warning;
   inline and GitHub results are still returned.

Phases 2 and 3 do not search on the title alone. A small, explainable **query
plan** widens recall using the paper title, a short pre-colon project/system name
(the "SystemName: subtitle" convention), the lead-author surname, and the
arXiv/DOI identifier. The plan stays bounded — at most three queries per phase,
de-duplicated — and is echoed in the report (`query_plan`, and each network
phase's `queries`) so a reviewer can see exactly what was asked.

Candidates rank by **origin first** (`paper_inline` > `provider_metadata` >
`github_search` > `search`), then trust tier, officialness, and scores. A generic
`project_page` found only by search is downgraded to `secondary_unknown` because
its ownership is unverified. Every candidate is `recommended_action: review` and
linked to the seed paper (`seed_source_id`); pass `--request-id ID` to also tie
them to a source request.

Repository candidates additionally state their `repository_link_origin` inside
the `companions` block and carry `quality_gates.companion_repository`. Inline and
provider-metadata repository links are higher-confidence origins because they came
from the analyzed paper or provider metadata, but they still require review.
GitHub-search and general-search repositories are recorded as `search_only` and
must never be treated as verified author-owned companion repositories without a
later review/acquisition step.

```json
{
  "schema_version": "1.0",
  "candidate_id": "cand-7f3a9c2b1d",
  "request_id": null,
  "seed_source_id": "paper:2005.11401",
  "discovery_run_id": "companions-1a2b3c4d",
  "discovered_at": "2026-06-20T12:00:00Z",
  "discovered_by": "discover_sources.py/companions",
  "provider": "companions",
  "url": "https://github.com/acme/rag-toolkit",
  "title": "https://github.com/acme/rag-toolkit",
  "source_type": "code_repository",
  "trust_tier": "primary_non_official",
  "relevance_score": 0.85,
  "trust_score": 0.74,
  "official_source": null,
  "jurisdiction": null,
  "license": null,
  "terms_url": null,
  "rationale": "Companion code_repository at https://github.com/acme/rag-toolkit (a link cited directly in the analyzed paper) classified primary_non_official; recommended for review before fetch.",
  "recommended_action": "review",
  "network_io_executed": false,
  "evidence_origin": "paper_inline",
  "reasoning": {
    "matched_query_terms": ["rag", "retrieval", "toolkit"],
    "authority_reason": "github.com classified as code_repository (primary_non_official) from its host; the link is a link cited directly in the analyzed paper. Canonical ownership is not verified from the link, so official_source requires review.",
    "freshness_reason": "No separate freshness signal; the link is treated as evergreen evidence context.",
    "scope_reason": "Title/URL shares analyzed-paper term(s); retained as a companion code_repository for review.",
    "risk_flags": ["unknown_officialness"]
  },
  "quality_gates": {
    "companion_repository": {
      "status": "review_required",
      "repository_link_origin": "paper_inline",
      "origin_confidence": "paper_linked",
      "review_required": true
    }
  },
  "companions": {
    "host": "github.com",
    "evidence_origin": "paper_inline",
    "source_type": "code_repository",
    "repository_link_origin": "paper_inline"
  }
}
```

The run report adds a `phases` array (per-phase `network_io_executed`,
`candidate_count`, and the executed `queries`, with `skipped: true` for
`--no-github`/`--no-search`) so a reviewer can see which phases ran, what they
asked, and whether any contacted the network. A local fixture/command search
backend does no network, so its search phase reports `network_io_executed: false`.

## Candidate Review and Selection

Reviewing candidates and choosing which to pursue is a deliberate, **offline**
stage separate from both discovery and acquisition. The `candidates` subcommand
of `discover_sources.py` reads and updates the durable candidate store and never
contacts a provider — it runs even when discovery is disabled, because it only
inspects and annotates records that already exist.

```bash
python3 scripts/discover_sources.py candidates list --state proposed
python3 scripts/discover_sources.py candidates transition --candidate-id cand-1a2b3c4d5e --expected-state proposed --to-state reviewed --reason "authority and evidence fit reviewed"
python3 scripts/discover_sources.py candidates select --candidate-id cand-1a2b3c4d5e --request-id req-1a2b3c4d5e
python3 scripts/discover_sources.py candidates select --candidate-id cand-1a2b3c4d5e --create-request --priority high
python3 scripts/discover_sources.py candidates reject --candidate-id cand-1a2b3c4d5e --reason "lower-trust mirror of the official source"
```

### Lifecycle state machine

Each candidate carries an authoritative `lifecycle_state`, distinct from the
discovery `recommended_action` (which is only a suggested disposition). New
discovery output starts at `proposed`. The explicit transition table is:

| Prior state | Allowed new states |
|---|---|
| `proposed` | `reviewed`, `selected`, `rejected`, `deferred`, `superseded` |
| `reviewed` | `selected`, `rejected`, `deferred`, `superseded` |
| `selected` | `rejected`, `deferred`, `fetched`, `failed`, `superseded` |
| `deferred` | `reviewed`, `selected`, `rejected`, `superseded` |
| `failed` | `selected`, `rejected`, `deferred`, `superseded` |
| `rejected` | terminal; idempotent same-state repeat only |
| `fetched` | terminal; idempotent same-state repeat only |
| `superseded` | terminal; idempotent same-state repeat only |

`candidates list --state STATE` filters by canonical state and reports
`state_counts` for all eight states. Its older `--status new|selected|rejected|fetched`
filter and `counts` remain as coarse compatibility views for existing status/run
consumers; `status` is not the state-machine authority.

- **`select`** applies a legal transition to `selected`, sets
  `selected_for_request_id`, `selected_at`, and `selected_by`, and links an
  existing source request or creates one. Selection never fetches.
- **`reject`** applies a legal transition to terminal `rejected` and records a
  required reason. A rejected candidate cannot later be selected; use
  `deferred` before a decision or `superseded` when a distinct candidate replaces it.
- **`transition`** executes the other table edges. It requires
  `--expected-state`, `--to-state`, `--reason`, and an audit actor (defaulting to
  the candidate command actor). `selected` requires `--request-id`; `fetched`
  requires an inventoried `--source-id`; `superseded` requires a distinct,
  active `--superseded-by-candidate-id`. `--run-id` records optional run
  correlation.

Examples for acquisition outcomes and postponement:

```bash
python3 scripts/discover_sources.py candidates transition --candidate-id cand-1a2b3c4d5e --expected-state selected --to-state fetched --source-id paper:2601.00001v1 --reason "inventoried acquisition completed"
python3 scripts/discover_sources.py candidates transition --candidate-id cand-1a2b3c4d5e --expected-state selected --to-state failed --reason "provider returned no usable artifact"
python3 scripts/discover_sources.py candidates transition --candidate-id cand-1a2b3c4d5e --expected-state reviewed --to-state deferred --reason "current budget exhausted"
python3 scripts/discover_sources.py candidates transition --candidate-id cand-old000000 --expected-state reviewed --to-state superseded --superseded-by-candidate-id cand-current000 --reason "current official edition replaces historical page"
```

All state checks happen under the candidate-store lock. A stale
`--expected-state` fails with `CANDIDATE_STATE_STALE`; an edge absent from the
table fails with `CANDIDATE_TRANSITION_INVALID`; and a same-state repeat that
changes request/source/replacement/reason correlation fails with
`CANDIDATE_CORRELATION_CONFLICT`. Exact repeats are no-ops and do not append an
event.

### Legacy migration semantics

Records without lifecycle fields remain readable through an explicit mapping:
missing `status` or `status: new` maps to `proposed`; legacy `selected`,
`rejected`, and `fetched` map to the same-named canonical states. The read view
adds `lifecycle_migration` with `review_state_inferred: false`; listing never
rewrites the file. A later successful mutation may persist that explicit mapping
alongside the record, but it never upgrades `new`/missing records to `reviewed`.
Unknown or contradictory lifecycle fields fail closed.

The compatibility commands retain their established behavior within the table:

- **`select`** sets `status: selected` and records
  `selected_for_request_id`, `selected_at`, and `selected_by`. During the
  transition from earlier candidate stores it also writes the legacy
  `selected_request_id` alias and continues to read either field. It links the
  candidate to an existing source request (`--request-id`, which must exist) or
  mints one derived from the candidate (`--create-request`, reusing an open
  request for the same evidence rather than duplicating it). Selection **never
  fetches**: it only links the candidate so a later, explicit acquisition step can
  act on it.
- **`reject`** sets `status: rejected` and records the required `--reason`,
  `rejected_at`, and `rejected_by`.

Both are idempotent for the same state and correlation. They accept optional
`--expected-state`, `--actor`, and `--run-id` so orchestrated writers can use the
same optimistic concurrency and audit contract as `transition`.

### Durable, concurrent-safe updates

Lifecycle changes are written atomically: the candidate store is rewritten with a
temp-file rename under a stable workspace lock (`sources/discovery/.locks/`), so
concurrent writers serialize instead of clobbering each other. Every applied
transition also appends a structured event to the
append-only audit trail at `sources/discovery/audit.jsonl`, so the workspace
keeps `event_id`, actor, prior/new state, reason, candidate id, request id, run
id, source id, UTC time, and request-creation correlation without contacting a
provider again.

## From Candidate To Evidence

A candidate becomes real evidence only through the existing acquisition and
delivery pipeline, never automatically:

1. An agent reviews candidates in `sources/discovery/candidates.jsonl` and the
   rationale, trust tier, and recommended action for each
   (`discover_sources.py candidates list`).
2. The agent explicitly selects a candidate and links it to a source request
   (`discover_sources.py candidates select`), or rejects it with a reason.
3. Acquisition fetches *only* the selected candidate into the configured
   `raw/` target with a `.provenance.yml` sidecar
   ([source-delivery.md](source-delivery.md), [acquisition.md](acquisition.md)).
4. Inventory produces the manifest source id. Transition the candidate from
   `selected` to `fetched` with that `--source-id`; this bookkeeping command does
   not fetch the artifact itself.
5. Normalization turns the delivered file into a normalized record;
   `source_requests.py fulfill` closes the request loop.

Until step 3 writes a sidecar under `raw/`, a candidate has no manifest record,
no normalized record, and no standing as evidence.

For academic papers, `source_requests.py plan-fetch` prefers the selected
candidate's `paper` metadata over URL parsing. A versioned arXiv id becomes an
exact `fetch_sources.py arxiv download` command; an unversioned arXiv id becomes
an `arxiv search --id-list` inspection command; an OpenAlex work with an
open-access PDF becomes `openalex download-pdf`; metadata-only, non-open-access,
or uncertain OpenAlex candidates become explicit `openalex get` or `resolve`
commands plus warnings. Planning remains read-only and records no new evidence.
When at least one candidate is selected, `candidate_routes` is the sole
executable plan, `routing_basis` is `selected_candidates`, and heuristic
request-level `routes` is empty. This prevents an unscoped command without
`--candidate-id` from competing with the reviewed route.

## Related Documents

- [acquisition.md](acquisition.md) — optional provider registry, safety model,
  and provenance requirements for fetching a selected candidate.
- [source-delivery.md](source-delivery.md) — target roots, atomic delivery,
  provenance sidecars, and source requests that close the discovery loop.
- [orchestrator-handoff.md](orchestrator-handoff.md) — where discovery sits in
  the external orchestrator lifecycle.
- [source-manifest.md](source-manifest.md) — manifest record fields a fetched
  candidate ultimately becomes.
- [normalized-source-format.md](normalized-source-format.md) — normalized record
  format produced after a candidate is fetched.
- [prompt-injection-hardening.md](prompt-injection-hardening.md) — candidate and
  source content is evidence data, never instructions.
