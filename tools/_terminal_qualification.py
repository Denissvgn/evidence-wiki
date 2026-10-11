"""Finite qualification joins over existing original evidence producers."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import math
import shlex
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools._acceptance_protocol import bounded_json, canonical_json, digest, hash_file
from tools._integration_costs import freeze_cost_plan, project_cost_report, validate_cost_plan
from tools._terminal_cases import automatic_cases
from tools.measure_integration_costs import _authenticated, _signed, read_command_receipt
from tools.run_comparative_trials import command_manifest, read_private
from tools.verify_acceptance_paths import candidate_manifest, read_acceptance_report

ROLES = ("Build", "Attack", "Integration", "Measurement", "Verification")
READERS = {"Build": {"archives"}, "Attack": {"attack", "automatic"},
           "Integration": {"transports"}, "Measurement": {"costs"}, "Verification": {"verification"}}
OUTCOMES = ("accepted", "expected_abstention", "failed", "cancelled", "budget_stopped", "externally_blocked")
CONTEXT_FIELDS = {"policy", "rubric", "evaluators", "decision_rules", "source_rights", "source_packets",
                  "reference_split", "reference_provenance", "cost_scope"}
TRANSPORT_SCOPE = {"business_executions": 1, "transport_journeys": 3, "request_mode": "exact_request_replay"}
INDEPENDENT_CONTROLS = ("old-profile-as-current", "wrapper-as-console", "cycle-only-as-full-journey",
                        "unrelated-tool-call", "missing-final", "unconfirmed-cleanup")
FINAL_AUDIT_OBLIGATIONS = {
    5: ["Verification original assignment, receipt and identity isolation"],
    7: ["Verification exact original tool/poll/final report custody"],
    8: ["Verification terminal and cleanup accounting"],
    9: ["One final all-five exact-candidate manifest"],
    10: ["All-five common-clock closing observation"],
    11: ["Verification complete reservation-to-final interval <=240 seconds"],
    30: ["Independent final all-five gate replay"],
    31: ["Final costs include original Measurement and Verification intervals"],
    33: ["Terminal owner joins sealed first four to actual Verification"],
    35: ["Verification authenticated terminal/final/cleanup and parent tool provenance"],
    36: ["Final all-five qualification closure with all required checks"],
}
LIMITATIONS = [
    "Qualification is artifact_checked fixture mechanics, not general semantic accuracy or production security.",
    "Copied trusted-host tool transcripts bind actual assignments; their hashes do not authenticate the platform or physical credential isolation.",
    "Same-user workers, native children, dependencies and restored transient mutations are outside hostile-worker containment proof.",
    "Provider usage, billing, customer adoption, human savings and business value remain unobserved unless their original qualified owners supply observations.",
    "Expected abstention passes its scenario only and never grants accepted research or publishing authority.",
]


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _sha(value):
    require(type(value) is str and len(value) == 64 and all(c in "0123456789abcdef" for c in value), "qualification_digest")


def _clock(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(parsed.utcoffset().total_seconds() == 0, "qualification_clock")
    return parsed


def _original(path, expected):
    """Read original private bytes against an externally retained commitment."""
    _sha(expected)
    raw = read_private(Path(path), 4_194_304)
    require(hashlib.sha256(raw).hexdigest() == expected, "qualification_original_changed")
    return bounded_json(json.loads(raw), 4_194_304)


def _actual_role_call(call, assignment, role, observation, plan, execution):
    """Keep the raw outer tool command distinct from its observed child argv."""
    if call.get("tool") != "exec_command" or call.get("agent_id") != assignment["agent_id"]:
        return False
    try:
        argv = shlex.split(call["command"])
        cwd = Path(call["cwd"]).resolve(strict=True)
        root = Path(plan["repo_root"])
        if cwd != root or len(argv) != 9:
            return False
        executable = Path(argv[0]) if Path(argv[0]).is_absolute() else cwd/argv[0]
        script = Path(argv[1]) if Path(argv[1]).is_absolute() else cwd/argv[1]
        if executable != root/".venv/bin/python" or script != root/"tools/qualify_unattended.py" or argv[2] != "observe":
            return False
        options = dict(zip(argv[3::2],argv[4::2],strict=True))
        if set(options) != {"--directory","--observer-key","--role"} or options["--role"] != role:
            return False
        if Path(options["--directory"]).resolve() != Path(execution["directory"]).parent:
            return False
        output = json.loads(call["output"])
        return output == {"role":role,"receipt_sha256":observation["receipt_sha256"]}
    except (ValueError, OSError, KeyError, TypeError):
        return False


def _role_provenance(role, row, job, plan, execution, key):
    provenance_ref = _authenticated(Path(execution["directory"]).parent/(role.lower()+".provenance.json"),key)
    provenance = _original(provenance_ref["path"],provenance_ref["sha256"])
    assignment = next(item for item in plan["assignments"] if item["role"] == role)
    require(provenance.get("source") == "trusted_host_tool_transcript"
        and all(provenance.get(field) == assignment[field] for field in ("agent_id","controller_ref","context_ref","task_path"))
        and provenance.get("plan_sha256") == plan["plan_sha256"] and provenance.get("candidate_sha256") == digest(plan["candidate"])
        and provenance.get("deadline_utc") == execution["deadline_utc"] and provenance.get("observed_child_argv") == job["argv"]
        and provenance.get("reports") == row["reports"] and provenance.get("receipt_sha256") == row["receipt_sha256"],
        "qualification_actual_role_job_binding")
    require(type(provenance.get("tool_calls")) is list and any(_actual_role_call(call,assignment,role,row,plan,execution)
        for call in provenance["tool_calls"]), "qualification_actual_role_tool_receipt_required")
    return provenance


def _outer_tool_matches(outer, nested, tool):
    """Join the exact prospective wrapper literal and original returned JSON."""
    request, returned = nested.get("request"), nested.get("returned_result")
    if outer.get("tool") == tool:
        return outer.get("request") == request and outer.get("returned_result") == returned
    if outer.get("tool") != "functions.exec" or type(outer.get("request")) is not str:
        return False
    prefix = "const request = "
    suffix = ";\nconst result = await tools." + tool + "(request);\ntext(JSON.stringify({request, result}));"
    code = outer["request"]
    if not code.startswith(prefix) or not code.endswith(suffix):
        return False
    try:
        if json.loads(code[len(prefix):-len(suffix)]) != request:
            return False
        observed = outer.get("returned_result")
        if type(observed) is str:
            payload = json.loads(observed)
        elif type(observed) is dict and type(observed.get("content")) is list:
            content = observed["content"]
            if len(content) != 1 or content[0].get("type") != "text":
                return False
            payload = json.loads(content[0]["text"])
        else:
            payload = observed
        return type(payload) is dict and set(payload) == {"request", "result"} and payload == {"request": request, "result": returned}
    except (ValueError, TypeError, KeyError):
        return False


def _raw_role_provenance(role, row, job, plan, execution, key):
    """Join original nested requests/results and their copied outer tool records."""
    provenance = _role_provenance(role, row, job, plan, execution, key)
    assignment = next(item for item in plan["assignments"] if item["role"] == role)
    ref = provenance.get("raw_execution_ref")
    require(type(ref) is dict and set(ref) == {"path", "sha256"}, "qualification_raw_tool_reference")
    raw = _original(ref["path"], ref["sha256"])
    require(raw.get("schema_version") == "qualification-raw-role-tool-transcript/v1" and raw.get("role") == role
        and raw.get("assignment_ref") == {"path": assignment["transcript_path"], "sha256": assignment["transcript_sha256"]}
        and raw.get("tool_calls") == provenance["tool_calls"], "qualification_raw_tool_original_binding")
    original_assignment = _original(assignment["transcript_path"], assignment["transcript_sha256"])
    spawn_identities = set()
    for call in original_assignment.get("tool_calls", []):
        request, result = call.get("request"), call.get("returned_result")
        require(type(request) is dict and type(result) is dict and type(request.get("message")) is str
            and request["message"], "qualification_raw_assignment_objects")
        supplied = [result[name] for name in ("agent_id", "agent_name", "task_name") if name in result]
        require(all(value in {assignment["agent_id"], assignment["task_path"]} for value in supplied),
            "qualification_raw_assignment_identity_conflict")
        if call.get("tool") == "collaboration.spawn_agent":
            require(request.get("task_name") == assignment["task_path"].rsplit("/", 1)[-1] and supplied,
                "qualification_raw_spawn_identity_missing")
            spawn_identities.update(supplied)
        elif call.get("tool") == "collaboration.followup_task":
            require(request.get("target") in spawn_identities, "qualification_raw_followup_spawn_binding")
    require(spawn_identities, "qualification_raw_original_spawn_missing")
    go_ref = raw.get("execution_assignment_ref")
    require(type(go_ref) is dict and set(go_ref) == {"path", "sha256"},
        "qualification_raw_execution_assignment_missing")
    go = _original(go_ref["path"], go_ref["sha256"])
    original_calls = original_assignment["tool_calls"]
    go_calls = go.get("tool_calls")
    require(type(go_calls) is list and go_calls[:len(original_calls)] == original_calls
        and len(go_calls) > len(original_calls), "qualification_raw_execution_spawn_ancestry")
    execution_messages = []
    for dispatch in go_calls[len(original_calls):]:
        request, returned = dispatch.get("request"), dispatch.get("returned_result")
        require(dispatch.get("tool") == "collaboration.followup_task" and type(request) is dict
            and request.get("target") in spawn_identities and type(request.get("message")) is str
            and request["message"] and type(returned) is dict,
            "qualification_raw_execution_dispatch_changed")
        require(all(returned[field] in spawn_identities for field in
            ("agent_id", "agent_name", "task_name") if field in returned),
            "qualification_raw_execution_dispatch_identity")
        execution_messages.append(request["message"])
    calls, outers = raw["tool_calls"], raw.get("outer_calls")
    require(type(outers) is list and outers, "qualification_raw_outer_calls_missing")
    indices = []
    for outer in outers:
        require(outer.get("tool") == "functions.exec" and type(outer.get("request")) in {str, dict}
            and bool(outer["request"]) and outer.get("returned_result") is not None
            and type(outer.get("observed_nested_call_indices")) is list,
            "qualification_raw_outer_call_shape")
        indices.extend(outer["observed_nested_call_indices"])
    require(all(type(index) is int for index in indices) and sorted(indices) == list(range(len(calls))),
        "qualification_raw_outer_nested_inventory")
    matched = False
    used_outer = []
    for call in calls:
        if call.get("tool") != "exec_command":
            continue
        request, returned, polls = call.get("request"), call.get("returned_result"), call.get("polls")
        require(type(request) is dict and request.get("cmd") == call.get("command")
            and Path(request.get("workdir", plan["repo_root"])).resolve() == Path(call.get("cwd", "")).resolve()
            and type(returned) is dict and type(returned.get("output")) is str and type(polls) is list,
            "qualification_raw_launch_changed")
        output = returned["output"]
        session = returned.get("session_id")
        require(not polls or (session is not None and returned.get("exit_code") is None),
            "qualification_raw_sync_poll_graft")
        terminal = returned
        nested = [(call, "exec_command")]
        for poll in polls:
            require(terminal.get("exit_code") is None, "qualification_raw_postterminal_poll")
            require(type(poll) is dict and type(poll.get("request")) is dict and type(poll.get("returned_result")) is dict
                and session is not None and poll["request"].get("session_id") == session
                and poll["request"].get("chars", "") == ""
                and type(poll["returned_result"].get("output")) is str,
                "qualification_raw_poll_chain_changed")
            terminal = poll["returned_result"]
            require(terminal.get("session_id") in {None, session}, "qualification_raw_poll_session_changed")
            output += terminal["output"]
            nested.append((poll, "write_stdin"))
        for item, tool in nested:
            index = item.get("outer_call_index")
            require(type(index) is int and 0 <= index < len(outers) and index not in used_outer
                and _outer_tool_matches(outers[index], item, tool), "qualification_raw_outer_nested_changed")
            used_outer.append(index)
        require(output == call.get("output") and terminal.get("exit_code") == 0,
            "qualification_raw_original_output_incomplete")
        if _actual_role_call(call, assignment, role, row, plan, execution):
            require(any(call["command"] in message for message in execution_messages),
                "qualification_raw_execution_command_not_assigned")
            matched = True
    require(matched, "qualification_raw_actual_role_missing")
    require(sorted(used_outer) == list(range(len(outers))), "qualification_raw_outer_extra_or_missing")
    return {"path": ref["path"], "sha256": ref["sha256"], "outer_calls": len(outers),
            "scope": "copied_original_tool_records; platform_authentication_and_physical_custody_unobserved"}


def _new_audit_expectations(adapter, job, independent):
    """Resolve a source-literal adjacent freeze, pinned before producer dispatch."""
    literals = {}
    for node in ast.parse(adapter.read_text()).body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and type(node.value.value) is str:
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in {"EXPECTATIONS_FILENAME", "EXPECTATIONS_SHA256"}:
                    require(target.id not in literals, "qualification_expectations_literal_duplicate")
                    literals[target.id] = node.value.value
    filename = literals.get("EXPECTATIONS_FILENAME", "frozen-expectations.json")
    require(Path(filename).name == filename and filename not in {"", ".", ".."}, "qualification_expectations_filename")
    path = adapter.with_name(filename)
    expected = hash_file(path)["sha256"]
    require(literals.get("EXPECTATIONS_SHA256") == expected and independent.get("expectations_sha256") == expected,
        "qualification_original_expectations_changed")
    expectations = _original(path, expected)
    if expectations.get("requirements_count") == 36:
        require("EXPECTATIONS_FILENAME" in literals and independent.get("expectations_path") == str(path.resolve())
            and "--expectations" in job["argv"] and job["argv"][job["argv"].index("--expectations") + 1] == str(path.resolve())
            and any(item["path"] == str(path.resolve()) and item["sha256"] == expected
            for item in job["command_manifest"]), "qualification_expectations_not_frozen_in_command")
    return expectations, expected


def _audit_job_declaration(job, timeout_seconds):
    """Pin a new freeze and its exact bounds before any producer can start."""
    if "--adapter" not in job["argv"]:
        return None
    adapter = Path(job["argv"][job["argv"].index("--adapter") + 1])
    literals = {target.id: node.value.value for node in ast.parse(adapter.read_text()).body
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and type(node.value.value) is str
        for target in node.targets if isinstance(target, ast.Name)
        and target.id in {"EXPECTATIONS_FILENAME", "EXPECTATIONS_SHA256"}}
    filename = literals.get("EXPECTATIONS_FILENAME", "frozen-expectations.json")
    require(Path(filename).name == filename, "qualification_expectations_filename")
    path = adapter.with_name(filename)
    expected = hash_file(path)["sha256"]
    expectations, expected = _new_audit_expectations(adapter, job,
        {"expectations_sha256": expected, "expectations_path": str(path.resolve())})
    if expectations.get("requirements_count") != 36:
        return None
    inventory = expectations.get("immutable_inventory", {})
    bounds = inventory.get("bounds", {})
    requirements = expectations.get("requirements")
    require(type(requirements) is list and len(requirements) == 36
        and all(type(row) is dict and type(row.get("id")) is str and row["id"] for row in requirements)
        and len({row["id"] for row in requirements}) == 36, "qualification_new_requirement_inventory")
    require(timeout_seconds == bounds.get("enclosing_seconds") == 7200
        and bounds.get("local_role_seconds") == {"Build": 180, "Attack": 4200, "Integration": 1800,
                                                  "Measurement": 60, "Verification": 240}
        and job["timeout_seconds"] == 240, "qualification_new_frozen_budget_inventory")
    require([row.get("id") for row in inventory.get("mandatory_negative_controls", [])] == list(INDEPENDENT_CONTROLS),
        "qualification_new_control_inventory")
    return {"path": str(path.resolve()), "sha256": expected,
            "requirement_ids": [row["id"] for row in requirements],
            "control_ids": list(INDEPENDENT_CONTROLS), "bounds": bounds}


def control_task_evidence(tasks):
    """Keep direct task facts and losslessly frame rich report results."""
    tasks = bounded_json(tasks, 4_194_304)
    require(type(tasks) is list, "qualification_control_task_shape")
    result = []
    for task in tasks:
        require(type(task) is dict and not
            {"report_results_json", "report_results_sha256"}.intersection(task),
            "qualification_control_task_encoding_collision")
        row = dict(task)
        if "reports" in row:
            require(type(row["reports"]) is list, "qualification_control_task_report_shape")
            raw = canonical_json(row.pop("reports"))
            row["report_results_json"] = raw.decode("utf-8")
            row["report_results_sha256"] = hashlib.sha256(raw).hexdigest()
        result.append(row)
    return bounded_json(result, 4_194_304)


def read_control_task_evidence(tasks):
    """Check canonical bytes and recover every original report result."""
    tasks = bounded_json(tasks, 4_194_304)
    require(type(tasks) is list, "qualification_control_task_shape")
    result = []
    for task in tasks:
        require(type(task) is dict and "reports" not in task,
            "qualification_control_task_unframed_reports")
        row = dict(task)
        fields = {"report_results_json", "report_results_sha256"}.intersection(row)
        require(not fields or len(fields) == 2, "qualification_control_task_encoding_incomplete")
        if fields:
            require(type(row["report_results_json"]) is str,
                "qualification_control_task_encoding_shape")
            raw = row.pop("report_results_json").encode("utf-8")
            require(hashlib.sha256(raw).hexdigest() == row.pop("report_results_sha256"),
                "qualification_control_task_report_digest")
            reports = bounded_json(json.loads(raw), 4_194_304)
            require(type(reports) is list and canonical_json(reports) == raw,
                "qualification_control_task_report_canonical")
            row["reports"] = reports
        result.append(row)
    return bounded_json(result, 4_194_304)


def _audit_original_joins(adapter, independent, plan, execution, preliminary, key):
    """Recompute evidence and exact mutation effects from original read owners."""
    deadline = _clock(execution["deadline_utc"])
    require(datetime.now(timezone.utc) <= deadline, "qualification_audit_owner_replay_deadline")
    before = hash_file(adapter)["sha256"]
    require(before == independent["adapter_sha256"], "qualification_audit_source_changed")
    spec = importlib.util.spec_from_file_location("_qualification_pinned_independent_reader", adapter)
    require(spec is not None and spec.loader is not None, "qualification_audit_reader_unavailable")
    reader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reader)
    data = reader.read_baseline(Path(execution["directory"]).parent, key, digest(preliminary),
                                independent["expectations_path"])
    require(data["plan"] == plan and data["execution"] == execution
        and data["preliminary"] == preliminary, "qualification_audit_original_baseline_changed")
    predicates = reader.evaluate(data, key)
    require(all(row["errors"] == [] for row in predicates), "qualification_audit_original_predicate_refused")
    baseline_errors = set(data["rebuilt"]["gaps"] + data["rebuilt"]["failures"])
    effects = []
    for control in INDEPENDENT_CONTROLS:
        require(datetime.now(timezone.utc) <= deadline, "qualification_audit_owner_replay_deadline")
        trace = []
        with reader.substituted_view(data, key, control, trace) as substitution:
            mutated = reconstruct(plan, execution, data["observations"], observer_key=key,
                                  as_of_utc=preliminary["finished_utc"])
        effects.append({"id": control, "substitution": substitution, "targeted_refusals": trace,
            "additional_errors": sorted(set(mutated["gaps"] + mutated["failures"]) - baseline_errors),
            "tasks": control_task_evidence(mutated["tasks"]), "started_tasks": mutated["denominators"]["started_tasks"],
            "started_stop_unknown_tasks": mutated["denominators"]["started_stop_unknown_tasks"]})
    require(hash_file(adapter)["sha256"] == before, "qualification_audit_source_changed")
    require(datetime.now(timezone.utc) <= deadline, "qualification_audit_owner_replay_deadline")
    return {"predicate_results": predicates, "controls": effects}


def _checked_audit_v3(audit, independent, expectations, expected_hash, plan, preliminary,
                      *, original_joins=None, execution=None, parent_receipt=None, parent_intent=None):
    """Validate every frozen predicate and actual control, retaining final duties."""
    requirements = expectations.get("requirements")
    require(expectations.get("requirements_count") == 36 and type(requirements) is list and len(requirements) == 36
        and all(type(row) is dict and type(row.get("id")) is str and row["id"] for row in requirements)
        and len({row["id"] for row in requirements}) == 36, "qualification_new_requirement_inventory")
    ids = [row["id"] for row in requirements]
    inventory = expectations.get("immutable_inventory", {})
    bounds = inventory.get("bounds", {})
    require(bounds.get("enclosing_seconds") == plan["timeout_seconds"] == 7200
        and bounds.get("local_role_seconds") == {"Build": 180, "Attack": 4200, "Integration": 1800,
                                                 "Measurement": 60, "Verification": 240}
        and all(job["timeout_seconds"] == bounds["local_role_seconds"][job["role"]] for job in plan["jobs"]),
        "qualification_new_frozen_budget_inventory")
    frozen_controls = [row.get("id") for row in inventory.get("mandatory_negative_controls", [])]
    require(frozen_controls == list(INDEPENDENT_CONTROLS), "qualification_new_control_inventory")
    scope = "complete_first_four_only_missing_verification"
    require(audit.get("schema_version") == "independent-qualification-audit/v3"
        and audit.get("original_expectations_sha256") == expected_hash
        and audit.get("original_report_sha256") == digest(preliminary)
        and audit.get("original_report_decision") == preliminary["decision"]
        and audit.get("plan_sha256") == plan["plan_sha256"]
        and audit.get("baseline_first_four_complete") is True and audit.get("baseline_scope") == scope
        and independent.get("baseline_scope") == scope
        and independent.get("original_preliminary_report_sha256") == digest(preliminary)
        and independent.get("requirement_inventory") == ids
        and independent.get("control_inventory") == frozen_controls, "qualification_new_audit_binding")
    results = audit.get("predicate_results")
    require(type(results) is list and [row.get("id") for row in results] == ids,
        "qualification_new_predicate_inventory")
    require(type(original_joins) is dict and type(original_joins.get("predicate_results")) is list
        and type(original_joins.get("controls")) is list
        and execution is not None and parent_receipt is not None and parent_intent is not None,
        "qualification_new_original_evidence_required")
    original_results = original_joins["predicate_results"]
    require(len(original_results) == 36, "qualification_new_original_predicate_inventory")
    obligations = []
    for ordinal, row in enumerate(results, 1):
        pending = FINAL_AUDIT_OBLIGATIONS.get(ordinal, [])
        require(row.get("status") == ("pass_with_final_obligations" if pending else "pass")
            and row.get("errors") == [] and type(row.get("evidence")) is list and row["evidence"]
            and all(type(evidence) is dict for evidence in row["evidence"])
            and row.get("final_obligations") == pending, "qualification_new_predicate_unqualified")
        obligations.extend({"id": row["id"], "obligation": duty} for duty in pending)
        if ordinal != 34:
            require(row == original_results[ordinal - 1], "qualification_new_predicate_original_join")
    controls = audit.get("controls")
    require(type(controls) is list and [row.get("id") for row in controls] == frozen_controls
        and audit.get("controls_executed") == audit.get("controls_passed") == 6
        and audit.get("all_rejections_passed") is True, "qualification_new_controls_not_executed")
    previous = _clock(parent_intent["reserved_at"])
    closing = _clock(parent_receipt["finished_at"])
    require(_clock(execution["started_utc"]) <= previous <= closing <= _clock(execution["deadline_utc"])
        and (closing - previous).total_seconds() <= 240, "qualification_new_parent_budget")
    require(len(original_joins["controls"]) == 6, "qualification_new_original_control_inventory")
    for row, original in zip(controls, original_joins["controls"], strict=True):
        targeted = {
            "old-profile-as-current": {"qualification_transport_binding"},
            "wrapper-as-console": {"qualification_real_console_required", "original_receipt_refusal:cost_receipt_binding_changed"},
            "cycle-only-as-full-journey": {"qualification_transport_original_output", "qualification_transport_exact_request_replay", "qualification_five_step_journey"},
            "unrelated-tool-call": {"qualification_actual_role_tool_receipt_required"},
            "missing-final": {"required_original_final_receipt_missing"},
            "unconfirmed-cleanup": {"unconfirmed_original_cleanup_substitution"},
        }
        substitution = row.get("substitution")
        require(row.get("executed") is True and row.get("pass") is True and row.get("additional_rejection") is True
            and type(row.get("targeted_refusals")) is list and row["targeted_refusals"]
            and all(type(reason) is str and reason for reason in row["targeted_refusals"])
            and row.get("errors") == [] and type(row.get("additional_errors")) is list and row["additional_errors"]
            and all(type(error) is str and error for error in row["additional_errors"])
            and type(substitution) is dict
            and substitution.get("control") == row["id"] and type(substitution.get("target_path")) is str
            and type(substitution.get("substitution_class")) is str
            and type(row.get("tasks")) is list and row["tasks"], "qualification_new_control_unqualified")
        require(bool(set(row["targeted_refusals"]) & targeted[row["id"]]), "qualification_new_control_target_refusal")
        read_control_task_evidence(row["tasks"])
        require(type(row.get("started_utc")) is str and type(row.get("finished_utc")) is str
            and previous <= _clock(row["started_utc"]) <= _clock(row["finished_utc"]) <= closing,
            "qualification_new_control_chronology")
        previous = _clock(row["finished_utc"])
        changed = substitution.get("changed_scope")
        require(type(changed) is list and changed and all(type(item) is dict for item in changed),
            "qualification_new_control_mutation_scope")
        if row["id"] in {"missing-final", "unconfirmed-cleanup"}:
            require(any(task.get("role") == "Build" and task.get("started") is True
                and task.get("terminal_observed") is False for task in row["tasks"]),
                "qualification_new_control_lost_start_or_fake_terminal")
        for field in ("original_transport_sha256", "original_view_sha256"):
            _sha(substitution.get(field))
        if row["id"] == "missing-final":
            require(substitution.get("mutated_view_sha256") is None, "qualification_missing_final_fabricated_bytes")
        else:
            _sha(substitution.get("mutated_view_sha256"))
        require(substitution["original_view_sha256"] != substitution.get("mutated_view_sha256"),
            "qualification_new_control_not_mutated")
        require(all(row.get(field) == original.get(field) for field in
            ("id", "substitution", "targeted_refusals", "additional_errors", "tasks",
             "started_tasks", "started_stop_unknown_tasks")), "qualification_new_control_original_join")
        target_role = "Integration" if row["id"] in INDEPENDENT_CONTROLS[:3] else "Build"
        require(any(task.get("role") == target_role and task.get("started") is True
            and task.get("outcome") != "accepted" for task in row["tasks"]),
            "qualification_new_control_target_still_accepted")
    deadline = min(_clock(execution["deadline_utc"]),
                   _clock(parent_intent["reserved_at"]) + timedelta(seconds=240))
    require(results[33]["evidence"] == [{"actual_controls": controls,
        "original_deadline_utc": deadline.isoformat()}], "qualification_new_control_predicate_original_join")
    return {"check_inventory": ids, "final_obligations": obligations,
            "control_inventory": frozen_controls, "freeze_sha256": expected_hash}


def _final_audit_closure(plan, execution, observations, tasks, started_cases, cost_plan, cost_events,
                         *, key, preliminary, complete):
    """Discharge deferred duties from authenticated all-five original owners."""
    verification = tasks[-1]
    audits = [ref["result"] for ref in verification["reports"]
              if ref["result"].get("audit_schema") == "independent-qualification-audit/v3"]
    if not audits:
        return None, []
    require(len(audits) == 1 and complete, "qualification_final_audit_required_work_incomplete")
    deadline = _clock(execution["deadline_utc"])
    require(datetime.now(timezone.utc) <= deadline, "qualification_final_audit_deadline")
    ready_for_verification(preliminary)
    audit = audits[0]
    require(len(observations) == 5 and all(task["started"] and task["terminal_observed"]
        and task["outcome"] == "accepted" for task in tasks), "qualification_final_role_closure")
    require(len(started_cases) == 106 and all(row["terminal_observed"] for row in started_cases),
        "qualification_final_case_closure")
    costs = project_cost_report(cost_plan, cost_events)
    require(costs["coverage"]["missing_attempt_ids"] == [] and costs["coverage"]["missing_metrics"] == [],
        "qualification_final_cost_closure")
    evidence = []
    for job in plan["jobs"]:
        row = next(item for item in observations if item["role"] == job["role"])
        raw = _raw_role_provenance(job["role"], row, job, plan, execution, key)
        evidence.append({"role": job["role"], "receipt_sha256": row["receipt_sha256"],
                         "reports": row["reports"], "original_raw_tool_reference": raw})
        require(datetime.now(timezone.utc) <= deadline, "qualification_final_audit_deadline")
    manifest = {"schema_version": "qualification-final-evidence-manifest/v1", "plan_sha256": plan["plan_sha256"],
        "candidate_sha256": digest(plan["candidate"]), "execution_sha256": digest(execution),
        "preliminary_sha256": digest(preliminary), "original_expectations_sha256": audit["freeze_sha256"],
        "independent_audit": audit["audit_reference"], "roles": evidence,
        "started_case_inventory_sha256": digest(started_cases), "started_case_nodes": 106,
        "original_cost_report_sha256": digest(costs), "required_role_count": 5,
        "controls": audit["control_inventory"], "limitations": LIMITATIONS}
    path = Path(execution["directory"]).parent / "evidence-manifest.json"
    if not path.exists():
        return {"path": str(path), "manifest": manifest, "obligations": audit["final_obligations"],
                "complete": False}, ["qualification_final_manifest_not_published"]
    original = _authenticated(path, key)
    require(original == _signed(manifest, key), "qualification_final_manifest_changed")
    manifest_sha256 = hash_file(path)["sha256"]
    require(datetime.now(timezone.utc) <= deadline, "qualification_final_audit_deadline")
    return {"path": str(path), "sha256": manifest_sha256, "manifest": manifest,
            "obligations": [{**row, "status": "passed", "original_manifest_sha256": manifest_sha256}
                            for row in audit["final_obligations"]], "complete": True}, []


def policy():
    return {"schema_version": "unattended-qualification-policy/v1", "roles": list(ROLES),
            "required_readers": {role: sorted(kinds) for role, kinds in READERS.items()},
            "automatic_cases_sha256": digest(automatic_cases()), "retry_budget": 0,
            "workers": 1, "all_started_terminal": True, "required_failure_blocks": True,
            "expected_abstention_is_research_acceptance": False,
            "transport_scope": dict(TRANSPORT_SCOPE),
            "measurement_required_metrics": ["elapsed_seconds"], "maximum_seconds": 7200}


def ready_for_verification(preliminary):
    """Require positive original producer completion before mutation controls."""
    require(preliminary.get("preliminary") is True and preliminary.get("decision") == "not_qualified"
        and preliminary.get("failures") == [] and preliminary.get("gaps") == ["required_job_missing:Verification"],
        "qualification_verification_baseline_incomplete")
    tasks = preliminary.get("tasks")
    require(type(tasks) is list and [row.get("role") for row in tasks] == list(ROLES),
        "qualification_verification_baseline_inventory")
    for row in tasks[:-1]:
        require(row.get("started") is True and row.get("terminal_observed") is True and row.get("outcome") == "accepted"
            and {ref.get("kind") for ref in row.get("reports",[])} == READERS[row["role"]]
            and all(ref.get("result",{}).get("passed") is True for ref in row["reports"]),
            "qualification_verification_producer_incomplete")
    require(tasks[-1].get("started") is False and tasks[-1].get("reports") == []
        and preliminary.get("denominators",{}).get("started_tasks") == 4
        and preliminary.get("denominators",{}).get("terminal_tasks") == 4,
        "qualification_verification_baseline_terminal_gap")
    return True


def freeze_plan(*, repo_root, assignments_path, assignments_sha256, jobs, context, timeout_seconds=7200):
    """Freeze commands, roles and original obligations before starting a clock."""
    root = Path(repo_root).resolve(strict=True)
    require(type(timeout_seconds) in {int, float} and math.isfinite(timeout_seconds)
            and 0 < timeout_seconds <= 7200, "qualification_budget")
    context = bounded_json(context)
    require(type(context) is dict and set(context) == CONTEXT_FIELDS, "qualification_context_inventory")
    assignments = _original(assignments_path, assignments_sha256)
    require(type(assignments) is list and len(assignments) == len(ROLES), "qualification_assignment_inventory")
    for item, role in zip(assignments, ROLES, strict=True):
        require(type(item) is dict and set(item) == {"role", "agent_id", "controller_ref", "context_ref",
            "task_path", "transcript_path", "transcript_sha256"} and item["role"] == role,
            "qualification_assignment_shape")
        transcript = _original(item["transcript_path"], item["transcript_sha256"])
        require(transcript.get("agent_id") == item["agent_id"] and transcript.get("task_path") == item["task_path"]
                and transcript.get("controller_ref") == item["controller_ref"] and transcript.get("context_ref") == item["context_ref"]
                and transcript.get("source") == "trusted_host_tool_transcript"
                and type(transcript.get("tool_calls")) is list and transcript["tool_calls"], "qualification_assignment_original")
        require(any(call.get("tool") in {"collaboration.spawn_agent", "collaboration.followup_task"}
            and call.get("agent_id") == item["agent_id"] and call.get("task_path") == item["task_path"]
            for call in transcript["tool_calls"]), "qualification_actual_agent_assignment_required")
    for field in ("agent_id", "controller_ref", "context_ref", "task_path", "transcript_path"):
        require(all(type(row[field]) is str and row[field] for row in assignments)
                and len({row[field] for row in assignments}) == len(ROLES), "qualification_roles_not_distinct")
    jobs = bounded_json(jobs)
    require(type(jobs) is list and len(jobs) == len(ROLES), "qualification_job_inventory")
    frozen = []
    for job, role in zip(jobs, ROLES, strict=True):
        require(type(job) is dict and set(job) == {"role", "argv", "timeout_seconds", "reports"}
                and job["role"] == role and type(job["argv"]) is list and job["argv"]
                and all(type(part) is str and part for part in job["argv"]), "qualification_job_shape")
        require(type(job["timeout_seconds"]) in {int, float} and math.isfinite(job["timeout_seconds"])
                and 0 < job["timeout_seconds"] <= timeout_seconds, "qualification_job_budget")
        refs = job["reports"]
        require(type(refs) is list and {row.get("kind") for row in refs} == READERS[role]
                and len(refs) == len(READERS[role]), "qualification_required_report_inventory")
        for ref in refs:
            require(type(ref) is dict and set(ref) == {"kind", "path"}
                    and type(ref["path"]) is str and Path(ref["path"]).is_absolute(), "qualification_report_path")
        selected = {**job, "command_manifest": command_manifest(job["argv"])}
        declaration = _audit_job_declaration(selected, timeout_seconds) if role == "Verification" else None
        if declaration is not None:
            selected["independent_requirements"] = declaration
        frozen.append(selected)
    declaration = frozen[-1].get("independent_requirements")
    if declaration is not None:
        require(all(job["timeout_seconds"] == declaration["bounds"]["local_role_seconds"][job["role"]]
            for job in frozen), "qualification_new_frozen_budget_inventory")
    value = {"schema_version": "unattended-qualification-plan/v1", "repo_root": str(root), "policy": policy(),
             "candidate": candidate_manifest(root), "context": context, "assignments": assignments,
             "assignments_path": str(Path(assignments_path).resolve()), "assignments_sha256": assignments_sha256,
             "jobs": frozen, "timeout_seconds": timeout_seconds,
             "entrypoint_template": (root/".venv/bin/evidence-wiki").read_text().split("\n",1)[1]}
    value["plan_sha256"] = digest(value)
    return value


def validate_plan(value):
    value = bounded_json(value, 4_194_304)
    require(type(value) is dict and set(value) == {"schema_version", "repo_root", "policy", "candidate", "context",
            "assignments", "assignments_path", "assignments_sha256", "jobs", "timeout_seconds", "entrypoint_template", "plan_sha256"}, "qualification_plan_shape")
    require(value["schema_version"] == "unattended-qualification-plan/v1" and value["policy"] == policy()
            and value["plan_sha256"] == digest({key: item for key, item in value.items() if key != "plan_sha256"}), "qualification_plan_changed")
    rebuilt = freeze_plan(repo_root=value["repo_root"], assignments_path=value["assignments_path"],
        assignments_sha256=value["assignments_sha256"], jobs=[{k: v for k, v in row.items() if k not in {"command_manifest", "independent_requirements"}}
            for row in value["jobs"]], context=value["context"], timeout_seconds=value["timeout_seconds"])
    require(rebuilt == value, "qualification_candidate_or_assignment_drift")
    return value


def command_terminal(checked, *, intent, deadline_utc, expected_exit=0):
    """Classify actual retained observations; absent work cannot become a pass."""
    receipt = checked["receipt"]
    if receipt["status"] == "interrupted":
        return "cancelled"
    failure = receipt.get("failure") or ""
    if "timeout" in failure.lower() or "closure_capacity" in failure:
        return "budget_stopped"
    if receipt.get("started_at") is None:
        return "externally_blocked"
    started, finished = _clock(receipt["started_at"]), _clock(receipt["finished_at"])
    require(_clock(intent["reserved_at"]) <= started <= finished, "qualification_clock_order")
    within = finished <= _clock(deadline_utc) and (finished - _clock(intent["reserved_at"])).total_seconds() <= float(intent["timeout_seconds"])
    clean = (checked["current_command_manifest_matches"] is True and checked["coverage_gap"] is None
             and receipt["status"] == ("succeeded" if expected_exit == 0 else "failed")
             and receipt["exit_code"] == expected_exit and receipt["failure"] is None
             and receipt["output_overflow"] is False and receipt["output_reader_work_unresolved"] is False
             and receipt["meter_work_unresolved"] is False and receipt["command_manifest_after"] == intent["command_manifest"])
    return "accepted" if clean and within else "budget_stopped" if not within else "failed"


def _acceptance(ref, plan, key, catalog):
    report = read_acceptance_report(ref["path"], observer_key=key, expected_report_sha256=ref["report_sha256"])
    from tools._acceptance_cases import acceptance_cases
    cases = acceptance_cases() if catalog == "attack" else automatic_cases()
    require(report["manifest"] is not None and report["manifest"]["cases"] == cases
            and report["manifest"]["inputs"]["candidate"] == plan["candidate"], "qualification_acceptance_binding")
    events = [json.loads(line) for line in read_private(Path(ref["path"]).parent/"execute.events.jsonl",1_048_576).splitlines()]
    started = [row["nodeid"] for row in events if row.get("event") == "started"]
    finished = {row["nodeid"] for row in events if row.get("event") == "finished"}
    execute = next(row for row in report["processes"] if row["attempt_id"] == "execute")
    receipt = execute.get("receipt") or {}
    stop_known = receipt.get("finished_at") is not None and receipt.get("exit_code") is not None and receipt.get("output_reader_work_unresolved") is False
    nodes = []
    for node in started:
        case_id = report["manifest"]["membership"][node]
        failed = any(row.get("event") == "report" and row.get("nodeid") == node and row.get("outcome") == "failed" for row in events)
        if node in finished:
            phases = [row for row in events if row.get("event") == "report" and row.get("nodeid") == node]
            clean = len(phases) == 3 and all(row["outcome"] == "passed" and row["wasxfail"] is None for row in phases)
            outcome = "failed" if failed else "externally_blocked" if not clean else "accepted" if catalog == "automatic" and case_id in {"supported-claim","judge-disagreement"} else "expected_abstention"
        else:
            outcome = "cancelled" if receipt.get("status") == "interrupted" else "budget_stopped" if "timeout" in (receipt.get("failure") or "").lower() else "externally_blocked"
        nodes.append({"nodeid":node,"case_id":case_id,"outcome":outcome,"terminal_observed":node in finished or stop_known,
            "research_accepted":False,"scope":"conformance_obligation; original native claims retain their owner verdicts"})
    return {"passed": report["grade"]["status"] == "PASS", "grade": report["grade"], "started_nodes":nodes,
            "manifest_sha256": report["manifest"]["manifest_sha256"], "scope": report["catalog_scope"]}


def _archives(ref, plan):
    report = _original(ref["path"], ref["sha256"])
    from tools.validate_installed_artifacts import (
        check_archive_membership,
        check_archive_source_bytes,
        source_identity,
        source_layout,
    )
    require(set(report) == {"schema_version", "candidate_sha256", "wheel", "sdist"}
            and report["schema_version"] == "qualification-archives/v1"
            and report["candidate_sha256"] == digest(plan["candidate"]), "qualification_archive_report")
    files = {}
    for kind in ("wheel", "sdist"):
        row = report[kind]
        require(type(row) is dict and set(row) == {"path", "sha256", "bytes"}, "qualification_archive_shape")
        files[kind] = Path(row["path"])
        require(hash_file(files[kind]) == {key: row[key] for key in ("sha256", "bytes")}, "qualification_archive_changed")
    layout, source_files = source_layout(Path(plan["repo_root"]))
    check_archive_membership(files["wheel"], files["sdist"])
    check_archive_source_bytes(files["wheel"], files["sdist"], source_identity(source_files), layout)
    return {"passed": True, "artifacts": {kind: report[kind] for kind in files}}


def transport_requests(run_id):
    """The same public requests are observed through each installed adapter."""
    requests = []
    for operation in ("current", "next-work", "run-cycle", "brief", "refresh"):
        arguments = {"run_id": run_id}
        if operation == "run-cycle":
            arguments.update(request_id=run_id+"-drive", max_steps=64)
        elif operation == "refresh":
            arguments.update(run_id=run_id+"-refresh", previous_run_id=run_id,
                request_id=run_id+"-refresh-drive", max_steps=1, planner_config_ref="planner")
        requests.append({"schema_version":"research-command-request/v1", "operation":operation, "arguments":arguments})
    return requests


def replay_inventory(rows, identity_fields):
    """Count original identities once and preserve every route's owner receipt."""
    inventory, memberships = {}, {route:set() for route in ("python", "console", "stdio")}
    for row in rows:
        route = row["route"]
        require(route in memberships, "qualification_replay_route")
        identity = tuple(row[field] for field in identity_fields)
        require(all(type(value) is str and value for value in identity), "qualification_replay_identity")
        require(identity not in memberships[route], "qualification_replay_duplicate_observation")
        memberships[route].add(identity)
        original = {name:value for name,value in row.items() if name not in {"route", "original_producer_receipt_sha256"}}
        observation = {"route":route, "original_producer_receipt_sha256":row["original_producer_receipt_sha256"]}
        _sha(observation["original_producer_receipt_sha256"])
        if identity in inventory:
            require(inventory[identity]["original"] == original, "qualification_replay_original_conflict")
            inventory[identity]["route_observations"].append(observation)
        else:
            inventory[identity] = {"original":original, "route_observations":[observation]}
    require(memberships["python"] == memberships["console"] == memberships["stdio"], "qualification_replay_inventory_gap")
    return [{**item["original"], "route_observations":item["route_observations"]} for item in inventory.values()]


