# Acquisition

This document defines the optional source-acquisition contract for fetch agents
that run inside or beside a research workspace. It documents when acquisition is
allowed, where fetched files land, which provider IDs are supported, and what
provenance is mandatory.

Acquisition is intentionally disabled by default. The workspace remains usable
as a local-files-only system unless a profile or maintainer explicitly enables
`integrations.acquisition`.

## Safety Model

- Acquisition is disabled by default. No command may touch the network unless
  `integrations.acquisition.enabled: true` is present in `research.yml`.
- Enabled acquisition must use an explicit provider allow-list in
  `integrations.acquisition.providers`. Supported provider IDs are `arxiv`,
  `openalex`, `github`, and `web`.
- No secrets in `research.yml`. The rule is: no secrets in `research.yml`.
  OpenAlex commands read `OPENALEX_API_KEY` and GitHub commands read
  `GITHUB_TOKEN` from the process environment only; workspaces must not store
  API keys, tokens, cookies, or credentials.
- Downloads must land only under the configured `target_root`, which must stay
  under `raw/`. The default target is `raw/papers`.
- Every automated download must write a `.provenance.yml` sidecar next to the
  downloaded file or directory before inventory and normalization run.
- Acquisition must not install hooks, start background sync, start background
  agents, auto-fetch, auto-download, auto-add, or auto-commit.
- Fetch agents must respect `max_downloads_per_run`, configured provider IDs,
  provider terms, and per-paper license limits. License uncertainty must be
  surfaced instead of guessed.
- The harmonized acquisition budget reported by `workspace_status.py` is for
  orchestrator decisions, not provider throttling. It reports
  `max_acquisition_downloads_per_run` from
  `integrations.acquisition.max_downloads_per_run` and
  `max_github_archive_bytes_per_run` from
  `integrations.acquisition.github.max_archive_bytes`, alongside discovery
  result, OpenAlex/arXiv request, contracted web download, and manual URL
  delivery counters.

Minimal enabled shape:

```yaml
integrations:
  acquisition:
    enabled: true
    providers:
      - arxiv
      - openalex
      - web
    target_root: raw/papers
    max_downloads_per_run: 10
    require_license_check: true
    web:
      target_root: raw/web
      allowed_domains:
        - example.gov
      max_download_bytes: 10485760
```

The initializer and smoke validation reject acquisition automation keys such as
hooks, background sync, auto-fetch, auto-download, auto-add, and auto-commit.
Domain packs may recommend providers through `domain_pack.recommended_acquisition`,
but recommendations never enable acquisition by themselves.

Discovery and acquisition are independent permissions. Discovery produces
metadata candidates; acquisition retrieves only an explicitly selected
candidate. Allowing `arxiv` or `openalex` in one phase does not allow it in the
other. arXiv is primarily a preprint host and OpenAlex is an index; neither
proves peer review. A successful transport also does not infer reuse rights:
the selected artifact's license and provenance remain publication gates.

`source_inventory.py` and `normalize_sources.py` operate only on files already
delivered below configured `raw/` roots. They never search, resolve provider
metadata, or download a candidate.

## Provider Secrets And Rotation

Provider credentials are operator-managed runtime secrets. Inject
`OPENALEX_API_KEY` and `GITHUB_TOKEN` into the process environment for the one
run that needs them, preferably through the host secret store, CI secret
manager, or orchestrator secret injection. Do not store provider keys in
`research.yml`, provenance sidecars, source request records, run reports, logs,
or maintained wiki pages. A repo-root or workspace-root `.env` is acceptable
only as a local development convenience; `scripts/doctor.py` warns when a
readable `.env` is present and never prints its values.

Rotate `OPENALEX_API_KEY` when a `.env`, shell history, log, report, or shared
workspace may have exposed it:

1. Revoke or regenerate the key in the OpenAlex account or organization that
   issued it.
2. Update the operator secret store or CI secret.
3. Rerun `scripts/doctor.py --format json` and confirm no readable `.env`
   warning remains for production workspaces.
4. Rerun only the affected acquisition or citation-verification step; do not
   rewrite prior provenance unless it actually exposed the old value.

