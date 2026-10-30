"""Typed inert advice, canonical decision replay and finite unavailable outcomes."""

from __future__ import annotations

import contextlib
import io
import json
from datetime import datetime, timezone

import pytest

from evidence_wiki import Workspace
from evidence_wiki._filesystem import os as native_os
from evidence_wiki._pack_io import canonical
from evidence_wiki.host_context import HostContext
from evidence_wiki.mutation_contracts import digest
from evidence_wiki.planning import compile_plan
from evidence_wiki.research_session import ResearchSession, current, next_work
from evidence_wiki.research_session_contracts import (
    BINDING,
    CURRENT,
    NEXT,
    STEP,
    checked,
    decode,
    encode,
    schema_document,
)
from evidence_wiki.run_policy import RunPolicy
from evidence_wiki.setup_worker import execute
from evidence_wiki.workflow_contracts import WorkflowContractError
from tests._assessment_fixture import QUESTION, SOURCE_ID
from tests.test_caller_research import freeze_reference_request
from tests.test_caller_research import workspace as caller_workspace
from tests.test_research_planning import question_plan, request
from tests.test_run_policy import policy_document
from tests.test_strict_evidence import host as strict_host

GRANTS = {"workspace.open", "workspace_status.run_status_report", "source_requests.run_list_requests",
    "strict_evidence.run_operation.check", "strict_evidence.run_operation.export", "strict_evidence.run_operation.prepare-review",
    "research_session.current", "research_session.next_work", "research_session.session",
    "research_session.advance", "research_session.run_until_terminal", "research_session.host_job",
    "research_session.host_job.generate", "research_session.host_job.evaluate", "research_session.host_job.acquire",
    "research_session.pending", "research_session.reconcile_pending", "research_session.cancel_pending",
    "run_controller.run_session_start", "run_controller.run_session_record", "run_controller.run_session_transition",
    "run_controller.run_session_finish"}


@pytest.fixture
def root(tmp_path):
    return caller_workspace.__wrapped__(tmp_path)


@pytest.fixture
def evidence(tmp_path, monkeypatch, request):
    value = strict_host.__wrapped__(tmp_path, monkeypatch, request)
    freeze_reference_request(value)
    return value


def policy(*, paid=False):
    value = policy_document()
    value["scope"]["question_ids"] = ["q1"]
    value["scope"]["source_scope"] = ["source-concrete"]
    if paid:
        value["budgets"].update(model_calls=2, tokens=1000, cost_limit="1")
    return RunPolicy(value)


def host(root, tmp_path, *, grants=GRANTS, **options):
    control = tmp_path / "control"
    if not control.exists():
        native_os.mkdir(control, mode=0o700)
    return HostContext([root], grants, "caller", [root], mutation_control_directory=control,
        controller_refs={"host_worker": "worker", "evaluator": "independent"}, **options)


def files(root):
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def prepared_workspace(tmp_path, originals):
    value = request(tmp_path)
    value["request"]["payload"]["questions"] = originals
    value["decisions"]["question_plans"] = [question_plan(row["id"]) for row in originals]
    plan = compile_plan(canonical(value))
    with contextlib.redirect_stdout(io.StringIO()):
        for operation in ("initialize", "intake", "coverage", "inventory"):
            execute(operation, plan, datetime.now(timezone.utc).isoformat())
    return tmp_path / "workspace"


def test_current_and_next_are_inert_detached_and_preserve_unknown_measurements(root, tmp_path):
    with host(root, tmp_path).activate():
        before = files(root)
        view = current(root, policy())
        advice = next_work(root, policy())
        assert view["accounting"]["original_ids"] == ["q1"]
        assert view["usage"]["tokens"] is None and view["qualification"]["state"] == "unknown"
        assert not view["authorized"] and not view["evidence_accepted"]
        assert not advice["authorized"] and not advice["evidence_accepted"]
        assert advice["decision"]["outcome"] == "stop_budget" and advice["decision"]["reason"] == "usage_unknown"
        assert all(not action["authorized"] for action in advice["actions"])
        assert decode(CURRENT, encode(CURRENT, view)) == view
        assert decode(NEXT, encode(NEXT, advice)) == advice
        assert files(root) == before


