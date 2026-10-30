"""Exact host controller and immutable scoped dispatch correlation."""

import copy
from dataclasses import replace

import pytest

from evidence_wiki import ExecutionRoleRegistry, ExecutionRoleScope
from evidence_wiki.agent_execution import ExecutionAttempt, ExecutionError, _digest, _json
from evidence_wiki.errors import ConfigError
from evidence_wiki.host_context import HostContext, controller_allowed
from evidence_wiki.workflow_contracts import (
    EXECUTION_OBSERVATION,
    JOB_V2,
    JOB_V3,
    WorkflowContractError,
    validate_document,
)
from tests.test_agent_execution import attempt, profile


def scoped():
    registry = ExecutionRoleRegistry((ExecutionRoleScope("researcher", "researcher", ("model",)),))
    selected = profile(role_scopes=registry)
    old = attempt()
    job = old.job | {"schema_version": JOB_V3, "task_role": "researcher",
        "role_scope_sha256": registry.scope("researcher").sha256, "context_id": "fresh-context",
        "packet_sha256": old.input_sha256, "required_capabilities": ["model"], "expected_result_schema": EXECUTION_OBSERVATION}
    return selected, old, job


def create(selected, old, job):
    return ExecutionAttempt.create(attempt_id=old.attempt_id, nonce=old.nonce, run_id=old.run_id,
        binding_id=old.binding_id, profile=selected, config_ref=old.config_ref,
        job=job, input_document=old.input_document)


def test_scoped_profile_preserves_legacy_documents_and_requires_closed_job_version():
    selected, old, job = scoped()
    assert selected.document()["schema_version"] == "research-execution-profile/v3"
    assert profile().document()["schema_version"] == "research-execution-profile/v1"
    value = create(selected, old, job)
    assert value.input_sha256 == job["packet_sha256"]
    with pytest.raises(ExecutionError, match="scope_required"):
        create(selected, old, old.job)
    with pytest.raises(ExecutionError, match="scope_unavailable"):
        create(profile(), old, job)
    with pytest.raises(WorkflowContractError):
        validate_document(JOB_V2, job | {"schema_version": JOB_V2})


@pytest.mark.parametrize("changes", [{"task_role": "evaluator"}, {"role_scope_sha256": "0" * 64},
    {"packet_sha256": "0" * 64}, {"required_capabilities": []}, {"required_capabilities": ["review.sign"]}])
def test_constructing_an_attempt_cannot_bypass_exact_scope_or_packet_binding(changes):
    selected, old, job = scoped()
    with pytest.raises(ExecutionError):
        create(selected, old, job | changes)


def test_reference_host_rechecks_scope_even_for_directly_constructed_attempt(tmp_path):
    from evidence_wiki.agent_execution import BoundedSubprocessExecutor
    from tests._usage_fixture import require_reference_executor

    require_reference_executor()

    selected, old, job = scoped()
    forged = job | {"task_role": "release"}
    value = ExecutionAttempt(old.attempt_id, old.nonce, old.run_id, old.binding_id,
        selected.sha256, old.config_ref, _json(forged), _json(old.input_document))
    host = BoundedSubprocessExecutor(selected, commands={}, cwd=tmp_path, receipt_key=b"r" * 32)
    with pytest.raises(ExecutionError, match="scope_mismatch"):
        host.poll(value)


def test_exact_controller_mappings_fail_closed_and_preserve_legacy_selection(tmp_path):
    rows = [["worker", "host_worker", "generator"], ["evidence", "evaluator", "judge-a"],
        ["counter", "evaluator", "judge-b"]]
    context = HostContext([tmp_path], [], "host", controller_refs={"evaluator": "legacy"},
        role_controller_refs=rows)
    rows.clear()
    assert context.controller_for("evaluator", "evidence") == "judge-a"
    assert context.controller_for("evaluator", "counter") == "judge-b"
    assert context.controller_for("evaluator", "missing") is None
    assert context.controller_for("evaluator", "worker") is None
    with context.activate():
        assert controller_allowed("evaluator", "judge-a") and controller_allowed("evaluator", "judge-b")
        assert not controller_allowed("evaluator", "legacy")
        assert not controller_allowed("evaluator", "generator")
    legacy = HostContext([tmp_path], [], "host", controller_refs={"evaluator": "legacy"})
    assert legacy.controller_for("evaluator", "arbitrary-config") == "legacy"


@pytest.mark.parametrize("rows", [[["same", "evaluator", "a"], ["same", "host_worker", "b"]],
    [["worker", "human-review", "generator"]], [["worker", [], "generator"]], [["worker", "evaluator"]]])
def test_controller_scope_rejects_ambiguous_or_ungranted_roles(tmp_path, rows):
    with pytest.raises(ConfigError):
        HostContext([tmp_path], [], "host", role_controller_refs=rows)


def test_input_packet_context_identity_cannot_be_swapped_after_host_selection():
    selected, old, job = scoped()
    data = copy.deepcopy(old.input_document)
    data["packet"] = {"schema_version": "research-review-context/v1", "context_id": "other-context",
        "task_role": "researcher"}
    other = replace(old, _input_bytes=_json(data))
    with pytest.raises(ExecutionError, match="context_mismatch"):
        create(selected, other, job | {"packet_sha256": _digest(data)})
