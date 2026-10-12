# Repeated reference integrations

`tools/run_repeated_integrations.py` is a checkout maintainer tool for a frozen
nine-slot integration study and automatic improvement selection. It reuses
native setup, source permission, research, review, refresh and current owners.
It does not change native acceptance rules or publish a release.

The reference recipe has three distinct logical host scenarios on the same
physical machine: direct Python, current-caller command envelopes and scoped
in-process MCP JSON-RPC. The MCP scenario initializes, discovers granted tools
and calls the same research owner; it does not represent a stdio or remote host.
The recipe plans one workspace per scenario with a baseline and two distinct
source-update slots.
Inputs are explicitly synthetic finite facts: a scoped sum of 9 and 2. Updates
change the permitted normalized evidence title and complete source-revision
identity while preserving the value 11. Completed changes exercise invalidation and fresh review after identity changes;
they do not establish adaptation to changed measurements. A failed cycle or
nonterminal parent leaves later slots unstarted. Pending effects cannot supply a
terminal parent or justify continuing an expired allocation.

## Frozen study and original execution

```sh
.venv/bin/python tools/run_repeated_integrations.py freeze \
  --proposals /private/host/proposals.json --output /private/host/study.json
.venv/bin/python tools/run_repeated_integrations.py run \
  --plan /private/host/study.json --expected-plan EXTERNAL_PLAN_SHA256 \
  --host direct --directory /private/work/direct
```

Repeat the run through `current_caller` and `scoped_mcp`, each with a new
directory. A trusted orchestrating host assigns fresh Integration agents and
retains their actual command observations. Use the existing
[`measure_integration_costs` observer](integration-costs.md) around each exact
run argv, with closed stdin, externally retained receipt hashes, private signing
custody separate from workers and the original frozen allowance. The generated reference plan defaults to 900 seconds for baseline setup and
execution, 600 seconds per change, and a 2100-second enclosing scenario
allowance. A trusted host may declare a separate future study with different
elapsed allowances and a sufficient study lifetime before any of its outputs
exist. That declaration preserves earlier attempts and their original caps; it
does not extend existing allocations or change authority, quality or witness
expiry requirements. An outer supervisor must cover startup,
native children and ordinary cleanup. Failed or timed-out work must remain in
the original inventory; exact replay never dispatches another integration.

The plan pins candidate bytes, actual local interpreter/system/machine and
reference host implementation bytes, route scenarios, all nine input identities,
cost caps, policy and proposal thresholds before trial evidence is consumed.
Keep its hash outside worker inputs and retained evidence. The supplied-source
recipe creates native protected fixture services and synthetic signing keys;
those bytes stay private and are never exported in the study report. Shared-user
processes and logical controller names do not establish hostile-worker isolation
or reasoning independence.

Each real update deposits an exact permitted artifact closure and materializes
it through public usage APIs under authenticated grant/scrub/command ownership.
Refresh uses a new run/request linked to the original terminal parent. Parent
file manifests are retained before and after; source/basis changes, fresh
evaluation, current accepted briefs and approval reuse remain separately checked.
When stale source bindings make the calibrated review context unknown, the
current native refresh owner prepares and drafts the superseding context before
qualified planner selection resumes. Evaluation and release still require the
new exact claim bindings and fresh qualification.
Each original result, brief and accounting response is retained immediately in
an immutable private file with byte commitments. Native response envelopes,
including refusals, are retained before subsequent reads or deadline checks;
reports expose only bounded error codes and reasons alongside byte commitments.
An exact `RESEARCH_RESOLUTION_REFUSED` with reason
`resolution_current_inputs_changed` permits at most one fresh read-only brief
read under the same remaining operation and cycle allowance. The fresh read
must pass every native current-input and acceptance check. The original refusal
and its elapsed cost remain retained; no clock or witness is renewed, and the
read does not count as an applied native repair. Other refusals, an exhausted
allowance or a second refusal fail the slot and leave successors unstarted.
A fresh brief cannot change the outward refresh result or retroactively qualify
an earlier failed study. Full native authenticated journals supply applied
review counts including adjudication and applied repair counts. These counters
cover completed applications; failed or unknown dispatch intents stay in native
accounting and elapsed time and cannot be inferred as additional applied work.
A bounded report page alone cannot prove complete work.

