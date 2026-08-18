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
| URLs and link lists | `raw/links/*.txt`, `.url`, `.webloc` | Newline-separated HTTP(S) URLs; `#` comments allowed. A `.txt` list is only expanded into one source per URL when it sits under a link root; `.url`/`.webloc` are expanded anywhere. A URL list delivered elsewhere is inventoried as a single `link` record whose URLs never become sources, and the inventory report says to move it. |
| Datasets, tables | `raw/data/` | CSV/TSV files are normalized (columns, row counts, sample rows); Excel/Parquet/Feather stay classified-only. |
| Structured payloads | `raw/data/` | `.json`/`.jsonl` are classified as `structured_data`. This package does not extract them, so they stay classified-only unless the workspace configures a normalization adapter for the kind, or an external normalizer writes the record directly. |
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
request_id: req-1a2b3c4d5e                       # optional; required for delegated acquisition (see below)
candidate_id: cand-official-product              # optional: selected discovery candidate being delivered
scope:                                           # optional: what this delivery answers, matched against the request's scope
  facet_id: supplier_quote
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
be an integer or four-digit string, `date_metadata` is a scalar mapping,
`supported_evidence_areas` is a list of non-empty strings, and `scope` is a
mapping of string keys to scalar values. Each `scope` key must match
`^[a-z0-9_][a-z0-9._-]*$`; values are opaque and are compared as text. A
non-string scalar is accepted and coerced — an unquoted `2026` matches a
request scope of `"2026"` — but booleans, sequences, mappings, and empty
values are dropped from the parsed scope rather than failing the sidecar, so
quote any value whose YAML type is not obviously a string. The workspace
stores and matches these keys — it never interprets them. `facet_id` is the
convention `coverage_manifest.py` tooling uses to link a request to the facet
it unblocks, not a schema this package knows; a pack or host may use any keys
that make sense for its own pairing. `evidence_usability_override`
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

Under `orchestration.acquisition: delegated`, `request_id` stops being optional
for anything delivered to satisfy a source request. It is the only link between a
delivered artifact and the request it fulfils — there is no candidate id to fall
back on — and inventory merges it into the manifest record only from a sidecar
sitting beside the delivery. That is what makes "this source carries a provenance
sidecar" a checkable claim: an acquisition action fulfilling a request whose
manifest record has no matching `provenance.request_id` is refused with
`ORCHESTRATION_POSTCONDITION_FAILED`. `candidate_id` stays absent in that mode.

The stamp is made at delivery time and only at delivery time. A source already in
the manifest whose sidecar does not already name the request cannot be made to
satisfy it by editing that sidecar now: `raw/` is immutable, and adding a
`request_id` turns a delivery-time record into an after-the-fact assertion by the
party whose claims this contract exists to check. The orchestration baseline of
reusable evidence is fingerprinted when the work order is issued, so a
`request_id` that appears after that is a change to protected evidence — refused
with `ORCHESTRATION_POSTCONDITION_FAILED`, not accepted as a correlation. When
the bytes are already on disk and the order's reuse baseline (below) does not
admit them, deliver them again as a new source under the target its kind owns,
with its own sidecar, or record an attempt failure. Most manifest IDs are derived
from the delivered relative path (see "Naming guidance" above), so a distinct
capture at a distinct path is a distinct source carrying its own provenance —
that is the identity model, not a workaround. The new sidecar records the
retrieval that actually happened — the `origin_url` and `retrieved_at` of the
fetch that produced these bytes — plus the `request_id` this delivery answers. It
is a second delivery of one retrieval, not a claim of a second retrieval.

Two kinds do not take their ID from the path, and a copy of one lands on the
record already in the manifest rather than beside it: an arXiv bundle directory
named exactly `arxiv-<id>` takes its ID from that arXiv ID, and each URL expanded
from a link file takes its ID from the URL. Re-deliver a bundle under a directory
name that is not the `arxiv-<id>` form, and re-deliver an already-inventoried URL
as an actual capture under `raw/web/` rather than as a second link list. Where no
distinct ID is reachable, the request cannot be satisfied by re-delivery at all:
record an attempt failure.