def test_bounded_advance_uses_canonical_terminal_run_and_durable_decision(root, tmp_path):
    with host(root, tmp_path).activate():
        session = ResearchSession(root, policy(), run_id="session")
        basis = current(root, policy())["basis_id"]
        result = session.run_until_terminal(request_id="step", expected_basis_id=basis, max_steps=2)
        assert result["schema_version"] == STEP and result["terminal"]
        assert result["outcome"] == "stop_budget" and result["canonical_run_state"] == "no_ship"
        assert result["accounting"]["original_outcomes"][0]["original_id"] == "q1"
        assert not result["evidence_accepted"]
        state = json.loads((root / "runs/session/run-state.json").read_text())
        assert state["state"]["current"] == "no_ship" and state["research_session"] == session.binding
        events = [json.loads(line) for line in (root / "runs/session/events.jsonl").read_text().splitlines()]
        decisions = [row for row in events if row["event_type"] == "custom.research.decision"]
        assert len(decisions) == 1 and decisions[0]["data"]["request_id"] == "step"
        replay = session.advance(request_id="step", expected_basis_id=basis)
        assert replay["historical"] and replay["event_ref"] == result["event_ref"]
        assert replay["canonical_run_state"] == "no_ship"
        assert len((root / "runs/session/events.jsonl").read_text().splitlines()) == len(events)
        compact = session.current()["run"]
        assert compact["state"] == "no_ship" and compact["terminal"]
        assert compact["session_binding_id"] == digest(session.binding)
        assert "caller_context" not in compact and "workspace_baseline" not in compact


def test_session_before_start_returns_pre_start_current_and_next(root, tmp_path):
    with host(root, tmp_path).activate():
        session = ResearchSession(root, policy(), run_id="session")
        assert session.current()["run_id"] is None and session.current()["run"] is None
        assert session.next()["current"]["run_id"] is None
        assert not (root / "runs/session").exists()


@pytest.mark.parametrize("grant", ["strict_evidence.run_operation.check", "strict_evidence.run_operation.export"])
@pytest.mark.parametrize("operation", ["current", "next_work", "session"])
def test_missing_strict_read_grant_refuses_before_any_owner_observation(root, tmp_path, monkeypatch, grant, operation):
    import evidence_wiki.research_session as module
    calls = []
    def forbidden_observation(*args, **kwargs):
        calls.append(args)
        raise AssertionError("Owner read preceded grant preflight")
    monkeypatch.setattr(module, "ResearchObservation", forbidden_observation)
    with host(root, tmp_path, grants=GRANTS - {grant}).activate():
        arguments = {"run_id": "session"} if operation == "session" else {}
        with pytest.raises(Exception) as error:
            getattr(module, operation)(root, policy(), **arguments)
        assert error.value.error_code == "HOST_CONTEXT_REFUSED"
        assert calls == []
        assert not (root / "runs/session").exists()


@pytest.mark.parametrize("operation", ["current", "next_work", "session"])
def test_direct_public_calls_qualify_paths_before_owner_observation(root, tmp_path, monkeypatch, operation):
    import evidence_wiki.research_session as module
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "sources").rename(root / "previous-sources")
    (root / "sources").symlink_to(outside, target_is_directory=True)
    calls = []
    def forbidden_observation(*args, **kwargs):
        calls.append(args)
        raise AssertionError("Owner read preceded path preflight")
    monkeypatch.setattr(module, "ResearchObservation", forbidden_observation)
    with host(root, tmp_path).activate():
        arguments = {"run_id": "session"} if operation == "session" else {}
        with pytest.raises(Exception) as error:
            getattr(module, operation)(root, policy(), **arguments)
        assert error.value.error_code == "HOST_CONTEXT_REFUSED"
        assert calls == []


def test_missing_downstream_grant_refuses_before_first_run_effect(root, tmp_path):
    selected = host(root, tmp_path, grants=GRANTS - {"run_controller.run_session_finish"})
    with selected.activate():
        session = ResearchSession(root, policy(), run_id="session")
        basis = current(root, policy())["basis_id"]
        with pytest.raises(Exception) as error:
            session.advance(request_id="step", expected_basis_id=basis)
        assert error.value.error_code == "HOST_CONTEXT_REFUSED"
        assert not (root / "runs/session").exists()
        advice = next_work(root, policy())
        assert "run_controller.run_session_start" in advice["actions"][0]["required_grants"]
        assert advice["decision"]["outcome"] == "blocked_external"


