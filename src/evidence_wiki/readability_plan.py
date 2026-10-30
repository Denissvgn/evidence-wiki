"""Immutable critic and protected reference selection grants no work or approval."""

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from .agent_execution import ExecutionError, _decimal, _json, _ref
from .mutation_contracts import detached, digest
from .readability_contracts import CAPABILITIES, MAX_INPUT, MAX_OUTPUT

PLAN = "research-readability-plan/v1"
HANDOFF = "research-readability-input-handoff/v1"
PURPOSE = "readability"


@dataclass(frozen=True, init=False)
class ReadabilityCalibrationSelection:
    """Freeze independently reproduced presentation references before qualification."""

    config_ref: str
    _bytes: bytes = field(repr=False)

    def __init__(self, config_ref, suite, *, model_revision):
        from .qualification_calibration import calibration_suite
        from .readability_reference import DESCRIPTOR, DOMAIN, assess_references
        _ref(config_ref)
        _ref(model_revision)
        value = detached(suite)
        expected = calibration_suite(value["cases"], **{key: value[key] for key in
            ("suite_ref", "split_ref", "domains", "rubric", "model_settings", "drift_ref")})
        if value != expected or value["domains"] != [DOMAIN]:
            raise ExecutionError("readability_selected_suite_invalid")
        assessment = assess_references(value["cases"])
        document = {"schema_version": "research-readability-calibration-selection/v1", "config_ref": config_ref,
            "model_revision": model_revision, "model_settings": value["model_settings"], "rubric": value["rubric"],
            "rubric_sha256": hashlib.sha256(_json(value["rubric"])).hexdigest(), "suite_sha256": value["suite_sha256"],
            "split_ref": value["split_ref"], "drift_ref": value["drift_ref"], "domain_descriptor": DESCRIPTOR,
            "reference_assessment": assessment,
            "scorer_source_sha256": hashlib.sha256(Path(__file__).with_name("qualification_calibration.py").read_bytes()).hexdigest(),
            "classifier_source_sha256": hashlib.sha256(Path(__file__).with_name("readability_reference.py").read_bytes()).hexdigest()}
        object.__setattr__(self, "config_ref", config_ref)
        object.__setattr__(self, "_bytes", _json(document))

    def document(self):
        return json.loads(self._bytes)

    @property
    def sha256(self):
        return hashlib.sha256(self._bytes).hexdigest()


@dataclass(frozen=True)
class ReadabilityInputHandoff:
    """Typed exact bundled stdin custody; a returned worker ACK is insufficient."""

    delivery_seconds: int = 30

    def __post_init__(self):
        if type(self.delivery_seconds) is not int or not 1 <= self.delivery_seconds <= 30:
            raise ExecutionError("readability_handoff_invalid")

    def document(self):
        return {"schema_version": HANDOFF, "route": "new_protected_readability_invocation",
            "adapter": "exact_bounded_subprocess_executor", "delivery_seconds": self.delivery_seconds,
            "custody": "native_same_thread_source_use_through_stdin", "worker_comprehension": "not_observed"}