Reuse without re-delivery is available too, for a source the controller admitted
to the order's reuse baseline when it issued the order — one whose sidecar
already named this request then, or one that the same scope check `fulfill` runs
would accept for this request. Its manifest record must stay byte-unchanged. One
already normalized then must keep that exact normalized output as well; one
inventoried but never normalized — the earlier order that delivered evidence and
did not complete — is normalized inside the action instead, and the fulfilment is
refused without that output. The baseline is computed by the controller from
evidence that predates the order, so nothing an acquirer writes during the action
can add to it. None of this is in tension with the idempotency guarantee below
that re-delivering identical bytes changes nothing: that guarantee is about the
*normalizer*, which skips a source whose `raw_fingerprint` is unchanged, while
the orchestration postcondition asks the different question of whether this
action produced a manifest record correlated to the request it claims to fulfil.
Re-delivering the same bytes at a new path costs one more copy of them, not a
violated guarantee.

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

A domain pack may require evidence to come from a named provider, through a policy
rule using `one_of_provenance: {providers: [...]}` (see
[evidence-policies.md](evidence-policies.md)). Provider membership is decided
against exactly three sidecar fields — `provider_registration.id` (see
[provider-registration.md](provider-registration.md)), `academic_provider`, and
`standards.registry_provider` — matched exactly but for case, since registry
metadata spells the same provider `ISO` or `iso` depending on who wrote the
sidecar. Nothing else is folded: a fullwidth or en-dash lookalike of an allowed
id is a different id. A host delivering
through this contract that wants its sources to satisfy such a rule stamps
`provider_registration.id` with its own connector id. `retrieved_by` is never
consulted for provider membership: it identifies the *agent* that performed the
fetch and carries path-shaped values such as `fetch_sources.py/arxiv`, so matching
on it would match the fetcher rather than where the evidence came from. The
`domains` variant of the same primitive needs no extra fields — it matches the
host of `origin_url`, which every delivery already records — and is the
zero-effort path for a host that would rather not stamp registration blocks.

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

### Acquisition-attempt failures

The table above describes a delivery that happened but cannot be trusted. An acquisition
attempt that produced **nothing at all** has no artifact and therefore no sidecar to carry
a code, so those outcomes are recorded against the source request instead of against a
file. The attempt vocabulary is the delivery vocabulary plus three connector-level codes:

| Code | Meaning | Default remediation guidance |
|------|---------|------------------------------|
| `provider_throttled` | The connector was rate-limited before it could retrieve the source. | Retry after the connector's declared rate window. |
| `not_authorized` | The acquirer's credentials or egress policy refuse this source. | Fix authorization host-side or record the decision and replace the request. |
| `no_result` | The connector completed but returned nothing usable for this request. | Refine the request or try another source. |

An attempt reports the most specific code that fits: a plain HTTP 500 is `http_error`, not
`no_result`. These three are **not** valid `delivery_failure_code` values — inventory
rejects them in a sidecar with a warning, because a sidecar sits beside an artifact and
these codes mean no artifact exists.

`not_authorized`, `robots_or_terms_blocked`, `license_or_terms_unknown`, and
`manual_review_required` are **not retryable**: each reports a standing decision rather
than a transient condition, so trying again within the same session cannot change the
answer. Every other code is retryable, bounded by the per-request attempt budget.

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

Delivering a source is not enough to make it usable evidence. A source is only quotable
and only reopens a blocked question once it has a **normalized record**: `reopen`
refuses with `SOURCE_NOT_NORMALIZED`, and `verify_quotes.py` checks grounding against
the normalized body, not the raw bytes.

