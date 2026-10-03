"""Frozen redacted outbox, bounded host callback and uncertain delivery replay."""

from __future__ import annotations

import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta

import pytest

from evidence_wiki import telemetry
from evidence_wiki import telemetry_delivery as delivery
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.telemetry_contracts import ACK
from tests.test_telemetry import event, folded
from tests.test_telemetry import selected as _selected


@pytest.fixture
def selected(tmp_path):
    return _selected.__wrapped__(tmp_path)


def projection(selected, monkeypatch):
    return telemetry.project(folded(selected, monkeypatch, [event()]))


def acknowledgement(request, status="acknowledged"):
    return {"schema_version": ACK, "delivery_id": request["delivery_id"], "projection_sha256": request["projection_sha256"],
        "status": status, "ack_ref": "synthetic-ack" if status == "acknowledged" else None}


def sink(callback, **kwargs):
    return delivery.DeadlineSink("synthetic-sink", callback, 10, host_deadline_enforced=True, **kwargs)


def test_frozen_projection_ack_replay_has_one_callback_and_no_authority(selected, monkeypatch):
    value = projection(selected, monkeypatch)
    calls = []
    def callback(request):
        calls.append(copy.deepcopy(request))
        request["projection"]["report"]["limitations"].append("host mutation")
        return acknowledgement(request)
    with selected[1].activate():
        receipt = delivery.deliver(selected[0], run_id="run", delivery_id="delivery", projection=value, sink=sink(callback))
        assert receipt["status"] == "acknowledged" and receipt["attempts"] == 1
        assert not receipt["authorized"] and not receipt["evidence_accepted"]
        assert delivery.deliver(selected[0], run_id="run", delivery_id="delivery", projection=value, sink=sink(callback)) == receipt
        assert delivery.inspect(selected[0], delivery_id="delivery") == receipt
    assert len(calls) == 1 and "host mutation" not in value["report"]["limitations"]
    assert calls[0]["projection"]["measurement_authentication"] == "not_reverified"
    assert calls[0]["projection"]["report_is_declared"] and not list(selected[0].iterdir())


@pytest.mark.parametrize("failure", ["exception", "malformed", "wrong_id", "system_exit"])
def test_uncertain_callback_is_inspectable_and_never_blindly_retried(selected, monkeypatch, failure):
    value = projection(selected, monkeypatch)
    calls = []
    def callback(request):
        calls.append(request)
        if failure == "exception":
            raise OSError("Bearer synthetic-secret-should-not-persist")
        if failure == "system_exit":
            raise SystemExit("Bearer synthetic-secret-should-not-persist")
        if failure == "malformed":
            return {"raw_authority": "private"}
        return {**acknowledgement(request), "delivery_id": "other"}
    with selected[1].activate():
        receipt = delivery.deliver(selected[0], run_id="run", delivery_id="delivery", projection=value, sink=sink(callback))
        assert receipt["status"] == "unknown" and receipt["ack_ref"] is None
        assert delivery.inspect(selected[0], delivery_id="delivery") == receipt
        assert delivery.deliver(selected[0], run_id="run", delivery_id="delivery", projection=value, sink=sink(callback)) == receipt
        with pytest.raises(EvidenceWikiError) as error:
            delivery.retry(selected[0], delivery_id="delivery", sink=sink(callback))
        assert error.value.error_code == "TELEMETRY_DELIVERY_RETRY_UNSAFE"
    assert len(calls) == 1
    assert all(b"synthetic-secret-should-not-persist" not in path.read_bytes()
               for path in selected[1].mutation_control_directory.rglob("*.json"))


def test_known_negative_ack_has_finite_explicit_retries_and_immutable_attempt_receipts(selected, monkeypatch):
    value = projection(selected, monkeypatch)
    calls = []
    def callback(request):
        calls.append(request)
        return acknowledgement(request, "failed")
    with selected[1].activate():
        receipt = delivery.deliver(selected[0], run_id="run", delivery_id="delivery", projection=value, sink=sink(callback), max_attempts=2)
        assert receipt["status"] == "failed" and receipt["attempts"] == 1
        after = delivery.retry(selected[0], delivery_id="delivery", sink=sink(callback))
        assert after["status"] == "failed" and after["attempts"] == 2
        with pytest.raises(EvidenceWikiError) as error:
            delivery.retry(selected[0], delivery_id="delivery", sink=sink(callback))
        assert error.value.error_code == "TELEMETRY_DELIVERY_ATTEMPTS_EXHAUSTED"
    assert len(calls) == 2
    receipts = list(selected[1].mutation_control_directory.rglob("attempt-*.json"))
    assert len(receipts) == 2 and receipt != after


def test_active_callback_releases_gates_and_inspection_preserves_pending(selected, monkeypatch):
    value = projection(selected, monkeypatch)
    started, release = threading.Event(), threading.Event()
    calls = []
    def callback(request):
        calls.append(request)
        started.set()
        assert release.wait(5)
        return acknowledgement(request)
    def perform():
        with selected[1].activate():
            return delivery.deliver(selected[0], run_id="run", delivery_id="delivery", projection=value, sink=sink(callback))
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(perform)
        try:
            assert started.wait(5)
            with selected[1].activate():
                receipt = delivery.inspect(selected[0], delivery_id="delivery")
                assert receipt["status"] == "sending" and receipt["attempts"] == 1
                assert delivery.deliver(selected[0], run_id="run", delivery_id="delivery", projection=value, sink=sink(callback)) == receipt
                with pytest.raises(EvidenceWikiError):
                    delivery.retry(selected[0], delivery_id="delivery", sink=sink(callback))
        finally:
            release.set()
        assert future.result(timeout=5)["status"] == "acknowledged"
    assert len(calls) == 1


