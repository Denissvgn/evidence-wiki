"""Same-workspace reference integrations through three existing public routes."""

from __future__ import annotations

import hashlib
import json
import platform
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from evidence_wiki import OperationControl, Workspace
from evidence_wiki.errors import EvidenceWikiError, error_from_envelope
from evidence_wiki.onboarding_mcp import OnboardingMcpServer
from evidence_wiki.research_command_contracts import error_document
from tools._acceptance_protocol import digest as owner_digest
from tools._integration_improvement import BOOLS, COUNTERS, CYCLES, ROUTES
from tools._release_decision import bounded_json, canonical, digest
from tools.decide_release import _deadline
from tools.verify_acceptance_paths import candidate_manifest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples/unattended-host"
REFERENCE = {"schema_version": "finite-structured-observation/v1", "facts": {
    "operation": "sum", "values": ["9", "2"], "units": "count", "scope": "fixture-cohort",
    "time": "fixture-observation", "contradiction": False}}


def scenario(route):
    if route not in ROUTES:
        raise ValueError("integration_route")
    return {"route": route, "reference": REFERENCE, "changes": ["title revision 1", "title revision 2"],
            "meaning": "same finite value; evidence identity changes only", "steps": 64,
            "transport": "in_process_jsonrpc" if route == "scoped_mcp" else "python"}


def host_identity(route):
    return {"route": route, "interpreter": str(Path(sys.executable).resolve()),
            "python": platform.python_version(), "system": platform.system(), "machine": platform.machine(),
            "source": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "example": {name: hashlib.sha256((EXAMPLE / name).read_bytes()).hexdigest()
                        for name in ("finite_host.py", "finite_workers.py", "fixture_host.py", "support.py")}}


def input_identity(route, cycle):
    return digest({"scenario": scenario(route), "cycle": cycle})


def _manifest(path):
    return {p.relative_to(path).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(path.rglob("*")) if p.is_file() and "__pycache__" not in p.parts}


def reference_useful(brief):
    claims = [claim for original in brief["original_outcomes"] for question in original["eligible_questions"]
              for claim in question["claims"]]
    expected = "Finite observation: sum; value=11; units=count; scope=fixture-cohort; time=fixture-observation."
    return brief["verdict"] == "accept" and len(claims) == 1 and claims[0]["text"] == expected


def supported_proposals(plan, projection):
    """Cite only frozen triggers crossed by actual measured scope counters."""
    return sorted(proposal["proposal_id"] for proposal in plan["proposals"]
                  if projection[proposal["trigger_metric"]] is not None
                  and projection[proposal["trigger_metric"]] >= proposal["trigger_threshold"])


def applied_review_count(records):
    """Count applied evaluations and adjudications, not every dispatch intent."""
    return sum(row["kind"] == "execution_applied" and row["data"].get("operation") in {"evaluate", "adjudicate"}
               for row in records)


