# Evidence usage

EvidenceWiki can keep exact sanitized source revisions in private host state
and require separate retrieval, training, and export permissions. Permission
does not establish evidence quality, correctness, freshness, or a passing
evaluation. Every applicable ancestor must permit the requested use.

## Host boundary

The host owns upstream capture and sanitization. Before the package creates a
protected payload file, temporary file, or retained source delivery, it requires
an authenticated grant and a passing scrub receipt bound to the complete
sanitized revision. A `scrubbed` label or secret-pattern scan cannot supply
that authority. Protected payloads enter through `usage.transact`; ordinary
discovery, acquisition, inventory, normalization, and orchestration mutation
routes refuse protected intake before their writes.

Provision a private directory outside the workspace and a separate authority
file using the [execution authority contract](../workspace-template/docs/execution-evidence.md). Configure
the host environment and select its identities in `research.yml`:

```text
EVIDENCE_WIKI_STATE_DIR=/absolute/private/host-state
EVIDENCE_WIKI_AUTHORITY_FILE=/absolute/private/authority.json
```

```yaml
evidence_trust:
  policy_id: host-authority
  policy_revision: '1'
evidence_usage:
  state_id: research-evidence
```

The directory must belong to the current user with no group or other access.
State and lock files must be private regular files with one hard link. Paths
must have no symlink components. This implementation requires POSIX directory
descriptors, no-follow opens, and advisory file locking; unsupported systems
refuse. Read operations do not create a missing store. The host must provision
the directory; initialization creates only the state and lock inside it.

Workspace configuration selects authority; it cannot supply keys. Removing the
configuration while the state environment variable remains set does not enable
legacy behavior. The host must keep the external authority and state protected
from untrusted writers. Hash chains detect incoherent state, but cannot detect a
privileged host replacing the entire store with an older coherent backup.

## Signed commands and exact revisions

Commands use the whole-payload `evidence-authentication/v1` envelope. All actions
require the `usage` role except `revoke`, which requires `revocation`, and
`register-assessment` / `invalidate-assessments`, which require `assessment`. A command
payload has exactly these fields:

| Field | Meaning |
| --- | --- |
| `schema_version` | `evidence-usage-command/v1` |
| `state_id` | Selected host state identity |
| `workspace_binding` | Content identity of the resolved workspace root |
| `request_id` | Unique host request identity; reuse only for exact retries |
| `expected_checkpoint` | Current event identity, or null for initialization |
| `action` | `initialize`, `deposit`, `authorize`, `attest-availability`, `revoke`, `register`, `register-assessment`, or `invalidate-assessments` |
| `body` | Action-specific fields below |

The two assessment actions are prepared, independently qualified and applied
through the [assessment API](evidence-assessments.md). They share this ledger's
atomic writes, request identity, checkpoint and host authority. They retain
whole assessment envelopes and monotone invalidation history; derived usage
lineage also refuses an invalidated assessment ancestor.

Content identities hash the UTF-8 domain, a NUL byte, and canonical JSON with
one trailing newline, using SHA-256 and the prefix `sha256:`. Canonical JSON
sorts keys, uses compact separators and UTF-8, preserves Unicode, and refuses
non-finite numbers. The workspace binding uses domain
`evidence-host-workspace/v1` and value `{"root":"/resolved/root"}`. Hosts must use
the exact resolved path; moving the workspace changes this binding.

`initialize` has an empty body. `deposit` and `authorize` have `source_id`,
`source_revision`, `grant`, and `scrub`. Deposit supplies a complete map of
portable relative paths to bytes. Its revision uses the
`evidence-artifact-closure/v1` content identity over a mapping of paths to
`{"content_hash":"sha256:<hex>","size_bytes":123}` file bindings. Each file hash
covers its raw bytes, including every byte of `source-record.json`. Authorize changes the grant and
scrub receipt for existing exact bytes; it cannot change or revive a revoked
revision. Retrying an identical request returns its original receipt without
adding another event. Reusing its identity with different input refuses.

`source-record.json` contains `schema_version: evidence-source-revision/v1`,
`source_id`, `parents` (known source or derived node identities),
`normalized_path` (an included Markdown file or null), `evidence_root` (an
included artifact directory prefix or null), and `temporal` (a mapping of
temporal claims). Temporal claims alone do not establish historical availability.
An optional `attest-availability` command binds an independently authenticated
public-availability receipt to exact existing proof artifacts and one source
revision. Its outer command needs `usage` authority and its inner receipt needs
an independently controlled `availability` principal. See
[Temporal evidence](temporal-evidence.md) for both schemas and clock semantics.
Paths cannot traverse directories, collide by case, or refer outside the supplied
closure. The bounds are 256 files, 16 MiB per file, and 64 MiB per closure.
The state and CLI transport have their own 64 MiB encoded limits, so usable
payload capacity is smaller after base64 encoding and event history.