def console_request(argv, workspace, services):
    """Read operation arguments from the originally observed console argv."""
    parts = argv[3:]
    require(parts.count("--noninteractive") == 1, "qualification_console_noninteractive")
    parts = [part for part in parts if part != "--noninteractive"]
    require(len(parts) % 2 == 0 and len(parts[::2]) == len(set(parts[::2])), "qualification_console_request_options")
    options = dict(zip(parts[::2],parts[1::2],strict=True))
    require(options.pop("--target") == workspace and options.pop("--services") == services
        and options.pop("--format") == "json", "qualification_console_request_target")
    arguments = {name.removeprefix("--").replace("-","_"):value for name,value in options.items()}
    if "max_steps" in arguments:
        arguments["max_steps"] = int(arguments["max_steps"])
    return {"schema_version":"research-command-request/v1", "operation":argv[2], "arguments":arguments}


def stdio_request(machine_input, workspace):
    """Read the tool request from the original input committed by the observer."""
    calls = [item for item in map(json.loads,machine_input.splitlines()) if item.get("method") == "tools/call"]
    require(len(calls) == 1, "qualification_stdio_request_inventory")
    arguments = dict(calls[0]["params"]["arguments"])
    require(arguments.pop("target") == workspace, "qualification_stdio_request_target")
    return {"schema_version":"research-command-request/v1",
        "operation":calls[0]["params"]["name"].removeprefix("research_").replace("_","-"),"arguments":arguments}


