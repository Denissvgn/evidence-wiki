#!/usr/bin/env python3
"""Bounded host command receipts and read-only comparative cost projections.

Private signatures qualify retention only: they do not authenticate a provider,
model, bill, OS sandbox, complete host inventory, human effort, or savings.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools._integration_costs import (  # noqa: E402
    MAX_BYTES,
    METRICS,
    freeze_cost_plan,
    project_cost_report,
    validate_cost_event,
    validate_cost_plan,
)
from tools._journey_cases import canonical, digest  # noqa: E402
from tools._qualification_process import interruptible, stop_process  # noqa: E402
from tools.run_comparative_trials import (  # noqa: E402
    MAX_EVENT_BYTES,
    TrialLedger,
    checked_directory,
    command_manifest,
    publish_new,
    read_private,
)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _seconds(start, end):
    delta = datetime.fromisoformat(end.replace("Z", "+00:00")) - datetime.fromisoformat(start.replace("Z", "+00:00"))
    micros = (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
    if micros < 0:
        raise ValueError("cost_host_clock_order")
    return f"{micros // 1_000_000}.{micros % 1_000_000:06d}"


def _key(key):
    if type(key) is not bytes or not 32 <= len(key) <= 128:
        raise ValueError("cost_observer_key_invalid")


def _signed(value, key):
    value = {**value, "authentication": None}
    value["authentication"] = hmac.new(key, canonical(value), hashlib.sha256).hexdigest()
    return value


def _authenticated(path, key):
    value = json.loads(read_private(path, MAX_EVENT_BYTES))
    if (type(value) is not dict or type(value.get("authentication")) is not str
            or not hmac.compare_digest(_signed(value, key)["authentication"], value["authentication"])):
        raise ValueError("cost_receipt_authentication_failed")
    return value


def _paths(directory, work_root):
    directory, work_root = checked_directory(directory), checked_directory(work_root)
    if (work_root.is_relative_to(ROOT) or ROOT.is_relative_to(work_root)
            or work_root.is_relative_to(directory) or directory.is_relative_to(work_root)):
        raise ValueError("cost_workspaces_must_be_separate")
    return directory, work_root


def _identity(path):
    info = checked_directory(path).stat()
    return [info.st_dev, info.st_ino]


def _binding(plan, attempt_id, argv, *, pin=True):
    plan = validate_cost_plan(plan)
    attempt = next((row for row in plan["attempts"] if row["attempt_id"] == attempt_id), None)
    if attempt is None:
        raise ValueError("cost_attempt_not_declared")
    source = next(row for row in plan["sources"] if row["source_id"] == attempt["source_id"])
    if source["provenance"] != "host_observed":
        raise ValueError("cost_observer_source_not_host")
    if (type(argv) is not list or not 1 <= len(argv) <= 128
            or any(type(part) is not str or not part or len(part.encode()) > 8192 for part in argv)
            or len(canonical(argv)) > 65536):
        raise ValueError("cost_command_invalid")
    return plan, attempt, source, command_manifest(argv) if pin else None


def _within(source, at):
    start, end = source["collection_started_at"], source["collection_ended_at"]
    if start is None or not (datetime.fromisoformat(start.replace("Z", "+00:00")) <= datetime.fromisoformat(at)
                             <= datetime.fromisoformat(end.replace("Z", "+00:00"))):
        raise ValueError("cost_observation_outside_declared_window")


def _publish_bytes(path, raw):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _event(plan, attempt, status, start, end, receipt_hash, usage=None):
    metrics = dict.fromkeys(METRICS)
    if start is not None:
        metrics["elapsed_seconds"] = _seconds(start, end)
    metrics.update(usage or {})
    return validate_cost_event(plan, {"event_id": attempt["attempt_id"] + ".final",
        "attempt_id": attempt["attempt_id"], "plan_sha256": plan["plan_sha256"],
        "source_id": attempt["source_id"], "receipt_sha256": receipt_hash, "status": status,
        "started_at": start, "finished_at": end if start is not None else None, "metrics": metrics})


def observe_command(plan, attempt_id, argv, *, observer_key, directory, work_root,
                    timeout_seconds=60, input_bytes=b"", meter=None, meter_id=None, meter_timeout_seconds=5):
    """Observe explicitly selected argv once; retain failures without redispatch.

    Keep the returned receipt hash in a separate trusted custody boundary. A
    selected in-process meter may supply provider metrics but never host timing.
    Parent waiting for that callback is bounded; its background thread cannot be
    forcibly terminated and does not establish provider or OS containment.
    Same-user workers and process groups are not a malicious-worker sandbox.
    """
    budget_started = time.monotonic()
    reserved = _now()
    _key(observer_key)
    if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 7200 or type(input_bytes) is not bytes or len(input_bytes) > MAX_BYTES):
        raise ValueError("cost_command_budget_invalid")
    if (meter is None) != (meter_id is None) or (meter is not None and
            (not callable(meter) or type(meter_id) is not str or not 1 <= len(meter_id.encode()) <= 128)):
        raise ValueError("cost_meter_selection_invalid")
    if (type(meter_timeout_seconds) not in (int, float) or not math.isfinite(meter_timeout_seconds)
            or not 0 < meter_timeout_seconds <= 5):
        raise ValueError("cost_meter_timeout_invalid")
    plan, attempt, source, manifest = _binding(plan, attempt_id, argv)
    directory, work_root = _paths(directory, work_root)
    base = directory / attempt_id
    paths = {name: Path(str(base) + suffix) for name, suffix in
             (("intent", ".intent.json"), ("start", ".start.json"), ("final", ".receipt.json"),
              ("stdout", ".stdout"), ("stderr", ".stderr"))}
    if directory.is_relative_to(ROOT):
        for path in paths.values():
            subprocess.run(["git", "check-ignore", "-q", "--", str(path)], cwd=ROOT, check=True, capture_output=True)  # noqa: S603,S607 -- fixed read-only policy check.
    if any(path.exists() or path.is_symlink() for path in paths.values()):
        raise ValueError("cost_attempt_already_reserved")
    _within(source, reserved)
    window_left = (datetime.fromisoformat(source["collection_ended_at"].replace("Z", "+00:00"))
                   - datetime.fromisoformat(reserved)).total_seconds()
    # Keep closure capacity for the process-group cleanup and final retention.
    effective_timeout = min(timeout_seconds, window_left - 12)
    deadline = budget_started + max(0, effective_timeout)
    effective_timeout = max(0, deadline - time.monotonic())
    intent = _signed({"schema_version": "integration-cost-intent/v1", "plan_sha256": plan["plan_sha256"],
        "attempt": attempt, "source": source, "argv": argv, "command_manifest": manifest,
        "observer_id": hashlib.sha256(observer_key).hexdigest(), "directory": str(directory),
        "directory_identity": _identity(directory), "work_root": str(work_root),
        "work_root_identity": _identity(work_root), "reserved_at": reserved,
        "input_sha256": hashlib.sha256(input_bytes).hexdigest(), "timeout_seconds": str(timeout_seconds),
        "effective_timeout_seconds": str(max(0, effective_timeout)),
        "output_limit_bytes": MAX_EVENT_BYTES, "meter_id": meter_id,
        "meter_timeout_seconds": str(meter_timeout_seconds)}, observer_key)
    publish_new(paths["intent"], intent)
    process, started, failure, pending_interrupt = None, None, None, None
    status, usage, telemetry = "failed", {}, "unobserved"
    meter_work_unresolved = False
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    overflow, lock, readers = threading.Event(), threading.Lock(), []

    def drain(name, pipe):
        try:
            while chunk := pipe.read(65536):
                with lock:
                    remaining = MAX_EVENT_BYTES - sum(map(len, buffers.values()))
                    buffers[name].extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        overflow.set()
                        break
        finally:
            pipe.close()

    try:
        if effective_timeout <= 0:
            raise TimeoutError("cost_source_window_closure_capacity_unavailable")
        worker = work_root / attempt_id
        worker.mkdir(mode=0o700, exist_ok=False)
        checked_directory(worker)
        if command_manifest(argv) != manifest:
            raise ValueError("cost_command_changed")
        with tempfile.TemporaryFile() as stdin:
            stdin.write(input_bytes)
            stdin.seek(0)
            environment = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TMPDIR") if key in os.environ}
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            if time.monotonic() >= deadline:
                raise TimeoutError("cost_command_timeout")
            process = subprocess.Popen(argv, cwd=worker, env=environment, stdin=stdin,  # noqa: S603 -- explicitly selected frozen argv.
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            started = _now()
            publish_new(paths["start"], _signed({"intent_sha256": digest(intent), "started_at": started,
                "pid": process.pid}, observer_key))
            for name in buffers:
                reader = threading.Thread(target=drain, args=(name, getattr(process, name)), daemon=True)
                readers.append(reader)
                reader.start()
            while process.poll() is None:
                if overflow.is_set():
                    raise ValueError("cost_output_overflow")
                if time.monotonic() >= deadline:
                    raise TimeoutError("cost_command_timeout")
                time.sleep(0.01)
            status = "succeeded" if process.returncode == 0 else "failed"
            if time.monotonic() >= deadline:
                raise TimeoutError("cost_command_timeout")
    except BaseException as error:
        failure = type(error).__name__ + ": " + str(error)[:512]
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            status, pending_interrupt = "interrupted", error
        else:
            status = "failed"
    finally:
        if process is not None:
            try:
                stop_process(process)
            except Exception as error:
                status, failure = "failed", "cost_cleanup_failed: " + type(error).__name__
        for reader in readers:
            reader.join(timeout=1)
    reader_work_unresolved = any(reader.is_alive() for reader in readers)
    if reader_work_unresolved:
        status, failure = "failed", "cost_output_reader_unresolved"
    with lock:
        retained_buffers = {name: bytes(raw) for name, raw in buffers.items()}
    if overflow.is_set():
        status, failure = "failed", "cost_output_overflow"
    try:
        manifest_after = command_manifest(argv)
    except Exception:
        manifest_after = None
    if manifest_after != manifest:
        status, failure = "failed", "cost_command_changed"
    if _identity(directory) != intent["directory_identity"] or _identity(work_root) != intent["work_root_identity"]:
        raise ValueError("cost_observer_directory_changed")
    for name, raw in retained_buffers.items():
        _publish_bytes(paths[name], raw)
    finished = _now()
    if meter is not None:
        delivery = queue.Queue(maxsize=1)
        meter_context = {"attempt_id": attempt_id, "started_at": started, "finished_at": finished,
                         "exit_code": None if process is None else process.returncode}
        meter_validation_finished, meter_validation_status = finished, status

        def collect_meter():
            try:
                observed = meter(dict(meter_context))
                allowed = set(METRICS) - {"elapsed_seconds"}
                if type(observed) is not dict or not set(observed) <= allowed:
                    raise ValueError("cost_meter_metrics_invalid")
                validated = _event(plan, attempt, meter_validation_status, reserved, meter_validation_finished, "0" * 64, observed)
                delivery.put_nowait(("ok", {key: validated["metrics"][key] for key in sorted(allowed)}))
            except BaseException as error:
                delivery.put_nowait(("error", type(error).__name__))

        wait_budget = min(meter_timeout_seconds, deadline - time.monotonic(),
                          (datetime.fromisoformat(source["collection_ended_at"].replace("Z", "+00:00"))
                           - datetime.fromisoformat(finished)).total_seconds() - 2)
        try:
            if wait_budget <= 0:
                raise TimeoutError("cost_meter_deadline_exhausted")
            threading.Thread(target=collect_meter, daemon=True).start()
            outcome, observed = delivery.get(timeout=wait_budget)
            if outcome == "ok":
                usage = observed
                telemetry = "selected_callable; provider authenticity is outside retention scope"
            else:
                telemetry = "unknown: " + observed
                if observed in {"KeyboardInterrupt", "SystemExit"}:
                    status = "interrupted"
                    pending_interrupt = KeyboardInterrupt() if observed == "KeyboardInterrupt" else SystemExit()
        except queue.Empty:
            usage, telemetry, meter_work_unresolved = {}, "unknown: meter_callback_timeout", True
        except BaseException as error:
            usage, telemetry = {}, "unknown: " + type(error).__name__
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                status, pending_interrupt = "interrupted", error
                meter_work_unresolved = True
    finished = _now()
    coverage_gap = None
    try:
        _within(source, finished)
    except ValueError:
        status, failure = "failed", "cost_observation_outside_declared_window"
        coverage_gap = "Final retention fell outside the frozen source window; no cost event projected."
    if _identity(directory) != intent["directory_identity"]:
        raise ValueError("cost_observer_directory_changed")
    if _authenticated(paths["intent"], observer_key) != intent:
        raise ValueError("cost_intent_changed")
    for name, raw in retained_buffers.items():
        if read_private(paths[name], MAX_EVENT_BYTES) != raw:
            raise ValueError("cost_output_changed")
    receipt = _signed({"schema_version": "integration-cost-receipt/v1", "intent_sha256": digest(intent),
        "started_at": started, "finished_at": finished, "status": status, "failure": failure,
        "exit_code": None if process is None else process.returncode, "command_manifest_after": manifest_after,
        "streams": {name: {"sha256": hashlib.sha256(raw).hexdigest(), "retained_bytes": len(raw)}
                    for name, raw in retained_buffers.items()}, "output_overflow": overflow.is_set(),
        "output_reader_work_unresolved": reader_work_unresolved,
        "meter_id": meter_id, "telemetry_qualification": telemetry, "meter_work_unresolved": meter_work_unresolved,
        "usage": usage}, observer_key)
    publish_new(paths["final"], receipt)
    result = {"receipt": receipt, "receipt_sha256": digest(receipt),
              "event": _event(plan, attempt, status, reserved, finished, digest(receipt), usage) if coverage_gap is None else None,
              "coverage_gap": coverage_gap, "current_command_manifest_matches": manifest_after == manifest,
              "qualification": "host-private signed command retention; reservation-to-final host interval; external latest receipt custody required"}
    if pending_interrupt is not None:
        pending_interrupt.cost_receipt = result
        raise pending_interrupt
    return result


def read_command_receipt(plan, attempt_id, argv, *, observer_key, directory, work_root, expected_receipt_sha256):
    """Authenticate a final receipt using the caller's externally held exact hash."""
    _key(observer_key)
    plan, attempt, source, _ = _binding(plan, attempt_id, argv, pin=False)
    directory, work_root = _paths(directory, work_root)
    base = directory / attempt_id
    intent = _authenticated(Path(str(base) + ".intent.json"), observer_key)
    if (intent.get("plan_sha256") != plan["plan_sha256"] or intent.get("attempt") != attempt
            or intent.get("source") != source or intent.get("argv") != argv
            or intent.get("directory") != str(directory)
            or intent.get("work_root") != str(work_root) or intent.get("directory_identity") != _identity(directory)
            or intent.get("work_root_identity") != _identity(work_root)):
        raise ValueError("cost_receipt_binding_changed")
    receipt = _authenticated(Path(str(base) + ".receipt.json"), observer_key)
    if type(expected_receipt_sha256) is not str or digest(receipt) != expected_receipt_sha256:
        raise ValueError("cost_trusted_receipt_hash_mismatch")
    if receipt.get("intent_sha256") != digest(intent):
        raise ValueError("cost_receipt_binding_changed")
    if receipt["started_at"] is not None:
        start = _authenticated(Path(str(base) + ".start.json"), observer_key)
        if start.get("intent_sha256") != digest(intent) or start.get("started_at") != receipt["started_at"]:
            raise ValueError("cost_start_binding_changed")
    for name, stream in receipt["streams"].items():
        raw = read_private(Path(str(base) + "." + name), MAX_EVENT_BYTES)
        if len(raw) != stream["retained_bytes"] or hashlib.sha256(raw).hexdigest() != stream["sha256"]:
            raise ValueError("cost_output_changed")
    if set(receipt["streams"]) != {"stdout", "stderr"} or sum(
            row["retained_bytes"] for row in receipt["streams"].values()) > MAX_EVENT_BYTES:
        raise ValueError("cost_output_overflow")
    if _identity(directory) != intent["directory_identity"] or _identity(work_root) != intent["work_root_identity"]:
        raise ValueError("cost_receipt_binding_changed")
    coverage_gap = None
    try:
        _within(source, receipt["finished_at"])
    except ValueError:
        coverage_gap = "Final retention fell outside the frozen source window; no cost event projected."
    try:
        current_matches = command_manifest(argv) == intent["command_manifest"]
    except Exception:
        current_matches = False
    return {"receipt": receipt, "receipt_sha256": digest(receipt),
            "event": _event(plan, attempt, receipt["status"], intent["reserved_at"], receipt["finished_at"],
                            digest(receipt), receipt["usage"]) if coverage_gap is None else None,
            "coverage_gap": coverage_gap, "current_command_manifest_matches": current_matches,
            "qualification": "host-private signed command retention; reservation-to-final host interval; external latest receipt custody required"}


