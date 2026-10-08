# Automated semantic judging

Select `SemanticJudgingPlan(evidence_config_ref, scope_units_config_ref,
counterevidence_config_ref)` when constructing an `ExecutionProfile`. Evidence
and Scope/Units use distinct `evaluator` configurations; Counterevidence uses
`critic`. All three controllers must be separate from generation. Select an
independently calibrated `adjudicator` with `DisagreementPlan`. The profile needs
complete role scopes and protected calibration selections for all four judges.
This opt-in profile does not select alternate review services.

Build this profile before issuing qualifications. Qualification binds the exact
profile, effective model revision/settings, prompt, rubric, reference program,
suite, split and evidence domain. Changing these inputs requires requalification.
Configure actual services with `HostRuntime.from_services`; no model SDK or
credential provisioning is implicit. A human-required policy remains a terminal
incompatibility, with no operator task.

The current calibrated domain is exact finite structured observations. Free
prose, unsupported premise graphs and refusal-only contexts cannot borrow that
qualification. Missing profiles, withdrawal, expired configuration probes or
out-of-domain evidence produce unknown or terminal results. Configured controller
separation does not prove independent reasoning, provider identity or OS isolation.

Each first pass receives a fresh immutable packet with the exact original,
required question units, current candidate claims and all authorized source
content. Generator/controller identity, prior judgments, credentials and protected
reference labels remain outside first-pass inputs. Other sources in the
calibrated original inventory are visible even when the target claim does not
cite them. The finite classifier requires every supplied source to be referenced
by some candidate claim; entirely uncited or unsupported alternatives make
qualification unknown. Multi-source conflict accuracy and exhaustive search
remain unestablished. Inputs
are byte bounded; omission or overload does not become successful evaluation.

Judges return the six native rubric verdicts plus separate checks for support,
scoped correctness, missing material evidence, appropriate abstention and
unnecessary refusal. Every check carries a unit, concise reason, severity,
self-reported uncertainty and exact string anchors. Native owners validate
pointers, character offsets, quotes, unit coverage and verdict consistency.
Numerators and denominators are computed from frozen owner units. Unknowns retain
their denominator; zero denominators have no score. Technical execution failure
is not evidence of unnecessary refusal. Native assertion review alone cannot
establish unwilling refusal or grade unsupported abstention cases.

Independent finite reference calculations can reject a model pass over a failed
declared value, scope, time, unit or contradiction invariant. These references
describe the declared facts, not the truth of the source. Executable references,
controlled mutations, published-reference provenance and model judgments are
distinct evidence classes; unsupported published labels remain unknown.

Existing disagreement handling owns targeted repair and investigation. Unanimous
first passes also receive advisory semantic adjudication in this profile. That
call shares the existing finite investigation allowance and cannot promote a
failed/unknown first pass, replace a native vote or override deterministic
failure. Unknown or failed advisory findings cannot release. Material revisions
require fresh first passes; exhaustion preserves unresolved originals.

Read schemas with `workspace.research.semantic_schemas()`. Within an explicitly
bound research session, read `session.semantic_report(max_records=16)`, or call
`research_request("semantic-report", run_id=..., max_records=16)` through the
shared Python/CLI/scoped MCP adapter. The report preserves every expected original
and unit, historical assessments, missing starts, actual judge configuration,
reference strata and current qualification. Only actual current qualified
adjudication supplies agent-adjudicated scores. Raw first passes remain agent
judgments; agreement alone is not ground truth or definitive accuracy.
Started counts require authenticated native worker starts or applied reviews;
missing start observations remain unconfirmed. Observed counts refer to applied
advisory findings. A failed or unapplied start supplies no score or unit coverage.

Pagination is bound to `snapshot_id`. Supply `expected_snapshot_id` for subsequent
`after_index` pages; changed snapshots refuse. Report access rechecks current
source permissions and closure. Reading dispatches no model or qualification
probe. An expired current probe can therefore suppress current scores while
retaining historical observations. Returned scores authorize no external action
or native acceptance, and per-dimension empirical calibration remains unknown.