def python_transport_command(command, row, installation, plan, deadline_utc, intent):
    """Join the installed driver to its frozen producer and actual arguments."""
    argv = command["argv"]
    require(argv[:3] == [installation["interpreter"],"-I","-B"] and len(argv) == 12
        and argv[4] == "python", "qualification_real_installed_python_required")
    driver = Path(argv[3])
    producer = next(item for item in plan["jobs"] if item["role"] == "Integration")
    script = Path(producer["argv"][2])
    require(any(item["path"] == str(script.resolve()) and item["sha256"] == hash_file(script)["sha256"]
        for item in producer["command_manifest"]), "qualification_python_producer_not_pinned")
    literals = [node.value.value for node in ast.parse(script.read_text()).body if isinstance(node,ast.Assign)
        and any(isinstance(target,ast.Name) and target.id == "DRIVER" for target in node.targets)
        and isinstance(node.value,ast.Constant) and type(node.value.value) is str]
    require(len(literals) == 1 and driver.read_bytes() == literals[0].encode()
        and any(item["path"] == str(driver.resolve()) and item["sha256"] == hash_file(driver)["sha256"]
            for item in intent["command_manifest"]), "qualification_python_driver_not_pinned")
    parts = [part.split("=",1) for part in argv[5:]]
    require(all(len(part) == 2 for part in parts) and len({part[0] for part in parts}) == len(parts),
        "qualification_python_request_options")
    options = dict(parts)
    require(set(options) == {"--bundle","--workspace","--services","--run-id","--deadline","--allowance-deadline","--timeout-seconds"}
        and options["--bundle"] == str(driver.parent/"public-bundle")
        and options["--workspace"] == row["workspace"] and options["--services"] == row["services"]
        and options["--run-id"] == row["run_id"] and options["--deadline"] == deadline_utc
        and float(options["--timeout-seconds"]) == producer["timeout_seconds"]
        and _clock(intent["reserved_at"]) < _clock(options["--allowance-deadline"]) <= _clock(deadline_utc),
        "qualification_python_runtime_binding")


