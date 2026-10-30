"""Caller-pinned capture bytes retain legacy qualifications and shared budgets."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests._script_loader import load_module
from tests.test_host_capture import profile
from tests.test_source_owner_api import manifest, workspace
from tests.test_source_owner_api import owners as owners

SCRIPTS = Path(__file__).resolve().parents[1] / "workspace-template" / "scripts"


def deposit(root):
    data = b"Exact host capture\r\nwith original newline bytes.\n"
    value = profile(data)
    path = root / "raw" / "web" / "capture.md"
    path.write_bytes(data)
    sidecar = {"host_capture": value, "checksum": value["content_sha256"], "origin_url": value["origin_url"],
               "retrieved_at": value["retrieved_at"], "license": value["rights"]["license"]}
    path.with_name(path.name + ".provenance.yml").write_text(yaml.safe_dump(sidecar))
    record = {"id": "capture", "kind": "host_capture", "raw_paths": ["raw/web/capture.md"], "provenance": sidecar}
    return data, record


def test_private_reader_reuses_exact_raw_and_sidecar_without_changing_legacy_result(tmp_path, owners, monkeypatch):
    inventory, _ = owners
    capture = load_module("host_capture_reader", SCRIPTS / "_host_capture.py")
    root = workspace(tmp_path / "ws")
    raw, record = deposit(root)
    legacy = capture.inspect_record(root, record)
    original_read = inventory.read_observed_file
    observed = []

    def read(*args):
        observed.append(args[1])
        return original_read(*args)

    monkeypatch.setattr(inventory, "read_observed_file", read)
    with inventory._bounded_inputs(root, max_entries=100, max_input_bytes=10_000):
        for _ in range(2):
            assert capture.inspect_record(root, record, read_input=inventory._capture_input_reader) == legacy
        assert inventory._bounded_bytes(root / "raw/web/capture.md") == raw
    assert observed == ["raw/web/capture.md", "raw/web/capture.md.provenance.yml"]


def test_public_capture_normalization_counts_sidecar_before_any_source_write(tmp_path, owners, monkeypatch):
    inventory, normalize = owners
    root = workspace(tmp_path / "ws")
    raw, record = deposit(root)
    (root / "sources" / "manifest.jsonl").write_text(json.dumps(record) + "\n")
    allowance = ((root / "research.yml").stat().st_size + (root / "sources" / "manifest.jsonl").stat().st_size + len(raw) + 64)
    monkeypatch.setattr(normalize, "normalize_selected_record", lambda *a, **k: pytest.fail("extraction before sidecar cap"))
    with pytest.raises(Exception) as caught:
        normalize.run_normalization_report(root, all_sources=True, max_input_bytes=allowance)
    assert caught.value.error_code == "SOURCE_INPUT_LIMIT"
    assert not (root / "sources" / "normalized").exists()
    assert inventory._INPUT_LIMITS.get() is None


def test_inventory_and_normalization_complete_public_capture_path_with_one_snapshot(tmp_path, owners):
    inventory, normalize = owners
    root = workspace(tmp_path / "ws")
    raw, _ = deposit(root)
    report = inventory.run_inventory_report(root)
    records = manifest(root)
    captured = next(row for row in records if row["kind"] == "host_capture")
    assert report["total"] == 2 and captured["provenance"]["checksum_verified"]
    result = normalize.run_normalization_report(root, source_ids=[captured["id"]])
    assert result["summary"]["created"] == 1 and result["summary"]["failed"] == 0
    text = (root / result["actions"][0]["output"]).read_text()
    assert "Exact host capture" in text
    assert (root / "raw/web/capture.md").read_bytes() == raw


def test_cold_process_public_capture_normalization_uses_no_standalone_source_imports(tmp_path):
    root = workspace(tmp_path / "ws")
    raw, _ = deposit(root)
    program = """
import json
import sys
from evidence_wiki import Workspace
assert 'source_inventory' not in sys.modules
with Workspace.open(sys.argv[1]) as ws:
    inventory = ws.sources.inventory()
    report = ws.normalize.sources(all_sources=True)
assert 'source_inventory' not in sys.modules
print(json.dumps({'total': inventory['total'], 'created': report['summary']['created'],
                  'failed': report['summary']['failed'], 'errors': [row.get('error') for row in report['actions']]}))
"""
    result = subprocess.run([sys.executable, "-c", program, str(root)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["total"] == 2 and report["created"] == 2 and report["failed"] == 0
    assert report["errors"] == [None, None]
    assert (root / "raw/web/capture.md").read_bytes() == raw
