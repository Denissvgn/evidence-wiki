"""Accelerated and portable YAML readers preserve frontmatter and usage refusals."""

import builtins

import pytest
import yaml

from tests._script_loader import load_script, load_script_uncached


@pytest.fixture(params=["accelerated", "portable"])
def reader(request, monkeypatch):
    if request.param == "accelerated":
        if not hasattr(yaml, "CSafeLoader"):
            pytest.skip("PyYAML was built without LibYAML")
        return load_script("accelerated_yaml_reader", "_yaml_safe.py")

    original_import = builtins.__import__

    def without_libyaml(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "yaml" and "CSafeLoader" in (fromlist or ()):
            raise ImportError("LibYAML is unavailable")
        return original_import(name, globals, locals, fromlist, level)

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "__import__", without_libyaml)
        return load_script_uncached("portable_yaml_reader", "_yaml_safe.py")


@pytest.fixture
def consumers(reader, monkeypatch):
    modules = [load_script(f"yaml_consumer_{name}", f"{name}.py") for name in (
        "lint", "query_index", "workspace_status", "_usage_gate",
    )]
    for module in modules:
        monkeypatch.setattr(module, "safe_load", reader.safe_load)
    return modules


@pytest.mark.parametrize("block", [
    'source_id: "007"\ntitle: "café \\u263a"\nsource_ids: ["on", "null", "2026-08-09"]\n',
    "source_id: sample\ncreated: 2026-08-09\ncount: 007\nreviewed: yes\nmissing: null\n",
    "source_id: sample\ndefaults: &policy {export_eligible: false}\nmetadata: {<<: *policy}\n",
    "source_id: sample\nprovenance: {retrieval_eligible: false}\n",
    "source_id: sample\nmarket_profile: demo\nsummary: |\n  First line.\n  Second line.\n",
])
def test_frontmatter_values_and_usage_claims_match_portable_yaml(reader, consumers, tmp_path, block):
    lint, query, status, gate = consumers
    assert reader.safe_load(block) == yaml.safe_load(block)
    expected = yaml.safe_load(block.rstrip("\n"))
    text = f"---\n{block}---\nBody.\n"
    path = tmp_path / "sample.md"
    path.write_text(text, encoding="utf-8")
    assert lint.load_frontmatter(path) == (expected, None)
    assert query.split_frontmatter(text) == (expected, "Body.\n")
    assert status.load_frontmatter(path) == expected
    present = gate.claims([expected], ["retrieval", "export"])[2] or gate.requires_authority([expected])
    assert gate.bytes_have_claims("sources/normalized/sample.md", text.encode(), {}) is present


@pytest.mark.parametrize("block", [
    "export_eligible: !!python/object/apply:builtins.eval ['1 + 1']\n",
    "export_eligible: !!python/name:builtins.eval ''\n",
    "export_eligible: !unknown false\n",
    "export_eligible: [false\n",
    "export_eligible: *undefined\n",
])
def test_invalid_or_object_tagged_claims_cannot_enable_legacy_access(reader, consumers, tmp_path, block):
    lint, query, status, gate = consumers
    with pytest.raises(yaml.YAMLError):
        reader.safe_load(block)
    text = f"---\n{block}---\nBody.\n"
    path = tmp_path / "sample.md"
    path.write_text(text, encoding="utf-8")
    frontmatter, error = lint.load_frontmatter(path)
    assert frontmatter is None
    assert error.startswith("invalid YAML frontmatter:")
    assert query.split_frontmatter(text) == ({}, "Body.\n")
    assert status.load_frontmatter(path) == {}
    with pytest.raises(gate.UsageRefusal) as caught:
        gate.bytes_have_claims("sources/normalized/sample.md", text.encode(), {})
    assert caught.value.details["reason"] == "usage_declarations_unreadable"


def test_each_parse_observes_current_bytes_and_returns_independent_values(reader, consumers, tmp_path):
    lint, query, status, gate = consumers
    original = "---\nsource_ids: [sample]\n---\nBody.\n"
    path = tmp_path / "sample.md"
    path.write_text(original, encoding="utf-8")
    frontmatter, error = lint.load_frontmatter(path)
    assert error is None
    frontmatter["source_ids"].append("mutated")
    assert lint.load_frontmatter(path) == ({"source_ids": ["sample"]}, None)
    assert not gate.bytes_have_claims("sources/normalized/sample.md", path.read_bytes(), {})

    updated = "---\nsource_ids: [replacement]\nexport_eligible: false\n---\nBody.\n"
    path.write_text(updated, encoding="utf-8")
    expected = {"source_ids": ["replacement"], "export_eligible": False}
    assert lint.load_frontmatter(path) == (expected, None)
    assert query.split_frontmatter(updated)[0] == expected
    assert status.load_frontmatter(path) == expected
    assert gate.bytes_have_claims("sources/normalized/sample.md", path.read_bytes(), {})
    assert reader.safe_load(b"value: [original]") == {"value": ["original"]}
