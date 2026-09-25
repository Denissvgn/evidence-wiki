# Plan a research workspace

`evidence-wiki agent plan` compiles a read-only setup plan through the installed
initializer, question-intake, coverage, strict-policy and computation validators.
It does not create the workspace, acquire evidence, run models or grant authority.

```sh
evidence-wiki agent plan-schemas
evidence-wiki agent plan-schemas --schema-id evidence-research-setup/v1
evidence-wiki agent plan --from-file request.json --output setup-plan.json
evidence-wiki agent plan --from-file request.json --format text
evidence-wiki agent plan-check --from-file setup-plan.json
```

The output file is optional, must be new and must have an existing parent outside
the target and installed assets. Publication is exclusive and atomic through native POSIX or Windows handles.
Planning without `--output` makes no target writes. Apply an authorized saved plan
with `evidence-wiki agent apply --from-file setup-plan.json`; the executor rechecks
its inputs under a target lock. See [application and recovery](workspace-application.md).
A current plan alone does not confer execution authority.

## Request and decisions

The input accepts an `onboarding/research_request/v2` document or this wrapper:

```json
{
  "schema_version": "evidence-research-setup/v1",
  "request": {"schema_version": "2.0", "kind": "research_request", "request_id": "research", "payload": {}},
  "decisions": {}
}
```

The empty payload above is a shape illustration; populate all required fields
from `agent resource onboarding/research_request/v2`. `plan-schemas` supplies
the complete wrapper and saved-plan schemas. Unknown fields are refused. A v1
request requires an explicit `decisions.mode` of `legacy` or `strict`. A v2 strict
request cannot be downgraded. A legacy request makes no strict acceptance claim.

Original question text and IDs are retained, including Unicode, multiline text
and leading/trailing whitespace. Derived questions must name existing original
IDs. Duplicate IDs are invalid; duplicate normalized text is an explicit setup
blocker. All questions use a single post-initialization intake batch with original
text and mappings in metadata. Initialization has no question seeds. Owner byte
and capacity limits remain blockers, without truncation or silent deduplication.

Defaults are explicit: project name `research`, language `en`, the starter's
immutable raw roots, no network providers, adapters, computation or host
tools. The plan records caller fields separately from defaults. Equivalent
expanded input has the same plan identity; object-key order and JSON whitespace
do not change it. Original question order and text remain meaningful.

`decisions` can supply a project name, language, raw roots, strict policy, public
trust selection, reviewer/host references, computation declaration, framework
ID/version/mode and per-question/source requirements. A domain pack is selected
with the exact name, version, research contract and tree/overlay digests. Bundled
locators are the pack name or `bundled:NAME`; caller-local locators are explicit
directories. Project-local guidance uses the initializer's existing schema.

## Evidence criteria

Each `question_plans` entry identifies one question and supplies a selected pack
template and/or caller-defined facets. Facets use the coverage owner's vocabulary
and remain pending with no accepted sources or fabricated request IDs. Template
and extra facet IDs must be distinct. Each facet has a matching `criteria` entry:
source classes, required scope names, time, units, counterevidence, stopping and
inference requirements. The plan freezes these criteria with the policy and rubric
identities. Unsupported templates or absent review criteria remain blockers.
Initialization persists the original request and expanded decisions in
`docs/research-requirements.json`. Its content hash is a mandatory strict-policy
instruction input. Changing frozen criteria therefore changes the review basis.

Quantitative criteria also identify source fields (JSON pointers and units),
graph/aggregation references and required invariant IDs. The computation owner
validates the merged pack/project declaration, references, arithmetic and explicit
clock policy. Engine, timezone and definition identities are retained. Planned
values are never presented as observations; usable numeric evidence is still
required for each affected facet.

## Access, budgets and release

Discovery and acquisition provider selections are independent. No provider is
selected by default. Built-in selections require corresponding caller actions
and positive budgets. `web` requires explicit allowed domains. Source locators
must fit the caller's URI-prefix scope. Query-only discovery with unresolved
scope stays blocked. Registered/search/codebase adapters requiring additional
qualification remain disabled with a named blocker. Planning never imports an
unselected plugin, installs a dependency or launches an external tool.

Source hints with kind `local_file` are read only under explicit absolute local
read scopes in `authority.source_scope`. Missing files are bound as absent.
Other kinds use the existing source-request vocabulary. `source_requirements`
binds each source ID to output format, completeness and supported source scope.
Full host declarations use `evidence-host-tools/v1`; abbreviated declarations in
the original request remain inert. Neither form proves access or host protection.

Credential references use environment-variable names only; secret/control fields,
credential-bearing URLs and known credential values are refused. Do not include
secrets in free text. The plan retains source provenance references and hashes,
not source file contents. Run/download controls use their existing owners. Byte
controls are per-artifact where supported; aggregate bytes, wall-clock and token
limits require host enforcement. Zero budgets disable routes. Positive-only
configuration fields retain an inert minimum; this is disclosed in the plan.

Strict policy compilation validates the policy through its owner and keeps
coverage and grounding mandatory. Frozen instruction identities must match the
selected installed assets. Public trust/reviewer references are not approvals.
Independent semantic review, human review where required, and protected execution
for `host_enforced` remain explicit release requirements. Effective assurance
stays unknown until observed. Quote matching cannot establish semantic support.

`setup_ready` reports only setup completeness. `research_ready` and
`actions_executed` remain false. Research and release blockers are question/facet
specific and remain in a plan even when setup is ready. Framework qualification
is bound to the selected version and mode; local access is not probed and bridge
acknowledgments are not computed or research output.

## Local pack authoring

For justified reusable guidance, `decisions.pack_authoring` carries a fit decision
plus a specification or derivation. The plan returns an inert authoring action
only for explicit guidance gaps; specification requirement IDs must cover them.
`pack resume` feeds a qualified local catalog revision into this same compiler,
binding its assessment identity and retaining independent domain review as a
release blocker. See [local pack authoring](pack-authoring.md).

## Replay and limits

The saved identity binds the normalized request, expanded configuration, proposed
actions, installed starter and package bytes, interpreter, dependency metadata,
pack contents, target directory identities, declared credential presence and
selected local file observations. `plan-check` recompiles read-only and refuses
drift or tampering. An absent file becoming present is a change. Authority must
still be checked when executing; revalidation is not a reservation or lock.

Documents and output are bounded to 1 MiB. Local input files are at most 16 MiB
each and 32 MiB in aggregate. Installed trees have bounded file/byte counts.
Strict bundles support at most 100 questions; question-intake byte/rate/capacity
limits also apply. Limits cause explicit refusals or blockers, never truncation.
