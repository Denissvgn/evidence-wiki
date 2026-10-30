"""Frozen optional investigation selection, separate from required first passes."""

from __future__ import annotations

from dataclasses import dataclass

from .agent_execution import ExecutionError, _digest, _ref
from .mutation_contracts import detached

PLAN = "research-disagreement-plan/v1"
AUXILIARY = "research-auxiliary-qualification/v1"


@dataclass(frozen=True)
class DisagreementPlan:
    """Enable bounded repair; optionally select one independently qualified investigator."""

    adjudicator_config_ref: str | None = None

    def __post_init__(self):
        if self.adjudicator_config_ref is not None:
            _ref(self.adjudicator_config_ref)

    def document(self):
        return {"schema_version": PLAN, "adjudicator_config_ref": self.adjudicator_config_ref,
                "claim_repair_limit": 2, "claim_adjudication_limit": 1}

    @property
    def sha256(self):
        return _digest(self.document())

    def check_profile(self, profile):
        """Validate configuration only; the reducer validates frozen policy role pairs."""
        scopes = getattr(profile, "role_scopes", None)
        if scopes is None:
            raise ExecutionError("disagreement_scopes_required")
        if self.adjudicator_config_ref is None:
            return
        calibration = getattr(profile, "calibration", None)
        if calibration is None:
            raise ExecutionError("disagreement_auxiliary_calibration_required")
        role = profile.role(self.adjudicator_config_ref)
        scope = scopes.scope(self.adjudicator_config_ref)
        if role.role != "evaluator" or scope.task_role != "adjudicator":
            raise ExecutionError("disagreement_auxiliary_role_invalid")
        calibration.selection(self.adjudicator_config_ref)
        if any(other.config_ref != role.config_ref and other.controller_ref == role.controller_ref for other in profile.roles):
            raise ExecutionError("disagreement_auxiliary_controller_shared")

    def auxiliary_profile(self, policy, profile):
        self.check_profile(profile)
        if self.adjudicator_config_ref is None:
            return None
        required = policy.to_dict()["required_evaluators"]
        if any(row["profile_ref"] == self.adjudicator_config_ref for row in required):
            raise ExecutionError("disagreement_auxiliary_is_required")
        role = profile.role(self.adjudicator_config_ref)
        return {"role": "adjudicator", "profile_ref": role.config_ref,
                "controller_ref": role.controller_ref, "human_required": False}


def auxiliary_observation(plan, registry, policy, profile, now_utc, rubric_id, *, rubric_sha256,
                          rubric, domain, probe):
    """Request an explicit auxiliary projection; it cannot become a required vote.

    The registry owns authenticated protected records and current eligibility.
    Legacy registries lacking the auxiliary route remain unknown. This adapter
    stores nothing and invokes no model/configuration callback.
    """
    if type(plan) is not DisagreementPlan:
        raise ExecutionError("disagreement_plan_invalid")
    selected = plan.auxiliary_profile(policy, profile)
    result = {"schema_version": AUXILIARY, "profile": selected,
        "state": "unknown" if selected else "unavailable", "reason": "qualification_unknown" if selected else "not_configured",
        "qualified": None, "independent": None, "qualification_ref": None,
        "native_review_allowed": False, "authorized": False, "evidence_accepted": False}
    if selected is None:
        return result
    rows = registry.observations(policy, profile, now_utc, rubric_id, rubric_sha256=rubric_sha256,
        rubric=rubric, domain=domain, probe=probe, selected_profiles=[selected], auxiliary=True)
    matches = [row for row in rows if type(row) is dict and all(row.get(key) == value for key, value in selected.items())]
    if len(rows) != 1 or len(matches) != 1:
        return result
    observed = matches[0]
    if observed.get("qualified") is True and observed.get("independent") is True:
        result.update(state="qualified", reason="qualified_auxiliary_profile", qualified=True,
            independent=True, qualification_ref=observed.get("qualification_ref"))
    return detached(result)
