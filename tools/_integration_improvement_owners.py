"""Current original readers for retained native reference integration hosts."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from evidence_wiki import HostRuntime, Workspace
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.research_command_contracts import RESULT, checked
from tools._acceptance_protocol import digest as native_digest
from tools._integration_improvement import OwnerEvidence, OwnerReader
from tools._release_decision import OriginalCitation, _original, _time, digest
from tools._repeated_integration_host import (
    CYCLES,
    ROOT,
    _manifest,
    applied_review_count,
    host_identity,
    input_identity,
    reference_useful,
    scenario,
    supported_proposals,
)
from tools.measure_integration_costs import read_command_receipt
from tools.replay_qualification_native import replay as native_replay
from tools.run_comparative_trials import read_private
from tools.verify_acceptance_paths import candidate_manifest


def _response_document(directory, filename, original):
    """Authenticate a committed response using only the owner's fixed location."""
    expected = directory / "observations" / filename
    if (type(original) is not dict or set(original) != {"path", "raw_sha256", "size_bytes"}
            or original["path"] != str(expected)
            or any(parent.is_symlink() for parent in (expected, *expected.parents))):
        raise ValueError("integration_original_response_changed")
    try:
        raw = read_private(expected, 4_194_304)
        if (type(original["size_bytes"]) is not int or len(raw) != original["size_bytes"]
                or hashlib.sha256(raw).hexdigest() != original["raw_sha256"]):
            raise ValueError("integration_original_response_changed")
        document = _original(raw)
        if type(document) is not dict:
            raise ValueError("integration_original_response_changed")
        return document
    except (OSError, ValueError, KeyError) as error:
        raise ValueError("integration_original_response_changed") from error


def _responses(report, directory, route):
    """Check all retained responses, including refusals and partial failures."""
    captured = []
    for index, row in enumerate(report["cycles"]):
        originals = row.get("originals", {})
        if index >= len(CYCLES) or type(originals) is not dict or set(originals) - {"result", "brief", "accounting"}:
            raise ValueError("integration_original_response_changed")
        if report["failure"] is None and set(originals) != {"result", "brief", "accounting"}:
            raise ValueError("integration_original_response_changed")
        captured.append({name: _response_document(directory, CYCLES[index] + "-" + name + ".json", original)
                         for name, original in originals.items()})
    for index, call in enumerate(report["calls"], 1):
        if "original_envelope" not in call:
            if ((call.get("state") == "started" and "result_sha256" not in call)
                    or (call.get("operation") == "tools/list" and set(call) == {"operation", "result_sha256"})):
                continue
            raise ValueError("integration_original_response_changed")
        envelope = _response_document(directory, "call-" + str(index) + ".json", call["original_envelope"])
        if route == "direct":
            if set(envelope) != {"status", "result", "error"}:
                raise ValueError("integration_original_response_changed")
        elif route in {"current_caller", "scoped_mcp"}:
            try:
                checked(RESULT, envelope)
            except EvidenceWikiError as error:
                raise ValueError("integration_original_response_changed") from error
            if envelope["operation"] != call.get("operation"):
                raise ValueError("integration_original_response_changed")
        else:
            raise ValueError("integration_original_response_changed")
        if (envelope["status"] != call.get("state")
                or digest(envelope) != call.get("result_sha256")
                or call.get("envelope_raw_sha256") != call["original_envelope"]["raw_sha256"]
                or call.get("envelope_size_bytes") != call["original_envelope"]["size_bytes"]):
            raise ValueError("integration_original_response_changed")
        if envelope["status"] == "refused":
            error = envelope["error"]
            if envelope["result"] is not None or type(error) is not dict or type(error.get("details", {})) is not dict:
                raise ValueError("integration_original_response_changed")
            for name, value in (("error_code", error.get("error_code")), ("reason", error.get("details", {}).get("reason"))):
                token = value if type(value) is str and re.fullmatch(r"[A-Za-z0-9_]{1,128}", value) else None
                if call.get(name) != token:
                    raise ValueError("integration_original_response_changed")
        elif envelope["status"] == "returned" and envelope["result"] is not None and envelope["error"] is None:
            name = {"run-cycle": "result", "refresh": "result", "brief": "brief", "report": "accounting"}.get(call.get("operation"))
            if name is None:
                raise ValueError("integration_original_response_changed")
            matches = [i for i, row in enumerate(report["cycles"]) if row["run_id"] == call.get("arguments", {}).get("run_id")]
            if len(matches) != 1 or (name in captured[matches[0]] and digest(captured[matches[0]][name]) != digest(envelope["result"])):
                raise ValueError("integration_original_response_changed")
        else:
            raise ValueError("integration_original_response_changed")
    return captured


