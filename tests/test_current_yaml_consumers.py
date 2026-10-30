"""Current owner readers retain portable values, refusals and fresh input reads."""

import sys
from pathlib import Path

import pytest
import yaml

from tests._script_loader import load_isolated_module
from tests.test_yaml_safe import reader as reader_fixture

OVERLAY = ('domain_pack: {name: sample, version: "1.0", compatible_research_yml_contract: "1.0"}\n'
           "project: {}\nraw: {}\nsources: {}\nwiki: {}\ntaxonomy: {}\ningest: {}\nlint: {}\noutputs: {}\nintegrations: {}\n")
ROUTES = ("lifecycle_mapping", "overlay", "health", "status", "smoke", "coverage_config", "coverage_mapping")


@pytest.fixture(params=["accelerated", "portable"])
def reader(request, monkeypatch):
    return reader_fixture.__wrapped__(request, monkeypatch)


@pytest.fixture
def consumers(reader, monkeypatch):
    monkeypatch.setitem(sys.modules, "_yaml_safe", reader)
    scripts = Path(__file__).resolve().parents[1] / "workspace-template/scripts"
    return {name: load_isolated_module("fresh_consumer_" + name, scripts / (name + ".py")) for name in (
        "_domain_pack_lifecycle", "_workspace_health", "workspace_status", "smoke_validate_workspace", "coverage_manifest")}


def call(consumers, route, root):
    path = root / "research.yml"
    if route == "lifecycle_mapping":
        return consumers["_domain_pack_lifecycle"].load_mapping(path, "state")
    if route == "overlay":
        return consumers["_domain_pack_lifecycle"]._load_overlay_content(path.read_bytes(), "overlay")
    if route == "health":
        findings = []
        result = consumers["_workspace_health"]._load_config(root, findings)
        assert not findings
        return result
    if route == "status":
        return consumers["workspace_status"].load_yaml_mapping(path, "config")
    if route == "smoke":
        results = {"issues": []}
        result = consumers["smoke_validate_workspace"].load_yaml_file(root, "research.yml", "config", results)
        assert not results["issues"]
        return result
    if route == "coverage_config":
        return consumers["coverage_manifest"].load_config(root)
    return consumers["coverage_manifest"].load_yaml_mapping(path, error_code="COVERAGE_MANIFEST_INVALID")


def document(block):
    if block.startswith("%"):
        header, body = block.split("---\n", 1)
        return header + "---\n" + OVERLAY + body
    return OVERLAY + block


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("block", [
    'value: "007"\nactive: "on"\n',
    'value: "café \\u263a"\n',
    "value: 007\nactive: yes\nmissing: null\n",
    "value: 2026-10-01\nobserved: 2026-10-01T12:00:00Z\n",
    "defaults: &policy {export_eligible: false}\nvalue: {<<: *policy}\n",
    'value: !!str 007\nsummary: |\n  ---\n  Scoped statement.\n',
    "%YAML 1.0\n---\nvalue: 1\n",
    "%YAML 1.3\n---\nvalue: 1\n",
    "%OTHER ignored\n---\nvalue: 1\n",
])
def test_fresh_owner_value_matches_portable_reader(consumers, tmp_path, route, block):
    text = document(block)
    (tmp_path / "research.yml").write_text(text, encoding="utf-8")
    assert call(consumers, route, tmp_path) == yaml.safe_load(text)


@pytest.mark.parametrize("route", ROUTES)
def test_repeated_current_read_observes_changed_bytes_and_detaches_values(consumers, tmp_path, route):
    path = tmp_path / "research.yml"
    path.write_text(document("value: [original]\n"), encoding="utf-8")
    result = call(consumers, route, tmp_path)
    result["value"].append("caller edit")
    assert call(consumers, route, tmp_path)["value"] == ["original"]
    path.write_text(document("value: [current]\n"), encoding="utf-8")
    assert call(consumers, route, tmp_path)["value"] == ["current"]


@pytest.mark.parametrize("block", [
    "value: !!python/object/apply:builtins.eval ['1+1']\n",
    "value: !!python/name:builtins.eval ''\n",
    "value: !unknown blocked\n",
    "value: [unfinished\n",
    "value: *undefined\n",
    "value: [one,\t two]\n",
    "value:\t blocked\n",
    "value: {first: one,\t second: two}\n",
    "value: plain\t scalar\n",
])
def test_each_current_consumer_preserves_owner_refusal_class(consumers, tmp_path, block):
    text = document(block)
    with pytest.raises(yaml.YAMLError):
        yaml.safe_load(text)
    path = tmp_path / "research.yml"
    path.write_text(text, encoding="utf-8")
    lifecycle = consumers["_domain_pack_lifecycle"]
    with pytest.raises(lifecycle.LifecycleFailure) as rejected:
        call(consumers, "lifecycle_mapping", tmp_path)
    assert rejected.value.error_code == "DOMAIN_PACK_STATE_INVALID"
    with pytest.raises(lifecycle.LifecycleFailure) as rejected:
        call(consumers, "overlay", tmp_path)
    assert rejected.value.error_code == "DOMAIN_PACK_INVALID"
    findings = []
    assert consumers["_workspace_health"]._load_config(tmp_path, findings) == {}
    assert findings[0]["code"] == "RESEARCH_CONFIG_INVALID"
    with pytest.raises(SystemExit):
        call(consumers, "status", tmp_path)
    results = {"issues": []}
    assert consumers["smoke_validate_workspace"].load_yaml_file(tmp_path, "research.yml", "config", results) is None
    assert results["issues"][0]["category"] == "yaml"
    coverage = consumers["coverage_manifest"]
    for route, code in (("coverage_config", "CONFIG_INVALID"), ("coverage_mapping", "COVERAGE_MANIFEST_INVALID")):
        with pytest.raises(coverage.CoverageManifestError) as rejected:
            call(consumers, route, tmp_path)
        assert rejected.value.error_code == code
