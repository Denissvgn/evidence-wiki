"""Decision reproduction and private-payload omission from bounded reports."""

from __future__ import annotations

import copy
import hashlib

import pytest

from evidence_wiki import execution_accounting as accounting
from evidence_wiki import execution_reporting as reporting
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.mutation_contracts import canonical, digest
from evidence_wiki.run_policy import RunPolicy
from tests import test_execution_accounting as accounting_fixture
from tests.test_run_policy import observation, policy_document

BINDING = "sha256:" + "3" * 64
PROFILE = {"profile_ref": "fixture-profile", "currency": "USD", "pricing_ref": "fixture-rate",
    "roles": [{"config_ref": "researcher", "role": "host_worker", "controller_ref": "generator",
        "model_ref": "fixture-model", "prompt_version": "prompt-v1"}]}


def rows(entries):
    result = []
    for index, (kind, data) in enumerate(entries):
        previous = {key: value for key, value in result[-1].items() if key != "event_ref"} if result else None
        result.append({"schema_version": "research-execution-journal/v1", "kind": kind, "binding_id": BINDING,
            "request_id": "request-" + str(index), "sequence": index,
            "previous_record_ref": digest(previous) if previous else None, "data": data,
            "authentication": "4" * 64, "event_ref": "event-" + str(index)})
    return result


def build(records, *, policy=None, **kwargs):
    policy = policy or RunPolicy(policy_document())
    return reporting.report(policy, PROFILE, BINDING, "run", records, None, **kwargs)


def step(policy, value=None):
    value = observation(policy) if value is None else value
    decision = policy.decide(value)
    return {"step": {"decision": decision, "attempt_id": None, "before_basis_id": "basis-a", "outcome": decision["outcome"]},
        "decision_input": value}


def test_policy_decision_reproduces_and_narrative_stays_private():
    policy = RunPolicy(policy_document())
    observed = observation(policy)
    observed["agent_rationale"] = "Bearer untrusted-private-narrative"
    # Optional narrative is deliberately excluded by RunPolicy's decision hash.
    value = build(rows([("execution_begin", {}), ("execution_step", step(policy, observed))]), policy=policy)
    decision = value["trace"][1]["decision"]
    assert decision["reproduced"] and decision["input_kind"] == "policy_observation"
    assert decision["outcome"] == "accepted" and decision["eligible_count"] == 2
    assert value["trace_complete"] and value["chain_complete"]
    assert b"untrusted-private-narrative" not in canonical(value)


def test_tampered_decision_or_input_refuses_even_outside_requested_page():
    policy = RunPolicy(policy_document())
    for field in ("decision", "decision_input"):
        data = step(policy)
        if field == "decision":
            data["step"]["decision"]["outcome"] = "abstained"
        else:
            data["decision_input"]["accepted_count"] = 0
        with pytest.raises(EvidenceWikiError):
            build(rows([("execution_begin", {}), ("execution_step", data)]), policy=policy, max_records=1)


def test_native_terminal_decision_is_hashed_and_reconstructed_without_authority_claim():
    policy = RunPolicy(policy_document())
    observed = {"basis_id": "basis-a", "policy_id": policy.policy_id, "outcome": "blocked_external",
        "reason": "executor_unavailable", "original_ids": policy.to_dict()["scope"]["question_ids"],
        "policy_compatible": True, "authority": {}, "qualification": {"state": "unknown"}, "usage": {},
        "steps_used": 1, "steps_limit": 4, "pending": None}
    decision = {"schema_version": "research-policy-decision/v1", "policy_id": policy.policy_id,
        "observation_sha256": hashlib.sha256(canonical(observed)).hexdigest(), "basis_id": "basis-a",
        "outcome": "blocked_external", "reason": "executor_unavailable", "terminal": True, "next_effect": None,
        "original_count": 2, "eligible_count": 0, "unresolved_count": 2, "selected_outcome_refs": [],
        "authorized": False, "evidence_accepted": False}
    data = {"step": {"decision": decision, "attempt_id": None, "before_basis_id": "basis-a", "outcome": "blocked_external"},
        "decision_input": observed}
    value = build(rows([("execution_step", data)]), policy=policy)
    assert value["trace"][0]["decision"]["input_kind"] == "native_terminal"
    assert not value["authorized"] and not value["evidence_accepted"]


