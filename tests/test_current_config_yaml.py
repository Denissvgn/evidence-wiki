"""Fresh configuration parsing preserves portable values and owner refusals."""

import shutil
from pathlib import Path

import pytest
import yaml

from tests._script_loader import load_isolated_module
from tests.test_yaml_safe import reader as _reader_fixture


@pytest.fixture(params=["accelerated", "portable"])
def reader(request, monkeypatch):
    return _reader_fixture.__wrapped__(request, monkeypatch)


@pytest.fixture
def owners(reader, monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / "workspace-template/scripts"
    loaded = {name: load_isolated_module("current_yaml_" + name, scripts / (name + ".py")) for name in
        ("_strict_evidence", "_caller_context", "_evidence_policies")}
    for owner in loaded.values():
        monkeypatch.setattr(owner, "safe_load", reader.safe_load_current)
    return loaded


@pytest.mark.parametrize("block", [
    'value: "007"\nquoted: "on"\n',
    "value: 007\nactive: yes\nmissing: null\n",
    "value: 2026-10-01\nobserved: 2026-10-01T12:00:00Z\n",
    "defaults: &policy {export_eligible: false}\nvalue: {<<: *policy}\n",
    'value: !!str 007\nsummary: |\n  ---\n  Scoped statement.\n',
    "%YAML 1.0\n---\nvalue: 1\n",
    "%YAML 1.3\n---\nvalue: 1\n",
    "%OTHER ignored\n---\nvalue: 1\n",
])
def test_current_config_and_frontmatter_match_portable_values(owners, tmp_path, block, monkeypatch):
    expected = yaml.safe_load(block)
    config = tmp_path / "research.yml"
    config.write_text(block, encoding="utf-8")
    policies = owners["_evidence_policies"]
    assert policies.load_config(tmp_path) == expected
    assert policies.load_yaml_mapping(config) == expected
    assert owners["_strict_evidence"].configuration(tmp_path) == expected
    path = tmp_path / "source.md"
    path.write_text("---\n" + block + "---\nBody.\n", encoding="utf-8")
    actual = policies.read_frontmatter(path)
    with monkeypatch.context() as patch:
        patch.setattr(policies, "safe_load", yaml.safe_load)
        assert actual == policies.read_frontmatter(path)


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
def test_current_owner_refusal_classes_match_without_unsafe_construction(owners, tmp_path, block):
    path = tmp_path / "research.yml"
    path.write_text(block, encoding="utf-8")
    policies = owners["_evidence_policies"]
    for call in (lambda: policies.load_config(tmp_path), lambda: owners["_strict_evidence"].configuration(tmp_path),
                 lambda: owners["_caller_context"].capture(tmp_path)):
        with pytest.raises(yaml.YAMLError):
            call()
    assert policies.load_yaml_mapping(path) == {}
    path = tmp_path / "source.md"
    path.write_text("---\n" + block + "---\nBody.\n", encoding="utf-8")
    assert policies.read_frontmatter(path) == {}
    strict = owners["_strict_evidence"]
    with pytest.raises(strict.ScriptRefusal) as caught:
        strict.machine_compatibility(tmp_path)
    assert caught.value.error_code == "STRICT_EVIDENCE_REFUSED"
    assert caught.value.details["reason"] == "strict_input_invalid_or_unreadable"


def test_each_owner_read_observes_changed_config_without_retaining_views(owners, tmp_path):
    policies = owners["_evidence_policies"]
    strict = owners["_strict_evidence"]
    path = tmp_path / "research.yml"
    for raw, value in (("value: [original]\n", "original"), ("value: [replacement]\n", "replacement")):
        path.write_text(raw, encoding="utf-8")
        for actual in (policies.load_config(tmp_path), strict.configuration(tmp_path), policies.load_yaml_mapping(path)):
            assert actual == {"value": [value]}
            actual["value"].append("caller-mutation")
        assert policies.load_config(tmp_path) == {"value": [value]}
        assert strict.configuration(tmp_path) == {"value": [value]}


def test_caller_capture_keeps_fresh_configuration_bytes_in_binding(owners, tmp_path):
    root = tmp_path / "workspace"
    shutil.copytree(Path(__file__).resolve().parents[1] / "workspace-template", root)
    caller = owners["_caller_context"]
    before = caller.capture(root)
    path = root / "research.yml"
    path.write_text(path.read_text(encoding="utf-8") + "\nparser_observation: fresh\n", encoding="utf-8")
    after = caller.capture(root)
    assert before["files"]["research.yml"] != after["files"]["research.yml"]
    assert before["context_id"] != after["context_id"]


@pytest.mark.parametrize("raw", [
    'value: "\\uD800"\n',
    'value: "\\U00110000"\n',
    "%YAML 1.0\n---\nvalue: 1\n",
    "%OTHER ignored\n---\nvalue: 1\n",
    "value: [one,\t two]\n",
])
@pytest.mark.parametrize("encoding", ["text", "bytes"])
def test_current_parser_preserves_portable_acceptance_and_exception_class(reader, raw, encoding):
    stream = raw if encoding == "text" else raw.encode("utf-8")
    try:
        expected = yaml.safe_load(stream)
    except Exception as error:
        with pytest.raises(type(error)):
            reader.safe_load_current(stream)
    else:
        assert reader.safe_load_current(stream) == expected


def test_current_ordinary_input_uses_fresh_accelerated_parse(reader, monkeypatch):
    calls = []
    original = reader.safe_load

    def counted(stream):
        calls.append(stream)
        return original(stream)

    monkeypatch.setattr(reader, "safe_load", counted)
    first = reader.safe_load_current("value: [original]\n")
    first["value"].append("mutated")
    assert reader.safe_load_current("value: [original]\n") == {"value": ["original"]}
    assert reader.safe_load_current("value: [replacement]\n") == {"value": ["replacement"]}
    assert len(calls) == 3


@pytest.fixture
def additional_owners(reader,monkeypatch):
    scripts=Path(__file__).resolve().parents[1]/"workspace-template/scripts"
    loaded={name:load_isolated_module("fresh_yaml_"+name,scripts/(name+".py")) for name in
        ("_selected_publication","_normalized_contract","_pack_revision_guard","_pack_revision_impact",
         "question_status","source_inventory")}
    for owner in loaded.values(): monkeypatch.setattr(owner,"safe_load",reader.safe_load_current)
    return loaded


@pytest.mark.parametrize("stream",[
    'value: "007"\nactive: yes\nmissing: null\n',
    "value: 2026-10-01\nobserved: 2026-10-01T12:00:00Z\n",
    "defaults: &policy {accepted: false}\nvalue: {<<: *policy}\n",
    "value: [one,\t two]\n",
    "%YAML 1.0\n---\nvalue: 1\n",
    "%OTHER ignored\n---\nvalue: 1\n",
    'value: "\\uD800"\n',
    "value: *undefined\n",
    "value: !unknown blocked\n",
    "value: !!python/object/apply:builtins.eval ['1+1']\n",
    "value: [unfinished\n",
])
@pytest.mark.parametrize("encoding",["text","bytes"])
def test_additional_fresh_owner_parsers_keep_portable_values_and_refusal_classes(additional_owners,stream,encoding):
    raw=stream if encoding=="text" else stream.encode("utf-8")
    try: expected=yaml.safe_load(raw)
    except Exception as error:
        for owner in additional_owners.values():
            with pytest.raises(type(error)): owner.safe_load(raw)
    else:
        for owner in additional_owners.values(): assert owner.safe_load(raw)==expected


def test_actual_config_and_normalized_consumers_observe_fresh_inputs(additional_owners,tmp_path):
    path=tmp_path/"research.yml"
    for name in ("question_status","source_inventory"):
        owner=additional_owners[name]
        path.write_text("value: [original]\n",encoding="utf-8")
        first=owner.load_config(tmp_path)
        first["value"].append("caller-mutation")
        assert owner.load_config(tmp_path)=={"value":["original"]}
        path.write_text("value: [replacement]\n",encoding="utf-8")
        assert owner.load_config(tmp_path)=={"value":["replacement"]}
    normalized=additional_owners["_normalized_contract"]
    for value in ("original","replacement"):
        actual,body,error=normalized.split_record("---\nvalue: ["+value+"]\n---\nBody.\n")
        assert actual=={"value":[value]} and body=="Body.\n" and error is None
    actual,_,error=normalized.split_record("---\nvalue: [one,\t two]\n---\nBody.\n")
    assert actual is None and error.startswith("invalid YAML frontmatter:")
