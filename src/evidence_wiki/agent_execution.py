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
from typing import Any, Protocol

from .workflow_contracts import JOB, JOB_V2, MAX_BYTES, document_sha256, validate_document, validate_job

PROFILE = "research-execution-profile/v1"
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

    def __post_init__(self) -> None:
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

    def document(self) -> dict[str, Any]:
        return {
            "schema_version": PROFILE, "profile_ref": self.profile_ref, "total_steps": self.total_steps,
            "currency": self.currency, "pricing_ref": self.pricing_ref,
            "roles": [role.document() for role in self.roles],
            "job_budgets": {"model_calls": self.model_calls, "tool_calls": self.tool_calls,
                            "tokens": self.tokens, "cost": self.cost_limit},
            "cleanup_timeout_seconds": self.cleanup_timeout_seconds,
        }

    @property
    def sha256(self) -> str:
        return _digest(self.document())

    def role(self, config_ref: str) -> ExecutionRole:
        for role in self.roles:
            if role.config_ref == config_ref:
                return role
        raise ExecutionError("execution_role_unavailable")


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
            if version not in {JOB, JOB_V2}:
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
        if version not in {JOB, JOB_V2}:
            raise ExecutionError("execution_job_version_unsupported")
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
        self._lock = threading.RLock()

    def _check(self, attempt: ExecutionAttempt) -> ExecutionRole:
        if type(attempt) is not ExecutionAttempt or attempt.profile_sha256 != self.profile.sha256:
            raise ExecutionError("execution_profile_mismatch")
        role = self.profile.role(attempt.config_ref)
        job = attempt.job
        if any(job[name] != getattr(role, name) for name in ("role", "controller_ref", "prompt_version")):
            raise ExecutionError("execution_role_mismatch")
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
        with self._lock:
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
                return self._unavailable(attempt, "executor_unavailable")
            timeout = min(job["timeout_seconds"], (_utc(job["deadline_utc"]) - _utc(stamp)).total_seconds())
            payload = _json(attempt.document())
            try:
                process = subprocess.Popen(  # noqa: S603 - argv is an explicit trusted host selection.
                    command, cwd=self._cwd, env=self._environment, stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True,
                )
            except OSError:
                return self._unavailable(attempt, "executor_unavailable")
            running = _Running(attempt, process, stamp, time.monotonic(), timeout)
            self._attempts[attempt.attempt_id] = running
            threading.Thread(target=self._read, args=(running,), daemon=True).start()
            threading.Thread(target=self._write, args=(running, payload), daemon=True).start()
            threading.Thread(target=self._watch, args=(running,), daemon=True).start()
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

    def _kill(self, running: _Running) -> None:
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
            return self._envelope(running.attempt, sequence=running.sequence, started_utc=running.started_utc,
                                  details=details)

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


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ExecutionError("execution_output_duplicate")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ExecutionError("execution_output_invalid")
