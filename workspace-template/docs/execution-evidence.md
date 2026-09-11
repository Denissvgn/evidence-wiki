# Execution evidence

Use source kind `execution_evidence` or declare
`metadata.execution_profile: execution_evidence/v1` to ingest inert execution
records. Place the original closure in `sources/evidence/<safe-source-id>/`,
with `execution-record.json` as its manifest. Source IDs use the same filename
encoding as normalized records: `execution:lab` becomes `execution--lab`.

The manifest declares schema `execution-evidence/v1`, the profile, source ID,
selected observation and receipt IDs, artifact members, generation envelopes,
and evaluator envelopes. Set `selected_receipt_id` to `null` when no receipt
exists. Every original file must have an exact size, SHA-256 digest, and role;
every member must be referenced. Capture is bounded to 256 files, 512 directory
entries, 64 MiB total, and 16 MiB per file. Links, special files, unsafe paths,
ambiguous JSON, unsupported schemas, and incomplete closures are refused.
No named runner, evaluator, comparison, or packet producer is executed.

Each generation payload binds episode, task, run, problem, input group, held-out
role, generating agent, input revisions, patch, declared input workspace digest,
environment, dependencies, container, tool revision, model configuration,
parameters, seed, timestamps, outputs, logs, units, warnings, limitations, and
verification scope. Explicit `unknown` and `not-applicable` values preserve
missing reproducibility information. The declared input workspace digest covers
only the listed input artifacts. It does not attest to an entire repository.

Historical generations declare `historical-audit` or `historical-available`
and a cutoff no later than their start time. A later computation or evaluator
receipt may analyze earlier inputs. A positive verification requires the exact
input revisions and their complete ancestry in accepted host storage, with
current retrieval permissions. Each ancestor must qualify at that generation's
cutoff, including completed measurements and market intervals where applicable.
Public-availability mode requires independently authenticated proof bound to
each input revision. Missing accepted context or ambiguous revisions refuse.
Explicit model prompt and context artifacts must also be declared inputs.
Nested execution inputs require their own authenticated history to exist by
the consuming generation's cutoff. Input clocks and result clocks are evaluated separately.

Historical input qualification does not establish a model's training cutoff or
promise regeneration of external model output. Keep unknown model information
explicit and use prospective observations when training-data leakage cannot be
excluded. Laboratory computations and market simulations share this contract.

Observations, hypotheses, and evaluator receipts have different content IDs.
History links must point to earlier records in the same episode and task.
A hypothesis records a proposal with an inconclusive outcome. A receipt binds
one observation, its exact verification scope, assertions, counts, and logs.
Passing receipts must cover every declared check. Failed, skipped, inconclusive,
and absent evaluation outcomes remain distinct. A prior observation's passing
receipt cannot authorize a different selected observation.

## Host authority

Structure and authority are separate. Normalization retains structural evidence,
including failed runs, and verification and lint recheck the original bytes.
Coverage policy `independent_execution_pass` additionally requires a selected
passing observation and a scope-complete passing receipt, authenticated by
principals with different host controller identities.

Select an authority policy in `research.yml`:

```yaml
evidence_trust:
  policy_id: host-evidence-authority
  policy_revision: "1"
```

The host supplies an absolute `EVIDENCE_WIKI_AUTHORITY_FILE` outside the workspace.
The file must be private, singly linked, regular, at most 1 MiB, and reachable
without symlinks. The policy uses schema `evidence-trust-policy/v1` and contains
the selected ID and revision, `not_before`, `expires_at`, `principals`,
`revoked_keys`, and `revoked_envelopes`. Each principal declares a controller,
roles, and a mapping of key IDs to secret hexadecimal keys. Generator and
evaluator receipts require their respective roles. Distinct controllers cannot
share a credential. Workspace-local keys and self-declared evaluator labels
cannot establish independence.

An envelope contains `payload` and optional `authentication`. Authentication
schema `evidence-authentication/v1` binds scheme `hmac-sha256`, principal, key ID,
role, authority policy ID and revision, issuance and expiry, and signature. The
host signs `evidence-attestation/v1` followed by a NUL byte and canonical JSON
of `{payload, authentication}` with the signature omitted. Canonical JSON uses
sorted keys, compact separators, literal UTF-8, finite numbers, and one final
newline. Keys contain 32–64 bytes encoded as lowercase hexadecimal; signatures
are lowercase SHA-256 HMAC hex. Payload revocation IDs use the
`evidence-authenticated-payload/v1` domain and the same canonical serialization.

These are host-verifiable symmetric receipts. A host with the secret can create
them; they do not provide public signature verification or prove independence
beyond the host's principal and controller assignments. Keep authority files
outside exported artifacts. Expiry, revoked credentials, changed policies, and
changed original bytes are checked when authority is requested.

## Consuming the result

```python
report = workspace.normalize.validate_execution("execution:lab")
if report["valid"] and report["verification"]["eligible"]:
    selected_observation = report["selected_record_id"]
```

The matching command is `evidence-wiki normalize execution --target PATH
--source-id execution:lab`. Exit status describes structural validity; the
`verification` field carries the separate current authority decision. A valid
failed run is still available for analysis.

Context artifacts with role `context-packet` also pass the pinned native
qualified-packet validator. Original packet bytes and all qualifications remain
available; normalization explicitly omits the response body from the frontmatter
copy. Packet integrity adds no evaluator authority and does not establish live
freshness. Plain text context remains plain text. See
[Codebase analysis](codebase-analysis.md#qualified-context-packet-intake).

An observation may opt into `parameters.market_simulation` with schema
`market-simulation/v1` and hash-bound `plan`, `prices`, and `result` artifacts.
The plan binds the strategy, historical universe, evaluation intervals,
selection history, complete run configuration, costs, fills, adjustments, and
metric definitions before the held-out interval. All decision inputs qualify
at the selection cutoff; the price history qualifies at the later run cutoff.
Reports separate evidence completeness, recomputed arithmetic, simulated
performance, and uncertainty. An independently evaluated loss can be a valid
calculation. A signed passing label cannot override incorrect arithmetic or
unavailable inputs. Explicit negative snapshot selections preserve authenticated
failed calculations. This optional profile requires historical input authority
and snapshot contract v4; ordinary execution records need no market metadata.
