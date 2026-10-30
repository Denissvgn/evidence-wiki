"""Host-tool declarations use one bounded decoder across Python and transport."""

import copy
import json

import pytest

from evidence_wiki import Onboarding
from evidence_wiki.errors import UsageError
from evidence_wiki.host_capabilities import read_tools
from evidence_wiki.onboarding_mcp import OnboardingMcpServer
from tests.test_source_capabilities import tools_manifest


def test_host_tool_object_and_encoded_input_are_equivalent_and_detached():
    supplied = tools_manifest()
    snapshot = copy.deepcopy(supplied)
    from_object = read_tools(supplied)
    from_bytes = read_tools(json.dumps(supplied).encode())
    assert from_object == from_bytes
    assert supplied == snapshot
    from_object[0]["operations"].clear()
    assert supplied == snapshot
    assert read_tools(supplied) == from_bytes


def test_public_python_and_mcp_object_inputs_match_encoded_input(tmp_path):
    supplied = tools_manifest()
    with Onboarding.open(allowed_roots=[tmp_path], allow=["inspect"]) as host:
        encoded = host.inspect(tmp_path / "absent", host_tools=json.dumps(supplied).encode())
        direct = host.inspect(tmp_path / "absent", host_tools=supplied)
    server = OnboardingMcpServer(allowed_roots=[tmp_path])
    try:
        through_mcp = server.call_tool("onboarding_inspect", {"target": str(tmp_path / "absent"), "host_tools": supplied})
    finally:
        server.close()
    assert direct == encoded == through_mcp
    assert not (tmp_path / "absent").exists()


@pytest.mark.parametrize("invalid", ["unknown", "cycle", "non_json", "oversized", "nonfinite", "deep"])
def test_object_intake_rejects_bad_shapes_without_mutation(invalid):
    supplied = tools_manifest()
    if invalid == "unknown":
        supplied["execute"] = True
    elif invalid == "cycle":
        supplied["tools"] = [supplied]
    elif invalid == "non_json":
        supplied["tools"][0]["formats"] = {"markdown"}
    elif invalid == "oversized":
        supplied["tools"][0]["version"] = "x" * 1_048_577
    elif invalid == "nonfinite":
        supplied["tools"][0]["limits"]["max_requests"] = float("nan")
    else:
        chain = {}
        for _ in range(100):
            chain = {"nested": chain}
        supplied["tools"][0]["scope"] = chain
    with pytest.raises(UsageError):
        read_tools(supplied)


def test_object_input_keeps_defensive_credential_rejection(monkeypatch):
    secret = "synthetic-private-token-value"
    monkeypatch.setenv("SYNTHETIC_TOKEN", secret)
    supplied = tools_manifest()
    supplied["tools"][0]["version"] = secret
    with pytest.raises(UsageError) as error:
        read_tools(supplied)
    assert secret not in str(error.value)
    assert secret not in json.dumps(error.value.details)
