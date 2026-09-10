# Evidence snapshots

A snapshot is a self-contained, canonical JSON bundle of exact authorized
source revisions and their referenced evidence. It supports external corpus
builders without running training, loading model weights, or executing recorded
tools. Existing answer export remains a separate operation.

## Prepare, authorize, and publish

Configure the private [host authority and usage store](evidence-usage.md) first.
Every included source and ancestor needs independently authenticated training
and export permissions for the selection's purpose and consumer, plus a passing
scrub receipt for its exact sanitized bytes. Local labels may restrict use;
they cannot grant it.

```python
from evidence_wiki import Workspace, verify_snapshot

selection = {
    "schema_version": "evidence-snapshot-selection/v1",
    "source_revisions": [execution_source_revision],
    "include_negative_examples": False,
    "purpose": "training-snapshot",
    "consumer": "evidence-wiki",
}
with Workspace.open("/resolved/workspace") as workspace:
    preparation = workspace.snapshots.prepare(selection)
    # The host reviews preparation["manifest"], adds a unique request_id to
    # preparation["registration"], and signs that entire payload with role usage.
    receipt = workspace.usage.transact(host_signed_registration)
    exported = workspace.snapshots.export(
        selection,
        registration_request_id=host_signed_registration["payload"]["request_id"],
    )
    bundle = (workspace.root / exported["path"]).read_bytes()
    current = workspace.snapshots.check(bundle)

historical = verify_snapshot(bundle, trust_policy_bytes=independent_trust_bytes)
```

Preparation is read-only. Its unsigned registration binds the manifest identity,
selected parents, workspace identity, and exact host checkpoint. It is not an
authorization. A host-state change before registration causes a stale-checkpoint
refusal; prepare again and obtain a new host decision. The package supplies no
signing key or signing operation. Keep the external authority file private and
never distribute its secret material with a bundle.

Publication reconstructs the prepared state from the accepted registration and
rechecks current authority and every ancestor. It holds the host's exclusive
lock while publishing through a private temporary file and atomic rename.
Source, policy, authorization, or expiry changes cannot produce an accepted
mixture. Unsupported host storage, conflicting bytes, unsafe links, and expired
or revoked authority refuse. Output is
`exports/evidence-snapshots/<manifest-digest-hex>.json` beneath the workspace.
A refusal after rename can leave the complete output committed. Repeating the
same registered request reconciles identical existing bytes and returns
`created: false`; it never overwrites a conflicting file.

## Included evidence and exclusions

Selected revisions must contain the `execution_evidence/v1` profile. A positive
example requires an authenticated passing observation and a complete passing
receipt from a separately controlled evaluator, bound to the same record,
original artifacts, evaluation scope, and environment. An explicit negative
selection can include an authenticated failed observation with a correspondingly
failed receipt. Skipped, inconclusive, missing, mismatched, or unauthenticated
verification cannot supply either label. Permission checks apply equally to
positive and negative examples.

The bundle preserves failed observations and hypotheses in each included
record's history, all original referenced inputs, outputs, logs, environments,
receipts, source descriptors, normalized bytes, grants, scrub receipts, and
complete ancestry. Each execution input must identify an included ancestor's
exact source revision and an exact artifact in that revision. Unknown references
refuse. Included context packets retain their original native bytes and pinned
validator qualifications, including stale or incomplete status. Packet integrity
does not establish worker identity or live reconciliation.

The manifest records selection and exclusions, stable reason codes and counts,
explicit closure coverage and bounds, source observations, captured host
checkpoint, permitted provenance, public trust policy and its original byte
hash, exporter contract and implementation digest, and durable episode, task,
run, problem, and input-group identities. Declared training, validation, held-out,
or unspecified roles are preserved. A downstream builder must keep related
attempts together and enforce split separation; labels alone do not do so.
Excluded payloads are absent, so their exclusion reasons are host-attested
selection decisions rather than independently recomputed evidence claims.

This contract exports current-state execution evidence. Historical-cutoff
execution is refused. Temporal source metadata alone does not establish past
availability. Retention is host-managed; the package does not delete old
exports, train models, construct dataset formats, or guarantee model-weight
reproducibility.

## Canonical bytes and bounds

`evidence-snapshot/v1` contains exactly `schema_version`, `snapshot_id`,
`manifest`, `registration`, and `blobs`. The manifest uses
`evidence-snapshot-manifest/v1` and `evidence-snapshot-contract/v1`. Serialization
is UTF-8 JSON with sorted keys, compact separators, preserved Unicode, no
non-finite numbers, and one trailing newline. Duplicate keys and noncanonical
bundle encodings refuse. Blobs are canonical base64 keyed by the SHA-256 of
original bytes; per-source file entries bind both hash and length.

The snapshot identity is SHA-256 over the UTF-8 manifest schema name, a NUL byte,
and canonical manifest bytes, prefixed with `sha256:`. The manifest binds every
included blob through the complete source file maps. It excludes the containing
snapshot ID and registration signature, which instead authenticates that ID.
Discovery order and incidental wall-clock preparation time do not affect the
identity. Identical captured bytes, permissions, authority, implementation,
selection, host checkpoint, and accepted registration produce identical bundle
bytes. A Git revision alone does not identify these inputs. Old bytes stay fixed
when source revisions or host state advance.

Limits are 32 selected revisions, 64 source revisions, 128 lineage nodes, 512
unique artifact blobs, 8 MiB of decoded unique blobs, and 16 MiB of bundle bytes.
Each source has at most 256 files. Paths are portable relative names without
traversal, case collisions, reserved device names, or file/directory collisions.
The verifier never extracts an archive or follows a reference into the originating
workspace. JSON nesting and element counts are also bounded.

## Historical validity and current use

`verify_snapshot(data, trust_policy_bytes=...)` works without the originating
workspace or host store. It independently checks the exact canonical manifest,
blob closure, source revisions, lineage, execution and receipt signatures and
bindings, declared qualifications, grants, scrub evidence, and host registration.
The explicit trust bytes must match the captured trust hash and contain the
independently provisioned keys; bundled public policy cannot authorize itself.
The report's `current_use` is always `not_evaluated`. Archived authority can
establish historical bindings even after a later host revocation.

Before each training or export use, the host must call `snapshots.check` against
current authority and state. A currently revoked source or ancestor returns
`eligible: false` and `current_use: denied`, even when historical verification
passes. Invalid, unregistered, expired, or changed-trust bundles raise
`SourceError` with `EVIDENCE_SNAPSHOT_REFUSED`. This synchronous decision does
not grant an indefinite lease; hosts must reconcile again at later consumption
boundaries and handle downstream dataset, adapter, or model lineage through the
usage store. Retained audit use requires its own permitted purpose.

The CLI mirrors these operations:

```text
evidence-wiki snapshot prepare --target PATH < selection.json
evidence-wiki snapshot export --target PATH --registration-request-id ID < selection.json
evidence-wiki snapshot check --target PATH < snapshot.json
evidence-wiki snapshot verify --trust-policy /private/authority.json < snapshot.json
```

Verify accepts no workspace option. Its explicitly selected trust file must be
private, user-owned, regular, singly linked, at most 1 MiB, and reachable without
symlinks. API verification accepts explicit bytes on any supported Python host.
All CLI output is JSON. Exit 0 is success, exit 1 is a negative verification or
current-use verdict, and exit 2 is a refused operation.
