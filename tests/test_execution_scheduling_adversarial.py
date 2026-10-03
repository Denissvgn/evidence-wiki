"""Independent continuation oracles for delayed receipts and shared allowances."""

import copy
from dataclasses import replace
from decimal import ROUND_UP, Inexact, localcontext
from fractions import Fraction

import pytest

from evidence_wiki import (
    DeterministicOfflineExecutor,
    ExecutionProfile,
    ExecutionRole,
    ExecutionRoleLimit,
    ExecutionSchedule,
)
from evidence_wiki.agent_execution import ExecutionError
from evidence_wiki.agent_qualification import QualificationAuthority, QualificationError
from evidence_wiki.execution_scheduling import retry_work, role_projection
from evidence_wiki.execution_session import _latest_observation, _pending
from evidence_wiki.run_policy import RunPolicy
from tests import test_execution_accounting as measured
from tests.test_run_policy import policy_document


def selected_profile(*, seconds=60, model_calls=10, cost="10", backoff=2):
    return ExecutionProfile("independent-host", 64, "USD", "meter-rate-v1", (
        ExecutionRole("primary", "host_worker", "generator", "model-primary", "prompt-a"),
        ExecutionRole("fallback", "host_worker", "generator", "model-fallback", "prompt-b")),
        1, 1, 100, "1", 0.1, ExecutionSchedule((ExecutionRoleLimit("host_worker", "generator",
            model_calls, 10, 1000, cost, seconds),), ("worker_failed",), backoff, (("primary", "fallback"),)))


def declared(identifier="a", config="primary", **data):
    return {"kind": "execution_attempt", "data": {"attempt": {"attempt_id": identifier, "config_ref": config,
        "job": {"id": "job-" + identifier, "operation": "generate", "deadline_utc": "2026-10-04T01:00:00+00:00"},
        "input": {}}, "original_id": "original", "purpose": "generate", **data}}


def receipt(sequence, *, state="running", final=False, identifier="a", timestamp="2026-10-04T00:00:00+00:00"):
    return {"kind": "execution_observation", "data": {"attempt_id": identifier,
        "observation": {"attempt_id": identifier, "sequence": sequence, "state": state,
            "reason": "worker_failed", "final_accounting": final, "observed_utc": timestamp,
            "cancellation_confirmed": state == "cancelled"}}}


def retry_pending(identifier="a"):
    return {"attempt_id": identifier, "reservation_occupied": False, "cancellation_requested": False}


@pytest.mark.parametrize("final_state", ["failed", "cancelled", "succeeded"])
def test_delayed_missing_receipt_cannot_replace_a_higher_logical_completion(final_state):
    records = [declared(), receipt(1), receipt(3, state=final_state, final=True), receipt(2)]
    before = copy.deepcopy(records)
    assert _latest_observation(records, "a")["sequence"] == 3
    pending = _pending(records)
    assert pending["state"] == final_state and not pending["reservation_occupied"]
    assert pending["cancellation_confirmed"] is (final_state == "cancelled")
    assert records == before


def test_repair_retry_preserves_detached_original_defects_across_multiple_failures():
    first = declared(purpose="repair")
    first["data"]["attempt"]["input"] = {"purpose": "repair", "defects": [
        {"claim_id": "claim", "checks": ["support"], "evidence_refs": ["retained-reference"]}]}
    continuation = retry_work(selected_profile(), [first, receipt(1, state="failed", final=True)], retry_pending())
    assert continuation["operation"] == continuation["retry_purpose"] == "repair"
    assert continuation["defects"] == first["data"]["attempt"]["input"]["defects"]
    continuation["defects"][0]["checks"].append("counterevidence")
    assert first["data"]["attempt"]["input"]["defects"][0]["checks"] == ["support"]
    second = declared("b", "fallback", purpose="retry", retry_purpose="repair")
    second["data"]["attempt"]["input"] = copy.deepcopy(first["data"]["attempt"]["input"])
    retry = retry_work(selected_profile(), [second, receipt(1, identifier="b", state="failed", final=True)], retry_pending("b"))
    assert retry["operation"] == "repair" and retry["defects"] == second["data"]["attempt"]["input"]["defects"]


def test_model_and_prompt_failover_cannot_reset_a_role_bucket_or_its_time_allowance():
    profile = selected_profile(seconds=5, model_calls=1)
    plan = measured.accounting.reserve_attempt(measured.opened(), measured.attempt("a"))
    plan = measured.record(plan, measured.observation("a-final", attempt_id="a",
        values=measured.usage(seconds="4.75", models=1, tools=1, tokens=10, cost="0.1")))
    records = [declared()]
    first = role_projection(profile, plan, records, "primary")
    second = role_projection(profile, copy.deepcopy(plan), copy.deepcopy(records), "fallback")
    assert first == second
    assert second["remaining"]["model_calls"] == 0 and second["remaining"]["seconds"] == "0.25"
    changed_controller = replace(profile.roles[1], controller_ref="other-generator")
    with pytest.raises(ExecutionError):
        replace(profile, roles=(profile.roles[0], changed_controller))