def _opening_transport_current(current, next_current, workspace):
    """Retain populated originals while accepting an owner-declared absent run."""
    require(current["run_id"] is None and next_current["run_id"] is None
        and current["run"] is None and next_current["run"] is None
        and current["target"] == workspace,
        "qualification_uninitialized_transport_context")
    require({key:value for key,value in current.items() if key != "observed_utc"}
        == {key:value for key,value in next_current.items() if key != "observed_utc"},
        "qualification_opening_observations_changed")
    _clock(current["observed_utc"])
    _clock(next_current["observed_utc"])
    execution, accounting = current["execution"], current["accounting"]
    require(type(execution) is dict and type(execution.get("steps_used")) is int and execution["steps_used"] == 0
        and all(name in execution and execution[name] is None
            for name in ("accounting","measurement_report","pending"))
        and current["canonical_release"].get("research_complete") is False
        and all(value is None for value in current["usage"].values())
        and current["authorized"] is False and current["evidence_accepted"] is False,
        "qualification_opening_execution_not_empty")
    identifiers, outcomes = accounting["original_ids"], accounting["original_outcomes"]
    require(identifiers and len(set(identifiers)) == len(identifiers)
        and len(outcomes) == len(identifiers) and all(type(row) is dict for row in outcomes)
        and [row.get("original_id") for row in outcomes] == identifiers
        and accounting["accounting_complete"] is True and accounting["missing_mapping"] is False
        and accounting["accepted_count"] == 0 and accounting["unresolved_count"] == len(identifiers)
        and all(row.get("accepted") is False and type(row.get("original_text")) is str
            and row["original_text"].strip() and type(row.get("questions")) is list
            and row["questions"] and all(type(question) is dict and question.get("accepted") is False
                for question in row["questions"])
            for row in outcomes), "qualification_opening_original_inventory")
    return current


