"""Actual stdio framing and scoped server authority are independent of workspace MCP."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from evidence_wiki.onboarding import Onboarding
from evidence_wiki.onboarding_mcp import OnboardingMcpServer
from tests.test_caller_research import workspace as workspace


def message(identifier, method, params=None):
    return {"jsonrpc": "2.0", "id": identifier, "method": method, "params": params or {}}


def initialize():
    return message(1, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "host", "version": "1"}})


def test_subprocess_stdio_bootstrap_resources_and_malformed_recovery(tmp_path):
    records = [initialize(), {"jsonrpc": "2.0", "method": "notifications/initialized"}, message(2, "tools/list"),
        message(3, "tools/call", {"name": "onboarding_bootstrap", "arguments": {"request_id": "stdio"}}),
        message(4, "resources/list"), message(5, "tools/call", {"name": "onboarding_apply", "arguments": {"value": {}}})]
    raw = "\n".join(json.dumps(row) for row in records) + '\n{"jsonrpc":"2.0","id":6,"id":7,"method":"ping"}\n' + json.dumps(message(8, "ping")) + "\n"
    result = subprocess.run([sys.executable, "-m", "evidence_wiki.cli", "serve-onboarding-mcp"], input=raw, capture_output=True,
        text=True, cwd=tmp_path, env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    output = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(output) == 7
    assert all(row["name"] != "onboarding_apply" for row in output[1]["result"]["tools"])
    with Onboarding.open() as handle:
        assert json.loads(output[2]["result"]["content"][0]["text"]) == handle.bootstrap(request_id="stdio")
    assert output[3]["result"]["resources"]
    assert output[4]["error"]["code"] == -32602
    assert output[5]["error"]["code"] == -32700
    assert output[6]["id"] == 8 and output[6]["result"] == {}


def test_lifecycle_and_ungranted_scope_refuse_without_writes(tmp_path):
    server = OnboardingMcpServer(allow=["apply"])
    try:
        assert server.handle_message(message(0, "tools/list"))["error"]["code"] == -32002
        server.handle_message(initialize())
        server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
        result = server.handle_message(message(2, "tools/call", {"name": "onboarding_bootstrap", "arguments": {"target": str(tmp_path)}}))
        assert result["result"]["isError"] is True
        error = json.loads(result["result"]["content"][0]["text"])
        assert error["error_code"] == "ONBOARDING_AUTHORITY_REQUIRED"
        assert not list(tmp_path.iterdir())
    finally:
        server.close()


def test_invalid_utf8_is_rejected_without_rewriting_string_values():
    import io

    stream = io.TextIOWrapper(io.BytesIO(b'{"jsonrpc":"2.0","method":"ping","id":"\xff"}\n' + json.dumps(message(3, "ping")).encode() + b"\n"))
    output = io.StringIO()
    server = OnboardingMcpServer()
    server.serve(stream, output)
    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    assert rows[0]["error"]["code"] == -32700
    assert rows[1]["result"] == {} and rows[1]["id"] == 3


def test_oversized_line_drains_before_next_request():
    import io

    from evidence_wiki.onboarding_mcp import MAX_LINE_BYTES

    stream = io.TextIOWrapper(io.BytesIO(b"x" * (MAX_LINE_BYTES + 1) + b"\n" + json.dumps(message(4, "ping")).encode() + b"\n"))
    output = io.StringIO()
    OnboardingMcpServer().serve(stream, output)
    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    assert rows[0]["error"]["code"] == -32600
    assert rows[1]["id"] == 4 and rows[1]["result"] == {}


@pytest.mark.skipif(os.name != "posix", reason="Research mutation owners require POSIX.")
def test_ingest_tool_accepts_each_exclusive_selector_and_reconciles_actual_owner(workspace):
    from evidence_wiki._pack_io import canonical
    from evidence_wiki.errors import EvidenceWikiError
    from evidence_wiki.source_delivery import deliver
    from tests.test_caller_research import block_request
    from tests.test_source_capabilities import capture_request, tools_manifest

    request = block_request(workspace)
    server = OnboardingMcpServer(allowed_roots=[workspace], allow=["research.start", "research.ingest"])
    try:
        common = {"target": str(workspace), "agent_id": "caller", "run_id": "capture-run"}
        server.call_tool("onboarding_research_start", common)
        value = capture_request(b"Retained reflectance observation: 0.74 for the selected surface.")
        value["capture"]["request_id"] = request["request_id"]
        value["capture"]["scope"] = {}
        deliver(canonical(value), target=workspace, path="raw/papers/capture.md", host_tools=canonical(tools_manifest()))
        common["request_id"] = request["request_id"]
        result = server.call_tool("onboarding_research_ingest", {**common, "source_path": "raw/papers/capture.md"})
        assert result["request"]["request"]["status"] == "fulfilled"
        again = server.call_tool("onboarding_research_ingest", {**common, "source_id": result["source"]["source_id"]})
        assert not again["reopened"] and not again["request"]["updated"]
        messages = [initialize(), {"jsonrpc": "2.0", "method": "notifications/initialized"},
            message(2, "tools/call", {"name": "onboarding_research_ingest", "arguments": {**common, "source_id": result["source"]["source_id"]}})]
        process = subprocess.run([sys.executable, "-I", "-m", "evidence_wiki.cli", "serve-onboarding-mcp",
            "--allow-root", str(workspace), "--allow-operation", "research.ingest"],
            input="\n".join(json.dumps(row) for row in messages) + "\n", capture_output=True, text=True, encoding="utf-8", timeout=30, check=False)
        assert process.returncode == 0, process.stderr
        responses = [json.loads(line) for line in process.stdout.splitlines()]
        assert len(responses) == 2 and not responses[-1]["result"]["isError"]
        with pytest.raises(EvidenceWikiError):
            server.call_tool("onboarding_research_ingest", {**common, "source_id": result["source"]["source_id"], "source_path": "raw/papers/capture.md"})
    finally:
        server.close()
