"""Exact frozen evidence, original tool chains and final owner obligations."""

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from tools import _terminal_qualification as owner
from tools import qualify_unattended as route
from tools._acceptance_protocol import digest


def checked_receipt():
    instant = datetime(2026, 1, 1, tzinfo=timezone.utc)
    receipt = {"status": "succeeded", "started_at": (instant + timedelta(seconds=1)).isoformat(),
        "finished_at": (instant + timedelta(seconds=2)).isoformat(), "exit_code": 0, "failure": None,
        "output_overflow": False, "output_reader_work_unresolved": False, "meter_work_unresolved": False,
        "command_manifest_after": []}
    checked = {"receipt": receipt, "current_command_manifest_matches": True, "coverage_gap": None}
    intent = {"reserved_at": instant.isoformat(), "timeout_seconds": "2", "command_manifest": []}
    return checked, intent, (instant + timedelta(seconds=20)).isoformat()


@pytest.mark.parametrize("elapsed,expected", [(2, "accepted"), (2.001, "budget_stopped"), (12, "budget_stopped")])
def test_exit_zero_does_not_expand_reservation_budget(elapsed, expected):
    checked, intent, deadline = checked_receipt()
    instant = datetime.fromisoformat(intent["reserved_at"])
    checked["receipt"]["finished_at"] = (instant + timedelta(seconds=elapsed)).isoformat()
    assert owner.command_terminal(checked, intent=intent, deadline_utc=deadline) == expected


def outer(request, result, tool="exec_command"):
    return {"tool": "functions.exec", "request": "const request = " + json.dumps(request) +
        ";\nconst result = await tools." + tool + "(request);\ntext(JSON.stringify({request, result}));",
        "returned_result": json.dumps({"request": request, "result": result})}


@pytest.mark.parametrize("mutation", ["positive", "request", "result", "method", "extra"])
def test_outer_literal_and_actual_result_must_match_nested_objects(mutation):
    request = {"cmd": "fixed-command", "workdir": "/fixed-root"}
    result = {"output": "{}\n", "exit_code": 0}
    nested = {"request": request, "returned_result": result}
    original = outer(request, result)
    if mutation == "request": nested["request"] = {**request, "cmd": "unrelated"}
    if mutation == "result": nested["returned_result"] = {**result, "output": "grafted"}
    if mutation == "method": original["request"] = original["request"].replace("exec_command", "write_stdin")
    if mutation == "extra": original["request"] += "\ntext('extra');"
    assert owner._outer_tool_matches(original, nested, "exec_command") is (mutation == "positive")