def test_public_fields_are_readonly_and_binding_views_are_detached(root, tmp_path):
    with host(root, tmp_path).activate():
        session = ResearchSession(root, policy(), run_id="session")
        for field, value in (("root", root.parent), ("policy", policy(paid=True)), ("run_id", "other"), ("context", None)):
            with pytest.raises(AttributeError):
                setattr(session, field, value)
        altered = session.binding
        altered["policy"]["scope"]["question_ids"].clear()
        assert session.binding["original_ids"] == ["q1"]
        assert session.binding["policy"]["scope"]["question_ids"] == ["q1"]
        assert decode(BINDING, encode(BINDING, session.binding)) == session.binding
        session.close()
        with pytest.raises(WorkflowContractError):
            session.current()


def test_namespace_session_refuses_after_workspace_closes(root, tmp_path):
    with host(root, tmp_path).activate():
        workspace = Workspace.open(root)
        session = workspace.research.session(policy(), run_id="session")
        workspace.close()
        with pytest.raises(Exception) as error:
            session.current()
        assert error.value.error_code == "WORKSPACE_UNREADABLE"


@pytest.mark.parametrize("failure", ["loader", "constructor"])
def test_namespace_session_contains_loader_and_constructor_process_exit(root, tmp_path, monkeypatch, failure):
    import builtins

    import evidence_wiki.research_session as module
    original_import = builtins.__import__
    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if globals and globals.get("__name__") == "evidence_wiki._facades.research" and "research_session" in fromlist:
            raise SystemExit(9)
        return original_import(name, globals, locals, fromlist, level)
    def refused_constructor(*args, **kwargs):
        raise SystemExit(9)
    with host(root, tmp_path).activate(), Workspace.open(root) as workspace:
        if failure == "loader":
            monkeypatch.setattr(builtins, "__import__", guarded_import)
        else:
            monkeypatch.setattr(module, "ResearchSession", refused_constructor)
        with pytest.raises(Exception) as error:
            workspace.research.session(policy(), run_id="session")
        assert error.value.error_code == "LIBRARY_PROCESS_EXIT"
        assert error.value.details["owner_exit_status"] == 9
        assert not (root / "runs/session").exists()


@pytest.mark.parametrize("operation", ["current", "next", "advance", "run_until_terminal", "host_job"])
def test_session_methods_contain_direct_owner_process_exit(root, tmp_path, monkeypatch, operation):
    import evidence_wiki.research_session as module
    def refused_observation(*args, **kwargs):
        raise SystemExit(7)
    with host(root, tmp_path).activate():
        selected = ResearchSession(root, policy(paid=True), run_id="session")
        basis = selected.current()["basis_id"]
        monkeypatch.setattr(module, "ResearchObservation", refused_observation)
        arguments = {"request_id": "request", "expected_basis_id": basis} if operation in {"advance", "run_until_terminal"} else {}
        if operation == "host_job":
            arguments = {"request_id": "request", "operation": "generate", "payload": {"question": "What does the evidence show?"}}
        with pytest.raises(Exception) as error:
            getattr(selected, operation)(**arguments)
        assert error.value.error_code == "LIBRARY_PROCESS_EXIT"
        assert error.value.details["owner_exit_status"] == 7
        assert not (root / "runs/session").exists()


def test_session_boundary_preserves_programming_exception(root, tmp_path, monkeypatch):
    import evidence_wiki.research_session as module
    expected = TypeError("programming failure")
    def broken_observation(*args, **kwargs):
        raise expected
    with host(root, tmp_path).activate():
        selected = ResearchSession(root, policy(), run_id="session")
        monkeypatch.setattr(module, "ResearchObservation", broken_observation)
        with pytest.raises(TypeError) as error:
            selected.current()
        assert error.value is expected


def test_context_switch_policy_change_and_stale_basis_refuse(root, tmp_path):
    selected = host(root, tmp_path)
    with selected.activate():
        session = ResearchSession(root, policy(), run_id="session")
        with pytest.raises(WorkflowContractError):
            session.advance(request_id="step", expected_basis_id="sha256:" + "1" * 64)
        assert not (root / "runs/session").exists()
        session._policy = policy(paid=True)
        with pytest.raises(WorkflowContractError):
            session.current()
    with host(root, tmp_path).activate(), pytest.raises(WorkflowContractError):
        session.current()


