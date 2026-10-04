"""Separate process custody and retained effects, without semantic model claims."""

import multiprocessing
import os
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from evidence_wiki import ResearchExecution, Workspace
from evidence_wiki import execution_session as native
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.operation_control import OperationControl
from evidence_wiki.pack_discovery import owner
from tests import test_execution_session as execution_cases


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    return execution_cases.fixture.__wrapped__(tmp_path, monkeypatch)


def hard_exit_after_owner(root, policy, execution, context, run_id, identifier, arguments):
    """Exit the actual child only after the canonical owner returned a receipt."""
    append = native.append
    def stop(bound, kind, request_id, data):
        if kind == "execution_native_observation" and data["observation"]["attempt_id"] == identifier:
            os._exit(73)
        return append(bound, kind, request_id, data)
    native.append = stop
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id=run_id, execution=execution) as session:
            native._mutation(session, identifier, "coverage.link", arguments)


def test_actual_child_hard_exit_retains_exact_receipt_without_second_effect(fixture, monkeypatch):
    root, policy, execution, context = fixture
    if not hasattr(os, "fork"):
        pytest.skip("Reference process handoff requires native fork")
    run_id, identifier = "hard-exit-recovery", "durable-effect"
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id=run_id, execution=execution) as session:
            with native.gate(session):
                native._begin(session)
            before = session.current()
            source_id = before["execution"]["source_bindings"][0]["source_id"]
            arguments = {"slug":"q", "facet_id":"primary", "accepted_source_ids":[source_id],
                "blocking_request_ids":None, "clear_accepted_source_ids":False, "clear_blocking_request_ids":False}
            original_deadline = native.deadline(session)
    process = multiprocessing.get_context("fork").Process(target=hard_exit_after_owner,
        args=(root, policy, execution, context, run_id, identifier, arguments))
    process.start()
    process.join(timeout=60)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        pytest.fail("Reference process did not reach its bounded hard exit")
    assert process.exitcode == 73
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id=run_id, execution=execution) as session:
            stored = workspace.mutations.inspect(identifier)["record"]["receipt"]
            assert stored["status"] == "committed"
            from evidence_wiki._facades.mutations import MutationsNamespace
            monkeypatch.setattr(MutationsNamespace, "submit", lambda *_a, **_k: pytest.fail("Recovery submitted another effect"))
            native._recover_history(session)
            native._recover_history(session)
            values = [row["data"] for row in execution.records(session) if row["kind"] == "execution_native_observation"]
            assert len(values) == 1 and values[0]["result"] == stored
            assert native.deadline(session) == original_deadline


def test_actual_foreign_holder_is_not_reclaimed_by_postcondition_recovery(fixture):
    root, policy, execution, context = fixture
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="foreign-holder", execution=execution) as session:
            with native.gate(session):
                native._begin(session)
    other = replace(context, actor_ref="other-owner")
    with other.activate(), Workspace.open(root) as workspace:
        workspace.questions.claim(slug="q", agent_id="other-owner")
    before = owner("_evidence_revision").capture_workspace(root).revision_id
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="foreign-holder", execution=execution) as session:
            control = OperationControl(deadline_utc=(datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat(), timeout_seconds=5)
            with pytest.raises(EvidenceWikiError):
                native._recover_question(session, "question_claim.run_claim", {"slug":"q", "agent_id":context.actor_ref}, control)
    assert owner("_evidence_revision").capture_workspace(root).revision_id == before


@pytest.mark.parametrize("change", ["policy", "control"])
def test_changed_policy_or_private_control_cannot_rebind_existing_run(fixture, tmp_path, change):
    root, policy, execution, context = fixture
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="immutable-binding", execution=execution) as session:
            with native.gate(session):
                native._begin(session)
    if change == "policy":
        from evidence_wiki import RunPolicy
        changed = policy.to_dict()
        changed["policy_id"] = None
        changed["budgets"]["tool_calls"] += 1
        policy = RunPolicy(changed)
    else:
        new_control = tmp_path / "other-control"
        new_control.mkdir(mode=0o700)
        context = replace(context, mutation_control_directory=new_control)
    before = owner("_evidence_revision").capture_workspace(root).revision_id
    with context.activate(), Workspace.open(root) as workspace, pytest.raises(EvidenceWikiError):
        with workspace.research.session(policy, run_id="immutable-binding", execution=execution) as session:
            session.current()
    assert owner("_evidence_revision").capture_workspace(root).revision_id == before


def child_step(root, policy, execution, context, run_id, queue, *, hard_exit=False):
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id=run_id, execution=execution) as session:
            observed = session.recovery_status()
            result = session.advance(request_id="child-next", expected_basis_id=observed["current_basis_id"])
            if hard_exit:
                os._exit(73)
            queue.put({"pid": os.getpid(), "status": observed, "step": result,
                "records": execution.records(session)})


