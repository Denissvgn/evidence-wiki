"""Private installation handoff reconstructs genuine validated native profiles."""

import json

import pytest

from evidence_wiki import (
    DisagreementPlan,
    ExecutionProfile,
    ExecutionRole,
    ExecutionRoleRegistry,
    ExecutionRoleScope,
    NativeInputHandoff,
    ReviewDelivery,
)
from tools._review_dependency_case_services import _construct_profile, _constructor_inputs


@pytest.mark.parametrize("context_version", ["complete", "dependencies"])
def test_profile_handoff_reconstructs_validated_native_objects(context_version):
    roles = (ExecutionRole("generator", "host_worker", "generator-controller", "simulated/generator", "generate/v1"),
        ExecutionRole("evidence", "evaluator", "evidence-controller", "simulated/evidence", "review/v1"),
        ExecutionRole("critic", "evaluator", "critic-controller", "simulated/critic", "review/v1"))
    scopes = ExecutionRoleRegistry(tuple(ExecutionRoleScope(role.config_ref, task, ("model",))
        for role, task in zip(roles, ("researcher", "evaluator", "critic"), strict=True)))
    original = ExecutionProfile("fixture-profile", 512, "EUR", "simulated-pricing/v1", roles,
        1, 1, 1000, "0", 1, role_scopes=scopes, required_diversity=("control", "context"),
        disagreement=DisagreementPlan(), input_handoff=NativeInputHandoff(),
        review_delivery=ReviewDelivery(context_version=context_version))
    wire = json.dumps(_constructor_inputs(original))
    restored = _construct_profile(json.loads(wire))
    assert type(restored) is ExecutionProfile
    assert restored is not original
    assert restored.document() == original.document()
    assert restored.sha256 == original.sha256
    assert type(restored.role_scopes) is ExecutionRoleRegistry
    assert type(restored.review_delivery) is ReviewDelivery
    assert type(restored.input_handoff) is NativeInputHandoff
    assert all(type(role) is ExecutionRole for role in restored.roles)


def test_profile_handoff_cannot_select_an_arbitrary_constructor():
    with pytest.raises(AssertionError, match="native_profile_constructor_unsupported"):
        _construct_profile({"constructor": "HostContext", "arguments": {}})


def test_profile_handoff_revalidates_nested_constructor_arguments():
    with pytest.raises(ValueError, match="execution_task_role_invalid"):
        _construct_profile({"constructor": "ExecutionRoleScope", "arguments": {
            "config_ref": "critic", "task_role": "administrator", "capabilities": ["model"]}})


def test_profile_handoff_rejects_owner_objects_as_inert_values():
    with pytest.raises(AssertionError, match="native_constructor_input_unsupported"):
        _constructor_inputs(object())
