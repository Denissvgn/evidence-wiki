# Host-owned decisions

`host_decisions.py` shows a small host adapter for the [assessment contract](../../docs/evidence-assessments.md). It uses Python's standard library and works with any action domain. It is an example application outside the `evidence_wiki` API.

Create `DecisionStore` in a private host-owned directory outside the research workspace. Supply trusted callbacks for current evidence verification, authenticated approval, risk policy, current operational state, submission and reconciliation. For evidence verification, pass `workspace.assessments.check`; each remaining callback belongs to the host. Provider credentials, roles, exposure limits and emergency stops belong there too.

Use a durable decision ID that identifies one intended action. The store binds it to the exact action and authenticated assessment envelope. Repeated delivery returns the existing decision; a changed binding refuses. A pending reservation commits before submission. An exception or missing receipt leaves an uncertain outcome. Redelivery reconciles that ID and never blindly submits it again, including when a crash occurred before the first submission.

The provider adapter must associate the decision ID with its own durable idempotency key and return receipts only for confirmed acceptance. Acceptance is not completion: partial results, later rejection, cancel/replace, reconciliation evidence and emergency recovery require the host's full action lifecycle. If the provider cannot establish what happened, the example retains uncertainty for operator resolution. It does not promise exactly-once external execution.

A procurement approval can require an authenticated reviewer and a spending limit. A simulation host can reject an otherwise valid losing calculation under a predeclared loss limit. Neither a library `ship` verdict nor an assessment signature grants action permission.
