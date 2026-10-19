"""Current owner reads bind each actual session without overlapping holders."""

from contextlib import contextmanager, nullcontext
from types import SimpleNamespace

import evidence_wiki
from evidence_wiki import _review_dependency_inputs as inputs
from evidence_wiki import execution_session as native
from tools import run_review_dependency_workload as workload


def test_selective_current_owner_read_binds_each_actual_session(monkeypatch):
    active = []
    sessions = []
    observed = []

    @contextmanager
    def session_factory(*args, run_id, **kwargs):
        assert not active
        session = SimpleNamespace(run_id=run_id)
        sessions.append(session)
        yield session
        assert not active

    @contextmanager
    def bind(session):
        assert not active
        active.append(session)
        try:
            yield
        finally:
            assert active.pop() is session

    def strict_check():
        assert active == [sessions[0]]
        observed.append(("strict", active[0]))
        return {"claims": []}

    def publication():
        assert active == [sessions[0]]
        observed.append(("publication", active[0]))
        return {"verdict": "unavailable"}

    @contextmanager
    def facts(session, *, operation):
        assert operation == "current"
        assert len(active) == 1 and active[0] is session
        observed.append(("current", session))
        yield

    qualification = {"state": "qualified"}

    def current_view(session):
        assert active[0] is session
        return {"run_id": session.run_id, "qualification": qualification}

    def witness(session):
        assert active[0] is session
        return {"run_id": session.run_id, "observed_utc": "2026-10-16T00:00:00+00:00"}

    workspace = SimpleNamespace(research=SimpleNamespace(session=session_factory),
                                strict=SimpleNamespace(check=strict_check, export=publication))
    monkeypatch.setattr(workload, "Workspace", SimpleNamespace(open=lambda root: nullcontext(workspace)))
    monkeypatch.setattr(workload, "authenticated_journal", lambda *args: ([], {}, {}, None))
    monkeypatch.setattr(inputs, "bind_session", bind)
    monkeypatch.setattr(native, "_current_facts", facts)
    monkeypatch.setattr(native, "_current_view", current_view)
    monkeypatch.setattr(native, "current_qualifications", lambda session: qualification)
    monkeypatch.setattr(evidence_wiki, "host_qualification", witness)
    runtime = SimpleNamespace(context=SimpleNamespace(activate=nullcontext), policy=object(), execution=object())
    result, _, _ = workload._owner_read("unused", runtime, ["first", "last"], [], selective_run_id="last")
    assert [row["run_id"] for row in result["native_current"]] == ["first", "last"]
    assert [session.run_id for session in sessions] == ["last", "first", "last"]
    assert len({id(session) for session in sessions}) == 3
    assert [kind for kind, _ in observed] == ["strict", "publication", "current", "current"]
    assert not active
