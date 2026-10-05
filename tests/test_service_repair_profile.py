"""Real immutable profile construction preserves old route boundaries."""

from dataclasses import replace

import pytest

from evidence_wiki import ServiceRepairBinding
from evidence_wiki.agent_execution import ExecutionError, ExecutionRole, ExecutionRoleLimit
from evidence_wiki.disagreement_configuration import DisagreementPlan
from evidence_wiki.execution_roles import ExecutionRoleRegistry, ExecutionRoleScope
from evidence_wiki.review_service_policy import ReviewServicePlan, ReviewServiceRoute
from tests.test_disagreement_configuration import simple_profile


def configured():
    base = simple_profile()
    roles = (*base.roles, ExecutionRole("backup", "evaluator", "backup-controller", "backup-model", "review/v1"))
    registry = ExecutionRoleRegistry((*base.role_scopes.scopes, ExecutionRoleScope("backup", "evaluator", ("model",))))
    routes = ReviewServicePlan((
        ReviewServiceRoute("profile-a", "evaluator", ("profile-a", "backup"), ExecutionRoleLimit("evaluator", "controller-a", 2, 2, 10, "0", 120)),
        ReviewServiceRoute("critic", "critic", ("critic",), ExecutionRoleLimit("evaluator", "critic-controller", 2, 2, 10, "0", 120))))
    return replace(base, roles=roles, role_scopes=registry, review_services=routes)


def test_explicit_successor_and_old_boundary_are_distinct():
    old = configured()
    before = old.document()
    assert before["schema_version"] == "research-execution-profile/v8"
    plan = DisagreementPlan()
    with pytest.raises(ExecutionError, match="service_disagreement_alternates_unsupported"):
        replace(old, disagreement=plan)
    binding = ServiceRepairBinding(old.review_services.sha256, "sha256:" + plan.sha256)
    new = replace(old, disagreement=plan, service_repair=binding)
    assert new.document()["schema_version"] == "research-execution-profile/v9"
    assert old.document() == before
    assert new.review_services == old.review_services and new.disagreement == plan


@pytest.mark.parametrize("field", ["service_plan_sha256", "disagreement_plan_sha256"])
def test_different_frozen_plan_cannot_be_silently_selected(field):
    old = configured()
    plan = DisagreementPlan()
    fields = {"service_plan_sha256": old.review_services.sha256, "disagreement_plan_sha256": "sha256:" + plan.sha256}
    fields[field] = "sha256:" + "0" * 64
    with pytest.raises(ExecutionError, match="service_repair_plan_changed"):
        replace(old, disagreement=plan, service_repair=ServiceRepairBinding(**fields))


def test_actual_controller_scope_cannot_be_overridden_by_successor_label():
    old = configured()
    plan = DisagreementPlan()
    binding = ServiceRepairBinding(old.review_services.sha256, "sha256:" + plan.sha256)
    roles = tuple(replace(role, controller_ref="generator-controller") if role.config_ref == "backup" else role for role in old.roles)
    with pytest.raises(ExecutionError, match="service_controllers_not_distinct"):
        replace(old, roles=roles, disagreement=plan, service_repair=binding)
