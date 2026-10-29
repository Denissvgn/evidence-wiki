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
    from tests._usage_fixture import require_reference_executor
    require_reference_executor()
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
    rotations, calls, returned, refreshes, events = [], [], {}, [], []
    pending_errors = iter(brief_errors)
    controls = []
    def rotate(*args):
        rotations.append(args[-1])
        return {}, {"revision": str(args[-1])}
    monkeypatch.setattr(host, "_rotate", rotate)
    def refresh_current(selected_runtime, selected_root, run_id):
        assert selected_runtime is runtime and selected_root == root
        refreshes.append(run_id)
        events.append((run_id.removeprefix("reference-"), "refresh_current"))
        (root / "runs" / run_id / "host-current.json").write_text(str(len(refreshes)))
        return True
    monkeypatch.setattr(host, "_load_current_refresh", lambda: refresh_current)
    class Routes:
        def __init__(self, *args):
            self.calls = calls
        def close(self):
            pass
        def call(self, operation, arguments, control):
            cycle = arguments["run_id"].removeprefix("reference-")
            calls.append({"cycle": cycle, "operation": operation})
            events.append((cycle, operation))
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
        returned=returned, records=records, controls=controls, refreshes=refreshes, events=events, root=root)


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
@pytest.mark.parametrize("stopped", ["expired", "cancelled"])
def test_mcp_original_control_stops_blocked_public_call(operation, stopped, tmp_path, monkeypatch):
    import threading
    from datetime import datetime, timedelta, timezone

    from evidence_wiki import OperationControl

    route = object.__new__(host.Routes)
    route.root, route.route, route.calls = tmp_path, "scoped_mcp", []
    checkpoints, observed = [], []
    cancellation = threading.Event()
    borrowed = OperationControl(deadline_utc=(datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(),
        timeout_seconds=0.05 if stopped == "expired" else 60,
        cancel_requested=cancellation.is_set if stopped == "cancelled" else None)
    original_checkpoint = borrowed.checkpoint

    def checkpoint(name):
        checkpoints.append(name)
        original_checkpoint(name)

    monkeypatch.setattr(borrowed, "checkpoint", checkpoint)

    class Server:
        def handle_message(self, message, *, operation_control):
            observed.append(message["params"]["name"])
            assert operation_control is borrowed
            if stopped == "expired":
                time.sleep(0.1)
            else:
                cancellation.set()
            with pytest.raises(EvidenceWikiError) as failure:
                operation_control.checkpoint("blocked_public_call")
            envelope = {"status": "refused", "result": None, "error": host.error_document(failure.value)}
            return {"result": {"content": [{"text": json.dumps(envelope)}]}}

    route.server = Server()
    try:
        with pytest.raises(EvidenceWikiError) as failure:
            route.call(operation, {"run_id": "finite-run"}, borrowed)
        expected = "OPERATION_DEADLINE_EXCEEDED" if stopped == "expired" else "OPERATION_CANCELLED"
        assert failure.value.error_code == expected
        assert observed == ["research_" + operation.replace("-", "_")]
        assert checkpoints == ["reference_route_before_call", "blocked_public_call", "reference_route_after_call"]
        assert route.calls[0]["state"] == "refused"
    finally:
        borrowed.close()


def test_mcp_borrowed_control_remains_usable_when_public_handler_raises(monkeypatch, tmp_path):
    from datetime import datetime, timedelta, timezone

    from evidence_wiki import OperationControl

    route = object.__new__(host.Routes)
    route.root, route.route, route.calls = tmp_path, "scoped_mcp", []
    observed, checkpoints = [], []
    borrowed = OperationControl(deadline_utc=(datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(),
                                timeout_seconds=60)
    original_checkpoint = borrowed.checkpoint

    def checkpoint(name):
        checkpoints.append(name)
        original_checkpoint(name)

    monkeypatch.setattr(borrowed, "checkpoint", checkpoint)
    monkeypatch.setattr(borrowed, "close", lambda: pytest.fail("route closed the borrowed control"))

    class Server:
        def handle_message(self, message, *, operation_control):
            observed.append(operation_control)
            raise RuntimeError("handler stopped")

    route.server = Server()
    try:
        with pytest.raises(RuntimeError, match="handler stopped"):
            route.call("brief", {"run_id": "finite-run"}, borrowed)
        assert observed == [borrowed]
        assert checkpoints == ["reference_route_before_call"]
        borrowed.checkpoint("host_reuses_control_after_handler_exception")
        assert route.calls[0]["state"] == "started"
    finally:
        OperationControl.close(borrowed)


def owner_fixture(monkeypatch, tmp_path, route="direct"):
    """Exercise adapter control flow without executing native workloads or keys."""
    from tests._usage_fixture import require_reference_storage
    require_reference_storage()
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
    runtime = SimpleNamespace(context=SimpleNamespace(activate=nullcontext, mutation_control_directory=tmp_path / "control"),
                              policy=None, execution=execution)
    monkeypatch.setattr(owners.HostRuntime, "from_factory", lambda *args: runtime)
    opened = SimpleNamespace(research=SimpleNamespace(session=lambda *args, **kwargs: nullcontext(None)))
    monkeypatch.setattr(owners.Workspace, "open", lambda _: nullcontext(opened))
    proof = {"read_finished_utc": "2030-01-01T12:00:01+00:00", "journal": {"events": [{"data": {"record": {
        "kind": "execution_qualification_probe", "data": {"probe": {"expires_utc": "2030-01-01T12:00:30+00:00"}}}}}]},
        "current": {"accounting": {"accepted_count": 1}, "canonical_release": {"research_complete": True}}, "host_inputs": {}}
    calls = []
    monkeypatch.setattr(owners, "native_replay", lambda *args: (calls.append(args) or proof))
    monkeypatch.setattr(owners, "verify_journal_closing", lambda _: None)
    return SimpleNamespace(plan=plan, report=report, files=files, selection=selection, verified=verified,
                           retain=retain, calls=calls, stdout_path=stdout_path, runtime=runtime, records=records, proof=proof,
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
    assert not data.calls


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
    assert not data.calls


def test_retained_native_authentication_precedes_current_witness(monkeypatch, tmp_path):
    data = owner_fixture(monkeypatch, tmp_path)
    order = []
    data.runtime.execution.records = lambda session: order.append("records") or data.records
    def proof(*args):
        order.append("proof")
        return data.proof
    monkeypatch.setattr(owners, "native_replay", proof)
    data.readers()[0].read()
    assert order == ["records", "records", "records", "proof"]


@pytest.mark.parametrize("mutation", ["history", "parent", "input", "slot"])
def test_invalid_retained_history_never_refreshes_witness(monkeypatch, tmp_path, mutation):
    data = owner_fixture(monkeypatch, tmp_path)
    row = data.report["cycles"][0]
    if mutation == "history":
        row["native_records_sha256"] = "0" * 64
    elif mutation == "parent":
        data.report["cycles"][1]["parent_history_after"] = {"events.jsonl": "changed"}
    elif mutation == "input":
        row["input_sha256"] = "0" * 64
    else:
        row["slot_id"] = "missing"
    data.retain()
    with pytest.raises(ValueError, match="integration_original_(history_changed|parent_changed|input_changed|slot_inventory)"):
        data.readers()[0].read()
    assert not data.calls


@pytest.mark.parametrize("mutation", ["response", "candidate", "host", "parent"])
def test_postproof_change_cannot_return_owner_evidence(monkeypatch, tmp_path, mutation):
    data = owner_fixture(monkeypatch, tmp_path)
    def proof(*args):
        if mutation == "response":
            path = Path(data.report["cycles"][0]["originals"]["brief"]["path"])
            data.files[path] += b" "
        elif mutation == "candidate":
            monkeypatch.setattr(owners, "candidate_manifest", lambda _: {"candidate": "changed"})
        elif mutation == "host":
            monkeypatch.setattr(owners, "host_identity", lambda _: {"host": "changed"})
        else:
            monkeypatch.setattr(owners, "_manifest", lambda _: {"events.jsonl": "changed"})
        return data.proof
    monkeypatch.setattr(owners, "native_replay", proof)
    with pytest.raises(ValueError, match="integration_(original_response_changed|current_binding_changed|original_parent_changed)"):
        data.readers()[0].read()


@pytest.mark.parametrize("mutation", ["source", "authority", "journal"])
def test_native_static_closing_checks_reject_postproof_bytes(monkeypatch, tmp_path, mutation):
    from tools.replay_qualification_native import verify_journal_closing

    data = owner_fixture(monkeypatch, tmp_path)
    workspace, factory = Path(data.report["workspace"]), Path(data.report["factory"])
    source = workspace / "raw/source.json"
    authority = factory.parent / "authority.json"
    journal = workspace / "runs/change_2/events.jsonl"
    state = journal.with_name("run-state.json")
    for path in (source, authority, journal, state):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"{}\n")
        path.chmod(0o600)
    live_paths = {str(journal), str(state)}
    data.proof["host_inputs"] = owners._host_files(workspace, factory.parent,
        data.runtime.context.mutation_control_directory, live_paths)
    data.proof["journal"].update(source_path=str(journal), state_path=str(state),
        source_sha256=sha256(journal.read_bytes()).hexdigest(), state_sha256=sha256(state.read_bytes()).hexdigest())
    def proof(*args):
        {"source": source, "authority": authority, "journal": journal}[mutation].write_bytes(b'{"changed":true}\n')
        return data.proof
    monkeypatch.setattr(owners, "native_replay", proof)
    monkeypatch.setattr(owners, "verify_journal_closing", verify_journal_closing)
    with pytest.raises(ValueError, match="integration_current_binding_changed|integration_original_history_changed"):
        data.readers()[0].read()
    if mutation == "journal":
        from datetime import datetime, timezone

        from tools import _integration_improvement as gate

        originals = {data.plan["hosts"][0]["host_id"]: data.selection}
        pins = owners.evidence_pins(data.plan, originals)
        decision = gate.decide(data.plan, digest(data.plan), pins, digest(pins), data.readers(),
                               datetime(2030, 1, 1, 12, tzinfo=timezone.utc))
        assert decision["outcome"] == "stop"


def test_late_closing_checks_cannot_extend_witness(monkeypatch, tmp_path):
    from tools._integration_improvement import _admit
    from tools._release_decision import _time

    data = owner_fixture(monkeypatch, tmp_path)
    reader = data.readers()[0]
    evidence = reader.read()
    slot = next(slot for slot in data.plan["slots"] if slot["slot_id"] == reader.slot_id)
    selected_host = next(host for host in data.plan["hosts"] if host["host_id"] == slot["host_id"])
    pin = {name: data.selection[name] for name in ("raw_sha256", "source_head_sha256")}
    with pytest.raises(ValueError, match="integration_owner_current"):
        _admit(evidence, pin, slot, selected_host, data.plan, _time(evidence.expires_at))


@pytest.mark.parametrize("unsafe", [False, "private credential /owner/path", "sk_exampleSecret123"])
def test_owner_diagnostic_exports_only_bounded_safe_failure(monkeypatch, unsafe):
    from tools._integration_improvement import OwnerReader

    secret = unsafe or "private credential /owner/path"
    def fail():
        raise EvidenceWikiError("RESEARCH_BUDGET_EXHAUSTED" if not unsafe else secret, secret,
                               details={"reason": "qualification_unknown" if not unsafe else secret})
    result = owners.diagnose_reader(OwnerReader("fixed", "owner", fail), timeout_seconds=1)
    assert result["status"] == "exception"
    assert result["exception_class"] == "EvidenceWikiError"
    assert result["error_code"] == (None if unsafe else "RESEARCH_BUDGET_EXHAUSTED")
    assert result["reason"] == (None if unsafe else "qualification_unknown")
    assert 0 <= result["elapsed_seconds"] < 1
    assert 1 <= len(result["frames"]) <= 12
    assert all(set(frame) == {"module_file", "line", "function"} for frame in result["frames"])
    assert secret not in json.dumps(result)
    assert all("/" not in (frame["module_file"] or "") for frame in result["frames"])


def test_owner_diagnostic_does_not_export_returned_projection():
    from tools._integration_improvement import OwnerReader

    result = owners.diagnose_reader(OwnerReader("fixed", "owner", lambda: {"accepted": True, "private": "secret"}),
                                    timeout_seconds=1)
    assert set(result) == {"status", "elapsed_seconds"}
    assert result["status"] == "read_returned"


def test_owner_diagnostic_catches_actual_host_deadline():
    from tools._integration_improvement import OwnerReader

    def wait():
        time.sleep(3)
    result = owners.diagnose_reader(OwnerReader("fixed", "owner", wait), timeout_seconds=1)
    assert result["status"] == "exception"
    assert result["exception_class"] == "_HostDeadline"
    assert result["error_code"] is None and result["reason"] is None
    assert 0.9 <= result["elapsed_seconds"] < 2
    assert any(frame["function"] == "expired" for frame in result["frames"])


def test_owner_diagnostic_omits_unknown_exception_class():
    from tools._integration_improvement import OwnerReader

    secret = "sk_exampleSecret123"
    unknown = type(secret, (Exception,), {})
    def fail():
        raise unknown(secret)
    result = owners.diagnose_reader(OwnerReader("fixed", "owner", fail), timeout_seconds=1)
    assert result["exception_class"] == "unknown"
    assert secret not in json.dumps(result)


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
    monkeypatch.setattr(host, "OperationControl", lambda **kwargs: SimpleNamespace(close=lambda: None,
        checkpoint=lambda _: None, remaining_seconds=lambda _: 1000, deadline_utc=kwargs["deadline_utc"]))
    monkeypatch.setattr(host, "_load_current_refresh", lambda: lambda *args: True)
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
            self.deadline_utc = kwargs["deadline_utc"]
        def checkpoint(self, name):
            if name == "reference_brief_retry":
                raise TimeoutError("expired")
        def remaining_seconds(self, name):
            return 5
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
            self.deadline_utc = kwargs["deadline_utc"]
        def checkpoint(self, name):
            pass
        def remaining_seconds(self, name):
            return remaining if name == "reference_brief_retry" else 5
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


def test_explicit_current_host_action_precedes_each_brief_and_preserves_parent(monkeypatch, tmp_path):
    error = EvidenceWikiError("RESEARCH_RESOLUTION_REFUSED", "refused",
                             details={"reason": "resolution_current_inputs_changed"})
    data = scenario_fixture(monkeypatch, tmp_path, {}, brief_errors=[error])
    report = host.run_scenario(data.plan, "direct", data.directory)
    assert report["failure"] is None
    assert data.refreshes == ["reference-baseline", "reference-change_1", "reference-change_1", "reference-change_2"]
    assert data.events == [("baseline", operation) for operation in ("run-cycle", "refresh_current", "brief", "report")] + [
        ("change_1", operation) for operation in ("refresh", "refresh_current", "brief", "refresh_current", "brief", "report")] + [
        ("change_2", operation) for operation in ("refresh", "refresh_current", "brief", "report")]
    for index, row in enumerate(report["cycles"]):
        assert set(row["originals"]) == {"result", "brief", "accounting"}
        documents = [json.loads(Path(original["path"]).read_bytes()) for original in row["host_actions"]]
        assert len(documents) == (2 if index == 1 else 1)
        for number, (original, action) in enumerate(zip(row["host_actions"], documents, strict=True), 1):
            path = data.directory / "observations" / (host.CYCLES[index] + "-host-current-" + str(number) + ".json")
            assert original["path"] == str(path)
            assert original["raw_sha256"] == sha256(path.read_bytes()).hexdigest()
            assert path.stat().st_mode & 0o777 == 0o400
            assert action["schema_version"] == "finite-host-current-action/v1"
            assert (action["caller_type"], action["operation"], action["run_id"]) == (
                "trusted_reference_host", "refresh_current", row["run_id"])
            assert action["state"] == "returned" and action["result"] is True and action["failure"] is None
            assert 0 <= action["allowance"]["remaining_seconds"] <= action["allowance"]["reserved_seconds"]
        assert len({document["allowance"]["deadline_utc"] for document in documents}) == 1
        if index:
            assert row["parent_history_before"] == row["parent_history_after"]
            assert "host-current.json" in row["parent_history_after"]
            assert row["parent_history_after"] == host._manifest(data.root / "runs" / row["parent_run_id"])
    assert all(call["operation"] != "refresh_current" for call in report["calls"])


@pytest.mark.parametrize("returned", [False, None, "true"])
def test_unavailable_current_host_action_prevents_independent_brief(monkeypatch, tmp_path, returned):
    data = scenario_fixture(monkeypatch, tmp_path, {})
    monkeypatch.setattr(host, "_load_current_refresh", lambda: lambda *args: returned)
    report = host.run_scenario(data.plan, "direct", data.directory)
    row = report["cycles"][0]
    action = json.loads(Path(row["host_actions"][0]["path"]).read_bytes())
    assert action["state"] == ("returned" if returned is False else "failed")
    assert action["result"] is (False if returned is False else None)
    assert report["failure"]["kind"] == "ValueError"
    assert row["projection"]["integration_success"] is False and row["projection"]["accepted"] is not True
    assert data.calls == [{"cycle": "baseline", "operation": "run-cycle"}]
    assert data.rotations == []


@pytest.mark.parametrize("reason", ["qualification_invalid_or_expired", "current_mandatory_qualification_unknown"])
def test_current_host_refusal_preserves_diagnosis_without_acceptance(monkeypatch, tmp_path, reason):
    data = scenario_fixture(monkeypatch, tmp_path, {})
    def refuse(*args):
        raise EvidenceWikiError("RESEARCH_RESOLUTION_REFUSED", "not exported", details={"reason": reason})
    monkeypatch.setattr(host, "_load_current_refresh", lambda: refuse)
    report = host.run_scenario(data.plan, "direct", data.directory)
    action = json.loads(Path(report["cycles"][0]["host_actions"][0]["path"]).read_bytes())
    assert action["state"] == "refused" and action["result"] is None
    assert action["failure"] == {"kind": "EvidenceWikiError", "error_code": "RESEARCH_RESOLUTION_REFUSED", "reason": reason}
    assert report["cycles"][0]["projection"]["accepted"] is not True
    assert data.calls == [{"cycle": "baseline", "operation": "run-cycle"}]
    assert "not exported" not in json.dumps(report) + json.dumps(action)


@pytest.mark.parametrize("native_error", [False, True])
def test_current_host_action_redacts_unreviewed_alphanumeric_diagnostics(monkeypatch, tmp_path, native_error):
    data = scenario_fixture(monkeypatch, tmp_path, {})
    error_type = type("PrivateClassName", (Exception,), {})
    error = (EvidenceWikiError("PRIVATE_ERROR_CODE", "PrivateMessage", details={"reason": "PrivateReason"})
             if native_error else error_type("PrivateMessage"))
    def fail(*args):
        raise error
    monkeypatch.setattr(host, "_load_current_refresh", lambda: fail)
    report = host.run_scenario(data.plan, "direct", data.directory)
    action = json.loads(Path(report["cycles"][0]["host_actions"][0]["path"]).read_bytes())
    assert action["failure"] == {"kind": "EvidenceWikiError" if native_error else "unknown",
                                 "error_code": None, "reason": None}
    exported = json.dumps(report) + json.dumps(action)
    assert all(value not in exported for value in ("PrivateClassName", "PRIVATE_ERROR_CODE", "PrivateMessage", "PrivateReason"))
    assert report["failure"] == {"kind": "ValueError", "error_code": None}


def test_current_host_action_is_retained_before_post_action_timeout(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(host, "_load_current_refresh", lambda: lambda *args: calls.append(args[-1]) or True)
    class Control:
        deadline_utc = "2030-01-01T00:00:00Z"
        def checkpoint(self, name):
            if name == "reference_host_current_after":
                raise TimeoutError("expired after host return")
        def remaining_seconds(self, name):
            return 5.0
    row = {"run_id": "current-child"}
    with pytest.raises(TimeoutError):
        host._refresh_for_brief(None, tmp_path, row, tmp_path, "change_1", Control())
    action = json.loads(Path(row["host_actions"][0]["path"]).read_bytes())
    assert calls == ["current-child"]
    assert action["state"] == "returned" and action["result"] is True
    assert action["allowance"]["deadline_utc"] == Control.deadline_utc


def test_performed_host_action_cannot_override_abstaining_brief(monkeypatch, tmp_path):
    data = scenario_fixture(monkeypatch, tmp_path, {})
    original_routes = host.Routes
    class Routes(original_routes):
        def call(self, operation, arguments, control):
            value = super().call(operation, arguments, control)
            if operation == "brief":
                return {"verdict": "abstain", "original_outcomes": [],
                        "reasons": ["current_mandatory_qualification_unknown"]}
            return value
    monkeypatch.setattr(host, "Routes", Routes)
    report = host.run_scenario(data.plan, "direct", data.directory)
    assert report["failure"] is None
    assert all(row["projection"]["accepted"] is False and row["projection"]["useful"] is False
               for row in report["cycles"])
    assert data.refreshes == ["reference-" + cycle for cycle in host.CYCLES]


@pytest.mark.parametrize("operation", ["brief", "report"])
def test_independent_read_does_not_perform_current_host_work(monkeypatch, tmp_path, operation):
    run = tmp_path / "runs" / "current-child"
    run.mkdir(parents=True)
    (run / "events.jsonl").write_text("retained current witness\n")
    before = host._manifest(run)
    session = SimpleNamespace(**{operation: lambda: {"read": operation}})
    opened = SimpleNamespace(research=SimpleNamespace(session=lambda *a, **kw: nullcontext(session)))
    monkeypatch.setattr(host.Workspace, "open", lambda _: nullcontext(opened))
    def forbidden():
        pytest.fail("read called the trusted host action")
    monkeypatch.setattr(host, "_load_current_refresh", forbidden)
    route = object.__new__(host.Routes)
    route.root, route.route, route.calls, route.server = tmp_path, "direct", [], None
    route.runtime = SimpleNamespace(context=SimpleNamespace(activate=nullcontext), policy=object(), execution=object())
    assert route.call(operation, {"run_id": "current-child"}) == {"read": operation}
    assert host._manifest(run) == before


@pytest.mark.parametrize("existing", [False, True])
def test_trusted_current_action_only_probes_exact_existing_run(monkeypatch, tmp_path, existing):
    from evidence_wiki import calibration_probes

    host._load_example()
    refresh = host._load_current_refresh()
    parent, child = tmp_path / "runs" / "parent", tmp_path / "runs" / "current-child"
    parent.mkdir(parents=True)
    child.mkdir()
    (parent / "events.jsonl").write_text("retained parent\n")
    (child / "events.jsonl").write_text("retained child\n")
    before = host._manifest(parent)
    selected, probes = [], []
    session = SimpleNamespace(run_id="current-child")
    def open_session(*args, **kwargs):
        selected.append(kwargs["run_id"])
        return nullcontext(session)
    opened = SimpleNamespace(research=SimpleNamespace(session=open_session))
    monkeypatch.setattr(host.Workspace, "open", lambda _: nullcontext(opened))
    def probe(selected_session, request_id):
        probes.append((selected_session.run_id, request_id))
        (child / "events.jsonl").write_text("retained child\ncurrent witness\n")
    monkeypatch.setattr(calibration_probes, "refresh", probe)
    runtime = SimpleNamespace(context=SimpleNamespace(activate=nullcontext), policy=object(),
                              execution=SimpleNamespace(records=lambda _: ["existing"] if existing else []))
    assert refresh(runtime, tmp_path, "current-child") is existing
    assert selected == ["current-child"]
    assert [run_id for run_id, _ in probes] == (["current-child"] if existing else [])
    assert host._manifest(parent) == before
    assert (child / "events.jsonl").read_text() == ("retained child\ncurrent witness\n" if existing else "retained child\n")
