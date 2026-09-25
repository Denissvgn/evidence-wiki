# Agent data and execution contracts

EvidenceWiki separates the calling agent's research judgment from deterministic
workspace operations. An artifact describes inputs or observations; it grants no
permission to execute a command, load a provider, fetch a URL, or write a path.

## Availability and schema access

`evidence-wiki contract` retains its full installation contract and adds
`onboarding_contract`. Its `schema_ids` are a closed inventory. Fetch one schema
without a workspace or package-asset extraction:

```python
import json
from evidence_wiki.onboarding_schemas import schema_document

schema = schema_document("onboarding/research_request/v1")
print(json.dumps(schema))
```

The accessor returns a fresh Draft 2020-12 JSON Schema with no external
references. IDs cannot be paths, URLs, or arbitrary schema documents. Access and
structural validation perform no filesystem writes, subprocesses, provider
imports, or network access. Operational Python and MCP onboarding have separate
root and operation grants, described below.

## Scoped Python onboarding

Library API version `13` adds `evidence_wiki.Onboarding`. Existing `Workspace`
handles retain their authority and lifetime. A new handle defaults to read and
planning operations; the host selects roots and grants mutations explicitly:

```python
from evidence_wiki import Onboarding

with Onboarding.open(allowed_roots=["/research"], allow=["apply"]) as host:
    bootstrap = host.bootstrap()  # installation only; no current-directory scan
    contracts = host.contracts()
    proposed = host.plan(setup_request)
    checked = host.check_plan(proposed)
    if proposed["setup_ready"] and checked["status"] == "current":
        result = host.apply(proposed)
```

Use the interpreter containing the selected package. The handle binds package
code, resource catalog, API version and interpreter identity. Restart the host
process after an installation change: another handle cannot reload cached Python
modules. Closing releases its root descriptors; returned content
belongs to the caller and shared extracted assets remain owned by the process.
Handles never implicitly close other handles. Calls are synchronous; serialize
operations on one handle and retain an outer timeout.
Root descriptors and mutation publishers use native POSIX or Windows filesystem
operations. Windows uses local-drive handles and rejects reparse points; private
host state is checked through owner and ACL information. Rootless content and
bootstrap access do not create these descriptors.

`host.contracts()` and `evidence-wiki agent extensions` return the same extension
schemas, command routes, recipes and MCP declarations. The operation matrix in
`contract().library_api` describes every public method. Plan/apply methods accept
decoded objects or bounded UTF-8 JSON bytes and return canonical owner payloads.
CLI `--output` publication is a separate effect. `bootstrap()` returns the
capabilities envelope; `bootstrap(target)` returns the workspace-aware envelope.
Resource methods return the CLI envelope's payload. Pack inspection returns full
owner observations before CLI filtering/summary formatting. Errors retain
`EvidenceWikiError` subclasses, stable codes, exit codes, recoverability and
remediation. Unknown codes retain the base-class fallback. Closed handles raise
`WORKSPACE_UNREADABLE`.

Methods cover pack discovery/decisions and local drafts, inspection/capture,
setup, revisions/migration/composition, fleet proposals, host transitions,
instruction installation, current-agent research coordination and computation.
Catalog registration, semantic assessment/acceptance, network acquisition,
managed model runners and arbitrary executable/provider probes retain their
separately authorized owners outside this scoped interface.

Root grants cover catalog member roots and setup coordination directories as
well as workspace data. Bundled read-only assets remain discoverable without a
root grant. Escaping links and overlap with protected host authority refuse.
Grants do not authenticate a reviewer, grant source-use permission or provide OS
process isolation. Current strict review and `research_export` remain the final
eligibility boundary. Setup, transport, arithmetic and migration success cannot
replace it.

## Optional onboarding MCP server

```bash
evidence-wiki serve-onboarding-mcp --allow-root /research --allow-operation apply
```

With no roots, this separate process can bootstrap and serve installed resources.
Mutation tools appear only for the exact operation names the host grants. Client
messages cannot change roots or grants. Existing `serve-mcp --target ...` callers
retain the separate read/append contract.

