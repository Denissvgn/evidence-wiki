#!/usr/bin/env python3
"""Run a finite frozen acceptance catalog and retain host-observed reproduction evidence."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import secrets
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools._acceptance_protocol import (  # noqa: E402
    MAX_FILES,
    MAX_INPUT_BYTES,
    MAX_RECORD_BYTES,
    bounded_json,
    digest,
    freeze_manifest,
    grade_evidence,
    hash_file,
    policy,
    validate_cases,
    validate_manifest,
)
from tools._integration_costs import METRICS, freeze_cost_plan, validate_cost_plan  # noqa: E402
from tools._qualification_process import interruptible, positive_seconds  # noqa: E402
from tools.measure_integration_costs import (  # noqa: E402
    _authenticated,
    _signed,
    observe_command,
    read_command_receipt,
)
from tools.run_comparative_trials import checked_directory, publish_new, read_private  # noqa: E402

CAPSULE_TREES = ("src", "tests", "tools", "examples", "workspace-template", "domain-packs", "orchestrator")
CAPSULE_FILES = ("pyproject.toml", "README.md", "LICENSE", "CHANGELOG.md", "THIRD_PARTY_NOTICES.md")
EXCLUDED = {"__pycache__", ".pytest_cache", ".ruff_cache", ".git", ".venv", "reports", ".llm-wiki",
            ".aws", ".codex", ".agents", "llm_wiki"}
LIMITATIONS = [
    "Finite synthetic challenge conformance is not semantic truth, production security, billing, or release qualification.",
    "Expected owner refusal passes only its challenge; it grants no native authorization or successful answer.",
    "Host-private signatures qualify retention only. Caller must retain the latest report hash and observer key outside workers.",
    "Same-user processes and shared OS are not a malicious-worker sandbox. External dependencies and native children are unqualified.",
    "Interpreter, pytest/pluggy Python inputs, candidate, harness, selected tests and fixture bytes are committed before/after; transient restored mutations are not excluded.",
    "Native artifact retention stores bounded public projections and byte hashes, never fixture keys or complete credential stores.",
]


def _private_directory(path):
    path.mkdir(mode=0o700, parents=False, exist_ok=False)
    return checked_directory(path)


def candidate_files(root):
    """Use an explicit bounded allowlist, including all candidate asset inputs."""
    paths = []
    for tree in CAPSULE_TREES:
        base = root / tree
        if base.is_symlink():
            raise ValueError("acceptance_capsule_symlink")
        if not base.exists():
            continue
        for directory, directories, files in os.walk(base, followlinks=False):
            directories[:] = sorted(name for name in directories if name not in EXCLUDED)
            for name in directories:
                if (Path(directory) / name).is_symlink():
                    raise ValueError("acceptance_capsule_symlink")
            for name in sorted(files):
                path = Path(directory) / name
                if path.suffix in {".pyc", ".pyo"} or name in {".DS_Store"}:
                    continue
                if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
                    raise ValueError("acceptance_capsule_nonregular")
                paths.append(path)
                if len(paths) > MAX_FILES:
                    raise ValueError("acceptance_capsule_file_bound")
    for name in CAPSULE_FILES:
        path = root / name
        if path.is_symlink():
            raise ValueError("acceptance_capsule_symlink")
        if path.exists():
            if not stat.S_ISREG(path.stat().st_mode):
                raise ValueError("acceptance_capsule_nonregular")
            paths.append(path)
    return sorted(paths)


def candidate_manifest(root):
    rows, total = {}, 0
    for path in candidate_files(root):
        row = hash_file(path)
        total += row["bytes"]
        if total > MAX_INPUT_BYTES or len(rows) >= MAX_FILES:
            raise ValueError("acceptance_capsule_byte_or_file_bound")
        rows[str(path.relative_to(root))] = row
    if "pyproject.toml" not in rows or not any(name.startswith("src/evidence_wiki/") for name in rows):
        raise ValueError("acceptance_candidate_inputs_missing")
    return rows


def framework_manifest():
    rows = {"interpreter:" + str(Path(sys.executable).resolve()): hash_file(Path(sys.executable).resolve())}
    for module in ("pytest", "_pytest", "pluggy"):
        spec = importlib.util.find_spec(module)
        if spec is None or spec.origin is None:
            raise ValueError("acceptance_framework_missing")
        base = Path(spec.origin).parent
        for path in sorted(base.rglob("*.py")):
            rows[module + ":" + str(path.relative_to(base))] = hash_file(path)
            if len(rows) > MAX_FILES or sum(row["bytes"] for row in rows.values()) > MAX_INPUT_BYTES:
                raise ValueError("acceptance_framework_bound")
    return rows


def _copy_capsule(root, capsule, manifest):
    for name, expected in manifest.items():
        source, target = root / name, capsule / name
        raw = source.read_bytes()
        if {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)} != expected:
            raise ValueError("acceptance_candidate_changed_before_copy")
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
        os.chmod(target, 0o700 if source.stat().st_mode & 0o111 else 0o600)


def _retained_refs(directory):
    rows = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError("acceptance_evidence_symlink")
        if path.is_file() and path.name != "report.json":
            raw = read_private(path, MAX_RECORD_BYTES)
            rows[str(path.relative_to(directory))] = {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
            if len(rows) > MAX_FILES:
                raise ValueError("acceptance_evidence_file_bound")
    return rows


def _unsigned(value):
    return {key: item for key, item in value.items() if key != "authentication"}


def _read_events(path):
    events = []
    if not path.exists():
        return events
    for line in read_private(path, MAX_RECORD_BYTES).splitlines():
        try:
            events.append(bounded_json(json.loads(line)))
        except (ValueError, UnicodeError):
            events.append({"event": "malformed"})
    return events


def _processes(retained, reservation, plan, observer_key):
    """Reconcile retained signed starts even when dispatch raised before returning."""
    rows, unstarted, unknown, gaps = [], [], [], []
    intact = True
    capsule, workers = retained.parent / "capsule", retained.parent / "workers"
    for mode in ("collect", "execute"):
        row = {"attempt_id": mode, "state": "not_reserved", "intent": None, "start": None,
               "receipt": None, "receipt_sha256": None, "event": None, "retention_authenticated": False}
        paths = {name: retained / (mode + suffix) for name, suffix in
                 (("intent", ".intent.json"), ("start", ".start.json"), ("receipt", ".receipt.json"))}
        for name, path in paths.items():
            if path.exists():
                try:
                    row[name] = _authenticated(path, observer_key)
                except (ValueError, OSError) as error:
                    gaps.append(mode + ":" + name + "_unqualified")
                    if "authentication_failed" in str(error):
                        intact = False
        intent, start, receipt = row["intent"], row["start"], row["receipt"]
        argv = [reservation["inputs"]["interpreter"], "-I", "-B", str(capsule / "tools/_acceptance_pytest.py"),
                str(retained / (mode + ".request.json"))]
        valid_start = bool(intent and start and set(start) == {"intent_sha256", "started_at", "pid", "authentication"}
                           and start["intent_sha256"] == digest(intent) and type(start["pid"]) is int and start["pid"] > 0
                           and type(start["started_at"]) is str)
        if valid_start:
            row["state"] = "started_stop_unknown"
        elif paths["start"].exists() or (receipt and receipt.get("started_at") is not None):
            row["state"] = "start_unknown"
        elif intent:
            row["state"] = "reserved_no_start"
        if receipt is not None:
            row["receipt_sha256"] = digest(receipt)
            try:
                checked = read_command_receipt(plan, mode, argv, observer_key=observer_key, directory=retained,
                    work_root=workers, expected_receipt_sha256=row["receipt_sha256"])
                if (intent["timeout_seconds"] != str(reservation["timeout_seconds"])
                        or intent["output_limit_bytes"] != MAX_RECORD_BYTES or intent["meter_id"] is not None
                        or intent["input_sha256"] != hashlib.sha256(b"").hexdigest()):
                    raise ValueError("acceptance_process_budget_binding")
                if valid_start:
                    times = [datetime.fromisoformat(value) for value in
                             (intent["reserved_at"], start["started_at"], receipt["finished_at"])]
                    if any(value.utcoffset() != timedelta(0) for value in times) or times != sorted(times):
                        raise ValueError("acceptance_process_clock_gap")
                row.update(checked, retention_authenticated=True)
                row["state"] = "closed_started" if valid_start else "closed_unstarted"
                if (receipt["started_at"] is not None) != valid_start:
                    raise ValueError("acceptance_start_stop_gap")
            except (ValueError, OSError, KeyError, TypeError) as error:
                row["retention_authenticated"] = False
                gaps.append(mode + ":receipt_unqualified:" + type(error).__name__)
        if row["state"] in {"not_reserved", "reserved_no_start", "closed_unstarted"}:
            unstarted.append(mode)
        if row["state"] in {"start_unknown", "started_stop_unknown"} or (valid_start and not row["retention_authenticated"]):
            unknown.append(mode)
        rows.append(row)
    return rows, unstarted, unknown, gaps, intact


def _validate_artifacts(events, retained, request, pid):
    """Check per-node native/projection bindings without pretending raw replay."""
    events = bounded_json(events)
    intact, gaps, used = True, [], set()
    fields = {"schema_version", "nodeid", "run_nonce", "capture_pid", "root_relative", "path", "native_sha256",
              "native_bytes", "before_native_sha256", "changed_since_setup", "public", "public_sha256", "scope"}
    for event in events:
        if type(event) is not dict or event.get("event") != "artifacts" or type(event.get("refs")) is not list:
            continue
        verified = []
        for ref in event["refs"]:
            try:
                if (type(ref) is not dict or type(ref.get("path")) is not str or Path(ref["path"]).name != ref["path"]
                        or ref["path"] in used):
                    raise ValueError("acceptance_artifact_reference")
                used.add(ref["path"])
                raw = read_private(retained / "artifacts" / ref["path"], MAX_RECORD_BYTES)
                if hashlib.sha256(raw).hexdigest() != ref.get("sha256"):
                    intact = False
                    raise ValueError("acceptance_artifact_drift")
                body = bounded_json(json.loads(raw))
                if type(body) is not dict or set(body) != fields:
                    raise ValueError("acceptance_artifact_shape")
                from tools._acceptance_pytest import _public
                if _public(body["public"], narrow=True) != body["public"]:
                    raise ValueError("acceptance_artifact_redaction")
                scope = body["scope"]
                if (body["schema_version"] != "acceptance-native-artifact/v1" or body["nodeid"] != event.get("nodeid")
                        or body["run_nonce"] != request["run_nonce"] or type(body["capture_pid"]) is not int
                        or body["capture_pid"] != pid or type(body["root_relative"]) is not str
                        or type(body["path"]) is not str or Path(body["root_relative"]).is_absolute()
                        or Path(body["path"]).is_absolute() or ".." in Path(body["root_relative"]).parts
                        or ".." in Path(body["path"]).parts or not body["path"]
                        or type(body["native_bytes"]) is not int or not 0 <= body["native_bytes"] <= MAX_RECORD_BYTES
                        or type(body["native_sha256"]) is not str or re.fullmatch(r"[a-f0-9]{64}", body["native_sha256"]) is None
                        or body["native_sha256"] != ref.get("native_sha256")
                        or type(body["changed_since_setup"]) is not bool or body["changed_since_setup"] != ref.get("changed_since_setup")
                        or body["changed_since_setup"] != (body["before_native_sha256"] != body["native_sha256"])
                        or (body["before_native_sha256"] is not None and (type(body["before_native_sha256"]) is not str
                            or re.fullmatch(r"[a-f0-9]{64}", body["before_native_sha256"]) is None))
                        or type(body["public"]) is not list or body["public_sha256"] != digest(body["public"])
                        or type(scope) is not dict or set(scope) != {"redacted", "projection_truncated", "native_records", "retained_records", "full_raw_replay"}
                        or scope["redacted"] is not True or type(scope["projection_truncated"]) is not bool
                        or type(scope["native_records"]) is not int or not 0 <= scope["native_records"] <= MAX_RECORD_BYTES
                        or type(scope["retained_records"]) is not int or scope["retained_records"] != len(body["public"])
                        or scope["retained_records"] != min(32, scope["native_records"])
                        or scope["projection_truncated"] != (scope["native_records"] > 32)
                        or scope["full_raw_replay"] != "unqualified"
                        or ref["path"] != digest([body["nodeid"], body["run_nonce"], body["root_relative"], body["path"]]) + ".json"):
                    raise ValueError("acceptance_artifact_binding")
                verified.append(ref)
            except (OSError, ValueError, KeyError, TypeError):
                gaps.append("artifact_binding_unqualified")
        event["refs"] = verified
    if gaps:
        events.append({"event": "artifact_binding_gap"})
    return events, intact, gaps


def _result_valid(value, *, request, row, collected, capsule, inputs, package_required):
    fields = {"schema_version", "run_nonce", "capture_pid", "collected", "exit_code", "collection_errors",
              "package_location", "plugin_location", "framework_location"}
    if type(value) is not dict or set(value) != fields or not row["retention_authenticated"] or row["start"] is None:
        return False
    receipt = row["receipt"]
    return (value["schema_version"] == "acceptance-pytest-result/v1" and value["run_nonce"] == request["run_nonce"]
        and type(value["capture_pid"]) is int and value["capture_pid"] == row["start"]["pid"]
        and type(value["exit_code"]) is int and value["exit_code"] == receipt["exit_code"]
        and value["collected"] == collected and value["collection_errors"] == []
        and value["plugin_location"] == str(capsule / "tools/_acceptance_pytest.py")
        and value["framework_location"] == inputs["framework_origins"]["pytest"]
        and (not package_required or value["package_location"] == str(capsule / "src/evidence_wiki/__init__.py"))
        and receipt["started_at"] == row["start"]["started_at"] and receipt["finished_at"] is not None
        and receipt["failure"] is None and row.get("coverage_gap") is None
        and not receipt["output_overflow"] and not receipt["output_reader_work_unresolved"]
        and ((value["exit_code"] == 0 and receipt["status"] == "succeeded")
             or (value["exit_code"] == 1 and receipt["status"] == "failed")))


def _regrade_retained(retained, report, observer_key):
    """Reconstruct grade from authenticated custody and actual retained joins."""
    reservation = validate_manifest(_unsigned(_authenticated(retained / "reservation.json", observer_key)))
    inputs = reservation["inputs"]
    plan = validate_cost_plan(json.loads(read_private(retained / "process-plan.json", MAX_RECORD_BYTES)))
    expected_attempts = [{"attempt_id": mode, "phase": "qualification", "allocation": "repeated", "source_id": "host",
                         "required_metrics": list(METRICS)} for mode in ("collect", "execute")]
    if (plan["binding"] != {"reservation_sha256": digest(reservation)}
            or plan["attempts"] != expected_attempts or plan["scope_id"] != "acceptance-paths"
            or plan["repeat_use_count"] is not None or len(plan["sources"]) != 1
            or plan["sources"][0]["source_id"] != "host" or plan["sources"][0]["provenance"] != "host_observed"
            or plan["sources"][0]["kind"] != "automated" or plan["sources"][0]["coverage"] != "partial"
            or plan["sources"][0]["authentication_ref"] is not None):
        raise ValueError("acceptance_process_plan_binding")
    if (report["policy"] != policy() or report["reservation_sha256"] != digest(reservation)
            or report["observer_id"] != hashlib.sha256(observer_key).hexdigest()):
        raise ValueError("acceptance_report_policy_binding")
    manifest_path = retained / "manifest.json"
    manifest = validate_manifest(_unsigned(_authenticated(manifest_path, observer_key))) if manifest_path.exists() else None
    if manifest is not None and (manifest["cases"] != reservation["cases"] or manifest["inputs"] != inputs
            or manifest["seed"] != reservation["seed"] or manifest["timeout_seconds"] != reservation["timeout_seconds"]):
        raise ValueError("acceptance_manifest_reservation_binding")
    effective = reservation if manifest is None else manifest
    processes, unstarted, unknown, process_gaps, intact = _processes(retained, reservation, plan, observer_key)
    events = _read_events(retained / "execute.events.jsonl")
    capsule = retained.parent / "capsule"
    valid = {}
    for row in processes:
        mode = row["attempt_id"]
        request_path, result_path = retained / (mode + ".request.json"), retained / (mode + ".result.json")
        valid[mode] = False
        if not request_path.exists():
            continue
        try:
            request = json.loads(read_private(request_path, MAX_RECORD_BYTES))
            expected_request = {"mode": mode, "run_nonce": inputs["run_nonce"],
                "selectors": [selector for case in reservation["cases"] for selector in case["selectors"]],
                "exact_nodeids": reservation["exact_nodeids"] if mode == "collect" else effective["exact_nodeids"],
                "events": str(retained / (mode + ".events.jsonl")), "result": str(result_path),
                "artifacts": str(retained / "artifacts"), "basetemp": str(retained.parent / "workers" / mode / "pytest-temp")}
            if request != expected_request:
                raise ValueError("acceptance_worker_request_binding")
            if mode == "execute":
                events, artifact_intact, artifact_gaps = _validate_artifacts(events, retained, request,
                    None if row["start"] is None else row["start"].get("pid"))
                intact &= artifact_intact
                process_gaps.extend(artifact_gaps)
            result = json.loads(read_private(result_path, MAX_RECORD_BYTES)) if result_path.exists() else None
            collected = [] if manifest is None else manifest["collected_nodeids"] if mode == "collect" else manifest["exact_nodeids"]
            valid[mode] = _result_valid(result, request=request, row=row, collected=collected,
                                       capsule=capsule, inputs=inputs, package_required=mode == "execute")
            if mode == "collect":
                valid[mode] &= bool(row["receipt"] and row["receipt"]["exit_code"] == 0)
        except (OSError, ValueError, KeyError, TypeError):
            process_gaps.append(mode + ":request_or_result_unqualified")
    started = any(row["start"] is not None for row in processes)
    try:
        drift = (candidate_manifest(Path(report["repo_root"])) != inputs["candidate"]
                 or (started and candidate_manifest(capsule) != inputs["candidate"])
                 or framework_manifest() != inputs["framework"])
    except (OSError, ValueError):
        drift = True
    grade = grade_evidence(effective, events, drift=drift, process_complete=all(valid.values()) and not process_gaps,
                           evidence_intact=intact)
    execute = processes[1]
    if execute["receipt"] is not None and execute["receipt"].get("exit_code") == 1 and grade["status"] != "FAIL":
        grade = grade_evidence(effective, events, drift=drift, process_complete=False, evidence_intact=intact)
    if manifest is None:
        grade["denominators"]["exact_nodes"] = None
        grade["denominators"]["expected_selectors"] = sum(len(case["selectors"]) for case in reservation["cases"])
    catalog_scope = "declared_fixture"
    if Path(report["repo_root"]) == ROOT:
        from tools._acceptance_cases import acceptance_cases
        if effective["cases"] == validate_cases(acceptance_cases()):
            catalog_scope = "frozen_registry"
    if report["catalog_scope"] != catalog_scope or report["catalog_sha256"] != digest(effective["cases"]):
        raise ValueError("acceptance_catalog_binding")
    if report.get("key_custody") != "caller_external_key" and grade["status"] != "FAIL":
        grade["status"] = "ABSTAIN"
        grade["gaps"] = sorted(set(grade["gaps"] + ["observer_key_custody_unqualified"]))
    return {"grade": grade, "processes": processes, "unstarted_reservations": unstarted,
            "started_or_stop_unknown_reservations": unknown, "reconstruction_gaps": process_gaps, "manifest": manifest}


def read_acceptance_report(path, *, observer_key, expected_report_sha256):
    """Authenticate then independently regrade the frozen retained transcript."""
    path = Path(path)
    value = _authenticated(path, observer_key)
    if digest(value) != expected_report_sha256:
        raise ValueError("acceptance_latest_report_mismatch")
    if value.get("retained_refs") != _retained_refs(path.parent):
        raise ValueError("acceptance_retained_evidence_changed")
    reconstructed = _regrade_retained(path.parent, value, observer_key)
    if any(value.get(field) != expected for field, expected in reconstructed.items()):
        raise ValueError("acceptance_independent_regrade_mismatch")
    return value


def run_acceptance(*, repo_root, cases, evidence_root, seed=0, timeout_seconds=1200, observer_key=None):
    """Collect then execute once; retain all starts and incomplete reservations."""
    repo_root = Path(repo_root).resolve(strict=True)
    evidence_root = Path(evidence_root).absolute()
    cases = validate_cases(cases)
    catalog_scope = "declared_fixture"
    if repo_root == ROOT:
        from tools._acceptance_cases import acceptance_cases
        if cases == validate_cases(acceptance_cases()):
            catalog_scope = "frozen_registry"
    # Validate every scalar and pre-freeze order before creating directories or spawning.
    selectors = [selector for case in cases for selector in case["selectors"]]
    reservation = freeze_manifest(cases, selectors, inputs={}, seed=seed, timeout_seconds=timeout_seconds)
    if evidence_root.resolve() != evidence_root or evidence_root.is_relative_to(repo_root) or repo_root.is_relative_to(evidence_root):
        raise ValueError("acceptance_evidence_must_be_external_canonical")
    supplied_key = observer_key is not None
    observer_key = secrets.token_bytes(32) if observer_key is None else observer_key
    if type(observer_key) is not bytes or not 32 <= len(observer_key) <= 128:
        raise ValueError("acceptance_observer_key")
    inputs = {"candidate": candidate_manifest(repo_root), "framework": framework_manifest(),
              "run_nonce": secrets.token_hex(16), "interpreter": sys.executable,
              "framework_origins": {module: str(Path(importlib.util.find_spec(module).origin).resolve())
                                    for module in ("pytest", "_pytest", "pluggy")}}
    _private_directory(evidence_root)
    retained = _private_directory(evidence_root / "retained")
    capsule = _private_directory(evidence_root / "capsule")
    workers = _private_directory(evidence_root / "workers")
    artifacts = _private_directory(retained / "artifacts")
    reservation["inputs"] = inputs
    reservation["manifest_sha256"] = digest({k: v for k, v in reservation.items() if k != "manifest_sha256"})
    publish_new(retained / "reservation.json", _signed(reservation, observer_key))
    now = datetime.now(timezone.utc)
    plan = freeze_cost_plan(scope_id="acceptance-paths", binding={"reservation_sha256": digest(reservation)},
        sources=[{"source_id": "host", "provenance": "host_observed", "kind": "automated", "origin": "Selected acceptance pytest processes",
                  "collection_started_at": (now - timedelta(seconds=1)).isoformat(),
                  "collection_ended_at": (now + timedelta(seconds=2 * timeout_seconds + 90)).isoformat(),
                  "coverage": "partial", "authentication_ref": None}],
        attempts=[{"attempt_id": label, "phase": "qualification", "allocation": "repeated", "source_id": "host",
                   "required_metrics": list(METRICS)} for label in ("collect", "execute")])
    publish_new(retained / "process-plan.json", plan)
    report = {"schema_version": "acceptance-observation/v1", "reservation_sha256": digest(reservation),
              "manifest": None, "processes": [], "errors": [], "limitations": LIMITATIONS,
              "observer_id": hashlib.sha256(observer_key).hexdigest(), "policy": policy(),
              "repo_root": str(repo_root), "key_custody": "caller_external_key" if supplied_key else "unqualified_ephemeral",
              "catalog_scope": catalog_scope, "catalog_sha256": digest(cases),
              "verifier_independence": "external agent assignment; not authenticated by role names or this gate",
              "unstarted_reservations": ["collect", "execute"]}
    manifest, events, drift = reservation, [], False
    try:
        _copy_capsule(repo_root, capsule, inputs["candidate"])
        for mode in ("collect", "execute"):
            if mode == "execute" and report["manifest"] is None:
                break
            request = {"mode": mode, "run_nonce": inputs["run_nonce"], "selectors": selectors, "exact_nodeids": manifest["exact_nodeids"],
                       "events": str(retained / (mode + ".events.jsonl")), "result": str(retained / (mode + ".result.json")),
                       "artifacts": str(artifacts), "basetemp": str(workers / mode / "pytest-temp")}
            request_path = retained / (mode + ".request.json")
            publish_new(request_path, request)
            argv = [sys.executable, "-I", "-B", str(capsule / "tools/_acceptance_pytest.py"), str(request_path)]
            outcome = observe_command(plan, mode, argv, observer_key=observer_key, directory=retained,
                                      work_root=workers, timeout_seconds=timeout_seconds, meter=None)
            report["processes"].append({"attempt_id": mode, **outcome, "retention_authenticated": False})
            if outcome["receipt"]["started_at"] is not None:
                report["unstarted_reservations"].remove(mode)
            authenticated = read_command_receipt(plan, mode, argv, observer_key=observer_key, directory=retained,
                work_root=workers, expected_receipt_sha256=outcome["receipt_sha256"])
            report["processes"][-1] = {"attempt_id": mode, **authenticated, "retention_authenticated": True}
            result_path = retained / (mode + ".result.json")
            result = json.loads(read_private(result_path, MAX_RECORD_BYTES)) if result_path.exists() else None
            if mode == "collect":
                if (authenticated["receipt"]["status"] != "succeeded" or result is None
                        or result.get("exit_code") != 0 or result.get("collection_errors")
                        or not _result_valid(result, request=request,
                            row={**authenticated, "retention_authenticated": True,
                                 "start": _authenticated(retained / "collect.start.json", observer_key)},
                            collected=result.get("collected"), capsule=capsule, inputs=inputs, package_required=False)):
                    report["errors"].append("collection_incomplete")
                    break
                manifest = freeze_manifest(cases, result["collected"], inputs=inputs, seed=seed, timeout_seconds=timeout_seconds)
                report["manifest"] = manifest
                publish_new(retained / "manifest.json", _signed(manifest, observer_key))
    except BaseException as error:
        report["errors"].append(type(error).__name__ + ": " + str(error)[:512])
        if "acceptance_candidate_changed" in str(error) or "acceptance_input_changed" in str(error):
            drift = True
        if hasattr(error, "cost_receipt"):
            report["processes"].append({"attempt_id": mode, **error.cost_receipt, "retention_authenticated": False})
            if error.cost_receipt["receipt"]["started_at"] is not None and mode in report["unstarted_reservations"]:
                report["unstarted_reservations"].remove(mode)
    finally:
        try:
            report.update(_regrade_retained(retained, report, observer_key))
        except (ValueError, OSError, KeyError, TypeError) as error:
            report["errors"].append(type(error).__name__ + ": reconstruction_incomplete")
            report["grade"] = grade_evidence(manifest, [], drift=drift, process_complete=False)
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        try:
            report["retained_refs"] = _retained_refs(retained)
        except (OSError, ValueError) as error:
            report["errors"].append(type(error).__name__ + ": retention_incomplete")
            report["retained_refs"] = {}
            report["grade"] = grade_evidence(manifest, events, drift=drift, process_complete=False, evidence_intact=False)
        signed = _signed(report, observer_key)
        publish_new(retained / "report.json", signed)
        for path in retained.rglob("*"):
            if path.is_file() and not path.is_symlink():
                path.chmod(0o400)
    return {"report": signed, "report_sha256": digest(signed), "report_path": str(retained / "report.json")}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--observer-key", type=Path, required=True,
                        help="Private 32–128 byte key file outside the entire evidence tree; retain it with the external latest report hash.")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--timeout", type=positive_seconds, default=1200)
    args = parser.parse_args(argv)
    key_path, evidence_path = args.observer_key.absolute(), args.evidence_dir.absolute()
    if key_path.resolve() != key_path or key_path.is_relative_to(evidence_path):
        parser.error("observer key must be canonical, non-symlink and outside the entire evidence directory")
    if any(key_path.is_relative_to(ROOT / tree) for tree in CAPSULE_TREES):
        parser.error("observer key must be outside the copied candidate input trees")
    try:
        observer_key = read_private(key_path, 128)
        if not 32 <= len(observer_key) <= 128:
            raise ValueError("observer key must contain 32–128 bytes")
    except (OSError, ValueError) as error:
        parser.error(str(error))
    from tools._acceptance_cases import acceptance_cases
    value = run_acceptance(repo_root=ROOT, cases=acceptance_cases(), evidence_root=args.evidence_dir,
                           seed=args.seed, timeout_seconds=args.timeout, observer_key=observer_key)
    print(json.dumps({"status": value["report"]["grade"]["status"], "report_path": value["report_path"],
                      "report_sha256": value["report_sha256"]}, sort_keys=True))
    return {"PASS": 0, "FAIL": 1, "ABSTAIN": 2}[value["report"]["grade"]["status"]]


if __name__ == "__main__":
    with interruptible():
        raise SystemExit(main())
