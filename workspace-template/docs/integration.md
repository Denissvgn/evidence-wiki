# Integration journey

Library API 60; operation matrix 41. Documentation basis: `f32b3fa83ee86bd6ec0dc35122116bb7851a95e60bb0e7d6086ee3af597c502a`.

## Journey

Fetch `guide/integration/v1` and `example/integration-docs/v1` with public
`Onboarding.open().resource` or MCP `onboarding_resource`. Open a read-only handle
with `with Onboarding.open() as onboarding:` before retrieving resources; the
Onboarding constructor itself is not a zero-argument factory. The installed example generates
this guide, dispatches a Documentation agent and independent critic, and returns
terminal structured data. Host recipes and source provisioning are separately
available as `example/agent-host/v1` and `example/unattended-host/v1`.

Use the same installed interpreter for parent and native children. Provision the
workspace, admitted sources and an autonomous HostRuntime before research.
Generating agents receive native question/evidence projections; the host retains
credentials, signing services and held-out qualification labels.

```python
from evidence_wiki import Workspace, research_request

def run_journey(target, runtime, run_id="original"):
    with Workspace.open(target) as workspace:
        result = workspace.research.command(
            research_request("run-cycle", run_id=run_id, request_id=run_id+"-cycle", max_steps=64),
            runtime=runtime,
        )
        brief = None
        if result["status"] == "returned":
            brief = workspace.research.command(research_request("brief", run_id=run_id), runtime=runtime)
        return {"cycle": result, "brief": brief}
```

Call run_journey with the host-provisioned target and runtime. Hosts can select
HostRuntime.from_file for compatible owner-private native runtime files, or
HostRuntime.from_services for explicitly provided public services.

For current outputs, use brief with format="structured". Its lifecycle has
terminal/outcome fields; verdict is current evidence eligibility. Require a
returned accepted/partial terminal cycle and current accept/partial brief before
requesting refresh with a new run_id/request_id and the previous_run_id.
Independent installation/route trials should use separately provisioned targets.
For continued work on an existing terminal target, use the native refresh path
and retained parent identity. Preserve any provenance refusal even if an earlier
cycle returned accepted; current output eligibility is checked separately.
After an uncertain write, reconcile the original IDs instead of creating a
replacement. report max_records and recovery-status max_effects accept 1..64;
retain their paging/truncation flags. Start refresh with an explicit small step
ceiling appropriate to the selected host budget. A trusted host enforces a
finite outer deadline covering preparation and all calls, in addition to native
per-run budgets. Record its numeric bound and unknown cleanup/effect observations.

`research_request` builds and validates the exact native envelope; callers do not
author schema documents. Inspect the domain outcome even when transport status
is returned. Missing runtime, capability or incompatible policy is terminal data;
there is no stdin prompt, credential discovery or human review task.

## Policy

The host selects immutable RunPolicy, roots, grants, finite usage/step/repair
ceilings and allowed effects. Native owners check actual current permissions,
source closure and policy at execution and closing boundaries. Declared refs and
tool discovery are not authority. Human-required profiles cannot be silently
converted to autonomous approval; incompatibility terminates without an operator
request. Do not reset original budgets or business IDs after a timeout.

## Executor

Wire an explicit AgentExecutor with profile plus invoke, poll, cancel and
authenticate methods, and use HostRuntime.from_services(policy, context,
execution) for an embedding host. ResearchExecution wraps the selected executor,
protected qualifications and host signing/observation services. The native
BoundedSubprocessExecutor is a bounded reference adapter; host-owned commands and
usage meters remain explicit selections. Native scoped worker_document input
withholds parent correlation and keys. A receipt proves host-observed execution;
it does not prove provider identity, OS isolation, billing or semantic truth.
Unmetered usage remains unknown. A second model CLI is optional.

## Qualification

Qualified evaluators need authenticated, current, purpose/domain/rubric-bound
qualification under the native policy, independent configured controllers and
complete original evidence context. Host-protected reference labels stay outside
worker inputs. Calibration, source/usage admission and review signing belong to
native host owners; generator output and agent agreement cannot qualify them.
Scripted fixture mechanics, live host execution and production semantic
qualification are separate evidence classes. The installed synthetic examples
demonstrate mechanics and cannot certify production accuracy.

## Effects

Operation effects and required arguments below derive from the public shared
declarations. Research writes require both startup host grants and native policy/
source permissions. Current/retained reads may expose only permitted material;
instruction/schema discovery never admits a source or approves publication.
Prepare/deliver/reconcile use native destination and receipt owners. Reports
preserve truncation and unknowns; a bounded missing result is not proof of absence.

## Concurrency

Native owners serialize protected workspace/request writes and reserve work
before releasing gates for agents. The scoped MCP stdio server has one research
worker and bounded request buffering; concurrent business work may return busy.
Use explicit original run/request IDs and retained pending/recovery status.
Cancellation is cooperative at safe checkpoints, suppresses a transport response
and preserves effects. Reconnect cannot roll back a write or renew budgets.
Resolve uncertain dispatch through native pending/cancel/reconcile operations
before starting another attempt. Hosts retain an outer finite operation deadline.