The server implements newline-delimited JSON-RPC over stdio with protocol
`2024-11-05`, `initialize`, `notifications/initialized`, `ping`, `tools/list`,
`tools/call`, `resources/list` and `resources/read`. One bounded UTF-8 document
(at most 1 MiB of input, 4 MiB of output) occupies each line. Duplicate keys and invalid JSON refuse;
stdout carries protocol and stderr carries diagnostics. Resources use closed
`evidence-wiki://resource/ID` URIs. `tools/list` supplies tool schemas;
`onboarding_contracts` supplies nested request/plan schemas. Successful tool JSON
text equals its Python owner result. Typed owner errors use `isError: true`;
invalid protocol messages use JSON-RPC errors.

EOF closes the handle. Installation or root replacement requires restart.
Execution is serial; notifications never invoke tools, and cancellation does not
undo a committed owner effect. Retain plans/IDs/receipts and inspect recovery
before retrying interrupted mutations. Tool declarations confer no host
authentication or stronger evidence assurance. Framework-native MCP is not
assumed.

## Pack identity, composition and fleet operations

Retrieve request shapes with `agent extensions`. Commands accept
`--from-file DOCUMENT`; plan commands optionally save a new `--output FILE`.

| Intent | Preview | Apply |
| --- | --- | --- |
| First pack or changed identity | `pack migration-plan` | `pack migration-apply` |
| Related domains in one pinned pack | `pack compose-plan` | `pack compose --output NEW_CONTAINER` |
| Selected workspaces and one candidate | `pack fleet-plan` | `pack fleet-apply` |

Migration requires one candidate path or catalog revision, rationale, explicit
configuration conflict resolutions, and policy/request-kind/template mappings.
Map every removed ID to a valid unique replacement or `null` retirement.
Ambiguous mappings refuse. Source IDs, evidence, requests, answers, host controls
and old pack directories remain retained. Mappings express migration intent;
they do not rewrite historical requests or certify answers. The lifecycle owner
provides journals, backups, locks, revalidation and interrupted-write recovery.
Reevaluate affected questions before fresh evidence/review and final export.
Same-name changes continue through `pack revision-plan`/`revision-apply`.

Composition accepts 2–8 members with path, unique alias and applicability, plus a
new name/version/scope. It scopes policy/request/template names, retains original
member bytes and guidance, and pins their identities in `composition.lock.json`.
Shared declarations must agree; page-type/directory sets may be combined.
Conflicting taxonomy, configuration, schema or computation declarations refuse.
Nested compositions are unsupported. Validation rebuilds compiled files from
pinned members; merely rehashing weakened output is insufficient. Review
applicability and select the resulting single identity. Any member change needs
recomposition and a whole-pack revision/migration.

Fleet planning reads 1–16 explicit nonoverlapping workspaces; it never scans for
others. Reports distinguish candidate availability, installed consistency, local
conflicts and research impact from semantic applicability. Apply requires an
explicit subset of proposed targets, with independent transactions and receipts.
Partial failure retains successes. Retry selected unchanged plans or replan
changed workspaces. Version labels never trigger propagation.

## Explicit host transitions

`agent transition-plan` prepares setup or a revision/reevaluation sequence and one
new parent session; `agent transition-apply` applies it. Name a separate existing
control root, transition ID, target, optional terminal previous session, next
agent/session IDs, optional saved setup/revision plans and explicit reevaluation
documents. New setup cannot be combined with historical revision inputs.

Live work, pending orders, modified deployed runtime and stale requirements
refuse. The private journal binds the plan and generation, and completed steps
are checked against owner artifacts. Session creation binds correlation/current
requirements under the controller lock. A retry reconciles interrupted creation
without making another session. The operation creates the parent only; driving
it, launching a model, writing computation results and dispatching schedules
remain explicit. Old approvals retain their original basis. The ordinary route
is `artifact_checked`; protected parent execution remains unsupported.
`already_complete` confirms historical correlation, not current research
readiness. Run the current export gate.

Correlated host sessions use schema `1.1`, published as
`contract().artifact_schema_documents.orchestration_host_session`. Ordinary
sessions retain schema `1.0`. Work orders and results retain their own versions.
The controller supports both session shapes and requires the correlation and
frozen requirement basis for `1.1`; unknown versions refuse.

