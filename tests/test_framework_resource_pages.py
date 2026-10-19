"""Explicit versioned framework pagination preserves legacy wire contracts."""

import hashlib
import json
import sys

import pytest

from evidence_wiki import frameworks
from evidence_wiki._agent_catalog import resource_paths
from evidence_wiki.agent_resources import resource_document
from evidence_wiki.errors import UsageError
from tests.test_frameworks import call
from tests.test_orchestration_contract_schemas import assert_matches_schema


def paged(parameters=None, operation="resources"):
    return {**call(operation, parameters), "schema_version": frameworks.PAGED_CALL_SCHEMA}


def test_legacy_framework_schema_exports_remain_byte_identical():
    hashes = {
        frameworks.CALL_SCHEMA: "6ebe73b23d0cbcab35dbfa27136b2039b532bef614f6dfe494acf3496d8b09e1",
        frameworks.RESULT_SCHEMA: "347b409b3a56173b92114ab30803d5ed18f22cd124316b3423584476800b8319",
    }
    for identifier, digest in hashes.items():
        raw = (json.dumps(frameworks.tool_schemas()[identifier], ensure_ascii=False,
            sort_keys=True, separators=(",", ":")) + "\n").encode()
        assert hashlib.sha256(raw).hexdigest() == digest


@pytest.mark.parametrize("parameters", [{}, {"offset": 0}, {"offset": 1_000_000},
    {"limit": 1}, {"limit": 128}, {"offset": 128, "limit": 64}])
def test_paged_resource_request_accepts_bounded_integer_tokens(parameters):
    request = paged(parameters)
    assert frameworks.decode_call(json.dumps(request).encode()) == request
    assert_matches_schema(request, frameworks.tool_schemas()[frameworks.PAGED_CALL_SCHEMA])


@pytest.mark.parametrize("parameters", [
    {"offset": True}, {"offset": 0.0}, {"offset": "0"}, {"offset": -1}, {"offset": 1_000_001},
    {"limit": False}, {"limit": 1.0}, {"limit": "128"}, {"limit": 0}, {"limit": 129},
    {"resource_id": "guide/bootstrap/v1"}, {"target": "/tmp"}, {"argv": []},
])
def test_paged_resource_request_refuses_widened_or_noninteger_parameters(parameters):
    with pytest.raises(UsageError):
        frameworks.decode_call(json.dumps(paged(parameters)).encode())


@pytest.mark.parametrize("version, operation", [
    (frameworks.CALL_SCHEMA, "resources"), (frameworks.CALL_SCHEMA, "bootstrap"),
    (frameworks.PAGED_CALL_SCHEMA, "bootstrap"), (frameworks.PAGED_CALL_SCHEMA, "resource"),
    (frameworks.PAGED_CALL_SCHEMA, "computation_check"),
])
def test_paging_parameters_require_explicit_version_and_resources_operation(version, operation):
    request = {**call(operation, {"offset": 0, "limit": 1}), "schema_version": version}
    with pytest.raises(UsageError):
        frameworks.decode_call(json.dumps(request).encode())


def test_paged_framework_calls_map_integer_selectors_to_native_cli_arguments(tmp_path, monkeypatch):
    from evidence_wiki import orchestration

    arguments = []
    native = '{"schema_version":"1.0","kind":"resources","payload":{"resources":[]}}\n'

    def execute(argv, **options):
        arguments.append(argv)
        return orchestration.ProcessResult(0, native, "")

    monkeypatch.setattr(orchestration, "_execute_bounded", execute)
    result = frameworks.invoke(json.dumps(paged({"offset": 128, "limit": 64})).encode(), target=tmp_path)
    assert arguments == [[str(sys.executable), "-m", "evidence_wiki.cli", "agent", "resources",
        "--format", "json", "--offset", "128", "--limit", "64"]]
    assert result["schema_version"] == frameworks.RESULT_SCHEMA
    assert result["result_json"] == native


def test_paged_framework_discovery_reaches_the_complete_installed_inventory(tmp_path):
    identifiers, offset = [], 0
    while True:
        result = frameworks.invoke(json.dumps(paged({"offset": offset, "limit": 64})).encode(), target=tmp_path)
        assert result["exit_code"] == 0 and result["status"] == "completed"
        assert_matches_schema(result, frameworks.tool_schemas()[frameworks.RESULT_SCHEMA])
        page = json.loads(result["result_json"])["payload"]
        identifiers.extend(row["id"] for row in page["resources"])
        returned, total = page["bounds"]["returned"], page["bounds"]["total"]
        assert returned == len(page["resources"]) <= 64
        assert page["bounds"]["truncated"] is (returned < total)
        offset += returned
        if offset >= total:
            break
        assert returned > 0
    assert len(identifiers) == len(set(identifiers)) == total
    assert set(identifiers) == set(resource_paths())


def test_portable_bundle_exposes_paged_request_only_as_an_explicit_schema():
    bundle = json.loads(resource_document("framework/bundle/v1")["content"])
    assert json.loads(bundle["files"]["tool-call.schema.json"]) == frameworks.tool_schemas()[frameworks.CALL_SCHEMA]
    assert json.loads(bundle["files"]["tool-call-paged.schema.json"]) == frameworks.tool_schemas()[frameworks.PAGED_CALL_SCHEMA]
    assert '"../tool-call.schema.json"' in bundle["files"]["pi/evidence-wiki.js"]
