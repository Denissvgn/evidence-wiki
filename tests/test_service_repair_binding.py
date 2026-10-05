"""Immutable declarations stay inert and cannot relabel logical-slot proof."""

from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from evidence_wiki.agent_execution import ExecutionError
from evidence_wiki.service_repair_binding import ServiceRepairBinding


def selected():
    return ServiceRepairBinding("sha256:" + "a" * 64, "sha256:" + "b" * 64)


def test_binding_roundtrip_is_frozen_and_confers_no_grant():
    binding = selected()
    assert ServiceRepairBinding.restore(binding.document()) == binding
    assert binding.document()["acceptance_owner"] == "canonical_native_owners"
    assert binding.document()["historical_effects"] == "not_current_approval"
    with pytest.raises(FrozenInstanceError):
        binding.service_plan_sha256 = "sha256:" + "c" * 64


@pytest.mark.parametrize("field", ["service_plan_sha256", "disagreement_plan_sha256"])
def test_wrong_plan_identity_refuses(field):
    binding = selected()
    profile = SimpleNamespace(review_services=SimpleNamespace(sha256=binding.service_plan_sha256),
        disagreement=SimpleNamespace(sha256=binding.disagreement_plan_sha256.removeprefix("sha256:")))
    assert binding.check(profile)
    if field == "service_plan_sha256":
        profile.review_services.sha256 = "sha256:" + "c" * 64
    else:
        profile.disagreement.sha256 = "c" * 64
    with pytest.raises(ExecutionError, match="service_repair_plan_changed"):
        binding.check(profile)


@pytest.mark.parametrize("attack", ["extra", "approval", "history", "slot_label"])
def test_caller_labels_cannot_change_fixed_proof_contract(attack):
    document = selected().document()
    if attack == "extra":
        document["authenticated"] = True
    elif attack == "approval":
        document["acceptance_owner"] = "model_consensus"
    elif attack == "history":
        document["historical_effects"] = "accepted"
    else:
        document["first_passes"] = "primary_label_only"
    with pytest.raises(ExecutionError):
        ServiceRepairBinding.restore(document)
