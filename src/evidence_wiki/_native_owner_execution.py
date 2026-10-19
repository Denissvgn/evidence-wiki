"""Closed host execution bounds and original-window fixture authority."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

_SCHEMA = "native-owner-execution-policy/v1"


def execution_policy(profile="legacy"):
    """Return a fresh closed declaration; extended execution requires opt-in."""
    if type(profile) is not str or profile not in {"legacy", "extended"}:
        raise ValueError("native_execution_profile")
    extended = profile == "extended"
    return {"schema_version": _SCHEMA, "profile": profile,
            "slot_seconds": 1300 if extended else 900,
            "route_observer_seconds": 3900 if extended else 2700,
            "decision_seconds": 900, "replay_seconds": 900,
            "study_seconds": 21600 if extended else 14400,
            "native_run_seconds": 14400, "authority_seconds": 14400 if extended else 7200,
            "owner_pass_seconds": 120, "witness_seconds": 30, "cleanup_seconds": 60}


def validate_execution_policy(value):
    """Accept exactly one reviewed declaration, including exact JSON types."""
    if type(value) is not dict:
        raise ValueError("native_execution_policy")
    expected = execution_policy(value.get("profile"))
    try:
        actual = json.dumps(value, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):
        raise ValueError("native_execution_policy") from None
    if actual != json.dumps(expected, sort_keys=True, allow_nan=False):
        raise ValueError("native_execution_policy")
    if expected["profile"] == "extended":
        total = 4 * expected["route_observer_seconds"] + expected["decision_seconds"] + expected["replay_seconds"]
        primary = 3 * expected["route_observer_seconds"] + expected["decision_seconds"] + expected["replay_seconds"]
        if (total > expected["study_seconds"] or primary >= expected["authority_seconds"]
                or expected["route_observer_seconds"] != 3 * expected["slot_seconds"]):
            raise ValueError("native_execution_schedule")
    return expected


def _utc(value):
    try:
        instant = datetime.fromisoformat(value) if type(value) is str else value
        if not isinstance(instant, datetime) or instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError
        return instant.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("native_execution_window") from None


def fixture_window(value, frozen_at, expires_at, *, current_utc=None):
    """Bound fresh authority by the original frozen study without renewal."""
    selected = validate_execution_policy(value)
    start, end = _utc(frozen_at), _utc(expires_at)
    instant = datetime.now(timezone.utc) if current_utc is None else _utc(current_utc)
    if end - start != timedelta(seconds=selected["study_seconds"]) or not start <= instant < end:
        raise ValueError("native_execution_window")
    until = min(instant + timedelta(seconds=selected["authority_seconds"]), end)
    seconds = min(selected["native_run_seconds"], int((until - instant).total_seconds()))
    if seconds < 1:
        raise ValueError("native_execution_authority_exhausted")
    return until.isoformat(), seconds


def fixture_authority(value, frozen_at, expires_at, *, current_utc=None):
    """Record the initial host instant so reopening cannot slide the expiry."""
    instant = datetime.now(timezone.utc) if current_utc is None else _utc(current_utc)
    until, seconds = fixture_window(value, frozen_at, expires_at, current_utc=instant)
    return {"execution_policy": validate_execution_policy(value), "study_frozen_at": frozen_at,
            "study_expires_at": expires_at, "provisioned_at": instant.isoformat(),
            "expires_at": until, "run_seconds": seconds}


def validate_fixture_authority(value, *, current_utc=None):
    """Validate initial authority and current rights without issuing an extension."""
    fields = {"execution_policy", "study_frozen_at", "study_expires_at", "provisioned_at", "expires_at", "run_seconds"}
    if type(value) is not dict or set(value) != fields:
        raise ValueError("native_fixture_authority")
    expected = fixture_authority(value["execution_policy"], value["study_frozen_at"], value["study_expires_at"],
                                 current_utc=value["provisioned_at"])
    if (type(value["run_seconds"]) is not int or value != expected):
        raise ValueError("native_fixture_authority")
    instant = datetime.now(timezone.utc) if current_utc is None else _utc(current_utc)
    if not _utc(value["provisioned_at"]) <= instant < _utc(value["expires_at"]):
        raise ValueError("native_fixture_authority_expired")
    return expected