## Mcp

Start serve-onboarding-mcp with allowed roots, explicit research runtime and
write-operation grants. Initialize, send notifications/initialized and discover
tools/list on startup and reconnect. Research tools accept target and declared
operation arguments; clients cannot select runtime, credentials, grants or
qualification. Build arguments with research_request, then send its arguments
plus target to the corresponding tool name below. Onboarding protocol scope and
the research runtime are selected separately. No elicitation or sampling is
required. Legacy serve-mcp remains a separate workspace protocol.

## Migration

Negotiate the installed library API and operation matrix at startup; compare
the documentation basis rather than assuming package version alone identifies
the surface. Rebuild documentation when public declarations change. Existing
Workspace methods and legacy MCP retain their separate meanings. Native runtime
files support only their declared versions; use HostRuntime.from_services for
advanced public profiles instead of downgrading them. Migrate source/workspace
configuration only through explicit public plan/apply owners. Preserve native
history, authority and original budgets; missing capability is a terminal result.

## Terminal outcomes

`accepted`, `partial`, `abstained`, `rejected`, `stop_budget`, `blocked_external`, `policy_incompatible`.

Transport `returned` reports domain data, including negative outcomes. `refused` contains a typed error. Neither status grants external-action authority or evidence acceptance.

## Operation reference

| Operation | Effect | Required arguments | Optional arguments | MCP tool |
| --- | --- | --- | --- | --- |
| `run-cycle` | native write | run_id, request_id | expected_basis_id, max_steps, planner_config_ref | `research_run_cycle` |
| `refresh` | native write | run_id, request_id, previous_run_id | max_steps, planner_config_ref, source_updates | `research_refresh` |
| `current` | read | run_id | none | `research_current` |
| `next-work` | read | run_id | none | `research_next_work` |
| `brief` | read | run_id | format | `research_brief` |
| `resolution` | read | run_id | none | `research_resolution` |
| `recovery-status` | read | run_id | after_sequence, max_effects | `research_recovery_status` |
| `history` | read | run_id | kind, after_index, max_records, expected_history_basis_id | `research_history` |
| `changes` | read | pairs | before_requirements_revision_id | `research_changes` |
| `impact` | read | none | pairs, before_requirements_revision_id | `research_impact` |
| `advance` | native write | run_id, request_id, expected_basis_id | none | `research_advance` |
| `run-until-terminal` | native write | run_id, request_id, expected_basis_id | max_steps | `research_run_until_terminal` |
| `pending` | read | run_id | none | `research_pending` |
| `cancel-pending` | native write | run_id, request_id | none | `research_cancel_pending` |
| `reconcile-pending` | native write | run_id, request_id | none | `research_reconcile_pending` |
| `reconcile-recovery` | native write | run_id, request_id, expected_basis_id | none | `research_reconcile_recovery` |
| `report` | read | run_id | after_sequence, max_records | `research_report` |
| `semantic-report` | read | run_id | after_index, max_records, expected_snapshot_id | `research_semantic_report` |
| `repair-progress` | read | run_id | original_id, after_sequence, max_records | `research_repair_progress` |
| `review-services` | read | run_id | none | `research_review_services` |
| `prepare-acquisition` | read | run_id, request_id | alternative_ids | `research_prepare_acquisition` |
| `choose-acquisition` | native write | run_id, request_id, instruction, config_ref | none | `research_choose_acquisition` |
| `run-acquisition` | native write | run_id, request_id, instruction, config_ref, metadata, path | none | `research_run_acquisition` |
| `acquire-evidence` | native write | run_id, request_id, source_request_id, expected_request_basis, metadata, path | none | `research_acquire_evidence` |
| `dispatch-acquisition` | native write | run_id, request_id, source_request_id, expected_request_basis | none | `research_dispatch_acquisition` |
| `examine-coverage` | native write | run_id, request_id, question_slug, config_ref | source_ids | `research_examine_coverage` |
| `examine-counterevidence` | native write | run_id, request_id, question_slug, claim_id, config_ref | source_ids | `research_examine_counterevidence` |
| `calculate` | native write | run_id, request_id | as_of | `research_calculate` |
| `propose-calculation` | native write | run_id, request_id, original_id, config_ref | as_of | `research_propose_calculation` |
| `assemble-review-packet` | native write | run_id, request_id, claim_id | task_role, page_chars | `research_assemble_review_packet` |
| `prepare-delivery` | read | run_id | none | `research_prepare_delivery` |
| `deliver` | native write | run_id, request_id, proposal | none | `research_deliver` |
| `delivery-status` | read | run_id, request_id | none | `research_delivery_status` |
| `reconcile-delivery` | native write | run_id, request_id | none | `research_reconcile_delivery` |

Wire argument types and bounds are in `IntegrationDocumentation().reference()`. Native owners impose additional policy, custody and budget constraints.