def audit_fixture():
    ids = ["requirement-" + str(index) for index in range(1, 37)]
    budgets = {"Build": 180, "Attack": 4200, "Integration": 1800, "Measurement": 60, "Verification": 240}
    freeze = {"requirements_count": 36, "requirements": [{"id": name} for name in ids],
        "immutable_inventory": {"bounds": {"enclosing_seconds": 7200, "local_role_seconds": budgets},
            "mandatory_negative_controls": [{"id": name} for name in owner.INDEPENDENT_CONTROLS]}}
    plan = {"plan_sha256": "plan", "timeout_seconds": 7200,
        "jobs": [{"role": role, "timeout_seconds": seconds} for role, seconds in budgets.items()]}
    preliminary = {"decision": "not_qualified"}
    scope = "complete_first_four_only_missing_verification"
    independent = {"requirement_inventory": ids, "control_inventory": list(owner.INDEPENDENT_CONTROLS),
        "baseline_scope": scope, "original_preliminary_report_sha256": digest(preliminary)}
    reasons = ["qualification_transport_binding", "qualification_real_console_required",
        "qualification_five_step_journey", "qualification_actual_role_tool_receipt_required",
        "required_original_final_receipt_missing", "unconfirmed_original_cleanup_substitution"]
    controls = []
    for index, (name, reason) in enumerate(zip(owner.INDEPENDENT_CONTROLS, reasons, strict=True)):
        target_role = "Integration" if index < 3 else "Build"
        controls.append({"id": name, "executed": True, "pass": True, "additional_rejection": True,
            "targeted_refusals": [reason], "additional_errors": ["required_evidence_unavailable:Build"], "errors": [],
            "started_utc": f"2026-01-01T00:00:{2 * index + 1:02d}+00:00",
            "finished_utc": f"2026-01-01T00:00:{2 * index + 2:02d}+00:00",
            "tasks": [{"role": target_role, "started": True, "terminal_observed": False, "outcome": "failed"}],
            "started_tasks": 4, "started_stop_unknown_tasks": 1,
            "substitution": {"control": name, "target_path": "/original", "substitution_class": "authenticated_view",
                "original_transport_sha256": "a" * 64, "original_view_sha256": "b" * 64,
                "mutated_view_sha256": None if name == "missing-final" else "c" * 64,
                "changed_scope": [{"original": False, "substitute": True}]}})
    results = [{"id": name, "status": "pass_with_final_obligations" if index in owner.FINAL_AUDIT_OBLIGATIONS else "pass",
        "evidence": [{"original_artifact_sha256": "d" * 64}], "errors": [],
        "final_obligations": owner.FINAL_AUDIT_OBLIGATIONS.get(index, [])}
        for index, name in enumerate(ids, 1)]
    audit = {"schema_version": "independent-qualification-audit/v3", "original_expectations_sha256": "e" * 64,
        "original_report_sha256": digest(preliminary), "original_report_decision": "not_qualified", "plan_sha256": "plan",
        "baseline_scope": scope, "baseline_first_four_complete": True, "predicate_results": results,
        "controls": controls, "controls_executed": 6, "controls_passed": 6, "all_rejections_passed": True}
    results[33]["evidence"] = [{"actual_controls": deepcopy(controls),
        "original_deadline_utc": "2026-01-01T00:04:00+00:00"}]
    return audit, independent, freeze, plan, preliminary


def original_join_fixture(audit):
    """Represent separately owner-computed originals, never report-derived inputs."""
    return {"original_joins": {"predicate_results": deepcopy(audit["predicate_results"]),
                              "controls": deepcopy(audit["controls"])},
        "execution": {"started_utc": "2026-01-01T00:00:00+00:00",
                      "deadline_utc": "2026-01-01T02:00:00+00:00"},
        "parent_intent": {"reserved_at": "2026-01-01T00:00:00+00:00"},
        "parent_receipt": {"finished_at": "2026-01-01T00:00:20+00:00"}}


@pytest.mark.parametrize("mutation", ["positive", "duplicate", "missing", "false", "evidence", "control", "reason", "errors", "pending", "budget"])
def test_new_audit_requires_exact_inventory_evidence_and_actual_refusals(mutation):
    audit, independent, freeze, plan, preliminary = deepcopy(audit_fixture())
    originals = original_join_fixture(audit)
    if mutation == "duplicate": audit["predicate_results"][-1]["id"] = audit["predicate_results"][0]["id"]
    if mutation == "missing": audit["predicate_results"].pop()
    if mutation == "false": audit["predicate_results"][0]["status"] = "refused"
    if mutation == "evidence": audit["predicate_results"][0]["evidence"] = []
    if mutation == "control": audit["controls"][0]["executed"] = False
    if mutation == "reason": audit["controls"][0]["targeted_refusals"] = ["unrelated-string"]
    if mutation == "errors": audit["controls"][0]["additional_errors"] = []
    if mutation == "pending": audit["predicate_results"][4]["final_obligations"] = []
    if mutation == "budget": plan["jobs"][0]["timeout_seconds"] += 1
    if mutation == "positive":
        result = owner._checked_audit_v3(audit, independent, freeze, "e" * 64, plan, preliminary, **originals)
        assert len(result["check_inventory"]) == 36 and len(result["final_obligations"]) == 11
    else:
        with pytest.raises(ValueError):
            owner._checked_audit_v3(audit, independent, freeze, "e" * 64, plan, preliminary, **originals)