The grant is an authenticated `evidence-usage-grant/v1` payload with
`source_id`, `source_revision`, `permissions`, `purposes`, `consumers`,
`not_before`, `expires_at`, `redaction_policy`, and `retention`.
`permissions` contains three actual booleans: `retrieval`, `training`, and
`export`. Purpose and consumer lists are explicit, nonempty, and duplicate-free.
Time bounds require timezone offsets. `redaction_policy` has `id` and `revision`;
`retention` must be `host-managed`. The grant requires the `usage` role.

The independently authenticated scrub payload uses
`schema_version: evidence-scrub-receipt/v1`, `sanitized_revision`, the same
`redaction_policy`, `tool` with `name` and `version`, `outcome: passed`, and
`completed_at`. Its signer needs the `scrubber` role. Every byte in the closure,
including logs and provenance, is covered. The package verifies these claims;
the host remains responsible for the scrubber's actual behavior.

## Public operations

```python
from evidence_wiki import Workspace

with Workspace.open("/resolved/workspace") as workspace:
    status = workspace.usage.status()
    receipt = workspace.usage.transact(host_signed_command, artifacts=sanitized_files)
    decision = workspace.usage.check(
        source_revision,
        uses=["training", "export"],
        purpose="training-snapshot",
        consumer="evidence-wiki",
    )
    lineage = workspace.usage.lineage(source_revision)
```

The corresponding commands are `evidence-wiki usage status`, `transact`,
`check`, `lineage`, and `materialize`, each with `--target PATH`. Transact reads
one JSON object from stdin containing `command` and `artifacts`; artifact values
are canonical base64. Check uses `--revision`, repeated `--use`, `--purpose`,
and `--consumer`. All output is JSON. Exit 1 means an ineligible check; exit 2
is a refused operation. API refusals are `SourceError` with a stable reason and
no submitted content in the error envelope.

`usage.materialize(revision)` publishes only the approved normalized file into
the configured `sources` directory. It requires retrieval permission for purpose
`research` and consumer `evidence-wiki`. Existing identical bytes are an
idempotent success. Replacing different content requires its exact
`expected_content_hash`; symlinks, hardlinks, and changing destinations refuse.

Protected queries hold a coherent state read while checking and ranking exact
approved normalized bytes in memory. They exclude unapproved wiki content and
bypass persistent indexes, external retrieval providers, and enrichment.
Normalized evidence and coverage also check current authority and all ancestors.
Local permission labels may restrict use but cannot grant authority.
Protected retrieval checks matching records in the configured source manifest,
including nested metadata and provenance restrictions and exact revision
selections. An absent manifest remains compatible with host-only materialized
sources; an unreadable or ambiguous manifest refuses. A manifest change during
retrieval also refuses before returning results.

## Revocation, lineage, and reconciliation

Revoke bodies contain `source_id`, `scope`, `source_revision`, and a portable
`reason`. Scope `revision` binds one known revision. Scope `source` requires a
null revision and denies every existing and future revision of that source.
Revocation is permanent in this store. A corrected revision remains a distinct
identity; it does not erase the history of the earlier bytes.

Register bodies contain `node_id`, `kind`, and nonempty `parents`. Supported
kinds are `derived`, `snapshot`, `dataset`, `adapter`, and `model`. The host
attests these downstream references; registration does not inspect a dataset or
model file. Every parent must already exist, so cycles and missing ancestors
refuse. Lineage reports its bounds and `complete` explicitly. The store allows
10,000 events, 4,096 nodes, and 64 parent levels; reaching a bound refuses more
writes instead of silently discarding history.

One locked, atomic state replacement publishes events, grants, revocations, and
lineage together. An I/O interruption after rename can return a refusal even
though the new generation committed. Reconcile with
`usage.status(request_id=...)` and retry the exact signed command if necessary.
An interrupted normalized-file publication likewise needs an exact retry and
inspection of the resulting content hash. Never infer rollback from an error.

The host owns retention and deletion. Revocation stops authorization; it does
not erase retained payloads, snapshots, datasets, or learned weights. Immutable
history does not override deletion obligations. Automatic payload deletion is
not supported by this retention contract.

## Legacy compatibility

Existing Q&A export remains compatible for workspaces with no usage selection
and no source usage declarations. Unknown permission never authorizes training.
Once a source declares `usage_revision_id`, `usage_policy`, or any
`retrieval_eligible`, `training_eligible`, or `export_eligible` field, legacy
export and persistent cache routes refuse until authoritative handling is used.
Protected workspaces refuse legacy Q&A publication because its complete derived
artifact closure has not been approved for export. A usage check alone does not
make that closure safe to copy. Capability negotiation exposes this boundary
in `contract()["evidence_usage"]`.
