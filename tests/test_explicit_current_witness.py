"""Explicit host writes observe current configuration; reads retain expiry."""

import importlib
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from evidence_wiki import calibration_probes, execution_session, research_cycle


@pytest.mark.parametrize("historical,outcome,probed", [
    (False, "accepted", True), (False, "partial", True),
    (True, "accepted", False), (False, "stop_budget", False), (False, "rejected", False),
])
def test_explicit_cycle_closure_observes_configuration_after_finalization(monkeypatch, historical, outcome, probed):
    calls = []
    original = {"original_id": "original", "original_text": "Preserve the exact original.",
        "accepted": outcome == "accepted", "questions": [{"status": "open"}], "missing_question_ids": []}
    view = {"basis_id": "basis", "accounting": {"original_outcomes": [original], "accounting_complete": True},
        "canonical_release": {}, "usage": {}, "execution": {"pending": None}}
    session = SimpleNamespace(root="/workspace", run_id="run", binding={"original_ids": ["original"]},
        execution=SimpleNamespace(profile=SimpleNamespace(calibration=object()), records=lambda _: []))
    step = {"terminal": True, "historical": historical, "outcome": outcome,
        "canonical_run_state": "complete", "reason": "current_outcomes"}
    def finish(**_):
        calls.append("finalized")
        return step
    def current():
        calls.append("current")
        return view
    session.run_until_terminal, session.current = finish, current
    @contextmanager
    def gate(*_):
        yield
    monkeypatch.setattr(execution_session, "gate", gate)
    monkeypatch.setattr(execution_session, "retained", lambda _: [])
    monkeypatch.setattr(execution_session, "current", lambda _: view)
    monkeypatch.setattr(execution_session, "_begin", lambda _: None)
    monkeypatch.setattr(execution_session, "append", lambda *_: None)
    monkeypatch.setattr(calibration_probes, "refresh", lambda *_: calls.append("fresh_witness"))
    monkeypatch.setattr(research_cycle, "checked", lambda result: result)
    from evidence_wiki.research_observation import ResearchObservation
    monkeypatch.setattr(ResearchObservation, "__init__", lambda self, _: setattr(self, "managed", False))
    monkeypatch.setattr(ResearchObservation, "finish", lambda _: None)
    result = research_cycle.drive(session, request_id="drive")
    assert result["historical"] is historical
    assert calls == (["finalized", "fresh_witness", "current"] if probed else ["finalized", "current"])


@pytest.mark.parametrize("retained", [False, True])
def test_trusted_host_startup_refreshes_only_a_preexisting_authenticated_bound_run(monkeypatch, retained):
    example = Path(__file__).resolve().parents[1] / "examples/unattended-host"
    monkeypatch.syspath_prepend(str(example))
    recipe = importlib.import_module("finite_host")
    calls = []
    @contextmanager
    def scope():
        yield
    class Workspace:
        @classmethod
        @contextmanager
        def open(cls, root):
            assert root == "/owned-workspace"
            yield cls()
        @property
        def research(self):
            return self
        @contextmanager
        def session(self, policy, *, run_id, execution):
            assert (policy, run_id, execution) == ("policy", "owned-run", runtime.execution)
            yield "bound-session"
    runtime = SimpleNamespace(context=SimpleNamespace(activate=scope), policy="policy",
        execution=SimpleNamespace(records=lambda _: ["authenticated-record"] if retained else []))
    monkeypatch.setattr(recipe, "Workspace", Workspace)
    monkeypatch.setattr(calibration_probes, "refresh", lambda session, request: calls.append((session, request)))
    assert recipe.refresh_current(runtime, "/owned-workspace", "owned-run") is retained
    assert len(calls) == int(retained)
    if retained:
        assert calls[0][0] == "bound-session" and calls[0][1].startswith("host-current-")