@pytest.mark.parametrize("mutation", ["empty-evidence", "unrelated-evidence", "path", "scope", "digest",
    "future", "reverse", "accepted", "unrelated-error", "missing-originals"])
def test_audit_metadata_must_join_separately_recomputed_originals(mutation):
    audit, independent, freeze, plan, preliminary = audit_fixture()
    originals = original_join_fixture(audit)
    control = audit["controls"][0]
    if mutation == "empty-evidence": audit["predicate_results"][0]["evidence"] = [{}]
    if mutation == "unrelated-evidence": audit["predicate_results"][0]["evidence"] = [{"unrelated": True}]
    if mutation == "path": control["substitution"]["target_path"] = "/unrelated"
    if mutation == "scope": control["substitution"]["changed_scope"] = [{}]
    if mutation == "digest": control["substitution"]["original_transport_sha256"] = "f" * 64
    if mutation == "future": control.update(started_utc="2099-01-01T00:00:00Z", finished_utc="2099-01-01T00:00:01Z")
    if mutation == "reverse": audit["controls"][1].update(started_utc="2025-01-01T00:00:00Z", finished_utc="2025-01-01T00:00:01Z")
    if mutation == "accepted": control["tasks"][0]["outcome"] = "accepted"
    if mutation == "unrelated-error": control["additional_errors"] = ["unrelated"]
    if mutation == "missing-originals": originals.pop("original_joins")
    with pytest.raises(ValueError):
        owner._checked_audit_v3(audit, independent, freeze, "e" * 64, plan, preliminary, **originals)


def test_final_owner_does_not_qualify_pending_self_terminal_obligations():
    tasks = [{"reports": [{"result": {"audit_schema": "independent-qualification-audit/v3"}}]}]
    with pytest.raises(ValueError, match="required_work_incomplete"):
        owner._final_audit_closure({}, {}, [], tasks, [], {}, [], key=b"x" * 32, preliminary=None, complete=False)


@pytest.mark.parametrize("mutation", ["positive", "missing", "target", "identity", "future-spawn", "command"])
def test_original_execution_go_must_join_spawn_and_actual_command(monkeypatch, mutation):
    assignment = {"role": "Build", "agent_id": "/root/build", "task_path": "/root/build",
        "transcript_path": "/spawn.json", "transcript_sha256": "a" * 64}
    spawn = {"tool": "collaboration.spawn_agent", "request": {"task_name": "build", "message": "Prepare only"},
        "returned_result": {"task_name": "/root/build"}}
    go = {"tool": "collaboration.followup_task", "request": {"target": "/root/build", "message": "Run fixed-command"},
        "returned_result": {}}
    request, result = {"cmd": "fixed-command", "workdir": "/repo"}, {"output": "{}\n", "exit_code": 0}
    call = {"tool": "exec_command", "command": "fixed-command", "cwd": "/repo", "request": request,
        "returned_result": result, "polls": [], "output": "{}\n", "outer_call_index": 0}
    envelope = outer(request, result)
    envelope["observed_nested_call_indices"] = [0]
    raw = {"schema_version": "qualification-raw-role-tool-transcript/v1", "role": "Build",
        "assignment_ref": {"path": "/spawn.json", "sha256": "a" * 64},
        "execution_assignment_ref": {"path": "/go.json", "sha256": "b" * 64},
        "tool_calls": [call], "outer_calls": [envelope]}
    go_calls = [spawn, go]
    if mutation == "missing": raw.pop("execution_assignment_ref")
    if mutation == "target": go["request"]["target"] = "/root/other"
    if mutation == "identity": go["returned_result"]["agent_name"] = "/root/other"
    if mutation == "future-spawn": go_calls = [go, spawn]
    if mutation == "command": go["request"]["message"] = "Run unrelated-command"
    originals = {"/spawn.json": {"tool_calls": [spawn]}, "/go.json": {"tool_calls": go_calls}, "/raw.json": raw}
    monkeypatch.setattr(owner, "_original", lambda path, expected: originals[str(path)])
    monkeypatch.setattr(owner, "_role_provenance", lambda *args: {"raw_execution_ref":
        {"path": "/raw.json", "sha256": "c" * 64}, "tool_calls": [call]})
    monkeypatch.setattr(owner, "_actual_role_call", lambda *args: True)
    args = ("Build", {}, {}, {"assignments": [assignment], "repo_root": "/repo"}, {}, b"x" * 32)
    if mutation == "positive":
        result = owner._raw_role_provenance(*args)
        assert result["path"] == "/raw.json" and result["sha256"] == "c" * 64 and result["outer_calls"] == 1
    else:
        with pytest.raises(ValueError): owner._raw_role_provenance(*args)


