# Library API

Version `13` adds an explicitly scoped `Onboarding` handle for discovery, setup,
pack lifecycle and current-agent research. `Workspace` authority is unchanged.
See [scoped onboarding](../workspace-template/docs/agent-contracts.md#scoped-python-onboarding)
for root/operation grants, lifetime, payload parity and error behavior. The
operation matrix is version `2`; `agent extensions` returns the optional MCP
schemas and lifecycle request shapes. Strict evidence and final export gates
remain authoritative across these surfaces.

Version `14` adds typed acquisition responsibility to saved setup requests.
`contract()["research_planning"]["orchestration_selection"]` advertises the
selector used by `Onboarding.plan`, `check_plan` and `apply`. See the
[complete delegated setup example](../workspace-template/docs/research-planning.md#complete-delegated-setup-example).
Old requests remain accepted; old installations reject the new decision field.
Plans bind installed code and resources, so restart hosts and recompile after
an upgrade rather than editing a saved identity or adopting a partial target.

Version `15` adds public strict review, source inventory, native normalization,
source requests and inline coverage authoring. The operation matrix is version
`3`. New source operations have finite traversal and input limits; supported
paths use pinned original bytes. Unsupported nested or external extraction
returns a typed refusal. Source intake and normalized usability remain separate
from authenticated claim acceptance.

Version `16` adds serialized mutation requests and historical receipts through
`ws.mutations`; the operation matrix is version `4`. The trusted host selects
a private control directory outside the workspace. Clients writing the same
workspace must share that selection to use the same serialization gate.

Version `17` adds controlled draft admission. The operation matrix is version
`5`. Admission writes an unaccepted claim inventory through the serialized
canonical owner; review and current release remain separate operations.

Version `18` adds explicit journal telemetry and host-selected delivery. The
operation matrix is version `6`. Measurements, declared subtotals and delivery
receipts confer no research approval; complete whole-execution coverage remains
unavailable in this profile.

Version `19` adds retained pending-work observations and durable reconciliation
or cancellation fallback. Job reservations use the selected workspace gate and
canonical run lock; their leases cannot exceed the original run allowance. The
operation matrix is version `7`.

Version `20` adds concise setup preparation and read-only recovery inspection
through `Onboarding`. The operation matrix is version `8`. Setup readiness,
frozen research scope compatibility and accepted evidence remain separate.

Version `21` adds explicit host-driven research execution, authenticated scoped
accounting and redacted execution reports, plus shared operation deadlines.
Execution-aware v2 envelopes preserve the legacy refusal profile. The host
supplies executor, qualification and observation services; current canonical
owners continue to control evidence admission and release.

Version `22` adds optional execution-profile scheduling and a deterministic
offline host. Existing profiles keep their v1 identities when scheduling is
absent. Scheduling pins aggregate limits for each role/controller across its
configured worker backends. Authenticated final accounting and frozen policy
permission are required before retry; unknown dispatch remains reserved.

Version `23` adds optional explicit job-role scopes, exact configured controller
selections and immutable blinded review contexts. Scoped v3 jobs bind task purpose,
capabilities and exact input bytes. Native authenticated signing controllers must
match their dispatched configuration. Legacy profiles retain their document identity.

Version `24` adds an opt-in protected calibration profile. Profile v4 requires
`CalibrationPlan` and `CalibratedEvaluators`; older qualifications are not promoted.
The supported reference domain is exact finite structured observations. Missing
or unsupported domain/configuration/model evidence remains `qualification_unknown`.

Version `25` adds optional `DisagreementPlan` in execution profile v5. Separate
Evidence and Critic first passes must cover every rubric criterion. Each claim
has at most two targeted repair cycles and one investigation, subordinate to
the frozen run allowances. Repairs preserve claim identity and peers, must
change relevant material, and require fresh first passes. Transport retries
retain their continuation cycle while every attempt remains charged.
An optional `adjudicator_config_ref` requires its own protected calibration and
current configuration observation, separately from required first-pass profiles.
Issue its calibration with `auxiliary=True`; an auxiliary record cannot qualify
a required vote. The investigator returns only scoped repair or abstention
findings and cannot register a review, approve a claim, or override native gates.
Unsupported source acquisition remains an explicit finite abstention until an
authorized acquisition route is available.

Version `26` adds `ws.research.requirements.current`, `prepare_revision` and
`revise`. Views retain every original and derived obligation, current native
dispositions, declared criteria and explicit unknown constraints. Open work is
reported as blocked/pending, rather than inferred to have abstained.
Revisions append exact preauthorized question/facet combinations and inherit
their existing inline criteria. They cannot delete originals or edit success
criteria, exclusions, dates, stopping rules or source authority.
The host grants `requirements_operations.revise` and
`requirements.envelope.<SHA256>` for the immutable envelope selected at startup.
Preparation returns an existing mutation request; submission retains the
private staged plan, historical receipt and versioned requirements lineage.
Interrupted prefixes remain ineligible until exact owned reconciliation.
Protected instruction hashes continue to bind the original bytes while the
full revised document changes the current basis and invalidates older sessions.
Template-only inheritance and runs with unverifiable outstanding execution
reservations are refused explicitly. Legacy unprotected base documents are
identified as host declarations. Neither a revision receipt nor its transport
status approves research evidence.

Version `27` adds core source capture document builders, `prepare_capture`,
`capture_revision` and `resume_capture`; the operation matrix is version `10`.
The explicit v2 delivery supports text, Markdown, JSON, CSV and PDF originals.
Capture metadata and exact bytes establish a retained revision, while inventory,
extraction, current rights, request fulfillment and semantic acceptance remain
separate outcomes. Existing v1 text delivery retains its contract.

Version `28` adds current request observations, compatible-scope links and
checked fulfillment/block/reopen mutations. Operation matrix `11` distinguishes
historical bookkeeping from current original, normalized, scope and permission
checks. Existing request APIs retain their legacy behavior.

Version `29` adds `sources.usability` and operation matrix `12`. Explicit v2
capture normalization supports text/Markdown, JSON, CSV and PDF with current
producer revision, retained originals and precise structured/page locations.
Extraction and stored consistency remain separate from current rights and
semantic claim approval.

Version `30` adds `sources.search` and serialized `sources.build_index`, with
operation matrix `13`. Search filters current permission and source usability
before ranking, excerpts and disclosed counts. Cache use checks native schema
and full current material identity; stale or unsupported caches return an
explicit lexical fallback. The shared schema matcher accepts finite native
numeric ranking scores without boolean or string coercion.

Version `31` adds `research.run_cycle` and `session.run_cycle`, with operation
matrix `14`. One immutable host request drives the existing authenticated run,
owned mutations and review/release gates to a finite terminal outcome. Optional
scoped planner proposals select only actions permitted by the deterministic
owner; planner output carries no effect authority.

Version `32` adds bounded native acquisition preparation, genuine capture
reconciliation, strict completion and explicit session choice/transport/workflow
methods, with operation matrix `15`. Original scope and aggregate acquisition
budgets remain tied to frozen instruction custody across runs. An authenticated
choice is inert until fixed native owners recheck scope, permissions and allowance.

Version `33` adds `strict.diagnose`, with operation matrix `16`. Read-only
per-claim native findings preserve source revisions, declared quote/anchor
locations, current usage and mechanical results without publishing or accepting
a claim. Admission and semantic review retain their owning gates.

Version `34` adds `session.examine_coverage` and
`session.examine_counterevidence`, with operation matrix `17`. A dedicated
coverage profile schedules both roles automatically in the canonical drive.
Examination work uses `research-session-next/v3`; existing next/v2 retains its
operation enum. Coverage proposals can only create mechanically checked links
through the native coverage owner. Independent critic findings retain bounded
queries, source slices and exclusions; unresolved objections stop release.
Current host model usage, calibration, source rights and the existing producer
attestation are required. Empty bounded search remains unknown.

Version `35` adds `research.calculate`, `research.propose_calculation` and
matching session methods, with operation matrix `18`. A scoped formula agent
selects exact native values and references; the shared computation engine owns
arithmetic, input lineage, units, rounding and invariants. Selected references
must appear in the same original question's inference before admission, review
or release. Calculation profiles use job/v4, review-context/v2 and next/v4;
previous profile encodings remain supported.

Version `36` adds `research.recovery_status`, `research.reconcile_recovery` and
matching session methods, with operation matrix `19`. Inspection returns current
holders, unresolved reservations, the original deadline and a bounded retained
effect page. Reconciliation observes exact existing receipts or question
postconditions under unchanged host controls, policy and execution identity.
It does not repeat model or fetch work, renew allowance, transfer foreign
ownership or reopen terminal runs.

Version `37` adds `strict.prepare_packet`, `strict.packet_schemas` and
`session.assemble_review_packet`, with operation matrix `20`. Optional
`ReviewDelivery` produces profile/v7 and freezes complete host delivery bounds.
Automatic Evidence and Critic attempts share one blinded context and exact
required pages. Current native evidence and permissions are checked on every
page access and before dispatch, signing and application. Host delivery does
not establish worker consumption, comprehension, semantic correctness or
accepted release.

Version `45` exposes checked local artifact delivery and receipt-only
reconciliation, with operation matrix `28`. Optional `ArtifactDeliveryPlan`
produces execution profile/v12. See [checked local delivery](#checked-local-delivery)
for destinations, grants, interrupted writes and historical status.

Version `47` adds `workspace.research.impact` with operation matrix `30`.
Current native strict evaluation owns eligibility; declared dependencies explain
known effects while whole-workspace review bases require conservative invalidation.

Version `48` adds permission-filtered retained decision history through
`workspace.research.history`, with operation matrix `31`. Historical native
records establish neither current eligibility nor semantic truth.

Version `49` adds bounded autonomous `workspace.research.refresh`,
`session.refresh` and `workspace.research.refresh_schemas`, with operation matrix
`32`. A fresh run supersedes an authenticated terminal parent without reusing
its acceptance or resetting its accounting.

Version `50` adds `research.schedule_refresh`, detached scheduling schemas and
explicit host schedule current/poll/advance operations, with matrix `33`.
No schedule registration starts a background daemon.

Version `51` adds `research.performance_schemas` and
`research.qualify_performance`, with matrix `34`. Performance qualification is
bounded synthetic native contract evidence, separate from scientific approval.

`evidence-wiki` can be driven two ways: by spawning the CLI, or by calling the
package in-process. This document is the contract for the second. It is aimed at
a host that embeds EvidenceWiki as an evidence layer inside a longer-lived
process — an ASGI service, a scheduler, a batch worker — and wants coverage
evaluation, quote verification, and the orchestration protocol without paying a
process spawn and a JSON round-trip per operation.

The subprocess boundary is not removed and is not deprecated. `evidence-wiki` the
command remains the supported way to drive a workspace from a shell, from a
Makefile, or from a host that wants hard process isolation. The API is the
*choice*, not the replacement, and the two doors render from one seam per
operation — see [seam conformance](#the-cli-and-the-api-cannot-disagree) — so a
host can move an operation from one door to the other without changing what the
operation means.

```python
import evidence_wiki
from evidence_wiki import Workspace

with Workspace.open("/path/to/workspace") as ws:
    status = ws.status()
    report = ws.coverage.evaluate("benchmarks")
```

Everything the API returns is a plain `dict` — exactly the document the matching
`--format json` command prints, freshly built and owned by the caller. Everything
it refuses with is an [`EvidenceWikiError`](#errors) carrying the same stable
`error_code` the CLI writes into its stderr envelope.

## Opening A Workspace

### Explicit host selections

An embedding host can select per-call scope and authority without changing the
process environment:

```python
from evidence_wiki.host_context import HostContext

context = HostContext(
    allowed_roots=[workspace_path],
    writable_roots=[workspace_path],
    grants=["coverage_manifest.run_evaluate"],
    actor_ref="configured-worker",
    authority_file=external_authority_path,
    state_directory=external_state_path,
)
with context.activate():
    report = ws.coverage.evaluate("benchmarks")
```

The application supplies this context from trusted configuration. Root and
operation declarations in model output are insufficient. Grants name canonical
`owner.seam` operations; onboarding grants use `onboarding.operation` and
intersect the handle's existing grants. Calls check their actual arguments and
effects. A context without a writable root cannot authorize a mutation.
Direct handle reads require `workspace.open` and `workspace.versions` grants.
Unknown operations and the deployed orchestration subprocess refuse under an
active context. With no active context, existing environment-based authority
and state selection remain compatible. Explicit absent selections suppress
those environment values.
Credential inspection under an explicit context reports the selected opaque
reference separately, with availability unknown until the host observes it.
It does not inspect another host's ambient credential presence. With no context,
the legacy presence check remains available. Defensive secret redaction still
scans ambient values to prevent accidental disclosure.

Selections are immutable and isolated across asynchronous tasks and threads
that activate their own context. A retained evidence view cannot move to a
different selection. Controller and credential references select configured
host capabilities; they do not authenticate a principal. Canonical signature,
role, controller-separation and revocation checks still apply. Signing keys
belong to the host, outside agent requests and results. `HostContext` is an
in-process boundary for trusted application code; execute untrusted workers
in a separately isolated host.
The host must prevent concurrent replacement of workspace paths during an
invoked legacy owner. The guard pins directory identities and rechecks them
after loading and execution; it is not an atomic filesystem transaction or a
substitute for operating-system isolation.

### Inert workflow contracts and policy decisions

`evidence_wiki.workflow_contracts` provides bounded, closed JSON schemas and
validation for agent jobs, results and decision envelopes. Result validation
binds a response to the original job identity and qualifications. Unknown
versions, fields, duplicate JSON keys and nonfinite measurements refuse.
Unavailable observed usage remains `null`.

`evidence_wiki.run_policy.RunPolicy` freezes a versioned policy under a
canonical content identity. Its `decide(observation)` method derives a
deterministic terminal outcome or bounded continuation from authenticated
canonical-owner observations supplied by the host. Exact question accounting,
current evidence, explicit selected claim references, qualified review and
resource limits are checked separately. Human-required policies return
`policy_incompatible`.

These modules neither dispatch work nor grant access, sign receipts or accept
evidence. Their `authorized` and `evidence_accepted` fields remain false even
when the policy outcome is `accepted`. Hosts must authenticate observations
and use the existing canonical acceptance owners.

`evidence_wiki.request_compiler.compile_request` accepts an ordinary UTF-8
objective, trusted host scope, a frozen `RunPolicy`, optional visible defaults
and an optional inert planning proposal. It returns the existing setup request
shape for `Onboarding.plan`, retaining exact original bytes and question anchors.
Missing mandatory context returns terminal `insufficient_context`. Every
authority action is intersected with its representable policy effect and
configured capability reference; source locations come only from host
declarations. Proposal confidence and semantic adequacy remain unverified.
Compilation performs no acquisition, model execution or workspace setup.

The scoped lifecycle handle composes compilation with the existing setup owner:

```python
from evidence_wiki import Onboarding

with Onboarding.open(allowed_roots=[setup_root], allow=["apply"]) as host:
    prepared = host.prepare(objective, host_scope=host_selections, policy=run_policy)
    if prepared["plan_ready"]:
        plan = prepared["plan"]
        host.check_plan(plan)
        applied = host.apply(plan)
        recovery = host.setup_status(plan)
```

Missing mandatory context is a terminal preparation outcome. An agent can use
the closed plan directly; no YAML editing or shell command is required.
`setup_status` inspects existing private coordination state and current owned
artifacts without creating directories, executing checks or renewing authority.
Its `recorded_setup_ready` is an archived local declaration; `setup_ready` stays
unknown until `apply` repeats the owning checks. Owned plans can be checked after
application. Changed controls, installation or unexplained workspace effects
return stale/conflict outcomes and preserve user bytes.

Preparation and status need their own `onboarding.prepare` and
`onboarding.setup_status` grants under an active `HostContext`. They require
read scope; application also needs `onboarding.apply`, the handle's apply grant
and writable scope. Entering the handle adds no hidden bootstrap permission.
New operations also have closed tools in the scoped onboarding MCP adapter.

Explicit local setup excludes ambient authority, state and provider credentials
from its fixed child workers while preserving the parent environment. Selected
protected state, authority, mutation control or a merged trust configuration
requires a separate worker bridge and refuses before journal/workspace effects
in this profile. The no-context legacy selection boundary remains compatible.
Fixed bounded tool-version probes and filesystem coordination do not establish
worker isolation. Research compatibility reports native admission scope gaps
separately: external/URI source extents, computation and multi-origin mappings
cannot be made acceptable by a successful setup.

`Workspace.open(path)` validates and never creates:

```python
from evidence_wiki import Workspace

ws = Workspace.open("~/research/solid-state-batteries")
print(ws.root)      # resolved, user-expanded
print(ws.closed)    # False
```

The path is `expanduser()`-ed and `resolve()`-d, must be a directory, and must
contain `research.yml`. Two refusals, both `ConfigError`:

| Condition | `error_code` |
|-----------|--------------|
| Not a usable directory, or a path that cannot be resolved at all (symlink loop, over-long path, `~` with no home) | `WORKSPACE_UNREADABLE` |
| A directory with no `research.yml` | `CONFIG_MISSING` |

Pointing at the wrong path is therefore a typed refusal, never a stray directory
tree. There is no programmatic `init`, `upgrade`, pack adoption, or pack refresh;
use the corresponding `evidence-wiki` CLI commands for those (see [what the API
does not expose](#what-the-api-deliberately-omits)).

`Workspace` is a context manager, and `with` is the shape to prefer — see
[handle lifetime](#handle-lifetime-and-close) for what `close()` does and,
importantly, what it does not.

## The Surface

Most operations hang off an open handle, in namespaces; the
exceptions are `Workspace.open` itself and the three module-level functions that
belong to no single workspace.

**`Workspace`** — the handle itself:

| Operation | Signature |
|-----------|-----------|
| `workspace.open` | `Workspace.open(path: str \| Path) -> Workspace` |
| `workspace.close` | `ws.close() -> None` |
| `workspace.versions` | `ws.versions() -> dict` |
| `workspace.status` | `ws.status(*, no_cache=False, run_id=None, **counters) -> dict` |
| `workspace.export_answers` | `ws.export_answers(status: list[str] \| None = None) -> dict` |
| `workspace.publish_selected` | `ws.publish_selected(question_slugs: list[str], *, expected_revision=None) -> dict` |
| `workspace.doctor` | `ws.doctor() -> dict` |

`ws.status()` takes nine optional counter keywords — `questions_processed_this_run`,
`source_requests_opened_this_run`, `releases_this_run`,
`discovery_results_this_run`, `acquisition_downloads_this_run`,
`github_archive_bytes_this_run`, `academic_provider_requests_this_run`,
`web_downloads_this_run`, `manual_url_deliveries_this_run` — each mirroring the
CLI flag of the same name one for one, plus `no_cache` and `run_id`.

**`ws.coverage`**, **`ws.grounding`**, **`ws.normalize`** — evidence checks:

| Operation | Signature |
|-----------|-----------|
| `coverage.evaluate` | `ws.coverage.evaluate(slug: str) -> dict` |
| `grounding.verify` | `ws.grounding.verify(slugs: Sequence[str], *, write=False, verified_by=None) -> dict` |
| `normalize.verify` | `ws.normalize.verify(source_ids: Sequence[str] \| None = None) -> dict` |
| `normalize.profiles` | `ws.normalize.profiles() -> dict` |
| `normalize.validate_packet` | `ws.normalize.validate_packet(source_id: str) -> dict` |
| `normalize.validate_execution` | `ws.normalize.validate_execution(source_id: str) -> dict` |
| `normalize.validate_market` | `ws.normalize.validate_market(source_id: str) -> dict` |

`coverage.evaluate` is not a read: the recomputed facet verdicts, coverage
verdict and `updated_at` are written back to `sources/coverage/<slug>.yml`,
exactly as the CLI leaves them. `normalize.verify(None)` means `--all`.

**Local source intake and coverage authoring:**

| Operation | Signature |
| --- | --- |
| `sources.inventory` | `ws.sources.inventory(*, dry_run=False, append_log=False, require_checksum=False, reject_mismatch=False, max_records=1000, max_entries=10000, max_input_bytes=16777216)` |
| `sources.usability` | `ws.sources.usability(source_ids: list[str]) -> dict` |
| `sources.search` | `ws.sources.search(query: str, *, source_ids: list[str], engine="auto", limit=10, excerpt_chars=1200, index_path=None) -> dict` |
| `sources.build_index` | `ws.sources.build_index(request: dict) -> dict` |
| `sources.capture_bytes_document` | `ws.sources.capture_bytes_document(content: bytes, metadata: dict) -> dict` |
| `sources.capture_path_document` | `ws.sources.capture_path_document(*, root, relative, metadata, expected_content_sha256, expected_bytes) -> dict` |
| `sources.prepare_capture` | `ws.sources.prepare_capture(*, request_id, path, delivery) -> dict` |
| `sources.capture_revision` | `ws.sources.capture_revision(request) -> dict` |
| `sources.resume_capture` | `ws.sources.resume_capture(request_id) -> dict` |
| `normalize.sources` | `ws.normalize.sources(source_ids=None, *, all_sources=False, dry_run=False, force=False, append_log=False, pdf_extractor=None, max_sources=128, allow_external=False, max_manifest_bytes=1048576, max_manifest_records=1000, max_entries=10000, max_input_bytes=16777216, operation_control=None)` |
| `source_requests.add` | `ws.source_requests.add(*, kind, query_or_identifier, rationale, priority="medium", question_slugs=None, scope=None)` |
| `source_requests.list` | `ws.source_requests.list(*, statuses=None, kinds=None, scope=None)` |
| `source_requests.fulfill` | `ws.source_requests.fulfill(*, request_id, source_id, match_scope=None, require_scope=False)` |
| `source_requests.prepare_acquisition` | `ws.source_requests.prepare_acquisition(*, request_id, alternative_ids=(), run_id=None)` |
| `source_requests.reconcile_acquisition` | `ws.source_requests.reconcile_acquisition(*, request_id, acquisition_ref)` |
| `source_requests.complete_acquisition` | `ws.source_requests.complete_acquisition(*, request_id, acquisition_ref, expected_request_basis, expected_material_basis, mutation_request_id)` |
| `source_requests.current` | `ws.source_requests.current(*, request_ids=(), question_slugs=(), max_records=200)` |
| `source_requests.link_current` | `ws.source_requests.link_current(request)` |
| `source_requests.fulfill_current` | `ws.source_requests.fulfill_current(request)` |
| `questions.block_current` | `ws.questions.block_current(request)` |
| `questions.reopen_current` | `ws.questions.reopen_current(request)` |
| `coverage.init` | `ws.coverage.init(slug, *, template=None, coverage_profile=None, force=False)` |
| `coverage.link` | `ws.coverage.link(slug, facet_id, *, accepted_source_ids=None, blocking_request_ids=None, clear_accepted_source_ids=False, clear_blocking_request_ids=False)` |
| `coverage.revision` | `ws.coverage.revision(request)` |

`source_ids=None` selects pending normalization; `all_sources=True` selects all
eligible records. A supplied ID list must be nonempty and excludes the all
selector. Failed native extraction remains report content. Fatal selection,
scope or resource-limit failures raise typed errors. Bounded normalization
currently refuses configured external extractors and execution/market closures;
qualified packet extraction also requires a supported snapshot bridge. Explicit
opt-in does not make those paths enforceable. Their legacy CLI routes remain
available under their existing contracts.

Capture builders consume the closed `evidence-source-capture-input/v1`
metadata schema exposed by `evidence-wiki agent source-schemas`. A delivery
binds origin, retrieval time, tool identity, rights declarations, scope,
completeness and original content identity. Those declarations confer no rights
or semantic approval. Inline content is limited to 49,152 bytes; an approved
path permits up to 512 KiB of text, 1 MiB of JSON/CSV or 8 MiB of PDF.
The path builder requires an active `HostContext` and the
`source_capture_contracts.from_path` grant, plus a permitted native source path.
It checks expected bytes and SHA before returning an inert descriptor; submission
rechecks that origin before retaining immutable execution bytes.

```python
delivery = ws.sources.capture_bytes_document(original_bytes, capture_metadata)
request = ws.sources.prepare_capture(
    request_id="capture-1", path="raw/papers/original.json", delivery=delivery,
)
receipt = ws.sources.capture_revision(request)
# Retain this exact request for replay and the receipt for historical inspection.
```

Publication requires selected private mutation custody, the existing mutation
prepare/submit grants and `source_capture_operations.deliver`. A source revision
binds both exact original bytes and immutable provenance. Different provenance
needs a separate immutable path even when content bytes are identical.
Capture returns `inventory` and `extraction` as `not_run`; request fulfillment
and evidence acceptance remain false. PDF signature validation establishes no
extraction fidelity.

Provision `source_capture_operations.resume` plus mutation inspect/reconcile
grants in the same original host selection if pair repair will be needed.
`resume_capture` only completes its retained sealed outputs. It preserves the
original partial receipt and reports desired-state proof separately from unknown
original completion. It cannot adopt an orphan original, overwrite foreign
bytes or refetch a missing source. Protected sources require the existing signed
usage deposit, scrub and ancestry route; capture metadata cannot supply authority
for ordinary raw publication.

Capture preparation and publication share the native raw acquisition barrier
with inventory and legacy delivery. Lock order is workspace/request custody,
then raw acquisition custody. Provenance is published before the original, so
an interrupted pair leaves no unprovenanced original for inventory to consume.
Contention refuses before original publication. The host must inspect retained
state; a prepared checkpoint remains unknown under the general mutation
contract and exact resubmission does not automatically repeat preparation. Lock
directories and the empty coordination file may be retained even when a new
capture cannot complete. Known conflicting or orphan pairs refuse before
creating that coordination state. Named directory/lock identity is rechecked
throughout pair publication and desired-state reconciliation.

Current request observations require an active `HostContext`, selected private
mutation custody and `request_lifecycle_operations.current`. They bind original
and derived question metadata to the effective frozen requirements. Inline
criteria and their declared scope remain unchanged. Missing or unsupported
requirements produce a typed refusal; unknown sources and unprocessed records
remain gaps. Requests keep their native `open`/`fulfilled` statuses while
`current_ready` separately checks current originals, normalized records, scope,
complete context and current usage. Permission compatibility for ordinary
legacy evidence is labeled separately from authenticated native usage revisions.
Neither route supplies semantic approval.

Prepare checked mutations through `ws.mutations.prepare` and retain the exact
request for replay:

```python
request = ws.mutations.prepare(
    request_id="fulfill-1", operation="source_requests.fulfill_current",
    arguments={"request_id": evidence_request_id, "source_id": source_id},
)
receipt = ws.source_requests.fulfill_current(request)
current = ws.source_requests.current(request_ids=[evidence_request_id])
```

The four operation argument shapes are:

- `source_requests.link_current`: `request_id`, `question_slugs`, `scope=None`.
- `source_requests.fulfill_current`: `request_id`, `source_id`,
  `match_scope=None`, `require_scope=True`, `needs_complete=True`.
- `questions.block_current`: `slug`, `blocking_request_ids`, `reason`.
- `questions.reopen_current`: `slug`, `needs_complete=True`.

Each requires its exact `request_lifecycle_operations.<operation>` grant, the
existing mutation prepare/submit grants, and the canonical inner seam grant.
Linking uses `source_requests.run_link_request`; ordinary fulfillment uses
`source_requests.run_fulfill_request`; protected fulfillment uses
`source_requests.run_fulfill_current_request`. Question transitions use their
existing `question_resolve.run_block`/`run_reopen` grants and the selected actor.
Blocking enforces the native question claim; it cannot select another actor or
silently use an unclaimed question.

Linking preserves exact declared scope and native managed-order freezes.
Fulfillment refuses missing, partial, unusable, stale, denied or out-of-scope
sources. `require_scope=False` is invalid; `needs_complete=False` cannot weaken
frozen requirements in this profile. Reopening checks every page and coverage
blocker, including missing or foreign request references. Native prewrite checks
repeat current source and scope checks while canonical request/question locks
are held. Protected use holds the native usage view through bookkeeping and
revalidates trust and expiry immediately before writing.

Completed replay retains the original historical receipt even after deletion or
revocation. Always request a new current view before treating evidence as ready.
An interrupted owner result remains partial or unknown under the mutation
contract; absence of a receipt does not prove non-execution. Current readiness
and transport completion never imply accepted claim support or release.

For supported v2 captures, use the existing inventory and normalization owners
explicitly, then observe usability without starting extraction:

```python
ws.sources.inventory()
ws.normalize.sources(source_ids=[source_id])
view = ws.sources.usability([source_id])
source = view["sources"][0]
rights = view["current_rights"][0]
```

Hosts that need retained transaction outcomes can prepare `sources.inventory`
and `normalize.sources` through `ws.mutations` instead. No source request is
fulfilled by normalization. Current usability requires the
`source_usability_operations.current` and `normalize_verify.run_verify` grants,
1–32 unique source IDs, at most 32 MiB of selected input and 16 MiB per file.
The returned view is limited to one MiB. Host path denials remain fatal before
verification can reread selected evidence.

JSON retains exact parsed data under `/data/<JSON pointer>`. CSV retains exact
scalar cells under `/rows/<zero-based logical row>/<column>`, with its header
and multiline cells. PDF retains parser-reported page text under
`/pages/<zero-based page>/text` and explicit one-based page locations. Only
offered measurement shapes with `value` and `unit`/`units` receive a missing-unit
gap; arbitrary numeric fields receive no scientific or units inference.

Known finite authentication/error-only text, closed structured error envelopes
and all-error CSV rows remain unusable. Mixed measurement/error data is not
rejected just because it contains an error word. This syntactic check does not
detect every authentication message or establish semantic source suitability.
Incomplete captures, partial rendered tables, OCR requirements, malformed
originals and failed extraction retain precise gaps and original bytes.

PDF extraction uses the explicitly selected available native backend once.
Read-only verification checks retained original identity, pages, sidecar and
body consistency without rerunning the parser. `mechanical_stored_consistency`
is not independent PDF glyph extraction fidelity. Unavailable capabilities are
reported finitely and are never installed implicitly; declared host titles
remain separate from parser-derived titles.

V2 normalized records carry `host_capture_normalization_version=1`. A missing
version requires an explicit normalization refresh; future or invalid versions
refuse current readiness and replacement before writes. Legacy v1 output keeps
its existing contract. Only the exact inventory deferral for normalization not
yet run is retired after current successful native validation; unrelated refusal
reasons and explicit denials remain intact.

The view reports usable, partial or not-ready content, aggregate stored consistency
and current rights separately. Native signed eligibility binds its exact usage
revision; ordinary legacy compatibility keeps that revision null. Revocation
changes current eligibility without rewriting retained evidence. Permission
to retrieve a protected normalized revision does not grant raw normalization.
`authorized`, `evidence_accepted` and `claim_support_eligible` remain false;
strict claim/release owners still perform their own checks.

Read bounded current evidence without invoking configured providers or building
an index:

```python
view = ws.sources.search(
    "measured reflectance", source_ids=[source_id], engine="lexical",
    limit=10, excerpt_chars=1200,
)
for result in view["results"]:
    text = result["excerpt"]["text"]
    revision = result["source_revision"]
```

Search requires an active `HostContext` and `retrieval_operations.search`.
Selectors contain 1–32 unique IDs. Query length is at most 4,096 characters;
result count is 1–32 and exact excerpt width 128–4,096 characters. Selected bytes
are bounded to 32 MiB, individual source/index files to 16 MiB and output to
one MiB. `status` distinguishes `found`, `empty`, `unknown` and `truncated`.
Coverage counts only permitted selected material; excluded details and material
commitments are withheld. Empty matches do not establish exhaustive absence.

Results retain exact normalized-body character/line offsets and omitted-prefix
or suffix flags. Original document/page/table locations are not inferred from
those offsets; the source's retained structured/page anchors remain the original
location evidence. Local `source_revision` binds source ID, original/provenance,
normalized bytes and actual scope; `usage_revision` is separately the signed
native permission revision. Mechanical retrieval and matching words never
supply semantic approval.

`engine="fts"` without a path uses an eligible-only in-memory native FTS schema.
With `index_path`, cached bytes are copied to a bounded immutable read-only
snapshot, checked against exact native SQLite schema, selected material hashes
and current document content. Unknown schema, stale content, malformed caches
or unavailable hard-limit capabilities fall back visibly to lexical search.
Denied cached rows cannot affect snippets, disclosed document counts or ranking.
Search writes no index and never invokes the query command's provider/cache path.

Build a cache explicitly through the existing mutation owner:

```python
request = ws.mutations.prepare(
    request_id="index-1", operation="sources.build_index",
    arguments={"source_ids": [source_id]},
)
receipt = ws.sources.build_index(request)
view = ws.sources.search(
    "reflectance", source_ids=[source_id], index_path=receipt["result"]["path"],
)
```

Closed build arguments are `source_ids`,
`index_path="sources/indexes/evidence.sqlite"` and `expected_sha256=None`.
Only approved `sources/indexes/*.sqlite` paths are accepted. The host grants
`retrieval_operations.build_index` plus existing mutation prepare/submit and
workspace opening permissions. Publication requires live writable mutation
custody, the canonical zero-wait index lock and prior-content CAS. An exact
already-desired file is a native safe no-op; conflicting prior content refuses.
SQLite serialization capability is checked before any cache output. Protected
persistent indexes are unsupported in this profile; permitted protected FTS
can remain in memory while the native usage view is held and revalidated.

Index replay returns its original historical mutation receipt. Cache contents
confer no permissions, claim approval or accepted release. Partial or uncertain
publication remains unknown under the generic mutation protocol; search never
repairs or rebuilds it implicitly. Historical read custody narrows all checked
writer calls, including legacy/no-context routes and mixed cache-writing status
operations, until the reader exits; its parent writer permission is then restored.

Acquisition observations use the existing request, capture and fulfillment owners:

```python
prepared = ws.source_requests.prepare_acquisition(request_id=source_request_id)
current = ws.source_requests.reconcile_acquisition(
    request_id=source_request_id, acquisition_ref=capture_mutation_id,
)
result = ws.source_requests.complete_acquisition(
    request_id=source_request_id, acquisition_ref=capture_mutation_id,
    expected_request_basis=current["request_basis"],
    expected_material_basis=current["source"]["material_basis"],
    mutation_request_id="complete-capture",
)
```

Read preparation never runs fetch planning or writes a log. Without a bound
execution session its allowance requires a selected-session recheck. Genuine
private capture intent, receipt, root, current host selection and exact sealed
delivery/result identity are verified before current source scope/usability.
Completion submits the existing strict fulfillment mutation. Its retained outer
binding shares that mutation's private intent, not a new lifecycle store.
Historical fulfillment remains distinct from current evidence usability; deleting
an original does not erase or upgrade the old receipt.

A configured session exposes `prepare_acquisition`, `choose_acquisition`,
`acquire`, `acquire_evidence` and `run_acquisition`. Preparation is read-only and
observes actual remaining original/per-run allowance. Choice requires an exact
researcher scope with `model` and `acquisition.propose`, configured controller,
current evaluation qualification and authenticated output/usage. Missing,
malformed or stale choice results are finite refusals; completed refused attempts
are settled durably and cannot enter draft admission.

`acquire` performs one explicit configured native HTTPS GET. The frozen original
instruction binds URI scope, original download/byte/source-request/time limits;
configured provider and run limits tighten them. Initial, redirect and final URLs
are checked against original scope before unauthorized response bodies are read.
Maximum-byte reservations are spent before transport in the authenticated native
run journal, including failed attempts. Request-wide history survives a fresh run
and authorized derived-question appends, while provider caps remain per-run.
Original time uses the signed baseline and sealed reservation clock. Unknown old
custody refuses rather than becoming zero consumption. Gates are released during
transport; exact retained replay does not refetch unobserved work.

`acquire_evidence` joins permitted transport to immutable capture, inventory,
normalization and strict current fulfillment. Host-provided capture metadata
must declare exact origin, request, scope, completeness and rights; HTTP success
cannot manufacture those facts. Native refusals and partial capture/normalization
retain useful gaps and leave the request open. Known complete authentication
notice wrappers cannot become full evidence through a plain-text declaration.
Capture, permission, source usability and semantic approval remain separate.

```python
result = session.run_acquisition(
    request_id="acquisition-1", instruction=closed_acquisition_instruction,
    config_ref="configured-researcher", metadata=host_capture_metadata,
    path="raw/web/acquired.txt",
)
```

The composed route selects one authenticated inert choice and applies only the
supported native-web alternative for the request's exact literal URL. Other URLs,
connectors or unavailable protected sanitization produce explicit finite results;
no ungranted connector or rights are inferred. This route starts no fresh run to
reset original budgets. Every transport, capture and fulfillment effect is retained
through its existing owner, including interruption recovery and exact replay.

`evidence_wiki.acquisition_contracts.schema_document` exposes closed preparation,
transport, reconciliation, fulfillment, agent and composed-workflow result shapes.
Transport encoding validates exact bytes, count and SHA; all source-accepted and
rights-inferred flags remain false. Local execution/reference fixtures qualify
mechanics, not live remote-model quality, billing or general semantic truth.

New source and authoring seams qualify actual configuration, store, question,
output and lock paths before effects. A linked workspace folder cannot redirect
an operation outside its owning workspace. The host still owns concurrent
filesystem coordination and outer operation timeouts.

Some refusals follow durable effects: checksum filtering can replace the admitted
manifest before refusal; coverage backfill can fail after the facet write; a log
failure can follow a request mutation. Inspect canonical state before retrying.
`COVERAGE_WRITE_UNCERTAIN` and `REQUEST_MUTATION_UNCERTAIN` retain an unknown
commit outcome instead of implying rollback.

**`ws.strict`** delegates authenticated review to the canonical strict owner:

| Operation | Signature |
| --- | --- |
| `strict.check` | `ws.strict.check()` |
| `strict.admit` | `ws.strict.admit(proposal, *, request_id, expected_revision)` |
| `strict.prepare_review` | `ws.strict.prepare_review(claim_id)` |
| `strict.prepare_packet` | `ws.strict.prepare_packet(claim_id, *, context_id, task_role="evaluator", source_ids=(), page_chars=4096)` |
| `strict.packet_schemas` | `ws.strict.packet_schemas()` |
| `strict.register_review` | `ws.strict.register_review(envelope)` |
| `strict.review_status` | `ws.strict.review_status(claim_id=None)` |
| `strict.machine_compatibility` | `ws.strict.machine_compatibility()` |
| `strict.supersede_review` | `ws.strict.supersede_review(envelope)` |
| `strict.revoke_source` | `ws.strict.revoke_source(envelope)` |
| `strict.export` | `ws.strict.export()` |

Preparation supplies the bound review inputs; the external host signs the
envelope. Controller strings and model verdicts cannot authenticate it. Current
policy, role, controller separation, source basis and revocation still determine
acceptance. These methods consume existing canonical claim inventories; a new
claim proposal is not admitted by registering its review. An explicit context
uses separate `strict_evidence.run_operation.check`, `.prepare-review`, `.review`
and `.export` grants; registration additionally requires writable scope.

`review_status` recomputes current native approval with a held authority view.
It selects the latest signed review matching the current claim basis; a later
failed or unknown review supersedes an earlier pass. Changed material, expired
attestations or current key/envelope revocation invalidate retained approval.
`supersede_review` registers another complete signed envelope through the same
owner, rather than deleting or editing history. `revoke_source` accepts only an
existing signed `revoke` command and delegates to `usage.transact`. Host key and
envelope revocation remain changes owned by the external authority service;
these methods do not write its policy. Legacy assessments confer no strict review.

`machine_compatibility` checks the current strict policy and actual coverage
contracts, including conditional manual outcomes and unlinked human-only rules.
Known machine rules may permit unfinished evidence; missing or invalid contracts
remain unknown and stop new machine work. Execution checks compatibility before
new model reservations, qualification probes and complete packet assembly.
An incompatible contract produces `policy_incompatible` with retained evidence.
The host signer never constructs `register-strict-human-review` from model output,
and the library never rewrites policy to remove a human requirement.
Use explicit `.review-status` and `.machine-compatibility` read grants under
`strict_evidence.run_operation`; source revocation requires
`evidence_usage.run_transact`, writable scope and a currently authorized signed
revocation principal. Six rubric criteria, exact claim/basis and independently
controlled current generator/evaluator attestations still decide approval.

Draft admission requires an explicit selected mutation control directory,
`strict_evidence.run_admit_document` and coordinator grants. Supply the revision
from `ws.mutations.revision()` as a precondition. The closed draft proposal
contains the complete strict claim inventory and its exact original-question
mapping. Source references bind actual current normalized bytes and retained
capture timestamps; quotes and anchors are mechanically checked. The target is
the configured strict claims path, with no caller-selected destination.
The workspace must match the frozen request target and writable roots. Selected
originals and companions must stay within that request's declared native
absolute file/directory extents inside the workspace. URI, relative and external
source extents currently refuse; broader active host grants cannot expand the
original request scope.

Admission stages exact candidate bytes before publishing that one inventory.
It creates no review, answer status or approval. Unknown, stale, oversized,
cyclic or unrepresentable original/source mappings refuse; partial publication
retains an explicit recoverable desired-state distinction. The public
`evidence_wiki.draft_contracts` codec exposes the proposal schema and detached
validation, encoding and decoding functions.

New publication rechecks current source age and usage permission immediately
before replacement, even when hashes and authority checkpoints are unchanged.
The report's `admitted_at` is the retained plan validation clock; it does not
reconstruct actual completion time. Historical receipts and proof of matching
inventory bytes cannot establish current eligibility.

`contract()["draft_admission"]` describes the offered admission profile. Its
codec recognizes strict inventory schemas, but configured computation,
calculation-bearing claims and questions derived from several originals are
currently unsupported and refuse before publication. Schema decoding alone
does not imply admission capability for those cases.

**`ws.mutations`** freezes and submits supported canonical owner operations:

| Operation | Signature |
| --- | --- |
| `mutations.revision` | `ws.mutations.revision()` |
| `mutations.prepare` | `ws.mutations.prepare(*, request_id, operation, arguments)` |
| `mutations.submit` | `ws.mutations.submit(request, *, operation_control=None)` |
| `mutations.inspect` | `ws.mutations.inspect(request_id)` |
| `mutations.reconcile` | `ws.mutations.reconcile(request_id, *, operation_control=None)` |

Activate a `HostContext` with `mutation_control_directory` pointing to an
existing private directory outside workspace, authority and evidence state
storage. Grants use `mutation_operations.revision`, `.prepare_request`,
`.submit`, `.inspect` and `.reconcile`; dispatch also checks the canonical
owner's grant and writable workspace scope. Requests cannot select storage or
supply credentials. Prepare binds workspace bytes, captured input files,
installed owners and trusted host selections. Reusing an ID with different
inputs returns a conflict.

The workspace gate precedes the request gate and existing canonical owner
locks. Selecting mutation control custody requires coverage and question-intake
writes to run through the coordinator's held native workspace gate. Direct
writes outside that gate refuse before effects. Contexts without that selection
retain the declared legacy host-serialization contract. Other underlying facades
retain their operation-specific locking contracts. A durable execution checkpoint precedes owner effects;
pending work is inspected before retry. Exact replay returns the retained
historical outcome before checking live revision preconditions. Receipts are
inert: they do not confer current permission, accepted evidence or export
eligibility. Interrupted multi-artifact writes can retain an explicit unknown
outcome; reconciliation preserves those artifacts and never blindly dispatches
them again.

The older compound `Onboarding.research(..., "ingest", ...)` route refuses when
mutation control custody is selected. Submit its supported normalization,
request-fulfillment and coverage primitives through the coordinator instead.

Reconciliation records are separate from the original receipt. Exact replay
continues to return that original outcome even after later inspection resolves
uncertainty. A retained owner report proves that it returned; its partial or
failed outcome still applies. Exact current postconditions can prove the desired
state without reconstructing the original execution time. Read those distinctions
from the reconciliation record before advancing a workflow.

Inventory, normalization, coverage evaluation and coverage revision retain their
canonical output plans before effects. Reconciliation compares complete output,
deletion, activity and dependency obligations against those retained bytes. A
completed interrupted write can prove the desired state without invoking its
owner again. Missing or conflicting obligations remain explicit partial or
unknown outcomes. The API provides no global rollback or blind retry.

The staged native profile requires separate generated source output paths and
workspace-relative coverage configuration. Outputs cannot replace raw originals,
questions, configuration or executable files. Unsupported overlapping input/output
trees and plans outside the declared bounds refuse before effects.

`contract()["serialized_mutations"]` exposes supported operation names and the
closed schema documents for host negotiation.

`ws.strict.diagnose(proposal)` validates a closed unaccepted draft and returns
`evidence-claim-diagnostics/v1` through the same native source checks used by
serialized admission. The host grants `claim_diagnostics.current` and
`strict_evidence.run_operation.check`; direct native reads still check every
captured host path. The method performs no publication, question transition,
review registration or source normalization.

The full proposal, original/derived mapping, instructions and inference DAG are
validated first. Malformed inventories and cyclic/forward premises refuse as
structural inputs. Evidence-specific failures retain valid peer claim findings,
so a generating host can correct wrong revisions, source age, missing inventory,
quote mismatch or invalid locations without losing the whole report.

Quote occurrence and native structured-pointer equality are mechanical results.
Negation, compound statements and irrelevant passages still require independent
semantic review; matching words confer no approval. The response retains claim
qualification, scope/time/units, premise IDs and each declared source record SHA.
`location_hint` and `anchor_pointer` identify caller-supplied locations;
`anchor_pointer` is not an independently verified original-document coordinate.
Expected/resolved source values, source text and incidental native error messages
are withheld. Current native usage revision is separate from record identity.

`basis_id` commits caller proposal material and only permitted mechanical/usage
facts. The full workspace revision stays private for closing revalidation;
changing denied source bytes cannot expose a public commitment to that material.
Source age, current native usage/trust and exact captured inputs are checked
again before return. Unknown or denied evidence stays unestablished, and
`accepted`, `published` remain false with `semantic_approval="not_evaluated"`.

The bounded profile supports up to100 claims/16 evidence references per claim and
one MiB output. Declared local extents remain mandatory even when valid HTTPS
acquisition declarations coexist; a URI grants no local file access. Nonlocal
source-locator kinds, computation admission and multiple-origin derived mapping
remain explicit unsupported profiles. Valid v2 capture inventory retains exact
UTC timestamp representation and fractional precision from its immutable metadata.
The schema is available through `evidence_wiki.claim_diagnostics.schema_document()`.

**`ws.research`** provides current accounting and explicit bounded sessions:

| Operation | Signature |
| --- | --- |
| `research.current` | `ws.research.current(policy, *, run_id=None, execution=None)` |
| `research.next` | `ws.research.next(policy, *, run_id=None, execution=None)` |
| `research.run_cycle` | `ws.research.run_cycle(policy, *, run_id, execution, request_id, expected_basis_id=None, max_steps=64, planner_config_ref=None)` |
| `research.session` | `ws.research.session(policy, *, run_id, execution=None)` |
| calculation observation | `session.calculate(*, request_id, as_of=None)` |
| formula interpretation | `session.propose_calculation(*, request_id, original_id, config_ref, as_of=None)` |
| complete review assembly | `session.assemble_review_packet(*, request_id, claim_id, task_role="evaluator", page_chars=4096)` |
| recovery observation | `session.recovery_status(*, after_sequence=0, max_effects=32)` |
| retained-effect reconciliation | `session.reconcile_recovery(*, request_id, expected_basis_id)` |
| session observation | `session.current()` and `session.next()` |
| session advance | `session.advance(*, request_id, expected_basis_id)` |
| bounded terminal route | `session.run_until_terminal(*, request_id, expected_basis_id, max_steps=1)` |
| host work cycle | `session.run_cycle(*, request_id, expected_basis_id=None, max_steps=64, planner_config_ref=None)` |
| inert host job | `session.host_job(*, request_id, operation, payload, original_text=None)` |
| retained pending work | `session.pending() -> dict \| None` |
| finite reconciliation | `session.reconcile_pending(*, request_id)` |
| cancellation request | `session.cancel_pending(*, request_id)` |
| authenticated history intake | `session.intake_observation(observation, *, request_id)` |
| execution metadata | `session.report(*, after_sequence=0, max_records=64)` |
| current delivery identity | `session.prepare_delivery()` |
| checked local publication | `session.deliver(*, request_id, proposal)` |
| historical delivery status | `session.delivery_status(*, request_id)` |
| receipt-only delivery reconciliation | `session.reconcile_delivery(*, request_id)` |
| session lifetime | `session.close()` or a context manager |

These operations require an active `HostContext` with selected mutation control
storage and explicit `research_session` grants. A session binds the selected
workspace identity, actor, host context, original questions and immutable
`RunPolicy`. Closing its parent workspace invalidates the session. Advice
remains inert; it reports the required canonical lifecycle grants before an
explicit step can create a run. Decisions and jobs use the existing run event
journal, with immutable request replay.

Calls without `execution` preserve the finite refusal profile: unavailable
usage or qualification cannot become accepted research. Supplying an explicit
`ResearchExecution` selects a host-driven route through the same canonical run,
source, admission, review and publication owners. It performs bounded generation,
independent evaluation, permitted repair and current release without interactive
approval. Missing capabilities or evidence produce explicit finite outcomes.
Current canonical release cannot supply missing evaluator qualification.

The integrating host supplies an immutable `ExecutionProfile`, an executor,
`QualifiedEvaluators`, `HostReviewSigner` and a stable protected observer key.
The library has no mandatory model SDK and discovers no credentials or provider.
`BoundedSubprocessExecutor` provides finite invocation, polling, process-group
cancellation, output limits and host-authenticated receipts. A host usage meter
must observe model calls, tokens and incremental charges independently of worker
output; unavailable measurements remain unknown. These execution bounds do not
establish malicious-worker OS isolation.

The accepted source profile starts with host-provided, approved normalized
evidence and its complete raw/provenance closure. Native local inventory and
normalization remain owning operations. Protected pending captures require the
host's established sanitization/materialization route; the driver retains the
precise owner refusal and terminates when that capability is unavailable. It
never creates a usage grant or treats a normalization retry as permission.

```python
from evidence_wiki import ResearchExecution, Workspace

execution = ResearchExecution(
    executor,
    qualifications=qualified_evaluators,
    review_signer=protected_review_signer,
    observer_key=protected_observer_key,
)
with host_context.activate(), Workspace.open(workspace_path) as workspace:
    with workspace.research.session(
        frozen_policy, run_id="research-run", execution=execution
    ) as session:
        result = session.run_until_terminal(
            request_id="research-drive",
            expected_basis_id=session.current()["basis_id"],
            max_steps=64,
        )
        metadata = session.report()
```

All services and permissions in this example come from trusted host provisioning.

A host can compose the same journey in one call:

```python
result = workspace.research.run_cycle(
    frozen_policy, run_id="research-run", execution=execution,
    request_id="research-drive", max_steps=64,
    planner_config_ref="configured-planner",  # omit for deterministic selection
)
```

Restart with the same policy, run ID, host context and `ResearchExecution`.
Grant `research_session.recovery_status` for inspection and
`research_session.reconcile_recovery` for explicit reconciliation. The facade
methods accept `policy`, `run_id` and `execution` and delegate to this same
session owner. `after_sequence` is a full native journal sequence cursor;
`max_effects` is between 1 and 64. `truncated` and `next_sequence` identify the
remaining effect page. Status values are `not_started`, `reconcilable`,
`ownership_blocked` and `terminal`; advice is never an authorization.

Changed instructions, policy or private control selection refuse the original
binding. Use a correctly authorized new run or requirement revision rather
than altering the retained run. Unknown external work stays reserved for its
original pending owner. A later exact native receipt can settle historical
unknown custody, preserving the earlier unknown event and original deadline.

The host grants `research_session.run_cycle` plus the existing session,
current-view, advancement, intake and native lifecycle/effect grants. A selected
planner requires an exact `planner` role scope with `model` and `plan.propose`,
its configured native host-worker controller and actual authenticated execution.
It receives bounded permitted evidence and exact references; proposals are
intersected with current canonical actions, policy and source bindings before
use. Claim-specific planning retains only its declared evidence dependencies.
Planner empirical semantic qualification remains unknown; it cannot review,
accept, sign or release claims.

Current source packets pass the retrieval owner's usability, path and native
permission checks before worker content is exposed. Generation receives exact
bounded normalized-body excerpts and explicit missing context. Independent
review retains its complete-context requirement. Material closure identity
remains separate from current native usage authority.

`research-work-cycle-result/v1` is available through
`evidence_wiki.cycle_result_contracts.schema_document()`. Results distinguish
attempted work, current native acceptance, native rejection/abstention, missing
question mappings and unattempted originals. The historical terminal step and
current release are separate observations; replay can retain an old outcome
while today's evidence is incomplete. `authorized` and `evidence_accepted`
remain false. An optional initial basis is frozen on first use and reused by the
same request; changing bounds or planner conflicts.

Exhausting the drive bound closes through canonical terminal and accounting
owners. Unknown external completion remains reserved; unavailable cleanup
permission does not imply cancellation or rollback. A live managed
orchestration refuses caller-cycle effects before starting a caller run.
ResearchExecution's existing retry, repair, qualification and release limits
remain in force; this call creates no competing lifecycle or default provider.


`ExecutionRoleLimit` and `ExecutionSchedule` optionally freeze aggregate role
limits, allowed failure reasons, a finite backoff and explicit worker fallback
chains. The role budget spans every configured backend. Actual model/prompt
identity is retained for the selected backend. Evaluator fallback requires a
qualified identity mapping and is currently refused by this scheduling profile.
Backoff and predecessor identity remain in the canonical run history; restarting
cannot reset consumption or refresh the original run deadline.

`ExecutionRoleRegistry` assigns each configured worker an immutable
`ExecutionRoleScope`. Task roles are `planner`, `researcher`, `evaluator`, `critic`,
`adjudicator`, `repair`, `integration` and `release`. They propose structured work;
they grant no canonical write, signing, acquisition or publication authority.
Every configured worker must have an exact scope when `ExecutionProfile.role_scopes`
is supplied. A repair requires a dedicated repair configuration. Scoped v3 jobs
cannot be downgraded to an unscoped envelope or relabeled as another role.

`HostContext.role_controller_refs` supplies `(config_ref, native_role, controller_ref)`
triples; reviewer and critic configurations both use native role `evaluator`.
An explicit mapping refuses omitted configurations rather than using a generic
role fallback. Native principal authentication separately establishes the actual
controller; matching reference strings alone does not authenticate an agent.
The parent matches those authenticated signing controllers with the dispatched
profile before registering a review and during replay.

First-pass reviews receive `ReviewContext` packets containing the exact question,
claim, premises, allowlisted scientific metadata, authorized source content and
rubric. Opaque aliases mask generator/source identifiers; parent-retained seeded
ordering and full owner/source commitments make reconstruction repeatable. Missing
or truncated decisive evidence remains incomplete. Computational and missing
premise-question context currently refuse completion in this projection.

Provider adapters must send `ExecutionAttempt.worker_document()` for scoped jobs.
The subprocess host uses this projection automatically. Its worker envelope omits
parent run/request/binding identities, native scope, original accounting and
signing/registration material. `document()` remains the full parent correlation
record. A trusted adapter that ignores this contract is outside the isolation
claim; process bounds alone do not restrict malicious filesystem or network access.

`isolation_report` separately reports authenticated controller references, immutable
context identities and observed model references. The calling owner authenticates
receipts before reducing them. Credential custody, host conversation isolation,
model-family diversity and semantic reliability remain unknown. Profile
`required_diversity` can require distinct selected controller/model references and
fresh bound contexts; unavailable selections block the run. These are bounded
identity checks, not proof of independent reasoning.

`DeterministicOfflineExecutor` consumes explicit host fixture outputs under
reserved `offline/` model identities. It measures actual local elapsed work and
reports zero provider calls, tokens and charge. Lost work remains unknown.
Offline fixtures cannot establish semantic evaluator qualification.
An execution profile pins role/controller, model and prompt identities, pricing,
finite total steps and per-job ceilings. Consumed steps and reservations survive
restart. A bounded call can yield a nonterminal result; the host can drive the
next slice under the original run deadline and remaining allowance. A yield is
not completed research. Reusing an exact request inspects retained work rather
than dispatching a second model call.

`QualificationAuthority` derives scoped evaluator metrics from protected reference
cases and authenticated execution observations. Qualification binds the exact
policy, model/settings, prompt, controller, scope, rubric, domain and expiry.
Missing or insufficient reference coverage stays unknown. The protected signer
supplies the existing strict attestation format; canonical owners still check
controller independence, current evidence and release eligibility. Qualification
for one domain does not establish general semantic correctness.

For stricter empirical qualification, `calibration_suite` freezes protected
logical reference units, rubric, effective settings and held-out split.
`CalibrationSelection` independently reproduces finite executable references and
pins actual oracle/scorer/classifier source plus arithmetic environment. The
reference language covers sum, difference, product, mean, ratio and declared
units/scope/time/contradiction/insufficiency. It cannot qualify unrestricted prose
or a different domain by changing a pack label. Fixed external public labels and
new domains require their own qualified provenance owner; unsupported references
refuse this finite selection.

`CalibrationAuthority` scores actual authenticated correlated observations.
Independent baseline counts exclude presentation variants and duplicate material;
critical false acceptance, necessary/unnecessary abstention, repeat stability and
meaning/identity/position/verbosity sensitivity remain separate. Existing frozen
thresholds still decide adequacy. Scored reports and agent-generated exploratory
labels convey no permission or accepted evidence. Insufficient cases, families,
classes or complete axis coverage remain unknown.

A `CalibrationPlan` pins each configured evaluator's `CalibrationSelection`,
including exact model revision/settings, suite/split and domain descriptor. The
host supplies `calibration_configuration(config_ref)` for issuance and
`qualification_configuration(request)` for current use. The latter echoes the
fresh request nonce, execution-profile/registry identities and canonical semantic
basis, and returns the exact effective configuration and typed active status.
Absent, mismatched, timed-out, expired or withdrawn observations cannot qualify.
Callbacks run outside coordination locks; owner gates then check pure exact
correlation before semantic dispatch/application/release and unfinished accepted
recovery. A finite status snapshot cannot promise instantaneous future withdrawal.

Current domain derives from actual authorized source closures, strict claims,
original question grammar and exact rubric. Native bookkeeping changes do not
relabel that domain. Supported finite sources use `finite-structured-observation/v1`
JSON and exact declared observation grammar; free prose, missing context,
computational premise graphs and incompatible data remain unknown. Unaccepted
generation proposals may proceed under the original model grants and budgets;
evaluation and release still require current qualified profiles. Completed
historical replay does not refresh host status or grow the journal.

The integrating host must actually execute and meter its selected models.
Deterministic fixtures, scripted reference calculations, model self-confidence
and majority agreement do not establish live semantic model reliability. Missing
provider execution evidence remains unknown; the core discovers no credentials,
network destinations or additional permission. Control-plane host reads and
calibration preparation are outside session-work accounting and must not be
presented as a zero whole-execution cost.

Qualification records are immutable scoped snapshots. The host must withdraw or
invalidate the bound execution services when it revokes a qualification. Close
every affected retained session, or invalidate the effective executor/profile
seen by its binding checks before further use; changing a separate registry does
not update existing handles. Service identity changes refuse further use. This
profile does not query an online qualification revocation registry. Canonical
strict owners independently check current principal, signing-key and policy
authority before release.

Generation and evaluation retain finite original leases and one active external
reservation. The legacy generation route requires one original and the
closed payload `{"question": <exact original text>}`. Evaluation uses
`{"claim_id": <claim>, "packet_id": <snapshot digest>}`; the digest is
`sha256:` followed by the SHA-256 of the public strict review snapshot encoded
as UTF-8 JSON with sorted keys, no ASCII escaping and compact separators.
Evaluation binds its claim and complete source closure to the frozen scope.
Policy v1 names exact source identities. Policy v2 can name native absolute
extents inside the original workspace; current owning capture and normalization
records bind those extents to actual source IDs and complete provenance. Source
IDs are not predicted or added to an active policy. Unsupported external/URI and
multi-origin mappings refuse explicitly. Inspect
`contract()["research_sessions"]` before selecting this profile.

Pending reads never renew a lease. Under explicit execution, reconciliation and
cancellation use bounded host operations outside canonical locks. A requested
cancellation, confirmed stop, racing completion and unknown external effect have
distinct observations. Stopping never implies rollback. Unknown completion or
usage keeps the reservation occupied. Authenticated late intake records history
and actual charges without reopening a terminal run or accepting stale results.
Request replay returns historical receipts and does not grant new work.
Decision/job requests and pending-control requests have separate namespaces in
the same canonical journal, so caller IDs cannot collide with automatic
settlement. Pending reads validate immutable run, binding and lease fields;
edited history cannot supply completion or release a reservation.
The core installs no background scheduler. A live integrating host drives the
bounded loop; after a host disconnect, the next host invocation observes the
retained deadline and reconciles existing effects. UTC observations reject
regression. `OperationControl(deadline_utc=..., timeout_seconds=...,
cancel_requested=...)` supplies one fixed allowance to `normalize.sources`,
`mutations.submit` and `mutations.reconcile`. Native extraction has an enforceable
worker boundary; surrounding canonical phases check the same deadline before
publication. A stalled cancellation observer stops work with explicit uncertainty.
The reference subprocess and controlled extraction worker require POSIX process
groups. Unsupported hosts refuse before worker dispatch; hosts can supply their
own qualified executor boundary. Calls without explicit operation control retain
their existing platform behavior.

**`ws.research.telemetry`** retains bounded measurement declarations and exposes
their provenance:

| Operation | Signature |
| --- | --- |
| record | `record(event, *, run_id, agent_id)` |
| report | `report(*, run_id, verifier=None, execution_session=None)` |
| projection | `project(report)` |
| delivery | `deliver(projection, *, run_id, delivery_id, sink, max_attempts=3)` |
| delivery status | `inspect(delivery_id)` |
| known-failure retry | `retry(delivery_id, *, sink)` |

Recording uses the selected private mutation workspace gate and canonical run
lock. The explicit coordinator also accepts `telemetry.record` with the same
`run_id`, `agent_id` and event data. The event's actor must match the active
context. Report reads use a bounded anchored journal snapshot; limits apply
before parsing. Exact logical IDs count once, and conflicting reuse refuses.

Optional `MeasurementVerifier` objects come from trusted host code. A verifier
must authenticate exact event, workspace, controller, actor, metric and expiry
bindings. Declared measured/imported labels and reference strings cannot do
that. Reports separate exact subtotals by provenance, currency and pricing
identity. Unobserved totals stay unknown. UTC intervals and clock references
alone do not establish monotonic execution time. An explicit execution-session
report reconstructs decisions from authenticated retained inputs and exposes
exact totals only for its complete declared scope. Attempts and measurement slots
are declared before work; missing observations or sequence gaps remain unknown.
Duplicate receipts count once, and authenticated late costs remain visible.
Its redacted metadata omits private inputs, model outputs and signing material.
Use paging cursors when the declared trace exceeds one report page.

Native trace timing identifies an observed owner call, a recovered receipt or a
desired-state proof. Recovery lifetime stays separate from active execution;
unobserved active and waiting time remain unknown. `activity_summary` counts
concrete retained step, repair, retry, cancellation, recovery, review-conflict
and budget-stop records across the entire snapshot, including rows outside the
displayed page. Its completeness flags describe the declared scope. Those
counts do not certify repaired defects, caller autonomy or human zero.

Whole-host human intervention is unknown without a separately observed complete
boundary. Closed stdin, an empty intervention list or successful agent execution
does not prove a global zero. Setup, calibration and excluded host activity must
retain their own measurement scope.

Projections are redacted declarations whose measurements are not reverified.
They retain inert flags. Delivery requires an explicit `DeadlineSink` supplied
by the host and a finite host-enforced deadline contract. The core checks late
results and uncertain acknowledgements, and cannot interrupt arbitrary Python
callbacks. Private intent and sending checkpoints precede the callback; their
gates are released while it runs. Sending stays pending until its deadline;
expired or uncertain delivery cannot be retried blindly. Only a known failed
attempt can use the finite retry allowance. Sink success creates no approval,
permission or qualification. Inspect `contract()["telemetry"]` before selecting
these capabilities.

The coordinated output profile retains complete owner plans in private custody
before effects. Its limits are 4,096 immutable artifact slots, 16 MiB per final
artifact and 256 MiB in aggregate per request, with separate 1 MiB checkpoint
and transport limits and at most 3,000 source effects per plan. Plans that exceed these bounds refuse before workspace
writes. Native APIs retain their declared extraction limits; a large rendered
output may fit that native profile while exceeding the coordinated profile.

**`ws.usage`** — host-authorized source revisions:

| Operation | Signature |
|-----------|-----------|
| `usage.status` | `ws.usage.status(*, request_id=None) -> dict` |
| `usage.transact` | `ws.usage.transact(command: dict, *, artifacts: dict[str, bytes] \| None = None) -> dict` |
| `usage.check` | `ws.usage.check(revision: str, *, uses: list[str], purpose: str, consumer: str) -> dict` |
| `usage.lineage` | `ws.usage.lineage(revision: str, *, limit=4096) -> dict` |
| `usage.materialize` | `ws.usage.materialize(revision: str, *, expected_content_hash=None) -> dict` |

These operations use separately provisioned host authority and state. See
[Evidence usage](evidence-usage.md) for the signing, sanitization, revocation,
retention, and legacy compatibility contracts.

**`ws.temporal`** — bounded source evaluation at one cutoff:

| Operation | Signature |
| --- | --- |
| `temporal.evaluate` | `ws.temporal.evaluate(request: dict) -> dict` |

See [Temporal evidence](temporal-evidence.md) for explicit source clocks,
accepted checkpoints, independent public-availability receipts, and replay
limits. Selection, lexical retrieval, declarative facets, and scalar grounding
share the same cutoff. Current retrieval permission remains mandatory.

**`ws.assessments`** — authenticated selected evidence and current refresh:

| Operation | Signature |
| --- | --- |
| `assessments.prepare` | `ws.assessments.prepare(request: dict) -> dict` |
| `assessments.issue` | `ws.assessments.issue(envelope: dict) -> dict` |
| `assessments.check` | `ws.assessments.check(envelope: dict) -> dict` |
| `assessments.plan_refresh` | `ws.assessments.plan_refresh(request: dict) -> dict` |
| `assessments.apply_refresh` | `ws.assessments.apply_refresh(envelope: dict) -> dict` |

See [Authenticated evidence assessments](evidence-assessments.md) for host
attestation, current-use verification, explicit validity, bounded refresh and
retained history. Host action approval and execution remain separate.

**`ws.snapshots`** and `evidence_wiki.verify_snapshot` — frozen evidence:

| Operation | Signature |
| --- | --- |
| `snapshots.prepare` | `ws.snapshots.prepare(selection: dict) -> dict` |
| `snapshots.export` | `ws.snapshots.export(selection: dict, *, registration_request_id: str) -> dict` |
| `snapshots.check` | `ws.snapshots.check(data: bytes) -> dict` |
| `verify_snapshot` | `verify_snapshot(data: bytes, *, trust_policy_bytes: bytes) -> dict` |

See [Evidence snapshots](evidence-snapshots.md) for the selection, host signing,
canonical bundle, offline verification, and current-use contracts. Offline
verification opens no workspace and requires explicit independent trust bytes.

**`ws.questions`** — the question lifecycle. Every keyword mirrors the CLI flag
of the same name, and every mutating call writes its `log.md` entry and its page
frontmatter inside the call, so a question moved in-process leaves the same audit
trail as one moved from a shell:

| Operation | Signature |
|-----------|-----------|
| `questions.claim` | `claim(*, slug, agent_id, steal=False, if_older_than=None)` |
| `questions.release` | `release(*, slug, agent_id)` |
| `questions.answer` | `answer(*, slug, agent_id, answer_page, source_id=None, allow_uncited=False, allow_unclaimed=False, confidence=None, evidence_strength=None, require_coverage=False, require_grounding=False, coverage_manifest=None, grounding_file=None)` |
| `questions.block` | `block(*, slug, agent_id, blocked_reason, request_id=None, allow_unclaimed=False)` |
| `questions.defer` | `defer(*, slug, agent_id, reason, allow_unclaimed=False)` |
| `questions.reject` | `reject(*, slug, agent_id, reason, allow_unclaimed=False)` |
| `questions.reopen` | `reopen(*, slug, agent_id, source_id, request_id=None, require_decisive_scope=False)` |
| `questions.approve` | `approve(*, slug, reviewer)` |
| `questions.review` | `review(*, slug, policy, verdict, reviewed_by, review_ref=None, note=None)` |
| `questions.set_grounding` | `set_grounding(*, slug, agent_id, from_file, allow_unclaimed=False)` |
| `questions.add_batch` | `add_batch(*, from_file="-", dry_run=False)` |

Three details are deliberate rather than oversights. `reopen` has no
`allow_unclaimed`, because a blocked question is never claimed. `approve` and
`review` record a `reviewer`/`reviewed_by` rather than an `agent_id`, matching
the CLI's trust model: this authenticates nobody, and the audit trail is the
frontmatter entry plus `log.md`. And `add_batch(from_file="-")` reads the batch
from *this process's* stdin, exactly as the CLI does — rarely what a server wants,
so pass a path. `add_batch(dry_run=True)` returns the report the writes *would*
have produced; it is the supported way to preview intake, since the page writes,
the `index.md` update, and the `log.md` entry are otherwise part of the same
operation as the report.

Question batches may carry bounded optional `metadata` mappings under each
item. The library delegates to the workspace script, so validation, canonical
candidate-aware deduplication, persistence, and zero-based `item_index` report
correlation are identical to CLI and MCP intake. Metadata is caller-controlled
policy input persisted in the question page; it is not trusted evidence and
must not contain secrets. Older workspaces must be upgraded before using the
additive member.

**`ws.orchestrate`** — the protocol, one work order at a time:

| Operation | Signature |
|-----------|-----------|
| `orchestrate.start` | `ws.orchestrate.start(agent_id, *, orchestration_id=None, max_actions=None, action_timeout_seconds=None, total_timeout_seconds=None) -> OrchestrationSession` |
| `orchestrate.session.next` | `session.next(*, agent_id=None, resume=False) -> dict` |
| `orchestrate.session.submit` | `session.submit(action_id, result: dict \| str \| PathLike, *, agent_id=None) -> dict` |
| `orchestrate.session.status` | `session.status() -> dict` |
| `orchestrate.session.retire` | `session.retire(reason: str, *, apply=False, payload_policy="retain", driver_wait_seconds=None) -> dict` |
| `orchestrate.session.cleanup_claims` | `session.cleanup_claims(*, apply=False, driver_wait_seconds=None) -> dict` |

Every limit left as `None` is omitted from the controller's argv, so the
workspace's deployed controller applies its own default rather than this package
pinning one a newer workspace has moved on from. See [version
authority](#version-authority) for why these six are the operations that keep a
subprocess.

**Module level** — no single handle owns these:

| Operation | Signature |
|-----------|-----------|
| `fleet_status` | `evidence_wiki.fleet_status(targets: Sequence[str \| Path], *, no_cache=False) -> dict` |
| `contract` | `evidence_wiki.contract() -> dict` |

`fleet_status` takes paths rather than open handles because it aggregates across
many workspaces at once. `contract()` is the same payload `evidence-wiki
contract` prints, built once below both front ends.

### Negotiating Against The Contract

The authoritative list of what this installation implements is in the capability
contract, and a host should negotiate against it rather than hard-coding a
version comparison:

```python
import evidence_wiki

library_api = evidence_wiki.contract()["library_api"]
assert "coverage.evaluate" in library_api["surface"]
```

`surface` is a list of `<namespace>.<operation>` names; `version` is the
compatibility signal. The list is a *declaration*, deliberately not introspected
from live objects — walking the classes at call time would make the published
contract depend on import order and would silently widen or narrow the API every
time an internal helper was renamed. The version advances when the declared surface changes. Version `"2"` adds
selected publication and a versioned operation matrix; callers that only understand
version `"1"` must negotiate before using the expanded contract. Version `"3"`
adds offline qualified packet profile discovery and original-byte validation.
Version `"4"` adds `normalize.validate_execution(source_id)` and
`evidence-wiki normalize execution --target PATH --source-id ID`. This read-only
operation returns execution structure and current independent evaluation
authority as separate results. A structurally valid failed run remains usable
evidence. Execution CLI exit 1 means the original structure is invalid;
callers must inspect `verification.eligible` when a passing evaluation is required.
Version `"5"` adds the five `usage` operations and the `evidence_usage`
capability description. A usage check returns ineligibility as a verdict;
invalid operations raise `SourceError` with `EVIDENCE_USAGE_REFUSED`.
Version `"6"` adds snapshot preparation, publication, current-use reconciliation,
and workspace-independent verification. Snapshot operations refuse invalid
inputs with `EVIDENCE_SNAPSHOT_REFUSED`; verification returns a validity verdict.
Version `"7"` adds read-only `temporal.evaluate`, availability attestations in
the usage command contract, and historical snapshot selection with the v2
snapshot schemas. Invalid temporal requests raise `SourceError` with
`EVIDENCE_TEMPORAL_REFUSED`. A completed evaluation can contain source gaps,
failed grounding, or unresolved review; inspect the returned outcomes.
Version `"8"` adds read-only `normalize.validate_market(source_id)` and
`evidence-wiki normalize market --target PATH --source-id ID --format json`.
The optional `market_evidence/v1` profile validates inert, bounded filing and
price deliveries. Inspect `valid`, `completeness`, and current usage eligibility
separately. A valid structure can contain gaps; the operation grants no rights.
Other source kinds and existing provider selections do not acquire market requirements.
Version `"9"` adds accepted historical execution inputs and snapshot v3.
Every historical generation binds its inputs and their complete ancestry at its
own cutoff; the result and its independent evaluation can occur later. Inspect
`historical_inputs` separately from the structural execution report. Historical
input qualification does not establish a model's training cutoff.
Version `"10"` adds optional `market-simulation/v1` execution qualifications and
snapshot v4 with explicitly declared execution profiles. Existing generic
execution records retain their contract. The [simulation profile](market-simulation.md)
recalculates bounded portfolio results and qualifies preselected configuration
without starting a runner or adding a feed dependency.
Version `"11"` adds the five `assessments` operations and their versioned
capabilities. Assessment checks return current eligibility; invalid operations
raise `EvidenceWikiError` with `EVIDENCE_ASSESSMENT_REFUSED`. Signed issuance and
refresh application use the existing host usage ledger and checkpoint contract.
Version `"12"` adds `orchestrate.session.retire` and
`orchestrate.session.cleanup_claims`. Both return a read-only plan by default;
`apply=True` archives and seals a terminal session or removes its matching live
claim ledgers. See the [retention contract](../workspace-template/docs/orchestration.md#claim-retirement-and-cleanup)
before applying either operation. Archives and tombstones remain permanent;
payload erasure is unsupported and explicitly refused.
Version `"21"` adds explicit execution-session observation intake and redacted
reports, shared native operation controls, and host-driven advancement through
canonical research owners. Session v2 and host-job v2 are explicit selections;
legacy session and host-result codecs retain their original meanings. Hosts
must provision execution, qualification and observation custody before use.
`contract()["intake_profiles"]` advertises the supported native schemas,
validator pin, bounds, and reconciliation limits. Validation returns a report
with separate delivery, native consistency, and host reconciliation results;
the CLI returns exit 1 when validity or the selected policy is unsatisfied.

One public method is intentionally absent from that list:
`ws.orchestrate.session(orchestration_id)` returns a driver for an existing
session without touching it (naming a session is not reading one), so a host that
restarts can reconstruct its drivers without a controller spawn per session. It
is a handle constructor; the six protocol operations are declared separately.

## Operation Boundaries

`contract()["library_api"]` includes `matrix_version`, `operations`, `cli_only`,
and `timeout_policy`. Every supported operation has a CLI entry point (or an
explicit lack of an equivalent), mutation effects, locking behavior, and a
subprocess boundary. The independently versioned matrix is caller-owned JSON.
Hosts should check the declared surface at startup and retain an outer timeout.
Orchestration still launches one version-matched controller process per call;
`doctor` may probe external tools with individual timeouts.

Standalone lint, workspace-wide publication bundles, delegated fetch planning
and legacy external normalization remain CLI-only. Scoped onboarding covers
explicit setup, upgrade and pack lifecycle operations. The
matrix names their supported entry points. Package API use alone does not
establish that an operation has no subprocesses or writes.

## Selected Publication

```python
report = ws.publish_selected(["battery-lifetime", "charging-safety"])
if report["verdict"] == "ship":
    answers = report["export"]["questions"]
# Optionally refuse if the workspace differs from this exact captured input:
again = ws.publish_selected(report["question_slugs"],
                            expected_revision=report["revision"]["revision_id"])
```

The CLI uses the same operation:

```bash
evidence-wiki publication --target /path/to/workspace --question battery-lifetime --question charging-safety
```

Selections must contain 1–1,000 portable question slugs. Duplicates collapse to a
sorted set; empty, malformed, and unknown selections refuse. The response binds
readiness and exported answers to one content identity and a separate trusted
producer identity. Question lifecycle, review, grounding, citations, and coverage
are evaluated for the selected questions. Source/configuration integrity, active
source normalization, request and candidate integrity, claims/contradictions,
licensing, curation, secrets, and retained output checks remain global. An
unrelated pending question review is excluded; an unrelated source defect can
block the selected result. Read `gate_scope` and consume the returned verdict.

The operation captures all workspace files except declared runtime caches and
Git metadata, evaluates a private read-only materialization, then revalidates
live bytes. It executes trusted installed scripts. Captured scripts are data.
Known secret patterns are checked before materialization; this heuristic is not
permission to export sensitive data. Configured paths must stay within the
workspace. Three bounded evaluation attempts end in a typed refusal if inputs or
the producer keep changing. Timestamps and inode values do not enter content
identity. Identity establishes bytes, not authenticity, historical availability,
or training rights.

Capture is limited to 10,000 files, 20,000 directory entries, 256 MiB total,
16 MiB per file, and depth 64. Symlinks, hardlinks, special files, and nonportable
paths refuse. Platforms lacking no-follow descriptor operations, including
Windows, return `EVIDENCE_REVISION_UNSUPPORTED`. The live workspace is unchanged;
CLI `--output` must name a destination outside it. Exit 0 means `ship`, exit 1
means another research verdict, and exit 2 means refusal. The unscoped operation
and its bundle behavior retain their existing contract.

## Errors

Every refusal is an `evidence_wiki.errors.EvidenceWikiError` or a subclass, and
carries the whole error envelope:

| Attribute | Meaning |
|-----------|---------|
| `error_code` | The stable code. Branch on this. |
| `message` | Human-readable. Log as-is; `str(exc)` is the same string. |
| `recoverable` | Whether a retry is meaningful. `False` for `CLAIM_HELD` and `CLAIM_NOT_STALE`. |
| `remediation` | What an operator should do. Surface it. |
| `details` | Structured context, possibly empty. |
| `exit_code` | The status the CLI would have exited with: `2` for a fatal caller-fixable error, `3` for a conflict, `6` for `ORCHESTRATION_DRIVER_BUSY`. Envelopes carry no status, so it is reconstructed from `error_code`; a code whose script exits with something other than `2` must be registered in `errors._EXIT_CODE_OVERRIDES` for the two doors to agree. Dispatch on `error_code` when you need a specific condition — this attribute groups several. |

Error families sit under the base class. The family is selected from the code
by prefix, with exact codes winning over prefixes and longer prefixes over
shorter ones — which is how `QUESTION_NOT_CLAIMED` lands in `ClaimError` while
`QUESTION_REOPEN_DELEGATED` lands in `RequestError`:

| Family | Covers |
|--------|--------|
| `ConfigError` | The workspace, its configuration, or the runtime cannot support the call: `CONFIG_*`, `WORKSPACE_UNREADABLE`, `DEPENDENCY_MISSING`, `TOOLING_MISSING`, `UPGRADE_WRITE_FAILED`. |
| `LockError` | `LOCK_UNAVAILABLE` — no workspace lock backend could be established, or a workspace lock stayed held past a call's bounded wait. A contended *orchestration session* lock is not this: it is `OrchestrationError` / `ORCHESTRATION_DRIVER_BUSY`. |
| `ClaimError` | A claim could not be taken, stolen, released, or resolved: `CLAIM_*`, `STEAL_*`, `STATUS_NOT_*`, `QUESTION_NOT_CLAIMED`. |
| `QuestionError` | A question, slug, or answer page is unusable: `QUESTION_*`, `SLUG_*`, `ANSWER_*`, `PAGE_INVALID`, `RESOLUTION_REASON_INVALID`. |
| `PublicationError` | Selected publication selection, configuration, safety, or output refusal: `PUBLICATION_*`. |
| `RevisionError` | Bounded capture, unsafe paths, unsupported platform, or concurrent changes: `EVIDENCE_REVISION_*`. |
| `CoverageError` | `COVERAGE_*`, `FACET_SCOPE_CONFLICT`. |
| `GroundingError` | `GROUNDING_*` — a claim is not grounded in a verifiable quote from an accepted source. |
| `RequestError` | Source requests: `REQUEST_*`, `SOURCE_REQUEST_FULFILL_DELEGATED`, `QUESTION_REOPEN_DELEGATED`, `ATTEMPT_FAILURE_CODE_INVALID`. |
| `SourceError` | Sources and their inventory: `SOURCE_*`, `MANIFEST_*`, `INVENTORY_*`. |
| `IntakeError` | Externally supplied intake refused by a cap, rate limit, or signature check: `INTAKE_*`, `HANDOFF_SIGNATURE_INVALID`. |
| `ProviderError` | Discovery and acquisition: `PROVIDER_*`, `ACQUISITION_*`, `DISCOVERY_*`, `GITHUB_*`, `OPENALEX_*`, `ARXIV_*`. |
| `RunError` | `RUN_*`, `BASELINE_*`, `EVENT_*`, `FINAL_VERDICT_REQUIRED`. |
| `OrchestrationError` | `ORCHESTRATION_*`, including the two host-side codes below. |
| `UsageError` | The call itself is malformed, refused before any workspace state is read: `AGENT_ID_INVALID`, `QUERY_MISSING`, `VALUE_INVALID`, `NOT_IMPLEMENTED`. |

Two `ORCHESTRATION_`-prefixed codes are minted on the host side of the process
boundary, where the workspace's vocabulary cannot reach:
`ORCHESTRATION_HOST_FAILED` (the controller failed without emitting a usable
envelope) and `ORCHESTRATION_HOST_EXITED` (a workspace script exited instead of
returning). They keep the prefix so a host already catching `OrchestrationError`
catches them too.

### An Unknown Code Never Explodes

A code this version has never seen — a newer workspace, a newer domain pack —
degrades to the base class with the code preserved:

```python
from evidence_wiki import errors

error = errors.error_from_envelope(
    {"error_code": "SOMETHING_FROM_THE_FUTURE", "message": "a newer workspace said so"}
)
type(error)         # <class 'evidence_wiki.errors.EvidenceWikiError'>
error.error_code    # 'SOMETHING_FROM_THE_FUTURE'
```

This is the property that keeps an older host working against a newer workspace,
and it is why **`except EvidenceWikiError` must be the outermost arm** in any
host that catches by family. Family catches are a convenience, not a partition of
the code space: a handful of codes the deployed orchestration controller can emit
today — `RESULT_INVALID`, `WORK_ORDER_INVALID`, `ACTION_ID_INVALID`,
`ARTIFACT_PATH_INVALID`, `CANDIDATE_STORE_INVALID` — have no family entry and
arrive as the base class, so `except OrchestrationError` alone would miss them.
Dispatch on `error_code` when the distinction matters.

Two more degradations, all on the failure path where a second exception would
bury the original diagnosis: an envelope with no code reports `UNKNOWN`, and
input that is not a usable envelope reports `ENVELOPE_MALFORMED` while keeping
whatever code it did carry. `error_from_envelope` never raises.

Note also what is *not* translated. An exception that is neither a workspace
refusal nor a `SystemExit` propagates unchanged — a `TypeError` from a bad
argument is a bug in the calling code, not a workspace condition, and dressing it
as an `EvidenceWikiError` would hide it. `SystemExit` is always translated,
because a library that let one escape would terminate an ASGI worker.
An unclassified process-control exit without a result raises
`LIBRARY_PROCESS_EXIT` with exit code 2. Its bounded scalar status is retained
in `details.owner_exit_status`; arbitrary exit payloads are not exposed.

### The CLI And The API Cannot Disagree

The codes above are the same strings the CLI writes into its stderr envelopes,
and that is a structural property rather than a convention that has to be
maintained. Each operation has exactly one implementation — a `run_<op>(...) ->
dict` seam in the workspace script — and both front ends render from it: the CLI
prints the returned document or the raised refusal's envelope, the API returns
the document or raises the typed exception built from that same envelope.
Both interfaces preserve the owning operation’s document, refusal code, and
exit status. Presentation choices such as JSON indentation and output files belong
to the CLI.

## Thread Safety

The operation matrix declares each operation's filesystem effects and locking.
Per-question claims and controller sessions use their owning locks. Coverage
rewrites and question intake have no transaction lock; hosts must serialize
writers to those surfaces. Atomic cache replacement protects individual cache
files, without making a sequence of workspace reads a transaction.

The API never redirects `sys.stdout`. Script loading temporarily changes
`sys.path` and `sys.modules` under a process-wide reentrant lock and restores
those bindings. Sibling loading uses the same lock. Returned documents are
separate caller-owned objects. Selected publication instead captures and
revalidates bytes, retrying or refusing when the workspace changes.

What remains the host's job:

> **Decide what a busy session should do to your request.** The protocol is a
> single-driver protocol, and the workspace enforces it with a per-session lock
> at `runs/orchestrations/<id>/.locks/session.lock` held for the whole of
> `start`, `next`, and `submit`. A second driver that finds it held is refused
> **immediately** with `OrchestrationError` / `ORCHESTRATION_DRIVER_BUSY`
> (`recoverable=True`), whose `details["holder"]` names the holder's `agent_id`
> (or `None`), `pid`, `hostname`, `command`, and `acquired_at`. Branch on
> `error_code`, which is the stable contract; `exit_code` agrees with it here
> (`6`, the status the controller process exits with) because the code is
> registered in the reconstruction table. A refused call writes nothing. This is `LOCK_UNAVAILABLE`'s
> replacement for *contention only*: that code kept its older, narrower meaning
> — no lock backend could be established on this filesystem at all — and the two
> stay distinct because retrying fixes one and never fixes the other.
>
> **A host-side lock is now a throughput optimization, not a correctness
> requirement.** It buys one thing: not spending a controller subprocess merely
> to be told you are busy. Drop it and concurrent requests get a typed refusal
> instead of a queue; keep it and they queue in your process, which is cheaper
> and lets you apply your own fairness policy. The [ASGI
> example](#embedding-in-an-asgi-service) below shows the shape.
>
> `next` is idempotent, but that no longer means concurrent callers all get
> through. Eight threads calling `next()` on one session with no host lock used
> to all succeed and all receive the same replayed work order; now one holds the
> lock and every thread that arrives while it does is refused with
> `ORCHESTRATION_DRIVER_BUSY`, because refusing is the default — the bounded
> wait it replaced is exactly what let two interleaving drivers both succeed,
> silently. A caller that wants the old behavior asks for it: with
> a large enough wait, the eight serialize and all receive the *same* pending
> work order, since the controller replays rather than issuing eight actions.
> That is `--driver-wait-seconds` on the CLI and `driver_wait_seconds=` on
> `start`, `next`, and `submit` here — omit it and the controller's own
> immediate-refusal default applies, because this seam never restates a default
> the deployed controller owns. Prefer your own in-process queue where you have
> one: a controller-side wait costs a blocked subprocess, while an in-process
> wait is cheap and lets you set your own fairness policy. Reach for the
> parameter when the competing driver is in *another* process, which is the case
> a host lock cannot see.
>
> **What the per-session lock does not cover** is interleaving *across* the
> `next` → `submit` span: it is scoped to one invocation, so two drivers taking
> turns — one calling `next`, the other `submit` — are never simultaneous and
> are never refused. `submit` still refuses a second, divergent result for the
> same action with `RESULT_CONFLICT`, and the `action_issued` /
> `action_completed` events record a `driver` block naming the writing process,
> so the interleaving is at least legible after the fact. Enforcing it is
> deliberately not attempted here.

Nothing here makes a *question* safe to drive from two places at once either.
The workspace protects its own state; it does not merge two agents' intentions.

## Handle Lifetime And `close()`

Packaged assets are held by a **process-wide shared assets root**, entered once
on first use and released at interpreter exit.

This matters for a zip or otherwise non-extracted install, where
`evidence_wiki.resources.assets_root()` has to materialize the asset tree into a
temporary directory that only survives inside a `with` block. If every handle
entered its own, N open handles would mean N full asset extractions — and N
disjoint families of cached script modules, since that cache is keyed by the
resolved script directory. Entering exactly once per process bounds both. (The
CLI keeps its per-command entry; a one-shot process cannot tell the difference.)

So:

```python
ws = Workspace.open(root)
ws.status()
ws.close()

ws.closed          # True
repr(ws)           # '<Workspace /path/to/workspace (closed)>'
ws.root            # still readable
ws.status()        # ConfigError: WORKSPACE_UNREADABLE
```

`close()` invalidates *this handle only*. Every later operation on it raises
`ConfigError` / `WORKSPACE_UNREADABLE` — calling one then is a use-after-close bug
in the host, not a question about the workspace — while the inert identity
accessors (`root`, `closed`, `repr()`) keep working, so a host can still log which
handle it over-released. `close()` is idempotent.

`close()` does **not** tear down the shared assets root, because sibling handles
in the same process are still using it. Releasing it at interpreter exit is the
whole design, not a leak.

A handle is cheap: opening one validates a path and touches no assets at all. The
usual shape is one long-lived handle per workspace root for the life of the
process.

## Version Authority

Two different rules apply, and the difference is the point.

**Read, evaluate, and resolve operations run the packaged scripts in-process.**
The scripts come from the assets that ship with the *installed package* —
deliberately not from `<workspace>/scripts` — so one installed version drives
every open workspace, and a host cannot be made to execute code out of a
workspace directory. The installed library version is the behavior version,
exactly as it already is for `evidence-wiki status`.

**Orchestration `start`/`next`/`submit`/`status`/`retire`/`cleanup-claims` keeps a subprocess.** Each of
those six spawns the workspace's *own deployed* controller at
`<workspace>/scripts/orchestration_controller.py`:

```text
sys.executable -B <workspace>/scripts/orchestration_controller.py \
    --project-root <workspace> next --orchestration-id ... [--agent-id ...]
```

That deployed controller is authoritative for run-state mutation precisely
because it is version-matched to the session state it owns. Loading it in-process
— or substituting this distribution's packaged copy — would let one installed
library version mutate run state belonging to a different workspace version. It
is the same reason managed orchestration is absent from the MCP server.

What the facade removes is the *CLI* process: argument parsing, config discovery,
and the interpreter start for `evidence_wiki.cli`. It does not remove the
controller boundary, and **the subprocess must not be optimized away.** If a
future change makes orchestration feel slow, the answer is fewer controller
round-trips, not an in-process shortcut.

## Version Skew

`Workspace.open` has **no version gate**, on purpose. It refuses only structural
absence — a path that is not a usable directory, or one without `research.yml`.
It does not compare the installed package's version against the workspace's
deployed scripts, because the CLI has no such gate: adding one here would make
the API refuse workspaces the CLI happily serves, which is exactly the CLI/API
divergence this API exists to remove. The workspace scripts already own their own
compatibility checks.

Skew therefore surfaces where it already surfaced: as a script's own typed
refusal, at the call that actually depends on the incompatible piece.

`ws.versions()` reports the installed package version beside the workspace's
recorded metadata:

```python
ws.versions()
# {'package': '1.0.0',
#  'workspace': {'starter_version': '0.7.0',
#                'schema_version': '0.1',
#                'compatible_research_yml_contract': '0.1'}}
```

`starter_version` identifies reusable starter content; `schema_version` identifies
the metadata shape. Neither the package version nor these recorded values prove
that deployed script bytes match a release. Hosts that need that identity must
compare the files or use their own content digests.

This is a pure read and never refuses *on the basis of what it reads*. Every
workspace key degrades to `None` when `workspace-system.yml` is absent,
unreadable, not valid YAML, or shaped unexpectedly — reporting skew is the whole
job, so raising on the very file that would reveal it would defeat the purpose. A
host that wants a hard failure compares the values itself. The one refusal is a
closed handle, which is not about content.

`ws.doctor()` is the documented preflight. It diagnoses runtime, tooling, and
configuration and returns the same report `evidence-wiki doctor --format json`
prints; almost nothing refuses, because diagnosing a broken workspace is the
reason to call it. A workspace the doctor cannot read at all comes back as
`verdict: "missing"` with a full report rather than an exception:

```python
report = ws.doctor()
if report["verdict"] != "ok":
    failed = [check for check in report["checks"] if check["status"] != "ok"]
    for check in failed:
        check["id"], check["label"], check["message"], check["remediation"]
```

Every entry in `report["checks"]` carries `id`, `label`, `status`, `required`,
`message`, `remediation`, and `implication`; some also carry `version` and
`details`. A domain error raised while running a check is folded into that
check's own entry rather than aborting the report, and the failing checks of an
unreadable workspace are marked `degraded` or `missing` rather than omitted.

## Two Behaviors A Host Will Otherwise Get Wrong

### A Failed Verification Is Not A Refusal

`ws.grounding.verify` and `ws.normalize.verify` **return** a report whose
`overall_result` is `not_verified` when something does not verify. The CLI prints
that same document and *then* exits non-zero — the non-zero status is a verdict,
not an error, and the document is the answer:

```python
report = ws.grounding.verify(["benchmarks"])
if report["overall_result"] != "verified":
    for question in report["questions"]:
        if not question["all_verified"]:
            ...   # which claim, against which record, and what would fix it
```

Which claim failed, against which record, and what would fix it are the entire
point of asking, and none of it can be read off an exception. Only a genuine
refusal raises — a malformed grounding block (`GroundingError`), an unknown slug
(`QuestionError`), an empty `slugs` sequence (`QuestionError` / `SLUG_INVALID`,
exactly as omitting `--slug` does), a workspace that cannot be read at all
(`ConfigError`).

### `fleet_status` Never Raises For A Bad Target

Ten good workspaces and one bad path yield eleven answers:

```python
report = evidence_wiki.fleet_status([good_root, "/definitely/not/a/workspace"])
report["counts"]["targets"]   # 2
report["counts"]["errors"]    # 1

for entry in report["targets"]:
    if not entry["ok"]:
        entry["path"], entry["error_code"], entry["message"]
```

Each entry carries `path` and `ok`; a failing entry adds that target's own
`error_code` and `message`. That degradation is the contract of this call, not an
accident, and it is why an operator can point it at a whole fleet without
pre-validating it. `fleet_status` raises only if the packaged scripts themselves
cannot be loaded, which is an installation problem rather than a fleet finding.

## Embedding In An ASGI Service

**The package ships no HTTP server, and will not.** Transport, authentication,
tenancy, and rate limiting are the host's, and an HTTP surface baked into this
package would have to guess all four. What follows is the shape a host builds on
top — framework-agnostic, and complete enough to lift.

Four properties carry the weight:

1. **One `Workspace` per workspace root**, held for the life of the process.
   Opening is cheap; extraction and script caches are process-wide anyway.
2. **Calls are offloaded to a thread.** The API is blocking filesystem I/O, not
   async. Calling it directly from a coroutine blocks the event loop.
3. **Orchestration `next`/`submit` is serialized per session** by the host's own
   lock — an optimization, not the correctness boundary. The workspace's
   per-session lock refuses a concurrent driver with
   `ORCHESTRATION_DRIVER_BUSY` either way; queueing in-process means a waiter
   costs an `asyncio.Lock` rather than a controller subprocess and a refusal
   the caller has to retry.
4. **Typed errors map to HTTP responses** using `error_code` and `recoverable`.

```python
"""An embedding layer for EvidenceWiki. Wire the two coroutines into any ASGI
framework's routes; nothing here depends on one."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from evidence_wiki import Workspace, errors

# 1. One handle per workspace root, for the life of the process.
_workspaces: dict[str, Workspace] = {}
_workspaces_guard = asyncio.Lock()

# 3. One lock per orchestration session. The workspace enforces single-driver
#    on its own; this keeps a waiter from paying for a controller subprocess
#    just to be told ORCHESTRATION_DRIVER_BUSY. It cannot help across replicas.
_session_locks: dict[str, asyncio.Lock] = {}


async def workspace_for(root: str | Path) -> Workspace:
    key = str(Path(root).expanduser().resolve())
    async with _workspaces_guard:
        handle = _workspaces.get(key)
        if handle is None or handle.closed:
            handle = _workspaces[key] = Workspace.open(key)
        return handle


async def session_lock(orchestration_id: str) -> asyncio.Lock:
    async with _workspaces_guard:
        return _session_locks.setdefault(orchestration_id, asyncio.Lock())


# 4. error_code and recoverable decide the status; the envelope is the body.
_NOT_FOUND = {"CONFIG_MISSING", "WORKSPACE_UNREADABLE"}


def http_error(exc: errors.EvidenceWikiError) -> tuple[int, dict[str, Any]]:
    if isinstance(exc, errors.ConfigError) and exc.error_code in _NOT_FOUND:
        status = 404
    elif isinstance(exc, errors.UsageError):
        status = 400
    elif exc.exit_code == 3:        # a conflict: CLAIM_HELD, CLAIM_NOT_STALE
        status = 409
    elif exc.error_code == "ORCHESTRATION_DRIVER_BUSY":
        status = 503               # another driver holds the session right now;
                                   # details["holder"] says who. Matched by code
                                   # rather than exit_code (6) so this stays
                                   # distinct from any future code sharing it.
    elif isinstance(exc, errors.LockError):
        status = 503               # contended, and worth retrying
    elif exc.recoverable:
        status = 422
    else:
        status = 409
    return status, {
        "error_code": exc.error_code,
        "message": exc.message,
        "remediation": exc.remediation,
        "recoverable": exc.recoverable,
        "details": exc.details,
    }


async def get_coverage(root: str, slug: str) -> tuple[int, dict[str, Any]]:
    """GET /workspaces/{root}/coverage/{slug}"""
    try:
        # Opening refuses too, so it belongs inside the handler.
        ws = await workspace_for(root)
        # 2. Blocking I/O belongs on a worker thread, never on the event loop.
        return 200, await asyncio.to_thread(ws.coverage.evaluate, slug)
    except errors.EvidenceWikiError as exc:
        return http_error(exc)


async def post_orchestration_next(root: str, orchestration_id: str) -> tuple[int, dict[str, Any]]:
    """POST /workspaces/{root}/orchestrations/{id}/next"""
    try:
        ws = await workspace_for(root)
        session = ws.orchestrate.session(orchestration_id)
        async with await session_lock(orchestration_id):
            return 200, await asyncio.to_thread(session.next)
    except errors.EvidenceWikiError as exc:
        return http_error(exc)
```

Notes on the shape:

- `except errors.EvidenceWikiError` is the outermost arm, for the reason given
  under [errors](#an-unknown-code-never-explodes): a newer workspace can emit a
  code this host has never seen, and it must become a 4xx/5xx rather than a 500
  traceback.
- A returned work order is validated by the same `_validate_work_order` the
  managed runner uses — bounded size, safe relative paths, no absolute paths, no
  environment credential values — so an embedding host gets exactly the
  guarantees the managed path gets.
- `session.next()` returns the *work order* while the session can still make
  progress and the *session document* once it cannot. A session that has ended is
  an answer, not an error, even though the controller signals it with a non-zero
  exit; loop on the returned `artifact_type`
  (`orchestration_work_order` vs `orchestration_session`), not on an exception.
- `session.submit(action_id, result)` takes the result document itself — written
  to a temporary file outside the workspace tree and removed again, on every path
  including the one where the controller refuses — or a path to a file that
  already holds one. The document is a *claim*: the controller re-verifies the
  workspace artifacts it names and refuses a result whose postconditions the
  workspace does not actually satisfy, so a well-formed document is not a way to
  make a session progress.
- After upgrading a workspace, drop and reopen any handles the host process
  retained. `versions()` can reveal changed metadata, but an unchanged result
  does not prove that scripts are unchanged.

## What The API Deliberately Omits

Absences worth stating, so they are not read as gaps:

- **`--format` and `--output` have no counterparts.** They choose how a document
  is rendered and where it is delivered, not what it says. The API returns the
  document; a host that wants `jsonl` bytes or a file on disk reshapes and writes
  it itself.
- **`status`'s `--append-log` has no counterpart.** Appending to `log.md` is
  something the CLI does *after* the status document is produced, not part of
  producing it. A host that wants a log entry writes one.
- **`doctor`'s `env` injection point is not exposed.** `DoctorEnvironment` is
  defined inside a packaged script asset rather than in this package, so a host
  has no supported way to name the type it would have to construct; it exists for
  the doctor's own tests, which reach the seam directly. Exposing it would publish
  a parameter whose only legal value the public API cannot hand out.
- **Programmatic workspace and pack mutation are out of scope.**
  `Workspace.open` validates and never creates. Workspace creation, starter
  upgrades, domain-pack adoption, and domain-pack refresh stay with
  `evidence-wiki init`, `evidence-wiki deploy`, `evidence-wiki upgrade`,
  `evidence-wiki pack adopt`, and `evidence-wiki pack refresh`, which own their
  own dry-run, conflict, transaction, and preservation rules.
- **Managed orchestration runners are not on the API.** `orchestrate run` and
  `orchestrate resume` drive a package-managed Codex or Claude worker under an
  isolation boundary; the API exposes the model-neutral protocol
  (`start`/`next`/`submit`/`status`) that an embedding host drives itself.

## See Also

- [Parent orchestration](../workspace-template/docs/orchestration.md) — session
  artifacts, routing, recovery, and the isolation boundaries the protocol assumes.
- [Orchestrator handoff](../workspace-template/docs/orchestrator-handoff.md) —
  what an external driver owes the workspace.
- [Question API](../workspace-template/docs/question-api.md) — slugs, grounding
  blocks, resolution outcomes, and the full refusal-code table.
- [Coverage manifests](../workspace-template/docs/coverage-manifest.md) — what
  `coverage.evaluate` reads and writes.
- [Workspace status](../workspace-template/docs/workspace-status.md) — the
  document `status()` returns.

A bound session can call
`examine_coverage(request_id=..., question_slug=..., config_ref=..., source_ids=...)`
and
`examine_counterevidence(request_id=..., question_slug=..., claim_id=..., config_ref=..., source_ids=...)`.
The host grants `coverage_session.examine`, `coverage_observation.observe`,
`retrieval_operations.search`, existing execution grants and the native coverage
mutation grants. The coverage worker requires `coverage.propose`; a separately
controlled critic requires `objection.propose`. A critic-only invocation also
activates the terminal objection gate. Findings never register strict reviews.
Repeated requests retain the original attempt and exact native effect intents;
completed results are historical and confer no fresh release eligibility.

Examination uses the whole permitted run-source binding set when no source
selector is supplied. Context is bounded to 32 sources; a larger decisive
context is refused rather than silently omitted. Completed examination replay
preserves its historical effect records, then rechecks current exact material
before returning quotations. Withdrawn access or a changed source returns
blocked metadata with no cached findings or query text.


### Complete Blinded Review Delivery

`ws.strict.prepare_packet` returns a protected handle. Its bounded `context_id`
is a host correlation label: the library combines it with a private random seed
to produce an opaque worker context ID. Automatic delivery derives one shared
opaque context from its parent binding. Caller labels stay with the parent.
`handle.header()` lists
blinded claim, premise, source and required page descriptors. `handle.page(page_id)`
returns the exact named source or decisive section slice. `handle.complete(pages)`
checks the supplied pages against that immutable manifest and returns missing
page IDs. The handle stays bound to its original open workspace and active
`HostContext`; closing the workspace or changing the host selection invalidates
access. Each operation checks current native material, source permissions,
mechanical diagnostics and calculation inputs. Cached pages grant no later
access to withdrawn evidence.

The header is a delivery plan: `complete_evidence_delivered` remains false.
`complete.complete` establishes that every required page was supplied and no
known decisive section is missing. It does not establish an exhaustive
counterevidence search. A standalone packet with no alternative source selector
has no observed counter context; the automatic route supplies the permitted run
source set and requires the supported native examination configuration.
Mechanical matches and bounded search results supply no semantic approval.

For automatic delivery, construct the final `ExecutionProfile` with
`review_delivery=ReviewDelivery(page_chars=4096, max_packet_bytes=196608,
max_input_bytes=524288)` before issuing its exact evaluator qualifications.
Each required Evidence or Critic configuration needs a frozen scope and exact
current qualification, together with current native source and examination owners. The page width accepts 256–16384
characters; packet bounds accept 4096–196608 bytes and complete input bounds
4096–524288 bytes. The input bound must accommodate the packet bound. These
frozen limits apply to the complete worker input, including instructions.
Existing profiles retain their original encodings and behavior.

Grant `review_page_operations.prepare` and `review_page_operations.page` for
protected reads. Session assembly additionally requires
`research_session.assemble_review_packet`, `research_session.advance` and
`review_page_session.assembled`;
configured evaluator delivery uses `review_packet_execution.bind` plus the
existing execution and native review grants. Automatic first-pass Evidence and
Critic reservations require complete delivery. Direct job submission cannot
bypass that requirement. Regular and paged context identities, source aliases
and input digests are pinned together; computation is a required decisive
section when configured.

Session assembly records host delivery in the existing run journal and charges
its native read reservation. Repeating the exact request rechecks retained
material; it does not dispatch a model. New assembly intents are refused after
terminal completion. Missing pages, stale sources, withdrawn permissions,
unsupported examination context or exceeded bounds cannot authorize review
application. The canonical drive stops finitely when complete delivery is
unavailable. `worker_consumption` and `worker_comprehension` remain
`not_observed`; `semantic_approval` remains `not_evaluated`. Only independently
qualified current reviews and the native release owner decide acceptance.


API version 39 / operation matrix version 22 adds `session.review_services()`
and `ws.strict.service_schemas()`. The status read checks only already configured
parent-held credentials against current native trust and role/controller
selection. It performs no credential discovery, enrollment, remote mutual
authentication or service-health request. A credential challenge proves parent
key possession and native role usability; `authenticated_owned_execution` is a
retained correlated execution fact, not current reachability or approval of a
current claim. Current semantic qualification remains the separate qualification
owner's exact scoped decision. These reports keep `authorized` and
`evidence_accepted` false.

An explicit `ReviewServicePlan` in both policy version 3 and execution-profile
version 8 declares evaluator/critic role slots, ordered approved configurations,
and one immutable `ExecutionRoleLimit` per slot. Earlier policy/profile documents
keep their existing behavior. Every selected target retains its actual
configuration, controller, model, prompt and qualification commitments in the
native journal and signed review. Another target's qualification cannot cover a
stale vote. Parent signing requires the actual measured producer application,
owned reviewer output, complete current packet and canonical mechanical checks;
worker inputs contain no signing keys.

Automatic evaluation uses the existing run journal, reservation, invocation,
intake and application owners. Only known predispatch service unavailability or
authenticated settled service failure may use an already approved alternate,
within the frozen retry grant, backoff, run deadline and shared role-slot budget.
Unknown external work retains its reservation; a failed or unknown semantic
verdict never buys another service vote. Direct reservations obey the same
selection and timing rules. A known final ineligible output settles as a retained
refusal without signing or redispatch, while uncertain effects remain pending.
Missing configured signing credentials, qualified identity or controller
separation produces a finite blocked result under the same valid host context.
Deleting or changing the foundational authority invalidates the immutable host
selection and yields a typed custody conflict without new records; it does not
authorize a finish under changed custody. Key/envelope policy changes remain host-owned, and source
rights still come from the canonical usage owner.

`session.review_services()` requires `research_session.review_services` and
`service_authentication.status` in addition to the existing current-read grants.
The automatic bridge uses the ordinary model/evaluation/intake and native
registration grants; the schema read creates no authority. Alternate-service
routes combined with `DisagreementPlan` are explicitly unsupported in this
version. Single-configuration service plans and existing legacy disagreement
profiles retain their supported contracts. Native host protocol observations do
not by themselves establish live model quality, worker understanding or operating
system isolation.

API version 40 / operation matrix version 23 adds `session.repair_progress()`
and `ws.strict.repair_schemas()`. Progress is a bounded read of the existing
native execution journal, with `original_id`, `after_sequence` and `max_records`
selectors (at most 32 records). `historical_effect_recorded` reports an earlier
owned application; `current_review` and `fresh_independent_reviews` report
separate current native observations. Neither a material difference nor a
retained effect authorizes acceptance. Missing, changed, expired or withdrawn
source dependencies withhold historical field and material commitments and
make the outward basis unavailable. The read retains protected usage custody
through final source, host selection and journal checks. Its additional grants
are `research_session.repair_progress` and `repair_progress_owner.current`.

Explicit `ServiceRepairBinding(service_plan.sha256, "sha256:" +
 disagreement.sha256)` binds the exact immutable `ReviewServicePlan` and
`DisagreementPlan` into execution-profile version 9. It enables scoped repair
with approved alternate services, preserving actual target configuration,
controller, current qualification and native envelope alongside each logical
required slot. Version 8 keeps its existing unsupported alternate/disagreement
combination. Earlier profile documents retain their serialized bytes and exact
retained-attempt recovery. New scoped repair attempts use a narrower worker input
and a deterministic parent merge that preserves unrelated claims. A failed
semantic vote cannot select an alternate to obtain a different verdict, and
unknown occupied work retains its reservation.

Repair preserves the target identifier, every peer claim, original questions,
source journal and frozen criteria. Strict v2 quantitative claims retain typed
native calculation references, exact configured arithmetic, units, time and
invariants. A new expression requires the current native engine result; a model
cannot replace its value or waive an invariant. Intrinsic material differences
exclude artifact bookkeeping while native approval still checks exact current
result and source identities. Every material revision requires fresh independent
first passes. The existing per-claim limit of two repairs, frozen global limits
and actual role budgets end unchanged, oscillating, unknown or irreparable work
with an unresolved original; no human review is manufactured. Reference-process
protocol simulations establish bounded control behavior, not live model quality
or scientific semantic approval.

Scoped repair sends only the target and its permitted decisive premises to the
worker. Full prior inventory and exact dependency commitments stay in the
parent's authenticated journal. A deterministic native admission projection
merges untouched peers, records the exact actual worker proposal commitment,
and credits only the targeted claim as repair-produced. Preserved peers retain
their earlier genuine producer. Raw coverage notes and historical model prose
are withheld; current typed failure criteria and permitted evidence remain.
The full-inventory admission profile requires every retained dependency to have
an exact current permitted source closure. A denied, stale or unavailable peer
therefore stops before repair dispatch; it does not gain a grant through a merge.
Progress rechecks actual target qualification and native review age at read
closure, so unchanged material alone cannot keep an expired approval current.

New protected scoped repair dispatch also requires an immutable
`NativeInputHandoff(delivery_seconds=30)` on the final execution profile before
qualification and session binding. This produces profile version 10 and declares
the exact bundled `BoundedSubprocessExecutor` route. Its ceiling is 1–30 seconds,
including preflight, and never extends the original run or job deadline. The
executing thread retains the native source-use read lease through synchronous
stdin delivery, rechecking current scope, source material and expiry before each
chunk and at closure. Parent-owned delivery records bind the exact job and full
worker input before application. Generic adapters, subclasses and returned ACKs
do not satisfy this boundary. Begun, partial, timed-out or otherwise unknown work
keeps its occupied reservation; polling and historical replay never resend the
input. Complete delivery does not establish model comprehension. Older serialized
profiles remain unchanged, but cannot authorize a new protected repair handoff
without this declaration.


API version 41 / operation matrix version 24 adds `session.resolution()` and
`ws.strict.resolution_schemas()`. The session method accepts no arguments and
requires a bound `ResearchExecution`, `research_session.resolution` and
`resolution_operations.current`. The schema accessor returns the closed
`research-question-resolution/v1` document and follows the workspace lifetime.
The session read remains available under the existing historical read-only scope;
it cannot dispatch, sign a review, resolve a question or authorize an external action.

The report accounts for every frozen original and its derived question identifiers.
Each original reports `accept`, `partial`, `abstain` or `reject`, with unresolved
questions, missing identifiers and global blockers retained. A derived subset can
receive partial credit only when the frozen policy permits it and each included
question has current canonical publication, complete owned mandatory reviews,
genuine production and current actual reviewer qualification. Such a subset does
not increase the run's accepted-original count. A coarse rejected run cannot reject
all originals; a local rejection needs its current native claim decision.

Eligible content contains the permitted native question and claim fields, including
strict v2 calculation references. Private native verification commitments are
withheld. `current_checks` summarizes the current source/mechanical, mandatory
review, actual target qualification and producer/context checks for each eligible
question. It carries no reusable approval receipt. `current_basis_id` is a separate
commitment to the visible report content, or null when no content is eligible;
`publication_basis_id` and `current_publication` are null. The full workspace and
native review commitments remain private, so denied unrelated material is not
included in an outward hash.

Resolution holds current native source-use custody through an independent closing
pass over publication, journal, policy, mandatory reviewer identities, qualifications,
review age, full required packet delivery and budgets. Unknown occupied work,
per-job or shared-role overruns, hard global blockers and changed or withdrawn
material withhold eligibility. A closing change refuses the read rather than
returning an earlier snapshot. Legacy structural answers and generic latest-review
flags cannot substitute for authenticated mandatory reviews. Current acceptance,
historical terminal lifecycle and external-action authority are separate fields;
`authorized` and `external_action_authorized` remain false. This read establishes
current owner eligibility under the declared contracts, not semantic truth or
permission to publish outside the workspace.


API version 42 / operation matrix version 25 adds
session.brief(format="structured") and ws.strict.brief_schemas().
The argument-free structured default is a caller-owned
research-current-brief/v1; format="markdown" returns
research-brief-markdown/v1 with exact UTF-8 output and structured-content
digests. Both read the installed current resolution under private native
closing custody. Findings and inferences retain their typed qualification,
scope, time, units, evidence, premises and calculation references; every
original/derived unresolved outcome and limitation remains explicit. Markdown
uses fixed headings and literal content escaping, with no recommendation or
substantive prose generation. Oversized complete content refuses rather than
being silently shortened. Native verification/global material commitments,
historical lifecycle and external-action authority remain separate.

The public and inner read grants are research_session.brief and
brief_operations.current, alongside research_session.resolution and
resolution_operations.current. A read never starts or renews model work.
Schema discovery is inert and follows the existing Workspace lifetime.

Optional ReadabilityPlan selects execution-profile/v11 and a distinct exact
critic/evaluator configuration, controller, immutable
ReadabilityCalibrationSelection and ReadabilityInputHandoff. Freeze the final
profile and all role/model/settings/rubric/reference selections before either
ordinary or readability qualification and session construction. Omission
preserves older profile documents. The existing CalibrationAuthority issues a
readability-specific protected report with readability=True; an ordinary
reviewer or auxiliary-adjudicator report cannot qualify this purpose. Protected
references reproduce finite printable-ASCII scalar/presentation associations,
including qualification, scope, time, units, evidence, limitations and unresolved
outcomes. This scope does not qualify unrestricted language quality, real
provider consumption or scientific truth. Unsupported scalar/context scope
produces a bounded unresolved outcome.

The core brief retains the resolution's bound of 100 original questions. A
selected critic's complete job scope is limited to 64 originals by the native
job contract; larger inventories refuse that critic operation without
partitioning, dropping originals or implying qualification of a subset.

For legacy ordinary qualifications, CalibratedEvaluators can explicitly compose
one protected readability report with legacy=an_exact_QualifiedEvaluators.
Its identity binds that immutable owner and preserves the original ordinary
qualification rules; it does not relabel or promote an old certificate. This
combination requires the explicit readability plan and profile.calibration=None.
With protected ordinary calibration, use the existing CalibrationPlan and
distinct readability selection instead. Actual current credentials, issuer
record expiry and correlated host configuration remain required independently.

Configured execution schedules readability before terminal release or bounded
soft exhaustion of a permitted derived subset. It uses the same HMAC journal,
actual reservation/intake, model/tool/cost/time caps and original accounting.
The worker receives a complete masked permitted inventory and the same content
presentation as the public formatter, with parent keys, alias maps, prior
reviewer identities/verdict details and diagnostic prose withheld. Output is
research-readability-issues/v1: bounded enum codes, existing field pointers and
exact field hashes, or explicit abstention. Replacement prose, grants,
recommendations and approval are rejected.

Only the exact bundled BoundedSubprocessExecutor can perform a new protected
readability invocation through its typed native stdin lease. Final serialized
worker bytes are bounded; actual executing-thread source-use custody,
current material, terminal state, credentials/qualification and finite
wall/monotonic deadlines are checked through delivery and closure. Generic
invoke refuses the readability purpose. Completion is transport evidence, not
model comprehension or current acceptance.

The native owner prepares protected critic input only for the exact newly
reserved job. Its pending usage remains unknown and its maximum reservation
counts against committed budgets. Public resolution and brief reads continue
to withhold pending work; existing attempts reconcile rather than dispatch again.
The first dispatch observes the exact job's configured service before source
custody, then obtains fresh input proof. The original delivery deadline includes
that observation and all opening, transfer and closing checks. An application
report uses its own retained observation; an unrelated later observation cannot
renew it. New work and application require that short observation to be live.
A completed application binds its actual check time into the authenticated
journal. Later reads validate the original configuration observation at that
check time, while checking issuer expiry, credentials, plan, program, content,
strict eligibility and source rights at the current time. This historical
execution configuration does not report current remote service availability.
Older application records without an authenticated check time cannot supply
current readability qualification.

The selected critic's complete persisted input must also fit the existing
65,536-character canonical storage field and 262,144-byte escaped journal
record. These storage limits may refuse an input below the worker's byte cap;
complete input is never truncated to fit.

After external wait, a fresh full current resolution and purpose qualification
must reproduce the exact scientific content and private binding before semantic
application or current reporting. Authenticated usage settlement remains
separate from permission to apply a result.
Claim issues enter existing stable-target/peer-preserving repair, then require
fresh ordinary independent actual-target reviews and a new current brief.
Non-claim/global issues re-evaluate their owner or refuse formatting; they
cannot invent a repair target. Unknown delivery, effects or billing retain
occupied reservations and reconcile without redispatch. Structured reads show
pending/refused critic state; a configured required critic must be currently
observed before Markdown is returned. Final dedicated issuer, authenticated
application configuration and credential checks occur after native resolution
work and at the observed return boundary.
Historical terminal acceptance never restores current eligibility or grants
external delivery.

If parent intake is interrupted after protected critic delivery, the same live
bundled executor can recover its exact authenticated first dispatch receipt.
This private retention is bounded to eight MiB per executor. Exhausted or lost
retention, including host restart, stays unavailable; it never proves dispatch
absent. Missing intermediate observations remain unknown until authentic
receipts arrive. Recovery settles history without resending input or renewing
current qualification, source rights or semantic eligibility.


### Retained decision history

`workspace.research.history(run_id=..., execution=..., kind=None,
after_index=0, max_records=16, expected_history_basis_id=None)` reads recorded claim inventories, retained
normalized sources, signed review snapshots and completed literal briefs.
It requires `history_operations.current`, current host roots, protected
original source scope and the original observer identity supplied through
`ResearchExecution`. It invokes no executor, model, review signer or provider; the original observer
authenticates the continuation basis locally.
The read is independent of a live session's current original-question mapping;
the original workspace identity and authenticated journal must still match.
An unchanged protected legacy declaration supports read-only scope when its
exact original instruction hash and request schema validate. This compatibility
path grants no requirements revision, canonical planning or recovery authority;
append-only declarations continue to require their protected native lineage.

Each row identifies historical qualification as authenticated native recording,
which establishes no semantic truth or current approval. Native application
recording remains separate from an inert proposal. Every row reports current
eligibility as `not_evaluated` and keeps authorization and evidence acceptance
false. Current acceptance requires an ordinary fresh resolution or brief read.

Current source scope and retrieval/export rights are checked for the exact
retained revisions under both research and export purposes. Material edits
need not erase permitted history. Denied records withhold payload and historical
application/qualification details. Missing, expired, deleted or unsupported
retention produces unavailable evidence or a typed refusal. Older unsupported
journal encodings are not treated as empty successful history. Brief history
uses a conservative closure of recorded scientific sources; ambiguous revision
closure remains unavailable rather than authorizing disclosure. Claim premises
and computational snapshots with unsupported dependency closure are unavailable.

Pages contain at most 32 records and one MiB; a normalized source is at most
65,536 bytes. Continuation requires the returned opaque `history_basis_id` as
`expected_history_basis_id`; a changed authenticated journal or selection refuses
the cursor. Pages report exact total and returned counts and truncation.
`next_index` is explicit continuation, and page completeness is
history inventory coverage rather than research acceptance. Retained content is
never truncated to fit an accepted result. Host services own retention, deletion,
backup and restoration of the native journal and protected source/delivery
stores. This read creates no replacement snapshots or recovery authority.

### Checked local delivery

The host freezes `ArtifactDeliveryPlan` and `ArtifactDestination` on its final
`ExecutionProfile` before binding a session or qualifying its configured roles.
Destinations are opaque references to unique Markdown filenames inside
`runs/<run_id>/deliveries/`. The initial adapter requires native POSIX descriptor
operations, supports at most 16 destinations and one MiB per artifact, and never
replaces an existing file. Remote destinations are unsupported.

```python
from evidence_wiki import ArtifactDeliveryPlan, ArtifactDestination

delivery = ArtifactDeliveryPlan(
    config_ref="release", controller_ref="release-controller",
    destinations=(ArtifactDestination("accepted-brief", "brief.md"),),
    timeout_seconds=60, tokens=4096, cost_limit="1",
)
```

The selected host-worker configuration must use task role `release` and explicit
`model`, `eligibility.read` and `release.propose` scopes. The frozen run policy
must permit model and delivery effects with host capability references. The
host supplies matching controller selection, model executor, current evidence
qualification, write paths and operation grants. A proposal cannot create any
of those capabilities. `contract()["research_artifact_delivery"]` supplies the
four closed schemas and supported limits.

`prepare_delivery()` returns current exact artifact identity and approved
destination references. It grants no acceptance or publication authority.
The configured autonomous workflow invokes one bounded Release worker to select
that identity and a destination. Output text, paths, replacement content and
approvals are rejected. Native publication rechecks current eligible evidence,
mandatory review, source rights, the exact rendered bytes and frozen policy.
Its private occupied reservation does not make ordinary pending brief reads
eligible. Staged publication retains the actual inode and checks destination
custody before linking and before protected owner completion.

Public grants are `research_session.prepare_delivery`, `research_session.deliver`,
`research_session.delivery_status` and `research_session.reconcile_delivery`.
Their corresponding inner grants are `artifact_delivery_operations.prepare`,
`artifact_delivery_operations.deliver`, `artifact_delivery_operations.delivery_status`
and `artifact_delivery_operations.reconcile_delivery`. Current artifact reads
also require the documented brief and resolution grants.

A protected completion receipt can settle a lost journal return through
`reconcile_delivery(request_id=...)` without repeating the write or agent call.
Recovered usage records UTC lifetime including recovery wait; unknown usage is
never recorded as zero. A file linked before final owner checks completed stays
`commit_unknown`, even if identical bytes are present. Retained live writers are
bounded to 16; exhaustion or loss of custody does not justify adopting a file
or retrying publication. Terminal accounting cannot be reopened.

Status reads create no effects and report history separately from current
eligibility. Artifact, destination and receipt references are withheld when
current identity or eligibility cannot be established. Terminal annotations or
later journal changes can change the rendered identity; historical delivery
does not renew approval. The local adapter reports `artifact_checked` and makes
no `host_enforced` or semantic Release qualification claim.
Changing the bound authority policy invalidates the immutable session and
returns a typed binding refusal. Source revocation under the existing policy
does not restore disclosure or permit terminal reconciliation.

### Local source and requirements changes

`workspace.research.changes(pairs=[{"before_source_id": source_id,
"before_revision": revision_id, "after_source_id": source_id}],
before_requirements_revision_id=None)` compares explicitly selected retained
revisions with current local originals. It requires `change_operations.current`,
a protected original requirements binding, current host path scope, and native
source usage custody. The optional requirements selector names an authenticated
append receipt; omission compares the preserved original requirements with the
current effective requirements.

The report separates raw content, normalized content, extraction metadata,
capture provenance, current rights, source scope and requirements. Exact byte
identity does not imply identical provenance or semantic identity. Denied
inputs expose no protected hashes or evidence references; missing or unsupported
retention produces unavailable dimensions. Local comparison neither contacts
remote sources nor grants current acceptance. Sources and original requirements
remain unchanged. At most 32 pairs and 32 distinct source IDs may be selected;
retained inputs and the output each have a 1 MiB bound. Unsupported original
path bindings are unavailable. Whole-workspace closing checks refuse concurrent
changes, and a closed workspace cannot perform the read.


### Current strict impact

`workspace.research.impact(pairs=None, before_requirements_revision_id=None)`
requires a current `HostContext`, `workspace.open` and
`impact_operations.current`, protected source usage and a configured strict policy.
It returns `research-current-impact/v1`, recomputing the full strict owner before
reporting claim acceptance, review reasons and claims requiring review. Missing,
denied or changing strict inputs return a typed refusal rather than eligible results.

Each current claim exposes its declared direct and transitive source dependencies,
premise claims and calculation references. Retained native reviews explain known
claim, premise, question, policy, permission and implementation differences.
`conservatively_invalidated_claim_ids` identifies latest retained review bases that
no longer match the current whole-workspace strict basis or whose global review
authority has changed. Even a claim without a
known changed source can require review. New current reviews remain subject to
native acceptance; the dependency map does not enable selective approval reuse.

Optional `pairs` use the same bounded source selectors as `research.changes` and
additionally require `change_operations.current` and protected original requirements.
The selected local comparison is joined to the strict evaluation under closing
workspace and authority checks. Disclosed retained revisions are rechecked in
the held strict usage view through return. Unknown comparison dimensions remain explicit;
absence of a selected source change never proves absence of wider effects.
Pairs between different source IDs remain comparison candidates in
`comparison_affected_source_ids`; they cannot establish a changed dependency
through semantic identity inference. Without pairs, source comparison is
reported as not requested. A requirements
revision selector requires pairs.

Policy, instructions, questions, permissions, implementation, computation and time
remain global dependencies. Legacy assessment refresh retains its own declared
contract and cannot grant strict approval. The operation performs no remote fetch,
review registration, assessment refresh or executor invocation. Output is bounded
to the strict owner's 100 current claims and one MiB; overflow is a refusal.


### Autonomous decision refresh

```python
result = workspace.research.refresh(
    policy,
    run_id="updated-decision",
    previous_run_id="original-decision",
    execution=execution,
    request_id="update-request",
    planner_config_ref="change-planner",
    max_steps=64,
    source_updates=(),
)
```

The host explicitly allocates the new run and supplies a configured planner with
`model` and `plan.propose` capabilities. Its authenticated proposal selects the
exact native preparation action from a bounded packet containing the parent
link, permitted material changes and immutable acquisition requests. The planner
cannot expand source scope, controller grants or budgets. The parent must have
an authenticated terminal decision and closure under the same retained observer
and execution profile;
its original question identities must match. Another active run using those
originals prevents preparation.

Refresh creates a unique material marker, reopens the original questions using
protected native mutation preimages, admits fresh drafts and obtains fresh
mandatory independent reviews. Even identical evidence does not revive prior
acceptance. Previous run files and measured usage remain retained. The new run
uses the caller's explicit per-run budget allocation; prior unknown external
accounting blocks work rather than being reset or treated as zero. Existing
lifecycle, qualification, role, source permission, retry and step bounds apply.

At most sixteen frozen `source_updates` may be provided. Each contains exactly
`source_request_id`, `expected_request_basis`, `metadata` and a distinct relative
`path`, using the existing acquisition input contracts. Collection follows the
existing permitted transport, protected capture, inventory, normalization and
fulfillment owners before generation. Updates cannot authorize new destinations,
providers, rights or claims. A refused update remains unresolved in the report.

The host requires `research_session.refresh`, `refresh_owner.apply`,
`research_session.run_cycle`, `research_session.brief`, `research_session.resolution`,
`brief_operations.current` and `resolution_operations.current`, alongside the
existing run, mutation, worker, review and optional acquisition grants. A terminal
parent is linked to a distinct new run; refresh does not reopen its journal or
extend its deadline. A changed refresh head refuses a fork. Replaying the same
new-run request preserves historical cycle identity and re-evaluates the current
brief; altered arguments refuse.

The checked `research-refresh-result/v1` report includes retained parent outcome,
reused and changed permitted source identities, new work and previous usage,
collection status/error summaries, unresolved update requests, and the current
superseding brief. Historical source IDs are filtered against actual currently
permitted packets while the native brief remains held through report construction;
protected retained collection receipts are not exported. Accepted or partial refresh requires a currently eligible native brief;
a retained successful cycle can subsequently report abstention when current
eligibility changes. Previous current eligibility is explicitly unevaluated.
Protected recovery proves exact desired state separately from historical effect
completion. Partial or uncertain publication never grants review acceptance or
justifies redispatch; it can leave the refresh blocked. The report grants no
external action authority. `refresh_schemas()` returns its detached schema without
performing refresh work.


### External host scheduled refresh

```python
from datetime import datetime, timedelta, timezone

schedule = workspace.research.schedule_refresh(
    policy,
    schedule_id="weekly-check",
    previous_run_id="original-decision",
    execution=execution,
    planner_config_ref="change-planner",
    interval_seconds=3600,
    expires_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
    max_runs=2,
    max_steps=64,
    max_notices=64,
    source_updates=(),
)
status = schedule.poll(request_id="poll-1", notices=())
result = schedule.advance(request_id="dispatch-1")
page = schedule.current(cursor=status["cursor"], max_events=16)
```

An external host supplies the wake-up mechanism and stable command IDs. Registration
stores the exact `RunPolicy`, approved acquisitions, execution/qualification/signer/
observer identities, current host selection, owner generation and root/control
identities. It authenticates the original terminal parent and signed closure.
Reopening the same registration retains its counters, frozen policy and deadline;
changed configuration refuses. The owning Workspace lifetime applies to every
handle call. No daemon, timer, notification sink or provider is discovered or started.

`refresh_schedule_schemas()` returns the detached plan, notice, status and cursor
schemas. A notice contains `schema_version="research-refresh-notice/v1"`,
`notice_id`, `source_id`, positive per-source `sequence`, and nullable `revision_id`.
The revision is an inert hint, not an attestation. A batch contains at most 32
notices. IDs deduplicate exact payloads; conflicting duplicates refuse. Older
source sequence numbers are retained as ignored observations. Current permitted
source packets determine which hints can affect the queue. Notices cannot supply
URLs, paths, acquisition metadata, policies, new controllers or grants.

Polling also examines native material identity, current native ineligibility and
the stored due time. Missing or out-of-order delivery cannot make stale evidence
eligible. Multiple changes coalesce into one queued native refresh; an obsolete
queued job is cancelled before replacement. Already reserved or uncertain work
retains its identity and allocation. `current()` is an effect-free queue read;
`poll()` reconciles queue data without dispatching, and `advance()` performs that
reconciliation and at most one new reserved refresh allocation. Duplicate advancing
command IDs cannot invoke another run. Historical cursor pages describe control
observations rather than current evidence eligibility.

Advance requires `refresh_scheduling.advance` and the existing native refresh
and current-brief grants. Registration, queue polling and reads require respectively
`refresh_scheduling.register`, `refresh_scheduling.poll` and
`refresh_scheduling.current`, alongside the existing scoped session/native read
requirements. Current policy compatibility, qualified evaluator services, terminal
grants and immutable host/execution selection remain required. Losing native
scope blocks dispatch and cancels queued work; changing the bound host identity
refuses the handle. Scheduler status grants no authority and exports no protected
evidence or retained acquisition receipts. `recorded_usage` distinguishes known
scheduled-dispatch totals from accounting completeness.

The private schedule namespace uses native descriptor custody and atomic authenticated
state comparison. Its dispatch lease stays separate from the workspace gate;
research and worker calls occur after that gate is released. A lost host return
can reconcile only the same existing canonical refresh. If dispatch was reserved
but no native run is retained, the schedule exhausts with unknown accounting and
never redispatches or creates a replacement run. Missing/corrupt schedule retention
is a refusal, not a new ledger. An external host should retain the returned signed
cursor; forged, cross-binding or unavailable cursor prefixes refuse.

A registration bounds new work to at most eight explicitly allocated runs, each
under the stored policy and at most 64 cycle steps. It retains at most 64 distinct
notices, 128 distinct control commands and 256 events; pages contain at most 32
events. Run/notice/command/event exhaustion is terminal. The expiry must initially
be in the next 30 days and stops new allocations. Already reserved work can only
be reconciled within its original native deadline and budget. Unknown accounting
in the parent refresh ancestry or a dispatch blocks further work. A later fresh
allocation is an explicit host decision; delivery cannot renew old budgets or
acceptance.


### Bounds and performance qualification

```python
report = workspace.research.qualify_performance(
    policy,
    execution=execution,
    request_id="native-performance-check",
    benchmark_config_ref="benchmark-worker",
    critic_config_ref="performance-reviewer",
    seed=17,
)
```

The host explicitly selects a scoped integration worker with `model` and
`integration.propose`, and an operationally independent evaluator controller with
`model` and `verdict.propose`. The worker chooses the order of the complete
frozen twelve-workload catalog; it cannot omit a required case, change thresholds,
expand input sizes or select arbitrary commands/paths. An authenticated accounting
critic checks the actual measurement set, and the fixed deterministic gate decides
`passed`, `failed` or `incomplete`. The method requires
`performance_qualification.run`, immutable `RunPolicy` permission for model and
offline checks, current controller mapping and a private mutation control directory.
Schema discovery is a detached parent-lifetime read and starts no agent or probe.

Workloads include small inputs based on the frozen policy budgets, the native
boundary and one controlled overflow for strict claims, assessment change hints,
review packet sources and whole JSON output bytes. These are synthetic native
codec fixtures, not evidence adoption, semantic review, assessment issuance or
publication. Current capability limits remain independent: strict claims 100 and
1 MiB; assessment change hints 128 and 1 MiB with native assessment page bound 32;
paged review sources 64 and 512 KiB; whole JSON reads 1 MiB with their own text,
node and depth bounds. No task can raise those native limits.

A fixed fresh child interpreter measures each workload's first call, same-process
warm repeat and two changed revisions. Descriptor validation precedes the first
call, so `cache_scope` describes that boundary precisely rather than asserting
that every imported module/cache is cold. Each sample identifies its canonical
input hash/size, construction time, validation time, serialization time and Python
traced peak allocation. The child also reports process RSS high-water; these two
memory scopes are not whole-system allocation estimates. Hardware records include
platform, architecture, Python and CPU count. Authenticated agent lifetime and
usage accounting stay separate from native probe time; provider latency is not
inferred from a model label or host process time.

Fixed qualification requires all twelve cases, all four samples per case, exact
expected native boundary outcomes and three distinct material input revisions.
Per sample, validation and serialization each stay within one second and traced
peak within 64 MiB; each probe stays within 30 seconds and process peak RSS within
256 MiB. Missing, unknown, nonfinite, truncated or unaccounted observations cannot
qualify. Positive measured model use and complete host usage fields are required
for both agents. The critic's declared completeness cannot replace those owner
checks, and output decoding reproduces the deterministic gate from recorded data.

Expected overflow includes an explicit maximum-unit/byte partition suggestion.
It is neither a complete result nor a cursor permitting approval reuse: every
partition needs native scope and fresh full review. The performance report always
sets `authorized` and `evidence_accepted` false, even when performance qualifies.
It does not certify scientific correctness, adversarial worker isolation, genuine
model quality or every workspace owner flow.

Private authenticated CAS freezes policy, role/service identities, host selection,
owner generation, root/control identities, seed and workload catalog. Two bounded
agent reservations are retained before invocation; known-final observations settle
against original per-job and aggregate policy ceilings. Lost observations can only
poll the same retained attempt, never reset or redispatch it. Missing existing
retention refuses. Exact request replay returns retained performance evidence as
historical; it does not renew the environment or scientific eligibility. Source
workspace files remain unchanged, and no daemon or external provider is created.

The separate offline scale tool measures generated workspace inventory,
normalization, status/cache and indexed-query performance. Its fixed profile gate
also requires complete counts, clean normalization/lint/status/query results and
finite measured timings, memory and output. That pipeline evidence complements
the codec report; neither scope substitutes for the other.


Version `52` adds `ws.research.command(request, runtime=...)` and
`command_schemas()`, with operation matrix version `35`. Caller commands `next`,
`start`, `resume`, `heartbeat`, `acquire`, `ingest`, `research-export` and `progress`
use scoped `Onboarding` methods. Discovery has finite ceilings of 128 resources
and 128 operations, shared by its readers and exported schemas. `research_progress` is read-only unless the host
selects an external output root and grants `research_progress.write`.

The autonomous command adapter shares its public dispatcher with Python:

```python
from evidence_wiki import HostRuntime, Workspace

runtime = HostRuntime.from_services(policy, context, execution)
request = {"schema_version": "research-command-request/v1",
           "operation": "run-cycle",
           "arguments": {"run_id": "study", "request_id": "drive", "max_steps": 64}}
with Workspace.open("WORKSPACE") as workspace:
    response = workspace.research.command(request, runtime=runtime)
```

```bash
evidence-wiki agent run-cycle --target WORKSPACE --runtime PRIVATE_RUNTIME.json \
  --run-id study --request-id drive --max-steps 64 --noninteractive --format json
evidence-wiki agent command-schemas --format json
```

`policy`, `context` and `execution` above are existing host-selected `RunPolicy`,
`HostContext` and `ResearchExecution` services. Python service construction retains
the full supported execution configuration. The CLI file loader is deliberately
finite: its `research-host-runtime/v1` object requires `policy`, `context`,
`profile`, `commands`, `cwd`, `meter`, `keys`, `qualification_authority`,
`qualification_records` and `review_signer`, alongside `schema_version`.
`policy` is `RunPolicy.to_dict()`; `profile` is the exact `ExecutionProfile.document()`
for versions v1-v3, including optional scheduling and role scopes. Advanced profile
versions and calibrated registry services are refused by this file loader.

`context` supplies the explicit `HostContext` constructor selections, including
allowed/writable roots, grants, actor/controller references, authority file,
state directory and mutation control directory. `commands` maps frozen role
configuration references to executable argument arrays; `cwd` selects their
working directory. Executables are trusted host selections, never paths selected
by a worker proposal. `keys` contains lowercase hexadecimal `observer` and
`receipt` keys; `qualification_authority` supplies `issuer_ref`, `key` and
`generator_controller_ref`. `qualification_records` contains existing signed
records, without issuing or promoting qualifications. `review_signer` contains
`authority_policy_id`, `authority_revision`, `generator`, `reviewers`,
`expires_utc` and `not_before_utc`. Each signing identity contains `config_ref`,
`principal`, `key_id`, `controller_ref`, `role` and its hexadecimal `key`.
Keys must contain 32-64 bytes. Keep the runtime outside worker input paths.

The file must be a regular file owned by the current user, with no group/world
permissions, no hard links and no symlink components; reads are limited to 1 MiB
and validate file and directory identity. Unsupported platforms refuse this
loader. `meter: "native_process"` is the host's explicit selection for local
executables that make no model/provider calls: it records process seconds, one
tool call and zero model calls/tokens/provider charge. It cannot meter a process
that calls a provider. Select `"unobserved"` when that accounting is unavailable;
unknown observations cannot qualify research. A custom real host meter remains
available through Python service construction. Runtime loading does not discover
plugins, provision credentials or create grants.

Operations are `run-cycle`, `refresh`, `current`, `next-work`, `brief`,
`resolution`, `recovery-status`, `changes`, `impact` and `history`. The shell uses
`--request-file` for an operation argument object, with `--run-id`, `--request-id`,
`--previous-run-id`, `--planner-config-ref` and `--max-steps` as common overrides;
conflicting duplicates refuse. All reads preserve the native scoped permissions.
`run-cycle` and `refresh` can mutate canonical run, question, draft and review
state only through the existing owner grants. Refresh requires a retained previous
run and the same native planner qualification. Neither adapter launches a daemon.

JSON stdout contains exactly one bounded `research-command-result/v1` envelope,
with either the unchanged public result or exact typed error fields. Typed
refusals retain their native exit code; returned negative/blocked/budget outcomes
exit `3`, and accepted or partial outcomes exit `0`. Brief abstention/rejection
also exits `3`. `--format text` renders a concise summary. The envelope's
`terminal: true` means the command invocation has settled; a capability refusal
has not created or completed a research run. `authorized` and `evidence_accepted`
remain false. No operation reads user stdin or prompts for semantic choices,
review or signoff; `--noninteractive` states the behavior already always enabled.
Stdin file selectors (`-`) are refused. Canonical research acceptance remains
separate from command transport success and retained historical replay.


Version `53` adds `scoped-research-tools/v1` to the optional scoped onboarding
MCP server, with operation matrix version `36`. Its finite tools share the public
research command definitions and dispatcher with Python and CLI. Startup selects
roots, mutation grants and native `HostRuntime`; tool arguments cannot create
credentials, policy, grants or machine qualification. Current/next execution
reads require an explicit stable run ID, even before the run starts.

The command family also includes public session role steps, acquisition,
calculation, review-packet assembly, artifact delivery, pending/cancel/reconcile,
repair/service observations and current recovery. Optional host-selected
`OperationControl` passes through command/run-cycle/refresh/bounded drives for
cooperative safe cancellation; it never enters request JSON. `close()` releases
an idle cancellation probe and makes the control terminal without renewing its
clock. Reconnect preserves native original deadlines and consumed allowances.
See [scoped autonomous research tools](../workspace-template/docs/agent-contracts.md#scoped-autonomous-research-tools)
for startup, schemas, no-elicitation behavior and explicit capability limits.
The legacy workspace MCP protocol is unchanged.
