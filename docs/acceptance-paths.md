# Adversarial acceptance checks

`tools/verify_acceptance_paths.py` runs a fixed finite catalog over existing
native owners and boundary codecs. It covers stale and forged review inputs,
hostile source text, workspace custody, replay, concurrent requests, revocation,
output substitution, cancellation, incomplete context, unsupported output,
identity claims, colluding passes, hidden labels, policy edits, judge positions
and finite repair allowances.

The source registry selects every required challenge. Before execution, the
host freezes candidate, harness, plugin, tests, fixtures, framework commitments,
exact collected node IDs, ordering and finite process caps. A copied capsule
keeps qualification inputs separate from execution workspaces. Missing selectors,
skips and unresolved collection do not count as coverage.

The seed declares challenge priority. Execution keeps each Python module in one
contiguous block, preserving that priority within the block so module fixtures
have one lifecycle. The manifest commits both module order and every exact node;
all selected challenges retain their original obligations and finite caps.

```sh
.venv/bin/python tools/verify_acceptance_paths.py \
  --evidence-dir /private/host/acceptance-run \
  --observer-key /private/host-keys/acceptance.key \
  --seed 17 --timeout 1800
```

Select an existing owner-private 32–128-byte observer key, kept outside the
evidence root. The evidence directory must be a new canonical private path
outside the checkout. Preserve the returned report hash separately from its
directory. It binds the retained report and observations for later verification;
the key is never supplied to the pytest worker.

The observer retains exclusive process reservations, confirmed starts,
exit/cleanup outcomes and bounded stdout/stderr. The pytest adapter records exact
start, setup, call, teardown and finish events. It records executed assertions,
explicit public owner returns, and bounded projections and byte commitments of
native artifacts. Credential stores are excluded. A successful log line or
aggregate test count cannot replace these joins.

The automatic result is:

- `FAIL` when a critical check fails or committed inputs or retained evidence
  change. Consensus and proposed threshold changes cannot remove that failure.
- `ABSTAIN` when observations are missing, malformed, skipped, unsupported,
  interrupted or otherwise insufficient to establish the frozen obligations.
- `PASS` only when all exact obligations and their required evidence complete.

A correct owner refusal may pass its challenge. The refused operation remains
refused: this grants no answer success, source approval, spending authority or
native release authorization. A failed check also does not by itself identify
the cause as a proven authority bypass; setup and observation failures remain
distinct from demonstrated invariant failures.

`run_acceptance` returns the report and its exact retained hash. Custom catalogs
remain declared fixtures; they do not become the source-fixed official catalog
by supplying category names. `read_acceptance_report` requires the independently
retained key and report hash, authenticates custody, and checks retained evidence
and the frozen grade rather than trusting caller-provided approval labels.

A fresh verifier should execute and reproduce the sealed candidate separately
from the implementation agent. CLI role names cannot authenticate that agent
assignment or reasoning independence. Retain previous failures, interruptions
and incomplete reservations; reproduction uses the same committed inputs and
finite rules, without an automatic repair loop or reset of allowances.

These checks establish finite declared conformance. Hostile text probes test
input and capability boundaries; they do not establish a real model's resistance
to instructions. Native workers, reference judges and calibration observations
may be synthetic. Source-order and position variants are finite checks rather
than a general bias measurement. Redacted or truncated native projections are
not complete raw journals. Same-user processes, shared dependencies and
before/after file hashes do not prove hostile-worker OS isolation or exclude
transient change-and-restore attacks. Provider usage, billing, semantic truth and
production security remain unqualified.
