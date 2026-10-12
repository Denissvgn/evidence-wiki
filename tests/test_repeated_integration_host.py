"""Native owner projections retain original authentication and finite validity."""

import json
import runpy
import time
from contextlib import nullcontext
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from evidence_wiki import Workspace
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.host_context import HostContext
from tools import _integration_improvement_owners as owners
from tools import _repeated_integration_host as host
from tools import run_repeated_integrations as cli
from tools._release_decision import canonical, digest


def scenario_fixture(monkeypatch, tmp_path, terminals, *, rejected=False, brief_errors=()):
    """Observe recipe continuation without native workloads or signing material."""
    monkeypatch.setattr(host, "candidate_manifest", lambda _: {"candidate": "fixed"})
    monkeypatch.setattr(cli, "candidate_manifest", host.candidate_manifest)
    monkeypatch.setattr(host, "host_identity", lambda route: {"route": route})
    monkeypatch.setattr(cli, "host_identity", host.host_identity)
    plan = cli.reference_plan()
    root = tmp_path / "scenario" / "fixture" / "workspace"
    root.mkdir(parents=True)
    records = [{"kind": "execution_applied", "data": {"operation": "evaluate"}}]
    session = SimpleNamespace()
    runtime = SimpleNamespace(context=SimpleNamespace(activate=lambda: nullcontext()),
        execution=SimpleNamespace(records=lambda _: records))
    opened = SimpleNamespace(research=SimpleNamespace(session=lambda *a, **kw: nullcontext(session)))
    runtime.policy = object()
    monkeypatch.setattr(host.Workspace, "open", lambda _: nullcontext(opened))
    monkeypatch.setattr(host, "_load_example", lambda: (lambda _: (root, runtime, None), None, None, None, None))
    monkeypatch.setattr(host, "_closure", lambda _: ({}, {}))
    rotations, calls, returned = [], [], {}
    pending_errors = iter(brief_errors)
    controls = []
    def rotate(*args):
        rotations.append(args[-1])
        return {}, {"revision": str(args[-1])}
    monkeypatch.setattr(host, "_rotate", rotate)
    class Routes:
        def __init__(self, *args):
            self.calls = calls
        def close(self):
            pass
        def call(self, operation, arguments, control):
            cycle = arguments["run_id"].removeprefix("reference-")
            calls.append({"cycle": cycle, "operation": operation})
            controls.append(control)
            if operation in {"run-cycle", "refresh"}:
                (root / "runs" / arguments["run_id"]).mkdir(parents=True, exist_ok=True)
                value = {"outcome": "rejected" if rejected else "accepted",
                    "cycle": {"current_basis_id": cycle}, "changed_source_ids": ["source"], "approval_reuse": False}
                if terminals.get(cycle, True) != "missing":
                    value["terminal"] = terminals.get(cycle, True)
            elif operation == "brief":
                if cycle == "change_1":
                    error = next(pending_errors, None)
                    if error is not None:
                        raise error
                value = {"verdict": "reject" if rejected else "accept", "original_outcomes": [
                    {"eligible_questions": [{"claims": [{"text": "Finite observation: sum; value=11; units=count; scope=fixture-cohort; time=fixture-observation."}]}]}]}
            else:
                value = {"ledger": cycle, "original_count": 1}
            returned[(cycle, operation)] = value
            return value
    monkeypatch.setattr(host, "Routes", Routes)
    # run_scenario requires a new enclosing directory; fixture provision is inert.
    directory = tmp_path / "original-scenario"
    return SimpleNamespace(plan=plan, directory=directory, calls=calls, rotations=rotations,
        returned=returned, records=records, controls=controls)


@pytest.mark.parametrize("cycle", ["baseline", "change_1"])
@pytest.mark.parametrize("terminal", [False, None, 0, 1, "true", "missing"])
def test_nonterminal_parent_preserves_originals_without_later_rotation(monkeypatch, tmp_path, cycle, terminal):
    data = scenario_fixture(monkeypatch, tmp_path, {cycle: terminal})
    report = host.run_scenario(data.plan, "direct", data.directory)
    index = host.CYCLES.index(cycle)
    row = report["cycles"][index]
    assert report["failure"]["kind"] == "ValueError"
    assert row["projection"]["disposition"] == "failed"
    assert row["projection"]["integration_success"] is False
    assert row["native_records_sha256"] == owners.native_digest(data.records)
    assert row["native_record_count"] == len(data.records)
    for name, operation in [("result", "run-cycle" if index == 0 else "refresh"), ("brief", "brief"), ("accounting", "report")]:
        original = row["originals"][name]
        path = Path(original["path"])
        raw = canonical(data.returned[(cycle, operation)])
        assert path.read_bytes() == raw
        assert original["raw_sha256"] == sha256(raw).hexdigest()
        assert path.stat().st_mode & 0o777 == 0o400
    assert data.rotations == ([] if index == 0 else [1])
    assert all(r["projection"]["disposition"] == "unstarted" and "source_update" not in r
        for r in report["cycles"][index + 1:])
    assert all(call["cycle"] in host.CYCLES[:index + 1] for call in data.calls)


def test_terminal_rejected_parents_can_refresh_under_distinct_slots(monkeypatch, tmp_path):
    data = scenario_fixture(monkeypatch, tmp_path, {}, rejected=True)
    report = host.run_scenario(data.plan, "direct", data.directory)
    assert report["failure"] is None
    assert data.rotations == [1, 2]
    assert all(row["projection"]["disposition"] == "completed" and row["projection"]["integration_success"] is True
        and row["outcome"] == "rejected" and row["projection"]["accepted"] is False for row in report["cycles"])