def _retain(target, raw):
    if len(raw) > 4_194_304:
        raise ValueError("integration_original_output_bound")
    with target.open("xb") as stream:
        stream.write(raw)
    target.chmod(0o400)
    return {"path": str(target), "raw_sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)}


def _diagnosis(error):
    def token(value):
        return value if type(value) is str and re.fullmatch(r"[A-Za-z0-9_]{1,128}", value) else None
    return {"error_code": token(error.error_code), "reason": token(error.details.get("reason"))}


def _load_example():
    # Fixed reviewed public host code, never evidence-selected Python.
    sys.path.insert(0, str(EXAMPLE))
    from finite_host import provision
    from support import attest, identity, now, sha
    return provision, attest, identity, now, sha


def _closure(root):
    rows = [json.loads(line) for line in (root / "sources/manifest.jsonl").read_text().splitlines()]
    if len(rows) != 1:
        raise ValueError("integration_reference_inventory")
    source_id = rows[0]["id"]
    normalized = next(p for p in (root / "sources/normalized").glob("*.md") if source_id in p.read_text())
    descriptor = {"schema_version": "evidence-source-revision/v1", "source_id": source_id,
                  "parents": [], "normalized_path": normalized.relative_to(root).as_posix(),
                  "evidence_root": "evidence", "temporal": {}}
    files = {"source-record.json": canonical(descriptor), descriptor["normalized_path"]: normalized.read_bytes()}
    files.update({"evidence/" + p.relative_to(root).as_posix(): p.read_bytes() for p in (root / "raw").rglob("*")
                  if p.is_file() and not any(part.startswith(".") for part in p.relative_to(root).parts)})
    if files.get("evidence/raw/papers/study.md") != canonical(REFERENCE):
        raise ValueError("integration_reference_bytes")
    return descriptor, files


def _rotate(workspace, descriptor, files, private, index):
    _, attest, identity, now, sha = _load_example()
    # This is host-only fixture custody; no key is returned in observations.
    services = json.loads((private / "finite-services.json").read_bytes())
    keys = {name: bytes.fromhex(value) for name, value in services["keys"].items()}
    keys["expires_at"] = services["expires_utc"]
    path = descriptor["normalized_path"]
    previous = files[path]
    _, front, body = previous.decode().split("---", 2)
    metadata = yaml.safe_load(front)
    metadata["title"] = "Synthetic finite observation title revision " + str(index)
    files = {**files, path: ("---\n" + yaml.safe_dump(metadata, sort_keys=False) + "---" + body).encode()}
    revision = identity("evidence-artifact-closure/v1", {name: {"content_hash": sha(raw), "size_bytes": len(raw)}
                                                       for name, raw in files.items()})
    redaction = {"id": "fixture-scrub", "revision": "1"}
    grant = {"schema_version": "evidence-usage-grant/v1", "source_id": descriptor["source_id"],
             "source_revision": revision, "permissions": dict.fromkeys(("retrieval", "training", "export"), True),
             "purposes": ["research", "qa-export"], "consumers": ["evidence-wiki"], "not_before": now(),
             "expires_at": keys["expires_at"], "redaction_policy": redaction, "retention": "host-managed"}
    scrub = {"schema_version": "evidence-scrub-receipt/v1", "sanitized_revision": revision,
             "redaction_policy": redaction, "tool": {"name": "fixture-host-scrubber", "version": "1"},
             "outcome": "passed", "completed_at": now()}
    status = workspace.usage.status()
    command = {"schema_version": "evidence-usage-command/v1", "state_id": status["state_id"],
               "workspace_binding": status["workspace_binding"], "expected_checkpoint": status["checkpoint"],
               "request_id": "integration-revision-" + str(index), "action": "deposit", "body": {
                   "source_id": descriptor["source_id"], "source_revision": revision,
                   "grant": attest(grant, "usage", "usage", keys), "scrub": attest(scrub, "scrubber", "scrubber", keys)}}
    receipt = workspace.usage.transact(attest(command, "usage", "usage", keys), artifacts=files)
    materialized = workspace.usage.materialize(revision, expected_content_hash=sha(previous))
    return files, {"revision": revision, "receipt": receipt, "materialized": materialized,
                   "previous_normalized_sha256": hashlib.sha256(previous).hexdigest(),
                   "normalized_sha256": hashlib.sha256(files[path]).hexdigest()}


class Routes:
    def __init__(self, root, runtime, route):
        self.root, self.runtime, self.route = root, runtime, route
        self.calls = []
        self.server = None
        if route == "scoped_mcp":
            self.server = OnboardingMcpServer(allowed_roots=[root], runtime=runtime, research_allow=["run-cycle", "refresh"])
            result = self.server.handle_message({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
                "protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "reference-host", "version": "1"}}})
            if "error" in result:
                raise ValueError("integration_mcp_initialize")
            self.server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
            result = self.server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
            names = {row["name"] for row in result["result"]["tools"]}
            if not {"research_run_cycle", "research_refresh", "research_brief", "research_report"} <= names:
                raise ValueError("integration_mcp_tools")
            self.calls.append({"operation": "tools/list", "result_sha256": digest(result)})

    def call(self, operation, arguments, control=None):
        if control is not None:
            control.checkpoint("reference_route_before_call")
        start = time.monotonic()
        record = {"operation": operation, "arguments": arguments, "state": "started"}
        self.calls.append(record)
        raw = None
        if self.server is not None:
            cancellation = threading.Event() if control is not None else None
            timer = None
            if control is not None:
                timer = threading.Timer(control.remaining_seconds("reference_mcp_call"), cancellation.set)
                timer.daemon = True
                timer.start()
            try:
                response = self.server.handle_message({"jsonrpc": "2.0", "id": len(self.calls) + 1,
                    "method": "tools/call", "params": {"name": "research_" + operation.replace("-", "_"),
                    "arguments": {"target": str(self.root), **arguments}}}, cancellation=cancellation)
            finally:
                if timer is not None:
                    timer.cancel()
            if "error" in response:
                raise ValueError("integration_mcp_refused")
            raw = response["result"]["content"][0]["text"].encode()
            if len(raw) > 4_194_304:
                raise ValueError("integration_original_output_bound")
            envelope = json.loads(raw)
        else:
            try:
                with self.runtime.context.activate(), Workspace.open(self.root) as workspace:
                    if self.route == "current_caller":
                        envelope = workspace.research.command({"schema_version": "research-command-request/v1",
                            "operation": operation, "arguments": arguments}, runtime=self.runtime, operation_control=control)
                    else:
                        shared = {"execution": self.runtime.execution}
                        if operation in {"run-cycle", "refresh"}:
                            result = getattr(workspace.research, operation.replace("-", "_"))(
                                self.runtime.policy, **shared, **arguments, operation_control=control)
                        else:
                            selected = dict(arguments)
                            with workspace.research.session(self.runtime.policy, run_id=selected.pop("run_id"), **shared) as session:
                                result = getattr(session, operation)(**selected)
                        envelope = {"status": "returned", "result": result, "error": None}
            except EvidenceWikiError as error:
                envelope = {"status": "refused", "result": None, "error": error_document(error)}
        record.update(state=envelope["status"], result_sha256=digest(envelope), elapsed_seconds=time.monotonic() - start)
        raw = canonical(envelope) if raw is None else raw
        record["envelope_raw_sha256"] = hashlib.sha256(raw).hexdigest()
        record["envelope_size_bytes"] = len(raw)
        if getattr(self, "observations", None) is not None:
            record["original_envelope"] = _retain(self.observations / ("call-" + str(len(self.calls)) + ".json"), raw)
        if envelope["result"] is None:
            error = error_from_envelope(envelope["error"])
            record.update(_diagnosis(error))
        if control is not None:
            control.checkpoint("reference_route_after_call")
        if envelope["result"] is None:
            raise error
        return envelope["result"]

    def close(self):
        if self.server is not None:
            self.server.close()


def run_scenario(plan, host_id, directory, *, remaining_seconds=900):
    """Observe baseline plus two real permitted evidence-identity revisions."""
    host = next(row for row in plan["hosts"] if row["host_id"] == host_id)
    route = host["route"]
    if digest(candidate_manifest(ROOT)) != plan["candidate_sha256"] or digest(host_identity(route)) != host["host_sha256"]:
        raise ValueError("integration_candidate_or_host_changed")
    if digest(scenario(route)) != host["scenario_sha256"]:
        raise ValueError("integration_scenario_changed")
    directory = Path(directory).absolute()
    if directory.resolve() != directory or directory.is_relative_to(ROOT) or directory.exists():
        raise ValueError("integration_new_external_directory")
    directory.mkdir(mode=0o700)
    observations = directory / "observations"
    observations.mkdir(mode=0o700)
    selected = {slot["cycle"]: slot for slot in plan["slots"] if slot["host_id"] == host_id}
    if any(slot["input_sha256"] != input_identity(route, cycle) for cycle, slot in selected.items()):
        raise ValueError("integration_input_changed")
    report = {"schema_version": "repeated-integration-host/v1", "plan_sha256": digest(plan),
        "candidate_sha256": plan["candidate_sha256"], "host": host, "scenario": scenario(route),
        "workspace": None, "factory": None, "cycles": [], "calls": [], "failure": None,
        "observation_class": "fixture_mechanics", "provider_usage": None, "whole_host_cost": None,
        "adoption": "unobserved", "business_value": "unobserved", "release_published": False}
    for cycle in CYCLES:
        projection = {**dict.fromkeys(BOOLS), **dict.fromkeys(COUNTERS), "disposition": "unstarted",
            "usefulness_scope": "fixture_mechanics", "elapsed_seconds": None, "supported_proposals": []}
        report["cycles"].append({"slot_id": selected[cycle]["slot_id"], "input_sha256": selected[cycle]["input_sha256"],
                                 "run_id": "reference-" + cycle, "projection": projection})
    started = time.monotonic()
    cycle_start = started
    current_row, routes = report["cycles"][0], None
    current_row["projection"]["disposition"] = "started_stop_unknown"
    try:
        with _deadline(min(remaining_seconds, sum(float(slot["elapsed_cap_seconds"]) for slot in selected.values()))):
            provision, _, _, _, _ = _load_example()
            root, runtime, _ = provision(directory / "fixture")
            report.update(workspace=str(root), factory=str(root.parent / "fixture-private/services.py"))
            descriptor, files = _closure(root)
            report["reference_sha256"] = hashlib.sha256(canonical(REFERENCE)).hexdigest()
            routes = Routes(root, runtime, route)
            routes.observations = observations
            parent_id, previous_basis = None, None
            for index, row in enumerate(report["cycles"]):
                current_row = row
                p = row["projection"]
                p["disposition"] = "started_stop_unknown"
                cycle_start = started if index == 0 else time.monotonic()
                before = _manifest(root / "runs" / parent_id) if parent_id else None
                if index:
                    with runtime.context.activate(), Workspace.open(root) as workspace:
                        files, row["source_update"] = _rotate(workspace, descriptor, files, root.parent / "fixture-private", index)
                cap = float(selected[CYCLES[index]]["elapsed_cap_seconds"])
                remaining = cap - (time.monotonic() - cycle_start)
                if remaining <= 0:
                    raise TimeoutError("integration_slot_budget")
                control = OperationControl(timeout_seconds=remaining,
                    deadline_utc=(datetime.now(timezone.utc) + timedelta(seconds=remaining)).isoformat())
                try:
                    arguments = {"run_id": row["run_id"], "request_id": "drive-" + CYCLES[index], "max_steps": 64}
                    if parent_id:
                        arguments.update(previous_run_id=parent_id, planner_config_ref="planner")
                    originals = row["originals"] = {}
                    result = routes.call("refresh" if parent_id else "run-cycle", arguments, control)
                    originals["result"] = _retain(observations / (CYCLES[index] + "-result.json"), canonical(result))
                    try:
                        brief = routes.call("brief", {"run_id": row["run_id"]}, control)
                    except EvidenceWikiError as error:
                        row["brief_refusals"] = [_diagnosis(error)]
                        if (error.error_code != "RESEARCH_RESOLUTION_REFUSED"
                                or error.details.get("reason") != "resolution_current_inputs_changed"):
                            raise
                        control.checkpoint("reference_brief_retry")
                        if control.remaining_seconds("reference_brief_retry") <= 0:
                            raise TimeoutError("integration_slot_budget") from None
                        try:
                            brief = routes.call("brief", {"run_id": row["run_id"]}, control)
                        except EvidenceWikiError as retry_error:
                            row["brief_refusals"].append(_diagnosis(retry_error))
                            raise
                    originals["brief"] = _retain(observations / (CYCLES[index] + "-brief.json"), canonical(brief))
                    accounting = routes.call("report", {"run_id": row["run_id"], "max_records": 64}, control)
                    originals["accounting"] = _retain(observations / (CYCLES[index] + "-accounting.json"), canonical(accounting))
                    with runtime.context.activate(), Workspace.open(root) as workspace:
                        with workspace.research.session(runtime.policy, run_id=row["run_id"], execution=runtime.execution) as session:
                            records = runtime.execution.records(session)
                    applied = [item["data"] for item in records if item["kind"] == "execution_applied"]
                    cycle_result = result.get("cycle", result)
                    basis = cycle_result.get("current_basis_id")
                    accepted = result["outcome"] == "accepted" and brief["verdict"] == "accept"
                    # Exact source and bounded arithmetic reference, no broad semantic rating.
                    useful = accepted and reference_useful(brief)
                    history = parent_id is None or before == _manifest(root / "runs" / parent_id)
                    drift = parent_id is None or bool(result.get("changed_source_ids")) and basis != previous_basis
                    terminal_parent = result.get("terminal") is True
                    p.update(disposition="completed", integration_success=terminal_parent, accepted=accepted, useful=useful,
                        unsupported_scope=False, elapsed_seconds=f"{time.monotonic()-cycle_start:.6f}",
                        repair_attempts=sum(item.get("operation") == "repair" for item in applied),
                        review_attempts=applied_review_count(records),
                        source_drift_detected=drift, stale_approval_reused=result.get("approval_reuse", False),
                        history_preserved=history, critical_failure=any(item.get("critical_failure") is True for item in applied))
                    p["supported_proposals"] = supported_proposals(plan, p)
                    row.update(originals=originals, outcome=result["outcome"], brief_verdict=brief["verdict"],
                               basis_id=basis, native_records_sha256=owner_digest(records),
                               native_record_count=len(records), parent_run_id=parent_id,
                               parent_history_before=before, parent_history_after=_manifest(root / "runs" / parent_id) if parent_id else None)
                    if not terminal_parent:
                        raise ValueError("integration_nonterminal_parent")
                    previous_basis, parent_id = basis, row["run_id"]
                finally:
                    control.close()
            report["calls"] = routes.calls
    except BaseException as error:
        report["failure"] = {"kind": type(error).__name__, "error_code": error.error_code if isinstance(error, EvidenceWikiError) else None}
        if isinstance(error, EvidenceWikiError):
            report["failure"].update(_diagnosis(error))
        current_row["projection"].update(disposition="failed", integration_success=False,
                                        elapsed_seconds=f"{time.monotonic()-cycle_start:.6f}")
        if routes is not None:
            report["calls"] = routes.calls
    finally:
        if routes is not None:
            routes.close()
    report["elapsed_seconds"] = f"{time.monotonic()-started:.6f}"
    report["retained_at"] = datetime.now(timezone.utc).isoformat()
    report["closing_failure"] = None
    if digest(candidate_manifest(ROOT)) != plan["candidate_sha256"]:
        report["closing_failure"] = {"kind": "CandidateChanged", "error_code": None}
        if report["failure"] is None:
            report["failure"] = report["closing_failure"]
    if report["failure"]:
        # Preserve historical original outcomes, but never admit positive
        # scenario qualification after a global producer failure.
        for row in report["cycles"]:
            if row["projection"]["disposition"] == "completed":
                row["projection"].update(integration_success=False, accepted=False, useful=None)
            if report["closing_failure"] is not None:
                row["projection"]["critical_failure"] = True
    return bounded_json(report)