`onboarding_contract.workflow_commands` lists the read-only `agent` bootstrap,
summary, resource index and content retrieval commands. Separate
`research_planning`, `workspace_application` and `pack_authoring` contracts expose
`agent plan`, `agent apply` and local pack creation with their own schemas and
mutation boundaries.
The separate `source_usability` contract provides `agent inspect`, source
readiness/routing and explicit host capture delivery. Read
[source-usability.md](source-usability.md); default inspection runs no plugins,
external tools or network requests, while explicit probes disclose their effects.
The `pack_revisions` contract exposes bounded revision planning/application,
current impact status and explicit coverage migration through the existing
owners. Discover it with `pack schemas`; read `pack guide --topic revisions`.
Revision records are local observations, never signed domain approvals.

The separate `pack_discovery` contract provides pack inspection, caller-local
catalogs and caller-declared fit decisions. Retrieve its schemas with
`pack schemas` and guidance with `pack guide`; see [pack-selection.md](pack-selection.md).
`evidence-wiki agent` works before initialization; retrieve its bounded summary
with `agent summary --format json` and exact resource content with
`agent resource ID --format json`. Bootstrap and resource responses use v2
envelopes; their v1 schemas remain available. Summary and index use v1 envelopes.
The setup protocol below is implemented by `agent apply` for local setup on POSIX and Windows.
See [application and recovery](workspace-application.md) for supported assurance,
observed results and conservative interruption boundaries. Direct `init` has no
setup journal. Supplemental result/checkpoint schemas use `agent setup-schemas`.

### Public artifacts

The original IDs below begin with `onboarding/` and end with `/v1`. New research
requests use `onboarding/research_request/v2`, with `schema_version: "2.0"` and
mandatory strict-policy selection. See [strict-evidence.md](strict-evidence.md)
for policy, review, host and controlled-release operations; request v1 remains
a compatible legacy artifact.

| Name | Payload and authority |
| --- | --- |
| `bootstrap` | Installation versions, guide content, schema IDs, supported operations, workspace observation and limitations. |
| `inspection` | Installation/target/workspace scope, independently qualified capabilities, observation bounds. A declaration is not a probe result. |
| `research_request` | Original questions and IDs, derived questions, scope, domain rationale, sources, declared host tools, authority reference, budgets and unresolved decisions. |
| `setup_plan` | Request digest, exact installation/starter/pack identities, interpreter, target preconditions, input digests, complete owned profile, question map, source routes and ordered local operations. |
| `setup_receipt` | Plan digest, transaction ID, state, observed checks, question map, readiness, checkpoint reference and recovery action. `research_complete` is always false. |
| `setup_checkpoint` | Plan and target binding, completed operations, owned file digests and one durable write intent. It is setup state, never source evidence. |
| `pack_spec` | Domain scope and exclusions, evidence requirements, human-review requirements, recommendations and optional exact revision basis. The pack validator owns the resulting pack. |
| `revision_impact` | Exact base/candidate identities, workspace basis, changes, affected questions, unresolved mappings and bounds. It cannot rewrite prior answers. |
| `resource` | Closed resource ID, version, media type, content digest and actual content; never a private extraction path. |
| `error` | Existing error-envelope shape: schema version, code, message, recoverability, remediation and bounded details. |

Success artifacts share `schema_version`, `kind`, `request_id` and `payload`;
use the version declared by the selected resource. `request_id` is caller correlation, not proof of ownership or an
idempotency key. Error envelopes retain the existing schema-1.0 shape and do not
need a request ID from malformed input. Emit one JSON document on stdout in a
JSON onboarding operation, including refusals; progress goes to stderr. This
rule does not redirect errors from existing commands, which retain their own
stdout/stderr and exit semantics. Input/schema refusals exit 2. A successful
inspection can include unknowns and limitations without being an error.

### Limits and refusal behavior

The published limits apply before any plan effects. Input artifacts and saved
plans are never silently truncated. Inspections and impact views can return
bounded observations, with exact `total`, `returned` and `truncated` values for
the primary collection and explicit limitations for any omitted related items.
A truncated impact cannot support an assertion that a change is harmless.

