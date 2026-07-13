# Source Delivery Contract

This document defines how files are delivered into a research workspace's `raw/` tree — by fetch agents, orchestrators, or humans — and how evidence gaps flow back out as structured source requests. A fetch-agent author should be able to implement a compliant delivery from this document alone.

Two artifacts close the acquisition loop:

- delivered files under `raw/` with provenance sidecars (input direction),
- `sources/source-requests.jsonl` (output direction, managed by `scripts/source_requests.py`).

The workspace itself never fetches anything unless acquisition is explicitly
enabled through the separate [acquisition.md](acquisition.md) contract. All
commands below are deterministic scripts.

## Delivery Targets

Deliver files only under the directories listed in `research.yml` `raw.source_roots`. Pick the root by evidence kind:

| Evidence | Target root (default config) | Notes |
|----------|------------------------------|-------|
| Papers, PDFs, reports | `raw/papers/` or `raw/pdf/` | Single PDFs pair automatically with LaTeX bundles by arXiv ID or filename slug. |
| arXiv source bundles | `raw/papers/arxiv-<id>/` | Directory names like `arxiv-2601.00001v1` trigger bundle detection; include `00README.json` when available. |
| URLs and link lists | `raw/links/*.txt`, `.url`, `.webloc` | Newline-separated HTTP(S) URLs; `#` comments allowed. |
| Datasets, tables | `raw/data/` | CSV/TSV files are normalized (columns, row counts, sample rows); Excel/Parquet/Feather stay classified-only. |
| Repositories, archives | `raw/code/` | Only treated as codebase evidence when `integrations.codebase_analysis.enabled` is true. |
| Web page snapshots, HTML papers | `raw/web/`, `raw/papers/` | `.html`/`.htm`/`.xhtml` files are normalized via stdlib extraction (no JS rendering, no asset fetching). |
| Other media | `raw/media/`, `raw/other/` | Classified by extension; unsupported types surface as `unknown` for review. |

Naming guidance:

- Keep names stable and content-derived (arXiv ID, DOI-derived slug, dataset name). Manifest IDs are derived from relative paths, so renaming a file later creates a new source ID.
- Raw files are immutable once delivered (`raw.immutable: true`). Deliver a newer version as a new file; never overwrite.
- Hidden files (dotfiles) are ignored by inventory.
- Symlinked sources are refused, not followed. Inventory excludes any symlink under `raw/` (whether it points inside or outside the workspace), and any path that resolves outside the workspace, recording a `refusing symlink in raw root: <path>` (or `refusing path that resolves outside workspace: <path>`) warning. Deliver real files, never links.

## Delivery Atomicity

Inventory may run while a delivery is in progress. Make each delivered artifact appear atomically:

1. Write the file (or assemble the directory) under a temporary name in the same target root, prefixed with a dot so inventory ignores it (for example `.incoming-2601.00001v1.pdf`).
2. Write the provenance sidecar the same way.
3. Rename the sidecar and then the artifact to their final names (`rename` is atomic on POSIX within one filesystem).

## Provenance Sidecars

Every automated delivery must place a provenance sidecar next to the delivered file or directory:

```text
raw/pdf/2601.00001v1.pdf
raw/pdf/2601.00001v1.pdf.provenance.yml
raw/papers/arxiv-2601.00001v1/
raw/papers/arxiv-2601.00001v1.provenance.yml
raw/web/<name>.html
raw/web/<name>.html.provenance.yml
```

The sidecar name is the delivered path plus the literal suffix `.provenance.yml`.
For HTML web deliveries, the canonical pair is exactly `raw/web/<name>.html`
plus `raw/web/<name>.html.provenance.yml`; `raw/web/<name>.provenance.yml` is a legacy mismatch because it points at `raw/web/<name>`, not the delivered HTML
file. Inventory reports that legacy sidecar with the expected canonical path and
also reports raw web HTML files that lack the canonical sidecar.

Sidecar format:

```yaml
url: https://www.seg-social.es/...                 # official page URL; aliases to origin_url
origin_url: https://arxiv.org/abs/2601.00001v1   # where the artifact came from
license: CC-BY-4.0                               # SPDX license id, or null when unknown
retrieved_at: 2026-06-10T12:00:00Z               # ISO 8601 retrieval time
retrieved_by: fetch-agent/arxiv                  # agent identifier (marks automated delivery)
sha256: "sha256:<64 hex chars>"                  # checksum alias accepted for manual web delivery
effective_date: 2026-01-01                       # optional currentness date for legal/product sources
publication_date: 2026-01-15T00:00:00Z           # optional source/page publication date
validity_period: 2026-01-01/2026-12-31           # optional ISO interval; open end allowed as 2026-01-01/
date_metadata:                                   # optional structured date/currentness metadata
  effective_date: "2026-01-01"
  valid_for_year: 2026
evidence_usability_override:                     # optional audited source-usability false-positive override
  usable: true
  reviewed_by: verifier-agent
  reviewed_at: "2026-07-04T12:00:00Z"
  reason: "Rich official guidance capture; JavaScript warning is boilerplate and quoted text verified from retrieved bytes."
source_type: official_web                        # optional source class for official web captures
jurisdiction: ES                                 # optional jurisdiction/profile id or country code
publisher: Seguridad Social                      # optional official publisher label
supported_evidence_areas:                        # optional stable evidence-area tags
  - social_security_contributions
  - current_legal_figure
curation_notes: Official source reviewed.        # optional curation note for manual official delivery
date_not_available: "No date shown on page"      # optional explanatory note, not a boolean
source_status: available                         # optional: available, error_page, not_found, unavailable
delivery_failure_code: javascript_required       # optional structured failure code, see below
delivery_failure_detail: Static fetch returned a JavaScript shell with no usable page body
delivery_failure_remediation: Capture with an approved browser/manual path or request an accessible export
checksum: "sha256:<64 hex chars>"                # checksum of the delivered file
request_id: req-1a2b3c4d5e                       # optional: source request being fulfilled
candidate_id: cand-official-product              # optional: selected discovery candidate being delivered
terms_url: https://example.org/terms             # optional license/terms page for web captures
terms_note: "Reuse terms reviewed on source page" # optional short terms/reuse note
standards:                                      # optional standards-registry metadata
  registry_provider: iso-open-data
  standards_body: ISO
  designation: "ISO 19131:2022"
  title: Geographic information - Data product specifications
  edition: 2
  publication_date: "2022-11-01"
  status: published
  registry_url: https://www.iso.org/standard/77442.html
  dataset_license: ODC-BY-1.0
notes: optional free text
```

All fields are optional strings (validated when present), except `license` may
be explicit YAML `null` to record known uncertainty, `publication_year` may
be an integer or four-digit string, `date_metadata` is a scalar mapping, and
`supported_evidence_areas` is a list of non-empty strings. `evidence_usability_override`
must be a mapping with `usable: true`, non-empty `reviewed_by`, non-empty
`reviewed_at`, and non-empty `reason`. It is an audited escape hatch for
deterministic source-usability false positives after reviewer inspection; it
cannot override delivery failures such as HTTP errors, missing files, checksum
mismatches, TLS failures, `source_status: unavailable`, or any
`delivery_failure_code`. `retrieved_at` must
be ISO 8601; `checksum` and `sha256` must match `sha256:<64 lowercase hex chars>`
(a bare 64-character SHA-256 value is normalized to that form); and a non-null
`license` must be one of the SPDX identifiers recognized by the starter
inventory tool. Manual official web captures should include `url`, `retrieved_at`,
`sha256`, `source_type`, `jurisdiction`, `publisher`, `date_metadata`,
`supported_evidence_areas`, and `curation_notes`. Automated web captures should
include `origin_url`, `retrieved_at`, `retrieved_by`, `checksum`, `license` or
`terms_url`/`terms_note`, and `notes`; when the file was delivered from a
selected discovery candidate, copy the candidate id into `candidate_id`.
Standards registry captures should include a `standards` mapping. Inventory
preserves valid mappings under `provenance.standards`; a malformed non-mapping
warns and marks the source `review_required` instead of crashing.

Provider-backed delivery is fail closed before this sidecar contract begins.
Automated acquisition requires verified TLS, successful DNS resolution whose
entire answer set is public, a policy-compliant HTTPS redirect chain, a 2xx
response, the provider's expected media type, and bounded response bytes within
a positive finite timeout. Failures such as `ACQUISITION_DNS_FAILED`,
`ACQUISITION_REDIRECT_UNSAFE`, `ACQUISITION_REDIRECT_LIMIT`,
`ACQUISITION_TLS_FAILED`, `ACQUISITION_STATUS_UNEXPECTED`,
`ACQUISITION_MIME_UNEXPECTED`, and `ACQUISITION_CONTENT_TOO_LARGE` leave no raw
file or sidecar to inventory. Do not turn one of those refusals into evidence by
manually copying the rejected response; a separately reviewed manual delivery
needs its own provenance and source-status decision.

