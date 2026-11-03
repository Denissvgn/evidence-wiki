"""Reproduce a closed validator without executing its producer or leaking imports."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

from evidence_wiki import _script_host

REPO = Path(__file__).resolve().parents[1]


def prepared_generator(tmp_path):
    tool = tmp_path / "vendor_packet_validator.py"
    shutil.copyfile(REPO / "tools/vendor_packet_validator.py", tool)
    spec = tmp_path / "packet-validator-spec.json"
    spec.write_text(json.dumps({"version": "4.0.0", "license_text": "MIT License", "modules": []}))
    producer = tmp_path / "producer/services"
    producer.mkdir(parents=True)
    (producer / "context_packet.py").write_text(
        'raise RuntimeError("Producer execution is forbidden")\n\n'
        'def validate_context_packet(value):\n'
        '    label = "caf\u00e9"\n'
        '    from .rules import accepts\n'
        '    return accepts(value) and label == "caf\u00e9"\n'
    )
    (producer / "rules.py").write_text(
        'from __future__ import annotations\n'
        'from collections.abc import Mapping\n\n'
        'LIMITS = {item: item for item in (1, 2)}\n\n'
        'class Rule:\n'
        '    children: Mapping[str, "Rule"]\n'
        '    parent: "Rule | None"\n\n'
        'def accepts(value: Rule) -> bool:\n'
        '    return type(value) is int and value in LIMITS\n'
    )
    destination = tmp_path / "isolated"
    args = [sys.executable, str(tool), "--producer-root", str(producer.parent), "--destination", str(destination)]
    return spec, producer, destination, args


def test_reviewed_closure_keeps_deferred_imports_in_the_loaded_generation(tmp_path, monkeypatch):
    spec, _producer, destination, args = prepared_generator(tmp_path)
    description = subprocess.run([*args, "--describe"], capture_output=True, text=True, check=True)
    assert not destination.exists()
    rows = json.loads(description.stdout)
    assert [row["module"] for row in rows] == ["llm_wiki_cli.services.context_packet", "llm_wiki_cli.services.rules"]
    pinned = json.loads(spec.read_text())
    pinned["modules"] = [{key: row[key] for key in ("module", "sha256", "symbols")} for row in rows]
    spec.write_text(json.dumps(pinned))
    subprocess.run(args, capture_output=True, text=True, check=True)
    subprocess.run([*args, "--check"], capture_output=True, text=True, check=True)
    loader = _script_host._load_workspace_loader(REPO / "workspace-template/scripts")
    original_path = list(sys.path)
    module = loader.load_workspace_module(destination, "_packet_vendor_services_context_packet", cache={})
    poison = ModuleType("_packet_vendor_services_rules")
    poison.accepts = lambda _value: False
    monkeypatch.setitem(sys.modules, poison.__name__, poison)
    assert sys.path == original_path
    assert module.validate_context_packet(1) is True
    assert module.validate_context_packet(3) is False
    assert sys.modules[poison.__name__] is poison


def test_changed_or_unresolved_producer_never_writes_a_partial_closure(tmp_path):
    spec, producer, destination, args = prepared_generator(tmp_path)
    rows = json.loads(subprocess.run([*args, "--describe"], capture_output=True, text=True, check=True).stdout)
    pinned = json.loads(spec.read_text())
    pinned["modules"] = [{key: row[key] for key in ("module", "sha256", "symbols")} for row in rows]
    spec.write_text(json.dumps(pinned))
    (producer / "rules.py").write_text("def accepts(value):\n    return True\n")
    changed = subprocess.run(args, capture_output=True, text=True)
    assert changed.returncode != 0 and "does not match the pinned validator" in changed.stderr + changed.stdout
    assert not destination.exists()
    (producer / "context_packet.py").write_text("def validate_context_packet(value):\n    return missing_validator(value)\n")
    unresolved = subprocess.run([*args, "--describe"], capture_output=True, text=True)
    assert unresolved.returncode != 0 and "Unresolved validation dependency" in unresolved.stderr + unresolved.stdout
    assert not destination.exists()