| Boundary | Maximum |
| --- | ---: |
| Encoded input or output document, UTF-8 bytes | 1,048,576 |
| Tree depth, root at depth 1 | 16 |
| JSON nodes including object keys | 65,536 |
| Properties per object / items per array | 128 / 4,096 |
| Characters per string (individual fields can be smaller) | 65,536 |
| Original / derived questions | 100 / 200 |
| Sources / declared host tools / source routes | 200 / 32 / 256 |
| Setup steps or checks / input or owned files | 64 / 4,096 |
| Resources / impact items | 64 / 1,000 |
| Research budget: questions / requests / downloads | 1,000 / 1,000 / 1,000 |
| Research budget: bytes / elapsed seconds | 1,073,741,824 / 86,400 |

These are protocol ceilings, not recommended research budgets. Existing
workspace/run limits can be lower and remain authoritative.

`evidence_wiki.onboarding_contract.decode_document(id, raw_bytes)` accepts one
UTF-8 JSON object. Unknown fields, duplicate keys, trailing documents, BOMs,
invalid Unicode, nonfinite numbers and incorrect types refuse. Booleans
are not integers.
Typed integer fields require integer JSON tokens: `1.0` and `1e0` refuse even
though a generic JSON Schema validator can treat them as mathematical integers.
The schema's `x-evidence-wiki-integer-fields-require-integer-tokens` annotation
records that wire restriction. Finite fractional values remain possible inside
the opaque initializer profile.

`ONBOARDING_INVALID` identifies malformed shapes;
`ONBOARDING_LIMIT` identifies a byte/tree/collection/string ceiling;
`ONBOARDING_VERSION_UNSUPPORTED` identifies an incompatible declared version;
`ONBOARDING_RESOURCE_UNKNOWN` identifies an unknown resource. All four exit 2,
are non-retryable without changing inputs, and map to `UsageError`. Failures
contain static remediation and schema-owned field paths, never input excerpts,
unknown field names, parser tracebacks or secret values. A string-aware nesting
check precedes JSON parsing so deep inputs have the same limit refusal across
supported interpreters.

Unknown fields are rejected at every contract-owned object. The embedded
`profile.workspace_init` is the one opaque owned boundary: the initializer's
`validate_profile` and `build_config` must validate it before a plan is accepted
for execution. This preserves its existing extensibility and provider rules.
Structural decoding alone does not claim a complete or applicable plan.

Any field/meaning change requiring a strict reader to accept different data
requires a new resource version. Never relabel old artifacts to bypass a
version refusal. The onboarding schema, installation package version, starter
version, library API version, init-profile schema and `research.yml` contract
version are independent. A workspace keeps its own recorded starter and
research contract; installed metadata must not be substituted for them.

`encode_document` returns canonical JSON: UTF-8, unescaped Unicode, sorted
object keys, compact separators, no BOM, newline or nonfinite numbers.
`document_sha256` hashes all these bytes. Array order and text are preserved.
A plan contains its request's digest; its receipt and checkpoint contain the
plan's digest, avoiding a self-referential hash. Digests detect changed bytes;
they are neither signatures nor authorization.

### Semantic owners

The workflow owner must establish the following beyond structural decoding:

- Request compiler: unique original/derived/source/tool IDs; every derived or
  routed question resolves to original IDs; no original is dropped; exactly one
  compatible domain mode; no recommendation implicitly grants access.
- Plan compiler: the complete profile passes the initializer's owning rules;
  target, pack, authority and original-question accounting agree with the
  request; every operation is allowlisted and dependency ordered. User-supplied
  command strings and `validation.results` never become executed commands or
  observed check results.
- Apply owner: current authority, roots, filesystem identities, original
  inputs, interpreter and all digests match under its lock. Recompute derived
  configuration through the same owners; a caller-edited plan requires a new
  plan. No unresolved material decision can be applied.
- Check owner: only a completed invocation can have an observed time, exit
  code and output digest. Pending/not-run checks have none. A failed required
  check prevents `setup_ready`; all required checks and original questions
  must be accounted for. Successful setup does not answer a question.
- Pack/impact owners: the pack validator accepts the candidate; lifecycle
  digests use the existing overlay normalization and full-tree algorithms;
  mappings, semantic classifications and completeness are established from
  actual inputs. Unknown impact requires reevaluation or explicit deferral.

