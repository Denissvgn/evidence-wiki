# Author and qualify a local domain pack

Authoring writes a new caller-owned draft container. Its distributable guidance
is in `packs/NAME`; specifications, ancestry, frozen cases and observations stay
in `records/`. Installed assets and existing workspace pack copies are protected.
No command publishes a pack, enables a provider, installs a model or applies a
workspace setup plan.

```sh
evidence-wiki pack schemas --schema-id evidence-pack-authoring-spec/v1
evidence-wiki pack guide --topic specification --format text > specification.json
evidence-wiki pack scaffold --from-file specification.json --output ASSET_ROOT/draft
evidence-wiki pack qualify --draft ASSET_ROOT/draft
evidence-wiki pack freeze-cases --draft ASSET_ROOT/draft --from-file cases.json
evidence-wiki pack assess --draft ASSET_ROOT/draft
evidence-wiki pack catalog init --catalog ASSET_ROOT/catalog --root drafts=ASSET_ROOT
evidence-wiki pack accept --draft ASSET_ROOT/draft --assessment-id SHA256 \
  --catalog ASSET_ROOT/catalog --root-id drafts --id revision-one --scope 'Reusable scope'
evidence-wiki pack resume --from-file research-request.json \
  --catalog ASSET_ROOT/catalog --id revision-one --output setup-plan.json
```

`pack guide --topic selection` retains the discovery/selection guide.
`pack guide --topic authoring` returns this guide. `pack schemas` includes the
separate authoring schemas without expanding the closed agent resource catalog.
`pack guide --topic specification` provides an editable structural example with
an explicit unresolved domain decision. `pack guide --topic references` returns
the frozen semantic and arithmetic data. Resolve the example's actual scope and
review needs before qualifying it for your task.

## Specifications and scaffolding

Supply scope and exclusions, intended users, question/source classes, required
scope inputs, review requirements, taxonomy, claim types/fields, extraction and
filing rules, output forms, policies and optional scaffolds/coverage/computation.
All content comes from the supplied specification. Unknown guidance belongs in
`unresolved`; it is not replaced with invented substantive rules.

Taxonomy entries name a directory, page type and description. Claim fields name
their type, requirement and interpretation. Scaffolds are Markdown bodies keyed
by stable slugs. Coverage templates and namespaced policies use the existing
coverage, request-kind and policy-primitive owners. Templates have no accepted
sources, fulfilled requests or answers. Computational declarations use the
canonical pure grammar and the selected pack/project merge.

Scaffolding produces `README.md`, `taxonomy.md`, `claims.md`, an overlay and the
declared optional guidance files. It preserves starter mechanics and recommends
providers without enabling them. Canonical temporary initialization/smoke preflight
runs before publishing the draft; `qualify` records the durable structural receipt.
All files are inert UTF-8 data. New native draft
publication uses POSIX descriptors and exclusive file creation; existing output
containers are refused. A failed write can leave a partial container, which is
preserved for inspection. A missing `records/draft.json` is not a completed draft.
Choose a new output rather than overwriting unexplained content.

## Revisions and specializations

For an existing workspace, continue an accepted same-pack revision with
`pack revision-plan`, `pack revision-apply` and `pack revision-status`. The
[revision workflow](pack-revisions.md) preserves merge conflicts, maps affected
research and requires explicit coverage migration before a new answer/review cycle.

`pack derive --from-file derivation.json --output NEW_CONTAINER` copies exactly
the selected base revision. Use `evidence-pack-derivation/v1`: select one explicit
base locator and tree digest, a mode, name, version, rationale, bounded file
changes, requirements and unresolved items. A revision keeps its name and changes
the display version. A specialization changes its name and consistently renames
the base policy/request namespace. Retained old namespaces are refused. The
external record lists rewritten files for review; legacy portable names may be
retained for same-name revisions, while new specializations use lowercase slugs.

The base remains untouched. Same-contract refresh and first-attachment migration
remain separate workspace operations. Candidate edits invalidate observations by
content identity. Revisions and post-scaffold edits retain a requirements-change
classification and independent review requirement; author labels cannot turn
weakened evidence criteria into routine repairs.

## Structural qualification

`pack qualify` runs the canonical validator on captured candidate bytes, including
temporary initialization, merged configuration/computation validation and smoke
checks. It rechecks candidate/checker identity and writes an immutable v2
structural observation. Failed canonical check IDs and statuses remain visible.
Use `pack validate --path CANDIDATE` for detailed local diagnostics.

Structural success does not establish source suitability, formula meaning,
research completeness or domain correctness. Guidance cannot configure arbitrary
commands, model runners, host trust or source acquisition. Executable engine code
and evaluation records stay outside the distributable pack.

## Frozen cases and separate domain assessment

Each draft has an immutable specification and ID. The case document names that
ID, requirement IDs, case IDs, supplied inputs, expected outcomes, rationale and
limitations. Cover adequate, missing, conflicting and wrong-scope evidence for
each requirement, or record explicit scenario exceptions with rationale. The
first `freeze-cases` publication is immutable: changing expected outcomes after a
failure requires a new declared draft/case history, not replacement of a pass.

Policy cases run existing pure primitives against supplied structured data,
provenance, question metadata and an explicit clock. Computation cases use
temporary synthetic retained records and the canonical renderer and engine.
Case inputs refuse fractional JSON number tokens: use lossless decimal strings
or the computation engine's `{"decimal":"1.25"}` representation. Adequate
mechanical cases require a positive expectation; unavailable capabilities remain
gaps even if the caller expected an unavailable outcome.
Their results do not advertise a new generic JSON extraction adapter. Cases never
dispatch schedules, write research outputs or accept external evidence.

Semantic cases record judgments separately. Optional `--observations FILE` uses
`evidence-pack-observations/v1`, binds the frozen suite and names a reviewer
reference, rationale and all six strict rubric verdicts. These remain declared,
unauthenticated observations. Their agreement with an expected label cannot
certify domain correctness or remove human gates.

The installed reference corpus retains existing synthetic semantic judgments,
counterevidence cases and independently specified arithmetic expectations across
three example domains. Every assessment binds its bytes, the strict rubric and
current engine, and replays the arithmetic cases. Formula labels, thresholds,
percentiles, schedules and synthetic filing rules still need independent domain
review. Missing cases, failed expectations and unresolved guidance remain gaps.

## Local registration and resuming research setup

`pack accept` revalidates the exact candidate and frozen assessment before the
existing catalog commits a new revision. It copies assessment records outside
the pack and records their limitations. Duplicate revision IDs, changed trees,
missing/moved roots, invalid observations and changed checkers are explicit.
Registration is local and never adopts, installs or publishes the pack.

`pack resume` selects a currently qualified catalog revision and calls the existing
setup compiler. The new plan binds both pack identity and the assessment record;
independent domain review remains a release blocker. It writes only an explicitly
requested new plan file. `agent apply` remains a separate capability.

The research request may contain a `decisions.pack_authoring` object with a pack
fit `decision` and optional `specification` or `derivation`. Only explicit create
or revise choices with a guidance gap yield an authoring action. Source failures
alone never request a new pack. Generic, project-local and deferred guidance
continue to use their existing routes.
