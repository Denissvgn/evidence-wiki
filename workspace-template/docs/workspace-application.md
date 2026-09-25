# Apply and recover a research workspace

Apply an authorized saved plan using the Python environment that compiled it:

```sh
evidence-wiki agent setup-schemas
evidence-wiki agent setup-guide --format text
evidence-wiki agent apply --from-file setup-plan.json
```

Use the same `apply` command to resume. No `--force`, shell command field,
installation, network acquisition or additional model is involved. An apply
process requires native no-follow file operations and native coordination.
POSIX uses directory descriptors and advisory locks; Windows uses anchored
filesystem handles, private ACLs and process-owned mutexes on local drives.
An explicitly selected `EVIDENCE_WIKI_PYTHON` must match the current command's
interpreter. Switching environments requires a new plan.

## Preconditions and effects

Apply recompiles the request, compares the full plan, and rechecks the installed
package, starter, dependency bytes, interpreter, selected pack, local inputs,
target root and parent identities. The declared writable authority must cover
both the target and the writable root containing setup state. It refuses an
unexpected nonempty target, symlinks, unexplained existing state, unresolved
setup decisions and unavailable required dependencies. Source documents and pack
guidance cannot replace its fixed operations.

The initializer writes the frozen profile and selected strict policy first.
Doctor and smoke precede intake, coverage, local delivery, inventory, normalization
and lint. Only selected built-in source routes are eligible; third-party provider
loading is left for explicit qualification. No network provider is invoked.

Question intake retains original IDs and full original text in metadata. Coverage
uses the selected owner-validated templates and facets, with pending evidence.
Only selected local files are copied, with provenance and original byte hashes.
The originals remain outside the workspace. Inventory and normalization cover
those copies. Missing, unsupported, partial and unusable evidence stays explicit.
A successful copy alone does not establish extraction, retrieval or source fit.

Selected computation runs through the read-only computation owner with an explicit
clock. A plan's declared clock is retained; otherwise each observation records
the execution clock. Invariants and missing inputs remain separate findings.
Setup never invokes warning intake, cadence dispatch or computation write actions.

## Receipts and readiness

The JSON result and retained `receipt.json` are the observed initialization
summary. The initializer's own decision document continues to describe requested
validation. Each bounded check records the actual process exit, outcome, clock,
workspace basis, checker and interpreter identities through the evidence authority
owner's local observation format. Required failures prevent setup readiness;
optional findings and checks that did not run are named separately.

| Status | Meaning |
| --- | --- |
| `ready` | Required setup checks passed; evidence may still be empty. |
| `partially_usable` | Setup passed and some selected evidence is usable; other routes or calculations have gaps. |
| `needs_input` | Setup passed but selected evidence or computation needs input or remediation. |
| `failed` | Creation or a required setup check did not complete successfully. |

`setup_ready` is independent of evidence completeness and `research_complete` is
always false. Receipts retain question mappings, selected profile and pack,
per-source usability, question-scoped route gaps, computation findings, framework
qualification and exact next-action arguments. Framework access remains
`not_probed`; an RPC acknowledgment cannot establish research output.

Strict setup establishes `artifact_checked` configuration and recomputable local
checks. It does not authenticate the answering caller or a reviewer, confirm
claims, or isolate future research tools. Missing independent review and release
requirements remain blockers. `host_enforced` setup is refused because this
executor cannot provision a protected worker boundary. A host reference or policy
label cannot substitute for separately protected provisioning and actual host
qualification.

Receipts and checkpoints are private local files, editable by the caller. They
are not authenticated review or claim-verification receipts. Reapplying checks
current artifacts and reruns checks; a caller-written success value is never
sufficient. A corrupted derived receipt can be regenerated from the retained
checkpoint and fresh observations. Corrupt ownership state requires inspection.

## Recovery boundaries

Setup state lives outside the target under the authorized writable root:

```text
.evidence-wiki/setup/targets/<target-key>/
  setup.lock
  transactions/<transaction-id>/
    request.json
    plan.json
    checkpoint.json
    receipt.json
    observations/<operation>.json
```

The target key follows the normalization in [agent-contracts.md](agent-contracts.md).
State directories and files are private. One permanent native lock covers setup;
a crashed process releases its kernel lock without deleting a PID file or stealing
a lock. Busy setup exits 6. Request and plan snapshots are immutable; checkpoint,
receipt and observations use durable atomic replacement.

Before each operation, the checkpoint records its pending state. Completed
mutations can be skipped only when all files, directory identities, permissions
and owning postconditions still match. An interruption before any effect can
resume. Interrupted read-only checks rerun. After a completed checkpoint, a fresh
agent can proceed without the original conversation.

The initializer, intake, coverage and normalization owners can perform multiple
writes without a common per-file journal. If they stop after changing the target
but before committing a checkpoint, apply preserves that partial state and returns
`ONBOARDING_OWNERSHIP_CONFLICT` (exit 3). Inspect the named pending step; choose a
fresh target and plan when the exact owned state cannot be restored. Never force
initialization or delete an unexplained path to make recovery pass.

User edits, additional files, directory replacement, changed source inputs and
package/pack/interpreter drift also refuse automatic replay. State cleanup is a
separate explicit retention action. Once research changes the workspace, use the
canonical research/status owners; setup is no longer its mutation authority.
