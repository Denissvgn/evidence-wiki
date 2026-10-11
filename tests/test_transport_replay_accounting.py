"""Replay observations retain their routes without multiplying original work."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from tools._acceptance_protocol import hash_file
from tools._terminal_qualification import (
    _opening_native_binding,
    _transport_command_order,
    console_request,
    python_transport_command,
    replay_inventory,
    stdio_request,
    stdio_transport_command,
    transport_requests,
    transport_responses,
)
from tools.measure_integration_costs import _authenticated, _signed

ROUTES = ("python", "console", "stdio")


def observations():
    rows = []
    for route in ROUTES:
        for run in ("business", "business-refresh"):
            rows.append({"route": route, "workspace": "/owned/workspace", "run_id": run,
                "original_id": "same-original", "original_text": "The original question.",
                "accepted": run == "business", "original_journal_sha256": "a" * 64,
                "original_producer_receipt_sha256": str(ROUTES.index(route) + 1) * 64})
    return rows


def test_shared_originals_count_once_with_all_three_producer_observations():
    rows = observations()
    saved = deepcopy(rows)
    result = replay_inventory(rows, ("workspace", "run_id", "original_id"))
    assert rows == saved
    assert len(result) == 2
    assert [row["run_id"] for row in result] == ["business", "business-refresh"]
    assert all([item["route"] for item in row["route_observations"]] == list(ROUTES) for row in result)
    assert all(len({item["original_producer_receipt_sha256"] for item in row["route_observations"]}) == 3 for row in result)


@pytest.mark.parametrize("field,value", [
    ("original_text", "Replaced question."), ("accepted", False),
    ("original_journal_sha256", "b" * 64),
])
def test_same_original_identity_with_different_owner_content_or_proof_refuses(field, value):
    rows = observations()
    rows[2][field] = value
    with pytest.raises(ValueError, match="replay_original_conflict"):
        replay_inventory(rows, ("workspace", "run_id", "original_id"))


@pytest.mark.parametrize("change", ["drop", "extra", "duplicate", "namespace"])
def test_missing_extra_duplicate_or_wrong_namespace_never_collapses_into_complete_replay(change):
    rows = observations()
    if change == "drop":
        rows.pop()
    elif change == "extra":
        rows.append({**rows[-1], "original_id": "extra-original"})
    elif change == "duplicate":
        rows.append(deepcopy(rows[0]))
    else:
        rows[-1]["workspace"] = "/other/workspace"
    with pytest.raises(ValueError, match="replay_(inventory_gap|duplicate_observation)"):
        replay_inventory(rows, ("workspace", "run_id", "original_id"))


def test_same_native_attempts_keep_full_owner_receipt_identity_and_route_proof():
    rows = [{"route": route, "workspace": "/owned/workspace", "run_id": "business",
        "attempt_id": "attempt-one", "config_ref": "generator", "state": "succeeded",
        "final_accounting": True, "receipt_sha256": "a" * 64,
        "original_producer_receipt_sha256": str(index + 1) * 64} for index, route in enumerate(ROUTES)]
    identity = ("workspace", "run_id", "attempt_id", "config_ref")
    assert len(replay_inventory(rows, identity)) == 1
    rows[-1]["receipt_sha256"] = "b" * 64
    with pytest.raises(ValueError, match="replay_original_conflict"):
        replay_inventory(rows, identity)


def console_argv(request):
    argv = ["/installed/.venv/bin/evidence-wiki", "agent", request["operation"],
        "--target", "/owned/workspace", "--services", "/private/factory.py",
        "--noninteractive", "--format", "json"]
    for name, value in request["arguments"].items():
        argv.extend(["--" + name.replace("_", "-"), str(value)])
    return argv


def machine_input(request):
    return (json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
        "name": "research_" + request["operation"].replace("-", "_"),
        "arguments": {"target": "/owned/workspace", **request["arguments"]}}}) + "\n").encode()


def test_every_original_console_and_stdio_request_matches_same_python_request():
    requests = transport_requests("business")
    assert len(requests) == 5
    assert requests[-1]["arguments"] == {"run_id": "business-refresh", "previous_run_id": "business",
        "request_id": "business-refresh-drive", "max_steps": 1, "planner_config_ref": "planner"}
    assert [console_request(console_argv(row), "/owned/workspace", "/private/factory.py") for row in requests] == requests
    assert [stdio_request(machine_input(row), "/owned/workspace") for row in requests] == requests
    altered = deepcopy(requests[-1])
    altered["arguments"]["max_steps"] = 2
    assert stdio_request(machine_input(altered), "/owned/workspace") != requests[-1]
    assert console_request(console_argv(altered), "/owned/workspace", "/private/factory.py") != requests[-1]


def test_duplicate_console_options_and_stdio_calls_or_changed_target_refuse():
    request = transport_requests("business")[2]
    argv = console_argv(request)
    with pytest.raises(ValueError, match="request_options"):
        console_request(argv + ["--run-id", "other"], "/owned/workspace", "/private/factory.py")
    with pytest.raises(ValueError, match="request_target"):
        console_request(argv, "/other/workspace", "/private/factory.py")
    raw = machine_input(request)
    with pytest.raises(ValueError, match="request_inventory"):
        stdio_request(raw + raw, "/owned/workspace")
    with pytest.raises(ValueError, match="request_target"):
        stdio_request(raw, "/other/workspace")


@pytest.fixture
def installed_python_binding(tmp_path):
    driver = tmp_path / "public_driver.py"
    literal = "print('owned driver')\n"
    driver.write_text(literal)
    producer = tmp_path / "integration.py"
    producer.write_text("DRIVER = " + repr(literal) + "\n")
    row = {"workspace": "/owned/workspace", "services": "/private/factory.py", "run_id": "business"}
    interpreter = "/installed/.venv/bin/python"
    deadline = "2026-10-11T02:00:00+00:00"
    argv = [interpreter, "-I", "-B", str(driver), "python", "--bundle=" + str(tmp_path / "public-bundle"),
        "--workspace=" + row["workspace"], "--services=" + row["services"], "--run-id=business",
        "--deadline=" + deadline, "--allowance-deadline=2026-10-11T01:30:00+00:00", "--timeout-seconds=1800"]
    manifest = lambda path: [{"path": str(path.resolve()), "sha256": hash_file(path)["sha256"]}]
    plan = {"jobs": [{"role": "Integration", "argv": ["/project/.venv/bin/python", "-B", str(producer)],
        "command_manifest": manifest(producer), "timeout_seconds": 1800}]}
    intent = {"reserved_at": "2026-10-11T01:00:00+00:00", "command_manifest": manifest(driver)}
    key = b"fixture-read-only-observer-key"
    original = tmp_path / "signed-intent.json"
    original.write_text(json.dumps(_signed(intent, key)))
    original.chmod(0o600)
    authenticated = _authenticated(original, key)
    return {"argv": argv}, row, {"interpreter": interpreter}, plan, deadline, authenticated


def test_signed_original_python_intent_still_requires_exact_installed_driver_and_runtime(installed_python_binding):
    python_transport_command(*installed_python_binding)


@pytest.mark.parametrize("position,value", [
    (0, "/project/.venv/bin/python"), (1, "-B"), (4, "identity"),
    (5, "--bundle=/other"), (6, "--workspace=/other"), (7, "--services=/other/factory.py"),
    (8, "--run-id=other"), (9, "--deadline=2026-10-11T03:00:00+00:00"),
    (10, "--allowance-deadline=2026-10-11T03:00:00+00:00"), (11, "--timeout-seconds=3600"),
])
def test_authenticated_python_output_cannot_certify_another_runtime_or_allowance(installed_python_binding, position, value):
    binding = deepcopy(installed_python_binding)
    binding[0]["argv"][position] = value
    with pytest.raises(ValueError, match="qualification_(real_installed_python_required|python_runtime_binding)"):
        python_transport_command(*binding)


def test_driver_bytes_and_both_original_source_commitments_are_required(installed_python_binding):
    command, row, installation, plan, deadline, intent = installed_python_binding
    altered = deepcopy(plan)
    altered["jobs"][0]["command_manifest"] = []
    with pytest.raises(ValueError, match="producer_not_pinned"):
        python_transport_command(command, row, installation, altered, deadline, intent)
    altered_intent = deepcopy(intent)
    altered_intent["command_manifest"] = []
    with pytest.raises(ValueError, match="driver_not_pinned"):
        python_transport_command(command, row, installation, plan, deadline, altered_intent)
    Path(command["argv"][3]).write_text("print('different driver')\n")
    with pytest.raises(ValueError, match="driver_not_pinned"):
        python_transport_command(command, row, installation, plan, deadline, intent)


@pytest.mark.parametrize("position,value", [(0, "/wrapper"), (3, "/other/root"), (5, "/other/factory.py"), (7, "other-operation")])
def test_stdio_original_command_must_bind_selected_console_root_factory_and_operations(position, value):
    row = {"workspace": "/owned/workspace", "services": "/private/factory.py"}
    console = "/installed/.venv/bin/evidence-wiki"
    argv = [console, "serve-onboarding-mcp", "--allow-root", row["workspace"], "--research-services", row["services"],
        "--allow-research-operation", "run-cycle", "--allow-research-operation", "refresh"]
    stdio_transport_command(argv, row, console)
    argv[position] = value
    with pytest.raises(ValueError, match="real_stdio_required"):
        stdio_transport_command(argv, row, console)


def current_view():
    return {"schema_version": "research-session-current/v2", "target": "/owned/workspace", "run_id": "business",
        "basis_id": "sha256:" + "a" * 64, "policy_id": "sha256:" + "b" * 64,
        "observed_utc": "2026-10-11T01:00:00+00:00", "accounting": {"original_ids": [], "original_outcomes": [],
            "accounting_complete": True, "missing_mapping": False, "accepted_count": 0, "unresolved_count": 0},
        "canonical_release": {}, "run": None, "sources": {}, "qualification": {"state": "unknown", "reason": "not_observed", "profiles": []},
        "usage": {name: None for name in ("seconds", "model_calls", "tool_calls", "tokens", "cost_limit", "claims", "sources",
            "repair_cycles", "adjudication_cycles", "retry_attempts", "acquisition_attempts")},
        "policy_compatible": True, "limitations": [], "authority": {}, "execution": {}, "authorized": False, "evidence_accepted": False}


def result_envelopes():
    current = current_view()
    next_work = {"schema_version": "research-session-next/v2", "current": deepcopy(current), "decision": None,
        "actions": [], "authorized": False, "evidence_accepted": False}
    results = [current, next_work, {"run_id": "business"}, {"verdict": "accept"}, {"run_id": "business-refresh"}]
    return [{"operation": request["operation"], "status": "returned", "error": None, "result": result}
        for request, result in zip(transport_requests("business"), results, strict=True)]


@pytest.mark.parametrize("change", ["missing_current", "missing_next", "empty", "duplicate", "extra", "wrong_run"])
def test_authenticated_returned_labels_cannot_hide_missing_or_misbound_operation_results(tmp_path, change):
    envelopes = result_envelopes()
    transport_responses(envelopes, "business")
    if change == "missing_current":
        envelopes[0].pop("result")
    elif change == "missing_next":
        envelopes[1].pop("result")
    elif change == "empty":
        envelopes[1]["result"] = {}
    elif change == "duplicate":
        envelopes[1] = deepcopy(envelopes[0])
    elif change == "extra":
        envelopes.append(deepcopy(envelopes[0]))
    else:
        envelopes[1]["result"]["current"]["run_id"] = "other"
    key = b"fixture-envelope-observer-key"
    original = tmp_path / "signed-original.json"
    original.write_text(json.dumps(_signed({"responses": envelopes}, key)))
    original.chmod(0o600)
    authenticated = _authenticated(original, key)
    with pytest.raises(ValueError, match="qualification_(five_step_journey|required_operation_refused|current_next_result_binding)"):
        transport_responses(authenticated["responses"], "business")


def test_native_next_work_null_decision_is_valid_but_missing_or_wrong_type_is_not():
    observed = result_envelopes()
    assert observed[1]["result"]["decision"] is None
    transport_responses(observed, "business")
    del observed[1]["result"]["decision"]
    with pytest.raises(ValueError, match="current_next_result_binding"):
        transport_responses(observed, "business")
    observed[1]["result"]["decision"] = "returned"
    with pytest.raises(ValueError, match="current_next_result_binding"):
        transport_responses(observed, "business")


def test_actual_nonterminal_next_work_owner_returns_valid_null_decision(monkeypatch):
    from evidence_wiki import execution_session

    current = current_view()
    monkeypatch.setattr(execution_session, "current", lambda session: deepcopy(current))
    monkeypatch.setattr(execution_session, "retained", lambda session: [])
    monkeypatch.setattr(execution_session, "_action", lambda session, view, records:
        {"operation": "generate", "reason": "original_material_required", "config_ref": "generator"})
    actual = execution_session.next_work(object())
    assert actual["decision"] is None and actual["actions"][0]["operation"] == "generate"
    observed = result_envelopes()
    observed[1]["result"] = actual
    transport_responses(observed, "business")
    changed = deepcopy(observed)
    changed[1]["result"]["actions"][0]["operation"] = "unregistered"
    with pytest.raises(ValueError, match="current_next_owner_contract_invalid") as error:
        transport_responses(changed, "business")
    assert error.value.__cause__.error_code == "RESEARCH_INVALID"


def opening_envelopes():
    observed = result_envelopes()
    current = observed[0]["result"]
    current.update(run_id=None, run=None, canonical_release={"research_complete": False})
    current["execution"] = {"profile_id": "sha256:"+"c"*64, "scope_id": "work-execution",
        "source_bindings": [{"source_id": "raw-original", "closure_id": "sha256:"+"d"*64}],
        "steps_used": 0, "accounting": None, "measurement_report": None, "pending": None}
    current["accounting"] = {"original_ids": ["q"], "original_outcomes": [{"original_id": "q",
        "original_text": "Assess the original declared observation.", "accepted": False,
        "questions": [{"question_id": "q", "accepted": False}]}], "accounting_complete": True,
        "missing_mapping": False, "accepted_count": 0, "unresolved_count": 1}
    observed[1]["result"]["current"] = deepcopy(current)
    observed[1]["result"]["current"]["observed_utc"] = "2026-10-11T01:00:01+00:00"
    observed[1]["result"]["actions"] = [{"operation": "prepare", "owner": "canonical_research_owners",
        "arguments": {"operation": "prepare", "reason": "original_material_required"},
        "preconditions": ["current_basis", "frozen_policy", "scoped_capabilities", "remaining_allowance"],
        "effects": ["durable_correlated_transition"], "required_grants": ["research_session.advance"],
        "reason": "original_material_required", "authorized": False, "evidence_accepted": False}]
    return observed


def opening_native_result(opening):
    current = deepcopy(opening)
    current["run_id"] = "business"
    current["accounting"]["accepted_count"] = 1
    current["accounting"]["unresolved_count"] = 0
    current["accounting"]["original_outcomes"][0]["accepted"] = True
    return {"workspace": "/owned/workspace", "run_id": "business", "current": current,
        "policy": {"policy_id": opening["policy_id"]}, "profile": {"profile_sha256": "c"*64}}


def test_nullable_opening_preserves_populated_originals_and_native_binding(monkeypatch):
    from evidence_wiki import execution_session

    observed = opening_envelopes()
    current = deepcopy(observed[0]["result"])
    monkeypatch.setattr(execution_session,"current",lambda session:deepcopy(current))
    monkeypatch.setattr(execution_session,"retained",lambda session:[])
    monkeypatch.setattr(execution_session,"_action",lambda session,view,records:
        {"operation":"prepare","reason":"original_material_required"})
    observed[1]["result"] = execution_session.next_work(object())
    assert observed[1]["result"]["current"]["run_id"] is None
    assert observed[1]["result"]["decision"] is None
    opening = transport_responses(observed,"business",route="python",first_route=True,workspace="/owned/workspace")
    assert opening["accounting"]["original_ids"] == ["q"]
    _opening_native_binding(opening,opening_native_result(opening))


@pytest.mark.parametrize("route,first",[("console",True),("stdio",True),("python",False),(None,False)])
def test_nullable_opening_requires_explicit_first_python_context(route,first):
    with pytest.raises(ValueError,match="current_next_result_binding"):
        transport_responses(opening_envelopes(),"business",route=route,first_route=first,workspace="/owned/workspace")


@pytest.mark.parametrize("change",["missing_run_id","populated_run","mixed_run","wrong_run","target", "basis",
    "policy","profile","scope","source","pending","accounting","measurement","steps","boolean_steps",
    "accepted","accepted_count","canonical_complete","lost_original","empty_originals","text","missing_mapping",
    "incomplete_accounting","unresolved_count","usage","authorized","decision","missing_result","no_actions",
    "duplicate_original"])
def test_nullable_opening_refuses_missing_mixed_or_prior_business_state(change):
    observed = opening_envelopes()
    current, next_current = observed[0]["result"], observed[1]["result"]["current"]
    if change == "missing_run_id": del current["run_id"]
    elif change == "populated_run": current["run"] = {"run_id":"business"}
    elif change == "mixed_run": next_current["run_id"] = "business"
    elif change == "wrong_run": current["run_id"] = next_current["run_id"] = "other"
    elif change in {"target","basis","policy"}:
        field={"target":"target","basis":"basis_id","policy":"policy_id"}[change]
        next_current[field] = "/other/workspace" if change == "target" else "sha256:"+"e"*64
    elif change in {"profile","scope","source"}:
        field={"profile":"profile_id","scope":"scope_id","source":"source_bindings"}[change]
        next_current["execution"][field] = [] if change == "source" else "other"
    elif change in {"pending","accounting","measurement","steps","boolean_steps"}:
        field={"pending":"pending","accounting":"accounting","measurement":"measurement_report",
            "steps":"steps_used","boolean_steps":"steps_used"}[change]
        for view in (current,next_current): view["execution"][field] = False if change == "boolean_steps" else 1
    elif change in {"accepted","text"}:
        for view in (current,next_current):
            view["accounting"]["original_outcomes"][0]["accepted" if change == "accepted" else "original_text"] = True if change == "accepted" else ""
    elif change in {"accepted_count","missing_mapping","incomplete_accounting","unresolved_count"}:
        field={"accepted_count":"accepted_count","missing_mapping":"missing_mapping",
            "incomplete_accounting":"accounting_complete","unresolved_count":"unresolved_count"}[change]
        for view in (current,next_current): view["accounting"][field] = {"accepted_count":1,"missing_mapping":True,
            "incomplete_accounting":False,"unresolved_count":0}[change]
    elif change == "canonical_complete":
        for view in (current,next_current): view["canonical_release"]["research_complete"] = True
    elif change == "duplicate_original":
        for view in (current,next_current):
            view["accounting"]["original_ids"].append("q")
            view["accounting"]["original_outcomes"].append(deepcopy(view["accounting"]["original_outcomes"][0]))
            view["accounting"]["unresolved_count"] = 2
    elif change in {"lost_original","empty_originals"}:
        for view in (current,next_current):
            view["accounting"]["original_outcomes"] = []
            if change == "empty_originals": view["accounting"].update(original_ids=[],unresolved_count=0)
    elif change == "usage":
        for view in (current,next_current): view["usage"]["seconds"] = 0
    elif change == "authorized": current["authorized"] = True
    elif change == "decision": observed[1]["result"]["decision"] = {}
    elif change == "missing_result": del observed[0]["result"]
    elif change == "no_actions": observed[1]["result"]["actions"] = []
    with pytest.raises((ValueError,KeyError),match="qualification_"):
        transport_responses(observed,"business",route="python",first_route=True,workspace="/owned/workspace")


@pytest.mark.parametrize("change",["target","policy","profile","scope","source","original_id","original_text","drop_original","null_native_run"])
def test_nullable_opening_must_join_exact_later_native_originals(change):
    opening = opening_envelopes()[0]["result"]
    result = opening_native_result(opening)
    if change == "null_native_run": result["current"]["run_id"] = None
    elif change == "target": result["workspace"] = "/other/workspace"
    elif change == "policy": result["current"]["policy_id"] = "sha256:"+"e"*64
    elif change == "profile": result["profile"]["profile_sha256"] = "e"*64
    elif change in {"scope","source"}: result["current"]["execution"]["scope_id" if change == "scope" else "source_bindings"] = "other" if change == "scope" else []
    elif change == "original_id": result["current"]["accounting"]["original_ids"] = ["other"]
    elif change == "original_text": result["current"]["accounting"]["original_outcomes"][0]["original_text"] = "Changed original observation."
    elif change == "drop_original": result["current"]["accounting"]["original_outcomes"] = []
    with pytest.raises(ValueError,match="opening_native_identity_changed"):
        _opening_native_binding(opening,result)


def test_declared_journey_order_uses_actual_receipt_epochs():
    first={"started_at":"2026-10-11T01:00:00+00:00","finished_at":"2026-10-11T01:00:01+00:00"}
    end=_transport_command_order(first,None)
    following={"started_at":"2026-10-11T01:00:01+00:00","finished_at":"2026-10-11T01:00:02+00:00"}
    assert _transport_command_order(following,end) > end
    with pytest.raises(ValueError,match="transport_command_order"):
        _transport_command_order(first,end)
    with pytest.raises(ValueError,match="transport_command_order"):
        _transport_command_order({"started_at":following["finished_at"],"finished_at":following["started_at"]},None)
