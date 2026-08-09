# Changelog

## Unreleased

- Add a source family the package has never heard of, without forking it.
  Provider ids were validated against closed tuples — `arxiv`, `openalex`,
  `github`, `web` for acquisition — so an embedder whose evidence is market
  data, marketplace listings, or instrument output had exactly one route to it:
  edit the starter scripts, and inherit the maintenance of every transport,
  provenance, budget, and quarantine guarantee those scripts carry. A
  pip-installed distribution can now **register** a provider through an entry
  point, behind a capability declaration the package machine-checks:

  ```toml
  [project.entry-points."evidence_wiki.acquisition_providers"]
  keepa = "autoseller_connectors.evidence_wiki:KeepaProvider"
  ```

  ```python
  class KeepaProvider(AcquisitionProvider):
      id = "keepa"
      provider_api_version = 1
      capabilities = ProviderCapabilities(
          allowed_domains=("api.keepa.com",),
          terms_urls=("https://keepa.com/#!api",),
          license_inference="none",
          rate_limit=RateLimit(60, per="minute"),
          credentials=("KEEPA_API_KEY",),      # env-var NAMES, never values
      )
  ```

  Authorize `keepa` in `research.yml` and fetch through one generic subcommand
  — `fetch_sources.py registered get --id keepa --request-file request.json`,
  with `discover_sources.py registered search` as the read-only mirror.

  What makes the declaration worth more than a comment is a **planner/executor
  split**. The provider is a request planner and a response interpreter: it
  returns `PlannedRequest` values and later turns responses into a
  `SourceArtifact`, and the *package* performs every fetch, through the same
  pinned transport the built-in adapters use — `allowed_domains` checked before
  any socket work, DNS pinning, public-address enforcement, bounded downloads,
  redacted diagnostics — and the package's own writer produces the sidecar and
  the bytes, in that order, with the quarantine discipline built-in acquisition
  already has. The alternative designs were both rejected for the same reason:
  a plugin that owns its sockets, in-process or in a subprocess, turns
  `allowed_domains` into documentation, which is the failure this change exists
  to avoid. Credentials follow from the same split — a provider declares
  variable *names*, writes `{{credential:KEEPA_API_KEY}}` in a header value, and
  the package resolves it at execution and registers the value for redaction.
  Placeholders are refused in URLs, because a secret in a query string would
  survive URL redaction and surface in logs and in `origin_url`.

  Enforcement is stated per field rather than implied. `allowed_domains` and
  `credentials` are enforced at transport, a declared `rate_limit` is enforced
  against a durable per-run ledger that tightens but never loosens
  `max_downloads_per_run`, `captures_raw` and `quarantine_on_incomplete` are
  validated once at registration (v1 requires both `true`, since the writer
  provides both unconditionally and would not honor a `false`), and
  `terms_urls`, `license_inference`, and `request_kinds` are recorded and shown
  by doctor. Recording is not nothing: every artifact acquired this way carries
  `provider_registration` (id, phase, distribution, version, entry point, api
  version) and `provider_capabilities` beside its usual provenance, so an
  auditor can ask which installed code produced a record and what it claimed it
  could reach. `evidence-wiki doctor` gains a Registered providers section
  listing each valid registration with that summary and whether `research.yml`
  authorizes it, plus every invalid registration with the reason it was refused
  — a broken entry point never crashes enumeration and never silently vanishes.
  Six stable codes join the set: `PROVIDER_NOT_REGISTERED`,
  `PROVIDER_REGISTRATION_INVALID`, `PROVIDER_REQUEST_INVALID`,
  `PROVIDER_PLAN_INVALID`, `ACQUISITION_DOMAIN_NOT_DECLARED`, and
  `ACQUISITION_PROVIDER_RATE_LIMITED`.

  One claim is deliberately not made. Installing a Python distribution is
  arbitrary code execution, so a malicious plugin can always open its own
  sockets in-process; nothing here sandboxes third-party code. The enforceable
  guarantee is narrower and is the one the docs state: every fetch performed
  *through the package's acquisition flow* is executed by the package's
  transport and checked against the declaration, and nothing enters the
  workspace except through the package's writer.

  **The authorization boundary is untouched.** Registration makes a provider
  *available*; `research.yml` still *enables* it, by explicit id, and an
  installed-but-unlisted provider refuses with the byte-identical
  `ACQUISITION_PROVIDER_DISABLED` envelope a disabled built-in produces. The
  inverse — an id authorized in an environment where the distribution is not
  installed — is a smoke failure naming the missing distribution, not a warning,
  because an authorization the environment cannot satisfy is drift on an
  authorization boundary; lint deliberately gains no check, so a lint report
  stays a pure function of the workspace tree. Built-in providers, their
  subcommand trees, and every existing error envelope are unchanged, and with no
  entry points installed the built-in lists are the whole universe, bit for bit
  as before. The new
  [provider-registration.md](workspace-template/docs/provider-registration.md)
  documents the authoring contract, the trust boundary, credential custody, the
  failure modes, and the v1 limits — no request signing or OAuth refresh, no
  subprocess isolation, `request_kinds` recorded but not routed, and no
  registering a provider from configuration.

