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
    "source_id: sample\ntitle: Ordinary --- title\nexport_eligible: false\n",
    'source_id: sample\ntitle: "--- quoted title"\nretrieval_eligible: false\n',
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


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_indented_yaml_delimiters_cannot_hide_usage_restrictions(consumers, newline):
    _lint, query, _status, gate = consumers
    text = "---\nsource_id: sample\nsummary: |\n  ---\n  Source summary.\nexport_eligible: false\n---\nBody.\n"
    text = text.replace("\n", newline)
    assert query.split_frontmatter(text)[0]["export_eligible"] is False
    assert gate.bytes_have_claims("sources/normalized/sample.md", text.encode(), {})


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


@pytest.mark.parametrize("text", [
    "computation: {mode: declared, graphs: {}}\n",
    '"\\u0063omputation": {}\ncomputation: null\n',
    "computation: {mode: declared, mode: null}\n",
    "outside: &same [value]\ncopy: *same\ncomputation: {}\n",
    "computation: {first: &same [value], second: *same}\n",
    "computation: &cycle {nested: *cycle}\n",
    "computation: {<<: {mode: declared}}\n",
    "computation: {[key]: value}\n",
    "%YAML 1.3\n---\ncomputation: {}\n",
    'computation: {label: "literal\ttab"}\n',
    "computation: [\n",
    "computation: *undefined\n",
])
def test_computation_node_inspection_preserves_portable_refusals(reader, monkeypatch, text):
    contract = load_script("yaml_computation_contract", "_computation_contract.py")
    def outcome():
        try:
            contract.validate_yaml(text)
        except contract.ComputationInvalid as error:
            return str(error)
        return "accepted"
    monkeypatch.setattr(contract, "compose_current", lambda value: yaml.compose(value, Loader=yaml.SafeLoader))
    expected = outcome()
    monkeypatch.setattr(contract, "compose_current", reader.compose_current)
    assert outcome() == expected


def test_composed_nodes_are_fresh_and_keep_alias_identity(reader):
    first = reader.compose_current("first: &same [original]\nsecond: *same\n")
    assert first.value[0][1] is first.value[1][1]
    first.value[0][1].value[0].value = "changed"
    current = reader.compose_current(b"first: &same [original]\nsecond: *same\n")
    assert current.value[0][1] is current.value[1][1]
    assert current.value[0][1].value[0].value == "original"


@pytest.mark.parametrize("raw", [
    b"origin: https://example.org/source\nrights: {status: allowed, license: null}\n",
    'title: "caf\u00e9"\ncount: 007\nobserved: 2026-08-09\n'.encode(),
    b"value: &once [first, second]\n",
    b"first: &same [value]\nsecond: *same\n",
    b"first: &same {nested: *same}\n",
    b"rights: {status: allowed, status: restricted}\n",
    b'"\\u0061": first\na: second\n',
    b"? [one, two]\n: invalid key\n",
    b"value: !!python/object/apply:builtins.eval ['1 + 1']\n",
    b"value: !unknown false\n",
    b"value: [\n",
    b"value: *undefined\n",
    b'quote: "literal\ttab"\n',
    b"%YAML 1.2\n---\nvalue: current\n",
    b"%YAML 1.3\n---\nvalue: current\n",
    b"[not, a, mapping]\n",
    b"value: " + b"[" * 12 + b"0" + b"]" * 12 + b"\n",
    b"value:\n" + b"- item\n" * 4096,
])
def test_capture_sidecars_preserve_portable_values_and_refusals(reader, monkeypatch, raw):
    capture = load_script("yaml_capture_sidecar", "_host_capture.py")

    def outcome():
        try:
            return "value", repr(capture.sidecar(raw))
        except (ValueError, TypeError, RecursionError, UnicodeError) as error:
            return type(error).__name__, str(error)

    monkeypatch.setattr(capture, "SafeLoader", yaml.SafeLoader)
    expected = outcome()
    monkeypatch.setattr(capture, "SafeLoader", reader.SafeLoader)
    assert outcome() == expected


def test_capture_sidecar_reads_are_fresh_and_detached(reader, monkeypatch):
    capture = load_script("yaml_capture_fresh_sidecar", "_host_capture.py")
    monkeypatch.setattr(capture, "SafeLoader", reader.SafeLoader)
    first = capture.sidecar(b"items: [original]\n")
    first["items"][0] = "changed"
    assert capture.sidecar(b"items: [original]\n") == {"items": ["original"]}
    assert capture.sidecar(b"items: [current]\n") == {"items": ["current"]}