@dataclass(frozen=True)
class ReadabilityPlan:
    selection: ReadabilityCalibrationSelection
    controller_ref: str
    model_calls: int = 1
    tool_calls: int = 1
    tokens: int = 50000
    cost_limit: str = "1"
    timeout_seconds: int = 120
    max_attempts: int = 1
    backoff_seconds: int = 1
    probe_seconds: int = 5
    probe_lifetime_seconds: int = 30
    max_input_bytes: int = MAX_INPUT
    max_output_bytes: int = MAX_OUTPUT
    handoff: ReadabilityInputHandoff = ReadabilityInputHandoff()

    def __post_init__(self):
        if type(self.selection) is not ReadabilityCalibrationSelection or type(self.handoff) is not ReadabilityInputHandoff:
            raise ExecutionError("readability_selection_required")
        _ref(self.controller_ref)
        _decimal(self.cost_limit)
        for key, lo, hi in (("model_calls", 1, 20), ("tool_calls", 0, 100), ("tokens", 1, 100000), ("timeout_seconds", 1, 600),
                ("max_attempts", 1, 3), ("backoff_seconds", 1, 60), ("probe_seconds", 1, 30), ("probe_lifetime_seconds", 1, 30),
                ("max_input_bytes", 4096, MAX_INPUT),
                ("max_output_bytes", 1024, MAX_OUTPUT)):
            value = getattr(self, key)
            if type(value) is not int or not lo <= value <= hi:
                raise ExecutionError("readability_bound_invalid")

    @property
    def config_ref(self):
        return self.selection.config_ref

    def document(self):
        return {"schema_version": PLAN, "config_ref": self.config_ref, "controller_ref": self.controller_ref,
            "qualification_selector_sha256": "sha256:" + self.selection.sha256,
            "rubric_sha256": "sha256:" + self.selection.document()["rubric_sha256"],
            "qualification_purpose": PURPOSE, "task_role": "critic", "authority_role": "evaluator",
            "required_capabilities": list(CAPABILITIES), "limits": {"model_calls": self.model_calls,
                "tool_calls": self.tool_calls, "tokens": self.tokens, "cost": self.cost_limit, "seconds": self.timeout_seconds,
                "max_attempts": self.max_attempts, "backoff_seconds": self.backoff_seconds},
            "max_input_bytes": self.max_input_bytes, "max_output_bytes": self.max_output_bytes,
            "max_originals": 64,
            "probe_seconds": self.probe_seconds, "probe_lifetime_seconds": self.probe_lifetime_seconds,
            "handoff": self.handoff.document(), "required_context": "complete_current_permitted_brief",
            "edit_policy": "field_issues_then_scoped_repair_and_fresh_review"}

    @property
    def sha256(self):
        return digest(self.document())

    def check_profile(self, profile):
        role = profile.role(self.config_ref)
        if profile.role_scopes is None:
            raise ExecutionError("readability_scoped_calibration_required")
        scope = profile.role_scopes.scope(self.config_ref)
        if role.role != "evaluator" or scope.task_role != "critic" or role.controller_ref != self.controller_ref:
            raise ExecutionError("readability_role_invalid")
        for capability in CAPABILITIES:
            scope.require(capability)
        if any(other.config_ref != self.config_ref and other.controller_ref == self.controller_ref for other in profile.roles):
            raise ExecutionError("readability_controller_shared")
        if (self.model_calls > profile.model_calls * self.max_attempts or self.tokens > profile.tokens * self.max_attempts
                or self.tool_calls > profile.tool_calls * self.max_attempts):
            raise ExecutionError("readability_profile_bound_invalid")
        if profile.disagreement is None:
            raise ExecutionError("readability_scoped_repair_required")
        if profile.schedule is not None:
            limit = profile.schedule.limit(role.role, role.controller_ref)
            if self.model_calls > limit.model_calls or self.tokens > limit.tokens or Decimal(self.cost_limit) > Decimal(limit.cost_limit):
                raise ExecutionError("readability_role_bound_invalid")

    def selected_profile(self, policy, profile):
        self.check_profile(profile)
        from .review_service_policy import approved_profiles
        if any(row["profile_ref"] == self.config_ref for row in approved_profiles(policy.to_dict(), profile)):
            raise ExecutionError("readability_is_mandatory_review")
        return {"role": "critic", "profile_ref": self.config_ref, "controller_ref": self.controller_ref, "human_required": False}


def schemas():
    """Fixed immutable declarations remain inert without actual native owners."""
    def obj(**fields):
        return {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}
    def integer(lo, hi):
        return {"type": "integer", "minimum": lo, "maximum": hi}
    ref = {"type": "string", "minLength": 1, "maxLength": 256}
    hashed = {"type": "string", "pattern": r"^sha256:[a-f0-9]{64}$(?![\s\S])"}
    handoff = obj(schema_version={"const": HANDOFF}, route={"const": "new_protected_readability_invocation"},
        adapter={"const": "exact_bounded_subprocess_executor"}, delivery_seconds=integer(1, 30),
        custody={"const": "native_same_thread_source_use_through_stdin"}, worker_comprehension={"const": "not_observed"})
    plan = obj(schema_version={"const": PLAN}, config_ref=ref, controller_ref=ref,
        qualification_selector_sha256=hashed, rubric_sha256=hashed, qualification_purpose={"const": PURPOSE},
        task_role={"const": "critic"}, authority_role={"const": "evaluator"},
        required_capabilities={"const": list(CAPABILITIES)}, limits=obj(model_calls=integer(1, 20), tool_calls=integer(0, 100),
            tokens=integer(1, 100000), cost={"type": "string", "maxLength": 25}, seconds=integer(1, 600),
            max_attempts=integer(1, 3), backoff_seconds=integer(1, 60)), probe_seconds=integer(1, 30),
        probe_lifetime_seconds=integer(1, 30), max_input_bytes=integer(4096, MAX_INPUT),
        max_originals={"const": 64},
        max_output_bytes=integer(1024, MAX_OUTPUT), handoff=handoff,
        required_context={"const": "complete_current_permitted_brief"},
        edit_policy={"const": "field_issues_then_scoped_repair_and_fresh_review"})
    return {PLAN: plan, HANDOFF: handoff}