For source kinds `normalize_sources.py` reads, the command above produces that record.
For kinds it does not read — structured API payloads, instrument output — an external
normalizer may write the record instead, and it counts as evidence on the same terms
once it conforms to the published record contract. Check it before relying on it:

```bash
python3 scripts/normalize_verify.py --source-id <source-id> --format json
```

See [normalized-source-format.md](normalized-source-format.md) for the contract, which
sources an external tool may write records for, and the violation codes verification
reports.

## Idempotency Guarantees

- Re-running `source_inventory.py` after a partial delivery only adds or refreshes affected records. Existing record IDs are stable (path-derived), `detected_at` is preserved across runs, and no prior records are lost when new files arrive.
- Re-delivering identical bytes changes nothing: `raw_fingerprint` is content-derived, so normalization skips unchanged sources. This is a normalizer property, not an orchestration postcondition: under `orchestration.acquisition: delegated`, re-running inventory over a source already in the manifest does not correlate it to a source request and cannot be made to — see "Provenance Sidecars" above.
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
  "scope": {"facet_id": "supplier_quote"},
  "status": "open",
  "created_at": "2026-06-10T12:00:00Z",
  "updated_at": "2026-06-10T12:00:00Z",
  "source_id": null
}
```

Field notes:

- `kind`: a built-in (`paper`, `dataset`, `web`, `code`, `structured_data`, `other`) or a domain-pack-declared kind namespaced `pack:<pack-name>/<kind-id>` (see `domain_pack.request_kinds` in [research-yml.md](research-yml.md)). `structured_data` is the built-in bucket for non-documentary payloads — API responses, sensor series, instrument output.
- `query_or_identifier`: what to fetch — an arXiv ID, DOI, URL, or search query.
- `question_slugs`: question pages this request unblocks; validated against the questions directory at `add` time, so a blocked question is discoverable from the request record.
- `scope`: optional mapping (`add --scope key=value`, repeatable) stating what would satisfy this request — see "Scope Matching" below. Omitted entirely when no `--scope` was given, so scope-less requests stay byte-identical to records written before this field existed.
- `status`: `open` or `fulfilled`. `fulfill` sets `source_id` to the manifest record that satisfied the request (validated against the manifest).

Commands (workspace root):

```bash
python3 scripts/source_requests.py add --kind paper --query-or-identifier "arXiv:2601.00001" \
  --rationale "Blocks the benchmark question." --priority high --question-slug which-benchmarks
python3 scripts/source_requests.py list --status open --format json
python3 scripts/source_requests.py plan-fetch --request-id req-1a2b3c4d5e --format json
python3 scripts/source_requests.py plan-fetch --request-id req-1a2b3c4d5e \
  --candidate-id cand-1a2b3c4d5e --format json
