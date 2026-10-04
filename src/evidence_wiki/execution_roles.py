"""Frozen semantic job scopes, separate from authenticated native principals.

Capabilities restrict a host-selected dispatch. They grant no canonical effect,
credentials, signing authority, filesystem access or model independence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .agent_execution import ExecutionError, ExecutionRole, _digest, _ref, validate_observation

ROLE_SCOPE = "research-execution-role-scope/v1"
ROLE_REGISTRY = "research-execution-role-registry/v1"
ISOLATION_REPORT = "research-execution-isolation-report/v1"
_ROLES = {
    "coverage": ("host_worker", frozenset({"model", "objective.read", "evidence.read", "coverage.propose"})),
    "planner": ("host_worker", frozenset({"model", "objective.read", "plan.propose"})),
    "researcher": ("host_worker", frozenset({"model", "objective.read", "evidence.read", "claim.propose", "acquisition.propose"})),
    "evaluator": ("evaluator", frozenset({"model", "review.read", "verdict.propose"})),
    "critic": ("evaluator", frozenset({"model", "review.read", "evidence.read", "objection.propose", "acquisition.propose"})),
    "adjudicator": ("evaluator", frozenset({"model", "review.read", "disagreement.read", "resolution.propose"})),
    "repair": ("host_worker", frozenset({"model", "evidence.read", "defects.read", "repair.propose"})),
    "integration": ("host_worker", frozenset({"model", "public_contract.read", "integration.propose", "offline_check"})),
    "release": ("host_worker", frozenset({"model", "eligibility.read", "release.propose"})),
}
TASK_ROLES = tuple(_ROLES)
TASK_OPERATIONS = {role: frozenset({"evaluate"}) if role in {"evaluator", "critic", "adjudicator"}
                   else frozenset({"generate", "probe"}) if role == "integration" else frozenset({"generate"})
                   for role in TASK_ROLES}


@dataclass(frozen=True)
class ExecutionRoleScope:
    """An exact configured worker's allowed task purpose and inert capabilities."""

    config_ref: str
    task_role: str
    capabilities: tuple[str, ...]

    def __post_init__(self) -> None:
        _ref(self.config_ref)
        if type(self.task_role) is not str or self.task_role not in _ROLES:
            raise ExecutionError("execution_task_role_invalid")
        if type(self.capabilities) not in (list, tuple) or len(self.capabilities) > 16:
            raise ExecutionError("execution_role_capabilities_invalid")
        capabilities = tuple(_ref(item) for item in self.capabilities)
        if len(set(capabilities)) != len(capabilities):
            raise ExecutionError("execution_role_capabilities_duplicate")
        if not set(capabilities) <= _ROLES[self.task_role][1]:
            raise ExecutionError("execution_role_capability_refused")
        object.__setattr__(self, "capabilities", tuple(sorted(capabilities)))

    @property
    def authority_role(self) -> str:
        return _ROLES[self.task_role][0]

    def require(self, capability: str) -> None:
        if _ref(capability) not in self.capabilities:
            raise ExecutionError("execution_role_capability_refused")

    def document(self) -> dict[str, Any]:
        return {"schema_version": ROLE_SCOPE, "config_ref": self.config_ref, "task_role": self.task_role,
                "authority_role": self.authority_role, "capabilities": list(self.capabilities)}

    @property
    def sha256(self) -> str:
        return _digest(self.document())

    def check_job(self, job: dict[str, Any]) -> None:
        """Check scope correlation after the owning closed job codec validates it."""
        if (type(job) is not dict or job.get("task_role") != self.task_role
                or job.get("role") != self.authority_role
                or job.get("role_scope_sha256") != self.sha256):
            raise ExecutionError("execution_role_scope_mismatch")
        capabilities = job.get("required_capabilities")
        if (type(capabilities) is not list or any(type(item) is not str for item in capabilities)
                or len(set(capabilities)) != len(capabilities)
                or not set(capabilities) <= set(self.capabilities)):
            raise ExecutionError("execution_role_capability_refused")
        operation = job.get("operation")
        if type(operation) is not str or operation not in TASK_OPERATIONS[self.task_role]:
            raise ExecutionError("execution_role_operation_refused")
        required = "offline_check" if operation == "probe" else "model"
        if required not in capabilities:
            raise ExecutionError("execution_role_capability_refused")


