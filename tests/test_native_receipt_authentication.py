"""Historical native receipts preserve current observations; host calls expire them."""

from dataclasses import replace
from threading import Event
from types import SimpleNamespace

import pytest

from evidence_wiki import agent_execution, research_execution
from evidence_wiki.agent_execution import BoundedSubprocessExecutor, ExecutionAttempt, ExecutionRole, _digest
from evidence_wiki.execution_roles import ExecutionRoleRegistry, ExecutionRoleScope
from evidence_wiki.research_execution import ResearchExecution
from evidence_wiki.workflow_contracts import WorkflowContractError
from tests import test_strict_observation_scope as strict_scope
from tests.test_agent_execution import attempt, executor
from tests.test_execution_role_binding import scoped

observation = strict_scope.observation
register = strict_scope.register


def execution(host):
    selected = ResearchExecution.__new__(ResearchExecution)
    selected._executor, selected._profile = host, host.profile
    return selected


def receipt(tmp_path):
    host = executor(tmp_path, "raise AssertionError('receipt verification must not dispatch')")
    selected = attempt(host.profile)
    observed = host._unavailable(selected, "dispatch_not_observed")
    assert host._attempts == {}
    return host, selected, observed


def test_native_receipt_preserves_registered_current_scope_through_closing(tmp_path, observation):
    host, selected, observed = receipt(tmp_path)
    scope, config, _, view, _, closes = observation
    register(observation)
    assert execution(host).authenticate(observed, selected) == observed
    scope.publication(scope.root, config, scope.policy, view, "artifact_checked")
    scope.close()
    assert len(closes) == 2
    assert host._attempts == {}


def test_scoped_native_evaluation_receipt_preserves_current_scope(tmp_path, observation):
    profile, old, job = scoped()
    role = ExecutionRole("researcher", "evaluator", "registered-controller", "local-process", "prompt/v1")
    worker_scope = ExecutionRoleScope("researcher", "evaluator", ["model"])
    profile = replace(profile, roles=(role,), role_scopes=ExecutionRoleRegistry([worker_scope]))
    supplied = {"purpose": "evaluate", "packet": {"schema_version": "research-review-context/v2",
        "context_id": job["context_id"], "task_role": "evaluator"}}
    job.update(operation="evaluate", role="evaluator", task_role="evaluator", role_scope_sha256=worker_scope.sha256,
        packet_sha256=_digest(supplied), payload={"claim_id": "claim-1", "packet_id": "packet-1"}, original_text=None)
    selected = ExecutionAttempt.create(attempt_id=old.attempt_id, nonce=old.nonce, run_id=old.run_id,
        binding_id=old.binding_id, profile=profile, config_ref=old.config_ref, job=job, input_document=supplied)
    host = BoundedSubprocessExecutor(profile, commands={}, cwd=tmp_path)
    observed = host._unavailable(selected, "dispatch_not_observed")
    scope, config, _, view, _, closes = observation
    register(observation)
    assert execution(host).authenticate(observed, selected) == observed
    scope.publication(scope.root, config, scope.policy, view, "artifact_checked")
    scope.close()
    assert len(closes) == 2 and host._attempts == {}


@pytest.mark.parametrize("changed", ["hmac", "nonce", "input", "profile"])
def test_native_receipt_still_requires_signature_and_exact_attempt_correlation(tmp_path, changed):
    host, selected, observed = receipt(tmp_path)
    if changed == "hmac":
        observed["reason"] = "changed"
    elif changed == "nonce":
        selected = replace(selected, nonce="other-nonce")
    elif changed == "input":
        selected = replace(selected, _input_bytes=b'{"packet":"other-evidence"}')
    else:
        selected = replace(selected, profile_sha256="0" * 64)
    with pytest.raises(WorkflowContractError) as error:
        execution(host).authenticate(observed, selected)
    assert error.value.error_code == "RESEARCH_EXECUTION_OBSERVATION_UNAUTHENTICATED"