python3 scripts/source_requests.py fulfill --request-id req-1a2b3c4d5e --source-id paper:2601.00001v1
```

### Scope Matching

When a request carries `scope`, fulfilment stops being positional convention
and starts comparing declared scope against the delivered source's sidecar
`scope` (read from the manifest record's merged `provenance.scope`; see
"Provenance Sidecars" above). Matching is layered:

1. **Contradiction check — always on, no flag.** For every key present on
   *both* the request's scope and the source's provenance scope, the values
   must agree; disagreement refuses the fulfil with `REQUEST_SCOPE_MISMATCH`,
   naming each conflicting key and both values, and leaves the request
   untouched. This check cannot fire unless both sides declared scope, so a
   workspace with no scoped requests and no scoped deliveries never sees it —
   existing deliveries are unaffected by construction.
2. **`fulfill --match-scope key=value`** (repeatable): the caller asserts
   scope keys at the command line. Each pair is checked against the
   *request's* own scope (an assertion that contradicts what the request
   already declared is refused) and against the source's metadata, the same
   way declared scope is checked.
3. **`fulfill --require-scope`** (opt-in strict mode): upgrades absence to
   refusal. Every key the request's scope declares **and** every key
   `--match-scope` asserts must be present *and* equal in the source's
   provenance scope, or the fulfil is refused with `REQUEST_SCOPE_MISSING`,
   naming each absent key. Asserted keys are covered because layer 2 can only
   catch a delivery that *disagrees* with an assertion: without this, an
   explicit `--match-scope` claim against an unstamped source would be
   accepted with nothing verified, and no flag could ask otherwise.

A key present on only one side is not a contradiction under layers 1–2 — the
package's own language for this check is "contradicts," which is lenient by
default. Absence is not treated as strictness because no existing delivery
carries sidecar `scope`: were absence refused by default, every scoped
request would become unfulfillable by any source delivered before this
feature existed. `--require-scope` is how a host whose delivery pipeline reliably
stamps `scope` opts into the fail-closed behavior instead.

`reopen` (`question_resolve.py`) uses the same contradiction layer to pair
each supplied request with the supplied source whose scope does not
contradict it, instead of zipping the two `--request-id`/`--source-id` lists
by argument order. Requests or sources without scope fall back to the
previous positional behavior. When a request's declared scope cannot single
out one supplied source, `reopen` still pairs — it does not refuse — but says
so; see "When scope cannot decide a pairing" below.

### Choosing scope keys

A scope key is a **join key**: two sides that never talk to each other have to
produce the same string independently. Declare a key only when they can.

- **The delivering side must be able to derive the value.** A value only the
  workspace can construct — a question slug, a page id, anything carrying a
  workspace-side hash — cannot be stamped into a sidecar, so under
  `--require-scope` the request becomes permanently unfulfillable. `--match-scope`
  is not an escape hatch: asserted keys join the required set rather than
  leaving it (layer 3 above).
- **The value must vary across the set being paired.** `reopen` only pairs
  requests that all reference the same question, so a key with one value per
  question is constant across that set and discriminates nothing. `facet_id`
  varies within a question; a product, listing, or candidate identifier usually
  does not.
- **Both sides should emit the value from one generator.** Comparison is exact
  text after stripping, with no case folding and no normalization, so two
  independent derivations of "the same" identifier are a
  `REQUEST_SCOPE_MISMATCH` waiting to happen.

The request↔delivery binding itself does not need a scope key. Under
`orchestration.acquisition: delegated` the sidecar's `request_id` is mandatory
and enforced by the orchestration postcondition, which is both stricter and
narrower than any scope comparison. Scope answers "is this the right *kind* of
evidence for this request", not "is this the right request".

A key set is well chosen when every key is derivable on both sides and at least
one key varies within the question being reopened. A second key earns its place
only when two requests on one question share a facet — two quotes for the same
facet from different suppliers, where the delivering side knows which supplier
it fetched from:

```bash
python3 scripts/source_requests.py add --kind pack:market-data/supplier_quote \
  --scope facet_id=supplier_quote --scope supplier=acme ...