def from_comparative_ledger(ledger, scope_id, repeat_use_count=None):
    """Read a live signed ledger, preserving retries, missing slots and unknowns.

    Generic process counters are orchestration, including legacy model_calls=1.
    They never establish provider usage or a price. This is not a pooled pilot.
    """
    if type(ledger) is not TrialLedger or ledger._expected_head is None:
        raise ValueError("cost_comparative_live_ledger_required")
    read_private(ledger.directory / "ledger.lock", 4096)
    with ledger.locked():
        if (ledger.directory / "pending.json").exists() or (ledger.directory / "pending.json").is_symlink():
            raise ValueError("cost_comparative_pending_retention_unresolved")
        if json.loads(read_private(ledger.directory / "plan.json", MAX_EVENT_BYTES)) != ledger.binding:
            raise ValueError("cost_comparative_binding_changed")
        rows = ledger.events()
        attempts = ledger.attempts()
        qualification = ledger.report()
        head = digest(ledger._head())
        comparative = ledger.plan
    times = [row["at"] for row in rows]
    for row in attempts:
        times.extend(at for at in (row["reserved_at"], row["started_at"], row["finished_at"]) if at is not None)
    grades = [row for row in rows if row["kind"] == "judgments" and row["data"]["grading_receipt"] is not None]
    if grades:
        grade = grades[0]["data"]["grading_receipt"]
        times.extend([grade["started_at"], grade["completed_at"]])
    chronological = sorted(times, key=lambda at: datetime.fromisoformat(at.replace("Z", "+00:00")))
    source = {"source_id": "comparison-host", "provenance": "host_observed", "kind": "automated",
              "origin": "Read-only verified comparative command ledger", "collection_started_at": chronological[0] if times else None,
              "collection_ended_at": chronological[-1] if times else None, "coverage": "partial", "authentication_ref": head}
    inventory, observations = [], []

    def add(identifier, phase, row=None, start=None, end=None, status="unknown", receipt_hash=None):
        attempt = {"attempt_id": identifier, "phase": phase,
                   "allocation": "repeated" if phase in {"operation", "review", "evidence_update"} else "one_time",
                   "source_id": source["source_id"], "required_metrics": list(METRICS)}
        inventory.append(attempt)
        if row is not None:
            observations.append((attempt, status, start if end is not None else None, end, receipt_hash))

    for slot in comparative["schedule"]:
        matching = [row for row in attempts if row["slot_id"] == slot["slot_id"]]
        if not matching:
            add("slot." + slot["slot_id"] + ".missing", "operation")
        for row in matching:
            status = row["status"] if row["status"] not in {"reserved", "running"} else "unknown"
            add("trial." + row["attempt_id"], "operation", row, row["started_at"], row["finished_at"], status, digest(row))
    if grades:
        grade = grades[0]["data"]["grading_receipt"]
        add("grading", "review", grade, grade["started_at"], grade["completed_at"], "succeeded", digest(grades[0]))
    else:
        add("review.missing", "review")
    for phase in ("integration", "setup", "repair", "qualification", "evidence_update"):
        add(phase + ".missing", phase)
    plan = freeze_cost_plan(scope_id=scope_id, binding={"comparative_plan_sha256": comparative["plan_sha256"],
        "comparative_retention_head_sha256": head}, sources=[source], attempts=inventory, repeat_use_count=repeat_use_count)
    events = [_event(plan, attempt, status, start, end, receipt_hash) for attempt, status, start, end, receipt_hash in observations]
    return {"plan": plan, "events": events, "report": project_cost_report(plan, events),
            "orchestration_diagnostics": [{"attempt_id": row["attempt_id"], "receipt_kind": row["receipt"]["kind"],
                "retained_counters": {key: row["receipt"][key] for key in ("model_calls", "tool_calls", "tokens", "cost")}}
                for row in attempts],
            "retention_head_sha256": head, "provenance_qualification": {
                "retention": "fresh HMAC verification under host lock and externally retained latest head",
                "provider_usage": "unknown; generic process receipts and legacy orchestration counters suppressed",
                "timing": "UTC confirmed-start to finished-retention interval; not worker or provider latency",
                "grading": qualification["grading_invocation_qualification"],
                "coverage": "partial command ledger; integration and other unobserved phases remain missing"}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("observe", "read", "report", "comparative"))
    parser.add_argument("--plan", required=True)
    parser.add_argument("--directory")
    parser.add_argument("--work-root")
    parser.add_argument("--observer-key")
    parser.add_argument("--attempt-id")
    parser.add_argument("--command", help="Explicit executable argv as an inert JSON array.")
    parser.add_argument("--timeout-seconds", type=float, default=60)
    parser.add_argument("--expected-receipt")
    parser.add_argument("--expected-head")
    parser.add_argument("--scope-id")
    parser.add_argument("--events")
    args = parser.parse_args(argv)
    plan_path = Path(args.plan).absolute()
    checked_directory(plan_path.parent)
    plan = json.loads(read_private(plan_path, MAX_BYTES))
    if args.action == "report":
        if not args.events:
            parser.error("report requires --events")
        events_path = Path(args.events).absolute()
        checked_directory(events_path.parent)
        result = project_cost_report(plan, json.loads(read_private(events_path, MAX_BYTES)))
    else:
        if not all((args.observer_key, args.directory, args.work_root)):
            parser.error("host actions require --observer-key, --directory and --work-root")
        key_path = Path(args.observer_key).absolute()
        checked_directory(key_path.parent)
        work = Path(args.work_root).absolute()
        if key_path.is_relative_to(work):
            parser.error("observer key must be separate from worker directory")
        key = read_private(key_path, 128)
        if args.action == "comparative":
            if not args.expected_head or not args.scope_id:
                parser.error("comparative requires --expected-head and --scope-id")
            owner = TrialLedger(args.directory, plan, observer_key=key, work_root=args.work_root,
                                expected_head=args.expected_head, read_only=True)
            result = from_comparative_ledger(owner, args.scope_id)
        else:
            if not args.attempt_id or not args.command:
                parser.error("command actions require --attempt-id and --command")
            options = {"observer_key": key, "directory": args.directory, "work_root": args.work_root}
            command = json.loads(args.command)
            if args.action == "observe":
                result = observe_command(plan, args.attempt_id, command, timeout_seconds=args.timeout_seconds, **options)
            else:
                result = read_command_receipt(plan, args.attempt_id, command, expected_receipt_sha256=args.expected_receipt, **options)
    print(json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False))
    return 0


if __name__ == "__main__":
    with interruptible():
        raise SystemExit(main())