def test_lost_whole_attempt_and_missing_sequence_keep_reservations_occupied():
    plan = measured.accounting.reserve_attempt(measured.opened(), measured.attempt("a"))
    records = [declared()]
    lost = role_projection(selected_profile(), plan, records, "fallback")
    assert lost["missing_attempt_ids"] == ["a"] and lost["usage_unknown"]
    assert lost["outstanding"]["model_calls"] == 2
    plan = measured.record(plan, measured.observation("a-first", attempt_id="a", sequence=1,
        state="started", final=False, values=measured.usage(models=1, cost="0.1")))
    plan = measured.record(plan, measured.observation("a-third", attempt_id="a", sequence=3,
        values=measured.usage(models=1, cost="0.3")))
    missing = role_projection(selected_profile(), plan, records, "fallback")
    assert missing["missing_sequence_ranges"] == [{"attempt_id": "a", "first_sequence": 2, "last_sequence": 2}]
    assert missing["usage_unknown"] and missing["outstanding"]["cost"] == "1"
    assert missing["spent"]["cost"] == "0.3"
    receipts = records + [receipt(1), receipt(3, state="failed", final=True)]
    pending = _pending(receipts, missing)
    assert pending["reservation_occupied"]
    assert retry_work(selected_profile(), receipts, pending) is None
    assert _pending(receipts)["reservation_occupied"]
    plan = measured.record(plan, measured.observation("a-second", attempt_id="a", sequence=2,
        state="started", final=False, values=measured.usage(models=1, cost="0.2")))
    receipts.append(receipt(2))
    restored = role_projection(selected_profile(), plan, receipts, "fallback")
    pending = _pending(receipts, restored)
    assert pending["state"] == "failed" and not pending["reservation_occupied"]
    assert retry_work(selected_profile(), receipts, pending)["config_ref"] == "fallback"


def test_conflicting_final_receipts_cannot_create_new_role_allowance():
    plan = measured.accounting.reserve_attempt(measured.opened(), measured.attempt("a"))
    plan = measured.record(plan, measured.observation("first", attempt_id="a", values=measured.usage(cost="0.1")))
    plan = measured.record(plan, measured.observation("second", attempt_id="a", sequence=2, values=measured.usage(cost="0.2")))
    projected = role_projection(selected_profile(), plan, [declared()], "fallback")
    assert projected["usage_unknown"] and projected["conflicting_attempt_ids"] == ["a"]
    assert projected["outstanding"]["cost"] == "1" and projected["spent"]["cost"] == "0.2"
    receipts = [declared(), receipt(1, state="failed", final=True), receipt(2, state="failed", final=True)]
    pending = _pending(receipts, projected)
    assert pending["reservation_occupied"]
    assert retry_work(selected_profile(), receipts, pending) is None


def test_first_final_backoff_anchor_survives_restarted_objects_and_later_duplicate_observations():
    records = [declared(), receipt(1, state="failed", final=True),
        receipt(2, state="failed", final=True, timestamp="2026-10-04T00:10:00+00:00")]
    action = retry_work(selected_profile(backoff=30), records, retry_pending())
    restarted = retry_work(copy.deepcopy(selected_profile(backoff=30)), copy.deepcopy(records), retry_pending())
    assert action == restarted
    assert action["retry_after_utc"] == "2026-10-04T00:00:30+00:00"
    assert records[0]["data"]["attempt"]["job"]["deadline_utc"] == "2026-10-04T01:00:00+00:00"


def test_twelve_place_role_cost_uses_fraction_oracle_under_hostile_decimal_context():
    plan = measured.opened()
    amounts = ["0.000000000001", "0.000000000002"]
    for identifier, amount in zip(("a", "b"), amounts, strict=True):
        plan = measured.accounting.reserve_attempt(plan, measured.attempt(identifier))
        plan = measured.record(plan, measured.observation(identifier, attempt_id=identifier,
            values=measured.usage(cost=amount)))
    with localcontext() as context:
        context.prec = 1
        context.rounding = ROUND_UP
        context.traps[Inexact] = True
        result = role_projection(selected_profile(cost="0.000000000004"), plan,
            [declared("a", "primary"), declared("b", "fallback")], "fallback")
    assert Fraction(result["spent"]["cost"]) == sum(map(Fraction, amounts))
    assert result["remaining"]["cost"] == "0.000000000001"


def test_offline_claimed_qualification_and_authorization_cannot_qualify_semantic_release():
    profile = ExecutionProfile("offline-only", 4, "USD", "offline/v1", (
        ExecutionRole("profile-a", "evaluator", "controller-a", "offline/fixture", "prompt/v1"),), 0, 0, 0, "0")
    executor = DeterministicOfflineExecutor(profile, outputs={"profile-a": {"authorized": True, "qualified": True}},
        receipt_key=b"o" * 32)
    authority = QualificationAuthority("authority", b"q" * 32, "generator")
    with pytest.raises(QualificationError, match="qualification_offline_fixture_unsupported"):
        authority.issue(RunPolicy(policy_document()), profile, "profile-a", {}, [], [], executor,
            expires_utc="2027-01-01T00:00:00Z")


@pytest.mark.parametrize("state", ["running", "unknown", "cancelled", "succeeded"])
def test_only_observed_retryable_failures_can_propose_continuation(state):
    records = [declared(), receipt(1, state=state, final=state not in {"running", "unknown"})]
    assert retry_work(selected_profile(), records, retry_pending()) is None