def _opening_native_binding(opening, result):
    """Join the genuine opening originals to the complete later native owner."""
    current = result["current"]
    require(current["run_id"] == result["run_id"]
        and opening["target"] == result["workspace"]
        and opening["policy_id"] == current["policy_id"] == result["policy"]["policy_id"]
        and opening["execution"]["profile_id"] == "sha256:"+result["profile"]["profile_sha256"]
        and opening["execution"]["scope_id"] == current["execution"]["scope_id"]
        and opening["execution"]["source_bindings"] == current["execution"]["source_bindings"]
        and opening["accounting"]["original_ids"] == current["accounting"]["original_ids"]
        and [(row["original_id"],row["original_text"]) for row in opening["accounting"]["original_outcomes"]]
            == [(row["original_id"],row["original_text"]) for row in current["accounting"]["original_outcomes"]],
        "qualification_opening_native_identity_changed")


def transport_responses(observed, run_id, *, route=None, first_route=False, workspace=None):
    """Each exact operation must return its usable operation-specific result."""
    from evidence_wiki.research_session_contracts import CURRENT_V2, NEXT_V2, NEXT_V3, NEXT_V4, checked
    from evidence_wiki.workflow_contracts import WorkflowContractError
    require([item.get("operation") for item in observed] == [item["operation"] for item in transport_requests(run_id)],
        "qualification_five_step_journey")
    require(all(item.get("status") == "returned" and item.get("error") is None
        and type(item.get("result")) is dict and item["result"] for item in observed),
        "qualification_required_operation_refused")
    current, next_work = observed[0]["result"], observed[1]["result"]
    require(type(current.get("accounting")) is dict and type(next_work.get("current")) is dict
        and type(next_work.get("actions")) is list and "decision" in next_work
        and (next_work["decision"] is None or type(next_work["decision"]) is dict),
        "qualification_current_next_result_binding")
    require(current.get("schema_version") == CURRENT_V2
        and next_work.get("schema_version") in {NEXT_V2,NEXT_V3,NEXT_V4},
        "qualification_current_next_schema")
    try:
        checked(CURRENT_V2,current)
        checked(next_work["schema_version"],next_work)
    except WorkflowContractError as error:
        raise ValueError("qualification_current_next_owner_contract_invalid") from error
    if current["run_id"] == run_id and next_work["current"]["run_id"] == run_id:
        return None
    require(route == "python" and first_route is True and type(workspace) is str
        and next_work["decision"] is None and next_work["actions"], "qualification_current_next_result_binding")
    return _opening_transport_current(current,next_work["current"],workspace)


def stdio_transport_command(argv, row, console):
    require(argv == [console,"serve-onboarding-mcp","--allow-root",row["workspace"],
        "--research-services",row["services"],"--allow-research-operation","run-cycle",
        "--allow-research-operation","refresh"], "qualification_real_stdio_required")


def native_journal_records(snapshot, workspace):
    """Validate the complete source chain authenticated by the fixed producer."""
    from evidence_wiki.mutation_contracts import digest as native_digest
    directory = Path(workspace)/'runs'/snapshot['run_id']
    require(snapshot['source_path'] == str(directory/'events.jsonl')
        and snapshot['state_path'] == str(directory/'run-state.json')
        and snapshot['state']['run_id'] == snapshot['run_id'], "qualification_native_journal_namespace")
    state_raw = snapshot['original_state_json'].encode()
    require(hashlib.sha256(state_raw).hexdigest() == snapshot['state_sha256']
        and json.loads(state_raw) == snapshot['state'], "qualification_native_original_state_bytes")
    raw = b''.join((json.dumps(item,sort_keys=True,separators=(',',':'))+'\n').encode() for item in snapshot['events'])
    require(hashlib.sha256(raw).hexdigest() == snapshot['source_sha256'], "qualification_native_journal_original_bytes")
    events, records, ids = snapshot['events'], [], set()
    for event in events:
        require(event['run_id'] == snapshot['run_id'] and event['event_id'] not in ids,
            "qualification_native_journal_event_identity")
        ids.add(event['event_id'])
        record = (event.get('data') or {}).get('record')
        if record is None:
            continue
        require(record['schema_version'] == 'research-execution-journal/v1' and record['sequence'] == len(records)
            and record['request_id'] == event['data']['request_id']
            and record['binding_id'] == native_digest(snapshot['state']['research_session'])
            and record['previous_record_ref'] == (native_digest(records[-1]) if records else None),
            "qualification_native_journal_chain")
        _sha(record['authentication'])
        records.append(record)
    require(records, "qualification_native_journal_empty")
    return records


