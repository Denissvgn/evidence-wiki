"""Real stalled sink waits remain finite and externally uncertain."""

import threading
import time

import pytest

from evidence_wiki import telemetry_delivery as delivery
from evidence_wiki.errors import EvidenceWikiError
from tests.test_telemetry_delivery import acknowledgement, projection
from tests.test_telemetry_delivery import selected as _selected


@pytest.fixture
def selected(tmp_path):
    return _selected.__wrapped__(tmp_path)


def test_stalled_callback_returns_unknown_and_never_redispatches(selected, monkeypatch):
    value = projection(selected, monkeypatch)
    release, finished = threading.Event(), threading.Event()
    calls, external_effects = [], []

    def callback(request):
        calls.append(request)
        release.wait(10)
        external_effects.append("late external delivery")
        finished.set()
        return acknowledgement(request)

    sink = delivery.DeadlineSink("bounded-sink", callback, 1, host_deadline_enforced=True)
    started = time.monotonic()
    try:
        with selected[1].activate():
            receipt = delivery.deliver(selected[0], run_id="run", delivery_id="stalled", projection=value, sink=sink)
            elapsed = time.monotonic() - started
            assert elapsed < 3 and receipt["status"] == "unknown"
            assert receipt["error_code"] == "TELEMETRY_DEADLINE_UNKNOWN"
            assert not finished.is_set() and not external_effects
            assert delivery.deliver(selected[0], run_id="run", delivery_id="stalled", projection=value, sink=sink) == receipt
            with pytest.raises(EvidenceWikiError) as error:
                delivery.retry(selected[0], delivery_id="stalled", sink=sink)
            assert error.value.error_code == "TELEMETRY_DELIVERY_RETRY_UNSAFE"
    finally:
        release.set()
        assert finished.wait(2)
    assert len(calls) == 1 and external_effects == ["late external delivery"]
    with selected[1].activate():
        assert delivery.inspect(selected[0], delivery_id="stalled") == receipt


def test_occupied_callback_capacity_is_finite_and_does_not_dispatch(selected, monkeypatch):
    value = projection(selected, monkeypatch)
    monkeypatch.setattr(delivery, "_CALLBACK_CAPACITY", threading.BoundedSemaphore(0))
    calls = []
    sink = delivery.DeadlineSink("bounded-sink", lambda request: calls.append(request), 1, host_deadline_enforced=True)
    started = time.monotonic()
    with selected[1].activate():
        receipt = delivery.deliver(selected[0], run_id="run", delivery_id="capacity", projection=value, sink=sink)
    assert time.monotonic() - started < 1 and receipt["status"] == "unknown"
    assert calls == []