Automated downloads may also record `downloaded_pdf_url`
(OpenAlex) or GitHub repository fields:
`downloaded_archive_url`,
`repository_owner`, `repository_name`, `repository_full_name`,
`repository_artifact_kind`, `repository_ref`, and `commit_sha`.
`repository_artifact_kind` is one of `source_archive`, `repository_metadata`, or
`release_metadata`.

Academic acquisition may also record `academic_provider`,
`academic_source_type`, `venue`, `publication_year`, `oa_status`,
`peer_review_status`, `arxiv_id`, `openalex_work_id`, and `doi`. arXiv
downloads record `peer_review_status: preprint` and keep `license: null` when
the adapter cannot determine a per-paper license. OpenAlex venue/article signals
are recorded as `peer_review_status: publisher_indexed`; this is not a stronger
peer-review claim.

Standards metadata is additive and terms-aware. It can support local policies
such as `official_standards_registry`, `current_standard_reference`, and
`standard_designation_matches_registry`, but it does not grant rights to store
full standards text.

Legal, regulatory, tax, and product evidence can record currentness metadata in
the same sidecar. `effective_date` and `publication_date` should be ISO dates or
timestamps. `validity_period` uses ISO interval text such as
`2026-01-01/2026-12-31`; omit the end date for an open-ended current period.
`date_not_available` must be a short human note explaining why no date appears
on the source page. `source_status` should be `available` for usable pages, or
`error_page`, `not_found`, or `unavailable` when the delivered artifact is an
official error/unavailable page that must not satisfy currentness. Unknown fields
are ignored with a warning.

Official web and product delivery failures use one domain-neutral failure
taxonomy. The same code set applies to government pages, standards bodies,
vendor documentation, product specifications, publisher pages, and other
official web evidence:

| Code | Meaning | Default remediation guidance |
|------|---------|------------------------------|
| `tls_verification_failed` | TLS or certificate-chain validation prevented a trusted capture. | Retry with a trusted TLS chain or deliver a reviewer-approved snapshot with provenance. |
| `http_error` | The upstream host returned a non-success HTTP status other than a clean not-found case. | Retry later, verify the URL, or record the upstream HTTP status in `delivery_failure_detail`. |
| `javascript_required` | Static capture produced only a JavaScript shell or otherwise requires browser rendering. | Use an approved browser/manual capture path or request an accessible static/export version. |
| `official_error_page` | The official host responded with its own maintenance, unavailable, or generic error page. | Find the canonical current page or record the outage as blocked source acquisition. |
| `not_found` | The URL returned a 404/not-found style response or equivalent official missing-page state. | Verify whether the source moved, was superseded, or should be replaced by a newer official URL. |
| `content_too_sparse` | The captured content is too thin to support claims, even if the URL resolved. | Acquire a fuller representation before using the source as evidence. |
| `license_or_terms_unknown` | License, reuse terms, or capture permission could not be determined. | Review source terms or license before reusing the captured content. |
| `robots_or_terms_blocked` | Robots, terms, or provider policy blocks automated fetching/reuse. | Do not fetch automatically; use a permitted manual review path or alternate source. |
| `manual_review_required` | The source needs an explicit reviewer decision before it can be delivered or used. | Keep the source request open until a reviewer records a concrete acquisition decision. |

When a delivery records one of these states, put the machine value in
`delivery_failure_code`, put fetch-specific evidence such as HTTP status,
browser requirement, or terms page in `delivery_failure_detail`, and copy or
specialize the remediation in `delivery_failure_remediation`. Source requests
remain schema-compatible: put remediation guidance in the request `rationale`
instead of adding request-only failure fields.

Failure-aware inventory and normalization are active for this vocabulary.
Inventory keeps failed captures auditable in `sources/manifest.jsonl`, but marks
them with `evidence_usable: false` and `unusable_evidence_reasons` so required
coverage facets cannot pass until the source is redelivered or replaced.

Behavior in `source_inventory.py`:

