"""Closed inert pending-work receipts over immutable canonical job leases.

The clock is supplied by the trusted owner. A codec validates neither an
executor nor external completion; references and terminal labels confer no
authority. Pure helpers never manufacture completion or cancellation.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from .errors import EvidenceWikiError
from .mutation_contracts import _shape, canonical, detached, digest
from .workflow_contracts import JOB, MAX_LEASE_SECONDS, WorkflowContractError
from .workflow_contracts import validate_document as workflow_document

PENDING = "research-pending-work/v1"
MAX_BYTES = 16_384
STATES = ("reserved", "expired", "cancellation_requested", "cancelled", "completed", "unknown")
FALLBACK_REASONS = ("executor_unavailable", "unobserved_external_completion", "cancellation_unconfirmed")
_TERMINAL = frozenset({"expired", "cancelled", "completed", "unknown"})
_OCCUPIED = frozenset({"reserved", "expired", "cancellation_requested", "unknown"})
_UTC = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|\+00:00)$(?![\s\S])"


def refuse(reason, field="/"):
    raise WorkflowContractError("PENDING_WORK_" + reason, "Pending work contract refused the input.", recoverable=False,
        details={"field": field}, remediation="Preserve the immutable job lease and occupied reservation; use current canonical observations.") from None


def _clock(value, field):
    if type(value) is not str or len(value) > 32 or re.fullmatch(_UTC, value) is None:
        refuse("CLOCK_INVALID", field)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, OverflowError):
        refuse("CLOCK_INVALID", field)


def schemas():
    identifier = {"type": "string", "minLength": 1, "maxLength": 256,
                  "pattern": r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$(?![\s\S])"}
    hashed = {"type": "string", "pattern": r"^sha256:[a-f0-9]{64}$(?![\s\S])"}
    utc = {"type": "string", "maxLength": 32, "pattern": _UTC}
    nullable = lambda shape: {"anyOf": [shape, {"type": "null"}]}
    fields = {"schema_version": {"const": PENDING}, "run_id": identifier, "binding_id": hashed,
        "request_id": identifier, "job_id": identifier, "job_sha256": hashed,
        **{name: utc for name in ("issued_utc", "job_deadline_utc", "run_deadline_utc", "effective_deadline_utc", "last_observed_utc")},
        "state": {"enum": list(STATES)}, "fallback_reason": nullable({"enum": ["deadline_expired", *FALLBACK_REASONS]}),
        "completion_ref": nullable(hashed), "cancellation_requested_utc": nullable(utc),
        "reservation_occupied": {"type": "boolean"}, "terminal": {"type": "boolean"},
        "authorized": {"const": False}, "evidence_accepted": {"const": False}}
    return {PENDING: {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
        "properties": fields, "required": list(fields), "additionalProperties": False}}


def schema_document(name):
    if type(name) is not str or name != PENDING:
        refuse("VERSION_UNSUPPORTED", "/schema_version")
    return schemas()[name]


def validate_document(name, value):
    """Return detached canonical UTC data; nothing here authenticates a receipt."""
    try:
        shape = schema_document(name)
        value = detached(value)
        if len(canonical(value)) > MAX_BYTES:
            refuse("LIMIT")
        _shape(value, shape)
        clocks = {key: _clock(value[key], "/" + key) for key in
            ("issued_utc", "job_deadline_utc", "run_deadline_utc", "effective_deadline_utc", "last_observed_utc")}
        if not clocks["issued_utc"] < clocks["job_deadline_utc"]:
            refuse("LEASE_INVALID", "/job_deadline_utc")
        if clocks["job_deadline_utc"] - clocks["issued_utc"] > timedelta(seconds=MAX_LEASE_SECONDS):
            refuse("LEASE_INVALID", "/job_deadline_utc")
        if clocks["effective_deadline_utc"] != min(clocks["job_deadline_utc"], clocks["run_deadline_utc"]):
            refuse("LEASE_INVALID", "/effective_deadline_utc")
        if clocks["last_observed_utc"] < clocks["issued_utc"]:
            refuse("CLOCK_REGRESSED", "/last_observed_utc")
        state = value["state"]
        if value["terminal"] != (state in _TERMINAL) or value["reservation_occupied"] != (state in _OCCUPIED):
            refuse("STATE_INVALID", "/state")
        if state in {"reserved", "cancellation_requested"} and clocks["last_observed_utc"] >= clocks["effective_deadline_utc"]:
            refuse("STATE_INVALID", "/state")
        if state == "expired" and (clocks["last_observed_utc"] < clocks["effective_deadline_utc"] or value["fallback_reason"] != "deadline_expired"):
            refuse("STATE_INVALID", "/state")
        if state == "unknown" and value["fallback_reason"] not in FALLBACK_REASONS:
            refuse("STATE_INVALID", "/fallback_reason")
        if state not in {"expired", "unknown"} and value["fallback_reason"] is not None:
            refuse("STATE_INVALID", "/fallback_reason")
        if (state in {"completed", "cancelled"}) != (value["completion_ref"] is not None):
            refuse("STATE_INVALID", "/completion_ref")
        cancelled_at = value["cancellation_requested_utc"]
        if state in {"cancellation_requested", "cancelled"} and cancelled_at is None:
            refuse("STATE_INVALID", "/cancellation_requested_utc")
        if state == "reserved" and cancelled_at is not None:
            refuse("STATE_INVALID", "/cancellation_requested_utc")
        if cancelled_at is not None:
            cancelled = _clock(cancelled_at, "/cancellation_requested_utc")
            if not clocks["issued_utc"] <= cancelled <= clocks["last_observed_utc"] or cancelled >= clocks["effective_deadline_utc"]:
                refuse("STATE_INVALID", "/cancellation_requested_utc")
            value["cancellation_requested_utc"] = cancelled.isoformat()
        for key, observed in clocks.items():
            value[key] = observed.isoformat()
        return value
    except WorkflowContractError:
        raise
    except EvidenceWikiError:
        refuse("INVALID")
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError):
        refuse("INVALID")


def encode_document(name, value):
    return canonical(validate_document(name, value))


def decode_document(name, raw):
    schema_document(name)
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_BYTES:
        refuse("LIMIT")
    try:
        def unique(pairs):
            value = {}
            for key, child in pairs:
                if key in value:
                    refuse("INVALID")
                value[key] = child
            return value
        return validate_document(name, json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
            parse_constant=lambda _value: refuse("INVALID")))
    except WorkflowContractError:
        raise
    except (ValueError, UnicodeError, RecursionError):
        refuse("INVALID")


def create(job, *, run_id, binding_id, request_id, run_deadline_utc, observed_utc):
    """Describe a canonical job without extending its live or historical lease."""
    job = workflow_document(JOB, job)
    observed = _clock(observed_utc, "/observed_utc")
    deadline = min(_clock(job["deadline_utc"], "/job_deadline_utc"), _clock(run_deadline_utc, "/run_deadline_utc"))
    expired = observed >= deadline
    return validate_document(PENDING, {"schema_version": PENDING, "run_id": run_id, "binding_id": binding_id,
        "request_id": request_id, "job_id": job["id"], "job_sha256": digest(job), "issued_utc": job["issued_utc"],
        "job_deadline_utc": job["deadline_utc"], "run_deadline_utc": run_deadline_utc,
        "effective_deadline_utc": deadline.isoformat(), "last_observed_utc": observed.isoformat(),
        "state": "expired" if expired else "reserved", "fallback_reason": "deadline_expired" if expired else None,
        "completion_ref": None, "cancellation_requested_utc": None, "reservation_occupied": True, "terminal": expired,
        "authorized": False, "evidence_accepted": False})


def observe(receipt, *, observed_utc):
    """Observe monotonic UTC and expire waiting work; never infer completion."""
    value = validate_document(PENDING, receipt)
    observed = _clock(observed_utc, "/observed_utc")
    if observed < _clock(value["last_observed_utc"], "/last_observed_utc"):
        refuse("CLOCK_REGRESSED", "/observed_utc")
    value["last_observed_utc"] = observed.isoformat()
    if value["state"] not in _TERMINAL and observed >= _clock(value["effective_deadline_utc"], "/effective_deadline_utc"):
        value.update(state="expired", fallback_reason="deadline_expired", terminal=True, reservation_occupied=True)
    return validate_document(PENDING, value)


def request_cancellation(receipt, *, observed_utc):
    """Record only a cancellation request while the immutable lease remains live."""
    value = observe(receipt, observed_utc=observed_utc)
    if value["state"] == "reserved":
        value.update(state="cancellation_requested", cancellation_requested_utc=value["last_observed_utc"])
    return validate_document(PENDING, value)


def fallback(receipt, *, observed_utc, reason):
    """Return an automatic terminal unknown while retaining the reservation."""
    if type(reason) is not str or reason not in FALLBACK_REASONS:
        refuse("REASON_INVALID", "/reason")
    value = observe(receipt, observed_utc=observed_utc)
    if value["state"] not in _TERMINAL:
        value.update(state="unknown", fallback_reason=reason, terminal=True, reservation_occupied=True)
    return validate_document(PENDING, value)


def validate_transition(reservation, receipt):
    """Check initial-profile control lineage; references cannot complete work."""
    previous = validate_document(PENDING, reservation)
    candidate = validate_document(PENDING, receipt)
    mutable = {"last_observed_utc", "state", "fallback_reason", "completion_ref", "cancellation_requested_utc", "terminal"}
    if (candidate["state"] not in {"expired", "unknown"} or not previous["reservation_occupied"]
            or not candidate["reservation_occupied"]
            or any(candidate[key] != value for key, value in previous.items() if key not in mutable)
            or _clock(candidate["last_observed_utc"], "/last_observed_utc") < _clock(previous["last_observed_utc"], "/last_observed_utc")
            or previous["terminal"] and candidate != previous):
        refuse("TRANSITION_INVALID", "/receipt")
    return candidate
