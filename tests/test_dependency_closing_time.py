"""Closing observations recheck time-sensitive eligibility at the refreshed clock."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from tests.test_strict_evidence import CORE


@pytest.fixture
def closing_case(tmp_path, monkeypatch):
    initial = datetime(2026, 1, 1, tzinfo=timezone.utc)
    capture = SimpleNamespace(revision_id="revision")
    config = {}
    policy = {"max_source_age_seconds": 60, "max_review_age_seconds": 60}
    view = SimpleNamespace(now=initial)
    view.revalidate = lambda *args: setattr(view, "now", initial + timedelta(seconds=2))
    monkeypatch.setattr(CORE, "capture_workspace", lambda root: capture)
    monkeypatch.setattr(CORE, "configuration", lambda root: config)
    monkeypatch.setattr(CORE, "resolve_policy", lambda *args: policy)
    monkeypatch.setattr(CORE, "selection_inputs", lambda *args, **kwargs: (capture, {}, "basis"))
    monkeypatch.setattr(CORE, "_dependency_inputs", lambda *args: None)
    return SimpleNamespace(root=tmp_path, initial=initial, capture=capture, config=config,
        policy=policy, view=view)


def close(case, currents=(), reviews=()):
    CORE._dependency_closing(case.root, case.config, case.policy, case.view, case.capture,
        "basis", None, currents, reviews)


def material(case, age, reasons=()):
    return {"material": {"claims": [{"id": "claim", "evidence": [
        {"observed_at": (case.initial - timedelta(seconds=age)).isoformat()}]}],
        "local_context": {"mechanical_reasons": [{"claim_id": "claim", "reasons": list(reasons)}]}}}


def test_source_temporal_check_uses_refreshed_clock(closing_case, monkeypatch):
    case = closing_case

    def source_check(*args):
        CORE.require(case.view.now < case.initial + timedelta(seconds=1), "strict_source_expired")

    monkeypatch.setattr(CORE, "_dependency_inputs", source_check)
    with pytest.raises(CORE.ScriptRefusal) as caught:
        close(case)
    assert caught.value.details["reason"] == "strict_source_expired"


@pytest.mark.parametrize(("age", "reasons"), [(59, ()), (-1, ("strict_source_age_invalid",))])
def test_closure_source_age_change_refuses_frozen_material(closing_case, age, reasons):
    with pytest.raises(CORE.ScriptRefusal) as caught:
        close(closing_case, (material(closing_case, age, reasons),))
    assert caught.value.details["reason"] == "selective_dependency_basis_changed"


@pytest.mark.parametrize(("age", "reasons"), [(10, ()), (100, ("strict_source_age_invalid",))])
def test_unchanged_positive_and_negative_source_age_remain_available(closing_case, age, reasons):
    close(closing_case, (material(closing_case, age, reasons),))


def test_review_age_reauthenticated_at_closing(closing_case, monkeypatch):
    case = closing_case
    current = {"required_review_role": "evaluator", "dependency_basis": {}, "observation": {}}
    payload = {"generator": {"payload": {}}, "reviewed_at": (case.initial - timedelta(seconds=59)).isoformat(),
        "dependency_basis": {}, "observation": {}}
    envelope = {}
    case.view.trust = {}
    monkeypatch.setattr(CORE, "_dependency_authorship", lambda payload: {})
    monkeypatch.setattr(CORE, "verify_attestation", lambda envelope, role, trust, now: {
        "authenticated": True, "controller": role, "issued_at": case.initial.isoformat()})
    with pytest.raises(CORE.ScriptRefusal) as caught:
        close(case, reviews=((envelope, payload, current),))
    assert caught.value.details["reason"] == "strict_review_expired"


def test_review_attestation_reauthenticated_at_closing(closing_case, monkeypatch):
    case = closing_case
    current = {"required_review_role": "evaluator"}
    case.view.trust = {}

    def attest(envelope, role, trust, now):
        assert now == case.initial + timedelta(seconds=2)
        CORE.require(now < case.initial + timedelta(seconds=1), "strict_review_authentication_failed")

    monkeypatch.setattr(CORE, "verify_attestation", attest)
    with pytest.raises(CORE.ScriptRefusal) as caught:
        close(case, reviews=(({}, {"generator": {}}, current),))
    assert caught.value.details["reason"] == "strict_review_authentication_failed"