def test_second_process_preserves_run_identity_and_consumed_allowance(fixture):
    root, policy, execution, original_context = fixture
    from dataclasses import replace
    context = replace(original_context, grants=original_context.grants | {"research_session.recovery_status"})
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="resume-reference", execution=execution) as session:
            before = session.current()
            first = session.advance(request_id="parent-prepare", expected_basis_id=before["basis_id"])
            assert not first["terminal"]
            original = session.recovery_status()
    process_context = multiprocessing.get_context("fork")
    queue = process_context.Queue()
    worker = process_context.Process(target=child_step,
        args=(root, policy, execution, context, "resume-reference", queue))
    worker.start()
    value = queue.get(timeout=60)
    worker.join(timeout=10)
    assert worker.exitcode == 0 and value["pid"] != os.getpid()
    assert value["status"]["binding_id"] == original["binding_id"]
    assert value["status"]["original_deadline_utc"] == original["original_deadline_utc"]
    assert value["status"]["steps_used"] == original["steps_used"]
    assert value["status"]["questions"] == original["questions"]
    assert len({row["request_id"] for row in value["records"]}) == len(value["records"])


def test_terminal_recovery_is_observation_only(fixture, monkeypatch):
    root, policy, execution, original_context = fixture
    from dataclasses import replace
    context = replace(original_context, grants=original_context.grants | {
        "research_session.recovery_status", "research_session.reconcile_recovery"})
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="terminal-recovery", execution=execution) as session:
            with native.gate(session):
                native._begin(session)
                session._finish(native._document(session), "Reference refusal")
            before = execution.records(session)
            monkeypatch.setattr(execution.executor, "invoke", lambda *_: pytest.fail("Terminal recovery dispatched"))
            monkeypatch.setattr(native, "_recover_history", lambda *_: pytest.fail("Terminal recovery mutated history"))
            value = session.reconcile_recovery(request_id="historical", expected_basis_id=session.current()["basis_id"])
            assert value["terminal"] and value["next_work"] is None
            assert execution.records(session) == before
            assert owner("run_controller").load_run_state(root, session.run_id)["state"]["current"] in owner("run_controller").TERMINAL_STATES


def test_unknown_native_history_can_observe_a_later_exact_receipt(fixture, monkeypatch):
    """A real owner receipt survives a lost observation and a temporary read gap."""
    root, policy, execution, context = fixture
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="delayed-receipt", execution=execution) as session:
            with native.gate(session):
                native._begin(session)
            identifier = "late-native"
            source_id = session.current()["execution"]["source_bindings"][0]["source_id"]
            arguments = {"slug": "q", "facet_id": "primary", "accepted_source_ids": [source_id],
                "blocking_request_ids": None, "clear_accepted_source_ids": False, "clear_blocking_request_ids": False}
            original_append = native.append
            def lose_observation(bound, kind, request_id, data):
                if kind == "execution_native_observation" and data["observation"]["attempt_id"] == identifier:
                    raise RuntimeError("Reference interrupted after the actual owner effect")
                return original_append(bound, kind, request_id, data)
            monkeypatch.setattr(native, "append", lose_observation)
            with pytest.raises(RuntimeError):
                native._mutation(session, identifier, "coverage.link", arguments)
            monkeypatch.setattr(native, "append", original_append)
            receipt = workspace.mutations.inspect(identifier)["record"]["receipt"]
            assert receipt["status"] == "committed"
            from evidence_wiki._facades.mutations import MutationsNamespace
            original_inspect = MutationsNamespace.inspect
            monkeypatch.setattr(MutationsNamespace, "inspect", lambda *_args, **_kwargs: {"record": None})
            native._recover_history(session)
            assert any(row["kind"] == "execution_native_history_unknown" for row in execution.records(session))
            monkeypatch.setattr(MutationsNamespace, "inspect", original_inspect)
            monkeypatch.setattr(MutationsNamespace, "submit", lambda *_args, **_kwargs: pytest.fail("Recovery submitted another effect"))
            native._recover_history(session)
            rows = execution.records(session)
            assert len([row for row in rows if row["kind"] == "execution_native_history_unknown"]) == 1
            completions = [row for row in rows if row["kind"] == "execution_native_observation"]
            assert len(completions) == 1 and completions[0]["data"]["result"] == receipt


