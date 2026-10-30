"""Canonical source reports, explicit bounds and unchanged command effects."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests._script_loader import load_module

SCRIPTS = Path(__file__).resolve().parents[1] / "workspace-template" / "scripts"


@pytest.fixture
def owners():
    from tests._usage_fixture import require_bounded_inventory
    require_bounded_inventory()
    return (load_module("source_inventory_api", SCRIPTS / "source_inventory.py"),
            load_module("normalize_sources_api", SCRIPTS / "normalize_sources.py"))


def workspace(path, *, count=1):
    (path / "raw" / "web").mkdir(parents=True)
    (path / "sources").mkdir()
    config = {"raw": {"source_roots": ["raw/web"]},
              "sources": {"manifest_path": "sources/manifest.jsonl", "normalized_dir": "sources/normalized"}}
    (path / "research.yml").write_text(yaml.safe_dump(config), encoding="utf-8")
    for index in range(count):
        (path / "raw" / "web" / f"source-{index}.html").write_text(
            "<html><title>Local observation</title><body><h1>Evidence</h1><p>" + "Observed content. " * 30 + "</p></body></html>",
            encoding="utf-8",
        )
    return path


def manifest(root):
    return [json.loads(line) for line in (root / "sources" / "manifest.jsonl").read_text().splitlines() if line]


def cli(stem, root, *options):
    return subprocess.run([sys.executable, str(SCRIPTS / (stem + ".py")), "--project-root", str(root), *options],
                          capture_output=True, text=True, check=False)


def test_direct_reports_are_silent_and_return_real_normalized_actions(tmp_path, owners, capsys):
    inventory, normalize = owners
    root = workspace(tmp_path / "ws")
    report = inventory.run_inventory_report(root)
    assert report["total"] == 1 and report["document_type"] == "source_inventory_report"
    source_id = manifest(root)[0]["id"]
    result = normalize.run_normalization_report(root, source_ids=[source_id])
    assert result["summary"]["created"] == 1 and result["actions"][0]["source_id"] == source_id
    assert (root / result["actions"][0]["output"]).is_file()
    assert capsys.readouterr() == ("", "")


def test_json_cli_report_matches_silent_owner_report(tmp_path, owners):
    inventory, normalize = owners
    for_api = workspace(tmp_path / "api")
    for_cli = tmp_path / "cli"
    shutil.copytree(for_api, for_cli)
    api = inventory.run_inventory_report(for_api)
    command = cli("source_inventory", for_cli, "--report", "--format", "json")
    assert command.returncode == 0
    printed = json.loads(command.stdout)
    api.pop("timestamp")
    printed.pop("timestamp")
    assert printed == api
    api = normalize.run_normalization_report(for_api, all_sources=True)
    command = cli("normalize_sources", for_cli, "--all", "--format", "json")
    assert command.returncode == 0
    printed = json.loads(command.stdout)
    api.pop("timestamp")
    printed.pop("timestamp")
    assert printed == api


def test_inventory_cli_renders_before_manifest_write(tmp_path, owners, monkeypatch, capsys):
    inventory, _ = owners
    root = workspace(tmp_path / "ws")
    write = inventory.write_manifest
    observed = []

    def checked(path, records):
        observed.append(json.loads(capsys.readouterr().out)["total"])
        write(path, records)

    monkeypatch.setattr(inventory, "write_manifest", checked)
    args = inventory.parse_args(["--project-root", str(root), "--report", "--format", "json"])
    assert inventory.run_inventory(args) == 0 and observed == [1]


def test_strict_refusal_keeps_admitted_manifest_and_matches_cli_envelope(tmp_path, owners, capsys):
    inventory, _ = owners
    for_api = workspace(tmp_path / "api", count=2)
    raw = for_api / "raw" / "web" / "source-0.html"
    raw.with_name(raw.name + ".provenance.yml").write_text(
        "checksum: sha256:" + hashlib.sha256(raw.read_bytes()).hexdigest() + "\n", encoding="utf-8")
    for_cli = tmp_path / "cli"
    shutil.copytree(for_api, for_cli)
    with pytest.raises(Exception) as caught:
        inventory.run_inventory_report(for_api, require_checksum=True)
    assert caught.value.error_code == "INVENTORY_CHECKSUM_REQUIRED"
    assert len(manifest(for_api)) == 1
    assert capsys.readouterr() == ("", "")
    command = cli("source_inventory", for_cli, "--report", "--format", "json", "--require-checksum")
    assert command.returncode == inventory.EXIT_STRICT_REFUSAL and json.loads(command.stdout)["total"] == 1
    assert json.loads(command.stderr) == caught.value.to_envelope()
    assert len(manifest(for_cli)) == 1


def test_dry_runs_have_no_extraction_manifest_or_log_writes(tmp_path, owners, monkeypatch):
    inventory, normalize = owners
    root = workspace(tmp_path / "ws")
    report = inventory.run_inventory_report(root, dry_run=True, append_log=True)
    assert report["dry_run"] and not (root / "sources" / "manifest.jsonl").exists()
    inventory.run_inventory_report(root)
    before = (root / "sources" / "manifest.jsonl").read_bytes()
    monkeypatch.setattr(normalize, "normalize_selected_record", lambda *a, **k: pytest.fail("extraction"))
    report = normalize.run_normalization_report(root, all_sources=True, dry_run=True, append_log=True)
    assert report["summary"]["would_create"] == 1 and report["dry_run"]
    assert not (root / "sources" / "normalized").exists() and not (root / "log.md").exists()
    assert (root / "sources" / "manifest.jsonl").read_bytes() == before


@pytest.mark.parametrize("selector", [["unknown"], ["unsupported"], ["known", "unknown"]])
def test_bad_selected_ids_refuse_before_any_good_write(tmp_path, owners, selector):
    _, normalize = owners
    root = workspace(tmp_path / "ws")
    records = [{"id": "known", "kind": "html", "raw_paths": ["raw/web/source-0.html"]},
               {"id": "unsupported", "kind": "markdown", "raw_paths": ["raw/web/note.md"]}]
    (root / "sources" / "manifest.jsonl").write_text("".join(json.dumps(row) + "\n" for row in records))
    with pytest.raises(Exception) as caught:
        normalize.run_normalization_report(root, source_ids=selector)
    assert caught.value.error_code in {"SOURCE_UNKNOWN", "SOURCE_NOT_NORMALIZABLE"}
    assert not (root / "sources" / "normalized").exists()


def test_failed_extraction_is_a_returned_report_not_a_fatal_refusal(tmp_path, owners, monkeypatch):
    inventory, normalize = owners
    root = workspace(tmp_path / "ws")
    inventory.run_inventory_report(root)
    monkeypatch.setattr(normalize, "normalize_selected_record", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("local failure")))
    report = normalize.run_normalization_report(root, all_sources=True)
    assert report["summary"]["failed"] == 1 and report["actions"][0]["error"] == "local failure"
    assert not (root / "sources" / "normalized").exists()


def test_changed_direct_input_is_refused_before_its_output_commit(tmp_path, owners, monkeypatch):
    inventory, normalize = owners
    root = workspace(tmp_path / "ws")
    inventory.run_inventory_report(root)
    native = normalize.normalize_selected_record

    def changed(*args, **kwargs):
        source = native(*args, **kwargs)
        (root / "raw" / "web" / "source-0.html").write_text("changed after bounded capture")
        return source

    monkeypatch.setattr(normalize, "normalize_selected_record", changed)
    result = normalize.run_normalization_report(root, all_sources=True)
    assert result["summary"]["failed"] == 1
    assert not (root / "sources" / "normalized").exists()


def test_two_forced_updates_do_not_invalidate_the_owners_own_prior_write(tmp_path, owners):
    inventory, normalize = owners
    root = workspace(tmp_path / "ws", count=2)
    inventory.run_inventory_report(root)
    assert normalize.run_normalization_report(root, all_sources=True)["summary"]["created"] == 2
    result = normalize.run_normalization_report(root, all_sources=True, force=True)
    assert result["summary"]["updated"] == 2 and result["summary"]["failed"] == 0


def test_raw_hash_and_binary_capture_stay_exact_while_native_text_normalizes_newlines(tmp_path, owners):
    inventory, _ = owners
    root = workspace(tmp_path / "ws")
    path = root / "raw" / "web" / "source-0.html"
    raw = b"<html>First\r\nSecond\rThird\n</html>"
    path.write_bytes(raw)
    with inventory._bounded_inputs(root, max_entries=100, max_input_bytes=10_000):
        assert inventory._bounded_bytes(path) == raw
        assert inventory.hash_file_contents(path) == hashlib.sha256(raw).hexdigest()
        assert inventory._bounded_text(path) == path.read_text() == "<html>First\nSecond\nThird\n</html>"
        assert inventory._bounded_bytes(path) == raw


def test_table_binary_reader_preserves_its_legacy_crlf_behavior(tmp_path, owners):
    _, normalize = owners
    root = workspace(tmp_path / "ws")
    path = root / "raw" / "web" / "table.csv"
    raw = b'name,value\r\n"first\r\nsecond",1\r\n'
    path.write_bytes(raw)
    legacy = normalize.read_table_text(path, "raw/web/table.csv")
    with normalize._bounded_inputs(root, max_entries=100, max_input_bytes=10_000):
        assert normalize.read_table_text(path, "raw/web/table.csv") == legacy
        assert normalize._bounded_bytes(path) == raw
    assert "\r\n" in legacy[0]


@pytest.mark.parametrize("bound", ["records", "entries", "bytes"])
def test_inventory_bounds_refuse_before_manifest_write_or_hashing(tmp_path, owners, monkeypatch, bound):
    inventory, _ = owners
    root = workspace(tmp_path / "ws", count=2)
    if bound != "records":
        monkeypatch.setattr(inventory, "hash_file_contents", lambda *a: pytest.fail("hash before bounded work refusal"))
    kwargs = {"records": {"max_records": 1}, "entries": {"max_entries": 1}, "bytes": {"max_input_bytes": 1}}[bound]
    with pytest.raises(Exception) as caught:
        inventory.run_inventory_report(root, **kwargs)
    assert caught.value.error_code in {"INVENTORY_LIMIT", "SOURCE_INPUT_LIMIT"}
    assert not (root / "sources" / "manifest.jsonl").exists()


@pytest.mark.parametrize("bound", ["manifest_bytes", "manifest_records", "sources", "input_bytes"])
def test_normalization_bounds_refuse_before_extraction_or_outputs(tmp_path, owners, monkeypatch, bound):
    inventory, normalize = owners
    root = workspace(tmp_path / "ws", count=2)
    inventory.run_inventory_report(root)
    monkeypatch.setattr(normalize, "normalize_selected_record", lambda *a, **k: pytest.fail("extraction before bound"))
    kwargs = {"manifest_bytes": {"max_manifest_bytes": 1}, "manifest_records": {"max_manifest_records": 1},
              "sources": {"max_sources": 1}, "input_bytes": {"max_input_bytes": 1}}[bound]
    with pytest.raises(Exception) as caught:
        normalize.run_normalization_report(root, all_sources=True, **kwargs)
    assert caught.value.error_code in {"SOURCE_INPUT_LIMIT", "SOURCE_NORMALIZATION_LIMIT"}
    assert not (root / "sources" / "normalized").exists()


@pytest.mark.parametrize("kwargs", [{"max_sources": True}, {"dry_run": 1}, {"source_ids": []},
                                    {"source_ids": "known"}, {"source_ids": ["known"], "all_sources": True},
                                    {"pdf_extractor": "unknown"}, {"max_input_bytes": None}])
def test_invalid_normalization_arguments_are_content_free_refusals(tmp_path, owners, kwargs):
    _, normalize = owners
    with pytest.raises(Exception) as caught:
        normalize.run_normalization_report(tmp_path, **kwargs)
    assert caught.value.error_code == "SOURCE_NORMALIZATION_REQUEST_INVALID"


def test_symlinked_input_is_not_hashed_or_extracted(tmp_path, owners, monkeypatch):
    inventory, normalize = owners
    root = workspace(tmp_path / "ws")
    outside = tmp_path / "outside.html"
    outside.write_text("Private content")
    (root / "raw" / "web" / "linked.html").symlink_to(outside)
    inventory.run_inventory_report(root)
    assert len(manifest(root)) == 1
    (root / "sources" / "manifest.jsonl").write_text(json.dumps({"id": "linked", "kind": "html", "raw_paths": ["raw/web/linked.html"]}) + "\n")
    monkeypatch.setattr(normalize, "normalize_selected_record", lambda *a, **k: pytest.fail("out-of-scope extraction"))
    with pytest.raises(Exception) as caught:
        normalize.run_normalization_report(root, source_ids=["linked"])
    assert caught.value.error_code == "SOURCE_SCOPE_INVALID"


@pytest.mark.parametrize("allow_external", [False, True])
def test_bounded_api_refuses_configured_adapter_transport_before_other_writes(tmp_path, owners, monkeypatch, allow_external):
    inventory, normalize = owners
    root = workspace(tmp_path / "ws")
    config = yaml.safe_load((root / "research.yml").read_text())
    config["normalization"] = {"adapters": [{"kinds": ["structured_data"], "provider": "command", "command": ["missing-tool"],
                                            "name": "external-tool", "version": "1", "timeout_seconds": 10}]}
    (root / "research.yml").write_text(yaml.safe_dump(config))
    (root / "raw" / "web" / "data.json").write_text('{"value":1}')
    inventory.run_inventory_report(root)
    monkeypatch.setattr(normalize, "normalize_selected_record", lambda *a, **k: pytest.fail("external execution"))
    with pytest.raises(Exception) as caught:
        normalize.run_normalization_report(root, all_sources=True, allow_external=allow_external)
    assert caught.value.error_code == "SOURCE_BOUNDED_EXECUTION_UNSUPPORTED"
    assert not (root / "sources" / "normalized").exists()


@pytest.mark.parametrize("kind", ["execution_evidence", "market_evidence"])
def test_nested_closure_routes_refuse_caller_bound_bypass_before_all_writes(tmp_path, owners, monkeypatch, kind):
    _, normalize = owners
    root = workspace(tmp_path / "ws")
    records = [{"id": "native", "kind": "html", "raw_paths": ["raw/web/source-0.html"]},
               {"id": "nested", "kind": kind, "raw_paths": []}]
    (root / "sources" / "manifest.jsonl").write_text("".join(json.dumps(row) + "\n" for row in records))
    monkeypatch.setattr(normalize, "normalize_selected_record", lambda *a, **k: pytest.fail("nested extraction or native write"))
    with pytest.raises(Exception) as caught:
        normalize.run_normalization_report(root, all_sources=True, max_entries=1)
    assert caught.value.error_code == "SOURCE_BOUNDED_EXECUTION_UNSUPPORTED"
    assert not (root / "sources" / "normalized").exists()


def test_inventory_sampling_uses_captured_text_and_top_level_worklist(tmp_path, owners, monkeypatch):
    inventory, _ = owners
    root = workspace(tmp_path / "ws")
    folder = root / "raw" / "web"
    (folder / "links.txt").write_text("https://example.test/declared\r\n")
    (folder / "custom.tex").write_text("\\documentclass{article}\r\n")
    (folder / "nested").mkdir()
    (folder / "nested" / "earlier.tex").write_text("\\documentclass{article}\n")
    with inventory._bounded_inputs(root, max_entries=100, max_input_bytes=10_000):
        inventory._capture_tree(folder)
        with monkeypatch.context() as sampling:
            sampling.setattr(Path, "open", lambda *a, **k: pytest.fail("unanchored sampling reopen"))
            assert inventory.looks_like_link_file(folder / "links.txt", folder)
            assert inventory.fallback_entrypoint(folder) == ("custom.tex", "fallback_documentclass")


@pytest.mark.parametrize("dry_run", [False, True])
def test_direct_raw_root_child_insertion_invalidates_report_before_return(tmp_path, owners, monkeypatch, dry_run):
    inventory, _ = owners
    root = workspace(tmp_path / "ws")
    build = inventory._build_records_unlocked

    def insert(*args, **kwargs):
        result = build(*args, **kwargs)
        (root / "raw/web/inserted.html").write_text("New unobserved direct child")
        return result

    monkeypatch.setattr(inventory, "_build_records_unlocked", insert)
    with pytest.raises(Exception) as caught:
        inventory.run_inventory_report(root, dry_run=dry_run)
    assert caught.value.error_code == "SOURCE_INPUT_LIMIT"
    assert not (root / "sources/manifest.jsonl").exists()
