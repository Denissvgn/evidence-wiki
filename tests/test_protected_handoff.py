"""Immutable declarations and executor delivery mechanics, not source permission proof."""

from dataclasses import replace

import pytest

from evidence_wiki import NativeInputHandoff
from evidence_wiki.agent_execution import ExecutionError
from evidence_wiki.disagreement_configuration import DisagreementPlan
from evidence_wiki.onboarding_contract import _matches
from evidence_wiki.protected_handoff import schema_document
from tests.test_agent_execution import LocalMeter, attempt, executor, final
from tests.test_disagreement_configuration import simple_profile


@pytest.mark.parametrize("value", [0, 31, True, 1.5, "30", None])
def test_delivery_ceiling_is_closed_and_typed(value):
    with pytest.raises(ExecutionError, match="protected_handoff_ceiling_invalid"):
        NativeInputHandoff(value)


def test_declaration_roundtrip_and_successor_keep_old_profile_bytes():
    base = replace(simple_profile(), disagreement=DisagreementPlan())
    old = base.document()
    declaration = NativeInputHandoff(12)
    assert NativeInputHandoff.restore(declaration.document()) == declaration
    _matches(declaration.document(), schema_document())
    selected = replace(base, input_handoff=declaration)
    assert selected.document()["schema_version"] == "research-execution-profile/v10"
    assert selected.document()["input_handoff"] == declaration.document()
    assert base.document() == old and "input_handoff" not in old
    assert selected.sha256 != base.sha256


@pytest.mark.parametrize("change", [{"adapter": "caller_ack"}, {"delivery_seconds": 31}, {"extra": True}])
def test_caller_flags_cannot_create_native_custody(change):
    with pytest.raises(ExecutionError, match="protected_handoff_"):
        NativeInputHandoff.restore(NativeInputHandoff().document() | change)


def test_unscoped_profile_cannot_declare_repair_delivery():
    with pytest.raises(ExecutionError, match="protected_handoff_profile_invalid"):
        replace(simple_profile(), input_handoff=NativeInputHandoff())


def test_bundled_delivery_failure_cannot_retain_early_child_success(tmp_path, monkeypatch):
    """Exercise only executor ordering; this stub establishes no source-use custody."""
    import time
    from types import SimpleNamespace

    host = executor(tmp_path, "print('{}')", meter=LocalMeter())
    value = attempt()

    def failed_handoff(_running, _payload, _lease):
        time.sleep(0.15)
        raise ExecutionError("protected_handoff_deadline_expired")

    monkeypatch.setattr(host, "_write_guarded", failed_handoff)
    with pytest.raises(ExecutionError, match="protected_handoff_deadline_expired"):
        host._invoke(value, lease=SimpleNamespace(_deadline=time.monotonic() + 1))
    observed = final(host, value)
    assert observed["state"] == "unknown" and observed["output"] is None
    assert not observed["final_accounting"]
    assert all(item is None for item in observed["observed_usage"].values())
    assert host.authenticate(observed, value) == observed
    assert host.poll(value)["state"] == "unknown"


def test_native_manager_closing_refusal_never_publishes_delivery_fact(monkeypatch):
    """Controlled owner sequencing only; these stubs confer no native permission."""
    import hashlib
    import threading
    import time
    from contextlib import contextmanager, nullcontext
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    from evidence_wiki import execution_handoff as handoff
    from evidence_wiki import execution_session as native
    from tests.test_repair_progress import inventory

    body = b"retained normalized bytes"
    material = inventory()
    material["claims"][0]["evidence"] = material["claims"][0]["evidence"][:1]
    sid = material["claims"][0]["evidence"][0]["source_id"]
    sha = "sha256:" + hashlib.sha256(body).hexdigest()
    material["claims"][0]["evidence"][0]["record_sha256"] = sha
    packet = {"source_id": sid, "record_sha256": sha, "source_binding": {"closure_id": "exact"}}
    worker = {"input": {"sources": [packet]}}
    selected_attempt = SimpleNamespace(attempt_id="attempt", job_sha256="opaque-job", input_sha256="opaque-input",
        job={"basis_id": "controlled-basis", "deadline_utc": (datetime.now(timezone.utc) + timedelta(seconds=20)).isoformat()},
        input_document=worker["input"], document=lambda: {"attempt_id": "attempt"}, worker_document=lambda: worker)
    context = SimpleNamespace(activate=lambda: nullcontext())
    selected_session = SimpleNamespace(root="root", context=context, binding={}, _check=lambda *_args, **_kwargs: None)
    lease = object.__new__(handoff.NativeHandoffLease)
    lease.session, lease.attempt = selected_session, selected_attempt
    lease._view = lease._config = lease._capture = lease._wall_deadline = None
    lease._deadline, lease._clock = time.monotonic() + 10, datetime.now(timezone.utc)
    lease._thread = threading.get_ident()
    lease.delivered_sha256 = lease._completed_sha256 = lease._worker_sha256 = None
    view = SimpleNamespace(state=SimpleNamespace(revisions={"rev": {"source_id": sid,
        "descriptor": {"normalized_path": "normalized"}, "files": {"normalized": body}}}),
        check=lambda *_args, **_kwargs: {"eligible": True, "complete": True}, revalidate=lambda *_args: None)

    @contextmanager
    def closes_with_refusal(*_args):
        yield view
        raise ExecutionError("native_closing_expiry_or_trust_refused")

    strict = SimpleNamespace(configuration=lambda *_args: {}, prepare_review=lambda *_args: {"result": {
        "questions": material["questions"], "claims": [{"claim": claim} for claim in material["claims"]]}})
    modules = {"_strict_evidence": strict, "strict_evidence": strict,
        "_evidence_usage": SimpleNamespace(configured=lambda *_args: True, current_view=closes_with_refusal),
        "_evidence_revision": SimpleNamespace(capture_workspace=lambda *_args: SimpleNamespace(revision_id="same"))}
    monkeypatch.setattr(handoff, "owner", modules.__getitem__)
    monkeypatch.setattr(native, "_document", lambda *_args: {"state": {"current": "answering"}})
    monkeypatch.setattr(native, "deadline", lambda *_args: datetime.now(timezone.utc) + timedelta(seconds=20))
    monkeypatch.setattr(native, "retained", lambda *_args: [{"kind": "execution_attempt", "data": {
        "attempt": selected_attempt.document(), "purpose": "repair", "target_claim_id": material["claims"][0]["id"],
        "repair_parent_inventory": material, "repair_parent_sources": [packet]}}])
    monkeypatch.setattr(native, "_sources", lambda *_args: ([packet], []))
    monkeypatch.setattr(native, "current", lambda *_args: {"basis_id": "controlled-basis",
        "policy_compatible": True, "qualification": {"state": "qualified"}})
    from evidence_wiki import research_session
    monkeypatch.setattr(research_session, "_load", lambda *_args: SimpleNamespace(TERMINAL_STATES={"finished"}))
    payload = native.canonical(worker)
    with pytest.raises(ExecutionError, match="native_closing_expiry_or_trust_refused"):
        with lease.custody(payload):
            lease.complete(payload)
            assert lease.delivered_sha256 is None
    assert lease.delivered_sha256 is None and lease._view is None and lease._thread is None
