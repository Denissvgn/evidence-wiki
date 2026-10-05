"""Real descriptor IO verifies mechanics, never permission or semantic approval."""

import errno
import os
import threading
import time

import pytest

from evidence_wiki import handoff_pipe as handoff


class Lease:
    def __init__(self, *, fail_at=None, delay=0):
        self.checkpoints = []
        self.completed = []
        self.fail_at = fail_at
        self.delay = delay

    def checkpoint(self):
        self.checkpoints.append(threading.get_ident())
        if self.delay:
            time.sleep(self.delay)
        if len(self.checkpoints) == self.fail_at:
            raise ValueError("actual_lease_refusal")

    def complete(self, payload):
        self.completed.append(payload)
        return {"mechanical_bytes": len(payload)}


def pipe():
    reader, writer = os.pipe()
    return reader, os.fdopen(writer, "wb")


def drain(reader, collected):
    try:
        while raw := os.read(reader, 8192):
            collected.append(raw)
    finally:
        os.close(reader)


def test_delayed_reader_exact_payload_same_thread_and_final_completion():
    reader, writer = pipe()
    collected = []
    def delayed():
        time.sleep(0.02)
        drain(reader, collected)
    thread = threading.Thread(target=delayed)
    thread.start()
    payload = bytes(range(256)) * 2048
    lease = Lease()
    result = handoff.write_handoff(writer, payload, lease, deadline=time.monotonic() + 2)
    thread.join(2)
    assert not thread.is_alive() and writer.closed
    assert b"".join(collected) == payload
    assert result == {"mechanical_bytes": len(payload)} and lease.completed == [payload]
    assert len(lease.checkpoints) >= 2 * (len(payload) // handoff.CHUNK_BYTES) + 1
    assert set(lease.checkpoints) == {threading.get_ident()}


@pytest.mark.parametrize("fail_at", [1, 3])
def test_lease_refusal_stops_before_any_further_chunk_and_closes(fail_at):
    reader, writer = pipe()
    lease = Lease(fail_at=fail_at)
    payload = b"secret" * 2048
    with pytest.raises(ValueError, match="^actual_lease_refusal$"):
        handoff.write_handoff(writer, payload, lease, deadline=time.monotonic() + 1)
    actual = []
    drain(reader, actual)
    received = b"".join(actual)
    assert received == payload[:0 if fail_at == 1 else handoff.CHUNK_BYTES]
    assert writer.closed and lease.completed == []


def test_delayed_nonreader_times_out_after_partial_io_without_completion():
    reader, writer = pipe()
    lease = Lease()
    payload = b"p" * handoff.MAX_BYTES
    started = time.monotonic()
    with pytest.raises(handoff.HandoffPipeError, match="deadline_expired"):
        handoff.write_handoff(writer, payload, lease, deadline=started + 0.04)
    assert time.monotonic() - started < 1
    actual = []
    drain(reader, actual)
    assert 0 < len(b"".join(actual)) < len(payload)
    assert lease.completed == [] and writer.closed


def test_expiry_while_native_checkpoint_runs_prevents_write():
    reader, writer = pipe()
    lease = Lease(delay=0.02)
    with pytest.raises(handoff.HandoffPipeError, match="deadline_expired"):
        handoff.write_handoff(writer, b"do not deliver", lease, deadline=time.monotonic() + 0.01)
    actual = []
    drain(reader, actual)
    assert actual == [] and lease.completed == [] and writer.closed


def test_broken_reader_is_typed_and_never_completed():
    reader, writer = pipe()
    os.close(reader)
    lease = Lease()
    with pytest.raises(handoff.HandoffPipeError, match="pipe_broken"):
        handoff.write_handoff(writer, b"input", lease, deadline=time.monotonic() + 1)
    assert lease.completed == [] and writer.closed


def test_real_short_writes_and_eagain_preserve_each_byte_once(monkeypatch):
    reader, writer = pipe()
    actual = handoff.os.write
    written = []
    def short(descriptor, payload):
        if not written:
            written.append(0)
            raise BlockingIOError(errno.EAGAIN, "controlled unavailable pipe")
        count = actual(descriptor, payload[:7])
        written.append(count)
        return count
    monkeypatch.setattr(handoff.os, "write", short)
    lease = Lease()
    payload = bytes(range(193))
    handoff.write_handoff(writer, payload, lease, deadline=time.monotonic() + 1)
    received = []
    drain(reader, received)
    assert b"".join(received) == payload and sum(written) == len(payload)
    assert lease.completed == [payload] and len(lease.checkpoints) >= 2 * len(written) + 1


def test_interrupted_wait_rechecks_custody(monkeypatch):
    reader, writer = pipe()
    actual = handoff.select.select
    calls = []
    def interrupted(*args):
        calls.append(True)
        if len(calls) == 1:
            raise InterruptedError()
        return actual(*args)
    monkeypatch.setattr(handoff.select, "select", interrupted)
    lease = Lease()
    handoff.write_handoff(writer, b"exact", lease, deadline=time.monotonic() + 1)
    received = []
    drain(reader, received)
    assert b"".join(received) == b"exact" and len(lease.checkpoints) >= 4


@pytest.mark.parametrize("field,value,reason", [
    ("payload", bytearray(b"mutable"), "payload_invalid"), ("payload", b"", "payload_invalid"),
    ("max_bytes", True, "byte_limit_invalid"), ("max_bytes", handoff.MAX_BYTES + 1, "byte_limit_invalid"),
    ("deadline", float("nan"), "deadline_invalid"), ("deadline", float("inf"), "deadline_invalid"),
    ("deadline", True, "deadline_invalid"), ("deadline", 0, "deadline_expired")])
def test_invalid_bounds_and_expired_deadline_close_without_bytes(field, value, reason):
    reader, writer = pipe()
    lease = Lease()
    parameters = {"payload": b"bounded", "max_bytes": handoff.MAX_BYTES, "deadline": time.monotonic() + 1}
    parameters[field] = value
    with pytest.raises(handoff.HandoffPipeError, match=reason):
        handoff.write_handoff(writer, lease=lease, **parameters)
    actual = []
    drain(reader, actual)
    assert not actual and writer.closed and lease.completed == []


def test_payload_cap_and_fixed_deadline_ceiling_are_not_softened():
    for parameters in ({"max_bytes": 2}, {"deadline": time.monotonic() + 31}):
        reader, writer = pipe()
        lease = Lease()
        arguments = {"deadline": time.monotonic() + 1, **parameters}
        with pytest.raises(handoff.HandoffPipeError):
            handoff.write_handoff(writer, b"three", lease, **arguments)
        actual = []
        drain(reader, actual)
        assert not actual and not lease.completed and writer.closed


def test_regular_file_cannot_substitute_for_child_stdin(tmp_path):
    path = tmp_path / "not-stdin"
    lease = Lease()
    with path.open("wb") as selected:
        with pytest.raises(handoff.HandoffPipeError, match="pipe_invalid"):
            handoff.write_handoff(selected, b"private", lease, deadline=time.monotonic() + 1)
        assert selected.closed
    assert path.read_bytes() == b"" and not lease.completed


def test_child_exit_during_delivery_is_not_completion():
    reader, writer = pipe()
    lease = Lease()
    with pytest.raises(handoff.HandoffPipeError, match="child_exit"):
        handoff.write_handoff(writer, b"input", lease, deadline=time.monotonic() + 1, is_alive=lambda: False)
    actual = []
    drain(reader, actual)
    assert not actual and writer.closed and not lease.completed