def test_final_raw_read_crossing_common_deadline_refuses_obligations(monkeypatch, tmp_path):
    instant = datetime(2026, 1, 1, tzinfo=timezone.utc)
    current = [instant]

    class Clock:
        @staticmethod
        def now(zone):
            return current[0]

    Clock.fromisoformat = datetime.fromisoformat
    monkeypatch.setattr(owner, "datetime", Clock)
    monkeypatch.setattr(owner, "ready_for_verification", lambda report: True)
    monkeypatch.setattr(owner, "project_cost_report", lambda *args: {"coverage":
        {"missing_attempt_ids": [], "missing_metrics": []}})
    audit = {"audit_schema": "independent-qualification-audit/v3"}
    tasks = [{"role": role, "started": True, "terminal_observed": True, "outcome": "accepted",
              "reports": [{"result": audit}] if role == "Verification" else []} for role in owner.ROLES]
    observations = [{"role": role, "receipt_sha256": "a" * 64, "reports": []} for role in owner.ROLES]

    def raw(role, *args):
        if role == "Verification": current[0] = instant + timedelta(seconds=11)
        return {"path": "/original", "sha256": "b" * 64}

    monkeypatch.setattr(owner, "_raw_role_provenance", raw)
    execution = {"directory": str(tmp_path / "commands"), "deadline_utc": (instant + timedelta(seconds=10)).isoformat()}
    with pytest.raises(ValueError, match="final_audit_deadline"):
        owner._final_audit_closure({"jobs": [{"role": role} for role in owner.ROLES]}, execution,
            observations, tasks, [{"terminal_observed": True}] * 106, {}, [], key=b"x" * 32,
            preliminary={}, complete=True)


def test_final_publication_crossing_common_deadline_is_not_qualified(monkeypatch, tmp_path):
    from tests._usage_fixture import require_reference_storage
    require_reference_storage()
    instant = datetime(2026, 1, 1, tzinfo=timezone.utc)

    class Clock:
        @staticmethod
        def now(zone):
            return instant + timedelta(seconds=11)

    monkeypatch.setattr(route, "datetime", Clock)
    Clock.fromisoformat = datetime.fromisoformat
    monkeypatch.setattr(route, "checked_directory", lambda path: path)
    monkeypatch.setattr(route, "_json", lambda path: {})
    monkeypatch.setattr(route, "_execution", lambda *args: {"deadline_utc": (instant + timedelta(seconds=10)).isoformat()})
    monkeypatch.setattr(route, "_authenticated", lambda *args: {})
    monkeypatch.setattr(route, "_observations", lambda *args: [])
    monkeypatch.setattr(route, "reconstruct", lambda *args, **kwargs: {"decision": "qualified", "gaps": []})
    result = route.seal(tmp_path, observer_key=b"x" * 32)
    published = json.loads((tmp_path / "report.json").read_bytes())
    assert result["decision"] == published["decision"] == "not_qualified"
    assert published["gaps"] == ["qualification_final_publication_deadline"]