def probe_only_extension(previous, current, workspace):
    """Permit fresh owner witnesses while preserving every original business byte."""
    before, after = native_journal_records(previous,workspace), native_journal_records(current,workspace)
    require(previous['run_id'] == current['run_id']
        and current['events'][:len(previous['events'])] == previous['events']
        and after[:len(before)] == before, "qualification_native_journal_ancestor_conflict")
    old_state = {key:value for key,value in previous['state'].items() if key != 'updated_at'}
    new_state = {key:value for key,value in current['state'].items() if key != 'updated_at'}
    require(old_state == new_state, "qualification_native_business_state_changed")
    prior_probes = [row['data']['probe'] for row in before if row['kind'] == 'execution_qualification_probe']
    prior = prior_probes[-1] if prior_probes else None
    requests = {row['request_id'] for row in before}
    tail = current['events'][len(previous['events']):]
    require(len(tail) == len(after)-len(before), "qualification_native_nonprobe_event_extension")
    for event in tail:
        record = (event.get('data') or {}).get('record') or {}
        require(event.get('event_type') == 'custom.research.decision'
            and event.get('from_state') is None and event.get('to_state') is None
            and record.get('kind') == 'execution_qualification_probe'
            and record['request_id'] not in requests and event['agent_id'] == previous['state']['agent_id'],
            "qualification_native_nonprobe_event_extension")
        requests.add(record['request_id'])
        probe = record['data']['probe']
        elapsed = (_clock(probe['expires_utc'])-_clock(probe['observed_utc'])).total_seconds()
        require(prior is not None and 0 < elapsed <= 30 and probe['reasons'] == []
            and not probe.get('configuration_reasons')
            and _clock(probe['observed_utc']) <= _clock(record['data']['completed_utc']) < _clock(probe['expires_utc'])
            and all(probe[field] == prior[field] for field in ('profile_sha256','registry_sha256','semantic_basis_id','configurations')),
            "qualification_native_current_witness_extension_invalid")
        prior = probe
    if tail:
        require(current['state']['updated_at'] == tail[-1]['occurred_at'], "qualification_native_state_timestamp_conflict")


def native_read_epoch(result, receipt):
    records = native_journal_records(result['journal'],result['workspace'])
    probes = [row['data']['probe'] for row in records if row['kind'] == 'execution_qualification_probe']
    require(probes and result['current']['qualification']['state'] == 'qualified', "qualification_native_current_unknown")
    probe = probes[-1]
    require(_clock(receipt['started_at']) <= _clock(result['read_started_utc'])
        <= _clock(result['read_finished_utc']) <= _clock(receipt['finished_at'])
        and _clock(probe['observed_utc']) <= _clock(result['read_started_utc'])
        <= _clock(result['read_finished_utc']) < _clock(probe['expires_utc'])
        and 0 < (_clock(probe['expires_utc'])-_clock(probe['observed_utc'])).total_seconds() <= 30
        and probe['reasons'] == [] and not probe.get('configuration_reasons'), "qualification_native_read_epoch_expired")
    return records


def _transport_command_order(receipt, previous_finished):
    """Join actual journey receipt epochs in the declared route order."""
    started, finished = _clock(receipt["started_at"]), _clock(receipt["finished_at"])
    require(finished >= started and (previous_finished is None or started >= previous_finished),
        "qualification_transport_command_order")
    return finished


def read_transports(ref, plan, key, *, deadline_utc, archives):
    """Require actual console/stdio transcripts with complete native outcomes."""
    report = _original(ref["path"], ref["sha256"])
    require(report.get("schema_version") == "qualification-transports/v1"
            and report.get("candidate_sha256") == digest(plan["candidate"])
            and report.get("context") == plan["context"] and report.get("interactive_requests") == []
            and report.get("profile_version") == "research-execution-profile/v13", "qualification_transport_binding")
    require(report.get("execution_scope") == TRANSPORT_SCOPE, "qualification_transport_scope")
    require(report["deadline_utc"] == deadline_utc, "qualification_transport_original_deadline")
    require(type(report.get("routes")) is list and [row.get("route") for row in report["routes"]] == ["python", "console", "stdio"], "qualification_transport_inventory")
    require(len({(row["workspace"],row["services"],row["run_id"]) for row in report["routes"]}) == 1,
        "qualification_replay_business_binding")
    requests = transport_requests(report["routes"][0]["run_id"])
    installation = report["installation"]
    require(installation["wheel"] == archives["wheel"], "qualification_transport_build_artifact_skew")
    wheel = Path(installation["wheel"]["path"])
    require(hash_file(wheel) == {name: installation["wheel"][name] for name in ("sha256", "bytes")}, "qualification_installed_archive_changed")
    with zipfile.ZipFile(wheel) as archive:
        package_files = {name.removeprefix("evidence_wiki/"):hashlib.sha256(archive.read(name)).hexdigest()
            for name in archive.namelist() if name.startswith("evidence_wiki/") and not name.endswith("/")}
    require(package_files and package_files.get("__init__.py") == plan["candidate"]["src/evidence_wiki/__init__.py"]["sha256"], "qualification_archive_candidate_skew")
    interpreter, console = installation["interpreter"], installation["console"]
    require(Path(interpreter).is_absolute() and Path(console).is_absolute()
            and Path(console).parent == Path(interpreter).parent and Path(console).name == "evidence-wiki", "qualification_installed_console_selection")
    console_raw = Path(console).read_bytes()
    require(console_raw.startswith(("#!"+interpreter+"\n").encode())
            and console_raw.split(b"\n",1)[1] == plan["entrypoint_template"].encode()
            and hash_file(console) == installation["console_file"], "qualification_installed_console_identity")
    native = []
    originals, reference_attempts, native_bindings, journals = [], [], [], {}
    last_command_finished = None
    for route_index,row in enumerate(report["routes"]):
        observed, actual_requests = [], []
        require(type(row.get("commands")) is list and row["commands"], "qualification_transport_commands")
        for command in row["commands"]:
            cost_plan = validate_cost_plan(command["process_plan"])
            checked = read_command_receipt(cost_plan, command["attempt_id"], command["argv"], observer_key=key,
                directory=command["directory"], work_root=command["work_root"], expected_receipt_sha256=command["receipt_sha256"])
            intent = _authenticated(Path(command["directory"]) / (command["attempt_id"] + ".intent.json"), key)
            expected_exit = command.get("expected_exit", 0)
            require(expected_exit in {0, 3} and command_terminal(checked, intent=intent,
                deadline_utc=report["deadline_utc"], expected_exit=expected_exit) == "accepted", "qualification_transport_process")
            last_command_finished = _transport_command_order(checked["receipt"],last_command_finished)
            raw = read_private(Path(command["directory"]) / (command["attempt_id"] + ".stdout"), 4_194_304)
            require(checked["receipt"]["streams"]["stderr"]["retained_bytes"] == 0, "qualification_protocol_stderr")
            output = [json.loads(line) for line in raw.splitlines()]
            if row["route"] == "stdio":
                stdio_transport_command(command["argv"],row,console)
                require(output and all(type(item) is dict and item.get("jsonrpc") == "2.0" and "method" not in item for item in output), "qualification_stdio_protocol")
                require(any(item.get("result",{}).get("protocolVersion") == "2024-11-05"
                    and set(item["result"].get("capabilities",{})) == {"tools","resources"} for item in output), "qualification_stdio_initialization_missing")
                machine_input = read_private(Path(command["input_path"]),4_194_304)
                require(intent["input_sha256"] == hashlib.sha256(machine_input).hexdigest(), "qualification_stdio_original_input_changed")
                actual_requests.append(stdio_request(machine_input,row["workspace"]))
                observed += [json.loads(item["result"]["content"][0]["text"]) for item in output if "content" in item.get("result", {})]
            else:
                require(len(output) == 1, "qualification_console_protocol")
                if row["route"] == "console":
                    require(command["argv"][:2] == [console,"agent"] and "--services" in command["argv"]
                            and output[0].get("exit_code") == expected_exit, "qualification_real_console_required")
                    actual_requests.append(console_request(command["argv"],row["workspace"],row["services"]))
                else:
                    python_transport_command(command,row,installation,plan,report["deadline_utc"],intent)
                    actual_requests += output[0]["requests"]
                observed += output[0]["responses"] if row["route"] == "python" else [output[0]]
        require(observed == row["responses"], "qualification_transport_original_output")
        require(actual_requests == requests, "qualification_transport_exact_request_replay")
        opening = transport_responses(observed,row["run_id"],route=row["route"],
            first_route=route_index == 0,workspace=row["workspace"])
        require(any(item.get("operation") == "run-cycle" and item.get("status") == "returned"
            and item["result"].get("terminal") is True and item["result"].get("outcome") == "accepted" for item in observed), "qualification_native_cycle_incomplete")
        brief = next(item["result"] for item in observed if item.get("operation") == "brief")
        require(brief.get("verdict") in {"accept","partial"}, "qualification_current_brief_unusable")
        cycle = next(item["result"] for item in observed if item.get("operation") == "run-cycle")
        require(cycle.get("run_id") == row["run_id"] and cycle.get("request_id") == row["run_id"]+"-drive",
            "qualification_cycle_request_binding")
        refreshed = next(item["result"] for item in observed if item.get("operation") == "refresh")
        require(refreshed.get("previous_run_id") == cycle.get("run_id")
                and refreshed.get("run_id") == row["run_id"]+"-refresh"
                and refreshed.get("request_id") == row["run_id"]+"-refresh-drive"
                and refreshed.get("cycle",{}).get("run_id") == refreshed["run_id"]
                and refreshed.get("cycle",{}).get("terminal") is True,
                "qualification_refresh_chain")
        replay = row["native_replay"]
        require(replay["attempt_id"] == row["route"]+"-native", "qualification_native_producer_route_identity")
        expected_argv = [interpreter,"-I","-B",str(Path(plan["repo_root"])/"tools/replay_qualification_native.py"),
            "--workspace",row["workspace"],"--services",row["services"],"--run-id",row["run_id"],
            "--additional-run-id",refreshed["cycle"]["run_id"],
            "--artifact-dir="+str(Path(ref["path"]).parent/"native-artifacts"/row["route"])]
        require(replay["argv"] == expected_argv, "qualification_native_replay_owner")
        checked = read_command_receipt(replay["process_plan"], replay["attempt_id"], expected_argv, observer_key=key,
            directory=replay["directory"],work_root=replay["work_root"],expected_receipt_sha256=replay["receipt_sha256"])
        intent = _authenticated(Path(replay["directory"])/(replay["attempt_id"]+".intent.json"),key)
        require(command_terminal(checked,intent=intent,deadline_utc=report["deadline_utc"]) == "accepted", "qualification_native_replay_unavailable")
        from tools.replay_qualification_native import read_artifact
        summary = json.loads(read_private(Path(replay["directory"])/(replay["attempt_id"]+".stdout"),32768))
        result = read_artifact(summary,Path(ref["path"]).parent/"native-artifacts"/row["route"])
        native_read_epoch(result,checked['receipt'])
        require(result["schema_version"] == "qualification-native-replay/v1" and result["profile"]["schema_version"] == "research-execution-profile/v13"
                and result["api_version"] == "63" and result["package_files"] == package_files
                and result["package_origin"] == installation["package_root"] and result["interpreter"] == interpreter
                and result["prefix"] == installation["prefix"]
                and result["workspace"] == row["workspace"] and result["run_id"] == row["run_id"], "qualification_current_native_producer_binding")
        if opening is not None:
            _opening_native_binding(opening,result)
        for path, expected in result["host_inputs"].items():
            require(hash_file(Path(path))["sha256"] == expected, "qualification_native_host_input_drift")
        require(hash_file(row["services"])["sha256"] == result["factory_sha256"], "qualification_native_factory_drift")
        for snapshot in [result['journal'],*(item['journal'] for item in result['additional_runs'])]:
            native_journal_records(snapshot,row['workspace'])
            identity = (row['workspace'],snapshot['run_id'])
            if identity in journals:
                probe_only_extension(journals[identity],snapshot,row['workspace'])
            journals[identity] = snapshot
        native.extend({"route":row["route"],"workspace":row["workspace"],
            **{name:item[name] for name in ("attempt_id","run_id","config_ref","state","final_accounting")},
            "receipt_sha256":digest(item),"original_producer_receipt_sha256":replay["receipt_sha256"]}
            for item in result["native_receipts"])
        originals.extend({**item,"route":row["route"],"workspace":row["workspace"],"run_id":row["run_id"],
            "original_producer_receipt_sha256":replay["receipt_sha256"]}
            for item in result["current"]["accounting"]["original_outcomes"])
        require([item["run_id"] for item in result["additional_runs"]] == [refreshed["cycle"]["run_id"]], "qualification_refresh_run_inventory")
        for additional in result["additional_runs"]:
            native.extend({"route":row["route"],"workspace":row["workspace"],
                **{name:item[name] for name in ("attempt_id","run_id","config_ref","state","final_accounting")},
                "receipt_sha256":digest(item),"original_producer_receipt_sha256":replay["receipt_sha256"]} for item in additional["native_receipts"])
            originals.extend({**item,"route":row["route"],"workspace":row["workspace"],"run_id":additional["run_id"],
                "original_producer_receipt_sha256":replay["receipt_sha256"]}
                for item in additional["current"]["accounting"]["original_outcomes"])
        references = result["reference_attempts"]
        require(references and len({item["attempt_id"] for item in references}) == len(references)
                and all(item["started_utc"] is not None and item["final_accounting"] is True for item in references), "qualification_reference_attempts_incomplete")
        reference_attempts.append({"route":row["route"],"workspace":row["workspace"],"inventory_sha256":digest(references),
            "started_attempts":len(references),"terminal_attempts":len(references),
            "state_counts":{state:sum(item["state"] == state for item in references) for state in sorted({item["state"] for item in references})},
            "original_producer_receipt_sha256":replay["receipt_sha256"],"original_reference_inventory":{
                path:sha for path,sha in result["host_inputs"].items() if Path(path).name == "reference-observations.jsonl"}})
        native_bindings.append({"route":row["route"],"workspace":row["workspace"],"run_id":row["run_id"],
            "policy":result["policy"],"profile_sha256":result["profile"]["profile_sha256"],
            "calibration":result["profile"].get("calibration"),"host_qualification":result["qualification"],
            "source_bindings":result["current"].get("execution",{}).get("source_bindings"),
            "complete_original_artifact":summary,
            "journals":[{key:snapshot[key] for key in ('run_id','source_path','source_sha256','state_path','state_sha256')}
                for snapshot in [result['journal'],*(item['journal'] for item in result['additional_runs'])]],
            "current_read_epoch":{key:result[key] for key in ('read_started_utc','read_finished_utc')},
            "original_producer_receipt_sha256":replay["receipt_sha256"]})
    for snapshot in journals.values():
        require(hash_file(snapshot['source_path'])['sha256'] == snapshot['source_sha256']
            and hash_file(snapshot['state_path'])['sha256'] == snapshot['state_sha256'], "qualification_native_latest_journal_drift")
    require(len({item["original_producer_receipt_sha256"] for item in native_bindings}) == 3,
        "qualification_native_producer_observations_missing")
    return {"passed": True, "routes": [row["route"] for row in report["routes"]],
            "native_attempts":replay_inventory(native,("workspace","run_id","attempt_id","config_ref")),
            "original_questions":replay_inventory(originals,("workspace","run_id","original_id")),"reference_attempts":reference_attempts,
            "execution_scope":dict(TRANSPORT_SCOPE),
            "native_bindings":native_bindings,"profile_version": report["profile_version"]}


