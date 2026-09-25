"""Portable contracts, authority boundaries and canonical operation conformance."""

import copy
import hashlib
import json
import sys

import pytest

from evidence_wiki import frameworks
from evidence_wiki._filesystem import os
from evidence_wiki.agent_resources import resource_document
from evidence_wiki.errors import UsageError
from tests.test_orchestration_contract_schemas import assert_matches_schema


def call(operation="bootstrap", parameters=None):
    return {"schema_version": frameworks.CALL_SCHEMA, "request_id": "fixture-call", "operation": operation,
            "instruction_sha256": resource_document("guide/bootstrap/v1")["sha256"], "parameters": parameters or {}}


def test_native_calls_preserve_canonical_json_and_scope(tmp_path):
    request = call()
    result = frameworks.invoke(json.dumps(request).encode(), target=tmp_path, python=sys.executable)
    assert_matches_schema(request, frameworks.tool_schemas()[frameworks.CALL_SCHEMA])
    assert_matches_schema(result, frameworks.tool_schemas()[frameworks.RESULT_SCHEMA])
    assert result["exit_code"] == 0
    assert json.loads(result["result_json"])["payload"]["workspace"] == "absent"
    assert result["result_sha256"] == hashlib.sha256(result["result_json"].encode()).hexdigest()
    assert result["evidence_acceptance"] == "not_evaluated" and not result["host_enforced"]
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("mutation", [
    {"operation": "shell"}, {"parameters": {"target": "../private"}}, {"parameters": {"argv": ["--force"]}},
    {"instruction_sha256": "0" * 64}, {"schema_version": "2"}, {"operation": []}, {"parameters": []},
])
def test_native_call_refuses_widening_or_stale_instructions(tmp_path, mutation):
    with pytest.raises(UsageError):
        frameworks.invoke(json.dumps({**call(), **mutation}).encode(), target=tmp_path)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("raw", [b"{}{}", b'{}\n{}', b'{"x":1,"x":2}', b'{"x":NaN}', b'"value"', b"[]", b"\xff"])
def test_native_input_requires_one_bounded_json_object(raw):
    with pytest.raises(UsageError):
        frameworks.decode_call(raw)


def test_portable_bundle_is_derived_and_collision_safe(tmp_path):
    bundle = json.loads(resource_document("framework/bundle/v1")["content"])
    frameworks.validate_bundle(bundle)
    assert bundle["files"]["skills/evidence-wiki/references/bootstrap.md"] == resource_document("guide/bootstrap/v1")["content"]
    target = tmp_path / "caller-assets"
    if os.open not in os.supports_dir_fd or os.rename not in os.supports_dir_fd:
        with pytest.raises(UsageError):
            frameworks.export_bundle(target)
        return
    assert frameworks.export_bundle(target)["status"] == "created"
    assert not (tmp_path / ".pi").exists()
    for name, digest in bundle["sha256"].items():
        assert hashlib.sha256((target / name).read_bytes()).hexdigest() == digest
    with pytest.raises(UsageError):
        frameworks.export_bundle(target)
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(UsageError):
        frameworks.export_bundle(alias)


@pytest.mark.parametrize("mutation", ["name", "description", "metadata", "traversal", "digest"])
def test_portable_validation_is_stricter_than_permissive_harness_loading(mutation):
    bundle = copy.deepcopy(json.loads(resource_document("framework/bundle/v1")["content"]))
    path = "skills/evidence-wiki/SKILL.md"
    if mutation == "name":
        bundle["files"][path] = bundle["files"][path].replace("name: evidence-wiki", "name: elsewhere")
    elif mutation == "description":
        bundle["files"][path] = bundle["files"][path].replace("description: ", "description: " + "x" * 1025)
    elif mutation == "metadata":
        bundle["files"][path] = bundle["files"][path].replace('version: "1.0"', 'version: 1')
    elif mutation == "traversal":
        bundle["files"]["../elsewhere"] = "data"
    else:
        bundle["files"]["skills/evidence-wiki/references/bootstrap.md"] += "changed"
    bundle["sha256"] = {key: hashlib.sha256(value.encode()).hexdigest() for key, value in bundle["files"].items()}
    with pytest.raises(UsageError):
        frameworks.validate_bundle(bundle)


def test_unknown_framework_modes_and_versions_do_not_negotiate():
    for kwargs in ({"framework": "pi", "version": "0.1", "mode": "canonical_fixture"},
                   {"framework": "pi", "version": "0.87.0", "mode": "host_enforced"},
                   {"framework": "a-model-name", "version": "0.87.0", "mode": "canonical_fixture"}):
        with pytest.raises(UsageError):
            frameworks.compatibility(**kwargs)
    with pytest.raises(UsageError) as error:
        frameworks.compatibility(framework="pi", version="0.87.0", mode="canonical_fixture", platform_id="unobserved")
    assert error.value.details["field"] == "framework_platform_unqualified"
    assert frameworks.compatibility(framework="pi", version="0.87.0", mode="canonical_fixture", platform_id="darwin-arm64")["status"] == "supported"


def test_bundle_publication_never_follows_a_replaced_destination(tmp_path, monkeypatch):
    target, moved, outside = tmp_path / "bundle", tmp_path / "moved", tmp_path / "outside"
    outside.mkdir()
    original = os.rename
    swapped = False

    def replace(source, destination, **options):
        nonlocal swapped
        if options.get("dst_dir_fd") is not None and not swapped:
            swapped = True
            original(target, moved)
            target.symlink_to(outside, target_is_directory=True)
        return original(source, destination, **options)

    # Capability checks use the original function identity on this platform.
    monkeypatch.setattr(os, "supports_dir_fd", {*os.supports_dir_fd, replace})
    monkeypatch.setattr(os, "rename", replace)
    with pytest.raises(UsageError) as error:
        frameworks.export_bundle(target)
    assert error.value.details["field"] == "bundle_destination_changed"
    assert not list(outside.iterdir())


def test_native_stdout_preserves_data_that_looks_like_diagnostics(tmp_path):
    from evidence_wiki.orchestration import _execute_bounded

    expected = '{"note":"Authorization: Bearer fixture-token","value":"0.3000000000000000001"}\n'
    result = _execute_bounded([sys.executable, "-c", "import sys;sys.stdout.buffer.write(" + repr(expected.encode()) + ")"],
                              cwd=tmp_path, stdin_text="", timeout_seconds=5, preserve_stdout=True)
    assert result.stdout == expected
    with pytest.raises(UnicodeError):
        _execute_bounded([sys.executable, "-c", "import sys;sys.stdout.buffer.write(b'\\xff')"],
                         cwd=tmp_path, stdin_text="", timeout_seconds=5, preserve_stdout=True)


def test_transport_success_cannot_create_an_unstructured_strict_acceptance(tmp_path, monkeypatch):
    from evidence_wiki import orchestration

    monkeypatch.setattr(orchestration, "_execute_bounded", lambda *args, **kwargs: orchestration.ProcessResult(0, '{"verdict":"ship"}', ""))
    with pytest.raises(UsageError):
        frameworks.invoke(json.dumps(call("strict_export")).encode(), target=tmp_path)