def test_wholly_missing_record_is_visible_and_duplicate_or_chain_tamper_refuses():
    original = rows([("execution_begin", {}), ("execution_drive", {}), ("execution_step", {"step": {"decision": None}})])
    missing = build([original[0], original[2]])
    assert missing["missing_sequences"] == [1] and not missing["trace_complete"]
    with pytest.raises(EvidenceWikiError):
        build([original[0], original[0]])
    changed = copy.deepcopy(original)
    changed[1]["previous_record_ref"] = "sha256:" + "f" * 64
    with pytest.raises(EvidenceWikiError):
        build(changed)


def test_raw_model_input_output_authentication_and_unsafe_identifiers_are_omitted():
    attempt = {"attempt_id": "attempt", "input": {"api_key": "private-input-canary"},
        "job": {"role": "host_worker", "controller_ref": "/private/controller", "prompt_version": "password=private-prompt",
            "basis_id": "basis-a", "issued_utc": "2026-10-03T00:00:00Z", "deadline_utc": "2026-10-03T00:00:01Z"}}
    receipt = {"attempt_id": "attempt", "output": {"narrative": "private-output-canary"}, "output_sha256": "5" * 64,
        "authentication": "private-observation-signature", "controller_ref": "generator", "state": "succeeded",
        "observed_usage": {"seconds": 1.0, "model_calls": 1, "tool_calls": 1, "tokens": 10, "cost": "0.1"}}
    value = build(rows([("execution_attempt", {"attempt": attempt}), ("execution_observation", {"observation": receipt})]))
    raw = canonical(value)
    for secret in (b"private-input-canary", b"private-output-canary", b"private-observation-signature", b"private-prompt", b"/private/controller"):
        assert secret not in raw
    assert value["redaction_count"] == 2
    assert value["trace"][0]["input_ref"] == digest(attempt["input"])
    assert value["trace"][1]["output_ref"] == "sha256:" + "5" * 64


def test_bounded_pages_have_stable_snapshot_and_do_not_claim_whole_trace():
    records = rows([("execution_begin", {}), ("execution_drive", {}), ("execution_drive", {})])
    first = build(records, max_records=2)
    assert len(first["trace"]) == 2 and first["paging"]["has_more"] and not first["trace_complete"]
    second = build(records, after_sequence=first["paging"]["next_after_sequence"], max_records=2)
    assert second["snapshot_ref"] == first["snapshot_ref"] and len(second["trace"]) == 1
    assert second["trace"][0]["sequence"] == 2 and not second["paging"]["has_more"]
    empty = build(records, after_sequence=3)
    assert empty["trace"] == [] and not empty["trace_complete"]
    assert not build([])["chain_complete"]


def test_closed_schema_and_page_limits_refuse_unsupported_fields():
    value = build(rows([("execution_begin", {})]))
    changed = copy.deepcopy(value)
    changed["trace"][0]["raw_output"] = "unsafe"
    with pytest.raises(EvidenceWikiError):
        reporting.validate_report(changed)
    with pytest.raises(EvidenceWikiError):
        build([], max_records=65)
    with pytest.raises(EvidenceWikiError):
        reporting.schema_document("research-execution-report/v1")


def test_large_private_snapshot_hashes_per_record_and_still_returns_a_small_metadata_page():
    payload = {"attempt_id": "attempt", "input": {"private_large_input_" + str(index): "private-canary" * 3500
        for index in range(5)}, "job": {}}
    records = rows([("execution_attempt", {"attempt": payload}) for _ in range(5)])
    assert sum(len(canonical(row)) for row in records) > 1_048_576
    value = build(records, max_records=1)
    assert len(canonical(value)) < 262_144 and len(value["trace"]) == 1
    assert value["paging"]["has_more"] and b"private-canary" not in canonical(value)


