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

## Acquisition responsibility

Library API `14` adds the optional `decisions.orchestration` selector to
`evidence-research-setup/v1`. Discover its schema with `agent plan-schemas`; the
installation contract also describes it under `research_planning.orchestration_selection`.
CLI planning, `Onboarding.plan` and `onboarding_plan` use the same contract.
Existing requests remain accepted. Older installations have a closed decision
schema and reject this new field, including newly normalized requests carrying
`orchestration: null`. Restart embedding hosts and recompile plans after upgrading.

Omission or `"orchestration": null` emits no override and retains starter/pack
inheritance. Without an inherited declaration, acquisition uses provider mode.
An explicit provider selection is `"orchestration": {"acquisition": "providers"}`.
Delegation uses this decision:

```json
{
  "orchestration": {
    "acquisition": "delegated",
    "acquirer_agent_id": "external-acquirer",
    "max_attempts_per_request": 3
  },
  "acquisition": []
}
```

Every non-null selector requires `acquisition`. Delegation requires a nonempty
agent ID of at most 160 characters after trimming surrounding whitespace; ASCII
control characters inside the trimmed ID are refused. Mode and ID input strings
are bounded to 4,096 characters before trimming. Attempts default to `2` and must
be an integer from `1` through `10`. Provider mode refuses delegated-only fields.
Unknown fields, including `x-` metadata, are refused by this typed selector.

An explicit nonempty `decisions.acquisition` conflicts with delegation, even if
provider qualification, budgets or authority would disable those providers.
Discovery remains independent. The initializer validates the merged starter,
pack and profile; switching an inherited delegated declaration to providers
does not silently delete its incompatible delegated-only fields.

The choice is frozen in the normalized request, native profile, effective
configuration and `docs/research-requirements.json`, including its strict
instruction hash. Naming an acquirer grants no connector, credentials, source
access or execution authority. Host capability and source routes require their
own declarations and checks; research and release gaps remain visible.

### Complete delegated setup example

Retrieve this guide with `evidence-wiki agent plan-guide --format text`. Save the
complete request below as `request.json` outside the target. It retains original
question text, evidence criteria, policy selection and delegation. It declares
local setup only and leaves evidence and host access unverified.

```json
{
  "decisions": {
    "acquisition": [],
    "orchestration": {
      "acquirer_agent_id": "external-acquirer",
      "acquisition": "delegated",
      "max_attempts_per_request": 3
    },
    "question_plans": [
      {
        "criteria": [
          {
            "counterevidence": "Retain contrary evidence",
            "facet_id": "primary",
            "inference": "Label all derivations",
            "quantitative": null,
            "required_scope": [
              "jurisdiction"
            ],
            "source_classes": [
              "official guidance"
            ],
            "stopping": "Support or explicit gaps for all facets",
            "time": "Keep observation dates",
            "units": "Keep quoted currencies and units"
          }
        ],
        "facets": [
          {
            "description": "Retained primary evidence",
            "evidence_path": "official_guidance",
            "facet_id": "primary",
            "freshness_policy": "no_staleness_check",
            "identity_policy": "official_domain_match",
            "min_sources": 1,
            "required": true,
            "source_policy": "official_primary"
          }
        ],
        "question_id": "needs-evidence",
        "template": null
      }
    ],
    "raw_roots": [
      "raw/data"
    ]
  },
  "request": {
    "kind": "research_request",
    "payload": {
      "assumptions": [],
      "authority": {
        "allowed_actions": [
          "local_setup"
        ],
        "credential_references": [],
        "reference": "local setup only",
        "role": "caller",
        "source_scope": [],
        "writable_roots": [
          "/tmp/evidence-research"
        ]
      },
      "budgets": {
        "bytes": 1048576,
        "downloads": 5,
        "questions": 5,
        "seconds": 300,
        "source_requests": 5
      },
      "derived_questions": [],
      "domain": {
        "mode": "none",
        "pack": null,
        "rationale": "Generic guidance is sufficient for this question."
      },
      "goal": "Answer the original question using retained evidence and explicit gaps.",
      "host_tools": [],
      "open_decisions": [],
      "outputs": [
        "markdown",
        "json"
      ],
      "questions": [
        {
          "id": "needs-evidence",
          "text": "What does the supplier quote?\nRetain stated conditions and units."
        }
      ],
      "scope": [
        {
          "name": "jurisdiction",
          "value": "Spain"
        }
      ],
      "sources": [],
      "strict_evidence": {
        "assurance": "artifact_checked",
        "mode": "strict",
        "policy_id": "reviewed-evidence",
        "policy_revision": "1"
      },
      "target": {
        "relative_path": "workspace",
        "writable_root": "/tmp/evidence-research"
      }
    },
    "request_id": "delegated-research",
    "schema_version": "2.0"
  },
  "schema_version": "evidence-research-setup/v1"
}
```

Create the fresh `/tmp/evidence-research` parent before planning. From the
directory containing `request.json`, run:

```sh
mkdir /tmp/evidence-research
evidence-wiki agent plan --from-file request.json --output setup-plan.json
evidence-wiki agent plan-check --from-file setup-plan.json
evidence-wiki agent apply --from-file setup-plan.json
evidence-wiki agent apply --from-file setup-plan.json
```

For another location, including Windows, choose an existing absolute writable
parent and set both `request.payload.target.writable_root` and
`request.payload.authority.writable_roots` to it before planning. Keep the target
new or empty and save the request and plan outside it. The second apply verifies
and reuses the same owned transaction. Do this before research changes its files.

After setup, drive external orchestration with `orchestrate start`, `next` and
`submit`. Retrieve the protocol with
`evidence-wiki agent resource guide/orchestrator/v1 --format text`.
When a question is blocked on a source request, the controller can address an
acquisition order to `external-acquirer`. The host must authorize delivery and
fulfil only requests scoped by that pending order. Managed `orchestrate run` and
`resume` refuse delegated workspaces. Do not rewrite `research.yml` after apply
to enable delegation; it is already part of the accepted setup.

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

Package, starter or interpreter changes can make an old plan stale even when its
request is unchanged. Compile a new plan for a fresh target under the current
installation. An old partial or populated target cannot be adopted by that new
plan. Preserve its files and setup state for inspection; use the documented
[recovery boundaries](workspace-application.md#recovery-boundaries) for an
interrupted transaction. For a workspace initialized without its intended
delegation, recreate it from the intended request when safe, or arrange an
explicit owner-reviewed migration that accounts for existing research and
strict bindings. Neither upgrade nor apply silently repairs its configuration.

Documents and output are bounded to 1 MiB. Local input files are at most 16 MiB
each and 32 MiB in aggregate. Installed trees have bounded file/byte counts.
Strict bundles support at most 100 questions; question-intake byte/rate/capacity
limits also apply. Limits cause explicit refusals or blockers, never truncation.