@pytest.mark.parametrize("grant_present,writable,reason", [
    (True, True, None),
    (False, True, "host_operation_not_granted"),
    (True, False, "host_path_outside_scope"),
])
def test_fixture_public_materialization_requires_explicit_grant_and_write_scope(
        monkeypatch, tmp_path, grant_present, writable, reason):
    """Exercise the real public facade/context boundary before mocked owner IO."""
    grants = runpy.run_path(str(host.EXAMPLE / "support.py"))["GRANTS"].copy()
    if not grant_present:
        grants.discard("evidence_usage.run_materialize")
    (tmp_path / "research.yml").write_text("{}\n")
    loaded, called = [], []
    def materialize(root, *, revision, expected_content_hash):
        called.append((root, revision, expected_content_hash))
        return {"materialized": True}
    def owner(self, stem):
        loaded.append(stem)
        return SimpleNamespace(run_materialize=materialize)
    monkeypatch.setattr(Workspace, "_script", owner)
    context = HostContext(allowed_roots=[tmp_path], grants=grants, actor_ref="fixture-host",
                          writable_roots=[tmp_path] if writable else [])
    with Workspace.open(tmp_path) as workspace, context.activate():
        if reason is not None:
            with pytest.raises(EvidenceWikiError) as error:
                workspace.usage.materialize("sha256:revision", expected_content_hash="sha256:previous")
            assert error.value.error_code == "HOST_CONTEXT_REFUSED"
            assert error.value.details["reason"] == reason
            assert loaded == called == []
        else:
            result = workspace.usage.materialize("sha256:revision", expected_content_hash="sha256:previous")
            assert result == {"materialized": True}
            assert loaded == ["evidence_usage"]
            assert called == [(tmp_path, "sha256:revision", "sha256:previous")]


def test_producer_support_requires_actual_frozen_counter_not_missing_or_prose():
    plan = {"proposals": [{"proposal_id": "small-review-experiment", "trigger_metric": "review_attempts", "trigger_threshold": 3},
                          {"proposal_id": "unobserved-retrieval", "trigger_metric": "omission_count", "trigger_threshold": 1}]}
    assert host.supported_proposals(plan, {"review_attempts": 2, "omission_count": None}) == []
    assert host.supported_proposals(plan, {"review_attempts": 3, "omission_count": None}) == ["small-review-experiment"]


def test_review_burden_includes_applied_adjudication_and_preserves_dispatch_scope():
    records = [{"kind": "execution_applied", "data": {"operation": "evaluate"}},
               {"kind": "execution_applied", "data": {"operation": "adjudicate"}},
               {"kind": "execution_applied", "data": {"operation": "generate"}},
               {"kind": "execution_attempt", "data": {"operation": "evaluate"}}]
    assert host.applied_review_count(records) == 2


@pytest.mark.parametrize("operation", ["run-cycle", "brief", "report"])
def test_mcp_original_remaining_allowance_cancels_blocked_public_call(operation, tmp_path):
    route = object.__new__(host.Routes)
    route.root, route.route, route.calls = tmp_path, "scoped_mcp", []
    checkpoints, remaining_names, observed = [], [], []
    class Control:
        def checkpoint(self, name):
            checkpoints.append(name)
            if name == "reference_route_after_call":
                raise TimeoutError("original allowance exhausted")
        def remaining_seconds(self, name):
            remaining_names.append(name)
            return 0.02
    class Server:
        def handle_message(self, message, *, cancellation):
            observed.append(message["params"]["name"])
            assert cancellation is not None
            assert cancellation.wait(0.5), "original finite deadline was not propagated"
            envelope = {"status": "returned", "result": {}, "error": None}
            return {"result": {"content": [{"text": json.dumps(envelope)}]}}
    route.server = Server()
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="original allowance exhausted"):
        route.call(operation, {"run_id": "finite-run"}, Control())
    assert time.monotonic() - started < 0.5
    assert observed == ["research_" + operation.replace("-", "_")]
    assert remaining_names == ["reference_mcp_call"]
    assert checkpoints == ["reference_route_before_call", "reference_route_after_call"]


def test_mcp_timer_is_cancelled_when_public_handler_raises(monkeypatch, tmp_path):
    route = object.__new__(host.Routes)
    route.root, route.route, route.calls = tmp_path, "scoped_mcp", []
    observed = []
    class Timer:
        def __init__(self, seconds, callback):
            observed.append(("allowance", seconds))
        def start(self):
            observed.append("started")
        def cancel(self):
            observed.append("cancelled")
    class Server:
        def handle_message(self, *args, **kwargs):
            raise RuntimeError("handler stopped")
    control = SimpleNamespace(checkpoint=lambda _: None, remaining_seconds=lambda _: 0.1)
    route.server = Server()
    monkeypatch.setattr(host.threading, "Timer", Timer)
    with pytest.raises(RuntimeError, match="handler stopped"):
        route.call("brief", {"run_id": "finite-run"}, control)
    assert observed == [("allowance", 0.1), "started", "cancelled"]