- Sidecars are never inventoried as sources themselves.
- Valid fields are merged into the matching manifest record under a `provenance` object, together with `sidecar_path`. The match is by delivered path: a record claims the sidecar sitting next to its LaTeX bundle root, its raw file, or its paired PDF (primary path first; additional matching sidecars are reported, not merged).
- A malformed sidecar (unparseable YAML, wrong field types) degrades to a parse warning in the inventory report; the run never fails because of it.
- Invalid `source_status` or `delivery_failure_code` values are dropped with a
  warning, while valid `delivery_failure_detail` and
  `delivery_failure_remediation` strings are preserved.
- Valid `delivery_failure_code` values and non-available `source_status` values
  (`error_page`, `not_found`, `unavailable`) mark the manifest record as
  unusable evidence without removing it from the inventory.
- A non-null `license` that is not in the in-repo SPDX allowlist is dropped,
  marks the record `review_required`, and raises a warning. `license: null` is
  preserved as an explicit unknown.
- When `checksum` is present and the target is a file, inventory recomputes the hash. The result is recorded as `provenance.checksum_verified`; a mismatch marks the record `review_required` and raises a prominent warning in the report. Directory targets cannot be checksum-verified and are warned about.
- High-trust deployments can opt into fail-closed inventory modes:
  `--reject-mismatch` excludes records whose sidecar checksum is present but not
  verified, and `--require-checksum` excludes records without
  `provenance.checksum_verified: true`. These modes filter records before the
  manifest is written and exit non-zero when they refuse sources.
- Provenance and evidence-usability fields flow into normalized-record
  frontmatter on the next normalization, so exported citations carry
  `origin_url`, `license`, academic venue/status metadata, and unusable-evidence
  reasons when present (see `export_answers.py`).
- Sidecar bytes count toward `raw_fingerprint` for paper and PDF records: correcting a sidecar re-triggers normalization for that source, keeping normalized provenance current.

Deliveries without sidecars (typically human drag-and-drop) behave exactly as before; provenance is additive.

## Post-Delivery Command Sequence

From the workspace root, after each delivery batch:

```bash
python3 scripts/source_inventory.py --report
python3 scripts/normalize_sources.py --all
```

When the delivery fulfills a source request, link it and unblock the affected questions:

```bash
python3 scripts/source_requests.py fulfill --request-id req-1a2b3c4d5e --source-id paper:2601.00001v1
```

## Idempotency Guarantees

- Re-running `source_inventory.py` after a partial delivery only adds or refreshes affected records. Existing record IDs are stable (path-derived), `detected_at` is preserved across runs, and no prior records are lost when new files arrive.
- Re-delivering identical bytes changes nothing: `raw_fingerprint` is content-derived, so normalization skips unchanged sources.
- Changed bytes under an existing path (discouraged — raw is immutable) change `raw_fingerprint`, and the next normalization run regenerates that record.
- `source_requests.py add` deduplicates against open requests by kind plus normalized query text; re-submitting is a reported no-op. `fulfill` with the same source ID twice is a no-op.

## Source Requests (Workspace → Fetch Agents)

Evidence gaps flow out through `sources/source-requests.jsonl` (path configurable via `sources.source_requests_path`). Each line is one request record, schema version 1.0:

```json
{
  "schema_version": "1.0",
  "request_id": "req-1a2b3c4d5e",
  "kind": "paper",
  "query_or_identifier": "arXiv:2601.00001",
  "rationale": "Blocks the benchmark question.",
  "priority": "high",
  "question_slugs": ["which-benchmarks"],
  "status": "open",
  "created_at": "2026-06-10T12:00:00Z",
  "updated_at": "2026-06-10T12:00:00Z",
  "source_id": null
}
```

Field notes:

- `kind`: one of `paper`, `dataset`, `web`, `code`, `other`.
- `query_or_identifier`: what to fetch — an arXiv ID, DOI, URL, or search query.
- `question_slugs`: question pages this request unblocks; validated against the questions directory at `add` time, so a blocked question is discoverable from the request record.
- `status`: `open` or `fulfilled`. `fulfill` sets `source_id` to the manifest record that satisfied the request (validated against the manifest).

Commands (workspace root):

```bash
python3 scripts/source_requests.py add --kind paper --query-or-identifier "arXiv:2601.00001" \
  --rationale "Blocks the benchmark question." --priority high --question-slug which-benchmarks
python3 scripts/source_requests.py list --status open --format json
python3 scripts/source_requests.py plan-fetch --request-id req-1a2b3c4d5e --format json
python3 scripts/source_requests.py fulfill --request-id req-1a2b3c4d5e --source-id paper:2601.00001v1
```

