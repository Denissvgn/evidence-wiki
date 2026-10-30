"""Controlled owner ordering and real signed-record expiry; no native runtime proof."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from evidence_wiki import resolution_operations as owner
from evidence_wiki.errors import UsageError
from evidence_wiki.mutation_contracts import detached
from tests.test_qualification_calibration import EXPIRY, NOW
from tests.test_readability_profile import profile as profile
from tests.test_readability_qualification import issued as issued
from tests.test_resolution_projection import inputs


@pytest.mark.parametrize("expiry_at,pending", [("render", False), ("native_closing", False), ("manager_exit", False), ("manager_exit", True)])
def test_dedicated_record_expiry_is_checked_after_native_work_and_manager_exit(issued, monkeypatch, tmp_path, expiry_at, pending):
    policy, profile, authority, record, _configuration = issued
    fixture = inputs()
    clock = {"now": NOW, "reads": 0}
    class Session:
        root = tmp_path
        run_id = "controlled"
        binding = {}
        execution = object()
        context = SimpleNamespace(require_grant=lambda *_: None)
        def _check(self, *_a, **_k):
            pass
    session = Session()
    session.policy = policy
    class Usage:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            if expiry_at == "manager_exit":
                clock["now"] = EXPIRY
        def revalidate(self, *_):
            pass
    def native_current(_session):
        clock["reads"] += 1
        if clock["reads"] >= 2 and expiry_at == "native_closing":
            clock["now"] = EXPIRY
        return detached(fixture["current"])
    selected = profile.readability.selection.document()
    def vector(_session, observed):
        observed.require(_session)
        result = authority.current(record, policy=policy, profile=profile, config_ref="readability",
            rubric=selected["rubric"], model_settings=selected["model_settings"], domain_ref=record["binding"]["domains"][0],
            now_utc=clock["now"], drift_ref=selected["drift_ref"], readability=True)
        return {"report": {"state": result["state"]}, "qualification_ref": result["record_ref"]}
    monkeypatch.setattr(owner, "ResearchSession", Session)
    monkeypatch.setattr(owner, "owner", lambda stem: SimpleNamespace(load_config=lambda *_: {}) if stem == "verify_quotes"
        else SimpleNamespace(configured=lambda *_: True, current_view=lambda *_: Usage()))
    monkeypatch.setattr(owner, "_Pass", lambda *_a, **_k: object())
    monkeypatch.setattr(owner, "questions", lambda *_a, **_k: fixture["derived_proofs"])
    monkeypatch.setattr(owner, "blockers", lambda *_: [])
    monkeypatch.setattr(owner.native, "retained", lambda *_: [])
    monkeypatch.setattr(owner.native, "current", native_current)
    @contextmanager
    def current_facts(_session):
        yield SimpleNamespace(view=native_current(_session), records=[], packets=[],
            observed=SimpleNamespace(require=lambda selected: None))
    monkeypatch.setattr(owner.native, "_current_facts", current_facts)
    monkeypatch.setattr(owner, "_journal_proof", lambda *_: {})
    monkeypatch.setattr(owner.native, "policy_observation", lambda *_: fixture["policy_observation"])
    monkeypatch.setattr(owner.native, "execution_budget_overrun", lambda *_: False)
    from evidence_wiki import readability_execution
    monkeypatch.setattr(readability_execution, "closing_vector", vector)
    monkeypatch.setattr(readability_execution, "qualification_vector", lambda session, observed: {
        "state": vector(session, observed)["report"]["state"], "qualification_ref": record["record_ref"], "authority_ref": "fixed-authority"})
    with pytest.raises(UsageError) as caught:
        with owner._held_current(session) as observed:
            if pending:
                observed.bind_readability_eligibility()
            else:
                assert observed.bind_readability()["state"] == "qualified"
            if expiry_at == "render":
                clock["now"] = EXPIRY
    assert caught.value.details["reason"] == "resolution_readability_currency_changed"


@pytest.mark.parametrize("expired", [False, True])
def test_closing_validates_application_once_and_rechecks_qualification(monkeypatch, expired):
    from evidence_wiki import readability_execution as execution
    calls = []
    session = SimpleNamespace(root="root")
    observed = SimpleNamespace(require=lambda current: calls.append("require"))
    app = {"attempt_id": "critic", "result": {"state": "issues", "issues": []}}
    monkeypatch.setattr(execution, "_current_application", lambda *_: calls.append("application") or app)
    monkeypatch.setattr(execution, "qualification", lambda *_a, **_k: calls.append("qualification") or {
        "state": "unknown" if expired else "qualified", "qualification_ref": "qualified-record"})
    authority = SimpleNamespace(load_trust=lambda *_: calls.append("trust") or {}, authority_basis=lambda *_: "current-trust")
    monkeypatch.setattr(execution.native, "owner", lambda stem: authority if stem == "_evidence_authority"
        else SimpleNamespace(configuration=lambda *_: {}))
    result = execution.closing_vector(session, observed)
    assert calls.count("application") == 1 and calls.count("qualification") == 1
    assert result["report"]["state"] == ("refused" if expired else "observed")
    assert result["application_ref"] == (None if expired else execution.digest(app))
    assert calls.count("trust") == (0 if expired else 1)
