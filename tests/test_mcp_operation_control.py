"""MCP hosts can lend their original finite control without transferring ownership."""

import json
import threading
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from evidence_wiki import OperationControl
from evidence_wiki.errors import ConfigError, EvidenceWikiError
from evidence_wiki.onboarding_mcp import OnboardingMcpServer
from tests.test_onboarding_mcp import initialize, message


@pytest.fixture
def server(tmp_path):
    selected = OnboardingMcpServer(allowed_roots=[tmp_path], research_allow=["run-cycle"])
    selected.handle_message(initialize())
    selected.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
    try:
        yield selected
    finally:
        selected.close()


def control(seconds=1300, **kwargs):
    return OperationControl(deadline_utc=(datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(),
                            timeout_seconds=max(seconds, 1), **kwargs)


def request(root):
    return message(2, "tools/call", {"name": "research_run_cycle", "arguments": {"target": str(root)}})


def payload(response):
    return json.loads(response["result"]["content"][0]["text"])


def test_borrowed_control_reaches_command_unchanged_and_remains_reusable(server, tmp_path, monkeypatch):
    from evidence_wiki.onboarding_mcp import Workspace

    received = []
    expected = {"status": "returned", "result": {"label": "observación"}, "error": None}

    def command(value, *, runtime, operation_control):
        received.append(operation_control)
        assert operation_control.timeout_seconds == 1300
        assert operation_control.remaining_seconds() > 600
        return expected

    opened = SimpleNamespace(research=SimpleNamespace(command=command))
    monkeypatch.setattr(Workspace, "open", lambda _: nullcontext(opened))
    borrowed = control()
    monkeypatch.setattr(borrowed, "close", lambda: pytest.fail("borrowed control was closed"))
    try:
        first = server.handle_message(request(tmp_path), operation_control=borrowed)
        borrowed.checkpoint("after_first_call")
        second = server.handle_message(request(tmp_path), operation_control=borrowed)
        borrowed.checkpoint("after_second_call")
        assert received == [borrowed, borrowed]
        assert first == second
        assert first["result"] == {"content": [{"type": "text", "text": json.dumps(expected, ensure_ascii=False,
                                                                                 allow_nan=False)}], "isError": False}
    finally:
        OperationControl.close(borrowed)


def test_borrowed_control_stays_host_owned_after_tool_refusal(server, tmp_path, monkeypatch):
    borrowed = control()
    monkeypatch.setattr(borrowed, "close", lambda: pytest.fail("borrowed control was closed"))

    def call_tool(*args, **kwargs):
        raise ConfigError("OPERATION_CONTROL_INVALID", "Fixture refusal.", recoverable=False)

    monkeypatch.setattr(server, "call_tool", call_tool)
    try:
        plain = server.handle_message(request(tmp_path))
        response = server.handle_message(request(tmp_path), operation_control=borrowed)
        assert response == plain
        borrowed.checkpoint("host_can_reuse_after_refusal")
    finally:
        OperationControl.close(borrowed)


@pytest.mark.parametrize("state,code", [
    ("expired", "OPERATION_DEADLINE_EXCEEDED"),
    ("cancelled", "OPERATION_CANCELLED"),
    ("closed", "OPERATION_CONTROL_CLOSED"),
    ("invalid", "OPERATION_CONTROL_INVALID"),
    ("subclass", "OPERATION_CONTROL_INVALID"),
    ("dual", "OPERATION_CONTROL_INVALID"),
])
def test_unusable_control_refuses_before_tool_effects(server, tmp_path, monkeypatch, state, code):
    effect = tmp_path / "effect"

    def call_tool(*args, **kwargs):
        effect.write_text("unexpected")
        pytest.fail("tool ran before control refusal")

    monkeypatch.setattr(server, "call_tool", call_tool)
    borrowed = control(-1 if state == "expired" else 1300,
                       cancel_requested=(lambda: True) if state == "cancelled" else None)
    supplied = borrowed
    if state == "closed":
        borrowed.close()
    elif state == "invalid":
        supplied = SimpleNamespace(checkpoint=lambda _: pytest.fail("duck-typed control was used"))
    elif state == "subclass":
        class Derived(OperationControl):
            pass
        supplied = object.__new__(Derived)
    try:
        response = server.handle_message(request(tmp_path), operation_control=supplied,
                                         cancellation=threading.Event() if state == "dual" else None)
        assert response["result"]["isError"] is True
        assert payload(response)["error_code"] == code
        assert not effect.exists()
        if state == "dual":
            borrowed.checkpoint("still_owned_by_host")
    finally:
        borrowed.close()


@pytest.mark.parametrize("runtime_seconds,expected_seconds", [(None, 600), (1300, 600), (120, 120)])
@pytest.mark.parametrize("refused", [False, True])
def test_default_cancellation_control_keeps_bound_and_closes(server, tmp_path, monkeypatch,
                                                           runtime_seconds, expected_seconds, refused):
    created, closed = [], []
    original_init, original_close = OperationControl.__init__, OperationControl.close

    def tracked_init(self, **kwargs):
        original_init(self, **kwargs)
        created.append(self)

    def tracked_close(self):
        closed.append(self)
        original_close(self)

    monkeypatch.setattr(OperationControl, "__init__", tracked_init)
    monkeypatch.setattr(OperationControl, "close", tracked_close)
    if runtime_seconds is not None:
        server.runtime = SimpleNamespace(policy=SimpleNamespace(to_dict=lambda: {"budgets": {"seconds": runtime_seconds}}))
    expected = {"status": "returned", "result": {"label": "observación"}, "error": None}
    error = ConfigError("OPERATION_CONTROL_INVALID", "Fixture refusal.", recoverable=False)

    def call_tool(name, arguments, *, operation_control):
        if operation_control is not None:
            assert operation_control is created[0]
            assert operation_control.timeout_seconds == expected_seconds
        if refused:
            raise error
        return expected

    monkeypatch.setattr(server, "call_tool", call_tool)
    plain = server.handle_message(request(tmp_path))
    assert created == closed == []
    response = server.handle_message(request(tmp_path), cancellation=threading.Event())
    assert response == plain
    assert closed == created and len(created) == 1
    with pytest.raises(EvidenceWikiError) as failure:
        created[0].checkpoint("closed_after_return")
    assert failure.value.error_code == "OPERATION_CONTROL_CLOSED"


def test_default_cancelled_control_refuses_before_effects_and_closes(server, tmp_path, monkeypatch):
    created = []
    original_init = OperationControl.__init__

    def tracked_init(self, **kwargs):
        original_init(self, **kwargs)
        created.append(self)

    monkeypatch.setattr(OperationControl, "__init__", tracked_init)
    monkeypatch.setattr(server, "call_tool", lambda *args, **kwargs: pytest.fail("cancelled tool ran"))
    cancellation = threading.Event()
    cancellation.set()
    response = server.handle_message(request(tmp_path), cancellation=cancellation)
    assert payload(response)["error_code"] == "OPERATION_CANCELLED"
    with pytest.raises(EvidenceWikiError) as failure:
        created[0].checkpoint("closed_after_refusal")
    assert failure.value.error_code == "OPERATION_CONTROL_CLOSED"


def test_scoped_route_borrows_original_control_with_before_and_after_checkpoints(server, tmp_path, monkeypatch):
    from tools import _repeated_integration_host as host

    received, checkpoints = [], []
    original_checkpoint = OperationControl.checkpoint

    def tracked_checkpoint(self, name):
        checkpoints.append(name)
        original_checkpoint(self, name)

    def call_tool(name, arguments, *, operation_control):
        received.append(operation_control)
        return {"status": "returned", "result": {"accepted": True}, "error": None}

    monkeypatch.setattr(OperationControl, "checkpoint", tracked_checkpoint)
    monkeypatch.setattr(server, "call_tool", call_tool)
    route = object.__new__(host.Routes)
    route.root, route.route, route.calls, route.server = tmp_path, "scoped_mcp", [], server
    borrowed = control()
    try:
        assert route.call("run-cycle", {}, borrowed) == {"accepted": True}
        assert received == [borrowed]
        assert checkpoints == ["reference_route_before_call", "before_mcp_tool", "reference_route_after_call"]
        borrowed.checkpoint("route_did_not_close_borrowed_control")
        assert borrowed.remaining_seconds() > 600
    finally:
        borrowed.close()