def test_decision_before_terminal_failure_replays_without_duplicate_decision(root, tmp_path, monkeypatch):
    import evidence_wiki.research_session as module
    owner = module._owner
    def interrupted(project, seam, **kwargs):
        if seam == "run_session_transition":
            raise KeyboardInterrupt
        return owner(project, seam, **kwargs)
    with host(root, tmp_path).activate():
        session = ResearchSession(root, policy(), run_id="session")
        basis = current(root, policy())["basis_id"]
        monkeypatch.setattr(module, "_owner", interrupted)
        with pytest.raises(KeyboardInterrupt):
            session.advance(request_id="step", expected_basis_id=basis)
        monkeypatch.setattr(module, "_owner", owner)
        result = session.advance(request_id="step", expected_basis_id=basis)
        assert result["historical"] and result["canonical_run_state"] == "no_ship"
        events = [json.loads(line) for line in (root / "runs/session/events.jsonl").read_text().splitlines()]
        assert sum(event["event_type"] == "custom.research.decision" for event in events) == 1


def test_host_job_is_stored_inert_with_immutable_original_lease_and_finite_reservation(root, tmp_path):
    with host(root, tmp_path).activate():
        session = ResearchSession(root, policy(paid=True), run_id="session")
        question = session.current()["accounting"]["original_outcomes"][0]["original_text"]
        job = session.host_job(request_id="job", operation="generate", payload={"question": question})
        replay = session.host_job(request_id="job", operation="generate", payload={"question": question},
                                  original_text=question)
        assert replay == job and job["qualifications"]["status"] == "unknown"
        assert job["max_attempts"] == 1 and job["timeout_seconds"] <= 600
        assert job["original_text"] == question == "What does the evidence show?"
        assert job["scope"] == ["q1"] and job["payload"] == {"question": question}
        with pytest.raises(WorkflowContractError):
            session.host_job(request_id="second", operation="generate", payload={})
        with pytest.raises(WorkflowContractError):
            session.host_job(request_id="job", operation="generate", payload={"changed": True})


@pytest.mark.parametrize("payload,text", [
    ({"question": "Changed original"}, None),
    ({"question": "What does the evidence show?"}, "Exact original."),
    ({"question_ids": ["unknown"]}, None),
    ({"question_ids": ["q1", "q1"]}, None),
    ({"question": "What does the evidence show?", "original_questions": []}, None),
])
def test_generation_refuses_changed_originals_and_out_of_contract_scope_before_effects(root, tmp_path, payload, text):
    with host(root, tmp_path).activate():
        session = ResearchSession(root, policy(paid=True), run_id="session")
        before = files(root)
        with pytest.raises(WorkflowContractError):
            session.host_job(request_id="job", operation="generate", payload=payload, original_text=text)
        assert files(root) == before
        assert not (root / "runs/session").exists()


def test_multi_original_generation_refuses_instead_of_selecting_or_combining_text(tmp_path):
    root = prepared_workspace(tmp_path, [{"id": "q1", "text": "What does the evidence show?"},
                                         {"id": "q2", "text": "A distinct original?"}])
    frozen = policy(paid=True).to_dict()
    frozen["policy_id"] = None
    frozen["scope"]["question_ids"] = ["q1", "q2"]
    with host(root, tmp_path).activate():
        session = ResearchSession(root, RunPolicy(frozen), run_id="session")
        with pytest.raises(WorkflowContractError) as error:
            session.host_job(request_id="job", operation="generate", payload={"question": "What does the evidence show?"})
        assert error.value.error_code == "RESEARCH_ORIGINAL_SCOPE_UNSUPPORTED"
        assert not (root / "runs/session").exists()


def test_generation_preserves_exact_unicode_spacing_and_newlines(tmp_path):
    exact = "  ¿Qué ocurre?\r\n第二行  "
    root = prepared_workspace(tmp_path, [{"id": "q1", "text": exact}])
    with host(root, tmp_path).activate():
        session = ResearchSession(root, policy(paid=True), run_id="session")
        job = session.host_job(request_id="job", operation="generate", payload={"question": exact})
        assert job["original_text"] == job["payload"]["question"] == exact


def test_job_closing_revision_check_refuses_drift_before_run_effect(root, tmp_path, monkeypatch):
    import evidence_wiki.research_session as module
    original = module._job_original
    def drift(*args):
        result = original(*args)
        (root / "log.md").write_text((root / "log.md").read_text() + "\nChanged during preparation.\n")
        return result
    with host(root, tmp_path).activate():
        session = ResearchSession(root, policy(paid=True), run_id="session")
        monkeypatch.setattr(module, "_job_original", drift)
        with pytest.raises(WorkflowContractError) as error:
            session.host_job(request_id="job", operation="generate", payload={"question": "What does the evidence show?"})
        assert error.value.error_code == "RESEARCH_BASIS_CHANGED"
        assert not (root / "runs/session").exists()


