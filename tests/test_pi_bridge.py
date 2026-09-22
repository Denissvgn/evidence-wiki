"""Adversarial framing and lifecycle checks, separate from real-harness qualification."""

import json
import os
import sys
import threading
from pathlib import Path

import pytest

from evidence_wiki.errors import UsageError
from evidence_wiki.pi_bridge import JsonLines, PiRpcBridge


def test_lf_framing_preserves_unicode_separators_and_fragmented_utf8():
    value = {"type": "message_end", "message": {"text": "é\u2028one\u2029two"}}
    data = (json.dumps(value, ensure_ascii=False) + "\r\n").encode()
    parser = JsonLines()
    result = []
    for byte in data:
        result += parser.feed(bytes([byte]))
    parser.eof()
    assert result == [value]


@pytest.mark.parametrize("data", [b"\n", b"{}\n", b"[]\n", b'{"type":"x","type":"y"}\n',
                                   b'{"type":"x","data":NaN}\n', b'{"type":"\xff"}\n'])
def test_malformed_records_refuse_without_quoting_stream(data):
    with pytest.raises(UsageError):
        JsonLines().feed(data)


def test_partial_and_oversized_frames_refuse():
    parser = JsonLines()
    parser.feed(b'{"type":')
    with pytest.raises(UsageError):
        parser.eof()
    with pytest.raises(UsageError):
        JsonLines().feed(b"x" * 1_048_577)


@pytest.fixture
def executable(tmp_path):
    if os.name != "posix":
        pytest.skip("The optional Pi process bridge requires POSIX pipes/process groups")
    path = tmp_path / "pi-stub"
    source = Path(__file__).parent / "fixtures/pi_rpc_stub.py"
    path.write_text(f"#!{sys.executable}\n" + source.read_text())
    path.chmod(0o700)
    return path


def bridge(executable, tmp_path):
    cwd, state = tmp_path / "work", tmp_path / "state"
    cwd.mkdir(exist_ok=True)
    state.mkdir(exist_ok=True)
    return PiRpcBridge(executable, cwd=cwd, state_dir=state, authority="fixture operator",
                       provider="fixture", model="fixture")


@pytest.mark.parametrize("message,status", [("completed", "settled"), ("failed", "failed"),
                                           ("rejected", "rejected"), ("eof", "indeterminate"),
                                           ("wrong-id", "indeterminate"), ("session-change", "indeterminate")])
def test_acceptance_failure_completion_and_session_binding_are_distinct(executable, tmp_path, message, status):
    with bridge(executable, tmp_path) as host:
        result = host.prompt(message, request_id="one", timeout=2)
    assert result["status"] == status
    assert result["evidence_acceptance"] == "not_evaluated" and not result["host_enforced"]
    assert "not a receipt" not in json.dumps(result)


def test_cancellation_clears_queue_and_prevents_replay(executable, tmp_path):
    with bridge(executable, tmp_path) as host:
        cancel = threading.Event()
        cancel.set()
        result = host.prompt("hanging", request_id="cancel", cancel=cancel)
        assert result["status"] == "cancelled"
        assert host.closed


def test_timeout_and_duplicate_request_never_replay(executable, tmp_path):
    with bridge(executable, tmp_path) as host:
        assert host.prompt("completed", request_id="same")["status"] == "settled"
        assert host.prompt("completed", request_id="same")["reason"] == "pi_prompt_replay_refused"
    with bridge(executable, tmp_path) as host:
        assert host.prompt("hanging", request_id="timeout", timeout=.2)["status"] == "indeterminate"
        assert host.closed


def test_replaced_cwd_and_unqualified_version_refuse(executable, tmp_path):
    with bridge(executable, tmp_path) as host:
        host.cwd.rename(tmp_path / "old-work")
        host.cwd.mkdir()
        result = host.prompt("completed", request_id="moved")
        assert result["reason"] == "pi_session_or_cwd_replaced"
    executable.write_text(executable.read_text().replace("0.87.0", "0.1.0"))
    with pytest.raises(UsageError) as error:
        bridge(executable, tmp_path)
    assert error.value.details["field"] == "pi_version_unqualified"


def test_unobserved_acceptance_remains_unknown(executable, tmp_path):
    with bridge(executable, tmp_path) as host:
        result = host.prompt("eof-before-ack", request_id="unknown")
    assert result["status"] == "indeterminate"
    assert result["prompt_accepted"] is None


def test_close_allows_owned_children_to_handle_termination(executable, tmp_path):
    with bridge(executable, tmp_path) as host:
        assert host.prompt("spawn-child", request_id="child")["status"] == "settled"
    assert (tmp_path / "child-stopped").read_text() == "stopped"