## Measurement and scoped usefulness

Outputs retain every planned slot, including unstarted, started with unknown
stop, failed and refused attempts. Terminal coverage and integration success
use the frozen nine-slot denominator. Accepted-result usefulness is exact
finite reference conformance from the current eligible claim; historical
accepted output cannot supply current acceptance after source changes. Native
review and repair counts, elapsed intervals, changed sources, parent linkage
and history preservation retain their original scope.

The enclosing observer authenticates actual command retention and timing, not
provider identity or a bill. Synthetic model counters and zero fixture charges
are not actual provider costs. Study work outside observed commands, model
waiting, tokens, pricing, human effort and whole-host cost remain unknown.
One-time reference setup is included in baseline elapsed; repeated operation
burden is retained separately. Unknown signals are not fabricated zeros.
Simulated integration success supports integration usability hypotheses, not
voluntary adoption, demand, saved human hours or ROI. Actual usage may be
imported separately through the original [`OutcomeIngestion`](library-api.md#observed-outcomes)
owner; its absence does not require recruitment or gate technical work.

## Current original replay and improvement policy

The trusted host builds an `originals` mapping per host ID with `path`, exact
`raw_sha256`, canonical `source_head_sha256` and `observation`. The latter
contains the original cost `plan`, `attempt_id`, exact producer `argv`,
`directory`, `work_root`, externally retained `expected_receipt_sha256` and
private `observer_key_path`. The cost binding is
`{"integration_plan_sha256": EXTERNAL_PLAN_SHA256}`. The argv must select this
exact run tool, plan bytes, host route and report directory. These are trusted
host selections, never credentials or reader flags supplied by an integrator.

Compute the evidence pin mapping using
`tools._integration_improvement_owners.evidence_pins(plan, originals)` and
retain its `tools._release_decision.digest` externally.

```sh
.venv/bin/python tools/run_repeated_integrations.py decide \
  --plan /private/host/study.json --expected-plan EXTERNAL_PLAN_SHA256 \
  --originals /private/host/originals.json --expected-evidence EXTERNAL_EVIDENCE_SHA256 \
  --output /private/host/decision.json
.venv/bin/python tools/run_repeated_integrations.py replay \
  --plan /private/host/study.json --expected-plan EXTERNAL_PLAN_SHA256 \
  --originals /private/host/originals.json --expected-evidence EXTERNAL_EVIDENCE_SHA256 \
  --report /private/host/decision.json
```

Current readers authenticate the original command receipt, exact stream bytes,
response files and native journals. Existing native replay authenticates
reference calibration and current configuration/source/acceptance before the
gate consumes a projection. Its original short current-proof expiry is
preserved; the study deadline cannot renew that witness. All owners are read
twice and final candidate/host, evidence and expiry checks precede publication.
Input bounds and strict duplicate-key checks apply. Exclusive output preserves
earlier artifacts. A retained decision requires current replay, not blind reuse.

Frozen precedence is `stop`, `revise`, `narrow`, `continue`. Unsafe invariants,
source/host integrity changes and exceeded caps stop. Functional failures,
missing/unknown required observations or invalid terminal assertions require
revision. Correct unsupported scopes or insufficient accepted usefulness narrow
the claim. Complete required coverage and useful supported results permit
continuation. Reasons link exact original projection values and byte pins.

Proposals freeze an ID, kind, positive estimated `agent_work_units` effort,
measured trigger/threshold and experiment capability permission before the
runs. Ranking uses the smallest estimated effort, then observed effect and ID;
estimates do not establish measured human time. Dependency review proposals
need measured change-cycle signals in at least two distinct host scenarios;
baseline setup alone cannot qualify repeated review optimization. Other
nonrepair proposals also need repeated scenario support. Unmeasured retrieval
omissions, host limitations and demand stay unknown. Selected capability work
is a next qualification experiment, with no substantive expansion qualification
or implementation grant. If no proposal is supported, the decision explicitly
selects none. A passing study never manufactures a business claim or silently
starts the next implementation.