def owner_fixture(monkeypatch, tmp_path, route="direct"):
    """Exercise adapter control flow without executing native workloads or keys."""
    monkeypatch.setattr(cli, "candidate_manifest", lambda _: {"candidate": "fixed"})
    monkeypatch.setattr(cli, "host_identity", lambda route: {"route": route})
    plan = cli.reference_plan()
    plan["frozen_at"] = "2030-01-01T11:00:00+00:00"
    plan["expires_at"] = "2030-01-01T13:00:00+00:00"
    selected = next(h for h in plan["hosts"] if h["route"] == route)
    directory = tmp_path / "original"
    report_path = directory / "report.json"
    workspace = directory / "fixture/workspace"
    factory = directory / "fixture/fixture-private/services.py"
    expected = "Finite observation: sum; value=11; units=count; scope=fixture-cohort; time=fixture-observation."
    brief = {"verdict": "accept", "original_outcomes": [{"eligible_questions": [{"claims": [{"text": expected}]}]}]}
    records = [{"kind": "execution_applied", "data": {"operation": "evaluate"}}]
    rows, files = [], {}
    for index, cycle in enumerate(host.CYCLES):
        slot = next(s for s in plan["slots"] if s["host_id"] == selected["host_id"] and s["cycle"] == cycle)
        result = {"terminal": True, "outcome": "accepted", "approval_reuse": False,
                  "changed_source_ids": ["source"], "reused_source_ids": [],
                  "cycle": {"current_basis_id": "basis-" + str(index)}}
        projection = {**dict.fromkeys(host.BOOLS, False), **dict.fromkeys(host.COUNTERS),
                      "disposition": "completed", "usefulness_scope": "fixture_mechanics",
                      "elapsed_seconds": "1", "supported_proposals": [], "integration_success": True,
                      "accepted": True, "useful": True, "history_preserved": True,
                      "source_drift_detected": True, "repair_attempts": 0, "review_attempts": 1}
        originals = {}
        for name, document in (("result", result), ("brief", brief), ("accounting", {})):
            path = directory / "observations" / (cycle + "-" + name + ".json")
            raw = canonical(document)
            files[path] = raw
            originals[name] = {"path": str(path), "raw_sha256": sha256(raw).hexdigest(), "size_bytes": len(raw)}
        rows.append({"slot_id": slot["slot_id"], "input_sha256": slot["input_sha256"],
                     "run_id": cycle, "projection": projection, "originals": originals,
                     "native_records_sha256": owners.native_digest(records), "basis_id": "basis-" + str(index),
                     "parent_run_id": None if index == 0 else host.CYCLES[index - 1],
                     "parent_history_before": {}, "parent_history_after": {}})
    report = {"plan_sha256": digest(plan), "candidate_sha256": plan["candidate_sha256"],
              "host": selected, "scenario": host.scenario(selected["route"]), "failure": None,
              "workspace": str(workspace), "factory": str(factory), "cycles": rows, "calls": []}
    observation_directory = tmp_path / "receipt"
    stdout_path = observation_directory / "attempt.stdout"
    key_path = tmp_path / "observer.key"
    plan_path = tmp_path / "plan.json"
    files[plan_path] = canonical(plan)
    argv = [str(owners.ROOT / ".venv/bin/python"), str(owners.ROOT / "tools/run_repeated_integrations.py"),
            "run", "--plan", str(plan_path), "--expected-plan", digest(plan),
            "--host", selected["route"], "--directory", str(directory)]
    selection = {"path": str(report_path), "raw_sha256": "", "source_head_sha256": "",
                 "observation": {"plan": {"binding": {"integration_plan_sha256": digest(plan)}},
                                 "attempt_id": "attempt", "argv": argv,
                                 "directory": str(observation_directory), "work_root": str(tmp_path),
                                 "expected_receipt_sha256": "trusted-receipt", "observer_key_path": str(key_path)}}
    verified = {"current_command_manifest_matches": True, "coverage_gap": None,
                "receipt": {"finished_at": "2030-01-01T12:00:00+00:00", "streams": {}}}

    def retain():
        raw = canonical(report)
        files[report_path] = raw
        selection["raw_sha256"] = sha256(raw).hexdigest()
        selection["source_head_sha256"] = digest(report)
        stdout = canonical({"report_path": str(report_path), "source_head_sha256": digest(report)})
        files[stdout_path] = stdout
        verified["receipt"]["streams"]["stdout"] = {"sha256": sha256(stdout).hexdigest(), "retained_bytes": len(stdout)}

    retain()
    files[key_path] = b"mocked observer key"
    monkeypatch.setattr(owners, "read_private", lambda path, _: files[Path(path)])
    monkeypatch.setattr(owners, "read_command_receipt", lambda *args, **kwargs: verified)
    monkeypatch.setattr(owners, "candidate_manifest", lambda _: {"candidate": "fixed"})
    monkeypatch.setattr(owners, "host_identity", lambda route: {"route": route})
    monkeypatch.setattr(owners, "_manifest", lambda _: {})
    execution = SimpleNamespace(records=lambda _: records)
    runtime = SimpleNamespace(context=SimpleNamespace(activate=nullcontext), policy=None, execution=execution)
    monkeypatch.setattr(owners.HostRuntime, "from_factory", lambda *args: runtime)
    opened = SimpleNamespace(research=SimpleNamespace(session=lambda *args, **kwargs: nullcontext(None)))
    monkeypatch.setattr(owners.Workspace, "open", lambda _: nullcontext(opened))
    proof = {"read_finished_utc": "2030-01-01T12:00:01+00:00", "journal": {"events": [{"data": {"record": {
        "kind": "execution_qualification_probe", "data": {"probe": {"expires_utc": "2030-01-01T12:00:30+00:00"}}}}}]},
        "current": {"accounting": {"accepted_count": 1}, "canonical_release": {"research_complete": True}}}
    calls = []
    monkeypatch.setattr(owners, "native_replay", lambda *args: (calls.append(args) or proof))
    return SimpleNamespace(plan=plan, report=report, files=files, selection=selection, verified=verified,
                           retain=retain, calls=calls, stdout_path=stdout_path,
                           readers=lambda: owners.host_readers(plan, {selected["host_id"]: selection}))


