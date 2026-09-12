# Authenticated evidence assessments

An assessment binds selected answers, readiness and grounding results to captured evidence, configuration, implementation and host authority. Current consumption rechecks those inputs, permissions, validity and assessment state. An eligible result authorizes no external action.

This optional contract is domain-neutral. It uses the same envelope for procurement, laboratory research or a host's market research workflow. The core opens no provider connections, holds no action credentials and executes no model, strategy or external action.

## Host setup and public API

Configure [host-owned usage authority](evidence-usage.md), including a private state directory and independently configured trust policy. Deposit exact sanitized raw and normalized source revisions with retrieval and export permissions, source temporal metadata and complete ancestry. The trusted signing principal needs the `assessment` role. Keys and signing remain host responsibilities.

Library API contract version `11` exposes `evidence_assessments` and these operations:

| Operation | Input | Result |
| --- | --- | --- |
| `ws.assessments.prepare(request)` | Assessment request below | Assessment and unsigned registration command |
| `ws.assessments.issue(envelope)` | Host-authenticated registration | Atomic host ledger receipt |
| `ws.assessments.check(envelope)` | Exact issued envelope | Current eligibility, reasons and checkpoint |
| `ws.assessments.plan_refresh(request)` | Bounded refresh request | Coverage, affected assessments and unsigned application command |
| `ws.assessments.apply_refresh(envelope)` | Host-authenticated application | Atomic invalidation receipt |

The CLI equivalents are `evidence-wiki assessments prepare`, `issue`, `check`, `plan-refresh` and `apply-refresh`, each with `--target WORKSPACE`. They accept one JSON document on stdin and emit JSON. `check` exits `0` when eligible and `1` when ineligible. Contract, authority or input refusals use `EVIDENCE_ASSESSMENT_REFUSED` and exit `2`; library refusals raise `EvidenceWikiError` with that `error_code` and a stable reason.

```python
import evidence_wiki

request = {
    "schema_version": "evidence-assessment-request/v1",
    "question_slugs": ["supplier-specification"],
    "temporal": {"mode": "current", "cutoff": None},
    "purpose": "research",
    "consumer": "evidence-wiki",
    "expires_at": None,
    "review": "approved",
}
with evidence_wiki.Workspace.open("research") as ws:
    prepared = ws.assessments.prepare(request)
    # The host assigns a durable request_id and authenticates the complete
    # registration command under its assessment-role trust anchor.
    envelope = host_sign_assessment(prepared["registration"])
    receipt = ws.assessments.issue(envelope)
    current = ws.assessments.check(envelope)
```

`host_sign_assessment` is supplied by the host. It follows the whole-envelope attestation contract in [usage authority](evidence-usage.md), preserves the prepared checkpoint and fills `request_id`. A reviewer name or `review: approved` request alone does not authenticate approval. Issuance independently recomputes the complete publication proof before committing the host-attested review outcome.

## Identity, validity and capture

`evidence-assessment/v1` declares required capabilities and contains the canonical request, selected-question scope, evaluation and expiry times, source revisions and ancestry, full selected-publication result, explicit gaps and recorded eligibility. Its basis binds the workspace revision, configuration, trusted producer and host authority. The workspace capture includes applicable local packs. `assessment_id` hashes the complete canonical payload except itself; a digest is an integrity identifier, not proof of origin. The host authentication covers the complete registration command, including that payload and any superseded assessment IDs.

Preparation uses one coherent selected-publication capture. Before private materialization it qualifies exact sanitized raw and normalized bytes against host-owned source closures and checks retrieval/export authority for both publication and the requested use. Coverage is explicitly `all_workspace_sources`: global publication gates depend on all declared workspace sources. Unregistered raw files, ambiguous byte-to-revision matches or missing dependencies refuse. When identical normalized bytes belong to multiple accepted revisions, an exact `usage_revision_id` in the source manifest disambiguates them. Ordinary selected publication retains its existing usage restrictions outside the private authorized capture.

Expiry is no later than the earliest required source, effective-interval, permission, scrub-authentication or policy limit; the request may shorten it. Unknown source validity remains explicit with no current-use eligibility. Pending/rejected review and historical replay can be recorded when their inputs can be qualified, but cannot pass current consumption. `current` uses the host clock. `historical-audit` and `historical-available` require an explicit cutoff and retain the [temporal qualifications](temporal-evidence.md).

Consumption requires the exact registered authenticated envelope, current authority, approved review, known unexpired validity and unchanged selected answers and dependencies. It rechecks current policy, source-use permissions, corrections and invalidations even when no notification arrived. An accepted correction never revives its predecessor merely because the correction was withdrawn or expired. Missing or ambiguous correction history is ineligible. Global input changes can conservatively invalidate multiple question scopes; an unrelated question edit with unchanged inputs and selected results need not invalidate them.

The check holds the shared host-state lock across evaluation and revalidates at the final read boundary. It is a point-in-time evidence decision. The host must check again at consumption and enforce its own current action constraints. `external_action_authorized` is always `false`.

## Bounded refresh and history

```json
{
  "schema_version": "evidence-assessment-refresh-request/v1",
  "changed_sources": [],
  "evaluation_time": null,
  "limit": 32,
  "cursor": null
}
```

Optional `changed_sources` entries contain `source_id` and `source_revision`. They are hints: the bounded scan still checks every assessment on the page, so lost or incomplete notifications cannot establish eligibility. An explicit future evaluation time can forecast expiry. An older time cannot roll back the host's current checks. Applying a forecast invalidation makes the affected assessment ineligible immediately; it requires a new assessment to restore eligibility.

Plans report total and scanned counts, limit, truncation, whether previous pages are excluded, and a resume cursor bound to the dependency generation. Only a first-page scan that covers the whole set reports `complete: true`. Resume pages never claim complete global coverage. A source, configuration, authority or assessment-set change invalidates the cursor. Applying the plan's own monotone invalidations preserves it.

Each affected entry retains the question scope, stable reasons and a durable `reevaluation_id`. A signed apply independently recomputes the plan under an exclusive lock and checks the prepared checkpoint before writing one complete ledger generation. Repeating the same command returns the original receipt. A stale or out-of-order command refuses. A failure after publication can be reconciled by retrying the same command, without duplicating events or jobs.

Applied invalidations retain original evidence and review history and mark assessments as needing reevaluation. A newly prepared registration can name prior same-scope assessment IDs in `supersedes`; those old assessments stay ineligible. Derived usage lineage also carries assessment invalidation. The host schedules the returned reevaluation IDs, coalesces notifications, enforces run budgets and prepares replacements. Refresh does not reopen blocked source requests or execute reevaluation, and does not reverse a previously accepted host action.

## Bounds and host integration

Requests and individual envelopes are limited to 1 MiB. An assessment accepts up to 32 questions, 64 workspace sources, 128 source ancestors and 64 candidate source-history revisions. Temporal artifact inspection has an 8 MiB bound. Each refresh scans at most 32 assessments and accepts 128 changed-source hints. The shared usage ledger remains bounded by 64 MiB, 4,096 lineage nodes and 10,000 events; full-state parsing is part of each operation. These are batch operations, with no streaming latency guarantee.

The [host decision example](../examples/assessment-host/README.md) uses a private SQLite database and standard-library callbacks. It separates current evidence checks, authenticated approval, risk policy, operational-state checks and durable submission identity. An uncertain timeout is reconciled before any retry; it cannot promise exactly-once external execution. Read-only provider and action credentials, partial outcomes, cancel/replace and emergency recovery remain host-owned. Historical records do not override retention or deletion policy; the host owns the permitted retention lifecycle.