```

With one request per facet, `facet_id` alone is the whole key set.

### When scope cannot decide a pairing

Declared scope narrows the candidates for each request; it does not always
narrow them to one. When two supplied requests declare the same scope, the scope
evidence cannot say which delivery answers which — and that holds even when the
deliveries differ, since scope is symmetric between the two requests: whichever
one ends up with the better-corroborated source got it by supply order, not
because scope chose it. `reopen` still
returns a pairing in that case — refusing would break reopens that are
legitimate today, and `reopen` reports a pairing rather than recording a
fulfilment — but it reports how each pair was decided:

- each `pairs[]` entry carries `decided_by`: `scope` when the declared scope
  determined it, `tie_break` when another equally corroborated source could have
  answered the request **or** another request could have taken the source, and
  the choice fell to the order the requests and sources were supplied;
- a `tie_break` pair adds a `request_scope_pairing_tie` entry to the report's
  `warnings` array, naming both the sources that could have answered that request
  (`alternative_source_ids`) and the requests that could have taken its source
  (`contending_request_ids`); and
- `log.md` records those pairs as tie-broken rather than claiming scope decided
  them.

A source that positively corroborates more of a request's scope still wins over
one that merely fails to contradict it, and that is a scope decision, not a tie.
`decided_by: tie_break` means specifically that scope ran out of discriminating
power, which is the signal to add a key that varies within the question — or to
reopen those requests in separate calls.

`reopen --require-decisive-scope` turns that signal into a refusal
(`REQUEST_SCOPE_UNDECIDED`), leaving the question `blocked`. It is the reopen
counterpart to `fulfill --require-scope` on the axis reopen has: that flag asks
whether the delivery *stated* the request's keys, this one whether the declared
keys *discriminate*. Opt-in for the same reason absence is tolerated by default —
requests that are genuinely interchangeable have ties with no consequence, and
only the host knows which kind it has.

### Recorded acquisition attempts

A request that was attempted and produced nothing leaves no trace in the record above: its
`status` stays `open`, which is correct but says nothing about whether anyone tried. Failed
attempts are recorded in an append-only audit beside the request store,
`sources/source-request-attempts.jsonl`, one JSON object per line:

```json
{
  "schema_version": "1.0",
  "event_type": "source_request_attempt_failed",
  "event_id": "attempt-d3e6a14b38",
  "request_id": "req-1a2b3c4d5e",
  "orchestration_id": "orch-20260808T120000Z-abcd1234",
  "action_id": "action-0001",
  "failure_code": "provider_throttled",
  "detail": "connector reported 429, retry-after 60s",
  "recorded_at": "2026-08-08T12:00:00Z"
}
```

```bash
python3 scripts/source_requests.py record-attempt-failure --request-id req-1a2b3c4d5e \
  --failure-code provider_throttled --orchestration-id ORCH_ID --action-id ACTION_ID \
  --detail "connector reported 429, retry-after 60s" --format json