def test_scoped_cost_summary_matches_declared_private_inventory():
    binding = {"session_binding_id": BINDING}
    baseline = accounting_fixture.opened()["observations"][0]["envelope"]
    baseline["binding"] = binding
    plan = accounting.start_coverage(binding=binding, scope_id=baseline["scope_id"],
        baseline=accounting_fixture.signed(baseline), verifier=accounting_fixture.verifier(), now_utc=accounting_fixture.NOW)
    intent = accounting_fixture.attempt()
    plan = accounting.reserve_attempt(plan, intent)
    result = accounting_fixture.observation()
    result["binding"] = binding
    plan = accounting.observe_attempt(plan, accounting_fixture.signed(result), accounting_fixture.verifier(), now_utc=accounting_fixture.NOW)
    closure = {"schema_version": accounting.CLOSURE, "binding": binding, "scope_id": baseline["scope_id"],
        "controller_ref": "host", "plan_sha256": accounting.digest(plan), "closed_at": "2026-10-03T00:00:03Z",
        "terminal_decision_ref": "terminal", "last_observation_ids": [row["envelope"]["observation_id"] for row in plan["observations"]],
        "authentication": None}
    plan = accounting.close_coverage(plan, accounting_fixture.signed(closure), accounting_fixture.verifier(), now_utc=accounting_fixture.NOW)
    summary = accounting.report(plan)
    records = rows([("execution_native", {"reservation": intent}), ("execution_native_observation", {"observation": result})])
    value = reporting.report(RunPolicy(policy_document()), PROFILE, BINDING, "run", records, summary)
    assert value["accounting"]["totals"]["cost"] == "0.125"
    assert value["accounting"]["observed_human_interventions"] is None
    with pytest.raises(EvidenceWikiError):
        reporting.report(RunPolicy(policy_document()), PROFILE, BINDING, "run", rows([("execution_begin", {})]), summary)


def native_intent(identifier="native"):
    return {"operation": "strict.register_review", "arguments": {"envelope": "private-envelope-canary"},
        "reservation": {"attempt_id": identifier, "job_id": identifier, "controller_ref": "session-observer",
            "issued_at": "2026-10-03T00:00:00Z"}}


def native_return(identifier="native", *, recovery=False):
    return {"observation": {"attempt_id": identifier, "controller_ref": "session-observer", "state": "completed",
        "observed_at": "2026-10-03T00:00:10Z", "usage": {"seconds": "10", "model_calls": 0,
            "tool_calls": 1, "tokens": 0, "cost": "0"}},
        "elapsed_scope": "utc_lifetime_including_recovery_wait" if recovery else "in_process_effect_elapsed",
        "lifetime_seconds": "10.000000001" if recovery else None,
        "active_execution_seconds": None if recovery else "0.250000001",
        "invocation_proof": "desired_state_only" if recovery else "observed_owner_call",
        "waiting_seconds": "api_key=private-wait-canary", "result": {"authentication": "private-result-signature"}}


def test_native_recovery_preserves_lifetime_with_active_and_waiting_explicitly_unknown():
    records = rows([("execution_native", native_intent()), ("execution_native_observation", native_return(recovery=True))])
    value = build(records)
    intent, observed = value["trace"]
    assert intent["attempt_id"] == intent["job_id"] == "native"
    assert intent["purpose"] == observed["purpose"] == "strict.register_review"
    assert observed["attempt_id"] == observed["job_id"] == "native"
    assert intent["input_ref"] == digest(native_intent()["arguments"])
    assert observed["output_ref"] == digest(native_return(recovery=True)["result"])
    assert observed["timing"] == {"provenance": "canonical_native_return",
        "elapsed_scope": "utc_lifetime_including_recovery_wait", "lifetime_seconds": "10.000000001",
        "active_execution_seconds": None, "waiting_seconds": None, "invocation_proof": "desired_state_only"}
    assert value["activity_summary"]["counts"]["native_recovery_observations"] == 1
    for secret in (b"private-envelope-canary", b"private-wait-canary", b"private-result-signature"):
        assert secret not in canonical(value)


def test_direct_native_execution_preserves_observed_active_interval_without_guessing_waiting():
    value = build(rows([("execution_native", native_intent()), ("execution_native_observation", native_return())]))
    timing = value["trace"][1]["timing"]
    assert timing["active_execution_seconds"] == "0.250000001" and timing["lifetime_seconds"] is None
    assert timing["waiting_seconds"] is None and timing["invocation_proof"] == "observed_owner_call"
    assert value["activity_summary"]["unobserved_measures"]["active_execution_seconds_total"] is None
    changed = copy.deepcopy(value)
    changed["trace"][1]["timing"]["raw_signature"] = "unsafe"
    with pytest.raises(EvidenceWikiError):
        reporting.validate_report(changed)