- Ground a claim in a named field instead of a quoted line, and give hosts a
  supported way to write grounding at all. Grounding was one shape — a verbatim
  quote, checked by substring containment against a normalized record's Markdown
  body. For prose that is the right check. For structured evidence it proves the
  record exists and contains *a* sentence, not that the claim's value is in it, so
  the path of least resistance was quoting any line of the cited section and
  calling the answer grounded. A grounding entry now carries `claim`, `source_id`,
  and **exactly one** form of evidence — the existing `quote` (with optional
  `location_hint`), or a new `anchor`:

  ```yaml
  grounding:
    - claim: "Current supplier price is 23.99 EUR"
      source_id: data--keepa--b0abc123
      anchor:
        pointer: "supplier_quote/price"
        expected: "23.99 EUR"
  ```

  `pointer` is an RFC 6901 JSON Pointer with the leading `/` optional, resolved
  against the cited record's structured view, and `expected` is compared to the
  field it reaches by canonical **equality — never containment**. Strings fold
  through the codebase's one deterministic text normalization; numbers compare as
  `Decimal`, so `23.99` and `"23.990"` agree while `"23.99 EUR"` against the number
  `23.99` is an honest mismatch; booleans are checked before numbers, so `true`
  never canonicalizes to `1`. A pointer that reaches a mapping or an array is
  refused (`anchor_target_not_scalar`) rather than searched: an anchor cites a
  field, not a subtree. Query languages were rejected for the same reason — an
  anchor must be a *reference*, and a query that selects a matching value is
  exactly the coincidental match the form exists to remove.

  Anchors resolve against a **structured-view sidecar**,
  `sources/normalized/<safe-source-id>.structured.json`: one JSON object holding the
  complete, uncapped structured rendering of a source, written beside the record and
  bound to it by `structured_view: {path, content_hash}` with a
  `sha256:<64 hex>` digest over the sidecar bytes. The hash is what makes a cited
  value the record's own rather than a neighbouring file's; bytes that no longer
  match are `structured_view_corrupt` and refuse rather than read. Resolving there,
  rather than against the raw payload, keeps pointers facet-shaped instead of
  provider-payload-shaped and keeps the verifier format-free — it always reads
  exactly one JSON object, whatever the source kind was. The sidecar is written
  before the record that binds it, each through an atomic replace, so the worst
  outcome of an interrupted write is an undeclared sidecar (inert, reported by the
  contract check and by lint) rather than a record pointing at a file that never
  existed. This is the fix the rendering-caps work named: `rendered_coverage` still
  describes the capped body a quote is found in, and the sidecar is its uncapped
  complement — a facet capped in the body is reachable verbatim through the sidecar.
  Three producers write one: a normalizer adapter returning the new optional
  `structured` key, the package's own CSV/TSV path, and any external normalizer that
  writes the pair itself and passes `normalize_verify.py`, exactly as CR-2
  legitimized foreign records.

  Native tabular records emit the sidecar from the parse `normalize_sources.py`
  already runs, which previously threw away everything but 20 sample rows with
  80-character-ellipsized cells: `{"columns": [...], "rows": [{<column>: <cell>}]}`,
  header-keyed so a pointer reads `rows/41/price`, every value a string with no type
  inference (`"007"` stays `"007"`), cells verbatim and uncapped. Emission is
  **fail-closed**: nothing is written if the read stopped at the 5 MB ceiling, if any
  row is ragged, or if a header cell is empty or duplicated, and a `parse_warnings`
  entry names which. One shape or none — never a partial one, and never a
  synthesized `price_2` for a duplicated column, which would address a column the
  source never had. Anything excluded degrades to exactly the previous behavior, so
  the addition cannot weaken a gate. Making the package the second producer of the
  sidecar it asks others to emit is the cheapest proof that the contract is not
  adapter-shaped.

  Grounding also stopped being something a host had to hand-edit. `question_resolve.py
  grounding set --slug S --from-file F --agent-id A` replaces a question's whole
  `grounding` block from a YAML (or JSON) file, under the same claim rules and the
  same per-question lock every other mutation uses, in a canonical serialization the
  docs publish as normative — fixed key order, fixed indentation, free-text fields
  always JSON double-quoted so `expected: "23.99"` cannot reload as a float. It
  **replaces** and never merges: grounding is authored as a set for one answer, and
  merging two invites duplicate claims in an order nobody chose. Replacement drops
  any `verified_by`/`grounding_verified_at` stamp in the same write, because the
  entries those attested no longer exist. `grounding: []` clears grounding
  explicitly; `grounding:` with nothing after it is refused as an unfinished edit.
  It deliberately does **not** verify — `verify_quotes.py --slug S` already *is* that
  seam, and a `--verify` flag would mean every future change to verification
  semantics had to remember two doors — so its envelope reports
  `verification: not_performed` and names the follow-up command. `answer
  --grounding-file F` is the single-write alternative: the file's entries and the
  resolution fields land in one atomic write under one lock, and with
  `--require-grounding` it is the *file's* entries that must verify, not whatever the
  page still holds from a previous cycle. A separate `grounding.py` script was
  rejected — it would duplicate claim enforcement, lock and atomic-write plumbing,
  and the frontmatter renderer, when question mutation already lives in exactly one
  script.

  Two new fatal codes join the stable set: `GROUNDING_ANCHOR_INVALID` when at least
  one failed entry is anchor-form, and `GROUNDING_FILE_INVALID` for a grounding file
  that is unreadable, not YAML, or neither accepted shape.
  `GROUNDING_QUOTE_INVALID` keeps its exact old meaning — an all-quote-form failure
  set — because hosts switch on it; both codes carry the full per-entry failure list,
  so a mixed set is fully enumerated whichever one tops the envelope. Verification
  reports gain five per-entry results (`structured_view_missing`,
  `structured_view_corrupt`, `anchor_pointer_not_found`, `anchor_target_not_scalar`,
  `anchor_value_mismatch`), a `form` and `policy` on every entry, and `by_form`
  counts on each question and the report. `normalize_verify.py` reports a
  `structured_view` summary per record and a `with_structured_view` count;
  `NORMALIZED_CONTRACT_STRUCTURED_VIEW_INVALID` names every way a record and its
  sidecar can disagree, including a sidecar sitting beside a record that fails to
  declare it. Lint counts the migration with `grounding_entries_quote` and
  `grounding_entries_anchor` and a `- grounding: quote=N anchor=M` log line, and its
  existing LOW `normalized_orphan` finding now also fires for a sidecar with no
  record beside it — the `normalized_orphans` stat still counts orphaned records
  only, so a host reading that number sees no change. Publication readiness renames
  the grounding reason policy to
  `retained_quote_evidence_or_structured_anchor_evidence`; both original names remain
  verbatim substrings, so a host grepping for either still matches.

  **Backward compatibility is the load-bearing part.** Quote-form grounding is
  verified exactly as before, bit for bit, including its error code and per-entry
  results. There is no new configuration anywhere — the feature is artifact-driven,
  and a workspace whose sources emit no sidecars and whose questions carry no anchor
  entries behaves exactly as it did before. Records that predate the field are
  re-normalized once, and only those whose extraction method could carry a sidecar at
  all: raising `NORMALIZER_VERSION` would have rewritten every record in the
  workspace to reach the few that can hold one. Entailment stays out of scope —
  anchors prove "this exact value in this exact record", deterministically; whether
  the claim *follows* from the value remains a human and policy question. See
  `docs/question-api.md` and `docs/normalized-source-format.md`.