@pytest.mark.parametrize("changed", ["adapter", "subclass", "authenticate", "check", "class_authenticate", "profile", "profile_descriptor"])
def test_external_or_replaced_verifier_invalidates_current_scope(tmp_path, observation, monkeypatch, changed):
    host, selected, observed = receipt(tmp_path)
    original_authenticate, original_check = host.authenticate, host._check
    calls = []

    def authenticate(value, pending):
        calls.append("authenticate")
        return original_authenticate(value, pending)

    if changed == "adapter":
        host = SimpleNamespace(profile=host.profile, authenticate=authenticate)
    elif changed == "subclass":
        class Adapter(BoundedSubprocessExecutor):
            pass
        host.__class__ = Adapter
    elif changed == "authenticate":
        monkeypatch.setattr(host, "authenticate", authenticate)
    elif changed == "check":
        def check(pending):
            calls.append("check")
            return original_check(pending)
        monkeypatch.setattr(host, "_check", check)
    elif changed == "class_authenticate":
        monkeypatch.setattr(BoundedSubprocessExecutor, "authenticate", lambda self, value, pending: authenticate(value, pending))
    elif changed == "profile":
        original_role = type(host.profile).role
        monkeypatch.setattr(type(host.profile), "role", lambda self, config: original_role(self, config))
    else:
        original_role = type(host.profile).role

        def role_descriptor(profile):
            calls.append(observation[0].valid)
            return lambda config: original_role(profile, config)

        monkeypatch.setattr(type(host.profile), "role", property(role_descriptor))
    scope, config, _, view, _, _ = observation
    register(observation)
    assert execution(host).authenticate(observed, selected) == observed
    with pytest.raises(WorkflowContractError) as error:
        scope.publication(scope.root, config, scope.policy, view, "artifact_checked")
    assert error.value.error_code == "RESEARCH_STRICT_OBSERVATION_CHANGED"
    if changed in {"adapter", "authenticate", "check", "class_authenticate"}:
        assert calls
    elif changed == "profile_descriptor":
        assert calls and all(valid is False for valid in calls)


def test_foreign_receipt_value_can_execute_only_after_scope_invalidation(tmp_path, observation):
    host, selected, observed = receipt(tmp_path)
    scope = observation[0]
    register(observation)
    calls = []

    class ForeignVersion:
        def __ne__(self, other):
            calls.append(scope.valid)
            return False

    observed["schema_version"] = ForeignVersion()
    with pytest.raises(WorkflowContractError) as error:
        execution(host).authenticate(observed, selected)
    assert error.value.error_code == "RESEARCH_EXECUTION_OBSERVATION_UNAUTHENTICATED"
    assert calls == [False]


def test_blocking_adapter_retains_bounded_wait_and_sticky_invalidation(tmp_path, observation):
    host, selected, observed = receipt(tmp_path)
    entered, release = Event(), Event()

    def blocking(value, pending):
        entered.set()
        release.wait(1)
        return value

    host = SimpleNamespace(profile=replace(host.profile, cleanup_timeout_seconds=0.01), authenticate=blocking)
    scope = observation[0]
    register(observation)
    try:
        with pytest.raises(WorkflowContractError) as error:
            execution(host).authenticate(observed, selected)
        assert error.value.error_code == "RESEARCH_EXECUTION_OBSERVATION_UNAUTHENTICATED"
        assert entered.is_set() and not release.is_set()
        assert not scope.valid
    finally:
        release.set()


@pytest.mark.parametrize("deleted", ["check", "profile_role"])
def test_deleted_native_validation_method_maps_to_authentication_refusal(tmp_path, observation, monkeypatch, deleted):
    host, selected, observed = receipt(tmp_path)
    register(observation)
    if deleted == "check":
        monkeypatch.delattr(BoundedSubprocessExecutor, "_check")
    else:
        monkeypatch.delattr(type(host.profile), "role")
    with pytest.raises(WorkflowContractError) as error:
        execution(host).authenticate(observed, selected)
    assert error.value.error_code == "RESEARCH_EXECUTION_OBSERVATION_UNAUTHENTICATED"
    assert not observation[0].valid


@pytest.mark.parametrize("oversized", ["key", "aggregate_values", "aggregate_keys", "integer"])
def test_large_receipt_routes_through_bounded_call_before_serialization(tmp_path, observation, monkeypatch, oversized):
    host, selected, observed = receipt(tmp_path)
    limit = agent_execution.MAX_BYTES
    if oversized == "key":
        payload = {"k" * (limit + 1): 1}
    elif oversized == "aggregate_values":
        payload = {"rows": ["v" * (limit // 2)] * 3}
    elif oversized == "aggregate_keys":
        payload = {str(index) + "k" * (limit // 2): index for index in range(3)}
    else:
        payload = {"value": 1 << 256}
    observed["output"] = payload
    scope = observation[0]
    register(observation)
    calls, serialized = [], []
    bounded, encode = research_execution.bounded_host_call, agent_execution._json

    def bounded_call(function, arguments, timeout):
        calls.append(function)
        return bounded(function, arguments, timeout)

    def checked_encoding(value):
        serialized.append(scope.valid)
        return encode(value)

    monkeypatch.setattr(research_execution, "bounded_host_call", bounded_call)
    monkeypatch.setattr(agent_execution, "_json", checked_encoding)
    with pytest.raises(WorkflowContractError) as error:
        execution(host).authenticate(observed, selected)
    assert error.value.error_code == "RESEARCH_EXECUTION_OBSERVATION_UNAUTHENTICATED"
    assert len(calls) == 1 and serialized and all(valid is False for valid in serialized)
    assert not scope.valid