def test_owner_retains_actual_native_witness_window_and_caches_one_round(monkeypatch, tmp_path):
    data = owner_fixture(monkeypatch, tmp_path)
    natives = [reader.read() for reader in data.readers()]
    assert len(data.calls) == 1
    assert all(n.observed_at == "2030-01-01T12:00:01+00:00" for n in natives)
    assert all(n.expires_at == "2030-01-01T12:00:30+00:00" for n in natives)
    assert all(n.evidence_class == "simulated" for n in natives)


def test_owner_checks_projection_against_original_result(monkeypatch, tmp_path):
    data = owner_fixture(monkeypatch, tmp_path)
    data.report["cycles"][0]["projection"]["accepted"] = False
    data.retain()
    with pytest.raises(ValueError, match="integration_original_projection_changed"):
        data.readers()[0].read()


def test_global_producer_failure_cannot_bypass_positive_native_checks(monkeypatch, tmp_path):
    data = owner_fixture(monkeypatch, tmp_path)
    data.report["failure"] = {"kind": "CandidateChanged"}
    data.retain()
    with pytest.raises(ValueError, match="integration_failed_producer_positive_projection"):
        data.readers()[0].read()
    assert data.calls == []


def test_native_original_response_bytes_are_still_required(monkeypatch, tmp_path):
    data = owner_fixture(monkeypatch, tmp_path)
    path = Path(data.report["cycles"][0]["originals"]["brief"]["path"])
    data.files[path] = canonical({"verdict": "accept", "claims": "changed"})
    with pytest.raises(ValueError, match="integration_original_response_changed"):
        data.readers()[0].read()


def retain_refusal(data):
    envelope = {"status": "refused", "result": None, "error": {
        "error_code": "RESEARCH_RESOLUTION_REFUSED", "message": "refused",
        "details": {"reason": "resolution_current_inputs_changed"}}}
    raw = json.dumps(envelope, indent=2).encode()
    path = Path(data.selection["path"]).parent / "observations/call-1.json"
    data.files[path] = raw
    data.report["calls"] = [{"operation": "brief", "arguments": {"run_id": "baseline"},
        "state": "refused", "result_sha256": digest(envelope), "envelope_raw_sha256": sha256(raw).hexdigest(),
        "envelope_size_bytes": len(raw), "error_code": "RESEARCH_RESOLUTION_REFUSED",
        "reason": "resolution_current_inputs_changed", "original_envelope": {
            "path": str(path), "raw_sha256": sha256(raw).hexdigest(), "size_bytes": len(raw)}}]
    data.retain()
    return path


def suspend_projections(data):
    data.report["failure"] = {"kind": "EvidenceWikiError", "error_code": "RESEARCH_RESOLUTION_REFUSED"}
    for row in data.report["cycles"]:
        row["projection"].update(accepted=False, integration_success=False, useful=False)
    data.report["cycles"][0]["originals"].pop("brief")
    data.report["cycles"][0]["originals"].pop("accounting")
    for row in data.report["cycles"][1:]:
        row.pop("originals")
    data.retain()


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("mutation", ["tamper", "delete", "rebind", "symlink", "size", "semantic", "metadata"])
def test_committed_refusal_custody_is_required(monkeypatch, tmp_path, failed, mutation):
    data = owner_fixture(monkeypatch, tmp_path)
    if failed:
        suspend_projections(data)
    path = retain_refusal(data)
    call = data.report["calls"][0]
    if mutation == "tamper":
        data.files[path] += b" "
    elif mutation == "delete":
        del data.files[path]
    elif mutation == "rebind":
        call["original_envelope"]["path"] = str(path.parent / "../observer.key")
    elif mutation == "symlink":
        path.parent.mkdir(parents=True)
        path.symlink_to(tmp_path / "unrelated")
    elif mutation == "size":
        call["original_envelope"]["size_bytes"] += 1
    elif mutation == "semantic":
        call["result_sha256"] = "0" * 64
    else:
        call["reason"] = "other_reason"
    data.retain()
    with pytest.raises(ValueError, match="integration_original_response_changed"):
        data.readers()[0].read()
    assert not data.calls


@pytest.mark.parametrize("mutation", ["tamper", "delete", "rebind", "unknown"])
def test_partial_failed_original_custody_is_required(monkeypatch, tmp_path, mutation):
    data = owner_fixture(monkeypatch, tmp_path)
    suspend_projections(data)
    originals = data.report["cycles"][0]["originals"]
    original = originals["result"]
    path = Path(original["path"])
    if mutation == "tamper":
        data.files[path] += b" "
    elif mutation == "delete":
        del data.files[path]
    elif mutation == "rebind":
        original["path"] = str(path.parent / "baseline-brief.json")
    else:
        originals["unknown"] = originals.pop("result")
    data.retain()
    with pytest.raises(ValueError, match="integration_original_response_changed"):
        data.readers()[0].read()
    assert not data.calls


