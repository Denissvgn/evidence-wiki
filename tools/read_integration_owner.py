#!/usr/bin/env python3
"""Read one host's original evidence in a parent-owned isolated process."""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import sys
from pathlib import Path

if __name__ == "__main__":
    signal.pthread_sigmask(signal.SIG_SETMASK, set())
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools._integration_improvement import validate_plan  # noqa: E402
from tools._integration_improvement_owners import host_readers  # noqa: E402
from tools._integration_owner_pass import MAX_REQUEST_BYTES, _frame, source_manifest  # noqa: E402
from tools._release_decision import bounded_json, digest  # noqa: E402
from tools.verify_acceptance_paths import candidate_manifest  # noqa: E402


def _request(raw):
    if len(raw) > MAX_REQUEST_BYTES:
        raise ValueError("integration_owner_binding")
    request = bounded_json(json.loads(raw))
    fields = {"schema_version", "pass_nonce", "plan", "plan_sha256", "originals", "originals_sha256",
              "source_manifest", "host_id", "slot_ids"}
    if (type(request) is not dict or set(request) != fields
            or request["schema_version"] != "integration-owner-request/v1"
            or type(request["pass_nonce"]) is not str or not re.fullmatch(r"[a-f0-9]{32}", request["pass_nonce"])):
        raise ValueError("integration_owner_binding")
    plan = validate_plan(request["plan"], request["plan_sha256"])
    slots = sorted(slot["slot_id"] for slot in plan["slots"] if slot["host_id"] == request["host_id"])
    if (len(slots) != 3 or request["slot_ids"] != slots or type(request["originals"]) is not dict
            or set(request["originals"]) != {request["host_id"]}
            or digest(request["originals"]) != request["originals_sha256"]
            or request["source_manifest"] != source_manifest()
            or plan["scope"].get("owner_read_controller_sha256") != digest(request["source_manifest"])
            or type(plan["scope"].get("owner_read_workers")) is not int
            or not 1 <= plan["scope"]["owner_read_workers"] <= 3
            or digest(candidate_manifest(ROOT)) != plan["candidate_sha256"]):
        raise ValueError("integration_current_candidate_changed")
    return request


def read_group(request, emit):
    """Reuse unchanged native readers serially, poisoning a failed host pass."""
    selected = request["originals"][request["host_id"]]
    control = Path(selected["path"]).parent / "fixture/fixture-control"
    registry = {reader.slot_id: reader for reader in host_readers(request["plan"], request["originals"],
                                                                expected_control_root=control)}
    poisoned = False
    for sid in request["slot_ids"]:
        if poisoned:
            emit(_frame(request, sid, error=OSError()))
            continue
        try:
            native = registry[sid].read()
            emit(_frame(request, sid, evidence=native))
        except Exception as error:
            emit(_frame(request, sid, error=error))
            poisoned = True


def main():
    request = None
    output = sys.stdout.buffer
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError("integration_owner_binding")
        request = bounded_json(json.loads(raw))
        request = _request(raw)
        def emit(frame):
            output.write(frame)
            output.flush()
        # Factory printing and unexpected runtime diagnostics are private to
        # this worker; only the closed per-slot grammar crosses its stdout.
        with open(os.devnull, "w", encoding="utf-8") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            read_group(request, emit)
        return 0
    except Exception as error:
        # Validation failures are still original errors from the fixed worker.
        # Emit no unbounded input, diagnostic text or worker-selected authority.
        if (type(request) is dict and type(request.get("host_id")) is str
                and type(request.get("slot_ids")) is list and len(request["slot_ids"]) == 3
                and all(type(sid) is str for sid in request["slot_ids"])):
            try:
                for sid in request["slot_ids"]:
                    output.write(_frame(request, sid, error=error))
                output.flush()
            except Exception:
                return 2
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