- Let a domain pack declare its own source-request kinds instead of being
  limited to the package's built-in set. A pack overlay lists kind
  declarations under `domain_pack.request_kinds` — a list of mappings with
  required `id`, `label`, and `description` — namespaced exactly like pack
  evidence policies: `pack:<pack-name>/<kind-id>`, with the namespace segment
  required to equal the pack's own `domain_pack.name`, so one pack can never
  declare kinds in another's namespace. Built-in ids stay reserved and
  unprefixed and cannot be redeclared. `evidence-wiki pack validate` checks id
  shape, namespace, and uniqueness before a pack ships; `source_requests.py
  add --kind` resolves the same declaration from the workspace's merged
  `research.yml` and refuses a malformed id with `REQUEST_KIND_INVALID`
  (naming the correctly prefixed id when a bare `<pack>/<kind>` id is
  submitted) or a well-formed but undeclared id with `REQUEST_KIND_UNDECLARED`.
  Everything here is additive: a workspace with no installed pack, or a pack
  that declares no `request_kinds`, accepts exactly the built-in kinds it
  always did.
- Add `structured_data` as a built-in source-request kind — the generic
  non-documentary bucket for evidence like price series, sensor output, and
  other data that isn't a paper, dataset file, web page, or code artifact. It
  dovetails with the existing manifest-side `structured_data` classification
  and CR-2's `normalization.adapters` config: a request opened with `--kind
  structured_data` and fulfilled by a delivery an adapter normalizes closes
  the same loop `paper` or `dataset` requests do. Like every kind other than
  `paper` in the heuristic route planner, `plan-fetch` reports the existing
  manual-delivery warning for it when no candidate is selected; a selected
  discovery candidate routes exactly as it would for any other kind.
- Let a source request carry a structured `scope` — an open key-value mapping
  stating what would satisfy it — instead of relying on a free-text
  `query_or_identifier` and positional convention to pair a delivered source
  back to the request it answers. `add --scope key=value` (repeatable) records
  the mapping on the request; a deliverer states the same keys in the
  `.provenance.yml` sidecar's new `scope:` field. `fulfill` always checks for
  contradiction — a key both sides declare with different values refuses with
  `REQUEST_SCOPE_MISMATCH`, naming the conflicting keys and both values, and
  writes nothing — which is what stops a source that answers facet Y from
  being recorded as fulfilling a request for facet X. `fulfill --match-scope
  key=value` lets the caller assert scope keys when fulfilling, checked the
  same way. `fulfill --require-scope` upgrades absence to a refusal: every key
  the request declares, and every key `--match-scope` asserts, must be present
  and equal in the delivered scope, or the fulfilment is refused with
  `REQUEST_SCOPE_MISSING` — the fail-closed mode for a delivery pipeline that
  can guarantee it stamps scope. Malformed
  `--scope`/`--match-scope` input (missing `=`, a malformed key, a repeated
  key) is refused with `REQUEST_SCOPE_INVALID`. None of this is required: a
  request that declares no scope, or a delivery whose sidecar declares none,
  behaves exactly as before — the contradiction check can only fire when both
  sides opted in.
- Pair a `reopen`'s supplied requests and sources by declared scope instead of
  by argument order. `question_resolve.py reopen` now computes, for each
  scoped request among the supplied `--request-id`/`--source-id` pairs, which
  supplied source's provenance scope does not contradict it, and reports the
  pairing as `pairs` in its result. A scoped request that matches no supplied
  source, or an assignment that would pair one source with two scoped
  requests, refuses with `REQUEST_SCOPE_MISMATCH` before any write. Requests
  and sources without scope fall back to the previous unpaired behavior, so an
  unscoped `reopen` call is unchanged.
- Back-fill a request's scope from the coverage manifest that blocks on it.
  `coverage_manifest.py set-facet --blocking-request-id` now writes the linked
  facet's id into the request's `scope.facet_id` when the request doesn't
  already declare one, so the manifest and the request can never disagree
  about which facet a request unblocks. A request that already carries a
  different `scope.facet_id` refuses the facet write with
  `FACET_SCOPE_CONFLICT`, naming both facet ids and the request. The
  back-fill is idempotent — rerunning `set-facet` is a no-op — and
  `--clear-blocking-request-ids` does not clear the request's own scope, since
  the request still states what would satisfy it even after the manifest link
  is removed.
- Add `kind` and `scope` read filters to the request-listing surfaces.
  `source_requests.py list --kind <id>` (repeatable) and `--scope key=value`
  (repeatable, AND semantics, exact string equality) narrow the listing the
  same way `--status` already does; a malformed `--scope` pair is refused with
  `REQUEST_SCOPE_INVALID` and an unknown `--kind` with
  `REQUEST_KIND_UNDECLARED`. The MCP `source_requests_list` tool gains
  matching `kind` and `scope` arguments so a host reading through MCP —
  `serve-mcp` is the read surface for every other service — can filter the
  same way. Both filters are optional additions to an existing read path;
  omitting them returns exactly what `list`/`source_requests_list` returned
  before.
- Let a registered external acquirer fulfil source requests, so a host that
  already owns fetching infrastructure — rate limits, credential custody, egress
  policy, compliance logging — can supply evidence without the workspace fetching
  anything. Previously such a host had no sanctioned path: with
  `integrations.acquisition.providers: []` the controller had no provider to
  address acquisition work to, so the phase was unreachable and the only way to
  close a blocked question was to run `source_requests.py fulfill` out of band,
  after `submit` returned. That produced exactly the thing the integrity baseline
  exists to prevent — a mutation of durable evidence state that no work order
  accounts for. `research.yml` can now name the acquirer instead:

  ```yaml
  orchestration:
    acquisition: delegated
    acquirer_agent_id: autoseller-orchestrator
  ```

  Under that declaration the controller issues acquisition work orders scoped to
  the open requests and addressed to `assigned_agent_id`, and the acquirer does
  its delivery, fulfilment, and question reopening *inside* the pending order.
  Submission verifies the result the same way it verifies a provider acquisition:
  every scoped request must end with a fulfilment whose evidence is normalized and
  linked by a provenance sidecar's `request_id`, or with a structured failure in a
  new append-only attempt audit naming that action. A partial batch is `completed`;
  `blocked` means nothing durable changed. While a delegated session is live, the
  workspace commands refuse mutations no pending order scopes
  (`SOURCE_REQUEST_FULFILL_DELEGATED`, `QUESTION_REOPEN_DELEGATED`), which is what
  makes "fulfilment stays inside the protocol" enforceable rather than advisory.

  Failed attempts use the shared delivery taxonomy plus three connector-level
  codes (`provider_throttled`, `not_authorized`, `no_result`); a request is retired
  after `max_attempts_per_request` attempts within a session, or immediately on a
  standing refusal, after which the session terminates `blocked_on_sources` with a
  stable reason prefix and an `exhausted_requests` map. Attempts are counted per
  session, so starting a new one is the supported retry after a host-side fix.

  Everything is opt-in and additive: `acquisition: providers` remains the default
  and is unchanged, the session and work-order schemas stay at `1.0` with optional
  fields, and managed `orchestrate run`/`resume` refuse a delegated workspace up
  front, since a delegated order is addressed to the host's own connectors. See
  `docs/orchestration.md`, `skills/research-acquire-delegated.md`, and the
  `orchestration:` section of `docs/research-yml.md`.

- Promote `docs/normalized-source-format.md` to a versioned public contract so an
  external normalizer can produce first-class normalized records for source kinds
  this package does not extract itself. Every record now declares the contract
  version it conforms to in `normalized_format` (currently `1`), which is separate
  from `normalizer`: `normalizer` says which tool produced a record and is only a
  regeneration trigger for this package's own output, while `normalized_format` is
  the single field an external writer must track. `evidence-wiki contract` reports
  the written version, the accepted versions, and the running normalizer identity
  under `normalized_source_format`. A record written outside this package must
  declare `normalized_format`; a record with no such field is read as a legacy
  record from this package's own normalizer. `docs/normalized-source-format.md`
  now carries the external-writer workflow, including which sources an external
  tool may write records for: records for kinds `normalize_sources.py` does not
  read are never touched, while a record written for a kind it does read is
  regenerated by the next normalization run.
- Deliver the commented optional-section examples into generated workspaces.
  `research.yml` is written by dumping parsed YAML, so the starter's comments never
  survived initialization and an operator's own config listed only active keys —
  giving no hint that scoped human review (`review:`) or external normalizer adapters
  (`normalization:`) could be enabled at all. Initialization now appends those
  examples verbatim from the starter as a commented footer, pointing at
  `docs/research-yml.md`. The blocks are extracted rather than duplicated, so the
  starter config stays the single source of truth and a future optional section is
  delivered without code changes; prose annotating active sections is not appended,
  and the footer adds no active configuration. `evidence-wiki upgrade` still never
  rewrites `research.yml`, so a section an operator enabled is untouched.
- Make quotability under rendering caps explicit. A normalized body for structured
  evidence is a rendering with caps, and grounding is verified by containment against
  that body — so whatever a renderer dropped is citable but not quotable, and a record
  that rendered a tenth of its payload looked exactly like one that rendered all of it.
  Records now carry `rendered_coverage`: the values considered, the values rendered,
  the ratio, and per-section counts with a `note` recording each cap. It is **required**
  for records this package renders through an adapter — the protocol can demand it, and
  a renderer that caps nothing reports equal counts with `ratio: 1.0` — and optional
  elsewhere, but checked whenever declared. The package verifies the claim is coherent
  (rendered never exceeds considered, the ratio matches its own counts, section counts
  stay within the record's totals, and a section claiming rendered values names a
  heading the body actually has) but never recomputes the counts, because it cannot
  know a foreign renderer's capping rules — which is why the renderer states them. A
  section that rendered nothing needs no heading: a facet dropped in full has none to
  point at, and its entry exists precisely to account for the loss. An adapter that
  omits or misstates coverage fails the action and writes no record.
- Surface rendering caps where an operator will see them. Every record entry in the
  `normalize_verify.py` report now carries `rendered_coverage` with the declared ratio,
  the two counts, and `capped_sections` naming the facets that lost content — the parts
  a claim can cite but never quote. The field is `null` for a record that declares no
  block, which is not the same as one that rendered nothing, and coverage is reported
  for invalid records too, since triaging one still needs to know how much of it is
  quotable. Lint gains an opt-in companion: set `lint.min_rendered_coverage_ratio` and
  any record declaring a lower ratio emits LOW `normalized_low_rendered_coverage`,
  counted in the `normalized_low_rendered_coverage` stat. It is unset by default and
  LOW when it fires, because capping a long series is a legitimate rendering choice
  rather than a defect — this is a visibility tool, and CR-7's structured grounding
  anchors are the actual fix for un-quotable content. Unlike the contract check, it
  applies to native and foreign records alike; a threshold that is not a number in
  `[0, 1]` disables the notice instead of failing the run.
- Cover the structured-evidence path end to end.
  `tests/test_structured_evidence_e2e.py` walks a delivered JSON payload and its
  sidecar through the whole chain in a workspace built by initialization — inventory,
  adapter normalization, contract verification, reopening the blocked question,
  grounding a claim in a facet value, lint, and the orchestration controller's own
  workspace-safety postcondition. The legs are load-bearing in sequence rather than
  individually: normalization is what opens the reopen gate, and the facet headings the
  adapter emits are what make a value quotable at all, so a regression in any stage
  surfaces as a broken chain instead of a passing unit test about a stage nothing can
  reach. Two companion cases pin the boundaries — the same delivery with no adapter
  configured is classified but never normalized and the gate stays shut, and a capped
  rendering still grounds what it did render while a dropped facet fails closed with
  `anchor_not_found`.
- Hold the hand-written path to the same chain. The same suite now runs a record an
  external tool wrote itself, with no adapter configured anywhere: it verifies against
  the same validator, opens the reopen gate, grounds a quote from a facet section, and
  leaves lint with nothing to say beyond the missing wiki source note — the same residue
  the adapter path leaves. A full `--all` normalization run is asserted to leave that
  record byte-identical, since a kind nothing here normalizes belongs to whoever wrote
  it. One mutation per contract violation family then shows each is named: verify
  reports exactly that family's code and nothing else, and lint reports it as MEDIUM
  `normalized_record_contract_violation` with the same code. The one asymmetry is
  asserted rather than glossed — a record with no frontmatter names no producing tool,
  so lint cannot tell it from a stray Markdown file and leaves it alone, while
  `normalize_verify.py` still refuses it.
- Guard the "no adapters configured, no change in behavior" promise.
  `tests/test_no_adapter_backward_compat.py` runs an existing fixture twice with one
  variable changed — whether `research.yml` has a `normalization:` section at all — and
  compares whole artifacts: the manifest, every normalized record byte for byte, the
  normalization report, the verifier report, and the lint payload. The declared command
  points at a path that cannot exist, so an adapter consulted for a kind it does not map
  would surface as a failed action rather than a silent no-op. Three further cases cover
  what the differential cannot see: a second normalization run rewrites nothing (the
  bumped `NORMALIZER_VERSION` must not perma-stale the records it was added to); a
  record written before the contract existed is still accepted, uncounted as foreign and
  unflagged; and the native frontmatter shape is pinned key by key, so a field added to
  every record has to be declared here rather than appearing quietly.
- Document the record fields that were being written but not published. Five
  codebase-record fields — `codebase_intake_status`, `codebase_execution_scope`,
  `codebase_artifact_manifest`, `codebase_artifact_checksums`, and
  `codebase_artifact_provenance` — went into every normalized record without appearing
  anywhere in `docs/normalized-source-format.md`, the contract hosts are told to target.
  They now have field-table rows covering the four intake verdicts, the standing
  `external_worker_only` nonexecution boundary, and the fact that
  `trust: self_asserted_external_worker` is literal: the worker asserts how it ran and
  this package records the assertion rather than confirming it. `title_confidence` and
  `rendered_coverage` gained rows too, so every field a record can carry is now in the
  table. One related correction: the HTML section claimed a title fallback produces a
  "lowered `title_confidence`", but that field is written for PDF records only and stays
  `null` on HTML records — the fallback is recorded in `parse_warnings`.
- Close the agent-facing gap for the two new lint findings. `research-lint` now covers
  `normalized_record_contract_violation` (run `normalize_verify.py` for the full list of
  breaches, then have the producer re-deliver) and `normalized_low_rendered_coverage` (a
  signal about which facets a claim cannot quote, not a defect to repair), and its fix
  policy now refuses hand-editing any record whose `normalizer.name` is not this
  package's: the edit would make the record claim a conformance its producer never gave
  it, and for a kind nothing here normalizes the edit sticks and hides the producer's
  bug. The example workspace's two legacy link records gained the `evidence_usable`
  field they predated, so the shipped workspace verifies against the contract. The
  format document now also says plainly that the legacy-absent rule exempts a record
  from the version check only — a record written before a required field existed can
  still be reported by `normalize_verify.py`, which the first re-normalization repairs
  and which lint never escalates.
- Make every normalized record in this repository pass its own contract, and keep it
  that way. Four fixture records did not: three battery-workspace stubs that carried no
  `type` at all, and a standards record missing half its required frontmatter and living
  at a path its `source_id` did not resolve to. Fixtures are what a reader opens to learn
  the format, so one that fails the verifier teaches the wrong shape and makes the
  verifier look broken. They are now full records — quoted prose preserved verbatim under
  `Extracted Text`, so grounding still verifies against them. A new conformance test walks
  the repository for workspaces with committed records rather than naming them, so a new
  fixture is enrolled by existing. Nothing caught this before: lint holds only externally
  produced records to the contract, which is what leaves a native record missing a field
  invisible.
- State the machine-output contract for hosts: `docs/orchestrator-handoff.md` gains a
  "Machine Output On stdout" section — under `--format json`, stdout carries exactly
  one JSON document, diagnostics go to stderr, and a fatal error leaves stdout empty —
  cross-linked from `docs/research-yml.md` and `docs/normalized-source-format.md`.
  **Embedders can delete defensive parsing**: there is no need to scan for the first
  `{`, skip banner lines, or tolerate trailing output, because a script producing any
  of those is failing the contract rather than expressing a variation of it. The
  section also names the two things not to mistake for violations — a non-zero exit
  still carries a report from the commands whose job is to assess a workspace, and
  `source_inventory.py --dry-run` without `--report` keeps its documented JSONL stream.
- Enforce machine-output purity per script. `tests/test_json_stdout_purity.py` runs
  every command that accepts `--format json` as a subprocess — in a real workspace and
  against an unreadable one — and asserts stdout parses as exactly one JSON document
  with nothing before or after it, by consuming the whole buffer rather than scanning
  for the first `{`. Fatal paths must leave stdout empty and put the shared envelope on
  stderr. Enrollment is automatic: a script that declares `--format` and is not listed
  fails the suite, so a new command cannot skip the contract. Two surfaces are encoded
  as the documented exceptions they are — `source_inventory.py --dry-run` without
  `--report` keeps its JSONL stream contract, and `query_index.py build-index` accepts
  no `--format` at all.
- Send `scripts/workspace_gc.py` fatal errors to stderr instead of stdout. Under
  `--format json` it printed a hand-rolled error object on stdout, where a caller
  parsing stdout could not tell it from a report document, and the object omitted
  `recoverable` so it did not match the envelope every other script emits. It now
  reports through the shared `emit_error` helper: stdout stays empty and stderr
  carries the standard envelope. This was the only stdout offender found in an audit
  of all 22 scripts that accept `--format json`, covering success paths, fatal-error
  paths, and reports about an unreadable workspace.
- Surface configured normalizer adapters in `scripts/doctor.py` under a new
  `normalization_adapters` check, so the one place normalization runs something the
  package did not ship is visible in the same preflight that reports runtime
  dependencies and workspace permissions. The check lists each adapter's kinds,
  command, declared identity, and timeout, states plainly when none are configured,
  and reports an invalid section as degraded with its `CONFIG_INVALID` code. Doctor
  only reads the declaration and never executes the command.
- Report a malformed `research.yml` `normalization:` section through the shared error
  envelope instead of a traceback. The section is read before any record is selected,
  so it cannot become a per-source failed action; `normalize_sources.py` now exits `2`
  with `error_code: CONFIG_INVALID`, its remediation, and empty stdout, matching every
  other fatal setup error.
- Tell an operator the right fix when a link file's URLs are not inventoried. A `.txt`
  URL list is only expanded into one source per URL when it sits under a link root, so
  a list delivered elsewhere becomes a single `link` record whose URLs never become
  sources. The inventory report surfaced that record but offered one action — "Fix
  malformed raw link files or remove them from raw link roots" — which describes the
  other case: such a file is not malformed, and moving it *out* of a link root is the
  reverse of the remedy. `link` records now carry `metadata.link_parse_status`
  (`outside_link_root` or `no_urls_parsed`), and the report emits the matching action
  for each, so a misplaced list is told to move under a link root or take a
  `.url`/`.webloc` extension. Records written before the field default to the parse
  failure, keeping their previous guidance.
- Report adapter work everywhere normalization reports itself: `summary.methods`
  gains an `adapter` counter in the JSON report, on the stderr summary line, and in
  the `log.md` entry written by `--append-log`, and an action produced by an external
  normalizer carries an `adapter` object naming the tool and version, so a report
  reader can attribute a record without opening it. The normalization report's
  `schema_version` stays `"1.0"`: new counters and action fields are
  forward-compatible additions, and the package bumps a schema version only for
  breaking shape changes.
- Decide adapter-record freshness by the adapter's identity rather than this package's
  normalizer version. An adapter record stores its adapter's version — often a string
  like `"1.4.0"` — which is not comparable to the package's integer
  `NORMALIZER_VERSION`, so every adapter record read as stale and every run
  re-executed the adapter to reproduce a record the workspace already had. A run now
  rebuilds an adapter record only when the configured adapter `name`/`version` differs
  from the producer the record names, or when the source's `raw_fingerprint` changed;
  otherwise it is reported `skipped_existing`. Bumping `version` in `research.yml` is
  the way to force a rebuild after changing an adapter's behavior. Native records are
  unaffected and still use the `NORMALIZER_VERSION` axis.
- Run external normalizer adapters from `normalize_sources.py`, closing the loop for
  evidence this package cannot extract. A record whose kind is mapped in
  `normalization.adapters` is selected, normalized, and re-normalized on staleness
  like any other record; the adapter returns content and the package writes the
  record, so frontmatter, section order, `raw_fingerprint`, and `content_hash` stay
  owned by one writer while `normalizer` names the adapter as producer and
  `extraction_method` is `adapter`. The protocol is one JSON document in on stdin, one
  out on stdout, documented in `docs/normalized-source-format.md`.
  Adapter output is treated as untrusted input and every failure is closed: a non-zero
  exit, a timeout, output that is not exactly one JSON document, an identity that does
  not match what `research.yml` authorized, a `failed` status, a missing body, a `##`
  heading that would collide with the record's own sections, or output past the size
  cap all produce a failed action with a named reason, a non-zero exit code, and no
  record on disk. The package re-checks its own rendering against the record contract
  after writing and removes the file if it does not conform, so a run can never leave
  a non-conforming record behind under the package's own name.
- Classify `.json` and `.jsonl` raw files as the new built-in `structured_data`
  kind, so structured payloads have a name an adapter can be mapped to instead of
  falling under `unknown`. Records carry a `raw_fingerprint` like other
  content-derived kinds, so a record built from a payload goes stale when the
  payload changes. The kind is classified-only: this package does not extract
  structured payloads, so nothing is normalized unless a workspace configures a
  `normalization:` adapter for the kind or an external normalizer writes the record.
  Re-running inventory on an existing workspace relabels affected records in place —
  source ids are path-derived and unchanged, and `detected_at` is carried forward.
  `.jsonl` previously classified as `table`, but only `.csv`/`.tsv` were ever
  normalized as tables, so nothing that worked before stops working. Lint is
  unaffected: `source_missing_normalized` already fired for these records under their
  old kinds and fires identically now.
- Add the optional `research.yml` `normalization:` section, which maps manifest
  source kinds to an external normalizer command so evidence this package cannot
  extract can produce normalized records through the normal pipeline. The section is
  read and validated by `scripts/_normalization_config.py`; when it is absent no
  adapter exists and normalization is unchanged, and
  `compatible_research_yml_contract` stays at `0.1` because the section is optional
  and additive. The template ships it commented out — executing a command is opt-in.
  Because configuring an adapter authorizes this package to run that command, the
  shape is strict: `command` must be an argv list (a string is refused rather than
  split, so argument boundaries are never guessed), `provider: command` must be
  stated explicitly, `name`/`version` are required so a record's claimed producer can
  be checked against what the workspace authorized, `timeout_seconds` defaults to 120
  and is bounded at 3600, one kind resolves to exactly one adapter, and kinds
  `normalize_sources.py` extracts itself are refused so a single config line cannot
  silently divert papers or PDFs from the built-in extractors. Kind membership is
  otherwise open, so a domain pack may declare its own. A malformed section is
  rejected rather than defaulted to "no adapter", which would let a workspace believe
  its structured evidence had been normalized when nothing ran.
- Lint now holds externally produced normalized records to the published record
  contract instead of accepting any file that exists. A record naming a producing
  tool other than this package's is accepted exactly as a native record when it
  conforms, and emits MEDIUM `normalized_record_contract_violation` naming the first
  breach, the offending field, and the remediation when it does not. The finding is
  MEDIUM rather than HIGH deliberately: a HIGH finding stops the orchestration
  controller from issuing the next work order, and one malformed record must not
  freeze research across a workspace — quote verification and the reopen check
  already fail closed on the record itself. Lint stats gain
  `sources_foreign_normalized` and `normalized_contract_violations`. Records that
  name no producing tool are unchanged: they predate the contract rather than claim
  it, and `normalize_verify.py` checks every record regardless. The existing
  `source_missing_normalized` finding for an absent record is untouched.
- Add `scripts/normalize_verify.py` and `evidence-wiki normalize verify`, which check
  normalized records against the published record contract so a host writing records
  with its own normalizer can prove they conform instead of matching an internal
  format by inspection. Records written outside the package are checked on exactly
  the same terms as records the package wrote. Selection is every record under the
  normalized directory, or specific `--source-id` values; `--format json` (default)
  emits one `normalize_verify_report` document naming each record's origin
  (`native` or `external`), declared contract version, verdict, and violations, and
  `--format text` renders the same findings for a human. A contract breach is report
  content rather than a fatal error, so one malformed record never hides the rest:
  the command exits `1` when any record is invalid and `2` only for a workspace it
  cannot read, which is reported through the shared stderr error envelope.
- Decide contract conformance in one place: `scripts/_normalized_contract.py`
  validates a normalized record against the published format and returns stable,
  namespaced violation codes (`NORMALIZED_CONTRACT_FRONTMATTER_MISSING`,
  `…_FRONTMATTER_INVALID`, `…_FORMAT_VERSION_UNSUPPORTED`, `…_SECTIONS_INVALID`,
  `…_MANIFEST_MISMATCH`, `…_WARNINGS_INCONSISTENT`), each carrying the offending
  field, what was expected, and what was found. `evidence-wiki contract` lists the
  codes under `normalized_source_format.violation_codes` so a host can handle them
  without scraping text. The module also owns the contract's own definitions — the
  accepted format versions and the source-id-to-path rule — which
  `normalize_sources.py` now imports rather than duplicating, so the record the
  package writes and the record it validates cannot describe different formats.
- `NORMALIZER_VERSION` moves from 2 to 3 to carry the new field, so the first
  normalization run after upgrading regenerates existing normalized records once to
  backfill `normalized_format` — the same one-time re-normalization that previous
  normalizer bumps performed. Grounded questions citing regenerated records should
  be re-verified with `verify_quotes.py --slug <slug> --write` before the next
  readiness evaluation, as documented for any change to normalized evidence text.

- Scope `human_review` escalation to the question instead of the workspace,
  behind a new optional `research.yml` `review:` section. Under
  `review.escalation_scope: question`, a question awaiting human review no
  longer flips the readiness verdict to `attention_required`: it is reported as
  `readiness.questions_awaiting_review`, orchestration keeps issuing work for
  the other questions, and a workspace whose only remaining work is pending
  reviews reports `in_progress` with the structured code
  `questions_awaiting_review_only` rather than `complete`. The default
  `review.escalation_scope: workspace` preserves 0.2.4 semantics, and
  `compatible_research_yml_contract` is unchanged at `0.1` because the section
  is optional and additive.
- Add `question_resolve.py review --slug S --policy P --verdict
  accepted|rejected --reviewed-by PRINCIPAL [--review-ref REF] [--note TEXT]`,
  which records a human review collected outside the workspace against one
  coverage policy at a time. Entries append to a `human_reviews` frontmatter
  list; the question becomes `answered` once every declared policy is accepted,
  writing the same review fields `approve` has always written, and a rejection
  returns it to `open` with the reason retained. `approve` is unchanged for
  callers and now accepts every still-pending policy through the same writer.
  Publication readiness accepts recorded external reviews exactly as it accepts
  `approve`, and still refuses to ship an answer whose required review is
  unrecorded.
- Report a review queue that has stopped moving: under
  `review.escalation_scope: question`, lint emits HIGH
  `question_human_review_stale` once a question has awaited review longer than
  `review.max_pending_review_hours` (default 168, `null` disables), which
  returns the workspace to `attention_required` through the existing
  lint-to-verdict path. A parked question with no usable
  `human_review_requested_at` emits MEDIUM `question_human_review_undated`.
- Surface the review queue for reviewers and hosts: `workspace_status.py` text
  output and MCP payload carry the awaiting-review count and slugs,
  `question_status.py` records carry `human_review_requested_at` and
  `human_review_pending_policies` (rendered as age and pending-policy count in
  text), and `fleet_status.py` and `run_report.py` report the counter per
  workspace. `export_answers.py` exports the per-policy `human_reviews` entries
  for audit.
- Declare the `human_review` question status and the review frontmatter fields
  in the starter `research.yml` frontmatter rules, so a parked question no
  longer draws a spurious `frontmatter` lint finding in a stock workspace.
- Refactor managed Codex and Claude execution behind a closed adapter registry,
  and document OpenCode, Pi, and other harnesses as external-protocol clients.
- Bump the reusable managed workspace starter to `0.5.5`, including the
  canonical `CLAUDE.md` instruction pointer in its required asset manifest.

## 0.2.4 - 2026-07-21

- Treat broken or partially initialized pypdf import-spec lookups as a missing
  required dependency in workspace health checks, returning a typed finding
  instead of raising an inspection error.
- Restore DNS and HTTPS for provider-enabled managed Codex actions on
  Linux/WSL2, including symlinked `/etc` layouts, by adding bounded read-only
  system resolver configuration to the named permission profile, while keeping
  offline actions unchanged and rejecting unexpected external resolver
  targets.
- Bump the reusable managed workspace starter to `0.5.4`; `evidence-wiki
  upgrade` refreshes its health-check and orchestration guidance while
  preserving project evidence and configuration.

## 0.2.3 - 2026-07-21

- Install pypdf as the portable, canonical PDF normalization backend so a
  normal wheel installation can process PDF-only evidence on macOS, Linux, and
  Windows without an undeclared system executable.
- Treat Poppler `pdftotext` as an explicit optional compatibility backend,
  document the pip/system-package boundary and platform installation commands,
  and align doctor/workspace-health reporting with the actual backend policy.
- Pause and replay retryable blocked orchestration actions, while treating an
  audited candidate-specific `selected` to `failed` acquisition transition as
  one exhausted route so the controller can try the next candidate before it
  declares terminal `blocked_on_sources`.
- Reject acquisition fulfillment backed by failed, stubbed, explicitly
  unusable, or text-empty normalized evidence, and validate the request
  correlation supplied for definitive candidate-route failures.
- Harden Windows workspace locking and managed-run runtime handling so
  concurrent index builders and protected virtual-environment checks fail
  predictably instead of raising platform-specific permission errors.
- Bump the managed workspace starter to `0.5.3`; `evidence-wiki upgrade`
  refreshes the compatible managed scripts while preserving project evidence and
  configuration.

## 0.2.2 - 2026-07-21

- Canonicalize package-managed runner results by discarding descriptive
  `runs/orchestrations/` artifact references before submission, while keeping
  direct protocol validation strict and retaining fail-closed control-tree
  mutation detection.
- Pin managed workspace scripts to the Python interpreter that launched
  EvidenceWiki, disable Codex login-shell PATH rewriting, grant its external
  runtime read-only, and preflight its PyYAML/TLS dependencies inside an
  isolated read-only permission profile before creating a session.

## 0.2.1 - 2026-07-21

- Fix the managed Codex result schema to use the supported Structured Outputs
  subset while retaining strict host-side validation of returned artifacts.
- Fail closed before managed execution when the runner cannot protect the
  host-owned parent orchestration tree. Codex requires its supported 0.138+
  permission-profile interface, and managed Claude execution is unavailable on
  native Windows. Claude isolation requires `bubblewrap` and `socat` on
  Linux/WSL2 or `sandbox-exec` and `touch` on macOS.
- Define the semantic baseline as bounded **tripwire-protected controls**:
  workspace contract/instruction files, `scripts/`, `skills/`, `docs/`, and the
  current parent session. Ignore timestamp-only drift and report exact
  workspace-relative changes without automatically restoring or rolling back
  operator-visible files. Keep `.git/`, `.codex/`, `.claude/`, `.agents/`, and
  workspace virtual environments, plus `runs/orchestration-guards/`,
  preventively read-only without adding those roots to the post-action
  tripwire snapshot.
- Add bounded `orchestration_attempt` records, private staged-result recovery,
  never-submittable quarantined results, and a durable control-repair marker
  without retaining prompts, transcripts, diagnostics, secrets, or absolute
  paths. Retain the guard outside the parent session at
  `runs/orchestration-guards/<orchestration_id>.json`.
  `CONTROL_ARTIFACT_TAMPERED` records the tripwire failure, and
  `CONTROL_REPAIR_REQUIRED` blocks managed resume until explicit review;
  acknowledgement requires the saved tripwire-protected-control snapshot and
  fails with `CONTROL_REPAIR_MISMATCH` when it still differs or
  `CONTROL_REPAIR_BASELINE_MISSING` when no trustworthy baseline survives.
- Make managed recovery checkpoint-first: use an accepted canonical result, an
  identical clean staged result, or then the same persisted action in a fresh
  worker. Deterministic submission and trusted-input fingerprints remain
  authoritative.
- Serialize each managed parent session for its full drive and return
  `ORCHESTRATION_ALREADY_RUNNING` before launching a competing worker. External
  protocol hosts must provide equivalent session-wide coordination.
- Refuse overlapping retained attempts with `ORCHESTRATION_LEASE_ACTIVE`, fail
  malformed or expired absolute leases with `ORCHESTRATION_LEASE_INVALID` or
  `ORCHESTRATION_LEASE_EXPIRED`, and cap each runner timeout to the lease's
  remaining lifetime.
- Strengthen discovery and candidate-review immutability checks with a bounded
  content digest for up to 10,000 raw files / 2 GiB plus the exact record count
  and content digest of `sources/manifest.jsonl` up to 32 MiB.
- Forbid daemons, hooks, background jobs, and detached subprocesses in managed
  work orders. Clean up the runner process group while documenting that
  untrusted process trees require an operator-controlled container or VM.
- Move new generated run reports to `runs/run-reports/`; existing
  `docs/run-reports/` files remain historical read-only inputs.
- Bind legacy pending actions to controller-owned static-input fingerprints on
  their first replay, and recompute authoritative verification/export outputs
  before accepting completion.
- Bump the managed workspace starter to `0.5.2` with explicit parent-control
  ownership and recovery guidance for research, discovery, acquisition, and
  verification workers.
- Make managed Codex runtime visibility exact and portable: resolve the selected
  executable and official platform package outside the writable workspace,
  grant only its canonical runtime tree as read-only, and preserve the same
  permission profile for capability probes and worker actions. Correct the
  empty Claude MCP configuration document without weakening either runner's
  sandbox or approval policy.
- Require every research action to prove scoped question progress, enforce the
  remaining per-run question budget, keep review and acquisition bound to the
  persisted request/candidate IDs, and allow rediscovery after exhausted or
  unroutable candidates without mutating terminal child runs.
- Add durable pre-transport arXiv/OpenAlex request accounting, fail closed on
  corrupt run accounting, and derive status counters from discovery requests
  rather than acquisitions. GitHub and OpenAlex acquisitions now retain both
  request and candidate provenance for deterministic orchestration checks.
- Publish complete Draft 2020-12 schemas for orchestration sessions, work
  orders, results, and managed attempts while keeping the existing schema
  version map stable for compatibility.
- Keep exact pre-action question, request, candidate, manifest, raw, and
  normalized-evidence guards in protected controller sidecars, leaving public
  work orders bounded to 256 KiB. Validate sidecar identity and content before
  replay, reject every out-of-scope mutation, and accept acquisition only when
  fulfilled evidence is either an unchanged scoped match or a genuinely new
  provenance-linked source.
- Roll an exhausted immutable child run into a fresh child for remaining
  research, discovery, or acquisition work, including source-request budget
  exhaustion while actionable questions remain.
- Bump the managed workspace starter to `0.5.2`; the default upgrade refreshes
  managed scripts while preserving existing research configuration, evidence,
  and wiki content. Optional skills and documentation remain reviewable
  `--include` groups and are never replaced silently.
- Fail closed when a pending legacy research, discovery, candidate-review, or
  acquisition action lacks the phase-specific pre-action baseline needed for
  deterministic reconciliation. Preserve the old parent session and start a
  fresh orchestration; never reconstruct these baselines after worker execution.
  Active legacy runs without the new academic-provider accounting marker also
  require a fresh run so an unknown prior call count cannot reset to zero.

## 0.2.0 - 2026-07-20

- Bump the managed workspace starter to `0.5.0` and package the orchestration
  controller and shared provider registry as upgrade-managed scripts.
- Add durable parent orchestration sessions with model-neutral work orders,
  Codex and Claude managed runners, restart-safe status, and verified result
  submission across immutable bounded research runs.
- Add explicit discovery/acquisition provider flags, fail-closed discovery
  provider validation, and request-backed arXiv/OpenAlex academic discovery.
- Treat legacy `legal`, `authors`, and `companions` discovery entries as
  deprecated strategies rather than provider authority; migration is manual
  because upgrades preserve `research.yml`. Enabled discovery with no concrete
  provider is now a HIGH configuration error.
- Document the empty-source autonomous workflow, source-provider permissions,
  runtime credentials, and the local-files-only alternative.

## 0.1.0 - 2026-07-13

Initial standalone release of EvidenceWiki.

- Verifiable, provenance-backed research workspaces with deterministic question,
  source, citation, and publication-readiness workflows.
- The `evidence-wiki` CLI for workspace creation, upgrades, health checks,
  question intake, answer export, domain-pack validation, fleet status, and MCP
  serving.
- Reusable workspace template, domain packs, orchestrator guidance, and a
  synthetic worked example.
- Python 3.10+ support on Windows, macOS, and Ubuntu under the MIT License.