### Secrets and resource lifetimes

Persist credential references only (`env:NAME`), never their values, cookies,
authorization headers, private keys, signed URLs, request bodies, full
environment dumps or raw connector output. Source locators must omit embedded
credentials. Treat prose as potentially sensitive: structural validation is
not secret detection, and callers must remove secrets before serialization.
Never print raw validation exceptions from an owning profile/provider rule.

Local plans can retain the explicitly selected canonical roots and interpreter
path needed for replay. Public summaries report versions and identity digests;
they omit private absolute paths. A managed worker must not report the value of
`EVIDENCE_WIKI_PYTHON`.

Schema documents are in-memory caller-owned values. A resource response carries
content and a digest. Existing `assets_root()` extraction lasts only inside its
context; `shared_assets_root()` lasts until process exit. Neither temporary
path may be serialized as a reusable resource. An executor needing filesystem
assets holds the context for the entire dependent operation and cleans up only
its private extraction on exit. After a crash, stale extractions are disposable
only when private ownership is established; they are never a resume basis.

## Roles and execution authority

| Role | Owns | Boundary |
| --- | --- | --- |
| Current trusted caller | Research judgment, authorized local configuration, pack candidates, source routing, direct question/run operations. | No managed isolation guarantee. Uses the existing scripts; another model is optional. |
| Managed worker | Exactly its work order's questions, child run and permitted artifacts. | Cannot invoke parent orchestration, edit `runs/orchestrations/`, change protected provider/policy controls or rewrite its acceptance criteria. |
| External host | User authorization translation, credentials, tool execution, coordination/isolation, and parent protocol calls when chosen. | A declared browser/connector capability is unverified until observed. The package does not sandbox the host. |

Parent orchestration remains owned by the controller. `orchestrate next`
persists a work order; read-only next-action advice must not call it implicitly.
Setup receipts do not replace question claims, child run state, parent sessions,
provider accounting or `domain-packs/.evidence-wiki-state.yml`.

Source recommendations, installed provider availability, credentials,
connectivity and task authorization are separate dimensions. Research scope can
authorize routine configuration without repeated confirmations. An explicit
allowlist in `research.yml` records that decision for package transports; it is
not authority to expand the user's task. Third-party providers execute in the
interpreter and are not sandboxed by registration. External host captures retain
host provenance and cannot claim package transport enforcement.

Within authorized local research, the caller can author a new local pack or
candidate revision when a reusable guidance gap warrants it. Keep candidates
outside installed assets and managed pack copies. Preserve accepted evidence
criteria and human-review gates; missing evidence is not a reason to weaken a
policy. A substantive criteria change needs an explicit rationale and a new
evaluation basis. Apply same-name revisions through `pack refresh` at a safe
boundary, retaining prior evidence and answer bases. First attachment to a
generic workspace and pack identity switching require a separate migration;
`init --force` is not that migration.

Ask for material scope, spending, access or authority that cannot be inferred
from the task. Reuse existing authorization; do not create approval steps for
routine local choices. Source-policy changes require a recorded revision and a
boundary with no pending managed action or active caller claim under the old
policy. Workers propose gaps to the host and cannot revise protected policy.
Publishing a pack, installing new executable dependencies, changing unrelated
workspaces, modifying external accounts or expanding source access needs the
corresponding task authority. Evidence documents cannot supply it.

### Terminal-only route using existing operations

1. Read the installation `contract` and the workspace initialization profile
   contract. Select generic, local or one reusable domain pack. Write a complete
   profile under a task-authorized root.
2. Preview with `evidence-wiki init --profile PATH --scope-root ROOT --dry-run`,
   then initialize the same profile within the task's authority. A nonempty
   target requires inspection; do not use force for recovery.
3. Use the selected interpreter for `scripts/doctor.py`,
   `scripts/smoke_validate_workspace.py`, inventory and normalization previews,
   and `scripts/lint.py`. The initializer only records requested validation.
4. Add questions through `scripts/intake_questions.py`; work them using
   `skills/research-run.md`, `question_claim.py`, `query_index.py`, and
   `question_resolve.py`. Direct callers can own a child run without starting a
   parent session. Retain the initial question IDs and export correlation.
