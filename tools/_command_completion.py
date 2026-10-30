"""Opt-in completion of an exact, parent-owned command process birth."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from datetime import datetime, timezone

STAGES = frozenset({"run-primary-direct", "run-primary-current_caller", "run-primary-scoped_mcp",
                    "run-audit-direct", "run-audit-current_caller", "run-audit-scoped_mcp", "decide", "replay"})
CERTIFICATE_FIELDS = frozenset({"schema_version", "worker_pid", "birth_id", "plan_sha256", "stage",
                                "leader_reaped", "group_absent", "returncode", "observed_at"})


def _require(value):
    if not value:
        raise ValueError("cost_producer_completion_invalid")


def _hex(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def validate_scope(scope):
    _require(type(scope) is dict and set(scope) == {"plan_sha256", "stage"})
    _require(_hex(scope["plan_sha256"]) and type(scope["stage"]) is str and scope["stage"] in STAGES)
    return dict(scope)


def _utc(value):
    _require(type(value) is str)
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        _require(instant.utcoffset() is not None and instant.utcoffset().total_seconds() == 0)
    except (ValueError, OverflowError) as error:
        raise ValueError("cost_producer_completion_invalid") from error
    return instant


def validate_completion(certificate, *, scope, start, receipt, source):
    """Validate lifecycle data only after its containing records authenticate."""
    validate_scope(scope)
    _require(type(start) is dict and set(start) == {"schema_version", "intent_sha256", "started_at", "pid",
                                                   "birth_id", "native_child_scope", "authentication"})
    _require(start["schema_version"] == "integration-cost-start/v2" and start["native_child_scope"] == scope)
    _require(type(start["pid"]) is int and start["pid"] > 0 and _hex(start["birth_id"]))
    _require(type(receipt["exit_code"]) is int or receipt["exit_code"] is None)
    began, finished = _utc(start["started_at"]), _utc(receipt["finished_at"])
    _require(_utc(source["collection_started_at"]) <= began <= finished)
    if certificate is None:
        _require(receipt["status"] != "succeeded")
        return
    _require(type(certificate) is dict and set(certificate) == CERTIFICATE_FIELDS)
    _require(certificate["schema_version"] == "native-producer-completion/v1")
    _require(type(certificate["worker_pid"]) is int and certificate["worker_pid"] == start["pid"])
    _require(_hex(certificate["birth_id"]) and certificate["birth_id"] == start["birth_id"])
    _require(certificate["plan_sha256"] == scope["plan_sha256"] and certificate["stage"] == scope["stage"])
    _require(certificate["leader_reaped"] is True and certificate["group_absent"] is True)
    _require(type(certificate["returncode"]) is int and certificate["returncode"] == receipt["exit_code"])
    _require(began <= _utc(certificate["observed_at"]) <= finished <= _utc(source["collection_ended_at"]))


class CommandCompletion:
    """Hold the actual Popen; never inspect a completed numeric identity again."""

    def __init__(self, process, scope, birth_id):
        self.process = process
        self.scope = validate_scope(scope)
        self.birth_id = birth_id
        self.certificate = None
        self.closed = False

    def close(self):
        if self.closed:
            return self.certificate
        self.closed = True
        process = self.process
        deadline = time.monotonic() + 5
        # A reaped leader never authorizes a signal, even if its old group is
        # present. Under Popen's own wait lock an unreaped leader pins its PID.
        with process._waitpid_lock:
            if process.returncode is None:
                try:
                    if os.getpgid(process.pid) != process.pid:
                        return None
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError:
                    return None
        if process.returncode is None:
            try:
                # Leave two seconds for the existing bounded read-only group probe.
                process.wait(timeout=max(0, deadline - time.monotonic() - 2))
            except (OSError, subprocess.TimeoutExpired):
                return None
        if type(process.returncode) is not int:
            return None
        # Lazy import avoids the owner adapter's observer import cycle. This
        # probe is read-only; only literal False is evidence of absence.
        from tools._integration_owner_pass import _group_alive

        try:
            absent = _group_alive(process) is False
        except Exception:
            absent = False
        if not absent:
            return None
        self.certificate = {"schema_version": "native-producer-completion/v1", "worker_pid": process.pid,
            "birth_id": self.birth_id, **self.scope, "leader_reaped": True, "group_absent": True,
            "returncode": process.returncode, "observed_at": datetime.now(timezone.utc).isoformat()}
        return self.certificate