def test_changed_original_instructions_refuse_recovery_without_journal_change(fixture):
    root, policy, execution, original_context = fixture
    context = replace(original_context, grants=original_context.grants | {
        "research_session.recovery_status", "research_session.reconcile_recovery"})
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="instruction-custody", execution=execution) as session:
            with native.gate(session):
                native._begin(session)
            records = execution.records(session)
            path = root / "skills/research-run.md"
            path.write_bytes(path.read_bytes() + b"\nChanged original execution instruction.\n")
            before = owner("_evidence_revision").capture_workspace(root).revision_id
            with pytest.raises(EvidenceWikiError):
                session.recovery_status()
            with pytest.raises(EvidenceWikiError):
                session.reconcile_recovery(request_id="refused", expected_basis_id=session.current()["basis_id"])
            assert execution.records(session) == records
            assert owner("_evidence_revision").capture_workspace(root).revision_id == before


class RetainedHostProxy:
    """Transport to the existing parent host executor, never a lifecycle store."""
    def __init__(self, profile, connection):
        self.profile, self.connection = profile, connection
        self.lock = threading.Lock()

    def call(self, method, *arguments):
        with self.lock:
            self.connection.send((method, arguments))
            if not self.connection.poll(10):
                raise RuntimeError("Reference host did not answer within its bound")
            value = self.connection.recv()
            if value[0] != "ok":
                raise RuntimeError("Reference host refused")
            return value[1]

    def invoke(self, attempt):
        return self.call("invoke", attempt)

    def poll(self, attempt):
        return self.call("poll", attempt)

    def cancel(self, attempt):
        return self.call("cancel", attempt)

    def authenticate(self, observation, attempt):
        return self.call("authenticate", observation, attempt)


def retain_completed_model_then_exit(root, policy, execution, context, run_id, queue):
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id=run_id, execution=execution) as session:
            with native.gate(session):
                native._begin(session)
                current = native.current(session)
                attempt = native.reserve(session, "retained-generation", {"operation":"generate", "purpose":"generate",
                    "original_id":"q", "config_ref":"researcher"}, current)
            observed, reason = native._external(session, "invoke", attempt)
            assert reason is None
            observed = native.intake(session, observed, request_id="child-return")
            for index in range(32):
                if observed["state"] != "running":
                    break
                observed, reason = native._external(session, "poll", attempt)
                assert reason is None
                observed = native.intake(session, observed, request_id="child-poll-" + str(index))
            assert observed["state"] == "succeeded" and observed["final_accounting"]
            queue.put({"attempt_id":attempt.attempt_id, "pid":os.getpid(), "deadline":native.deadline(session).isoformat()})
            queue.close()
            queue.join_thread()
            os._exit(73)


def test_real_engine_exit_after_completed_model_resumes_without_model_redispatch(fixture, monkeypatch):
    root, policy, host_execution, context = fixture
    if not hasattr(os, "fork"):
        pytest.skip("Reference process handoff requires native fork")
    context = replace(context, grants=context.grants | {"research_session.recovery_status", "research_session.reconcile_recovery"})
    processes = multiprocessing.get_context("fork")
    client, server = processes.Pipe()
    proxy = RetainedHostProxy(host_execution.profile, client)
    execution = ResearchExecution(proxy, qualifications=host_execution.qualifications,
        review_signer=host_execution.review_signer, observer_key=b"o" * 32)
    calls = []
    def serve():
        while True:
            try:
                method, arguments = server.recv()
            except EOFError:
                return
            if method == "stop":
                return
            calls.append((method, arguments[-1].attempt_id if method == "authenticate" else arguments[0].attempt_id))
            try:
                server.send(("ok", getattr(host_execution.executor, method)(*arguments)))
            except Exception:
                server.send(("refused", None))
    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    queue = processes.Queue()
    process = processes.Process(target=retain_completed_model_then_exit,
        args=(root, policy, execution, context, "completed-model", queue))
    process.start()
    try:
        value = queue.get(timeout=60)
        process.join(timeout=10)
        assert process.exitcode == 73 and value["pid"] != os.getpid()
        with context.activate(), Workspace.open(root) as workspace:
            with workspace.research.session(policy, run_id="completed-model", execution=execution) as session:
                before = session.recovery_status()
                assert before["pending"]["state"] == "succeeded" and not before["pending"]["applied"]
                assert before["original_deadline_utc"] == value["deadline"]
                step = session.advance(request_id="resume-application", expected_basis_id=before["current_basis_id"])
                assert not step["terminal"] and step["action"] == "admit"
                assert session.pending()["applied"]
                assert sum(method == "invoke" and attempt_id == value["attempt_id"] for method, attempt_id in calls) == 1
                assert len([row for row in execution.records(session) if row["kind"] == "execution_attempt"]) == 1
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        client.send(("stop", ()))
        worker.join(timeout=5)
        client.close()
        server.close()