def test_intact_partial_failure_and_unreturned_calls_remain_unknown(monkeypatch, tmp_path):
    data = owner_fixture(monkeypatch, tmp_path)
    suspend_projections(data)
    retain_refusal(data)
    data.report["calls"].extend([{"operation": "report", "arguments": {"run_id": "baseline"}, "state": "started"},
                                 {"operation": "tools/list", "result_sha256": "0" * 64}])
    data.retain()
    evidence = [reader.read() for reader in data.readers()]
    assert all(not item.projection["accepted"] and not item.projection["integration_success"] for item in evidence)
    assert not data.calls


def test_intact_refused_brief_remains_consumable_with_later_success(monkeypatch, tmp_path):
    data = owner_fixture(monkeypatch, tmp_path)
    retain_refusal(data)
    assert all(item.projection["accepted"] for item in (reader.read() for reader in data.readers()))
    assert len(data.calls) == 1


def test_returned_call_cannot_omit_committed_envelope(monkeypatch, tmp_path):
    data = owner_fixture(monkeypatch, tmp_path)
    retain_refusal(data)
    data.report["calls"][0].pop("original_envelope")
    data.retain()
    with pytest.raises(ValueError, match="integration_original_response_changed"):
        data.readers()[0].read()


def retain_command_envelope(data, refused):
    from evidence_wiki.research_command_contracts import RESULT, checked
    from evidence_wiki.research_command_operations import refusal

    path = retain_refusal(data)
    if refused:
        envelope = refusal("brief", EvidenceWikiError("RESEARCH_RESOLUTION_REFUSED", "refused",
            details={"reason": "resolution_current_inputs_changed"}))
    else:
        brief_path = Path(data.report["cycles"][0]["originals"]["brief"]["path"])
        envelope = checked(RESULT, {"schema_version": RESULT, "operation": "brief", "terminal": True,
            "status": "returned", "result": owners._original(data.files[brief_path]), "error": None,
            "exit_code": 0, "authorized": False, "evidence_accepted": False})
    return path, envelope


def commit_envelope(data, path, envelope, *, formatted=False):
    raw = json.dumps(envelope, indent=2).encode() if formatted else canonical(envelope)
    data.files[path] = raw
    call = data.report["calls"][0]
    call.update(state=envelope["status"], result_sha256=digest(envelope),
                envelope_raw_sha256=sha256(raw).hexdigest(), envelope_size_bytes=len(raw))
    call["original_envelope"].update(raw_sha256=sha256(raw).hexdigest(), size_bytes=len(raw))
    data.retain()


@pytest.mark.parametrize("route", ["direct", "current_caller", "scoped_mcp"])
@pytest.mark.parametrize("refused", [False, True])
def test_route_selected_native_envelope_is_consumable(monkeypatch, tmp_path, route, refused):
    data = owner_fixture(monkeypatch, tmp_path, route)
    path, envelope = retain_command_envelope(data, refused)
    if route == "direct":
        envelope = {name: envelope[name] for name in ("status", "result", "error")}
    commit_envelope(data, path, envelope, formatted=route == "scoped_mcp")
    evidence = [reader.read() for reader in data.readers()]
    # Transport flags do not override the result and current native acceptance.
    assert all(item.projection["accepted"] for item in evidence)
    assert len(data.calls) == 1


@pytest.mark.parametrize("route", ["current_caller", "scoped_mcp"])
def test_native_refusal_in_partial_failure_remains_unknown(monkeypatch, tmp_path, route):
    data = owner_fixture(monkeypatch, tmp_path, route)
    suspend_projections(data)
    path, envelope = retain_command_envelope(data, True)
    commit_envelope(data, path, envelope)
    assert all(not item.projection["accepted"] for item in (reader.read() for reader in data.readers()))
    assert not data.calls


@pytest.mark.parametrize("route", ["direct", "current_caller", "scoped_mcp"])
@pytest.mark.parametrize("mutation", ["shape", "operation", "schema", "terminal", "authorized", "evidence_accepted", "exit_code"])
def test_route_envelope_metadata_cannot_be_rebound(monkeypatch, tmp_path, route, mutation):
    data = owner_fixture(monkeypatch, tmp_path, route)
    path, envelope = retain_command_envelope(data, True)
    if route == "direct":
        if mutation != "shape":
            envelope = {name: envelope[name] for name in ("status", "result", "error")}
            envelope[mutation] = "unexpected"
    elif mutation == "shape":
        envelope = {name: envelope[name] for name in ("status", "result", "error")}
    else:
        envelope[mutation] = {"operation": "report", "schema": "unknown", "terminal": False,
                              "authorized": True, "evidence_accepted": True, "exit_code": -1}[mutation]
        if mutation == "schema":
            envelope["schema_version"] = envelope.pop("schema")
    # Recommit every byte/digest so refusal proves metadata validation itself.
    commit_envelope(data, path, envelope)
    with pytest.raises(ValueError, match="integration_original_response_changed"):
        data.readers()[0].read()
    assert not data.calls


def test_returned_envelope_matches_original_result(monkeypatch, tmp_path):
    data = owner_fixture(monkeypatch, tmp_path)
    path = retain_refusal(data)
    envelope = {"status": "returned", "result": {"outcome": "blocked"}, "error": None}
    raw = canonical(envelope)
    data.files[path] = raw
    call = data.report["calls"][0]
    call.update(operation="run-cycle", state="returned", result_sha256=digest(envelope),
                envelope_raw_sha256=sha256(raw).hexdigest(), envelope_size_bytes=len(raw))
    call["original_envelope"].update(raw_sha256=sha256(raw).hexdigest(), size_bytes=len(raw))
    data.retain()
    with pytest.raises(ValueError, match="integration_original_response_changed"):
        data.readers()[0].read()