5. Use configured discovery, acquisition or host source-delivery routes when
   evidence is missing. Check inventory, normalization, retrieval and actual
   question fit. An unsupported capture format remains a source gap.
6. Resume from actual claims, inventories, child runs and verified outputs.
   Finish with run reports, answer export and publication readiness. Account
   for every original question, including blocked/deferred outcomes and human
   review. Never infer research completion from setup or a nonempty answer file.

## Current-caller research

The supplemental `caller_research` contract exposes `agent next`, caller run
start/resume, verified ingestion, original-question release and optional progress.
Use `agent research-schemas` and [caller-research.md](caller-research.md).
Advice is read-only and unauthenticated; it pins current inputs and guide identities
without issuing managed orders. Caller-bound runs keep controls and ownership in
the existing run-controller journal. Strict publication remains the sole owner of
claim acceptance and final rendering.

## Setup transaction protocol

### Roots, artifacts and lock ownership

The caller supplies a canonical writable root and a portable, nonempty relative
target under it. The root and target's parent must already exist. Reject
traversal, drive/UNC paths, reserved path components, aliases and symlinks in the
write chain; reject installed asset trees, existing workspaces and their
descendants. Revalidate root/parent identity and target absence or emptiness
immediately before effects. Filesystem identity is device/inode where reliable;
an implementation without equivalent identity and no-follow guarantees must
refuse before writing.

Keep setup state outside the target, so recording intent never makes an empty
target nonempty. Under the writable root reserve:

```text
.evidence-wiki/setup/targets/<target-key>/
  setup.lock
  transactions/<transaction-id>/
    request.json
    plan.json
    checkpoint.json
    receipt.json
    observations/<check-id>.json
```

`target-key` is the lowercase SHA-256 hex digest of UTF-8 NFC-normalized,
case-folded canonical root, one NUL byte, and the NFC-normalized, case-folded
portable relative target. This deliberately serializes spelling aliases even
on case-sensitive filesystems. The actual target spelling and filesystem
identities remain in the plan and must match; sharing a lock key cannot make
different targets interchangeable. Roots, parents and owned target directories
have separately recorded identities.
`transaction-id` is a fresh random opaque ID. It is never taken from a source
document. Reserve files exclusively; reject existing unowned state, symlinks,
hardlinks or unsafe permissions. Directories are private (0700), files private
(0600) where supported. The caller owns the root; only the setup executor owns
this subtree. User pack assets/catalogs use a separate caller-selected root.

Hold one exclusive lock per canonical target across preflight, creation, local
effects and checkpoint/receipt writes. Reuse `_workspace_locks` for the locking
primitive; expose no PID-file deletion or stale-lock stealing shortcut. A busy
lock returns `ONBOARDING_LOCK_BUSY`, exit 6, with retry remediation.
Package/workspace lifecycle locks remain
owned by their existing operations; do not call parent orchestration or acquire
its locks from a worker. Serialize setup above per-question/intake/coverage
owners, since intake and coverage do not supply a common batch transaction.

Request and plan are immutable, exclusively created snapshots. Checkpoint,
receipt and sanitized observations use unique sibling temporary files, flush,
atomic replacement and directory durability where supported. Bound observations
to 65,536 bytes each and 64 files. Checkpoint and receipt are individually
bounded JSON documents. A receipt is a derived summary of a committed
checkpoint, never stronger evidence than that checkpoint.

### Input and interpreter binding

Bind the complete canonical request, package version and package implementation
identity, starter version and tree digest, schema/API versions, exact selected
pack origin and both lifecycle digests, profile, source inputs, authority scope,
target preconditions and interpreter before any effect. `setup_plan.inputs`
includes the package implementation files/distribution identity used by setup,
so editing code without changing its version also invalidates the plan. Record
hash algorithms and source ownership; do not treat a larger pack version as an
upgrade ordering rule.

