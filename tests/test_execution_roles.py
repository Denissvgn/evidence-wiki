"""Exact task scope selection and denial of role or capability escalation."""

from dataclasses import FrozenInstanceError

import pytest

from evidence_wiki.agent_execution import ExecutionError
from evidence_wiki.execution_roles import TASK_ROLES, ExecutionRoleRegistry, ExecutionRoleScope, isolation_report


@pytest.mark.parametrize("task_role", TASK_ROLES)
def test_task_roles_preserve_native_authority_and_only_propose(task_role):
    scope = ExecutionRoleScope("configured-" + task_role, task_role, ["model"])
    assert scope.authority_role == ("evaluator" if task_role in {"evaluator", "critic", "adjudicator"} else "host_worker")
    job = {"task_role": task_role, "role": scope.authority_role, "role_scope_sha256": scope.sha256,
           "required_capabilities": ["model"], "operation": "evaluate" if scope.authority_role == "evaluator" else "generate"}
    scope.check_job(job)
    for forbidden in ("review.sign", "human-review", "source.publish", "grant.create"):
        with pytest.raises(ExecutionError, match="capability_refused"):
            scope.require(forbidden)
    with pytest.raises(FrozenInstanceError):
        scope.task_role = "evaluator"


def test_scope_inputs_are_frozen_normalized_and_independent_of_names():
    capabilities = ["plan.propose", "model"]
    scope = ExecutionRoleScope("planner", "planner", capabilities)
    capabilities.append("verdict.propose")
    assert scope.document()["capabilities"] == ["model", "plan.propose"]
    assert scope.sha256 == ExecutionRoleScope("planner", "planner", ["model", "plan.propose"]).sha256
    document = scope.document()
    document["capabilities"].append("review.sign")
    assert "review.sign" not in scope.capabilities
    registry = ExecutionRoleRegistry([scope, ExecutionRoleScope("second-name", "planner", ["model"])])
    with pytest.raises(ExecutionError, match="scope_ambiguous"):
        registry.select("planner")
    assert registry.select("planner", config_ref="planner") == scope
    with pytest.raises(ExecutionError, match="scope_mismatch"):
        registry.select("evaluator", config_ref="planner")


@pytest.mark.parametrize("changes", [{"task_role": "human-review"}, {"task_role": []},
    {"config_ref": "bad ref"}, {"capabilities": ["model", "model"]}, {"capabilities": "model"},
    {"capabilities": ["verdict.propose"]}, {"capabilities": [None]}])
def test_scope_rejects_privilege_changes_and_invalid_configuration(changes):
    with pytest.raises(ExecutionError):
        ExecutionRoleScope(**(dict(config_ref="planner", task_role="planner", capabilities=["model"]) | changes))


@pytest.mark.parametrize("change", [{"role": "evaluator"}, {"task_role": "evaluator"},
    {"role_scope_sha256": "0" * 64}, {"required_capabilities": ["model", "verdict.propose"]},
    {"required_capabilities": ["model", "model"]}, {"required_capabilities": [None]},
    {"operation": "deliver"}, {"operation": []}, {"required_capabilities": []}, {"operation": "probe"}])
def test_job_cannot_relabel_generator_or_exercise_review_and_delivery_authority(change):
    scope = ExecutionRoleScope("researcher", "researcher", ["model", "evidence.read", "claim.propose"])
    job = {"task_role": "researcher", "role": "host_worker", "role_scope_sha256": scope.sha256,
           "required_capabilities": ["model"], "operation": "generate"}
    with pytest.raises(ExecutionError):
        scope.check_job(job | change)


def test_registry_is_bounded_exact_and_has_no_implicit_default():
    first = ExecutionRoleScope("evidence", "evaluator", ["model", "review.read", "verdict.propose"])
    supplied = [first]
    registry = ExecutionRoleRegistry(supplied)
    supplied.clear()
    assert registry.select("evaluator") == first
    with pytest.raises(ExecutionError, match="scope_unavailable"):
        registry.scope("Evidence")
    with pytest.raises(ExecutionError, match="scope_unavailable"):
        registry.select("critic")
    with pytest.raises(ExecutionError, match="registry_duplicate"):
        ExecutionRoleRegistry([first, first])
    with pytest.raises(ExecutionError, match="registry_invalid"):
        ExecutionRoleRegistry([first] * 33)
    with pytest.raises(ExecutionError, match="registry_invalid"):
        ExecutionRoleRegistry([])


def test_only_integration_can_select_an_explicit_offline_probe():
    scope = ExecutionRoleScope("integrator", "integration", ["offline_check", "model"])
    job = {"task_role": "integration", "role": "host_worker", "role_scope_sha256": scope.sha256,
           "required_capabilities": ["offline_check"], "operation": "probe"}
    scope.check_job(job)
    with pytest.raises(ExecutionError, match="capability_refused"):
        scope.check_job(job | {"required_capabilities": []})


def test_observed_model_refs_and_controller_labels_do_not_certify_isolation():
    from datetime import datetime, timedelta, timezone

    from evidence_wiki.agent_execution import DeterministicOfflineExecutor, ExecutionAttempt, ExecutionRole
    from tests.test_agent_execution import profile
    from tests.test_workflow_contracts import job

    roles = (ExecutionRole("a", "evaluator", "controller-a", "offline/model-a", "review/v1"),
             ExecutionRole("b", "evaluator", "controller-b", "offline/model-b", "review/v1"))
    selected = profile(roles=roles)
    host = DeterministicOfflineExecutor(selected, outputs={"a": {}, "b": {}}, receipt_key=b"r" * 32)
    observations = []
    for index, role in enumerate(roles):
        now = datetime.now(timezone.utc)
        supplied = job("evaluate")
        supplied.update(controller_ref=role.controller_ref, prompt_version=role.prompt_version,
            issued_utc=now.isoformat(), deadline_utc=(now + timedelta(seconds=10)).isoformat(), timeout_seconds=5)
        value = ExecutionAttempt.create(attempt_id=f"attempt-{index}", nonce=f"nonce-{index}", run_id="run",
            binding_id="binding", profile=selected, config_ref=role.config_ref, job=supplied)
        observations.append(host.authenticate(host.invoke(value), value))
    report = isolation_report(roles, authenticated_observations=observations, generator_controller_ref="generator")
    assert report["control"]["authenticated_controller_refs_distinct"] is True
    assert report["control"]["credential_isolation"] == "unknown"
    assert report["context"]["host_session_isolation"] == "unknown"
    assert report["model"]["distinct_model_refs"] is True
    assert report["model"]["model_family_diversity"] == "unknown"
    assert report["model"]["live_model_execution"] == "unknown"
    assert report["model"]["semantic_qualification"] == "unknown"
    assert not report["authorized"] and not report["evidence_accepted"]
    none = isolation_report(roles)
    assert none["control"]["authenticated_controller_refs_distinct"] is None
    assert none["model"]["distinct_model_refs"] is None
    forged = dict(observations[0], controller_ref="other")
    with pytest.raises(ExecutionError, match="identity_mismatch"):
        isolation_report(roles, authenticated_observations=[forged])
