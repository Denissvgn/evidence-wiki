"""Explicit host application windows independent of bounded worker execution."""

import asyncio
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .agent_execution import ExecutionError, _utc
from .workflow_contracts import (
    JOB_V5,
    JOB_V6,
    MAX_APPLICATION_LEASE_SECONDS,
    MAX_EXTENDED_APPLICATION_LEASE_SECONDS,
)

LEASE_V1 = "research-review-application-lease/v1"
LEASE_V2 = "research-review-application-lease/v2"

_WINDOW = ContextVar("review_application_window", default=None)


@dataclass(frozen=True)
class ReviewApplicationLease:
    """Immutable host selection; deadlines never extend worker execution."""

    seconds: int
    not_after_utc: str
    schema_version: str = LEASE_V1

    def __post_init__(self):
        if type(self.schema_version) is not str or self.schema_version not in {LEASE_V1, LEASE_V2}:
            raise ExecutionError("review_application_version_invalid")
        maximum = MAX_APPLICATION_LEASE_SECONDS if self.schema_version == LEASE_V1 else MAX_EXTENDED_APPLICATION_LEASE_SECONDS
        if type(self.seconds) is not int or not 1 <= self.seconds <= maximum:
            raise ExecutionError("review_application_bound_invalid")
        _utc(self.not_after_utc)

    def document(self):
        return {"schema_version": self.schema_version, "seconds": self.seconds,
            "not_after_utc": self.not_after_utc}


def _execution_owner():
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return threading.get_ident(), task


@contextmanager
def review_application_window(session):
    """Start one host service window before assembly, without renewing its end."""
    from .execution_session import deadline
    profile = session.execution.profile
    selected = profile.review_application_lease
    if type(selected) is not ReviewApplicationLease or _WINDOW.get() is not None:
        raise ExecutionError("review_application_window_invalid")
    started = datetime.now(timezone.utc)
    end = min(started + timedelta(seconds=selected.seconds), _utc(selected.not_after_utc), deadline(session))
    if end <= started:
        raise ExecutionError("review_application_window_expired")
    active = [True]
    token = _WINDOW.set((session, profile.sha256, started, end, _execution_owner(), active))
    try:
        yield
    finally:
        active[0] = False
        _WINDOW.reset(token)


def issuance(session):
    """Return only the original active host window bound to this exact session."""
    window = _WINDOW.get()
    if window is None or window[0] is not session or window[1] != session.execution.profile.sha256 or window[4] != _execution_owner() or not window[5][0]:
        raise ExecutionError("review_application_window_required")
    if datetime.now(timezone.utc) >= window[3]:
        raise ExecutionError("review_application_window_expired")
    return {"application_started_utc": window[2].isoformat(), "application_deadline_utc": window[3].isoformat()}


def check_job(profile, job, input_document):
    """Check immutable selection and ordinary contextual purpose at every use."""
    selected = profile.review_application_lease
    packet = input_document.get("packet")
    if (type(selected) is not ReviewApplicationLease or profile.review_services is not None
            or profile.semantic_judging is not None or job.get("operation") != "evaluate"
            or input_document.get("purpose") not in (None, "evaluate") or type(packet) is not dict
            or packet.get("schema_version") not in {"research-review-context/v1", "research-review-context/v2", "research-review-context/v3", "research-review-context/v4"}
            or job.get("task_role") not in {"evaluator", "critic"}):
        raise ExecutionError("review_application_job_invalid")
    expected = JOB_V5 if selected.schema_version == LEASE_V1 else JOB_V6
    if job.get("schema_version") != expected:
        raise ExecutionError("review_application_binding_invalid")
    start = _utc(job["application_started_utc"])
    end = _utc(job["application_deadline_utc"])
    if end > min(start + timedelta(seconds=selected.seconds), _utc(selected.not_after_utc)):
        raise ExecutionError("review_application_binding_invalid")


def application_deadline(session, attempt, row, observation):
    """Authenticate a frozen extended window; old attempts retain their lease."""
    job = attempt.job
    if job["schema_version"] not in {JOB_V5, JOB_V6}:
        return job["deadline_utc"]
    from .execution_session import deadline
    if (attempt.profile_sha256 != session.execution.profile.sha256
            or row.get("purpose") != "evaluate" or "retry_purpose" in row
            or "review_context" not in row or any(key in row for key in ("semantic_binding", "adjudication_binding"))):
        raise ExecutionError("review_application_binding_invalid")
    session.execution.profile.check_job_scope(attempt.config_ref, job, attempt.input_document)
    end = _utc(job["application_deadline_utc"])
    if end > deadline(session):
        raise ExecutionError("review_application_binding_invalid")
    completed = observation.get("completed_utc")
    if completed is None or _utc(completed) > _utc(job["deadline_utc"]):
        raise ExecutionError("review_application_worker_expired")
    return job["application_deadline_utc"]
