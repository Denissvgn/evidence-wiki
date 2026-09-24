# Discover and select guidance packs

Packs describe reusable research guidance. Selection does not acquire evidence,
enable providers, approve claims or establish that a domain policy is correct.
Human-review gates remain in force. Source content, URLs and recommendations in
a pack are inert data.

## Inspect explicit origins

```sh
evidence-wiki pack list --limit 10
evidence-wiki pack show bundled:general-science
evidence-wiki pack show --resource pack/bundled/general-science/v1
evidence-wiki pack show --path /path/to/my-pack
evidence-wiki pack list --target /path/to/workspace --catalog /path/to/catalog
evidence-wiki pack show installed:general-science --target /path/to/workspace
```

JSON is the default. `--format text` gives a compact display. Unqualified names
work only when exactly one inspected origin has that name. Prefer `bundled:NAME`,
`installed:NAME`, or `local:REVISION`; collisions refuse selection. Path and
resource options cannot be combined with another locator. Pack resource IDs are
retrieved through `pack show --resource`, separately from `agent resource`.

Summaries contain origin, compatibility, tree and overlay identities, review
gates and unknown selection fields. Details reuse the pack's descriptions,
source types, policy declarations and coverage templates. Missing optional
`domain_pack.selection` fields remain unknown. They do not mean a pack fits any
question. New declarations use `schema_version: "1.0"`, with optional arrays
`typical_questions`, `exclusions`, `required_scope_inputs` (objects with `id` and
`description`), and `review_requirements`.

Inspection checks bounded metadata and reports `structural_validation: not_run`.
Installed inspection also calls the workspace lifecycle owner against a private
captured copy. It exposes tracked revision, local modifications, absent packs,
untracked legacy installations and invalid state without refresh or adoption.
`current` is a comparison to tracked local state; `newer_revision` remains
`unknown`. It does not query an upstream pack registry. A catalog never replaces
workspace installation state.

## Register caller-local revisions

Choose an existing directory of pack assets and a new catalog directory. Neither
is inferred from the home directory, current workspace or global configuration.
Local assets must be outside the installed package's assets; copy a bundled pack
to a caller-owned location before modifying or registering it.

```sh
evidence-wiki pack catalog init --catalog /path/to/catalog --root authored=/path/to/packs
evidence-wiki pack catalog register --catalog /path/to/catalog \
  --id science-one --root-id authored --path general-science --scope "Declared study scope"
evidence-wiki pack catalog list --catalog /path/to/catalog
evidence-wiki pack show local:science-one --catalog /path/to/catalog
```

Registration invokes the canonical pack validator and its temporary workspace
checks. It records scope, exact revision digests and a structural observation
bound to the checker/runtime and starter bytes. An optional
`--derived-from bundled:general-science --derived-sha256 HASH` records declared
ancestry; it does not prove derivation. Revision IDs cannot be replaced. Register
changed bytes under a new ID. Existing observations report changed checkers,
mutated candidates, missing files or moved/replaced roots without silently
retargeting them. Receipt hashes identify caller-local files; these files are
unprotected observations and confer no trusted review or evidence acceptance.

Writes use anchored native file operations, process-safe coordination and atomic
catalog replacement on POSIX and Windows local drives. Missing native filesystem
capabilities explicitly refuse catalog writes. A failure can
leave an unreferenced immutable receipt; registration is complete only when the
revision appears in `catalog.json`. Inspection is read-only for caller assets;
workspace inspection and canonical validation use temporary directories.

## Declare domain fit

```sh
evidence-wiki pack schemas
evidence-wiki pack schemas --schema-id evidence-pack-decision/v1
evidence-wiki pack decide --from-file decision.json --catalog /path/to/catalog
```

The caller supplies requirements, a mapping for every requirement, rationale,
alternatives and unresolved scope. Selected revisions require an explicit origin
and the `tree_sha256` returned by inspection. `pack_basis` uses JSON Pointers into
the selected pack's `metadata` object, such as `/description` or
`/selection/review_requirements/0`. Each pointer must resolve to a known value.
Supported mappings for reused guidance require a basis; the caller remains
responsible for its meaning. The command revalidates selected bytes through the
canonical validator, even when a catalog observation matches.

For example, substitute the inspected hash before using this complete decision:

```json
{
  "schema_version": "evidence-pack-decision/v1",
  "request_id": "study-methods",
  "choice": "reuse",
  "rationale": "Compare study methods and retain uncertainty.",
  "requirements": [{"id": "methods", "text": "Compare study methods.", "kind": "evidence"}],
  "selections": [{"selector": "bundled:general-science", "tree_sha256": "REPLACE_WITH_INSPECTED_HASH"}],
  "scope_inputs": {"research_question": "Which methods apply?", "population": "Declared study population", "time_scope": "Declared study period"},
  "mapping": [{"requirement_id": "methods", "support": "supported", "pack_basis": [{"selector": "bundled:general-science", "pointer": "/description"}], "rationale": "The declared scientific scope fits this requirement."}],
  "alternatives": [{"choice": "project_local", "rationale": "Use narrower local guidance if this reusable scope is unsuitable."}],
  "gaps": [],
  "unresolved_scope": [],
  "local_guidance": null,
  "partitions": []
}
```

Choices are `generic`, `reuse`, `project_local`, `create`, `revise`, `defer` or
`partition`. Project-local guidance uses the initializer's existing contract;
it cannot be mixed with a selected pack. `create` and `revise` require an actual
`guidance` gap. A missing source or adapter remains an evidence-access problem.
Neither choice authors or installs a pack.

Multiple simultaneous selections return `unsupported` with explicit alternatives.
For mixed scope, `partition` proposes separate single-pack projects: every
requirement belongs to exactly one partition, each with its own scope inputs
and optional pinned selection. It does not compose packs or create workspaces.
Missing declared scope produces `needs_scope`; unknown/partial/gap mappings
remain qualified. Even `valid` means contract-valid caller judgment, with
`semantic_adequacy: not_evaluated` and `research_ready: false`.

## Bounds and refusals

Limits are 8 roots, 32 local revisions, 64 listed descriptors, 256 files and
512 filesystem entries per pack, 1 MiB per file/output and 8 MiB per pack tree.
Pack names are portable path components up to 128 characters. Files must be
inert UTF-8 content accepted by the canonical pack validator. Symlinks, executable
members, duplicate YAML/JSON keys, YAML aliases, nonfinite values and excessive
depth are refused. List truncation is explicit; omitted candidates are not
evidence of absence. Redacted refusals use the existing `ONBOARDING_*` codes.
Unsupported choices exit 2; invalid/unavailable inspection exits 1; stale pins
exit 3 and native lock contention exits 6 with retry permitted.
