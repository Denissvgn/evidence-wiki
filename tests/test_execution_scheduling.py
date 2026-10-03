"""Durable role allowances and bounded continuation from authenticated history."""

import copy
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from evidence_wiki import ExecutionProfile, ExecutionRole, ExecutionRoleLimit, ExecutionSchedule
from evidence_wiki import execution_scheduling as scheduling
from evidence_wiki.errors import EvidenceWikiError
from tests import test_execution_accounting as measured


def profile(*, backoff=2, cost="1", model_calls=3):
    return ExecutionProfile("scheduled", 100, "USD", "meter-rate-v1", (
        ExecutionRole("primary", "host_worker", "generator", "model-a", "prompt-a"),
        ExecutionRole("fallback", "host_worker", "generator", "model-b", "prompt-b"),
        ExecutionRole("judge", "evaluator", "reviewer", "judge-model", "review/v1")),
        1, 1, 100, "1", 0.1, ExecutionSchedule((
            ExecutionRoleLimit("host_worker", "generator", model_calls, 10, 1000, cost, 60),
            ExecutionRoleLimit("evaluator", "reviewer", 10, 10, 1000, "10", 60)),
            ("worker_failed", "executor_unavailable"), backoff, (("primary", "fallback"),)))


def intent(identifier, config="primary", purpose="generate"):
    return {"kind": "execution_attempt", "data": {"attempt": {"attempt_id": identifier,
        "config_ref": config, "job": {"operation": "generate" if config != "judge" else "evaluate"}},
        "purpose": purpose, "original_id": "original", "claim_id": "claim"}}


def final_record(identifier, *, state="failed", reason="worker_failed", final=True, at=None):
    return {"kind": "execution_observation", "data": {"attempt_id": identifier,
        "observation": {"state": state, "reason": reason, "final_accounting": final,
            "observed_utc": at or "2026-10-04T00:00:00Z"}}}


def pending(identifier="a", **changes):
    return {"attempt_id": identifier, "reservation_occupied": False, "cancellation_requested": False, **changes}


def test_fallback_configs_share_one_exact_usage_and_reservation_bucket():
    plan = measured.opened()
    records = [intent("a"), intent("b", "fallback"), intent("c", "judge")]
    for identifier in ("a", "b", "c"):
        plan = measured.accounting.reserve_attempt(plan, measured.attempt(identifier))
    plan = measured.record(plan, measured.observation("a-final", attempt_id="a",
        values=measured.usage(seconds="1", models=1, tools=1, tokens=20, cost="0.000000000001")))
    before = copy.deepcopy(plan)
    projected = scheduling.role_projection(profile(), plan, records, "fallback")
    assert projected["spent"]["model_calls"] == 1
    assert projected["outstanding"]["model_calls"] == 2
    assert projected["remaining"]["model_calls"] == 0
    assert projected["spent"]["cost"] == "0.000000000001"
    assert projected["incomplete_attempt_ids"] == ["b"]
    assert scheduling.role_projection(profile(), plan, records, "primary") == projected
    assert scheduling.role_projection(profile(), plan, records, "judge")["spent"]["model_calls"] == 0
    assert plan == before


def test_role_overrun_and_missing_history_never_disappear_at_restart():
    plan = measured.record(measured.reserved(), measured.observation(values=measured.usage(models=5, cost="2")))
    records = [intent("attempt")]
    assert scheduling.role_projection(profile(), copy.deepcopy(plan), copy.deepcopy(records), "fallback")["overrun"]
    with pytest.raises(EvidenceWikiError) as error:
        scheduling.role_projection(profile(), plan, [], "primary")
    assert error.value.error_code.endswith("SCHEDULING_HISTORY_INVALID")


def test_continuation_uses_first_final_time_and_records_actual_fallback_identity():
    records = [intent("a"), final_record("a"), final_record("a", at="2026-10-04T00:10:00Z")]
    action = scheduling.retry_work(profile(), records, pending())
    assert action["config_ref"] == "fallback" and action["purpose"] == "retry"
    assert action["retry_after_utc"] == "2026-10-04T00:00:02+00:00"
    assert action["predecessor_attempt_id"] == "a"
    assert scheduling.retry_work(profile(), copy.deepcopy(records), pending()) == action


@pytest.mark.parametrize("altered", [
    {"state": "unknown"}, {"state": "running"}, {"state": "cancelled"},
    {"reason": "output_invalid"}, {"final": False}])
def test_uncertain_or_unpermitted_outcome_cannot_supply_retry(altered):
    records = [intent("a"), final_record("a", **altered)]
    assert scheduling.retry_work(profile(), records, pending()) is None


@pytest.mark.parametrize("flags", [{"reservation_occupied": True}, {"cancellation_requested": True}])
def test_occupied_or_cancelled_attempt_cannot_supply_fallback(flags):
    assert scheduling.retry_work(profile(), [intent("a"), final_record("a")], pending(**flags)) is None


def test_retry_preserves_repair_intent_and_legacy_profile_is_inert():
    records = [intent("a", purpose="retry"), final_record("a")]
    records[0]["data"]["retry_purpose"] = "repair"
    assert scheduling.retry_work(profile(), records, pending())["operation"] == "repair"
    assert scheduling.retry_work(replace(profile(), schedule=None), records, pending()) is None


def test_known_predispatch_unavailable_settles_without_fabricating_dispatch():
    from evidence_wiki import research_execution

    owner = SimpleNamespace(sign=lambda value: value)
    attempt = SimpleNamespace(attempt_id="a", binding_id="binding")
    observed = {"state": "unavailable", "final_accounting": True, "sequence": 1,
        "observed_utc": datetime.now(timezone.utc).isoformat(), "currency": "USD", "pricing_ref": "rate",
        "observed_usage": {"seconds": 0, "model_calls": 0, "tool_calls": 0, "tokens": 0, "cost": "0"}}
    value = research_execution.ResearchExecution.accounting_observation(owner, attempt, observed, authenticated=True)
    assert value["state"] == "failed" and value["final_usage"]
    assert value["usage"]["model_calls"] == value["usage"]["tool_calls"] == 0


def test_public_failed_worker_falls_back_under_the_same_canonical_run(tmp_path, monkeypatch):
    from evidence_wiki import Workspace
    from tests.test_execution_session import fixture

    root, policy, execution, context = fixture.__wrapped__(tmp_path, monkeypatch,
        SimpleNamespace(param={"worker_failover": True}))
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="fallback", execution=execution) as session:
            before = session.current()["basis_id"]
            result = session.run_until_terminal(request_id="drive", expected_basis_id=before, max_steps=64)
            assert result["outcome"] == "accepted" and result["terminal"]
            records = execution.records(session)
            attempts = [row["data"] for row in records if row["kind"] == "execution_attempt"]
            workers = [row for row in attempts if row["attempt"]["config_ref"] != "profile-a"]
            assert [row["attempt"]["config_ref"] for row in workers] == ["researcher", "backup-researcher"]
            assert workers[1]["predecessor_attempt_id"] == workers[0]["attempt"]["attempt_id"]
            assert workers[1]["attempt"]["nonce"] != workers[0]["attempt"]["nonce"]
            assert session.current()["usage"]["retry_attempts"] == 1
            assert session.report()["accounting"]["coverage"]["coverage_complete"]
            replay = session.run_until_terminal(request_id="drive", expected_basis_id=before, max_steps=64)
            assert replay["historical"] and replay["outcome"] == "accepted"
            assert len([row for row in execution.records(session) if row["kind"] == "execution_attempt"]) == len(attempts)
