"""Independent crash, lock, and frozen execution allowance checks."""

from contextlib import contextmanager

import pytest

from evidence_wiki import Workspace, execution_session
from evidence_wiki.errors import EvidenceWikiError
from tests.test_execution_session import fixture as reference_fixture


@pytest.fixture
def execution_fixture(tmp_path, monkeypatch):
    return reference_fixture.__wrapped__(tmp_path, monkeypatch)


class SimulatedInterruption(BaseException):
    """Represent loss of the integrating host between durable boundaries."""


def advance_until(session, operation, *, prefix="advance", limit=32):
    for index in range(limit):
        action = session.next()["actions"][0]["operation"]
        if action == operation:
            return session.current()
        before = session.current()
        result = session.advance(request_id=f"{prefix}-{index}", expected_basis_id=before["basis_id"])
        assert not result["terminal"], (operation, result, session.report())
    pytest.fail("Bounded session did not reach the requested operation.")


def test_host_authentication_never_runs_with_workspace_gate_held(execution_fixture, monkeypatch):
    root, policy, execution, context = execution_fixture
    gate = execution_session.gate
    active = [0]
    locked_calls = []

    @contextmanager
    def tracked_gate(*args, **kwargs):
        with gate(*args, **kwargs):
            active[0] += 1
            try:
                yield
            finally:
                active[0] -= 1

    authenticate = execution.executor.authenticate

    def inspect_lock(observation, attempt):
        if active[0]:
            locked_calls.append(attempt.attempt_id)
        return authenticate(observation, attempt)

    monkeypatch.setattr(execution_session, "gate", tracked_gate)
    monkeypatch.setattr(execution.executor, "authenticate", inspect_lock)
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="lock-check", execution=execution) as session:
            advance_until(session, "generate")
            view = session.current()
            session.advance(request_id="generated", expected_basis_id=view["basis_id"])
    assert locked_calls == []


def test_dispatch_intent_charges_step_allowance_before_external_work(execution_fixture, monkeypatch):
    root, policy, execution, context = execution_fixture
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="intent-check", execution=execution) as session:
            view = advance_until(session, "generate")
            consumed = view["execution"]["steps_used"]

            def lose_host(*_args, **_kwargs):
                raise SimulatedInterruption

            monkeypatch.setattr(execution_session, "_external", lose_host)
            with pytest.raises(SimulatedInterruption):
                session.advance(request_id="interrupted-dispatch", expected_basis_id=view["basis_id"])
            retained = session.current()
            assert retained["execution"]["pending"] is not None
            assert retained["execution"]["steps_used"] == consumed + 1


def test_committed_draft_recovers_after_missing_native_return_record(execution_fixture, monkeypatch):
    root, policy, execution, context = execution_fixture
    append = execution_session.append
    interrupted = [False]

    def lose_return(session, kind, request_id, data):
        result = data.get("result") if type(data) is dict else None
        if (not interrupted[0] and kind == "execution_native_observation" and type(result) is dict
                and result.get("operation") == "strict.admit" and result.get("status") == "committed"):
            interrupted[0] = True
            raise SimulatedInterruption
        return append(session, kind, request_id, data)

    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="admission-recovery", execution=execution) as session:
            view = advance_until(session, "admit")
            monkeypatch.setattr(execution_session, "append", lose_return)
            with pytest.raises(SimulatedInterruption):
                session.advance(request_id="interrupted-admission", expected_basis_id=view["basis_id"])
            monkeypatch.setattr(execution_session, "append", append)
            result = session.advance(request_id="interrupted-admission", expected_basis_id=view["basis_id"])
            assert result["reason"] not in {"usage_unknown", "unsupported_claim"}
            attempts = [row for row in execution.records(session) if row["kind"] == "execution_attempt"
                        and row["data"]["attempt"]["job"]["operation"] == "generate"]
            assert len(attempts) == 1


def test_unfinished_accepted_replay_rechecks_current_qualification(execution_fixture, monkeypatch):
    root, policy, execution, context = execution_fixture
    finish = execution_session._finish
    interrupted = [False]

    def lose_finish(session, decision):
        if decision["outcome"] == "accepted" and not interrupted[0]:
            interrupted[0] = True
            raise SimulatedInterruption
        return finish(session, decision)

    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="completion-recovery", execution=execution) as session:
            monkeypatch.setattr(execution_session, "_finish", lose_finish)
            before = session.current()
            with pytest.raises(SimulatedInterruption):
                session.run_until_terminal(request_id="drive", expected_basis_id=before["basis_id"], max_steps=64)
            saved = next(row["data"]["step"] for row in execution.records(session)
                         if row["kind"] == "execution_step" and row["data"]["step"]["outcome"] == "accepted")
            monkeypatch.setattr(execution_session, "_finish", finish)
            monkeypatch.setattr(execution_session, "_qualifications", lambda *_args, **_kwargs: {
                "state": "unknown", "reason": "qualification_invalid_or_expired", "profiles": []})
            try:
                current_result = session.advance(request_id=saved["request_id"], expected_basis_id=saved["before_basis_id"])
                assert current_result["outcome"] not in {"accepted", "partial"}
                repeated = session.advance(request_id=saved["request_id"], expected_basis_id=saved["before_basis_id"])
                assert repeated["outcome"] == current_result["outcome"]
                assert repeated["historical"] and repeated["canonical_run_state"] != "complete"
            except EvidenceWikiError:
                pass
            assert session.current()["run"]["state"] != "complete"
