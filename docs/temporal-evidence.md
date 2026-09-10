# Temporal evidence

Temporal evaluation selects exact authorized source revisions at one decision
instant. The same cutoff governs selection, ancestor qualification, in-memory
lexical retrieval, declarative facet rules, and scalar grounding. It reads an
accepted host checkpoint without changing the workspace, stored sources,
questions, or query cache.

## Choose a temporal mode

| Mode | Required basis |
| --- | --- |
| `current` | Current host clock and current accepted state; both request cutoff and checkpoint must be null |
| `historical-audit` | Explicit timezone-aware cutoff and accepted checkpoint; every included revision was actually observed by the host by that cutoff |
| `historical-available` | Explicit cutoff and accepted checkpoint; every included revision has an independent public-availability receipt, even when the host retrieved it later |

A historical cutoff cannot exceed the current host clock. Publication and
availability must be known and no later than the cutoff. A measurement interval
must have finished, an effective interval must contain the cutoff, and an
applicable expiry must be later than it. Unknown required dates remain gaps.
Date-only and timezone-free timestamps refuse. Source corrections must form an
unambiguous chain in deposit order with nondecreasing availability. A future
correction cannot replace or invalidate an earlier qualified revision.

Every included ancestor must qualify. Current retrieval grants, purpose,
consumer, trust, and revocations still apply at the read boundary. Old state
cannot revive a revoked source. A historical result establishes neither current
freshness nor permission to act. The result always reports
`current_use.authorized: false`.

## Capture explicit source clocks

Use the [host usage store](evidence-usage.md) to deposit exact sanitized source
bytes with authenticated use and scrub permissions. Its `source-record.json`
can carry this complete `temporal` mapping:

```json
{
  "schema_version": "evidence-temporal-source/v1",
  "asserting_principal": "capture-provider",
  "measurement": {
    "start": {"value": "2026-09-01T00:00:00Z", "basis": "source-declared"},
    "end": {"value": "2026-09-02T00:00:00Z", "basis": "source-declared"}
  },
  "published_at": {"value": "2026-09-09T10:00:00Z", "basis": "source-declared"},
  "available_at": {"value": "2026-09-09T10:00:00Z", "basis": "provider-asserted"},
  "claimed_retrieved_at": {"value": null, "basis": "unknown"},
  "effective": null,
  "expires_at": null,
  "supersedes": null,
  "record_path": "structured.json",
  "provenance_path": "provenance.json"
}
```

Known clock claims use `source-declared` or `provider-asserted`; neither means
independently verified. Unknown claims have exactly null `value` and `unknown`
`basis`. `measurement`, `effective`, and `expires_at` can be null when not
applicable. Effective intervals have a claim for `start` and either an end
claim or null for an open end. Intervals are ordered, with an exclusive
effective end. Expiry is also exclusive. A claimed retrieval time cannot be
earlier than availability or later than the actual host observation.

`supersedes` is null or an exact earlier revision digest for the same source.
The host records actual observation time and immutable revision identity
separately. A provider cannot set that observation by writing a source date.
An empty legacy temporal mapping is unsupported for replay; the package does
not derive missing dates from filenames, mtime, Git history, or prose.

The two structured paths are null or exact included JSON artifacts. An included
normalized Markdown artifact must have frontmatter with the same `source_id`.
Original usage denials, inconsistent identities, and `evidence_usable: false`
remain disqualifying. Query and policy evaluation consume these stored bytes,
including their original qualifications.

## Independently attest public availability

The host verifies upstream publication evidence and signs a whole-payload
`evidence-authentication/v1` envelope with an authorized `availability` role.
The signer's configured controller must differ from the source's
`asserting_principal` controller. Changing a label inside a source cannot
provide this authority. The receipt payload has exactly these fields:

| Field | Value |
| --- | --- |
| `schema_version` | `evidence-availability-receipt/v1` |
| `source_id`, `source_revision` | Exact deposited source and closure digest |
| `asserting_principal` | Principal in the temporal source mapping |
| `published_at`, `available_at` | Explicit timestamps matching the original claims |
| `method` | `public-archive` or `publisher-publication-record` |
| `proof_artifacts` | One to sixteen distinct `{path, content_hash}` bindings to exact deposited proof files |

Submit a signed usage command with action `attest-availability` and body
`{source_id, source_revision, receipt}`. It uses the normal expected checkpoint
and idempotent request identity. Each revision accepts one receipt. Its event
must exist in the replay checkpoint. Proof signatures and authority are checked
at the host's receipt observation time, with current trust and revocations
applied when reading. The package checks inert proof bytes and authenticated
claims; it does not contact archives or execute provider instructions.

## Evaluate a bounded request

```python
from evidence_wiki import Workspace

request = {
    "schema_version": "evidence-temporal-request/v1",
    "mode": "historical-audit",
    "cutoff": "2026-09-10T01:00:00Z",
    "checkpoint": "sha256:<accepted-event-digest>",
    "source_ids": ["laboratory:observation"],
    "purpose": "research",
    "consumer": "evidence-wiki",
    "analysis": {
        "query": "observations",
        "facets": [],
        "grounding": [{"id": "measurement", "source_id": "laboratory:observation",
                       "pointer": "/measurement/value", "expected": "17.50"}],
        "domain_pack": {},
        "question_frontmatter": {},
    },
}
with Workspace.open("/work/research") as workspace:
    report = workspace.temporal.evaluate(request)
```

Each facet is `{id, source_ids, policy_ids}` scoped to requested sources and
declarative rules supplied in `analysis.domain_pack`. Rules share one cutoff
for age and clock-skew calculations. `analysis.question_frontmatter` supplies
the question context. These are caller-declared evaluation inputs, bound into
the result identity; this operation does not authenticate them as a selected
workspace question or an approved assessment. Undeclared policies and policies
requiring recorded review remain `manual_review`.

Grounding resolves JSON pointers against selected structured bytes and returns
the canonical actual scalar and comparison outcome. Lexical retrieval uses
only qualified normalized records; cached indexes, current wiki answers,
semantic providers, and unresolved source payloads are absent. Derived
execution evidence requires a passing independently authenticated result whose
exact inputs are qualified ancestors. Generic non-source lineage nodes and
records declaring historical simulation are currently unsupported.

The result binds request, analysis, implementation, authority, checkpoint,
source revisions, lineage, cutoff, exclusions, and gaps. Repeating a request
against the same accepted checkpoint and unchanged authority yields the same
result identity while current rights still permit the read. Returned retrieval
permission time and current checkpoint are separate from that frozen identity.
`result.complete` means all requested sources were selected; it does not mean
every facet or grounding check passed. Inspect each outcome.

Limits are 32 requested sources, 64 candidate revisions, 128 ancestry records,
8 MiB of source artifacts and result bytes, 256 KiB of request JSON, 32 facets,
128 grounding assertions, and 1,024 query characters. A bound violation refuses
the whole evaluation instead of returning an unexamined successful prefix.
Malformed requests raise `SourceError` with `EVIDENCE_TEMPORAL_REFUSED` and a
stable reason in `details.reason`; unsupported source evidence remains an
explicit gap or exclusion.

```sh
evidence-wiki temporal evaluate --target PATH < temporal-request.json
```

CLI exit 0 means complete source selection, exit 1 means selection gaps, and
exit 2 means a refused request. [Historical snapshots](evidence-snapshots.md)
preserve cutoff-qualified source and availability bindings for independent
offline verification; export additionally requires current training and export
permission for the full closure.