@pytest.mark.parametrize("failed", [False, True])
def test_missing_committed_response_stops_decision(monkeypatch, tmp_path, failed):
    from datetime import datetime, timezone

    from tools import _integration_improvement as gate

    data = owner_fixture(monkeypatch, tmp_path)
    if failed:
        suspend_projections(data)
    path = retain_refusal(data)
    del data.files[path]
    pins = owners.evidence_pins(data.plan, {data.plan["hosts"][0]["host_id"]: data.selection})
    report = gate.decide(data.plan, digest(data.plan), pins, digest(pins), data.readers(),
                         datetime(2030, 1, 1, 12, tzinfo=timezone.utc))
    assert report["outcome"] == "stop"
    assert not data.calls


def test_current_command_manifest_mismatch_refuses_owner(monkeypatch, tmp_path):
    data = owner_fixture(monkeypatch, tmp_path)
    data.verified["current_command_manifest_matches"] = False
    with pytest.raises(ValueError, match="integration_original_command_changed"):
        data.readers()[0].read()


def test_signed_unrelated_command_cannot_appoint_itself_producer(monkeypatch, tmp_path):
    data = owner_fixture(monkeypatch, tmp_path)
    data.selection["observation"]["argv"] = ["echo", "retained-original-path"]
    with pytest.raises(ValueError, match="integration_original_producer_selection"):
        data.readers()[0].read()


def test_changed_stdout_after_receipt_verification_is_not_authenticated(monkeypatch, tmp_path):
    data = owner_fixture(monkeypatch, tmp_path)
    # Values remain identical, but the bytes no longer match signed retention.
    data.files[data.stdout_path] = data.files[data.stdout_path] + b" "
    with pytest.raises(ValueError, match="integration_original_stream_changed"):
        data.readers()[0].read()


@pytest.mark.parametrize("text", ["value=11; fixture-cohort", "Finite observation: sum; value=12; units=count; scope=fixture-cohort; time=fixture-observation."])
def test_usefulness_requires_exact_claim_not_matching_substrings(text):
    brief = {"verdict": "accept", "original_outcomes": [{"eligible_questions": [{"claims": [{"text": text}]}]}]}
    assert not host.reference_useful(brief)


def test_cli_rechecks_candidate_after_current_owner_reads(monkeypatch, tmp_path, capsys):
    data = owner_fixture(monkeypatch, tmp_path)
    plan_path, output = tmp_path / "plan.json", tmp_path / "decision.json"
    manifests = iter(({"candidate": "fixed"}, {"candidate": "changed"}))
    monkeypatch.setattr(cli, "candidate_manifest", lambda _: next(manifests))
    monkeypatch.setattr(cli, "_read", lambda path: data.plan if path == plan_path else {})
    monkeypatch.setattr(cli, "host_readers", lambda *args: ())
    monkeypatch.setattr(cli, "evidence_pins", lambda *args: {})
    def close_gate(*args, closing_check):
        try:
            closing_check()
        except ValueError:
            return {"outcome": "stop"}
        return {"outcome": "continue"}
    monkeypatch.setattr(cli, "decide", close_gate)
    monkeypatch.setattr(cli, "_deadline", lambda _: nullcontext())
    published = []
    monkeypatch.setattr(cli, "_publish", lambda *args: published.append(args))
    status = cli.main(["decide", "--plan", str(plan_path), "--expected-plan", digest(data.plan),
                       "--originals", str(tmp_path / "originals.json"), "--expected-evidence", digest({}),
                       "--output", str(output)])
    assert status == 2 and published == [(output, {"outcome": "stop"})]
    assert '"outcome": "stop"' in capsys.readouterr().out


def test_failed_change_charges_current_cycle_and_suspends_previous_positive(monkeypatch, tmp_path):
    data = owner_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(host, "candidate_manifest", lambda _: {"candidate": "fixed"})
    monkeypatch.setattr(host, "host_identity", lambda route: {"route": route})
    monkeypatch.setattr(host, "_deadline", lambda _: nullcontext())
    clock = iter(range(0, 200, 10))
    monkeypatch.setattr(host.time, "monotonic", lambda: next(clock))
    runtime = SimpleNamespace(context=SimpleNamespace(activate=nullcontext), policy=None,
                              execution=SimpleNamespace(records=lambda _: [{"kind": "execution_applied", "data": {"operation": "evaluate"}}]))
    root = tmp_path / "new-host" / "fixture/workspace"
    def provision(_):
        root.mkdir(parents=True)
        return root, runtime, None
    monkeypatch.setattr(host, "_load_example", lambda: (provision, None, None, None, None))
    monkeypatch.setattr(host, "_closure", lambda _: ({}, {}))
    monkeypatch.setattr(host, "_manifest", lambda _: {})
    monkeypatch.setattr(host, "_rotate", lambda *args: ({}, {}))
    monkeypatch.setattr(host, "OperationControl", lambda **kwargs: SimpleNamespace(close=lambda: None))
    brief_path = Path(data.report["cycles"][0]["originals"]["brief"]["path"])
    brief = owners._original(data.files[brief_path])
    class Routes:
        calls = []
        def __init__(self, *args):
            pass
        def call(self, operation, *args):
            if operation == "refresh":
                raise RuntimeError("interrupted change")
            if operation == "brief":
                return brief
            if operation == "report":
                return {}
            return {"terminal": True, "outcome": "accepted", "current_basis_id": "baseline"}
        def close(self):
            pass
    monkeypatch.setattr(host, "Routes", Routes)
    result = host.run_scenario(data.plan, "direct", root.parent.parent)
    first, failed, unstarted = (row["projection"] for row in result["cycles"])
    assert first["disposition"] == "completed" and first["accepted"] is False
    assert first["integration_success"] is False
    assert failed["disposition"] == "failed" and unstarted["disposition"] == "unstarted"
    assert float(failed["elapsed_seconds"]) < float(result["elapsed_seconds"])