Rotate `GITHUB_TOKEN` the same way: revoke the token in GitHub, issue a
least-privilege replacement, update the operator secret store, and rerun the
bounded GitHub acquisition step. Prefer short-lived CI tokens or fine-grained
personal access tokens scoped only to the repositories being inspected.

## Provider Registry

| Provider ID | Terms and API docs | Rate-limit guidance | Attribution and license notes | Supported commands | License inference |
|-------------|--------------------|---------------------|-------------------------------|---------------------------|-------------------|
| `arxiv` | [arXiv API Terms](https://info.arxiv.org/help/api/tou.html), [arXiv API Manual](https://info.arxiv.org/help/api/user-manual.html), [arXiv Licenses](https://info.arxiv.org/help/license/index.html) | The arXiv API terms require no more than one request every three seconds for legacy APIs, using a single connection. Large result sets should be narrowed or paged in small slices. | arXiv metadata is CC0, but e-prints remain subject to copyright and per-paper licenses. Agents should link retrieved papers back to arXiv, prefer the abstract page for human review, and record license uncertainty when the adapter cannot determine a paper license. | `arxiv search`, `arxiv download` | Partial. Search metadata can identify the paper, but downloads should write `license: null` unless the adapter can determine the specific paper license. |
| `openalex` | [OpenAlex Authentication & Pricing](https://developers.openalex.org/api-reference/authentication), [OpenAlex Works Fields](https://developers.openalex.org/api-reference/works), [OpenAlex Licenses](https://developers.openalex.org/api-reference/licenses) | OpenAlex exposes usage budgets and rate-limit headers, returns 429 on exceeded limits, and recommends smaller selected fields plus backoff on rate limits. Use `select=` and bounded result pages for acquisition workflows. | OpenAlex metadata is open, and OpenAlex asks research users to cite its paper. Work license values are assigned to individual open-access locations; agents must record the OpenAlex work URL, downloaded URL, and any location license surfaced by metadata. | `openalex resolve`, `openalex get`, `openalex download-pdf` | Yes when an open-access location includes `license` or `license_id`; otherwise unknown and the sidecar must surface that uncertainty. |
| `github` | [GitHub Terms of Service](https://docs.github.com/en/site-policy/github-terms/github-terms-of-service), [REST API Rate Limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api), [REST API Authentication](https://docs.github.com/en/rest/authentication/authenticating-to-the-rest-api), [Licensing a Repository](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository) | The GitHub REST API limits unauthenticated requests to roughly 60 per hour per source IP and authenticated requests to roughly 5,000 per hour, returns 403/429 with `Retry-After` and `X-RateLimit-*` headers on exceeded limits, and forbids scraping or bulk cloning. Use bounded queries and small result pages; prefer authenticated requests via `GITHUB_TOKEN` for higher limits. | Repository contents stay under each repository's own license. GitHub's license detection is heuristic and frequently absent, so agents must record the detected SPDX license key when present and surface license uncertainty otherwise. Stars and forks are weak popularity signals, never trust or license proof. | `github repo-metadata`, `github release-metadata`, `github download-archive` | Partial. The API surfaces a detected `license` SPDX key for many repositories, but many repositories report no license; agents must not assume a permissive license when detection is absent. |
| `web` | Per-origin terms; record the reviewed origin terms in the sidecar with `terms_url` or `terms_note`. | Web acquisition is allow-list only. Configure `integrations.acquisition.web.allowed_domains`, keep `max_download_bytes` small, and fetch only explicit selected candidate URLs or user-provided URLs. `allowed_domains` is a transport allowlist, not a trust signal. | Generic web capture cannot infer a license. Sidecars write `license: null`, capture URL, retrieval metadata, checksum, byte count, TLS status, optional currentness flags, and optional publisher/jurisdiction/evidence areas. | `web get` | None. The operator must review and record license or terms status before reusable publication output depends on the source. |

## arXiv Adapter

The `scripts/fetch_sources.py arxiv` adapter is implemented with the Python
standard library. It uses `urllib` with bounded retries, 30 second request
timeouts, and a process-local limiter of one request every three seconds before
each arXiv request. Tests inject a mocked transport; normal workspace runs use
the public arXiv endpoints only after acquisition is explicitly enabled.

Search commands:

```bash
python3 scripts/fetch_sources.py arxiv search --query "language models" --max-results 5
python3 scripts/fetch_sources.py arxiv search --id-list 2601.00001v1,2601.00002v2 --max-results 2 --output sources/arxiv-search.json
```

Search output is compact JSON. Agents should redirect large result sets with
`--output`; stdout then contains only a small report naming the written file.
`--query` and `--id-list` may be combined, matching arXiv API filter semantics.

Download commands require versioned new-style arXiv identifiers so inventory can
produce stable `paper:<id>` records:

```bash
python3 scripts/fetch_sources.py arxiv download --id 2601.00001v1 --format pdf
python3 scripts/fetch_sources.py arxiv download --id 2601.00001v1 --format pdf --request-id req-1a2b3c4d5e --candidate-id cand-1a2b3c4d5e
python3 scripts/fetch_sources.py arxiv download --id 2601.00001v1 --format source --request-id req-1a2b3c4d5e --candidate-id cand-1a2b3c4d5e
```

PDF downloads write to `target_root/<id>.pdf`. Source downloads fetch the arXiv
e-print archive, safely extract regular files and directories into
`target_root/arxiv-<id>/`, and refuse to overwrite existing targets or sidecars.
Extraction rejects absolute paths, `..`, links, special files, and oversized
member names before writing files.

The arXiv adapter performs a bounded Atom metadata lookup at download time.
When it succeeds, the provenance sidecar records provider `title`, `authors`,
`published`, and any Atom DOI with `doi_source: arxiv-atom`. Metadata lookup
failure does not fail an otherwise successful download; the sidecar is still
written and the command reports the missing provider identity so verification can
fail honestly later.

When no license is inferable at download time, arXiv sidecars write
`license: unresolved` plus `terms_url` set to the arXiv abstract page. This is a
machine-readable uncertainty state: lint reports it as LOW while terms are
recorded, and `openalex enrich` is the autonomous upgrade path to a resolved
license. arXiv download sidecars also record `academic_provider: arxiv`,
`academic_source_type: preprint`, `venue: arXiv`, the derived publication year,
`oa_status: green`, and `peer_review_status: preprint` so exports can
distinguish preprints from indexed publisher records.

### Dual-Format arXiv Acquisition

Request-backed arXiv papers use dual-format arXiv acquisition: download the PDF
as the archival/checksum citation artifact and the source bundle as the
preferred normalization input. Pass the same `--request-id` and `--candidate-id`
to both commands:

```bash
python3 scripts/fetch_sources.py --format json arxiv download --id 2601.00001v1 --format pdf --request-id req-1a2b3c4d5e --candidate-id cand-1a2b3c4d5e
python3 scripts/fetch_sources.py --format json arxiv download --id 2601.00001v1 --format source --request-id req-1a2b3c4d5e --candidate-id cand-1a2b3c4d5e
```

`source_inventory.py` pairs the PDF and source bundle, and
`normalize_sources.py --all` prefers the LaTeX path; successful paired records
show `methods.latex` in normalization summaries. Size
`max_downloads_per_run` for `2 x papers + web deliveries`, because each arXiv
paper consumes one PDF download and one source-bundle download. PDF-only degradation
is allowed when the source bundle is unavailable, withdrawn, or
fails safe extraction; keep the warning visible and continue with PDF fallback.
If an already answered workspace is re-normalized and source-bundle text
changes quotes, rerun `verify_quotes.py --slug <slug> --write` for every
grounded answer before the next publication-readiness evaluation.

## OpenAlex Adapter

The `scripts/fetch_sources.py openalex` adapter is implemented with the Python
standard library. It reads `OPENALEX_API_KEY` from the process environment only;
keys, tokens, and other secrets must not be stored in `research.yml`. The
adapter still operates without a key by omitting `api_key`, using a conservative
one-request-per-second process-local limiter, and reporting a notice in command
JSON so agents know the run used keyless access.

OpenAlex metadata requests use selected root fields only:

```text
id,doi,display_name,publication_year,type,authorships,primary_location,best_oa_location,open_access,locations
```

Search and lookup commands:

```bash
python3 scripts/fetch_sources.py openalex resolve --entity works --query "Synthetic Retrieval Paper" --max-results 5
python3 scripts/fetch_sources.py openalex resolve --entity works --query "TurboQuant" --max-results 5 --allow-unconfirmed
python3 scripts/fetch_sources.py openalex get --id-or-doi W260100001
python3 scripts/fetch_sources.py openalex get --id-or-doi 10.5555/example
python3 scripts/fetch_sources.py openalex get --id-or-doi W260100002 --output raw/papers/openalex-W260100002-metadata.json --request-id req-1a2b3c4d5e
python3 scripts/fetch_sources.py openalex enrich --source-id paper:2601.00001v1 --request-id req-1a2b3c4d5e
python3 scripts/fetch_sources.py openalex enrich --all-arxiv --request-id req-1a2b3c4d5e
```

`resolve` returns compact JSON and only auto-selects a `resolved` work when a
candidate title matches the query exactly after whitespace and case
normalization. If OpenAlex returns only fuzzy or related candidates, the command
fails with `OPENALEX_RESOLUTION_UNCERTAIN`; agents must inspect candidates and
rerun `get --id-or-doi` with an explicit OpenAlex work ID or DOI instead of
fabricating an identifier.

For bounded negative claim probes, `resolve --allow-unconfirmed` keeps the
search bounded and returns a normal JSON report when no exact match exists:
`resolution_status: unconfirmed`, `resolved: null`, `candidate_count`,
`exact_match_count: 0`, and the limitation
`not found in configured providers for this bounded run; not a global nonexistence claim`.
Use that payload only as `coverage_manifest.yml` `claim_probe` metadata. It is
not a source, citation, or global nonexistence claim.

`openalex enrich` updates existing arXiv acquisition sidecars in place. It
resolves by recorded DOI first, then by the versionless DataCite DOI derived
from the arXiv id, and records `openalex_work_id`, canonical `doi`,
`doi_source`, `license`, `provider_license_slug`, `license_source`, `oa_status`,
`openalex_publication_year`, and an explicit enrichment status. It maps only
unambiguous provider license slugs to SPDX identifiers: for example
`public-domain` and `cc0` map to `CC0-1.0`, versioned slugs such as
`cc-by-4.0` map to canonical SPDX case, and ambiguous bare slugs such as
`cc-by` remain `license: unresolved` with the raw slug preserved. Enrichment
also cross-checks the OpenAlex title and authors against the arXiv sidecar.
Version-lag records add `openalex_title_lag`, `openalex_reported_title`, and
`openalex_identity_evidence`; wrong-work records add
`openalex_identity_conflict`, `openalex_reported_title`,
`openalex_reported_authors`, and `doi_resolution` evidence. For wrong-work
records, `doi_resolution` is an independent bounded DOI.org/DataCite
redirect-only HEAD check of the derived arXiv DOI. `matches_arxiv_id: true` is
recorded only when the resolved URL lands on `https://arxiv.org/abs/<arxiv-id>`
for the local versionless arXiv id. Redirects to other hosts or paths record
`status: redirect_mismatch`, DOI/network failures record `status: network_error`
or `status: not_found`, and all failed corroboration states keep
`matches_arxiv_id: false` without aborting enrichment for the source. It never
rewrites the downloaded artifact or its checksum, and a 404 records an
unresolved enrichment attempt instead of failing the whole command. Rerun
`source_inventory.py --report` and
`normalize_sources.py --all` after enrichment so manifest and normalized records
carry the new identity fields.

PDF downloads are limited to open-access PDF URLs surfaced by OpenAlex location
metadata:

```bash
python3 scripts/fetch_sources.py openalex download-pdf --work-id W260100001 --request-id req-1a2b3c4d5e --candidate-id cand-1a2b3c4d5e
python3 scripts/fetch_sources.py openalex download-pdf --work-id W260100001 --output raw/papers/custom-name.pdf
```

By default, PDFs write to `target_root/openalex-<work-id>.pdf`. Explicit
`--output` paths must stay under the configured `target_root`. The adapter
selects an HTTP(S) `pdf_url` only from `best_oa_location`, `primary_location`,
or `locations` entries marked `is_oa: true`; non-OA works and works without an
OA PDF URL fail before any file is written. OpenAlex 401/403 responses tell
agents to set `OPENALEX_API_KEY`, and 429 responses tell agents to back off,
reduce request volume, or use a key with a larger budget.

OpenAlex PDF sidecars record the OpenAlex work URL as `origin_url`, the actual
downloaded PDF as `downloaded_pdf_url`, `retrieved_by:
fetch_sources.py/openalex`, a file checksum, and the location `license` or
`license_id` when OpenAlex surfaces one. Only unambiguous versioned or explicit
license slugs are normalized to SPDX identifiers; ambiguous slugs are preserved
as provider slugs and left unresolved rather than guessed. If no location
license is present, the sidecar writes `license: null` plus a notes warning
rather than guessing. OpenAlex PDF and metadata-snapshot sidecars also record
`academic_provider: openalex`, venue, publication year, OA status, work id, DOI
when available, and a conservative `peer_review_status`. Article/venue signals
are labeled `publisher_indexed`; this is not treated as stronger proof of peer
review.

## Web Adapter

The `scripts/fetch_sources.py web get` adapter captures one explicit HTTPS URL
from a reviewed domain allow-list. It is intended for selected official web,
product, publisher, and legal candidates produced by `source_requests.py
plan-fetch`, or for a user-provided URL that has been explicitly reviewed.

Configuration is opt-in and separate from the global paper target:

```yaml
integrations:
  acquisition:
    enabled: true
    providers: [web]
    max_downloads_per_run: 10
    web:
      target_root: raw/web
      allowed_domains:
        - seg-social.example
      max_download_bytes: 10485760
```

Commands:

```bash
python3 scripts/fetch_sources.py --format json web get --url https://seg-social.example/fee --request-id req-1a2b3c4d5e
python3 scripts/fetch_sources.py --format json web get --url https://vendor.example/spec --candidate-id cand-123 --source-type web_page --publisher "Vendor Example" --evidence-area vendor_product_spec
python3 scripts/fetch_sources.py --format json web get --url https://official.example/guidance --publication-date 2026-05-06 --effective-date 2026-05-01 --validity-period 2026-01-01/2026-12-31 --valid-for-year 2026 --date-note "Date verified from retrieved bytes."
```

The adapter fails closed before promoting response bytes. It refuses non-HTTPS
or credential-bearing URLs, loopback/private/link-local/metadata addresses,
hosts outside `integrations.acquisition.web.allowed_domains`, more than five
redirects, and every redirect that escapes the same HTTPS, public-address, and
domain policy. DNS failure, an empty or malformed DNS answer, or any non-public
address in a host's complete answer set is a refusal. A resolver result is never
treated as safe merely because another address in the set is public.

TLS certificate verification is mandatory for automated acquisition; there is
no shipped option that creates an unverified TLS context. The transport also
requires a positive finite timeout, a successful 2xx response, an expected web
media type (`text/html`, `application/xhtml+xml`, `text/plain`, or XML), and a
body within `max_download_bytes`. Status and media type are checked before the
body is read, while both declared and streamed sizes are bounded. A rejected,
partial, error-page, wrong-media, or unverified response writes neither a raw
artifact nor a provenance sidecar. Provider diagnostics redact URL userinfo,
sensitive query values, and configured credential values. The arXiv,
OpenAlex, and GitHub adapters likewise declare route-specific metadata, PDF,
source-bundle, or archive media types; omitting that policy is a configuration
refusal rather than an accept-anything fallback.

Transient network failures and HTTP 429/5xx responses use at most three
attempts with deterministic exponential backoff capped at eight seconds, in
addition to each provider's minimum request interval. Authentication,
authorization, unsafe transport, invalid media, and hard byte/count caps fail
immediately. Downloads are promoted through a hidden transaction marker and
same-directory atomic writes. The marker is removed only after both payload and
provenance exist. If a process stops between those writes, the next retry moves
only the marker-backed output and any hidden partials into
`.acquisition-quarantine/`; unrelated user files are never moved. Consequently
an interrupted download cannot enter inventory as evidence, while retry creates
one final payload/sidecar pair rather than duplicate evidence.

Web sidecars write `origin_url`, `url`, `final_url`, `retrieved_at`,
`retrieved_by: fetch_sources.py/web`, `license: null`, checksum, byte count,
HTTP status, content type, redirect chain, verified TLS status, and optional
`request_id`, `candidate_id`, `source_type`, `publisher`, `jurisdiction`,
`terms_url`, `supported_evidence_areas`, `publication_date`, `effective_date`,
`validity_period`, `date_metadata.valid_for_year`, and `date_not_available`.
Pass `--publication-date`, `--effective-date`, `--validity-period`,
`--valid-for-year`, and `--date-note` only after verifying the date on retrieved
bytes or authoritative metadata; dates copied from a browsing briefing are not
enough. Generic web acquisition does not
infer licenses; publication workflows must treat `license: null` as explicit
uncertainty until a human records stronger terms/license status.

For standards registry captures, `web get` can merge a workspace-relative JSON
or YAML metadata file into `provenance.standards`:

```bash
python3 scripts/fetch_sources.py --format json web get \
  --url https://www.iso.org/standard/77442.html \
  --source-type standards_registry_entry \
  --evidence-area standards_registry_reference \
  --terms-url https://www.iso.org/open-data.html \
  --standards-metadata sources/discovery/iso-19131-standards.json
```

The metadata file should contain registry provider, standards body,
designation, title, edition or year, publication date, status, registry URL,
dataset license or terms note, and replacement-chain fields when known. This
captures registry metadata only. It is not permission to download or store full
standards text.

## Academic Citation Verification

`scripts/verify_citations.py` verifies academic citation identity from the same
workspace artifacts used by coverage policy evaluation. Default mode is
network-free and scans citation-bearing academic sources from the manifest and
normalized records. See [citation-verification.md](citation-verification.md)
for the focused verifier contract:

```bash
python3 scripts/verify_citations.py --format json
python3 scripts/verify_citations.py --format json --source-id paper:2601.00001v1
```

Network-free verification returns `verified` only when a source has valid local
arXiv, OpenAlex, or DOI metadata plus provider-backed acquisition provenance
from `fetch_sources.py/arxiv` or `fetch_sources.py/openalex`. Valid local
metadata without provider-backed provenance returns `skipped_no_live`, and
missing title or identifier metadata returns `insufficient_metadata`.

Live re-resolution is explicit and uses the same acquisition safety model as
fetch commands:

```bash
python3 scripts/verify_citations.py --format json --live --provider arxiv
python3 scripts/verify_citations.py --format json --live --provider openalex
```

Live mode refuses before network I/O unless `integrations.acquisition.enabled:
true` and the selected provider appears in `integrations.acquisition.providers`.
It re-resolves arXiv IDs, OpenAlex work IDs, and DOI values, then compares title,
year, and authors. For provider-backed sources, empty local authors are
`insufficient_metadata` rather than a vacuous pass; rerun `fetch_sources.py
openalex enrich`, `source_inventory.py --report`, and `normalize_sources.py
--all` to migrate older workspaces. Result entries include `title_source` when
normalized frontmatter records whether the compared title came from provider
metadata or PDF inference. Per-source results are `verified`, `mismatch`,
`not_found`, `skipped_no_live`, or `insufficient_metadata`; the report's
`overall_result` is `verified` only when every selected source verifies. Any
other result means publication-readiness tooling should treat the
citation-verification artifact as `no_ship` input.

The verifier never writes provider request URLs into its JSON report or fatal
error envelope, and it redacts API-key query parameters and known environment
secret values from provider error messages. OpenAlex keys still belong only in
`OPENALEX_API_KEY`; they must not be stored in `research.yml`, provenance
sidecars, logs, or exported artifacts.

## GitHub Provider

GitHub is a supported acquisition provider; allow-list it in
`integrations.acquisition.providers`. Repository candidate discovery is the
upstream, read-only step (`scripts/discover_sources.py github`). Bounded
acquisition captures an *explicitly selected* repository as evidence
through `scripts/fetch_sources.py github`:

```bash
python3 scripts/fetch_sources.py github repo-metadata --repo acme/rag-toolkit
python3 scripts/fetch_sources.py github release-metadata --repo acme/rag-toolkit
python3 scripts/fetch_sources.py github download-archive --repo acme/rag-toolkit --ref v1.2.0 --request-id req-1a2b3c4d5e
```

Every command requires an explicit repository, passed as `--repo owner/repo` or
`--url https://github.com/owner/repo`; `download-archive` also requires an
explicit `--ref` (branch, tag, or commit SHA). Acquisition never auto-selects a
repository from discovery search results — selection is always a human or agent
decision made before the fetch.

- `repo-metadata` writes a repository metadata snapshot (`full_name`, default
  branch, description, detected license, stars/forks, archived/fork flags) as a
  JSON file. It never reads repository file contents.
- `release-metadata` writes a release snapshot (tag, published time, and asset
  name/size/content-type/download URL for each asset). Assets themselves are not
  downloaded. Omitting `--tag` snapshots the latest release; a missing release or
  tag fails with `GITHUB_RELEASE_UNAVAILABLE`.
- `download-archive` downloads the source tarball for the selected ref, stores it
  as a `.tar.gz` file, and resolves the commit SHA when GitHub returns one. The
  archive is **never extracted**, so no repository code is ever written to disk as
  runnable files and nothing is executed. Inventory records the stored archive as
  a `code_archive` source.

Repository and release metadata snapshots are identity evidence only; they do not
satisfy source-code implementation facets unless a coverage facet explicitly
allows metadata-only artifacts through `accepted_artifact_kinds`.

Target root and size limit: GitHub downloads land under `raw/code` by default
(override with `integrations.acquisition.github.target_root`, which must stay
under `raw/`). Archives are bounded by
`integrations.acquisition.github.max_archive_bytes` (default 100 MiB). The
command refuses before downloading when the repository's reported size already
exceeds the limit, and again on the actual downloaded bytes, failing with
`GITHUB_ARCHIVE_TOO_LARGE` without writing a partial file.

Token handling: GitHub commands read `GITHUB_TOKEN` from the process environment
only. As with `OPENALEX_API_KEY`, the token must never be stored in
`research.yml` or any workspace file, and must never be written into provenance
sidecars, logs, or command output. Low-volume discovery may run unauthenticated
within GitHub's published unauthenticated rate limit; setting `GITHUB_TOKEN`
raises the limit and is the recommended mode for anything beyond a few queries.

Rate limits and terms: the GitHub REST API limits unauthenticated traffic to
roughly 60 requests per hour per source IP and authenticated traffic to roughly
5,000 requests per hour, and signals limits through 403/429 responses with
`Retry-After` and `X-RateLimit-*` headers. GitHub's terms forbid scraping and
bulk cloning, so adapters must use bounded queries and small result pages and
must never clone arbitrary repositories or fetch arbitrary file contents during
discovery.

License caveats: repository contents remain under each repository's own license.
GitHub's license detection is heuristic and is frequently absent, so adapters
must record the detected SPDX `license` key when the API surfaces one and
surface license uncertainty otherwise rather than assuming a permissive license.
Stars and forks are weak popularity metadata only, never trust or license proof.

## Provenance Requirements

Automated acquisition uses the same sidecar contract as external deliveries.
For each file or directory under `raw/`, write a sibling sidecar named with the
literal suffix `.provenance.yml`:

```text
raw/papers/2601.00001v1.pdf
raw/papers/2601.00001v1.pdf.provenance.yml
raw/papers/arxiv-2601.00001v1/
raw/papers/arxiv-2601.00001v1.provenance.yml
```

Required or expected fields:

```yaml
origin_url: https://arxiv.org/abs/2601.00001v1
downloaded_pdf_url: https://example.org/paper.pdf
license: null
retrieved_at: 2026-06-13T12:00:00Z
retrieved_by: fetch_sources.py/arxiv
checksum: "sha256:<64 lowercase hex chars>"
request_id: req-1a2b3c4d5e
notes: "License not inferable from provider metadata."
```

`origin_url`, `retrieved_at`, `retrieved_by`, and `license` status must be
visible to downstream agents. OpenAlex PDF downloads also record
`downloaded_pdf_url` so downstream inventory can distinguish the metadata work
record from the PDF host that served the bytes. GitHub acquisition adds
`repository_owner`, `repository_name`, `repository_full_name`,
`repository_artifact_kind`, `repository_ref` (the requested branch, tag, or SHA),
and `commit_sha` (the resolved commit, when GitHub returns one). Source archive
downloads also record `downloaded_archive_url` so the captured archive points
back to an exact repository state. `repository_artifact_kind` is one of
`source_archive`, `repository_metadata`, or `release_metadata`. File downloads
should include a checksum. Directory downloads should still write the sidecar;
inventory cannot verify a single checksum for a directory, so the sidecar must
describe the retrieved bundle.

Reusable output pages under `outputs.default_dir` must be especially explicit
about fetched-source licensing. With the default
`lint.validate_output_license_status: true`, `scripts/lint.py` reports LOW
`output_license_missing` when a page under `wiki/outputs/` cites a manifest
source whose provenance has `retrieved_by` but no concrete `license` value.
`license: null` remains useful as explicit acquisition uncertainty, but it still
triggers this output-page handoff warning until a concrete license is recorded
or the uncertainty is surfaced in the output workflow.

Reusable academic outputs must also surface publication context. With the
default `lint.validate_academic_publication_metadata: true`, `scripts/lint.py`
reports LOW `academic_metadata_missing` when a page under `wiki/outputs/` cites
an arXiv/OpenAlex-backed source whose provenance lacks `venue` or
`peer_review_status`.

After acquisition, run the normal local pipeline from the workspace root:

```bash
python3 scripts/source_inventory.py --report
python3 scripts/normalize_sources.py --all
```

When a download fulfills a source request, link the resulting manifest source:

```bash
python3 scripts/source_requests.py fulfill --request-id req-1a2b3c4d5e --source-id paper:2601.00001v1
```

Fetch agents should follow `skills/research-acquire.md` for the complete
request-backed loop: smoke validation, open-request listing, explicit provider
fetching, sidecar checks, inventory, normalization, request fulfillment,
blocked-question reopening, logging, and final workspace status.

## `fetch_sources.py` Registry Contract

`workspace-template/scripts/fetch_sources.py` exposes a module-level
`PROVIDER_REGISTRY` keyed by the documented provider IDs: `arxiv`, `openalex`,
and `github`. Tests compare the registry keys with this document so adding a
provider to code requires a matching provider-registry row and terms/license
note here. The same provider IDs back the `ACQUISITION_ALLOWED_PROVIDERS`
allow-lists in `init_research_workspace.py` and `smoke_validate_workspace.py`,
so initialization and smoke validation accept exactly the registered providers.

The registry should remain metadata-only: provider ID, terms URLs, supported
commands, and license-inference capability. Runtime behavior belongs in the
provider command implementations, which must still enforce the acquisition
config gate before any network operation.

## Related Documents

- [research-yml.md](research-yml.md) documents the `integrations.acquisition`
  configuration block and validation rules.
- [source-delivery.md](source-delivery.md) defines target roots, atomic
  delivery, provenance sidecars, source requests, and post-delivery commands.
- [source-discovery.md](source-discovery.md) defines the upstream
  candidate-discovery contract: ranked `source_candidate` records are proposed
  and reviewed before a candidate with `selected_for_request_id` is fetched
  through this acquisition contract. Candidate policy fields (`evidence_path`,
  `source_policy`, `freshness_policy`, and `identity_policy`) carry the shared
  evidence-policy context into acquisition planning.
- [orchestrator-handoff.md](orchestrator-handoff.md) places acquisition and
  delivery in the external chain lifecycle.
