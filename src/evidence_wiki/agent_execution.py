"""Explicit host execution with bounded processes and historical receipts.

The reference executor bounds a process group, not arbitrary filesystem or
network access. Commands and meters are trusted host selections. Workers receive
only the pinned job/input and an explicit environment, never receipt keys. A
receipt proves observed execution; canonical owners still decide eligibility.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import math
import os
import queue
import re
import secrets
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from .workflow_contracts import JOB, JOB_V2, JOB_V3, JOB_V4, MAX_BYTES, document_sha256, validate_document, validate_job

if TYPE_CHECKING:
    from .calibration_registry import CalibrationPlan
    from .disagreement_configuration import DisagreementPlan
    from .execution_roles import ExecutionRoleRegistry
    from .review_delivery import ReviewDelivery
    from .review_service_policy import ReviewServicePlan

PROFILE = "research-execution-profile/v1"
PROFILE_V2 = "research-execution-profile/v2"
PROFILE_V3 = "research-execution-profile/v3"
PROFILE_V4 = "research-execution-profile/v4"
PROFILE_V5 = "research-execution-profile/v5"
PROFILE_V6 = "research-execution-profile/v6"
PROFILE_V7 = "research-execution-profile/v7"
PROFILE_V8 = "research-execution-profile/v8"
PROFILE_V9 = "research-execution-profile/v9"
PROFILE_V10 = "research-execution-profile/v10"
PROFILE_V11 = "research-execution-profile/v11"
PROFILE_V12 = "research-execution-profile/v12"
OBSERVATION = "research-execution-observation/v1"
_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}$")
_COST = re.compile(r"^(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,12})?$")
_HASH = re.compile(r"^[a-f0-9]{64}$")
_TERMINAL = frozenset({"succeeded", "failed", "cancelled", "expired", "unknown", "unavailable"})
_OBSERVATION_FIELDS = frozenset({
    "schema_version", "run_id", "binding_id", "profile_sha256", "attempt_id", "nonce", "job_id",
    "job_sha256", "input_sha256", "controller_ref", "config_ref", "model_ref", "prompt_version", "sequence",
    "issued_utc", "deadline_utc", "started_utc", "completed_utc", "observed_utc", "output", "output_sha256",
    "currency", "pricing_ref", "state", "reason", "observed_usage", "final_accounting",
    "cancellation_confirmed", "authentication",
})


class ExecutionError(ValueError):
    """A host configuration, execution correlation or receipt was refused."""


def _ref(value: Any) -> str:
    if type(value) is not str or not _REFERENCE.fullmatch(value):
        raise ExecutionError("execution_reference_invalid")
    return value


def _integer(value: Any, maximum: int) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ExecutionError("execution_bound_invalid")
    return value


def _decimal(value: Any) -> str:
    if type(value) is not str or not _COST.fullmatch(value):
        raise ExecutionError("execution_cost_invalid")
    try:
        if not Decimal(value).is_finite():
            raise ExecutionError("execution_cost_invalid")
    except InvalidOperation:
        raise ExecutionError("execution_cost_invalid") from None
    return value


def _utc(value: str) -> datetime:
    if type(value) is not str or len(value) > 40:
        raise ExecutionError("execution_clock_invalid")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ExecutionError("execution_clock_invalid") from None
    if stamp.tzinfo is None or stamp.utcoffset().total_seconds() != 0:
        raise ExecutionError("execution_clock_invalid")
    return stamp


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> bytes:
    """Bound inert JSON without allowing recursive or nonfinite documents."""
    count = 0

    def visit(item: Any, depth: int) -> None:
        nonlocal count
        count += 1
        if count > 16384 or depth > 16:
            raise ExecutionError("execution_document_limit")
        if type(item) is dict:
            if any(type(key) is not str for key in item):
                raise ExecutionError("execution_document_invalid")
            for child in item.values():
                visit(child, depth + 1)
        elif type(item) is list:
            for child in item:
                visit(child, depth + 1)
        elif type(item) is float:
            if not math.isfinite(item):
                raise ExecutionError("execution_document_invalid")
        elif type(item) is str and len(item) > MAX_BYTES:
            raise ExecutionError("execution_document_limit")
        elif type(item) not in (str, int, bool, type(None)):
            raise ExecutionError("execution_document_invalid")

    visit(value, 0)
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(raw) > MAX_BYTES:
        raise ExecutionError("execution_document_limit")
    return raw


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value)).hexdigest()


@dataclass(frozen=True)
class ExecutionRole:
    """Host-selected role identity; distinct names do not prove isolation."""

    config_ref: str
    role: str
    controller_ref: str
    model_ref: str
    prompt_version: str

    def __post_init__(self) -> None:
        for name in ("config_ref", "controller_ref", "model_ref", "prompt_version"):
            _ref(getattr(self, name))
        if self.role not in {"host_worker", "evaluator"}:
            raise ExecutionError("execution_role_invalid")

    def document(self) -> dict[str, str]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True)
class ExecutionRoleLimit:
    """Aggregate ceilings for one semantic role and controller across configs."""

    role: str
    controller_ref: str
    model_calls: int
    tool_calls: int
    tokens: int
    cost_limit: str
    seconds: int

    def __post_init__(self) -> None:
        if type(self.role) is not str or self.role not in {"host_worker", "evaluator"}:
            raise ExecutionError("execution_role_invalid")
        _ref(self.controller_ref)
        for name in ("model_calls", "tool_calls", "tokens"):
            _integer(getattr(self, name), 100000000)
        _integer(self.seconds, 86400)
        _decimal(self.cost_limit)

    def document(self) -> dict[str, Any]:
        return {"role": self.role, "controller_ref": self.controller_ref,
                "budgets": {"model_calls": self.model_calls, "tool_calls": self.tool_calls,
                            "tokens": self.tokens, "cost": self.cost_limit, "seconds": self.seconds}}


@dataclass(frozen=True)
class ExecutionSchedule:
    """Explicit continuation policy; the canonical run owner controls dispatch."""

    role_limits: tuple[ExecutionRoleLimit, ...]
    retry_reasons: tuple[str, ...] = ("executor_unavailable",)
    backoff_seconds: int = 0
    failover: tuple[tuple[str, ...], ...] = ()

    def __post_init__(self) -> None:
        if type(self.role_limits) not in (list, tuple):
            raise ExecutionError("execution_role_limits_invalid")
        limits = tuple(self.role_limits)
        if not 1 <= len(limits) <= 32 or any(type(item) is not ExecutionRoleLimit for item in limits):
            raise ExecutionError("execution_role_limits_invalid")
        if len({(item.role, item.controller_ref) for item in limits}) != len(limits):
            raise ExecutionError("execution_role_limits_duplicate")
        if type(self.retry_reasons) not in (list, tuple) or len(self.retry_reasons) > 16:
            raise ExecutionError("execution_retry_reasons_invalid")
        reasons = tuple(_ref(item) for item in self.retry_reasons)
        if len(set(reasons)) != len(reasons):
            raise ExecutionError("execution_retry_reasons_invalid")
        _integer(self.backoff_seconds, 3600)
        if type(self.failover) not in (list, tuple) or len(self.failover) > 16:
            raise ExecutionError("execution_failover_invalid")
        chains, configs = [], set()
        for supplied in self.failover:
            if type(supplied) not in (list, tuple) or not 2 <= len(supplied) <= 32:
                raise ExecutionError("execution_failover_invalid")
            chain = tuple(_ref(item) for item in supplied)
            if len(set(chain)) != len(chain) or configs.intersection(chain):
                raise ExecutionError("execution_failover_overlap")
            configs.update(chain)
            chains.append(chain)
        object.__setattr__(self, "role_limits", limits)
        object.__setattr__(self, "retry_reasons", reasons)
        object.__setattr__(self, "failover", tuple(chains))

    def limit(self, role: str, controller_ref: str) -> ExecutionRoleLimit:
        for item in self.role_limits:
            if (item.role, item.controller_ref) == (role, controller_ref):
                return item
        raise ExecutionError("execution_role_limit_unavailable")

    def document(self) -> dict[str, Any]:
        return {"role_limits": [item.document() for item in self.role_limits],
                "retry_reasons": list(self.retry_reasons), "backoff_seconds": self.backoff_seconds,
                "failover": [list(chain) for chain in self.failover]}


@dataclass(frozen=True)
class ExecutionProfile:
    """Immutable host selection with finite steps and per-job ceilings.

    These ceilings do not authorize work or track aggregate run consumption.
    The canonical run owner binds this profile and owns durable reservations.
    """

    profile_ref: str
    total_steps: int
    currency: str
    pricing_ref: str
    roles: tuple[ExecutionRole, ...]
    model_calls: int
    tool_calls: int
    tokens: int
    cost_limit: str
    cleanup_timeout_seconds: float = 2.0
    schedule: ExecutionSchedule | None = None
    role_scopes: ExecutionRoleRegistry | None = None
    required_diversity: tuple[str, ...] = ()
    calibration: CalibrationPlan | None = None
    disagreement: DisagreementPlan | None = None
    review_delivery: ReviewDelivery | None = None
    review_services: ReviewServicePlan | None = None
    service_repair: object | None = None
    input_handoff: object | None = None
    readability: object | None = None
    artifact_delivery: object | None = None

    def __post_init__(self) -> None:
        if self.input_handoff is not None:
            from .protected_handoff import NativeInputHandoff
            if type(self.input_handoff) is not NativeInputHandoff or self.disagreement is None or self.role_scopes is None:
                raise ExecutionError("protected_handoff_profile_invalid")
        _ref(self.profile_ref)
        _ref(self.pricing_ref)
        if type(self.currency) is not str or not re.fullmatch(r"[A-Z]{3}", self.currency):
            raise ExecutionError("execution_currency_invalid")
        if not 1 <= _integer(self.total_steps, 1024):
            raise ExecutionError("execution_bound_invalid")
        for name, maximum in (("model_calls", 50), ("tool_calls", 100), ("tokens", 100000)):
            _integer(getattr(self, name), maximum)
        _decimal(self.cost_limit)
        if type(self.cleanup_timeout_seconds) not in (int, float) or not 0 < self.cleanup_timeout_seconds <= 30:
            raise ExecutionError("execution_cleanup_bound_invalid")
        if type(self.roles) not in (list, tuple):
            raise ExecutionError("execution_roles_invalid")
        roles = tuple(self.roles)
        if not 1 <= len(roles) <= 32 or any(type(role) is not ExecutionRole for role in roles):
            raise ExecutionError("execution_roles_invalid")
        if len({role.config_ref for role in roles}) != len(roles):
            raise ExecutionError("execution_roles_duplicate")
        object.__setattr__(self, "roles", roles)
        if self.schedule is not None:
            if type(self.schedule) is not ExecutionSchedule:
                raise ExecutionError("execution_schedule_invalid")
            if ({(role.role, role.controller_ref) for role in roles}
                    != {(item.role, item.controller_ref) for item in self.schedule.role_limits}):
                raise ExecutionError("execution_role_limits_incomplete")
            for chain in self.schedule.failover:
                selected = [self.role(config) for config in chain]
                if (any(role.role != "host_worker" for role in selected)
                        or len({role.controller_ref for role in selected}) != 1):
                    raise ExecutionError("execution_failover_identity_invalid")

        if self.role_scopes is not None:
            from .execution_roles import ExecutionRoleRegistry

            if type(self.role_scopes) is not ExecutionRoleRegistry or ({role.config_ref for role in roles}
                    != {scope.config_ref for scope in self.role_scopes.scopes}):
                raise ExecutionError("execution_role_scopes_incomplete")
            if any(self.role_scopes.scope(role.config_ref).authority_role != role.role for role in roles):
                raise ExecutionError("execution_role_scope_mismatch")
        if (type(self.required_diversity) not in (list, tuple)
                or any(type(item) is not str or item not in {"control", "context", "model"} for item in self.required_diversity)
                or len(set(self.required_diversity)) != len(self.required_diversity)
                or self.required_diversity and self.role_scopes is None):
            raise ExecutionError("execution_required_diversity_invalid")
        object.__setattr__(self, "required_diversity", tuple(sorted(self.required_diversity)))
        if self.calibration is not None:
            from .calibration_registry import CalibrationPlan

            if (type(self.calibration) is not CalibrationPlan or self.role_scopes is None
                    or {row.config_ref for row in self.calibration.selections}
                    != {role.config_ref for role in roles if role.role == "evaluator"
                        and role.config_ref != getattr(self.readability, "config_ref", None)}):
                raise ExecutionError("calibration_profile_incomplete")
        if self.disagreement is not None:
            from .disagreement_configuration import DisagreementPlan
            if type(self.disagreement) is not DisagreementPlan:
                raise ExecutionError("disagreement_profile_invalid")
            self.disagreement.check_profile(self)
        if self.review_delivery is not None:
            from .review_delivery import ReviewDelivery
            if type(self.review_delivery) is not ReviewDelivery or self.role_scopes is None:
                raise ExecutionError("execution_complete_review_scope_required")

        if self.review_services is not None:
            from .review_service_policy import ReviewServicePlan
            if type(self.review_services) is not ReviewServicePlan:
                raise ExecutionError("service_plan_invalid")
            if self.service_repair is not None:
                from .service_repair_binding import ServiceRepairBinding
                if type(self.service_repair) is not ServiceRepairBinding:
                    raise ExecutionError("service_repair_binding_invalid")
                self.service_repair.check(self)
            self.review_services.check_profile(self)
        elif self.service_repair is not None:
            raise ExecutionError("service_repair_plan_required")
        if self.readability is not None:
            from .readability_plan import ReadabilityPlan
            if type(self.readability) is not ReadabilityPlan:
                raise ExecutionError("readability_plan_invalid")
            self.readability.check_profile(self)

        if self.artifact_delivery is not None:
            from .artifact_delivery_contracts import ArtifactDeliveryPlan
            if type(self.artifact_delivery) is not ArtifactDeliveryPlan:
                raise ExecutionError("artifact_delivery_plan_invalid")
            self.artifact_delivery.check_profile(self)

    def document(self) -> dict[str, Any]:
        result = {
            "schema_version": PROFILE, "profile_ref": self.profile_ref, "total_steps": self.total_steps,
            "currency": self.currency, "pricing_ref": self.pricing_ref,
            "roles": [role.document() for role in self.roles],
            "job_budgets": {"model_calls": self.model_calls, "tool_calls": self.tool_calls,
                            "tokens": self.tokens, "cost": self.cost_limit},
            "cleanup_timeout_seconds": self.cleanup_timeout_seconds,
        }
        if self.schedule is not None:
            result.update(schema_version=PROFILE_V2, scheduling=self.schedule.document())
        if self.role_scopes is not None:
            result.update(schema_version=PROFILE_V3, role_scopes=self.role_scopes.document(),
                          required_diversity=list(self.required_diversity))
        if self.calibration is not None:
            result.update(schema_version=PROFILE_V4, calibration=self.calibration.document())
        if self.disagreement is not None:
            result.update(schema_version=PROFILE_V5, disagreement=self.disagreement.document())
        if self.role_scopes is not None and any(scope.task_role == "calculation" for scope in self.role_scopes.scopes):
            result["schema_version"] = PROFILE_V6
        if self.review_delivery is not None:
            result.update(schema_version=PROFILE_V7, review_delivery=self.review_delivery.document())
        if self.review_services is not None:
            result.update(schema_version=PROFILE_V8, review_services=self.review_services.document())
        if self.service_repair is not None:
            result.update(schema_version=PROFILE_V9, service_repair=self.service_repair.document())
        if self.input_handoff is not None:
            result.update(schema_version=PROFILE_V10, input_handoff=self.input_handoff.document())
        if self.readability is not None:
            result.update(schema_version=PROFILE_V11, readability=self.readability.document())
        if self.artifact_delivery is not None:
            result.update(schema_version=PROFILE_V12, artifact_delivery=self.artifact_delivery.document())
        return result

    @property
    def sha256(self) -> str:
        return _digest(self.document())

    def role(self, config_ref: str) -> ExecutionRole:
        for role in self.roles:
            if role.config_ref == config_ref:
                return role
        raise ExecutionError("execution_role_unavailable")


    def check_job_scope(self, config_ref: str, job: dict[str, Any], input_document: dict[str, Any]) -> None:
        """Bind an explicit job purpose and immutable input to its frozen scope."""
        if type(job) is not dict or type(input_document) is not dict:
            raise ExecutionError("execution_attempt_invalid")
        if self.role_scopes is None:
            if job["schema_version"] in {JOB_V3, JOB_V4}:
                raise ExecutionError("execution_role_scope_unavailable")
            return
        if job["schema_version"] not in {JOB_V3, JOB_V4}:
            raise ExecutionError("execution_role_scope_required")
        self.role_scopes.scope(config_ref).check_job(job)
        if job["packet_sha256"] != _digest(input_document):
            raise ExecutionError("execution_context_mismatch")
        packet = input_document.get("packet")
        if self.readability is not None and config_ref == self.readability.config_ref and input_document.get("purpose") not in {"readability", "readability_calibration"}:
            raise ExecutionError("execution_context_mismatch")
        if job.get("payload", {}).get("qualification_purpose") == "readability" and input_document.get("purpose") not in {"readability", "readability_calibration"}:
            raise ExecutionError("execution_context_mismatch")
        if input_document.get("purpose") in {"readability", "readability_calibration"}:
            from .readability_context import input_document as readability_input
            if (self.readability is None or config_ref != self.readability.config_ref or type(packet) is not dict
                    or packet.get("context_id") != job["context_id"] or packet.get("task_role") != job["task_role"]
                    or job["task_role"] != "critic" or set(input_document) != {"purpose", "packet"}):
                raise ExecutionError("execution_context_mismatch")
            if job["payload"].get("qualification_purpose") != "readability":
                raise ExecutionError("execution_context_mismatch")
            if input_document["purpose"] == "readability":
                readability_input(packet, max_input_bytes=self.readability.max_input_bytes)
            else:
                from .readability_reference import reference_document
                reference_document(input_document)
                case_id = job["payload"].get("calibration_case_id")
                matches = [row for row in self.readability.selection.document()["reference_assessment"]["results"]
                    if row["case_id"] == case_id and row["input_sha256"] == "sha256:" + _digest(input_document)]
                if len(matches) != 1:
                    raise ExecutionError("execution_context_mismatch")
        if type(packet) is dict and packet.get("schema_version") in {"research-review-context/v1", "research-review-context/v2"}:
            if packet.get("context_id") != job["context_id"] or packet.get("task_role") != job["task_role"]:
                raise ExecutionError("execution_context_mismatch")


@dataclass(frozen=True)
class ExecutionAttempt:
    """Pinned inert attempt; constructing it neither reserves nor dispatches."""

    attempt_id: str
    nonce: str
    run_id: str
    binding_id: str
    profile_sha256: str
    config_ref: str
    _job_bytes: bytes = field(repr=False)
    _input_bytes: bytes = field(repr=False)

    def __post_init__(self) -> None:
        for name in ("attempt_id", "nonce", "run_id", "binding_id", "config_ref"):
            _ref(getattr(self, name))
        if type(self.profile_sha256) is not str or not _HASH.fullmatch(self.profile_sha256):
            raise ExecutionError("execution_profile_digest_invalid")
        if type(self._job_bytes) is not bytes or type(self._input_bytes) is not bytes:
            raise ExecutionError("execution_attempt_invalid")
        if len(self._job_bytes) > MAX_BYTES or len(self._input_bytes) > MAX_BYTES:
            raise ExecutionError("execution_document_limit")
        try:
            raw_job = json.loads(self._job_bytes)
            version = raw_job.get("schema_version") if type(raw_job) is dict else None
            if version not in {JOB, JOB_V2, JOB_V3, JOB_V4}:
                raise ExecutionError("execution_job_version_unsupported")
            job = validate_document(version, raw_job)
            supplied = json.loads(self._input_bytes)
        except (ValueError, UnicodeError, RecursionError):
            raise ExecutionError("execution_attempt_invalid") from None
        if type(supplied) is not dict or _json(job) != self._job_bytes or _json(supplied) != self._input_bytes:
            raise ExecutionError("execution_attempt_invalid")

    @classmethod
    def create(cls, *, attempt_id: str, nonce: str, run_id: str, binding_id: str,
               profile: ExecutionProfile, config_ref: str, job: dict[str, Any],
               input_document: dict[str, Any] | None = None) -> ExecutionAttempt:
        profile.role(config_ref)
        version = job.get("schema_version") if type(job) is dict else None
        if version not in {JOB, JOB_V2, JOB_V3, JOB_V4}:
            raise ExecutionError("execution_job_version_unsupported")
        selected_input = {} if input_document is None else input_document
        job = validate_document(version, job)
        profile.check_job_scope(config_ref, job, selected_input)
        return cls(attempt_id, nonce, run_id, binding_id, profile.sha256, config_ref,
                   _json(validate_document(version, job)), _json({} if input_document is None else input_document))

    @property
    def job(self) -> dict[str, Any]:
        return json.loads(self._job_bytes)

    @property
    def input_document(self) -> dict[str, Any]:
        return json.loads(self._input_bytes)

    @property
    def job_sha256(self) -> str:
        return document_sha256(self.job["schema_version"], self.job)

    @property
    def input_sha256(self) -> str:
        return hashlib.sha256(self._input_bytes).hexdigest()

    def worker_document(self) -> dict[str, Any]:
        """Project scoped worker input while retaining full correlation in the host.

        Provider adapters must use this projection for scoped jobs. It never
        establishes filesystem, network or host conversation isolation.
        """
        job = self.job
        if job["schema_version"] not in {JOB_V3, JOB_V4}:
            return self.document()
        fields = ("operation", "role", "task_role", "required_capabilities", "context_id", "prompt_version",
                  "issued_utc", "deadline_utc", "timeout_seconds", "budgets", "expected_result_schema")
        return {"schema_version": "research-agent-input/v1", "job": {name: job[name] for name in fields},
                "input": self.input_document}

    def document(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id, "nonce": self.nonce, "run_id": self.run_id,
            "binding_id": self.binding_id, "profile_sha256": self.profile_sha256,
            "config_ref": self.config_ref, "job_sha256": self.job_sha256,
            "input_sha256": self.input_sha256, "job": self.job, "input": self.input_document,
        }


class UsageMeter(Protocol):
    """Trusted host observation service, independent of worker output.

    A timed-out service remains unknown; the executor makes no assertion that
    arbitrary host service code or its external effects were cancelled.
    """

    def finalize(self, attempt: ExecutionAttempt, process_summary: dict[str, Any]) -> dict[str, Any]: ...


class AgentExecutor(Protocol):
    profile: ExecutionProfile

    def invoke(self, attempt: ExecutionAttempt) -> dict[str, Any]: ...
    def poll(self, attempt: ExecutionAttempt) -> dict[str, Any]: ...
    def cancel(self, attempt: ExecutionAttempt) -> dict[str, Any]: ...
    def authenticate(self, observation: dict[str, Any], attempt: ExecutionAttempt) -> dict[str, Any]: ...


@dataclass
class _Running:
    attempt: ExecutionAttempt
    process: Any
    started_utc: str
    started_monotonic: float
    timeout: float
    data: bytearray = field(default_factory=bytearray)
    reason: str | None = None
    snapshot: dict[str, Any] | None = None
    sequence: int = 0
    lock: Any = field(default_factory=threading.RLock)
    finished: Any = field(default_factory=threading.Event)
    protected_delivery: bool = False
    handoff_complete: Any = field(default_factory=threading.Event)
    handoff_decided: Any = field(default_factory=threading.Event)
    handoff_deadline: float | None = None
    dispatch_observation: bytes | None = None


def _usage(value: Any) -> dict[str, Any]:
    if type(value) is not dict or set(value) != {"seconds", "model_calls", "tool_calls", "tokens", "cost"}:
        raise ExecutionError("execution_usage_invalid")
    result = copy.deepcopy(value)
    for name in ("model_calls", "tool_calls", "tokens"):
        if result[name] is not None:
            _integer(result[name], 100000000)
    if result["seconds"] is not None:
        number = result["seconds"]
        if type(number) not in (int, float) or not math.isfinite(number) or not 0 <= number <= 86400:
            raise ExecutionError("execution_usage_invalid")
    if result["cost"] is not None:
        _decimal(result["cost"])
    return result


def validate_observation(value: Any) -> dict[str, Any]:
    """Decode the closed inert receipt shape; this grants no authentication."""
    if type(value) is not dict or set(value) != _OBSERVATION_FIELDS or value["schema_version"] != OBSERVATION:
        raise ExecutionError("execution_observation_invalid")
    result = json.loads(_json(value))
    for name in ("run_id", "binding_id", "attempt_id", "nonce", "job_id", "controller_ref", "config_ref",
                 "model_ref", "prompt_version", "pricing_ref", "reason"):
        _ref(result[name])
    for name in ("profile_sha256", "job_sha256", "input_sha256", "authentication"):
        if type(result[name]) is not str or not _HASH.fullmatch(result[name]):
            raise ExecutionError("execution_observation_invalid")
    _integer(result["sequence"], 10000000)
    for name in ("issued_utc", "deadline_utc", "observed_utc"):
        _utc(result[name])
    for name in ("started_utc", "completed_utc"):
        if result[name] is not None:
            _utc(result[name])
    if _utc(result["issued_utc"]) >= _utc(result["deadline_utc"]):
        raise ExecutionError("execution_observation_invalid")
    if _utc(result["observed_utc"]) < _utc(result["issued_utc"]):
        raise ExecutionError("execution_clock_regressed")
    if result["started_utc"] is not None:
        started = _utc(result["started_utc"])
        if started < _utc(result["issued_utc"]) or started >= _utc(result["deadline_utc"]):
            raise ExecutionError("execution_observation_invalid")
        if started > _utc(result["observed_utc"]):
            raise ExecutionError("execution_clock_regressed")
    if result["completed_utc"] is not None:
        if result["started_utc"] is None:
            raise ExecutionError("execution_observation_invalid")
        if not _utc(result["started_utc"]) <= _utc(result["completed_utc"]) <= _utc(result["observed_utc"]):
            raise ExecutionError("execution_clock_regressed")
    if result["state"] not in _TERMINAL | {"running"}:
        raise ExecutionError("execution_observation_invalid")
    if type(result["currency"]) is not str or not re.fullmatch(r"[A-Z]{3}", result["currency"]):
        raise ExecutionError("execution_observation_invalid")
    if type(result["final_accounting"]) is not bool or type(result["cancellation_confirmed"]) is not bool:
        raise ExecutionError("execution_observation_invalid")
    usage = _usage(result["observed_usage"])
    if result["final_accounting"] and (any(item is None for item in usage.values()) or result["state"] == "running"):
        raise ExecutionError("execution_observation_invalid")
    if result["cancellation_confirmed"] != (result["state"] == "cancelled"):
        raise ExecutionError("execution_observation_invalid")
    if result["output"] is None:
        if result["output_sha256"] is not None:
            raise ExecutionError("execution_observation_invalid")
    elif type(result["output"]) is not dict or result["output_sha256"] != _digest(result["output"]):
        raise ExecutionError("execution_observation_invalid")
    if result["state"] == "succeeded" and (result["output"] is None or result["completed_utc"] is None):
        raise ExecutionError("execution_observation_invalid")
    if result["state"] == "running" and (result["completed_utc"] is not None or result["output"] is not None):
        raise ExecutionError("execution_observation_invalid")
    return result


class BoundedSubprocessExecutor:
    """A serial, in-memory reference host with independent process watchdogs.

    Restarting this host cannot prove prior dispatch absent. Unknown attempts
    must be reconciled by the durable canonical owner; ``poll`` never dispatches.
    Trusted commands must keep descendants in their process group. This is not
    an OS sandbox against malicious workers escaping that group.
    """

    def __init__(self, profile: ExecutionProfile, *, commands: dict[str, tuple[str, ...]], cwd: Path,
                 environment: dict[str, str] | None = None, meter: UsageMeter | None = None,
                 receipt_key: bytes | None = None, max_output_bytes: int = 262144) -> None:
        if os.name != "posix":
            raise ExecutionError("execution_process_boundary_unsupported")
        if type(profile) is not ExecutionProfile:
            raise ExecutionError("execution_profile_invalid")
        directory = Path(cwd)
        if not directory.is_absolute() or directory.is_symlink() or not directory.is_dir():
            raise ExecutionError("execution_directory_invalid")
        self.profile = profile
        self._cwd = directory.resolve()
        self._commands = {}
        if type(commands) is not dict or len(commands) > 32:
            raise ExecutionError("execution_commands_invalid")
        for config, command in commands.items():
            profile.role(config)
            if type(command) not in (list, tuple):
                raise ExecutionError("execution_command_invalid")
            argv = tuple(command)
            if not argv or len(argv) > 128 or any(type(arg) is not str or not arg or "\0" in arg for arg in argv):
                raise ExecutionError("execution_command_invalid")
            if not Path(argv[0]).is_absolute():
                raise ExecutionError("execution_command_must_be_absolute")
            self._commands[config] = argv
        self._environment = dict(environment or {"PATH": os.defpath, "LANG": "C.UTF-8"})
        if len(self._environment) > 32 or any(
            type(key) is not str or type(value) is not str or "\0" in key + value
            or key not in {"PATH", "LANG", "LC_ALL", "TZ", "PYTHONIOENCODING", "PYTHONHASHSEED"}
            for key, value in self._environment.items()
        ):
            raise ExecutionError("execution_environment_refused")
        self._key = receipt_key if receipt_key is not None else secrets.token_bytes(32)
        if type(self._key) is not bytes or len(self._key) < 32:
            raise ExecutionError("execution_receipt_key_invalid")
        self._meter = meter
        if not 1 <= _integer(max_output_bytes, MAX_BYTES // 2):
            raise ExecutionError("execution_output_limit_invalid")
        self._max_output = max_output_bytes
        self._attempts: dict[str, _Running] = {}
        self._dispatch_retained_bytes = 0
        self._dispatch_retention_lock = threading.Lock()
        self._lock = threading.RLock()

    def _check(self, attempt: ExecutionAttempt) -> ExecutionRole:
        if type(attempt) is not ExecutionAttempt or attempt.profile_sha256 != self.profile.sha256:
            raise ExecutionError("execution_profile_mismatch")
        role = self.profile.role(attempt.config_ref)
        job = attempt.job
        if any(job[name] != getattr(role, name) for name in ("role", "controller_ref", "prompt_version")):
            raise ExecutionError("execution_role_mismatch")
        self.profile.check_job_scope(attempt.config_ref, job, attempt.input_document)
        budgets = self.profile.document()["job_budgets"]
        if any(job["budgets"][name] > budgets[name] for name in ("model_calls", "tool_calls", "tokens")):
            raise ExecutionError("execution_job_budget_exceeded")
        if Decimal(job["budgets"]["cost"]) > Decimal(budgets["cost"]):
            raise ExecutionError("execution_job_budget_exceeded")
        return role

    def _stored(self, attempt: ExecutionAttempt) -> _Running | None:
        self._check(attempt)
        value = self._attempts.get(attempt.attempt_id)
        if value is not None and value.attempt.document() != attempt.document():
            raise ExecutionError("execution_attempt_conflict")
        return value

    def invoke(self, attempt: ExecutionAttempt) -> dict[str, Any]:
        if self.profile.readability is not None and attempt.input_document.get("purpose") == "readability":
            raise ExecutionError("readability_guarded_handoff_required")
        if self.profile.input_handoff is not None and attempt.input_document.get("purpose") == "repair":
            raise ExecutionError("protected_handoff_custody_required")
        return self._invoke(attempt)

    def invoke_guarded(self, attempt: ExecutionAttempt, lease) -> dict[str, Any]:
        from .execution_handoff import NativeHandoffLease
        if type(self) is not BoundedSubprocessExecutor or type(lease) is not NativeHandoffLease or lease.attempt.document() != attempt.document():
            raise ExecutionError("protected_handoff_adapter_unsupported")
        return self._invoke_owned_lease(attempt, lease)

    def invoke_readability_guarded(self, attempt: ExecutionAttempt, lease) -> dict[str, Any]:
        from .readability_handoff import ReadabilityHandoffLease
        if type(self) is not BoundedSubprocessExecutor or type(lease) is not ReadabilityHandoffLease or lease.attempt.document() != attempt.document():
            raise ExecutionError("readability_handoff_adapter_unsupported")
        lease._bind_executor(self)
        return self._invoke_owned_lease(attempt, lease)

    def _invoke_owned_lease(self, attempt, lease):
        payload = _json(attempt.worker_document())
        try:
            with lease.custody(payload):
                result = self._invoke(attempt, lease)
            return result
        except Exception:
            with self._lock:
                running = self._attempts.get(attempt.attempt_id)
                if running is not None:
                    with running.lock:
                        running.reason = "protected_handoff_unknown"
                        self._kill(running)
                        if running.snapshot is not None:
                            running.snapshot.update(state="unknown", reason="protected_handoff_unknown", output=None,
                                observed_usage={name: None for name in ("seconds", "model_calls", "tool_calls", "tokens", "cost")},
                                final_accounting=False, cancellation_confirmed=False)
            raise

    def _invoke(self, attempt: ExecutionAttempt, lease=None) -> dict[str, Any]:
        from .readability_handoff import ReadabilityHandoffLease
        readability = type(lease) is ReadabilityHandoffLease
        with self._lock:
            if readability:
                lease._before_start(self)
            existing = self._stored(attempt)
            if existing is not None:
                return self._observe(existing)
            if len(self._attempts) >= self.profile.total_steps:
                raise ExecutionError("execution_step_limit")
            if any(not row.finished.is_set() for row in self._attempts.values()):
                raise ExecutionError("execution_attempt_pending")
            stamp = _now()
            job = validate_job(attempt.job, now_utc=stamp)
            if job["operation"] in {"poll", "cancel"}:
                raise ExecutionError("execution_control_job_unsupported")
            command = self._commands.get(attempt.config_ref)
            if command is None:
                return self._not_started(attempt) if self.profile.review_services is not None else self._unavailable(attempt, "executor_unavailable")
            timeout = min(job["timeout_seconds"], (_utc(job["deadline_utc"]) - _utc(stamp)).total_seconds())
            payload = _json(attempt.worker_document())
            try:
                process = subprocess.Popen(  # noqa: S603 - argv is an explicit trusted host selection.
                    command, cwd=self._cwd, env=self._environment, stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True,
                )
            except OSError:
                return self._unavailable(attempt, "executor_unavailable")
            running = _Running(attempt, process, stamp, time.monotonic(), timeout)
            running.protected_delivery = lease is not None
            running.handoff_deadline = None if lease is None else lease._deadline
            self._attempts[attempt.attempt_id] = running
            threading.Thread(target=self._read, args=(running,), daemon=True).start()
            if lease is None:
                threading.Thread(target=self._write, args=(running, payload), daemon=True).start()
            threading.Thread(target=self._watch, args=(running,), daemon=True).start()
            if readability:
                lease._started(self, running)
        if lease is not None:
            try:
                self._write_guarded(running, payload, lease)
                running.handoff_complete.set()
            except Exception:
                with running.lock:
                    running.reason = "protected_handoff_unknown"
                    self._kill(running)
                    if running.snapshot is not None:
                        running.snapshot.update(state="unknown", reason="protected_handoff_unknown", output=None,
                            observed_usage={name: None for name in ("seconds", "model_calls", "tool_calls", "tokens", "cost")},
                            final_accounting=False, cancellation_confirmed=False)
                raise
            finally:
                running.handoff_decided.set()
        return self._observe(running)

    def _write(self, running: _Running, payload: bytes) -> None:
        try:
            running.process.stdin.write(payload)
        except (OSError, ValueError):
            pass
        finally:
            try:
                running.process.stdin.close()
            except (OSError, ValueError):
                pass

    def _write_guarded(self, running: _Running, payload: bytes, lease) -> None:
        from .handoff_pipe import write_handoff
        write_handoff(running.process.stdin, payload, lease, deadline=lease._deadline,
            max_bytes=MAX_BYTES, is_alive=lambda: running.process.poll() is None)

    def _kill(self, running: _Running) -> None:
        if running.process is None:
            return
        try:
            os.killpg(running.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def _read(self, running: _Running) -> None:
        try:
            while chunk := running.process.stdout.read(8192):
                with running.lock:
                    available = self._max_output - len(running.data)
                    running.data.extend(chunk[:available])
                    if len(chunk) > available:
                        running.reason = "output_limit"
                        self._kill(running)
                        break
        except (OSError, ValueError):
            pass
        finally:
            running.process.stdout.close()

    def _meter_usage(self, running: _Running, summary: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        usage = {"seconds": summary["seconds"], "model_calls": None, "tool_calls": 1, "tokens": None, "cost": None}
        if self._meter is None:
            return usage, False
        responses: queue.Queue = queue.Queue(maxsize=1)

        def call() -> None:
            try:
                result = self._meter.finalize(running.attempt, copy.deepcopy(summary))
                responses.put(_usage(result))
            except Exception:
                responses.put(None)

        threading.Thread(target=call, daemon=True).start()
        try:
            measured = responses.get(timeout=self.profile.cleanup_timeout_seconds)
        except queue.Empty:
            return usage, False
        if measured is None:
            return usage, False
        return measured, all(value is not None for value in measured.values())

    def _watch(self, running: _Running) -> None:
        try:
            remaining = max(0, running.timeout - (time.monotonic() - running.started_monotonic))
            try:
                code = running.process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                with running.lock:
                    if running.reason is None:
                        running.reason = "deadline_expired"
                    self._kill(running)
                code = running.process.wait(timeout=self.profile.cleanup_timeout_seconds)
            # A successful leader cannot leave continuing work in its group.
            self._kill(running)
            if running.protected_delivery:
                running.handoff_decided.wait(timeout=max(0, running.handoff_deadline - time.monotonic()))
            elapsed = time.monotonic() - running.started_monotonic
            completed = _now()
            output = None
            # Pipe EOF means no surviving holder of this output within the boundary.
            until = time.monotonic() + self.profile.cleanup_timeout_seconds
            while not running.process.stdout.closed and time.monotonic() < until:
                time.sleep(0.005)
            with running.lock:
                reason = running.reason
                if not running.process.stdout.closed:
                    state, reason = "unknown", "output_boundary_unknown"
                elif reason == "cancellation_requested":
                    state = "cancelled"
                elif reason == "deadline_expired":
                    state = "expired"
                elif reason is not None or code != 0:
                    state, reason = "failed", reason or "worker_failed"
                else:
                    try:
                        output = json.loads(bytes(running.data), object_pairs_hook=_unique_object,
                                            parse_constant=_reject_constant)
                        if type(output) is not dict:
                            raise ValueError
                        _json(output)
                        state, reason = "succeeded", "completed"
                    except (ValueError, UnicodeError, RecursionError):
                        state, reason = "failed", "output_invalid"
                        output = None
            usage, final = self._meter_usage(running, {"state": state, "seconds": elapsed,
                                                      "started_utc": running.started_utc, "completed_utc": completed})
            with running.lock:
                if running.reason == "protected_handoff_unknown" or running.protected_delivery and not running.handoff_complete.is_set():
                    state, reason, output = "unknown", "protected_handoff_unknown", None
                    usage, final = {name: None for name in ("seconds", "model_calls", "tool_calls", "tokens", "cost")}, False
                running.snapshot = {"state": state, "reason": reason, "completed_utc": completed,
                                    "output": output, "observed_usage": usage, "final_accounting": final,
                                    "cancellation_confirmed": state == "cancelled"}
        except Exception:
            with running.lock:
                self._kill(running)
                running.snapshot = {"state": "unknown", "reason": "executor_observation_unknown",
                                    "completed_utc": None, "output": None,
                                    "observed_usage": {"seconds": None, "model_calls": None, "tool_calls": None,
                                                       "tokens": None, "cost": None},
                                    "final_accounting": False, "cancellation_confirmed": False}
        finally:
            running.finished.set()

    def _envelope(self, attempt: ExecutionAttempt, *, sequence: int, started_utc: str | None,
                  details: dict[str, Any]) -> dict[str, Any]:
        role = self._check(attempt)
        job = attempt.job
        output = details["output"]
        value = {
            "schema_version": OBSERVATION, "run_id": attempt.run_id, "binding_id": attempt.binding_id,
            "profile_sha256": attempt.profile_sha256, "attempt_id": attempt.attempt_id, "nonce": attempt.nonce,
            "job_id": job["id"], "job_sha256": attempt.job_sha256, "input_sha256": attempt.input_sha256,
            "controller_ref": role.controller_ref, "config_ref": role.config_ref, "model_ref": role.model_ref,
            "prompt_version": role.prompt_version, "sequence": sequence, "issued_utc": job["issued_utc"],
            "deadline_utc": job["deadline_utc"], "started_utc": started_utc, "observed_utc": _now(),
            "output_sha256": _digest(output) if output is not None else None,
            "currency": self.profile.currency, "pricing_ref": self.profile.pricing_ref, **copy.deepcopy(details),
        }
        value["authentication"] = hmac.new(self._key, _json(value), hashlib.sha256).hexdigest()
        return validate_observation(value)

    def _not_started(self, attempt):
        """A local invoke precheck observed no process/effect; retain exact terminal state."""
        running = _Running(attempt, None, None, time.monotonic(), 0)
        running.snapshot = {"state": "unavailable", "reason": "executor_unavailable", "completed_utc": None,
            "output": None, "observed_usage": {"seconds": 0, "model_calls": 0, "tool_calls": 0, "tokens": 0, "cost": "0"},
            "final_accounting": True, "cancellation_confirmed": False}
        running.finished.set()
        self._attempts[attempt.attempt_id] = running
        return self._observe(running)

    def _unavailable(self, attempt: ExecutionAttempt, reason: str) -> dict[str, Any]:
        return self._envelope(attempt, sequence=0, started_utc=None, details={
            "state": "unavailable", "reason": reason, "completed_utc": None, "output": None,
            "observed_usage": {"seconds": None, "model_calls": None, "tool_calls": None, "tokens": None, "cost": None},
            "final_accounting": False, "cancellation_confirmed": False,
        })

    def _observe(self, running: _Running) -> dict[str, Any]:
        with running.lock:
            running.sequence += 1
            details = running.snapshot or {
                "state": "running", "reason": "execution_pending", "completed_utc": None, "output": None,
                "observed_usage": {"seconds": time.monotonic() - running.started_monotonic,
                                   "model_calls": None, "tool_calls": 1, "tokens": None, "cost": None},
                "final_accounting": False, "cancellation_confirmed": False,
            }
            value = self._envelope(running.attempt, sequence=running.sequence, started_utc=running.started_utc,
                                   details=details)
            if running.sequence == 1:
                encoded = _json(value)
                with self._dispatch_retention_lock:
                    if self._dispatch_retained_bytes + len(encoded) <= 8 * 1024 * 1024:
                        running.dispatch_observation = encoded
                        self._dispatch_retained_bytes += len(encoded)
            return value

    def _dispatch_observation(self, attempt):
        """Read exact retained first receipt, never synthesize or renew dispatch.

        Retention is bounded to eight MiB per live executor. Missing history,
        retention exhaustion and host restart remain unavailable observations.
        """
        with self._lock:
            running = self._stored(attempt)
            if running is None:
                return None
            with running.lock:
                return None if running.dispatch_observation is None else json.loads(running.dispatch_observation)

    def poll(self, attempt: ExecutionAttempt) -> dict[str, Any]:
        with self._lock:
            running = self._stored(attempt)
            if running is None:
                return self._unavailable(attempt, "dispatch_not_observed")
            return self._observe(running)

    def cancel(self, attempt: ExecutionAttempt) -> dict[str, Any]:
        with self._lock:
            running = self._stored(attempt)
            if running is None:
                return self._unavailable(attempt, "dispatch_not_observed")
            with running.lock:
                if running.snapshot is None and running.process.poll() is None:
                    running.reason = "cancellation_requested"
                    self._kill(running)
        running.finished.wait(timeout=2 * self.profile.cleanup_timeout_seconds + 0.1)
        return self._observe(running)

    def authenticate(self, observation: dict[str, Any], attempt: ExecutionAttempt) -> dict[str, Any]:
        """Authenticate historical correlation without renewing execution authority."""
        self._check(attempt)
        value = validate_observation(observation)
        signature = value.pop("authentication", None)
        if type(signature) is not str or not _HASH.fullmatch(signature):
            raise ExecutionError("execution_authentication_refused")
        expected = hmac.new(self._key, _json(value), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ExecutionError("execution_authentication_refused")
        role, job = self.profile.role(attempt.config_ref), attempt.job
        pinned = {"schema_version": OBSERVATION, "run_id": attempt.run_id, "binding_id": attempt.binding_id,
                  "profile_sha256": attempt.profile_sha256, "attempt_id": attempt.attempt_id,
                  "nonce": attempt.nonce, "job_id": job["id"], "job_sha256": attempt.job_sha256,
                  "input_sha256": attempt.input_sha256, "controller_ref": role.controller_ref,
                  "config_ref": role.config_ref, "model_ref": role.model_ref, "prompt_version": role.prompt_version,
                  "issued_utc": job["issued_utc"], "deadline_utc": job["deadline_utc"],
                  "currency": self.profile.currency, "pricing_ref": self.profile.pricing_ref}
        if any(value.get(name) != wanted for name, wanted in pinned.items()):
            raise ExecutionError("execution_correlation_refused")
        value["authentication"] = signature
        return value


class DeterministicOfflineExecutor:
    """Explicit inert fixtures for mechanics, with observed local usage only.

    Output selection performs no model, tool, network or subprocess invocation.
    Historical receipts authenticate fixture observations, never live judgments.
    The canonical run owner retains reservations and owns restart recovery.
    """

    def __init__(self, profile: ExecutionProfile, *, outputs: dict[str, dict[str, Any]],
                 receipt_key: bytes) -> None:
        if type(profile) is not ExecutionProfile:
            raise ExecutionError("execution_profile_invalid")
        if any(not role.model_ref.startswith("offline/") for role in profile.roles):
            raise ExecutionError("execution_offline_model_required")
        if type(receipt_key) is not bytes or not 32 <= len(receipt_key) <= 128:
            raise ExecutionError("execution_receipt_key_invalid")
        if type(outputs) is not dict or len(outputs) > 32:
            raise ExecutionError("execution_offline_outputs_invalid")
        _json(outputs)
        selected = {}
        for config_ref, output in outputs.items():
            profile.role(config_ref)
            if type(output) is not dict:
                raise ExecutionError("execution_offline_output_invalid")
            encoded = _json(output)
            if len(encoded) > MAX_BYTES // 2:
                raise ExecutionError("execution_output_limit_invalid")
            selected[config_ref] = json.loads(encoded)
        self.profile = profile
        self._key = receipt_key
        self._outputs = selected
        self._attempts: dict[str, tuple[ExecutionAttempt, dict[str, Any]]] = {}
        self._lock = threading.RLock()

    # Reuse the reference host's pure correlation and closed receipt contract.
    _check = BoundedSubprocessExecutor._check
    _envelope = BoundedSubprocessExecutor._envelope
    authenticate = BoundedSubprocessExecutor.authenticate

    def _stored(self, attempt: ExecutionAttempt) -> dict[str, Any] | None:
        self._check(attempt)
        stored = self._attempts.get(attempt.attempt_id)
        if stored is not None and stored[0].document() != attempt.document():
            raise ExecutionError("execution_attempt_conflict")
        return None if stored is None else stored[1]

    def invoke(self, attempt: ExecutionAttempt) -> dict[str, Any]:
        with self._lock:
            stored = self._stored(attempt)
            if stored is not None:
                return copy.deepcopy(stored)
            if len(self._attempts) >= self.profile.total_steps:
                raise ExecutionError("execution_step_limit")
            started = _now()
            job = validate_job(attempt.job, now_utc=started)
            if job["operation"] in {"poll", "cancel"}:
                raise ExecutionError("execution_control_job_unsupported")
            before = time.monotonic()
            output = copy.deepcopy(self._outputs.get(attempt.config_ref))
            elapsed = time.monotonic() - before
            completed = _now()
            unavailable = output is None
            expired = not unavailable and (_utc(completed) >= _utc(job["deadline_utc"])
                                          or elapsed >= job["timeout_seconds"])
            value = self._envelope(attempt, sequence=1, started_utc=None if unavailable else started, details={
                "state": "unavailable" if unavailable else "expired" if expired else "succeeded",
                "reason": "executor_unavailable" if unavailable else "deadline_expired" if expired else "offline_fixture",
                "completed_utc": None if unavailable else completed,
                "output": None if expired else output,
                "observed_usage": {"seconds": 0 if unavailable else elapsed, "model_calls": 0,
                                   "tool_calls": 0, "tokens": 0, "cost": "0"},
                "final_accounting": True, "cancellation_confirmed": False,
            })
            self._attempts[attempt.attempt_id] = (attempt, value)
            return copy.deepcopy(value)

    def poll(self, attempt: ExecutionAttempt) -> dict[str, Any]:
        with self._lock:
            stored = self._stored(attempt)
            if stored is not None:
                return copy.deepcopy(stored)
            return self._envelope(attempt, sequence=0, started_utc=None, details={
                "state": "unavailable", "reason": "dispatch_not_observed", "completed_utc": None,
                "output": None, "observed_usage": {"seconds": None, "model_calls": None,
                    "tool_calls": None, "tokens": None, "cost": None},
                "final_accounting": False, "cancellation_confirmed": False,
            })

    def cancel(self, attempt: ExecutionAttempt) -> dict[str, Any]:
        return self.poll(attempt)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ExecutionError("execution_output_duplicate")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ExecutionError("execution_output_invalid")