`plan-fetch` is read-only: it turns a request into candidate provider commands and records `network_io_executed: false`. A fetch agent's loop is: `list --status open --format json` → `plan-fetch --request-id ... --format json` → deliver files with sidecars (set `request_id` in the sidecar) → run inventory and normalization → `fulfill` each delivered request. Use `skills/research-acquire.md` for the optional provider-backed version of this loop, including disabled-acquisition refusal, sidecar verification, blocked-question reopening, and final status reporting. `add` and `fulfill` append one `source-request` entry to `log.md`; `list` and `plan-fetch` do not mutate the request artifact or `log.md`.

### Selected discovery candidates

When a request has discovery candidates that were explicitly selected for it
(`discover_sources.py candidates select --candidate-id ... --request-id ...`, via
the `skills/research-discover.md` playbook; see
[source-discovery.md](source-discovery.md)), `plan-fetch` adds a `candidate_routes`
array — one explicit acquisition route per selected candidate, keyed by candidate
type. Selections are authoritative, so when present they upgrade an
`unsupported`/`ambiguous` request to `plan_status: ready`. Each route reuses real
provider syntax; it never invents commands:

| Candidate | Route | Suggested command / target |
|-----------|-------|----------------------------|
| arXiv id (paper URL or `paper.arxiv_id`) | `arxiv download-source` / `search-by-id` | `fetch_sources.py arxiv ...` |
| OpenAlex OA paper (`paper.provider_ids.openalex` plus `paper.pdf_url`) | `openalex download-pdf` | `fetch_sources.py openalex download-pdf --work-id ...` |
| OpenAlex metadata-only or non-OA paper | `openalex get` | `fetch_sources.py openalex get --id-or-doi ... --output raw/papers/openalex-...-metadata.json --request-id ...` plus non-OA/manual-delivery warning |
| Uncertain paper title | `openalex resolve` | `fetch_sources.py openalex resolve --entity works --query ... --max-results 5` plus resolution warning |
| DOI (paper URL or `paper.doi`) | `openalex get-by-doi` | `fetch_sources.py openalex get --id-or-doi ...` |
| GitHub repo (`code_repository` or a github.com URL) | `github repo-metadata` | `fetch_sources.py github repo-metadata --url ... --request-id ...` |
| Official legal URL (`official_legal`) | `manual manual-delivery` | deliver the URL into `raw/links/` (or a snapshot into `raw/web/`) with a provenance sidecar |
| Web/publisher/dataset/supplemental | `manual manual-delivery` | deliver into the matching `raw/` root with a provenance sidecar |

Provider-backed candidate routes (`provider_backed: true`) carry `command`/
`command_argv` and an `allowed_by_config` flag (true only when the provider is
allow-listed under `integrations.acquisition.providers`); manual routes carry a
`manual_delivery` object (`target_root`, `url`, delivery `note`) instead. Because
official legal sources have no automated scraper, they always route to manual
delivery — preserving the official-source-first reasoning recorded during
discovery. Academic paper routes also copy the selected candidate's `paper`,
`candidate_network_io_executed`, and `provider_budget` metadata into the route,
and warnings call out unknown licenses, non-open-access records, and uncertain
provider resolution. `plan-fetch` still runs no network I/O and never mutates the
candidate store, coverage manifests, request artifact, or `log.md`.

When coverage manifests under `sources.coverage_dir` contain facets whose
`blocking_request_ids` include the request, `plan-fetch` sets
`policy_source: coverage_manifest` and returns `policy_facets` with each linked
facet's `evidence_path`, policy fields, and mapped `policy_min_trust_tier`.
Every `candidate_routes[]` entry also carries matching `policy_facets`,
`policy_alignment`, and `policy_min_trust_tier`. If no linked facet exists, the
report keeps the legacy `request_min_trust_tier` behavior using `min_trust_tier`
on the request (default `secondary_reputable`).

`plan-fetch` warns when a selected candidate's `trust_tier` is below the linked
facet's source-policy threshold, when a candidate's `evidence_path` matches no
linked facet, and when a selected candidate was discovery-ranked
`recommended_action: reject` — so a reviewer is alerted before acquiring a
low-trust, policy-mismatched, or already-rejected source.

