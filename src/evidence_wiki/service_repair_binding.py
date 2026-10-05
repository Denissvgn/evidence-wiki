"""Explicit immutable activation of native logical-slot repair first passes."""

import re
from dataclasses import dataclass

from evidence_wiki.agent_execution import ExecutionError
from evidence_wiki.mutation_contracts import digest

BINDING = "research-service-repair-binding/v1"


@dataclass(frozen=True)
class ServiceRepairBinding:
    service_plan_sha256: str
    disagreement_plan_sha256: str

    def __post_init__(self):
        for value in (self.service_plan_sha256, self.disagreement_plan_sha256):
            if type(value) is not str or re.fullmatch(r"sha256:[a-f0-9]{64}", value) is None:
                raise ExecutionError("service_repair_binding_invalid")

    def document(self):
        return {"schema_version": BINDING, "service_plan_sha256": self.service_plan_sha256,
            "disagreement_plan_sha256": self.disagreement_plan_sha256,
            "first_passes": "actual_current_native_review_for_each_logical_slot",
            "historical_effects": "not_current_approval", "acceptance_owner": "canonical_native_owners"}

    @property
    def sha256(self):
        return digest(self.document())

    def check(self, profile):
        services = getattr(profile, "review_services", None)
        disagreement = getattr(profile, "disagreement", None)
        if (services is None or disagreement is None or services.sha256 != self.service_plan_sha256
                or "sha256:" + disagreement.sha256.removeprefix("sha256:") != self.disagreement_plan_sha256):
            raise ExecutionError("service_repair_plan_changed")
        # Existing construction still verifies real role scope/controller/model
        # separation and native deployment slot limits. This binding adds no grant.
        return True

    @classmethod
    def restore(cls, value):
        if type(value) is not dict or set(value) != {"schema_version", "service_plan_sha256",
                "disagreement_plan_sha256", "first_passes", "historical_effects", "acceptance_owner"}:
            raise ExecutionError("service_repair_document_invalid")
        result = cls(value["service_plan_sha256"], value["disagreement_plan_sha256"])
        if result.document() != value:
            raise ExecutionError("service_repair_document_invalid")
        return result


def schema_document():
    hashed = {"type": "string", "pattern": r"^sha256:[a-f0-9]{64}$", "minLength": 71, "maxLength": 71}
    properties = {"schema_version": {"const": BINDING}, "service_plan_sha256": hashed,
        "disagreement_plan_sha256": hashed, "first_passes": {"const": "actual_current_native_review_for_each_logical_slot"},
        "historical_effects": {"const": "not_current_approval"}, "acceptance_owner": {"const": "canonical_native_owners"}}
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}