def test_evaluation_job_binds_canonical_packet_original_and_complete_source_scope(evidence, tmp_path):
    value = policy(paid=True).to_dict()
    value["policy_id"] = None
    value["scope"] = {"question_ids": [QUESTION], "source_scope": [SOURCE_ID]}
    frozen = RunPolicy(value)
    with host(evidence.root, tmp_path, authority_file=evidence.policy_path, state_directory=evidence.host).activate():
        with Workspace.open(evidence.root) as workspace:
            prepared = workspace.strict.prepare_review("product-source")
        session = ResearchSession(evidence.root, frozen, run_id="session")
        payload = {"claim_id": "product-source", "packet_id": digest(prepared["snapshot"])}
        before = files(evidence.root)
        with pytest.raises(WorkflowContractError) as error:
            session.host_job(request_id="invalid", operation="evaluate", payload={**payload, "packet_id": "wrong"})
        assert error.value.error_code == "RESEARCH_PACKET_CHANGED"
        assert files(evidence.root) == before
        job = session.host_job(request_id="evaluate", operation="evaluate", payload=payload)
        assert job["scope"] == [QUESTION] and job["payload"] == payload
        assert job["original_text"] == session.current()["accounting"]["original_outcomes"][0]["original_text"]
        assert job["qualifications"]["status"] == "unknown"
        assert session.host_job(request_id="evaluate", operation="evaluate", payload=payload) == job


def test_evaluation_refuses_source_closure_outside_frozen_scope_before_effects(evidence, tmp_path):
    value = policy(paid=True).to_dict()
    value["policy_id"] = None
    value["scope"]["question_ids"] = [QUESTION]
    with host(evidence.root, tmp_path, authority_file=evidence.policy_path, state_directory=evidence.host).activate():
        with Workspace.open(evidence.root) as workspace:
            prepared = workspace.strict.prepare_review("product-source")
        session = ResearchSession(evidence.root, RunPolicy(value), run_id="session")
        before = files(evidence.root)
        with pytest.raises(WorkflowContractError) as error:
            session.host_job(request_id="evaluate", operation="evaluate", payload={"claim_id": "product-source", "packet_id": digest(prepared["snapshot"])})
        assert error.value.error_code == "RESEARCH_SOURCE_SCOPE_UNSUPPORTED"
        assert files(evidence.root) == before


def test_acquisition_prefix_is_not_a_concrete_source_reference(root, tmp_path):
    value = policy_document()
    value["scope"] = {"question_ids": ["q1"], "source_scope": ["https://example.org/"]}
    value["budgets"].update(model_calls=1, tokens=100, cost_limit="1")
    with host(root, tmp_path).activate():
        session = ResearchSession(root, RunPolicy(value), run_id="session")
        with pytest.raises(WorkflowContractError) as error:
            session.host_job(request_id="acquire", operation="acquire", payload={"url": "https://example.org/a"})
        assert error.value.error_code == "RESEARCH_SOURCE_MAPPING_UNSUPPORTED"
        assert not (root / "runs/session").exists()


@pytest.mark.parametrize("kind", ["unknown", "authority", "nan", "cycle", "version"])
def test_session_envelopes_refuse_hostile_changes(root, tmp_path, kind):
    with host(root, tmp_path).activate():
        value = current(root, policy())
    if kind == "unknown":
        value["unexpected"] = True
    elif kind == "authority":
        value["authorized"] = True
    elif kind == "nan":
        value["usage"]["seconds"] = float("nan")
    elif kind == "cycle":
        value["accounting"]["cycle"] = value
    else:
        value["schema_version"] = "future/v9"
    with pytest.raises(WorkflowContractError):
        checked(CURRENT, value)
    shape = schema_document(CURRENT)
    shape["properties"].clear()
    assert schema_document(CURRENT)["properties"]


@pytest.mark.parametrize("name", [None, False, 3, [], {}, "future/v9", "x" * 1000],
                         ids=["null", "bool", "number", "list", "object", "future", "oversized"])
def test_session_schema_lookup_refuses_unknown_types_and_versions(name):
    with pytest.raises(WorkflowContractError) as error:
        schema_document(name)
    assert error.value.error_code == "RESEARCH_VERSION_UNSUPPORTED"