def isolation_report(roles: tuple[ExecutionRole, ...], *, authenticated_observations: tuple[dict[str, Any], ...] = (),
                     contexts: tuple[Any, ...] = (), generator_controller_ref: str | None = None) -> dict[str, Any]:
    """Report independent evidence dimensions without converting labels to proof.

    The calling canonical owner must authenticate observations before supplying
    them. This pure reducer cannot authenticate host receipts or confer review
    eligibility. An immutable packet proves its content scope, not host session
    isolation; a model reference does not prove provider execution or diversity.
    """
    from .review_context import ReviewContext

    if (type(roles) not in (list, tuple) or not 1 <= len(roles) <= 32
            or any(type(role) is not ExecutionRole for role in roles)
            or type(authenticated_observations) not in (list, tuple) or len(authenticated_observations) > 1024
            or type(contexts) not in (list, tuple) or len(contexts) > 1024
            or any(type(context) is not ReviewContext for context in contexts)):
        raise ExecutionError("execution_isolation_inputs_invalid")
    configured = {role.config_ref: role for role in roles}
    if len(configured) != len(roles):
        raise ExecutionError("execution_roles_duplicate")
    if generator_controller_ref is not None:
        _ref(generator_controller_ref)
    observed = {}
    for supplied in authenticated_observations:
        receipt = validate_observation(supplied)
        role = configured.get(receipt["config_ref"])
        if role is None or any(receipt[name] != getattr(role, name) for name in ("controller_ref", "model_ref", "prompt_version")):
            raise ExecutionError("execution_isolation_identity_mismatch")
        key = (receipt["run_id"], receipt["attempt_id"])
        previous = observed.get(key)
        if previous is not None and previous["sequence"] == receipt["sequence"] and previous != receipt:
            raise ExecutionError("execution_isolation_observation_conflict")
        if previous is None or receipt["sequence"] > previous["sequence"]:
            observed[key] = receipt
    executed = [receipt for receipt in observed.values() if receipt["started_utc"] is not None]
    controllers = sorted({receipt["controller_ref"] for receipt in executed})
    models = sorted({receipt["model_ref"] for receipt in executed})
    reviewers = [receipt for receipt in executed if configured[receipt["config_ref"]].role == "evaluator"]
    reviewer_refs = sorted({receipt["controller_ref"] for receipt in reviewers})
    reviewer_configs = {receipt["config_ref"] for receipt in reviewers}
    separated = None if generator_controller_ref is None or not reviewers else (
        generator_controller_ref not in reviewer_refs and len(reviewer_refs) == len(reviewer_configs))
    context_ids = [context.document()["context_id"] for context in contexts]
    packet_ids = [context.packet_id for context in contexts]
    return {"schema_version": ISOLATION_REPORT,
        "control": {"configured_controller_refs": sorted({role.controller_ref for role in roles}),
            "observed_controller_refs": controllers, "reviewer_controller_refs": reviewer_refs,
            "authenticated_controller_refs_distinct": separated, "credential_isolation": "unknown"},
        "context": {"context_ids": context_ids, "packet_ids": packet_ids,
            "distinct_context_ids": None if not contexts else len(set(context_ids)) == len(context_ids),
            "immutable_packet_scope": "observed" if contexts else "unknown", "host_session_isolation": "unknown"},
        "model": {"configured_model_refs": sorted({role.model_ref for role in roles}), "observed_model_refs": models,
            "observed_prompt_versions": sorted({receipt["prompt_version"] for receipt in executed}),
            "distinct_model_refs": None if not executed else len(models) > 1,
            "model_family_diversity": "unknown", "live_model_execution": "unknown", "semantic_qualification": "unknown"},
        "limitations": ["Host-authenticated controller references do not prove independent credential custody.",
            "Fresh immutable packet identities do not prove isolated host conversation or filesystem state.",
            "Configured or observed model references do not establish model families, live execution or semantic reliability."],
        "authorized": False, "evidence_accepted": False}

@dataclass(frozen=True)
class ExecutionRoleRegistry:
    """One frozen role scope per configured worker, with exact-only selection."""

    scopes: tuple[ExecutionRoleScope, ...]

    def __post_init__(self) -> None:
        if type(self.scopes) not in (list, tuple) or not 1 <= len(self.scopes) <= 32:
            raise ExecutionError("execution_role_registry_invalid")
        scopes = tuple(self.scopes)
        if any(type(scope) is not ExecutionRoleScope for scope in scopes):
            raise ExecutionError("execution_role_registry_invalid")
        if len({scope.config_ref for scope in scopes}) != len(scopes):
            raise ExecutionError("execution_role_registry_duplicate")
        object.__setattr__(self, "scopes", tuple(sorted(scopes, key=lambda item: item.config_ref)))

    def scope(self, config_ref: str) -> ExecutionRoleScope:
        _ref(config_ref)
        for scope in self.scopes:
            if scope.config_ref == config_ref:
                return scope
        raise ExecutionError("execution_role_scope_unavailable")

    def select(self, task_role: str, *, config_ref: str | None = None) -> ExecutionRoleScope:
        if type(task_role) is not str or task_role not in _ROLES:
            raise ExecutionError("execution_task_role_invalid")
        if config_ref is not None:
            scope = self.scope(config_ref)
            if scope.task_role != task_role:
                raise ExecutionError("execution_role_scope_mismatch")
            return scope
        selected = [scope for scope in self.scopes if scope.task_role == task_role]
        if len(selected) != 1:
            raise ExecutionError("execution_role_scope_ambiguous" if selected else "execution_role_scope_unavailable")
        return selected[0]

    def document(self) -> dict[str, Any]:
        return {"schema_version": ROLE_REGISTRY, "scopes": [scope.document() for scope in self.scopes]}

    @property
    def sha256(self) -> str:
        return _digest(self.document())
