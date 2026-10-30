"""Complete installed discovery remains distinct from bounded response pages."""

from copy import deepcopy

import pytest

from evidence_wiki import agent, agent_resources
from evidence_wiki._agent_catalog import resource_paths
from evidence_wiki.errors import UsageError
from evidence_wiki.onboarding import Onboarding
from evidence_wiki.onboarding_contract import encode_document
from evidence_wiki.onboarding_mcp import OnboardingMcpServer
from evidence_wiki.onboarding_schemas import LIMITS, schema_document


def test_default_pages_reveal_the_exact_complete_installed_inventory():
    first = agent_resources.resource_index()
    second = agent_resources.resource_index(offset=128)
    identifiers = [row["id"] for row in first["resources"] + second["resources"]]
    assert len(first["resources"]) == LIMITS["resources"] == 128
    assert first["bounds"] == {"total": len(resource_paths()), "returned": 128, "truncated": True}
    assert 128 < len(identifiers) <= agent_resources.MAX_INSTALLED_RESOURCES
    assert identifiers == sorted(set(identifiers), key=lambda value: value.encode("utf-8"))
    assert set(identifiers) == set(resource_paths())
    assert second["bounds"] == {"total": len(identifiers), "returned": len(identifiers) - 128, "truncated": True}
    end = agent_resources.resource_index(offset=len(identifiers))
    assert end["resources"] == [] and end["bounds"]["truncated"] is True


@pytest.mark.parametrize("arguments", [
    {"offset": True}, {"offset": -1}, {"offset": 0.0}, {"offset": "0"},
    {"limit": True}, {"limit": 0}, {"limit": -1}, {"limit": 129}, {"limit": 1.5}, {"limit": "64"},
])
def test_page_parameters_require_exact_bounded_integers(arguments):
    with pytest.raises(UsageError):
        agent_resources.resource_index(**arguments)


def test_capabilities_advertise_all_schema_ids_and_bounded_metadata():
    summary = agent.capabilities()
    complete = agent_resources._resource_metadata()
    assert len(summary["resources"]) == agent.SUMMARY_RESOURCE_PAGE_SIZE == 64
    assert summary["resource_bounds"] == {"total": len(complete), "returned": 64, "truncated": True}
    schemas = {row["id"] for row in complete if row["media_type"] == "application/schema+json"}
    assert set(summary["schema_ids"]) == schemas
    tail_schemas = schemas - {row["id"] for row in summary["resources"]}
    assert tail_schemas
    agent._negotiate(summary, [sorted(tail_schemas)[0]], "artifact_checked")
    assert summary["strict"]["checker"]["available"] is True
    assert "onboarding/capabilities/v2" in schemas and "onboarding/bootstrap/v3" in schemas
    document = {"schema_version": "2.0", "kind": "capabilities", "request_id": "summary", "payload": summary}
    assert len(encode_document("onboarding/capabilities/v2", document)) + 1 <= agent.SUMMARY_BYTES


def test_bootstrap_anchor_metadata_does_not_depend_on_summary_page(tmp_path, monkeypatch):
    original = agent.capabilities

    def later_page():
        summary = original()
        summary["resources"] = agent_resources.resource_index(offset=128, limit=64)["resources"]
        summary["resource_bounds"]["returned"] = len(summary["resources"])
        assert "guide/bootstrap/v1" not in {row["id"] for row in summary["resources"]}
        return summary

    monkeypatch.setattr(agent, "capabilities", later_page)
    bootstrap = agent.bootstrap(str(tmp_path))
    assert bootstrap["guide"]["id"] == "guide/bootstrap/v1"
    assert bootstrap["strict_selection"]["instruction"]["sha256"] == bootstrap["guide"]["sha256"]


def test_new_versions_extend_old_shapes_without_changing_old_bounds():
    pairs = (("onboarding/capabilities/v1", "onboarding/capabilities/v2"),
        ("onboarding/bootstrap/v2", "onboarding/bootstrap/v3"))
    for old_id, new_id in pairs:
        old, new = schema_document(old_id), schema_document(new_id)
        assert "resource_bounds" not in old["properties"]["payload"]["properties"]
        restored = deepcopy(new)
        del restored["properties"]["payload"]["properties"]["resource_bounds"]
        restored["properties"]["payload"]["required"].remove("resource_bounds")
        restored["$id"] = old["$id"]
        restored["properties"]["schema_version"] = old["properties"]["schema_version"]
        assert restored == old
        assert old["properties"]["payload"]["properties"]["resources"]["maxItems"] == 128
    assert LIMITS["resources"] == 128


def test_onboarding_defaults_report_bounds_and_support_complete_paging(tmp_path):
    with Onboarding.open(allowed_roots=[tmp_path]) as handle:
        summary = handle.bootstrap(request_id="summary")
        assert summary["schema_version"] == "2.0" and summary["payload"]["resource_bounds"]["truncated"]
        bootstrap = handle.bootstrap(target=str(tmp_path), request_id="target")
        assert bootstrap["schema_version"] == "3.0"
        assert len(encode_document("onboarding/bootstrap/v3", bootstrap)) + 1 <= agent.SUMMARY_BYTES
        assert handle.resources(offset=128, limit=64) == agent_resources.resource_index(offset=128, limit=64)


def initialized_server():
    server = OnboardingMcpServer()
    server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}})
    server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
    return server


def test_mcp_resource_cursor_returns_every_resource_once_and_stops():
    server = initialized_server()
    try:
        first = server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "resources/list"})["result"]
        assert len(first["resources"]) == 128 and "nextCursor" in first
        second = server.handle_message({"jsonrpc": "2.0", "id": 3, "method": "resources/list",
            "params": {"cursor": first["nextCursor"]}})["result"]
        assert "nextCursor" not in second
        assert {row["name"] for row in first["resources"] + second["resources"]} == set(resource_paths())
        assert server.call_tool("onboarding_resources", {"offset": 128, "limit": 64})["bounds"]["total"] == len(resource_paths())
    finally:
        server.close()


@pytest.mark.parametrize("cursor", [None, True, 128, "128", "resources:01", "resources:-1", "resources:257", "resources:" + "1" * 100])
def test_mcp_cursor_is_a_bounded_read_selector(cursor):
    server = initialized_server()
    try:
        response = server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "resources/list", "params": {"cursor": cursor}})
        assert response["error"]["code"] == -32602
    finally:
        server.close()


@pytest.mark.parametrize("operation", ["summary", "bootstrap", "resource"])
def test_cli_paging_options_are_exclusive_to_resource_inventory(operation, capsys):
    arguments = [operation, "--offset", "0", "--format", "json"]
    if operation == "resource":
        arguments.insert(1, "guide/bootstrap/v1")
    assert agent.main(arguments) == 2
    assert "ONBOARDING_INVALID" in capsys.readouterr().out