def test_canonical_activity_counts_include_off_page_records_and_ignore_worker_counters():
    policy = RunPolicy(policy_document())
    observed = observation(policy)
    observed["spent"]["model_calls"] = 1
    budget_step = step(policy, observed)
    budget_step["step"].update(terminal=True, action="terminal")
    assert budget_step["step"]["decision"]["outcome"] == "stop_budget"
    repair = {"attempt": {"attempt_id": "repair", "config_ref": "researcher", "job": {"id": "repair-job", "operation": "generate"},
        "input": {}}, "purpose": "repair"}
    retry = {"attempt": {"attempt_id": "retry", "job": {"id": "retry-job", "operation": "generate"},
        "input": {}}, "purpose": "retry"}
    cancel = {"observation": {"attempt_id": "retry", "job_id": "retry-job", "state": "cancelled",
        "cancellation_confirmed": True, "output": {"counters": {"repair_attempt_intents": 999, "repaired_defects": 999}}}}
    records = rows([("execution_begin", {}), ("execution_advance", {}), ("execution_native", native_intent()),
        ("execution_native_observation", native_return(recovery=True)), ("execution_attempt", repair),
        ("execution_attempt", retry), ("execution_cancel", {"attempt_id": "retry"}),
        ("execution_observation", cancel), ("execution_observation", cancel),
        ("execution_applied", {"operation": "evaluate", "claim_id": "claim", "strict_basis_id": "basis",
            "profile_ref": "review-a", "passed": True, "critical_failure": False}),
        ("execution_applied", {"operation": "evaluate", "claim_id": "claim", "strict_basis_id": "basis",
            "profile_ref": "review-b", "passed": False, "critical_failure": True}),
        ("execution_step", {"step": {"decision": None, "action": "reconcile", "attempt_id": "retry"}}),
        ("execution_step", budget_step), ("execution_close", {})])
    value = build(records, policy=policy, max_records=1)
    assert len(value["trace"]) == 1 and not value["trace_complete"]
    summary = value["activity_summary"]
    counts = summary["counts"]
    assert counts["durable_advance_intents"] == 1 and counts["durable_steps"] == 2
    assert counts["native_attempt_intents"] == 1 and counts["external_attempt_intents"] == 2
    assert counts["repair_attempt_intents"] == counts["retry_attempt_intents"] == 1
    assert counts["native_recovery_observations"] == 1
    assert counts["cancel_requests"] == counts["cancellation_confirmed_attempts"] == 1
    assert counts["failed_review_applications"] == counts["critical_review_failures"] == 1
    assert counts["conflicting_review_basis_groups"] == counts["budget_stop_decisions"] == 1
    assert counts["terminal_step_records"] == counts["reconciliation_steps"] == 1
    assert summary["snapshot_complete"] and summary["execution_closed"] and not summary["totals_complete"]
    assert all(value is None for value in summary["unobserved_measures"].values())
    purpose_page = build(records, policy=policy, after_sequence=4, max_records=1)
    assert purpose_page["trace"][0]["purpose"] == "repair"
    assert purpose_page["trace"][0]["model_ref"] == "fixture-model"
    assert purpose_page["activity_summary"] == summary
    changed = copy.deepcopy(value)
    changed["activity_summary"]["unobserved_measures"]["repaired_defects"] = 999
    with pytest.raises(EvidenceWikiError):
        reporting.validate_report(changed)


def test_record_gap_keeps_activity_as_retained_counts_and_never_complete_totals():
    original = rows([("execution_native", native_intent()), ("execution_advance", {}),
        ("execution_native_observation", native_return(recovery=True))])
    value = build([original[0], original[2]], max_records=1)
    assert value["missing_sequences"] == [1]
    assert value["activity_summary"]["counts"]["native_recovery_observations"] == 1
    assert not value["activity_summary"]["snapshot_complete"] and not value["activity_summary"]["totals_complete"]
    tampered = copy.deepcopy(original)
    tampered[1]["previous_record_ref"] = "sha256:" + "f" * 64
    with pytest.raises(EvidenceWikiError):
        build(tampered, max_records=1)
