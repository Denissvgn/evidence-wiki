"""Frozen local delivery selections and exact proposals confer no approval."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from ._pack_io import bounded, canonical
from .agent_execution import ExecutionError, _decimal, _ref
from .errors import UsageError
from .mutation_contracts import detached, digest
from .onboarding_contract import _matches

PLAN = "research-artifact-delivery-plan/v1"
INPUT = "research-artifact-delivery-input/v1"
PROPOSAL = "research-artifact-delivery-proposal/v1"
RESULT = "research-artifact-delivery-result/v1"
MAX_BYTES = 1_048_576
CAPABILITIES = ("model", "eligibility.read", "release.propose")


def refuse(reason):
    raise UsageError("ARTIFACT_DELIVERY_REFUSED", "Checked artifact delivery refused.",
        details={"reason": reason}, recoverable=False)


@dataclass(frozen=True)
class ArtifactDestination:
    """An opaque approved reference and portable leaf inside the owning run."""

    destination_ref: str
    filename: str = "brief.md"

    def __post_init__(self):
        _ref(self.destination_ref)
        if (type(self.filename) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,90}\.md", self.filename) is None
                or re.fullmatch(r"(?i:con|prn|aux|nul|com[1-9]|lpt[1-9])", self.filename[:-3]) is not None):
            raise ExecutionError("artifact_destination_invalid")

    def document(self):
        return {"destination_ref": self.destination_ref, "filename": self.filename,
            "adapter": "native_posix_run_file", "replacement": "never"}


@dataclass(frozen=True)
class ArtifactDeliveryPlan:
    """Select host-owned destinations before binding any Release attempt."""

    config_ref: str
    controller_ref: str
    destinations: tuple[ArtifactDestination, ...]
    timeout_seconds: int = 60
    model_calls: int = 1
    tool_calls: int = 1
    tokens: int = 4096
    cost_limit: str = "1"
    max_output_bytes: int = MAX_BYTES

    def __post_init__(self):
        _ref(self.config_ref)
        _ref(self.controller_ref)
        _decimal(self.cost_limit)
        if type(self.destinations) not in (tuple, list):
            raise ExecutionError("artifact_destinations_invalid")
        selected = tuple(self.destinations)
        if (not 1 <= len(selected) <= 16 or any(type(row) is not ArtifactDestination for row in selected)
                or len({row.destination_ref for row in selected}) != len(selected)
                or len({row.filename for row in selected}) != len(selected)):
            raise ExecutionError("artifact_destinations_invalid")
        object.__setattr__(self, "destinations", selected)
        for name, low, high in (("timeout_seconds", 1, 600), ("model_calls", 1, 1),
                ("tool_calls", 0, 100), ("tokens", 1, 100000), ("max_output_bytes", 1, MAX_BYTES)):
            if type(getattr(self, name)) is not int or not low <= getattr(self, name) <= high:
                raise ExecutionError("artifact_delivery_bound_invalid")
        if Decimal(self.cost_limit) <= 0:
            raise ExecutionError("artifact_delivery_cost_invalid")

    def check_profile(self, profile):
        role = profile.role(self.config_ref)
        if (role.role != "host_worker" or role.controller_ref != self.controller_ref
                or role.model_ref.startswith("offline/") or profile.role_scopes is None):
            raise ExecutionError("artifact_release_profile_invalid")
        scope = profile.role_scopes.scope(self.config_ref)
        if scope.task_role != "release":
            raise ExecutionError("artifact_release_scope_invalid")
        for capability in CAPABILITIES:
            scope.require(capability)

    def document(self):
        return {"schema_version": PLAN, "config_ref": self.config_ref, "controller_ref": self.controller_ref,
            "destinations": [row.document() for row in self.destinations], "format": "markdown",
            "limits": {"seconds": self.timeout_seconds, "model_calls": self.model_calls,
                "tool_calls": self.tool_calls, "tokens": self.tokens, "cost": self.cost_limit,
                "artifact_bytes": self.max_output_bytes, "attempts": 1},
            "proposal_scope": "exact_identity_selection", "semantic_qualification": "not_claimed",
            "acceptance_owner": "current_native_brief", "authority": "frozen_run_policy_and_current_host_grants"}

    @property
    def sha256(self):
        return digest(self.document())

    def destination(self, reference):
        for row in self.destinations:
            if row.destination_ref == reference:
                return row
        refuse("destination_not_approved")


def schemas():
    def obj(**properties):
        return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}
    def text(maximum=256):
        return {"type": "string", "minLength": 1, "maxLength": maximum}
    def array(items, maximum):
        return {"type": "array", "items": items, "maxItems": maximum}
    hashed = {"type": "string", "pattern": r"^sha256:[a-f0-9]{64}$(?![\s\S])"}
    integer = lambda maximum: {"type": "integer", "minimum": 0, "maximum": maximum}
    false = {"const": False}
    artifact = obj(artifact_id=hashed, artifact_sha256=hashed, bytes=integer(MAX_BYTES),
        media_type={"const": "text/markdown; charset=utf-8"},
        basis_id=hashed, check_scope={"const": "current_native_brief"}, authorized=false)
    destination = obj(destination_ref=text(), filename={"type": "string", "pattern": r"^[A-Za-z0-9][A-Za-z0-9_-]{0,90}\.md$(?![\s\S])"},
        adapter={"const": "native_posix_run_file"}, replacement={"const": "never"})
    plan = obj(schema_version={"const": PLAN}, config_ref=text(), controller_ref=text(),
        destinations={**array(destination, 16), "minItems": 1}, format={"const": "markdown"},
        limits=obj(seconds=integer(600), model_calls={"const": 1}, tool_calls=integer(100), tokens=integer(100000),
            cost=text(128), artifact_bytes=integer(MAX_BYTES), attempts={"const": 1}),
        proposal_scope={"const": "exact_identity_selection"}, semantic_qualification={"const": "not_claimed"},
        acceptance_owner={"const": "current_native_brief"}, authority={"const": "frozen_run_policy_and_current_host_grants"})
    input_document = obj(schema_version={"const": INPUT}, artifact=artifact,
        approved_destination_refs={**array(text(), 16), "minItems": 1}, plan_sha256=hashed,
        proposal_scope={"const": "exact_identity_selection"}, output_schema={"const": PROPOSAL}, authorized=false)
    proposal = obj(schema_version={"const": PROPOSAL}, artifact_id=hashed, artifact_sha256=hashed,
        artifact_bytes=integer(MAX_BYTES), destination_ref=text(), plan_sha256=hashed)
    result = obj(schema_version={"const": RESULT}, request_id=text(),
        status={"enum": ["prepared", "committed", "refused", "commit_unknown", "reconciled", "unavailable"]},
        artifact={"anyOf": [artifact, {"type": "null"}]},
        destination_ref={"anyOf": [text(), {"type": "null"}]},
        receipt_ref={"anyOf": [hashed, {"type": "null"}]},
        boundary={"enum": ["artifact_checked", "host_enforced", "not_observed"]},
        current_eligibility={"enum": ["checked_at_delivery", "not_evaluated", "unavailable"]},
        historical={"type": "boolean"}, reasons=array(text(), 16),
        authorized=false, evidence_accepted=false)
    return {name: {"$schema": "https://json-schema.org/draft/2020-12/schema", **shape}
        for name, shape in ((PLAN, plan), (INPUT, input_document), (PROPOSAL, proposal), (RESULT, result))}


def checked(name, value):
    try:
        bounded(value)
        _matches(value, schemas()[name])
        if len(canonical(value)) > MAX_BYTES:
            refuse("document_bound")
        artifact = value.get("artifact") if name in {INPUT, RESULT} else value if name == PROPOSAL else None
        if artifact is not None and artifact["artifact_id"] != artifact["artifact_sha256"]:
            refuse("artifact_identity_invalid")
        if name == PLAN:
            ArtifactDeliveryPlan(value["config_ref"], value["controller_ref"],
                tuple(ArtifactDestination(row["destination_ref"], row["filename"]) for row in value["destinations"]),
                **{key: value["limits"][field] for key, field in (("timeout_seconds", "seconds"),
                    ("model_calls", "model_calls"), ("tool_calls", "tool_calls"), ("tokens", "tokens"),
                    ("cost_limit", "cost"), ("max_output_bytes", "artifact_bytes"))})
        return detached(value)
    except UsageError as error:
        if error.error_code == "ARTIFACT_DELIVERY_REFUSED":
            raise
        refuse("document_invalid")
    except (TypeError, ValueError, KeyError):
        refuse("document_invalid")