def _canonical_verification_argv(argv, root, directory, output):
    try:
        prefix = [str(root/".venv/bin/python")]
        if argv[1:2] == ["-B"]:
            prefix.append("-B")
        prefix.extend([str(root/"tools/qualify_unattended.py"),"verify"])
        return (argv[:len(prefix)] == prefix and "--directory" in argv
            and argv[argv.index("--directory")+1] == str(directory) and "--output="+output in argv)
    except (IndexError,TypeError,ValueError):
        return False


def checked_verification(value, job, plan, execution, preliminary, key, parent_receipt):
    """Join source-owned replay and positively executed independent controls."""
    require(preliminary is not None, "qualification_independent_preliminary_missing")
    ready_for_verification(preliminary)
    base = {"schema_version":"qualification-verification/v1","plan_sha256":plan["plan_sha256"],
        "preliminary_sha256":digest(preliminary),"decision":preliminary["decision"]}
    require(all(value.get(name) == item for name,item in base.items()), "qualification_independent_replay_binding")
    root, directory = Path(plan["repo_root"]),Path(execution["directory"]).parent
    require(set(value) == set(base)|{"canonical_verify","independent_audit"}, "qualification_verification_report_shape")
    canonical = value["canonical_verify"]
    require(_canonical_verification_argv(canonical["argv"],root,directory,canonical["output_path"]),
        "qualification_registered_nested_replay_owner")
    checked = read_command_receipt(canonical["process_plan"],canonical["attempt_id"],canonical["argv"],observer_key=key,
        directory=canonical["directory"],work_root=canonical["work_root"],expected_receipt_sha256=canonical["receipt_sha256"])
    intent = _authenticated(Path(canonical["directory"])/(canonical["attempt_id"]+".intent.json"),key)
    require(command_terminal(checked,intent=intent,deadline_utc=execution["deadline_utc"]) == "accepted"
        and intent["input_sha256"] == hashlib.sha256(b"").hexdigest()
        and _clock(parent_receipt["started_at"]) <= _clock(checked["receipt"]["started_at"])
        <= _clock(checked["receipt"]["finished_at"]) <= _clock(parent_receipt["finished_at"]),
        "qualification_nested_replay_receipt_unqualified")
    require(_original(canonical["output_path"],canonical["output_sha256"]) == base
        and json.loads(read_private(Path(canonical["directory"])/(canonical["attempt_id"]+".stdout"),1_048_576)) == base,
        "qualification_nested_replay_original_output")
    independent = value["independent_audit"]
    require(job["argv"][:2] == [str(root/".venv/bin/python"),"-B"] and Path(job["argv"][2]).suffix == ".py"
        and "--adapter" in job["argv"] and job["argv"][job["argv"].index("--adapter")+1] == independent["adapter_path"],
        "qualification_source_pinned_audit_wrapper_required")
    for path,expected in ((job["argv"][2],hash_file(job["argv"][2])["sha256"]),
                           (independent["adapter_path"],independent["adapter_sha256"])):
        require(any(row["path"] == str(Path(path).resolve()) and row["sha256"] == expected for row in job["command_manifest"]),
            "qualification_audit_source_not_pinned")
    adapter = Path(independent["adapter_path"])
    expectations, expected_hash = _new_audit_expectations(adapter, job, independent)
    audit = _original(independent["path"],independent["sha256"])
    if audit.get("schema_version") == "independent-qualification-audit/v3":
        originals = _audit_original_joins(adapter, independent, plan, execution, preliminary, key)
        parent_intent = _authenticated(Path(execution["directory"]) / "verification.intent.json", key)
        validated = _checked_audit_v3(audit, independent, expectations, expected_hash, plan, preliminary,
            original_joins=originals, execution=execution, parent_receipt=parent_receipt,
            parent_intent=parent_intent)
        return {"passed": True, "scope": "independent_preliminary_predicates_and_six_actual_controls",
            "audit_schema": "independent-qualification-audit/v3", "audit_sha256": independent["sha256"],
            "original_expectations_sha256": expected_hash, "canonical_receipt_sha256": canonical["receipt_sha256"],
            "audit_reference": {"path": independent["path"], "sha256": independent["sha256"]}, **validated}
    require(hash_file(adapter.with_name("frozen-expectations.json"))["sha256"] == expected_hash,
        "qualification_legacy_expectations_filename")
    require(expectations.get("check_count") == 36 and len(expectations.get("checks",[])) == 36
        and len({row["id"] for row in expectations["checks"]}) == 36
        and all(row.get("required") is True for row in expectations["checks"]), "qualification_original_check_inventory")
    inventory = ["original_replay","candidate","receipt_bytes","policy_dataset","role","attempt_inventory","original_deadline"]
    scope = "complete_first_four_only_missing_verification"
    require(independent["check_inventory"] == inventory and independent["baseline_scope"] == scope
        and independent["original_preliminary_report_sha256"] == digest(preliminary)
        and audit.get("schema_version") == "independent-qualification-audit/v2"
        and audit.get("original_expectations_sha256") == expected_hash
        and audit.get("original_report_sha256") == digest(preliminary)
        and audit.get("original_report_decision") == preliminary["decision"]
        and audit.get("baseline_first_four_complete") is True and audit.get("baseline_scope") == scope
        and audit.get("all_rejections_passed") is True and [row.get("id") for row in audit.get("results",[])] == inventory
        and all(row.get("pass") is True for row in audit["results"])
        and all(row.get("additional_rejection") is True for row in audit["results"][1:]),
        "qualification_positive_independent_controls_missing")
    return {"passed":True,"scope":"independent_original_replay_and_six_mutation_controls",
        "audit_sha256":independent["sha256"],"original_expectations_sha256":expected_hash,"check_inventory":inventory,
        "canonical_receipt_sha256":canonical["receipt_sha256"]}