def test_signed_command_coverage_gap_remains_unknown(monkeypatch, tmp_path):
    data = owner_fixture(monkeypatch, tmp_path)
    data.verified["coverage_gap"] = "missing_stop"
    with pytest.raises(ValueError, match="integration_original_command_coverage_unknown"):
        data.readers()[0].read()


def test_cli_candidate_preflight_integrity_stops_without_publication(monkeypatch, tmp_path, capsys):
    data = owner_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "candidate_manifest", lambda _: {"candidate": "changed"})
    monkeypatch.setattr(cli, "_read", lambda _: data.plan)
    published = []
    monkeypatch.setattr(cli, "_publish", lambda *args: published.append(args))
    status = cli.main(["decide", "--plan", str(tmp_path / "plan.json"), "--expected-plan", digest(data.plan)])
    assert status == 2 and published == []
    assert '"outcome": "stop"' in capsys.readouterr().out



def test_cli_closure_io_expiry_is_inside_final_gate_seal(monkeypatch, tmp_path):
    from datetime import datetime, timezone

    from tools import _integration_improvement as gate
    data = owner_fixture(monkeypatch, tmp_path)
    plan_path, originals_path, output = (tmp_path / name for name in ("plan.json", "originals.json", "decision.json"))
    originals = {data.plan["hosts"][0]["host_id"]: data.selection}
    monkeypatch.setattr(cli, "_read", lambda path: data.plan if path == plan_path else originals)
    clock = [datetime(2030, 1, 1, 12, 0, 2, tzinfo=timezone.utc)]
    monkeypatch.setattr(gate, "_clock", lambda _: clock[0])
    calls = []
    def manifest(_):
        calls.append(1)
        if len(calls) == 2:
            clock[0] = datetime(2030, 1, 1, 12, 0, 31, tzinfo=timezone.utc)
        return {"candidate": "fixed"}
    monkeypatch.setattr(cli, "candidate_manifest", manifest)
    monkeypatch.setattr(cli, "_deadline", lambda _: nullcontext())
    published = []
    monkeypatch.setattr(cli, "_publish", lambda *args: published.append(args))
    status = cli.main(["decide", "--plan", str(plan_path), "--expected-plan", digest(data.plan),
        "--originals", str(originals_path), "--expected-evidence", digest(owners.evidence_pins(data.plan, originals)),
        "--output", str(output)])
    assert status == 2 and len(calls) == 2
    report = published[0][1]
    observed = [row for row in report["slots"] if row["evidence"] is not None]
    assert len(observed) == 3
    assert all(row["problem"] == "owner_expired_at_close" for row in observed)
    assert report["metrics"]["terminal_slots"] == 0
    assert report["next_improvement"] is None



def test_malformed_changed_report_bytes_stop_before_parse(monkeypatch, tmp_path):
    from datetime import datetime, timezone

    from tools import _integration_improvement as gate

    data = owner_fixture(monkeypatch, tmp_path)
    originals = {data.plan["hosts"][0]["host_id"]: data.selection}
    data.files[Path(data.selection["path"])] = b"{malformed changed report"
    pins = owners.evidence_pins(data.plan, originals)
    report = gate.decide(data.plan, digest(data.plan), pins, digest(pins), data.readers(),
                         datetime(2030, 1, 1, 12, tzinfo=timezone.utc))
    assert report["outcome"] == "stop"
    assert data.calls == []


def test_changed_command_plan_manifest_checked_before_plan_parse(monkeypatch, tmp_path):
    data = owner_fixture(monkeypatch, tmp_path)
    plan_path = Path(data.selection["observation"]["argv"][4])
    data.files[plan_path] = b"{malformed changed plan"
    data.verified["current_command_manifest_matches"] = False
    with pytest.raises(ValueError, match="integration_original_command_changed"):
        data.readers()[0].read()



def test_cli_external_plan_pin_mismatch_stops_before_execution(monkeypatch, tmp_path, capsys):
    data = owner_fixture(monkeypatch, tmp_path)
    expected = digest(data.plan)
    data.plan["scope"]["observation"] = "changed"
    monkeypatch.setattr(cli, "_read", lambda _: data.plan)
    dispatched = []
    monkeypatch.setattr(cli, "run_scenario", lambda *args, **kwargs: dispatched.append(args))
    status = cli.main(["run", "--plan", str(tmp_path / "plan.json"), "--expected-plan", expected,
                       "--host", "direct", "--directory", str(tmp_path / "run")])
    assert status == 2 and dispatched == []
    assert '"outcome": "stop"' in capsys.readouterr().out