def host_readers(plan, originals):
    """Use host-selected native readers, never an authentication flag in JSON.

    Original locations and heads are external host input. Native producers own
    authentication and current acceptance. Reference facts and model meters are
    synthetic; local integration execution and host elapsed time are observed.
    """
    if type(originals) is not dict or set(originals) - {h["host_id"] for h in plan["hosts"]}:
        raise ValueError("integration_original_selection")
    result = []
    for host in plan["hosts"]:
        if host["host_id"] not in originals:
            continue
        selection = originals[host["host_id"]]
        if type(selection) is not dict or set(selection) != {"path", "raw_sha256", "source_head_sha256", "observation"}:
            raise ValueError("integration_original_selection")
        slots = [s for s in plan["slots"] if s["host_id"] == host["host_id"]]
        cache, calls = {}, [0]

        def read(slot, host=host, selection=selection, slots=slots, cache=cache, calls=calls):
            if calls[0] % len(slots) == 0:
                cache.clear()
                path = Path(selection["path"])
                raw = read_private(path, 1_048_576)
                if hashlib.sha256(raw).hexdigest() != selection["raw_sha256"]:
                    raise ValueError("integration_original_head_changed")
                report = _original(raw)
                observation = selection["observation"]
                if type(observation) is not dict or set(observation) != {
                        "plan", "attempt_id", "argv", "directory", "work_root", "expected_receipt_sha256", "observer_key_path"}:
                    raise ValueError("integration_original_observation_selection")
                argv = observation["argv"]
                if (type(argv) is not list or len(argv) != 11 or argv[:4] != [str(ROOT / ".venv/bin/python"),
                        str(ROOT / "tools/run_repeated_integrations.py"), "run", "--plan"] or argv[5:] != [
                            "--expected-plan", digest(plan), "--host", host["route"], "--directory", str(path.parent)]
                        or observation["plan"]["binding"] != {"integration_plan_sha256": digest(plan)}):
                    raise ValueError("integration_original_producer_selection")
                verified = read_command_receipt(observation["plan"], observation["attempt_id"], observation["argv"],
                    observer_key=read_private(Path(observation["observer_key_path"]), 128),
                    directory=observation["directory"], work_root=observation["work_root"],
                    expected_receipt_sha256=observation["expected_receipt_sha256"])
                if not verified["current_command_manifest_matches"]:
                    raise ValueError("integration_original_command_changed")
                if digest(_original(read_private(Path(argv[4]), 1_048_576))) != digest(plan):
                    raise ValueError("integration_original_producer_selection")
                if verified["coverage_gap"] is not None:
                    raise ValueError("integration_original_command_coverage_unknown")
                stdout_raw = read_private(Path(observation["directory"]) / (observation["attempt_id"] + ".stdout"), 1_048_576)
                retained = verified["receipt"]["streams"]["stdout"]
                if len(stdout_raw) != retained["retained_bytes"] or hashlib.sha256(stdout_raw).hexdigest() != retained["sha256"]:
                    raise ValueError("integration_original_stream_changed")
                stdout = _original(stdout_raw)
                if stdout.get("report_path") != str(path) or stdout.get("source_head_sha256") != selection["source_head_sha256"]:
                    raise ValueError("integration_original_command_output_changed")
                if hashlib.sha256(raw).hexdigest() != selection["raw_sha256"] or digest(report) != selection["source_head_sha256"]:
                    raise ValueError("integration_original_head_changed")
                if (report["plan_sha256"] != digest(plan) or report["candidate_sha256"] != plan["candidate_sha256"]
                        or report["host"] != host or report["scenario"] != scenario(host["route"])
                        or digest(host_identity(host["route"])) != host["host_sha256"]
                        or digest(candidate_manifest(ROOT)) != plan["candidate_sha256"]):
                    raise ValueError("integration_current_binding_changed")
                observed_at = verified["receipt"]["finished_at"]
                expires_at = plan["expires_at"]
                captured_responses = _responses(report, path.parent, host["route"])
                if report["failure"] is not None:
                    if any(row["projection"]["accepted"] is True or row["projection"]["integration_success"] is True for row in report["cycles"]):
                        raise ValueError("integration_failed_producer_positive_projection")
                else:
                    workspace, factory = Path(report["workspace"]), Path(report["factory"])
                    if workspace != path.parent / "fixture/workspace" or factory != path.parent / "fixture/fixture-private/services.py":
                        raise ValueError("integration_original_native_paths")
                    # Existing owner performs protected references, current
                    # configuration probes, full original journal/native proof.
                    proof = native_replay(workspace, factory, report["cycles"][-1]["run_id"])
                    probes = [event["data"]["record"]["data"]["probe"] for event in proof["journal"]["events"]
                              if event.get("data", {}).get("record", {}).get("kind") == "execution_qualification_probe"]
                    if not probes:
                        raise ValueError("integration_original_current_probe_missing")
                    observed_at, expires_at = proof["read_finished_utc"], min(
                        plan["expires_at"], probes[-1]["expires_utc"], key=_time)
                    runtime = HostRuntime.from_factory(factory, workspace)
                    with runtime.context.activate(), Workspace.open(workspace) as opened:
                        for index, row in enumerate(report["cycles"]):
                            with opened.research.session(runtime.policy, run_id=row["run_id"], execution=runtime.execution) as session:
                                records = runtime.execution.records(session)
                            if row is not report["cycles"][-1] and native_digest(records) != row["native_records_sha256"]:
                                raise ValueError("integration_original_history_changed")
                            captured_documents = captured_responses[index]
                            original_result = captured_documents["result"]
                            original_brief = captured_documents["brief"]
                            p = row["projection"]
                            applied = [r["data"] for r in records if r["kind"] == "execution_applied"]
                            accepted = original_result["outcome"] == "accepted" and original_brief["verdict"] == "accept"
                            if (p["accepted"] != accepted or p["useful"] != (accepted and reference_useful(original_brief))
                                    or p["integration_success"] != original_result["terminal"]
                                    or p["repair_attempts"] != sum(d.get("operation") == "repair" for d in applied)
                                    or p["review_attempts"] != applied_review_count(records)
                                    or p["stale_approval_reused"] != original_result.get("approval_reuse", False)
                                    or p["critical_failure"] != any(d.get("critical_failure") is True for d in applied)
                                    or p["supported_proposals"] != supported_proposals(plan, p)):
                                raise ValueError("integration_original_projection_changed")
                            if row["parent_run_id"] and (not original_result.get("changed_source_ids")
                                    or original_result.get("reused_source_ids") or original_result["cycle"]["current_basis_id"] ==
                                    next(r["basis_id"] for r in report["cycles"] if r["run_id"] == row["parent_run_id"])):
                                raise ValueError("integration_original_evidence_drift_not_detected")
                            if row["parent_run_id"]:
                                current_history = _manifest(workspace / "runs" / row["parent_run_id"])
                                if row["parent_history_before"] != row["parent_history_after"] or current_history != row["parent_history_after"]:
                                    raise ValueError("integration_original_parent_changed")
                    if proof["current"]["accounting"]["accepted_count"] != 1 or not proof["current"]["canonical_release"]["research_complete"]:
                        raise ValueError("integration_current_native_unavailable")
                if read_private(path, 1_048_576) != raw:
                    raise ValueError("integration_original_changed_during_read")
                cache.update(raw=raw, report=report, observed_at=observed_at, expires_at=expires_at)
            calls[0] += 1
            if "report" not in cache:
                raise ValueError("integration_original_read_unavailable")
            report = cache["report"]
            matches = [(i, row) for i, row in enumerate(report["cycles"]) if row["slot_id"] == slot["slot_id"]]
            if len(matches) != 1:
                raise ValueError("integration_original_slot_inventory")
            index, row = matches[0]
            if row["input_sha256"] != input_identity(host["route"], slot["cycle"]):
                raise ValueError("integration_original_input_changed")
            projection = row["projection"]
            return OwnerEvidence(raw=cache["raw"], source_head_sha256=selection["source_head_sha256"],
                candidate_sha256=report["candidate_sha256"], host_sha256=host["host_sha256"], input_sha256=row["input_sha256"],
                observed_at=cache["observed_at"], expires_at=cache["expires_at"], evidence_class="simulated",
                projection=projection, citations=(OriginalCitation("/cycles/" + str(index) + "/projection", projection),))

        result.extend(OwnerReader(s["slot_id"], s["owner_ref"], lambda s=s, read=read: read(s)) for s in slots)
    return tuple(result)


def evidence_pins(plan, originals):
    """Project explicit externally retained source pins; this is not a verifier."""
    return {s["slot_id"]: {key: originals[s["host_id"]][key] for key in ("raw_sha256", "source_head_sha256")}
            for s in plan["slots"] if s["host_id"] in originals}