Concurrency: the artifact is single-writer. `add` and `fulfill` serialize through the shared workspace lock helper while preserving complete-line append and atomic write-temp-rename behavior. Concurrent readers always see complete lines. Run one writer at a time; orchestrators should serialize mutations the same way they serialize question intake.

## Status And Lint Visibility

- `scripts/workspace_status.py` reports `sources.requests_open` and `sources.requests_open_ids`; a clean `blocked_on_sources` verdict also requires each blocked question to carry `blocking_request_ids` linked to open requests, and the verdict reasons name those linked request IDs. A blocked question without a linked open request is `attention_required`.
- `scripts/workspace_status.py` reports `sources.curation` counts for automated web records, cited automated web records, and missing terms/license, notes, origin URL, checksum, or candidate id metadata.
- `scripts/lint.py` (config-gated, default on) reports: automated non-web deliveries missing `license` provenance (MEDIUM, `validate_provenance`); automated web deliveries missing `license`, `terms_url`, or `terms_note` (LOW, `validate_curation_metadata`); cited automated web deliveries missing `notes` (MEDIUM) or `origin_url`/verified `checksum` (HIGH); selected-candidate web deliveries missing `candidate_id` (LOW); blocked questions with no linked source request (LOW), and fulfilled requests pointing at missing manifest sources (MEDIUM, both under `validate_source_requests`).

## Retained Mixed-Source Publication Matrix

The deterministic regression contract lives in
`tests/fixtures/publication-source-matrix/matrix.yml` and is executed as one
case family by `tests/test_publication_source_matrix.py`. It combines Markdown,
HTML, PDF, LaTeX, a local code repository, JSON, CSV, URL, and opaque binary
inputs with malformed sidecars, duplicate basenames, invalid UTF-8, nested
frontmatter, formulas, a huge line, active-content probes, and ambiguous
PDF/LaTeX pairing.

The matrix deliberately preserves the difference between inventory support and
normalization support:

- HTML, text CSV/TSV, LaTeX bundles, PDF records, URL records, and enabled
  codebase records have offline normalization paths.
- Bare Markdown, standalone JSON, and opaque binary records remain named in the
  manifest but are reported as unsupported by normalization. They do not gain
  normalized output or evidence standing merely because inventory found them.
- A URL normalizes to an unfetched stub. A local repository without a recorded
  adapter artifact normalizes to a codebase stub; source instructions cannot
  cause the adapter, hooks, or repository code to run.

The retained sequence covers inventory dry-run/write, normalization
dry-run/selected/incremental/all/force, raw-fingerprint modification updates,
rename and deletion orphans, normalizer-version inspection and forced repair,
partial PDF extraction and recovery, and an interruption between temp-file
write and atomic replacement followed by retry. Extraction-loss assertions keep
both preserved markers and intentional losses visible: script bodies are
removed from HTML, invalid UTF-8 becomes a replacement character, unsupported
formats remain unnormalized, and scanned-PDF output stays `partial` until a
later extraction succeeds. Raw hashes are checked around every pipeline action.

GPTQ, AWQ, KIVI, and TurboQuant identity cases are wholly synthetic replay
records. They retain provider title/authors beside the independently parsed PDF
title and require exact, normalized, or visible-mismatch outcomes as declared
by the fixture. They prove the offline comparison seam only. Live arXiv/OpenAlex
identity re-resolution is outside this offline matrix and must be recorded
separately when performed; the local fixture must never be cited as live
provider evidence.

The harness refuses unexpected socket, URL-open, or subprocess calls. PDF text
is supplied by a deterministic parser stub, and the only allowed mutations are
generated manifest/normalized/temp/log state. Any live-provider observation
requires its own provenance and cannot be inferred from this replay.

## Related Documents

- [acquisition.md](acquisition.md) — optional provider registry, safety model,
  and provenance requirements for future fetch commands.
- [source-discovery.md](source-discovery.md) — the candidate-discovery contract
  that proposes ranked `source_candidate` records before a selected candidate is
  delivered into `raw/` through this contract.
- [../skills/research-acquire.md](../skills/research-acquire.md) — fetch-agent
  workflow for request-backed provider acquisition.
- [orchestrator-handoff.md](orchestrator-handoff.md) — the end-to-end machine contract this delivery step belongs to.
- [source-manifest.md](source-manifest.md) — manifest record fields, including the `provenance` object.
- [normalized-source-format.md](normalized-source-format.md) — normalized record frontmatter, including propagated provenance.
