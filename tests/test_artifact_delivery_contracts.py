"""Exact immutable delivery selections never grant native publication."""

from dataclasses import FrozenInstanceError, replace

import pytest

from evidence_wiki import ArtifactDeliveryPlan, ArtifactDestination, contract
from evidence_wiki.agent_execution import ExecutionError, ExecutionRole
from evidence_wiki.artifact_delivery_contracts import INPUT, PLAN, PROPOSAL, RESULT, checked, schemas
from evidence_wiki.errors import UsageError
from evidence_wiki.execution_roles import ExecutionRoleRegistry, ExecutionRoleScope
from tests.test_disagreement_configuration import simple_profile

SHA = "sha256:" + "a" * 64


def selection():
    base = simple_profile()
    plan = ArtifactDeliveryPlan("release", "release-controller", (ArtifactDestination("approved"),))
    selected = replace(base, roles=(*base.roles, ExecutionRole("release", "host_worker", "release-controller", "selected-model", "release/v1")),
        role_scopes=ExecutionRoleRegistry((*base.role_scopes.scopes,
            ExecutionRoleScope("release", "release", ("model", "eligibility.read", "release.propose")))), artifact_delivery=plan)
    return base, selected


def proposal(**changes):
    return {"schema_version": PROPOSAL, "artifact_id": SHA, "artifact_sha256": SHA,
        "artifact_bytes": 19, "destination_ref": "approved", "plan_sha256": SHA} | changes


def test_optional_plan_preserves_old_profile_and_pins_exact_host_selection():
    old, selected = selection()
    assert "artifact_delivery" not in old.document()
    assert old.document()["schema_version"] == "research-execution-profile/v3"
    assert selected.document()["schema_version"] == "research-execution-profile/v12"
    assert checked(PLAN, selected.artifact_delivery.document()) == selected.artifact_delivery.document()
    exported = selected.artifact_delivery.document()
    exported["destinations"][0]["filename"] = "altered.md"
    assert selected.artifact_delivery.destinations[0].filename == "brief.md"
    with pytest.raises(FrozenInstanceError):
        selected.artifact_delivery.destinations[0].filename = "changed.md"
    with pytest.raises(UsageError):
        selected.artifact_delivery.destination("unapproved")


@pytest.mark.parametrize("filename", ["../brief.md", "nested/brief.md", "brief.json", "CON.md", ".brief.md", "brief\n.md", "x" * 100 + ".md"])
def test_worker_paths_and_nonportable_leaves_cannot_be_destinations(filename):
    with pytest.raises(ExecutionError):
        ArtifactDestination("approved", filename)


@pytest.mark.parametrize("fields", [{"destinations": []}, {"destinations": [ArtifactDestination("same"), ArtifactDestination("same")]},
    {"destinations": [ArtifactDestination("a"), ArtifactDestination("b")]}, {"timeout_seconds": True}, {"timeout_seconds": 601},
    {"max_output_bytes": 1048577}, {"tokens": 0}, {"cost_limit": "0"}, {"model_calls": 2}])
def test_invalid_or_unbounded_frozen_selections_are_rejected(fields):
    with pytest.raises(ExecutionError):
        ArtifactDeliveryPlan(**({"config_ref": "release", "controller_ref": "controller",
            "destinations": (ArtifactDestination("approved"),)} | fields))


@pytest.mark.parametrize("fields", [{"approved": True}, {"output": "replacement"}, {"path": "/tmp/brief.md"},
    {"artifact_id": "sha256:" + "b" * 64}, {"artifact_bytes": 1048577}])
def test_proposals_cannot_edit_output_authority_or_identity(fields):
    with pytest.raises(UsageError):
        checked(PROPOSAL, proposal(**fields))


def test_proposal_and_result_dictionaries_are_inert_detached_shapes():
    original = proposal()
    returned = checked(PROPOSAL, original)
    returned["destination_ref"] = "changed"
    assert original["destination_ref"] == "approved"
    assert set(schemas()) == {PLAN, INPUT, PROPOSAL, RESULT}
    checked(RESULT, {"schema_version": RESULT, "request_id": "delivery", "status": "unavailable",
        "artifact": None, "destination_ref": None, "receipt_ref": None,
        "boundary": "not_observed", "current_eligibility": "unavailable", "historical": True,
        "reasons": ["retention_unavailable"], "authorized": False, "evidence_accepted": False})


def test_public_contract_exposes_checked_scope_and_detached_delivery_schemas():
    selected = contract()["research_artifact_delivery"]
    assert selected["schema_documents"] == schemas()
    assert selected["remote_delivery"] == "unsupported"
    assert selected["replacement"] == "never"
    selected["schema_documents"].clear()
    assert contract()["research_artifact_delivery"]["schema_documents"] == schemas()