def test_late_ack_is_unknown_and_finite_deadline_is_passed_to_host(selected, monkeypatch):
    value = projection(selected, monkeypatch)
    now = delivery.current_time()
    received = []
    def callback(request):
        received.append(request)
        monkeypatch.setattr(delivery, "current_time", lambda: now + timedelta(seconds=100))
        return acknowledgement(request)
    with selected[1].activate():
        receipt = delivery.deliver(selected[0], run_id="run", delivery_id="delivery", projection=value, sink=sink(callback))
        assert receipt["status"] == "unknown" and receipt["error_code"] == "TELEMETRY_DEADLINE_UNKNOWN"
    assert received[0]["deadline_utc"] == receipt["deadline_utc"] and received[0]["attempt"] == 1


def test_unbounded_callable_profile_and_raw_projection_refuse_before_control_effects(selected, monkeypatch):
    value = projection(selected, monkeypatch)
    calls = []
    callback = lambda request: calls.append(request)
    unbounded = delivery.DeadlineSink("sink", callback, 10)
    with selected[1].activate():
        with pytest.raises(EvidenceWikiError):
            delivery.deliver(selected[0], run_id="run", delivery_id="delivery", projection=value, sink=unbounded)
        changed = copy.deepcopy(value)
        changed["report"]["trace"][0]["event"]["summary"]["text"] = "Bearer synthetic-secret-value"
        changed["report"]["trace"][0]["event_sha256"] = telemetry._sha(changed["report"]["trace"][0]["event"])
        changed["report_sha256"] = telemetry._sha(changed["report"])
        with pytest.raises(EvidenceWikiError):
            delivery.deliver(selected[0], run_id="run", delivery_id="delivery", projection=changed, sink=sink(callback))
    assert calls == [] and not list(selected[1].mutation_control_directory.iterdir())


def test_current_host_selection_and_changed_exact_intent_refuse_replay(selected, monkeypatch):
    value = projection(selected, monkeypatch)
    callback = acknowledgement
    with selected[1].activate():
        receipt = delivery.deliver(selected[0], run_id="run", delivery_id="delivery", projection=value, sink=sink(callback))
        with pytest.raises(EvidenceWikiError):
            delivery.deliver(selected[0], run_id="run", delivery_id="delivery", projection=value, sink=sink(callback), max_attempts=1)
    with replace(selected[1], actor_ref="other").activate(), pytest.raises(EvidenceWikiError):
        delivery.inspect(selected[0], delivery_id="delivery")
    assert receipt["status"] == "acknowledged"


def test_interrupted_owner_after_sending_checkpoint_is_pending_until_deadline_then_unknown(selected, monkeypatch):
    value = projection(selected, monkeypatch)
    calls = []
    original = delivery._Outbox.checkpoint
    def interrupted(store, state):
        result = original(store, state)
        if state["status"] == "sending":
            raise SystemExit(0)
        return result
    monkeypatch.setattr(delivery._Outbox, "checkpoint", interrupted)
    with selected[1].activate():
        with pytest.raises(EvidenceWikiError) as error:
            delivery.deliver(selected[0], run_id="run", delivery_id="delivery", projection=value, sink=sink(lambda request: calls.append(request)))
        assert error.value.error_code == "LIBRARY_PROCESS_EXIT"
        monkeypatch.setattr(delivery._Outbox, "checkpoint", original)
        pending = delivery.inspect(selected[0], delivery_id="delivery")
        assert pending["status"] == "sending"
        monkeypatch.setattr(delivery, "current_time", lambda: delivery._time(pending["deadline_utc"]) + timedelta(seconds=1))
        unknown = delivery.inspect(selected[0], delivery_id="delivery")
        assert unknown["status"] == "unknown"
        assert delivery.deliver(selected[0], run_id="run", delivery_id="delivery", projection=value, sink=sink(lambda request: calls.append(request))) == unknown
    assert calls == []


def test_acknowledged_attempt_before_checkpoint_failure_is_restored_without_resend(selected, monkeypatch):
    value = projection(selected, monkeypatch)
    calls = []
    def callback(request):
        calls.append(request)
        return acknowledgement(request)
    original = delivery._Outbox.checkpoint
    def interrupted(store, state):
        if state["status"] == "acknowledged":
            raise SystemExit(0)
        return original(store, state)
    monkeypatch.setattr(delivery._Outbox, "checkpoint", interrupted)
    with selected[1].activate():
        with pytest.raises(EvidenceWikiError):
            delivery.deliver(selected[0], run_id="run", delivery_id="delivery", projection=value, sink=sink(callback))
        monkeypatch.setattr(delivery._Outbox, "checkpoint", original)
        actual = delivery.inspect(selected[0], delivery_id="delivery")
        assert actual["status"] == "acknowledged"
        assert delivery.deliver(selected[0], run_id="run", delivery_id="delivery", projection=value, sink=sink(callback)) == actual
    assert len(calls) == 1


@pytest.mark.parametrize("reference", ["sk-syntheticcredentialvalue", "/private/host/credential", "https://user:password@example.invalid/callback", "opaque-secret-token-value"])
def test_callback_ack_reference_is_only_retained_as_content_commitment(selected, monkeypatch, reference):
    value = projection(selected, monkeypatch)
    def callback(request):
        return {**acknowledgement(request), "ack_ref": reference}
    with selected[1].activate():
        receipt = delivery.deliver(selected[0], run_id="run", delivery_id="delivery", projection=value, sink=sink(callback))
        assert receipt["status"] == "acknowledged" and receipt["ack_ref"] == telemetry._sha({"ack_ref": reference})
    assert reference not in str(receipt)
    assert all(reference.encode() not in path.read_bytes() for path in selected[1].mutation_control_directory.rglob("*.json"))
