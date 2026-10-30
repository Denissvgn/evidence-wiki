"""Inert approved service routes cannot turn unknown work into failover authority."""

from copy import deepcopy

import pytest

from evidence_wiki.agent_execution import ExecutionError, ExecutionRoleLimit
from evidence_wiki.review_service_policy import ReviewServicePlan, ReviewServiceRoute, service_action


def plan():
    return ReviewServicePlan((ReviewServiceRoute("evidence", "evaluator", ("evidence", "evidence-backup"),
        ExecutionRoleLimit("evaluator", "evidence-controller", 2, 2, 2000, "0", 120)),))


def failed(**changes):
    return {"config_ref": "evidence", "state": "unavailable", "reservation_occupied": False,
        "authenticated": True, "final_accounting": True, "reason": "executor_unavailable", **changes}


def test_frozen_roundtrip_preserves_exact_explicit_role_budget():
    value = plan()
    assert ReviewServicePlan.restore(value.document()) == value
    mutable = value.document()
    mutable["routes"][0]["config_refs"].append("unapproved")
    assert value.route("evidence").config_refs == ("evidence", "evidence-backup")


@pytest.mark.parametrize("change", [
    {"reservation_occupied": True}, {"authenticated": False}, {"final_accounting": False},
    {"state": "running"}, {"state": "unknown"}, {"reason": "deadline_unknown"},
    {"reason": "semantic_verdict_failed"}, {"reason": "output_invalid"},
])
def test_no_unknown_or_semantic_failure_gets_another_service(change):
    assert service_action(plan(), "evidence", [failed(**change)], ["evidence-backup"])["action"] == "blocked"


def test_known_settled_unavailable_uses_actual_declared_target_once():
    value = service_action(plan(), "evidence", [failed()], ["evidence-backup"])
    assert value == {"action": "select", "reason": "approved_service_route", "config_ref": "evidence-backup"}
    history = [failed(), failed(config_ref="evidence-backup")]
    assert service_action(plan(), "evidence", history, ["evidence-backup"])["reason"] == "approved_services_exhausted"


def test_success_is_not_retried_for_another_vote():
    assert service_action(plan(), "evidence", [failed(state="succeeded", reason="completed")],
        ["evidence-backup"])["action"] == "completed"


def test_unapproved_available_names_never_select_a_service():
    with pytest.raises(ExecutionError):
        service_action(plan(), "evidence", [], ["foreign-controller"])


@pytest.mark.parametrize("value", [True, 1.5, -1])
def test_counts_and_time_cannot_be_float_bool_or_negative(value):
    with pytest.raises(ExecutionError):
        ExecutionRoleLimit("evaluator", "owner", value, 1, 100, "0", 120)
    with pytest.raises(ExecutionError):
        ExecutionRoleLimit("evaluator", "owner", 1, 1, 100, "0", value)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", 0.0])
def test_cost_is_bounded_decimal_text(value):
    with pytest.raises(ExecutionError):
        ExecutionRoleLimit("evaluator", "owner", 1, 1, 100, value, 120)


def test_routes_cannot_share_an_actual_config_or_change_task_role():
    with pytest.raises(ExecutionError):
        ReviewServicePlan((plan().routes[0], ReviewServiceRoute("critic", "critic", ("critic", "evidence-backup"),
            ExecutionRoleLimit("evaluator", "critic-controller", 2, 2, 2000, "0", 120))))
    with pytest.raises(ExecutionError):
        ReviewServiceRoute("evidence", "generator", ("evidence",), plan().routes[0].limit)


@pytest.mark.parametrize("change", ["unknown", "continuation", "limit", "extra-config", "version"])
def test_closed_versioned_plan_refuses_silent_policy_extension(change):
    value = deepcopy(plan().document())
    if change == "unknown":
        value["approved"] = True
    elif change == "continuation":
        value["unknown_external_work"] = "retry"
    elif change == "limit":
        value["routes"][0]["limit"]["budgets"]["model_calls"] = True
    elif change == "extra-config":
        value["routes"][0]["config_refs"].append("evidence-backup")
    else:
        value["schema_version"] = "research-review-service-plan/v999"
    with pytest.raises(ExecutionError):
        ReviewServicePlan.restore(value)