```

Field notes:

- `failure_code`: any acquisition-attempt code from the taxonomy above. Delivery codes are
  valid here too — an attempt that failed with a plain HTTP 500 records `http_error`.
- `orchestration_id`, `action_id`: the session and work order the attempt ran under.
  Attempts are counted **per session**, so a new session gets a fresh look at every
  request; that is the supported way to retry after fixing a host-side cause, rather than
  editing this file.
- `detail`: optional operator context, truncated to 500 characters rather than refused.
- `event_id`: stable identity. The audit is append-only and fingerprinted by event id, so
  a recorded attempt cannot be rewritten or removed without detection.

The command refuses an unknown request id (`REQUEST_UNKNOWN`), a request that is already
fulfilled (`REQUEST_ALREADY_FULFILLED` — a fulfilled request has evidence and no failed
attempt to record), and an unrecognized failure code (`ATTEMPT_FAILURE_CODE_INVALID`). It
appends one `source-request` entry to `log.md`.

Readers of this file must ignore fields they do not recognize: the event shape is expected
to grow, and a reader pinned to today's exact key set would refuse events written by a
later version of this package.

Under `orchestration.acquisition: delegated` these commands run inside a pending
acquisition work order rather than between actions; see
[../skills/research-acquire-delegated.md](../skills/research-acquire-delegated.md) for the
external acquirer's loop and [orchestration.md](orchestration.md) for the session shape.

`plan-fetch` is read-only: it turns a request into candidate provider commands and records `network_io_executed: false`. Request-kind-based routing only special-cases `kind: paper`; every other kind — `dataset`, `web`, `code`, `other`, `structured_data`, and any pack-declared kind (`pack:<pack-name>/<kind-id>`) — has no provider-backed fetch plan and returns the same `unsupported` status and warning ("No provider-backed plan is available for this kind; use manual delivery."), with `network_io_executed: false` rather than an error. Repeating `--candidate-id` limits `candidate_routes` to exactly those selected candidates; an unknown, non-selected, or differently linked ID is rejected. Managed acquisition must pass the work order's candidate IDs so another selected candidate on the same request is never emitted accidentally. Omitting the flag retains the request-wide operator workflow. A fetch agent's loop is: `list --status open --format json` → scoped `plan-fetch --request-id ... --candidate-id ... --format json` → deliver files with sidecars (set `request_id` and `candidate_id` in the sidecar, and read the request's `scope` — if present, stamp the matching keys into the sidecar's `scope:` mapping so fulfilment can verify the delivery against the request instead of assuming it) → run inventory and normalization → `fulfill` each delivered request. Use `skills/research-acquire.md` for the optional provider-backed version of this loop, including disabled-acquisition refusal, sidecar verification, blocked-question reopening, and final status reporting. `add` and `fulfill` append one `source-request` entry to `log.md`; `list` and `plan-fetch` do not mutate the request artifact or `log.md`. When reopening a blocked question over multiple delivered sources, `reopen` pairs each request to a source by declared scope (see "Scope Matching" above) rather than by the order `--request-id`/`--source-id` were passed; where the declared scope cannot single out one source, the pair is still reported but marked `decided_by: tie_break` rather than presented as a scope decision.

### Selected discovery candidates

When a request has discovery candidates that were explicitly selected for it
(`discover_sources.py candidates select --candidate-id ... --request-id ...`, via
the `skills/research-discover.md` playbook; see
[source-discovery.md](source-discovery.md)), `plan-fetch` adds a `candidate_routes`
array — one explicit acquisition route per selected candidate, keyed by candidate
type. Selections are authoritative, so when present they upgrade an
`unsupported`/`ambiguous` request to `plan_status: ready`. Each route reuses real
provider syntax; it never invents commands:

When one or more `--candidate-id` values are supplied, this array contains only
the requested selected records, in argument order. This is the managed
work-order boundary; request-wide planning remains available only by omitting
the filter deliberately.

| Candidate | Route | Suggested command / target |
|-----------|-------|----------------------------|
| arXiv id (paper URL or `paper.arxiv_id`) | `arxiv download-source` / `search-by-id` | `fetch_sources.py arxiv ...` |
| OpenAlex OA paper (`paper.provider_ids.openalex` plus `paper.pdf_url`) | `openalex download-pdf` | `fetch_sources.py openalex download-pdf --work-id ...` |
| OpenAlex metadata-only or non-OA paper | `openalex get` | `fetch_sources.py openalex get --id-or-doi ... --output raw/papers/openalex-...-metadata.json --request-id ... --candidate-id ...` plus non-OA/manual-delivery warning |
| Uncertain paper title | `openalex resolve` | `fetch_sources.py openalex resolve --entity works --query ... --max-results 5` plus resolution warning |
| DOI (paper URL or `paper.doi`) | `openalex get-by-doi` | `fetch_sources.py openalex get --id-or-doi ... --output raw/papers/openalex-...-metadata.json --request-id ... --candidate-id ...` |
| GitHub repo (`code_repository` or a github.com URL) | `github repo-metadata` | `fetch_sources.py github repo-metadata --url ... --request-id ... --candidate-id ...` |
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
- `scripts/lint.py` also reports a delivered source with no normalized record (LOW, `source_missing_normalized`) and a normalized record from an external normalizer that does not conform to the record contract (MEDIUM, `normalized_record_contract_violation`). Acceptance of an externally written record is gated on that conformance check, not on its origin.

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
- [../skills/research-acquire-delegated.md](../skills/research-acquire-delegated.md) —
  the same delivery contract driven by an external acquirer under
  `orchestration.acquisition: delegated`, where the host owns the connectors.
- [orchestrator-handoff.md](orchestrator-handoff.md) — the end-to-end machine contract this delivery step belongs to.
- [source-manifest.md](source-manifest.md) — manifest record fields, including the `provenance` object.
- [normalized-source-format.md](normalized-source-format.md) — normalized record frontmatter, including propagated provenance.