def reconstruct(plan, execution, observations, *, observer_key, preliminary=None, as_of_utc=None):
    """Re-read original owners and retain every missing or stopped obligation."""
    gaps, failures, tasks, cases, cost_events, started_cases, native_attempts, reference_attempts, originals = [], [], [], [], [], [], [], [], []
    artifacts, observed_catalogs, native_inventory_observed = {}, set(), False
    native_bindings = []
    try:
        validate_plan(plan)
    except (ValueError, OSError, KeyError, TypeError):
        failures.append("candidate_or_plan_drift")
    require(execution["plan_sha256"] == plan["plan_sha256"], "qualification_execution_binding")
    original_execution = _authenticated(Path(execution["directory"]).parent / "execution.json", observer_key)
    require(original_execution == execution, "qualification_execution_original_changed")
    started_utc, deadline_utc = _clock(execution["started_utc"]), _clock(execution["deadline_utc"])
    require((deadline_utc-started_utc).total_seconds() == plan["timeout_seconds"], "qualification_original_budget_changed")
    cost_plan = validate_cost_plan(execution["cost_plan"])
    expected_cost_plan = freeze_cost_plan(scope_id="unattended-qualification", binding={"plan_sha256": plan["plan_sha256"]},
        sources=[{"source_id": "host", "provenance": "host_observed", "kind": "automated",
            "origin": "original qualification host job intervals; provider work remains unknown",
            "collection_started_at": execution["started_utc"], "collection_ended_at": execution["deadline_utc"],
            "coverage": "complete", "authentication_ref": None}],
        attempts=[{"attempt_id": role.lower(), "phase": "qualification", "allocation": "repeated",
            "source_id": "host", "required_metrics": ["elapsed_seconds"]} for role in ROLES])
    require(cost_plan == expected_cost_plan, "qualification_cost_binding")
    if as_of_utc is not None:
        snapshot = _authenticated(Path(execution["directory"]).parent/"preliminary.json",observer_key)
        require(snapshot.get("preliminary") is True and snapshot["finished_utc"] == as_of_utc,
            "qualification_preliminary_snapshot_clock")
    seen = set()
    for observation in observations:
        require(observation["role"] in ROLES and observation["role"] not in seen, "qualification_duplicate_job")
        seen.add(observation["role"])
    for job in plan["jobs"]:
        role = job["role"]
        row = next((item for item in observations if item["role"] == role), None)
        task = {"role": role, "started": False, "terminal_observed": False, "outcome": "externally_blocked",
                "receipt_sha256": None, "reports": []}
        tasks.append(task)
        start_path = Path(execution["directory"]) / (role.lower() + ".start.json")
        later_verification = False
        if start_path.exists():
            try:
                start = _authenticated(start_path, observer_key)
                intent = _authenticated(Path(execution["directory"]) / (role.lower() + ".intent.json"), observer_key)
                require(start.get("intent_sha256") == digest(intent) and intent["attempt"]["attempt_id"] == role.lower(), "qualification_actual_start_binding")
                later_verification = (role == "Verification" and as_of_utc is not None
                    and _clock(intent["reserved_at"]) > _clock(as_of_utc) and _clock(start["started_at"]) > _clock(as_of_utc))
                task["started"] = not later_verification and type(start.get("pid")) is int and start["pid"] > 0
            except (ValueError, OSError, TypeError, KeyError):
                gaps.append("start_authentication_unknown:" + role)
        if later_verification:
            require(row is None, "qualification_preliminary_contains_later_verification")
        if row is None:
            gaps.append("required_job_missing:" + role)
            continue
        try:
            require(row["plan_sha256"] == plan["plan_sha256"] and row["argv"] == job["argv"], "qualification_job_binding")
            checked = read_command_receipt(cost_plan, role.lower(), row["argv"], observer_key=observer_key,
                directory=execution["directory"], work_root=execution["work_root"], expected_receipt_sha256=row["receipt_sha256"])
            intent = _authenticated(Path(execution["directory"]) / (role.lower() + ".intent.json"), observer_key)
            require(intent["command_manifest"] == job["command_manifest"], "qualification_command_changed")
            require(float(intent["timeout_seconds"]) <= job["timeout_seconds"]
                    and intent["input_sha256"] == hashlib.sha256(b"").hexdigest(), "qualification_job_cap_or_stdin_changed")
            task.update(started=checked["receipt"]["started_at"] is not None, receipt_sha256=row["receipt_sha256"],
                        outcome=command_terminal(checked, intent=intent, deadline_utc=execution["deadline_utc"]),
                        terminal_observed=checked["receipt"]["finished_at"] is not None
                            and checked["receipt"]["exit_code"] is not None
                            and checked["receipt"]["output_reader_work_unresolved"] is False)
            if checked["event"] is not None:
                cost_events.append(checked["event"])
            _role_provenance(role,row,job,plan,execution,observer_key)
            if any(item.get("independent_requirements") for item in plan["jobs"]):
                _raw_role_provenance(role, row, job, plan, execution, observer_key)
            refs = row["reports"]
            require([{k: ref[k] for k in ("kind", "path")} for ref in refs] == job["reports"], "qualification_report_inventory")
            for ref in refs:
                if ref["kind"] in {"attack", "automatic"}:
                    value = _acceptance(ref, plan, observer_key, ref["kind"])
                    observed_catalogs.add(ref["kind"])
                    started_cases.extend({"catalog":ref["kind"],**item} for item in value["started_nodes"])
                    if ref["kind"] == "automatic":
                        cases = value["grade"]["cases"]
                elif ref["kind"] == "archives":
                    value = _archives(ref, plan)
                    artifacts = value["artifacts"]
                elif ref["kind"] == "transports":
                    value = read_transports(ref, plan, observer_key, deadline_utc=execution["deadline_utc"],archives=artifacts)
                    native_inventory_observed = True
                    native_attempts.extend(value["native_attempts"])
                    reference_attempts.extend(value["reference_attempts"])
                    originals.extend(value["original_questions"])
                    native_bindings.extend(value["native_bindings"])
                elif ref["kind"] == "costs":
                    value = _original(ref["path"], ref["sha256"])
                    expected = project_cost_report(cost_plan, cost_events[:-1])
                    require(value == expected, "qualification_cost_original_reduction")
                    require(value["coverage"]["missing_attempt_ids"] == ["measurement", "verification"], "qualification_cost_started_inventory")
                    value = {"passed": not any(row["attempt_id"] not in {"measurement", "verification"}
                        for row in value["coverage"]["missing_metrics"]), "report": value}
                else:
                    value = _original(ref["path"], ref["sha256"])
                    value = checked_verification(value,job,plan,execution,preliminary,observer_key,checked["receipt"])
                task["reports"].append({"kind": ref["kind"], "result": value})
                if value["passed"] is not True:
                    task["outcome"] = "failed"
            if task["outcome"] != "accepted":
                failures.append("required_job_not_passed:" + role)
        except (ValueError, OSError, KeyError, TypeError, UnicodeError, SystemExit) as error:
            if task["started"] and task["outcome"] == "accepted":
                task["outcome"] = "failed"
            gaps.append("required_evidence_unavailable:" + role + ":" + type(error).__name__)
    complete = not gaps and not failures and all(row["outcome"] == "accepted" for row in tasks)
    final_audit = None
    try:
        final_audit, final_gaps = _final_audit_closure(plan, execution, observations, tasks, started_cases,
            cost_plan, cost_events, key=observer_key, preliminary=preliminary, complete=complete)
        gaps.extend(final_gaps)
        complete = complete and not final_gaps
    except (ValueError, OSError, KeyError, TypeError) as error:
        gaps.append("qualification_final_audit_unavailable:" + type(error).__name__)
        complete = False
    return {"schema_version": "unattended-qualification-result/v1", "plan_sha256": plan["plan_sha256"],
            "candidate_sha256": digest(plan["candidate"]), "decision": "qualified" if complete else "not_qualified",
            "failures": failures, "gaps": gaps, "tasks": tasks, "cases": cases,
            "started_case_nodes":started_cases,"native_attempts":native_attempts,"reference_attempts":reference_attempts,
            "original_questions":originals,
            "native_bindings":native_bindings,"artifacts":artifacts,
            "denominators": {"required_tasks": len(ROLES), "started_tasks": sum(row["started"] for row in tasks),
                "terminal_tasks": sum(row["started"] and row["terminal_observed"] for row in tasks),
                "started_stop_unknown_tasks": sum(row["started"] and not row["terminal_observed"] for row in tasks), "task_outcomes": {
                    state: sum(row["started"] and row["outcome"] == state for row in tasks) for state in OUTCOMES},
                "started_case_nodes":len(started_cases) if observed_catalogs == {"attack","automatic"} else None,
                "known_started_case_nodes":len(started_cases),
                "terminal_case_nodes":sum(row["terminal_observed"] for row in started_cases) if observed_catalogs == {"attack","automatic"} else None,
                "case_outcomes":{state:sum(row["outcome"] == state for row in started_cases) for state in OUTCOMES},
                "native_attempts":len(native_attempts) if native_inventory_observed else None,"reference_attempt_groups":len(reference_attempts) if native_inventory_observed else None,
                "reference_attempts":sum(item["started_attempts"] for index,item in enumerate(reference_attempts)
                    if not any(item["original_reference_inventory"] == prior["original_reference_inventory"] for prior in reference_attempts[:index])) if native_inventory_observed else None,
                "original_questions":len(originals) if native_inventory_observed else None},
            "costs": project_cost_report(cost_plan, cost_events), "context": plan["context"],
            "final_independent_obligations": final_audit,
            "context_classification":"declared_configuration; actual native bindings are separately owner-observed",
            "assignment_scope": "trusted_host_copied_tool_transcripts", "physical_isolation": "unknown",
            "observation_class": "fixture_mechanics", "authorized": False, "evidence_accepted": False, "limitations": LIMITATIONS}