@pytest.mark.parametrize("reasons,completed", [
    (["resolution_current_inputs_changed"], True),
    (["resolution_current_inputs_changed", "resolution_current_inputs_changed"], False),
    (["resolution_configuration_changed"], False),
])
def test_fresh_brief_read_preserves_refresh_and_each_refusal(monkeypatch, tmp_path, reasons, completed):
    errors = [EvidenceWikiError("RESEARCH_RESOLUTION_REFUSED", "refused", details={"reason": reason})
              for reason in reasons]
    data = scenario_fixture(monkeypatch, tmp_path, {}, brief_errors=errors)
    report = host.run_scenario(data.plan, "direct", data.directory)
    row = report["cycles"][1]
    raw = canonical(data.returned[("change_1", "refresh")])
    assert Path(row["originals"]["result"]["path"]).read_bytes() == raw
    assert row["originals"]["result"]["raw_sha256"] == sha256(raw).hexdigest()
    assert row["brief_refusals"] == [{"error_code": "RESEARCH_RESOLUTION_REFUSED", "reason": r} for r in reasons]
    assert (report["failure"] is None) is completed
    brief_indices = [i for i, call in enumerate(data.calls) if call == {"cycle": "change_1", "operation": "brief"}]
    assert len(brief_indices) == (2 if reasons[0] == "resolution_current_inputs_changed" else 1)
    assert all(data.controls[i] is data.controls[brief_indices[0]] for i in brief_indices)
    if completed:
        assert row["projection"]["repair_attempts"] == 0
        assert row["projection"]["review_attempts"] == 1
        assert row["projection"]["accepted"] is True
    else:
        assert report["cycles"][2]["projection"]["disposition"] == "unstarted"
        assert data.rotations == [1]
        assert report["failure"]["reason"] == reasons[-1]


def test_closing_brief_refusal_cannot_renew_exhausted_control(monkeypatch, tmp_path):
    refusal = EvidenceWikiError("RESEARCH_RESOLUTION_REFUSED", "refused",
                               details={"reason": "resolution_current_inputs_changed"})
    data = scenario_fixture(monkeypatch, tmp_path, {}, brief_errors=[refusal])
    controls = []
    class Control:
        def __init__(self, **kwargs):
            controls.append(self)
        def checkpoint(self, name):
            raise TimeoutError("expired")
        def close(self):
            pass
    monkeypatch.setattr(host, "OperationControl", Control)
    report = host.run_scenario(data.plan, "direct", data.directory)
    assert report["failure"]["kind"] == "TimeoutError"
    assert len(controls) == 2
    assert sum(call == {"cycle": "change_1", "operation": "brief"} for call in data.calls) == 1
    assert report["cycles"][2]["projection"]["disposition"] == "unstarted"


@pytest.mark.parametrize("refused", [False, True])
def test_native_envelope_retained_before_post_call_timeout(tmp_path, refused):
    route = object.__new__(host.Routes)
    route.root, route.route, route.calls = tmp_path, "scoped_mcp", []
    route.observations = tmp_path
    error = {"error_code": "RESEARCH_RESOLUTION_REFUSED", "message": "private message",
             "details": {"reason": "resolution_current_inputs_changed", "private": "not exported"}}
    envelope = {"status": "refused" if refused else "returned", "result": None if refused else {"terminal": True},
                "error": error if refused else None}
    raw = json.dumps(envelope, indent=2).encode()
    class Server:
        def handle_message(self, *args, **kwargs):
            return {"result": {"content": [{"text": raw.decode()}]}}
    class Control:
        def checkpoint(self, name):
            if name == "reference_route_after_call":
                raise TimeoutError("expired after native return")
        def remaining_seconds(self, name):
            return 10
    route.server = Server()
    with pytest.raises(TimeoutError):
        route.call("brief", {"run_id": "finite-run"}, Control())
    record = route.calls[0]
    original = record["original_envelope"]
    assert Path(original["path"]).read_bytes() == raw
    assert original["raw_sha256"] == sha256(raw).hexdigest()
    assert Path(original["path"]).stat().st_mode & 0o777 == 0o400
    assert record["result_sha256"] == digest(envelope)
    assert "private message" not in json.dumps(record)
    assert "not exported" not in json.dumps(record)
    if refused:
        assert record["reason"] == "resolution_current_inputs_changed"


@pytest.mark.parametrize("code,remaining", [("OTHER_REFUSAL", 5), ("RESEARCH_RESOLUTION_REFUSED", 0)])
def test_brief_retry_requires_exact_code_and_positive_remaining_budget(monkeypatch, tmp_path, code, remaining):
    error = EvidenceWikiError(code, "refused", details={"reason": "resolution_current_inputs_changed"})
    data = scenario_fixture(monkeypatch, tmp_path, {}, brief_errors=[error])
    class Control:
        def __init__(self, **kwargs):
            pass
        def checkpoint(self, name):
            pass
        def remaining_seconds(self, name):
            return remaining
        def close(self):
            pass
    monkeypatch.setattr(host, "OperationControl", Control)
    report = host.run_scenario(data.plan, "direct", data.directory)
    assert report["failure"]["kind"] == ("TimeoutError" if remaining == 0 else "EvidenceWikiError")
    assert sum(call == {"cycle": "change_1", "operation": "brief"} for call in data.calls) == 1
    assert report["cycles"][2]["projection"]["disposition"] == "unstarted"


def test_original_retention_is_bounded_and_exclusive(tmp_path):
    target = tmp_path / "original.json"
    original = host._retain(target, b"original")
    assert original["size_bytes"] == 8
    with pytest.raises(FileExistsError):
        host._retain(target, b"replacement")
    assert target.read_bytes() == b"original"
    with pytest.raises(ValueError, match="integration_original_output_bound"):
        host._retain(tmp_path / "oversized.json", b"x" * 4_194_305)
    assert not (tmp_path / "oversized.json").exists()