Reuse the current command interpreter by default. An explicit interpreter is
accepted only as an operator-selected executable, never a shell string or
PATH-search fallback. Capture its implementation/version, executable digest and
a canonical identity of relevant installed distributions; use it for every
workspace script with `-B`. The target need not contain a virtual environment.
The environment digest hashes canonical JSON containing the interpreter's
implementation/version and a sorted list of normalized distribution names,
versions and implementation-file digests for EvidenceWiki, its required
dependencies, and selected providers/adapters. Exclude unrelated packages and
environment values. The plan's input list records which files supplied this
identity; missing or unidentifiable implementation bytes require reinspection.
If a managed host supplies `EVIDENCE_WIKI_PYTHON`, that exact interpreter remains
authoritative for the worker. A package-managed setup cannot silently create a
venv, install dependencies or switch interpreter after validation.

An incompatible/missing dependency returns `ONBOARDING_ENVIRONMENT_INCOMPATIBLE`
(exit 2), naming the missing distribution/required version without environment
values. Remediation is to select an existing compatible interpreter, or install
the named dependency into the selected environment when task authority permits,
then inspect and replan. No installation is performed by this refusal. Version,
input or interpreter drift returns `ONBOARDING_PLAN_STALE` (exit 3): regenerate
the plan from the preserved request; do not edit the digest or resume across it.
These executor codes are emitted by apply, not by the structural decoder. They are already accepted by the error schema and listed
with exit/retry semantics in `onboarding_contract.error_codes`, so an eventual
executor does not need to change the envelope. Error decoding rejects a
recoverability flag that conflicts with its code. Write/check failures and busy
locks are recoverable after the specified correction; version, ownership and
target conflicts require a new decision or plan.

### Crash points and recovery

The existing initializer writes several files without a setup journal. An
executor must wrap/adapt its file effects with ownership observations or stop
at the conservative recovery boundary below; merely rerunning initialization
cannot implement this protocol.

| Interruption or conflict | Recovery |
| --- | --- |
| Before durable request/plan/checkpoint | No target effect permitted. Revalidate inputs; discard only proven private temporary files. |
| After prepared checkpoint, before target creation | Recheck absence/emptiness and all identities under the target lock; start the same plan. |
| After target directory creation, before ownership checkpoint | An unrecorded directory is ambiguous. Refuse with `ONBOARDING_OWNERSHIP_CONFLICT` (exit 3); preserve it for inspection. |
| After durable write intent, before file replacement | If the destination equals the recorded before-image (or is still absent), perform that one write. Any other bytes are a conflict. |
| After replacement, before completed checkpoint | Only an exact intended after-image within the owned directory can complete that checkpoint. A digest match alone cannot establish directory ownership. |
| During unjournaled initializer or intake effects | Preserve partial creation; refuse automatic replay. Inspect/restore the specific artifacts, or choose a fresh target and plan. Never force initialization. |
| During inventory/normalization/coverage | Resume only with the owning operation's idempotency/postcondition evidence; otherwise preserve state and report the exact step for inspection. Never rewrite raw evidence. |
| During checks | A missing completed observation is pending. Rerun that bounded check; never copy requested success values into a receipt. |
| After ready checkpoint, before receipt write | Reconstruct the receipt from retained observations and current matching inputs. Do not rerun creation. |
| User edits or extra files during incomplete setup | Refuse before overwriting, adopting or deleting them. Preserve ownership journal and bytes; inspect the conflicting path. |
| Repeated apply after completion | Return the recorded result only after identity/input checks. A drifted or edited workspace is inspected as an existing workspace, never reinitialized. |
| Unsupported checkpoint version, changed plan or pack/interpreter drift | Preserve all artifacts; require a compatible reader or a new plan at a safe boundary. |

`ONBOARDING_TARGET_CONFLICT` (exit 3) covers a preexisting nonempty or different
target; choose an empty target or inspect the existing workspace.
`ONBOARDING_WRITE_FAILED` (exit 2) means restore permissions/free space, inspect
the last committed checkpoint, then retry only its documented recoverable
operation. `ONBOARDING_CHECK_FAILED` (exit 2) retains observed failing checks and
prevents readiness; correct their specific causes before repeating checks.
Never promise rollback of arbitrary workspace effects. A conflict needs a
material decision only when existing task authority cannot resolve it.

After setup completes, retained state supports audit, not control of future
research. Canonical workspace owners govern subsequent changes. Cleanup of
receipts, observations or abandoned targets is an explicit retention action;
successful setup never deletes them implicitly.
