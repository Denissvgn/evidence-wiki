"""Retained HTML upgrades and failed refreshes preserve classification boundaries.

Real older-producer records remain blocked until native normalization. Publication
faults cannot expose temporary records as current, and recovery retries only the
unfinished source. Missing originals cannot acquire a completed-classification stamp.
"""

import contextlib
import io
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

import pytest

from evidence_wiki.cli import main
from evidence_wiki.source_inspection import inspect
from tests.test_html_usability_profile import (
    CONTRACT,
    CURRENT,
    NORMALIZE,
    edit_metadata,
    normalize_command,
    workspace_bytes,
)
from tests.test_html_usability_profile import html_workspace as html_workspace

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("selector", ["pending", "all", "single", "multiple", "force"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_retained_classification_refreshes_once_without_rewriting_originals(tmp_path, selector, dry_run):
    fixture = json.loads((ROOT / "tests/fixtures/html-usability/prior-classification-records.json").read_text())
    assert fixture["producer"]["html_usability_version"] == 1 and CURRENT == 2
    root = tmp_path / "workspace"
    with contextlib.redirect_stdout(io.StringIO()):
        assert main(["init", "--target", str(root), "--project-name", "prior-classification",
                     "--project-description", "Refresh retained native HTML classifications."]) == 0
    for relative, content in fixture["files"].items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))
    cases = fixture["cases"]
    selected = cases[:1] if selector == "single" else cases[:2] if selector == "multiple" else cases
    if selector == "pending":
        options = []
    elif selector in {"all", "force"}:
        options = ["--all", *(["--force"] if selector == "force" else [])]
    else:
        options = [value for case in selected for value in ("--source-id", case["source_id"])]
    before = workspace_bytes(root)
    for case in cases:
        observed = inspect(target=root, source_ids=[case["source_id"]])["sources"][0]
        assert observed["usability"] == "not_ready" and "html_usability_recheck_required" in observed["reasons"]
    assert workspace_bytes(root) == before
    code, report = normalize_command(root, *options, *(["--dry-run"] if dry_run else []))
    assert code == 0 and report["summary"]["stale"] == len(selected)
    after = workspace_bytes(root)
    if dry_run:
        assert before == after and report["summary"]["would_update"] == len(selected)
        return
    assert {name for name in before if before[name] != after[name]} == {case["normalized_path"] for case in selected}
    for case in selected:
        path = root / case["normalized_path"]
        old, _, error = CONTRACT.split_record(fixture["files"][case["normalized_path"]])
        assert error is None
        metadata = NORMALIZE.read_output_frontmatter(path)
        assert metadata["html_usability_version"] == CURRENT
        assert metadata["raw_fingerprint"] == old["raw_fingerprint"] and metadata["created"] == old["created"]
        assert (metadata["unusable_evidence_reasons"] or []) == case["expected_reasons"]
        assert metadata["evidence_usable"] is (not case["expected_reasons"])
        if fixture["producer"]["python"] == platform.python_version() or case["name"] in {"useful-control", "gateway-control"}:
            assert metadata["content_hash"] == case["content_hash"]
    replay_before = workspace_bytes(root)
    code, replay = normalize_command(root, *[value for case in selected for value in ("--source-id", case["source_id"])])
    assert code == 0 and replay["summary"]["skipped_existing"] == len(selected)
    assert workspace_bytes(root) == replay_before


def test_retained_producer_records_upgrade_through_public_owners(tmp_path):
    cli = Path(sys.executable).with_name("evidence-wiki.exe" if os.name == "nt" else "evidence-wiki")
    result = subprocess.run([
        sys.executable, "-I", str(ROOT / "tools/probe_html_usability.py"), "--cli", str(cli),
        "--root", str(tmp_path / "workspace"), "--corpus", str(ROOT / "tests/fixtures/html-usability/pages.json"),
        "--legacy-fixture", str(ROOT / "tests/fixtures/html-usability/legacy-records.json"),
    ], cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", timeout=300, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["html_upgrade"] == report["future_profile_refusal"] == "passed"
    assert report["fixture_producer"]["package_version"] == "1.1.0" and report["originals_preserved"]
    assert [row["usability"] for row in report["cases"]] == ["not_ready", "not_ready", "usable"]


def test_partial_tool_copy_cannot_refresh_until_the_upgrade_owner_restores_helpers(html_workspace):
    workspace = html_workspace
    source_id = workspace.records["study"]["id"]
    edit_metadata(workspace.paths["study"], remove=("html_usability_version",))
    (workspace.root / "scripts/_html_usability_profile.py").unlink()
    before = workspace_bytes(workspace.root)
    observed = inspect(target=workspace.root, source_ids=[source_id])
    checker = next(row for row in observed["target"]["copied_checkers"] if row["id"] == "normalize_sources")
    assert checker["state"] == "matching_package_bytes"
    assert observed["sources"][0]["usability"] == "not_ready"
    arguments = [sys.executable, "-B", str(workspace.root / "scripts/normalize_sources.py"),
                 "--project-root", str(workspace.root), "--source-id", source_id, "--format", "json"]
    incomplete = subprocess.run(arguments, cwd=workspace.root, capture_output=True, text=True, timeout=60, check=False)
    assert incomplete.returncode != 0 and "_html_usability_profile" in incomplete.stderr
    assert workspace_bytes(workspace.root) == before
    upgraded = subprocess.run([sys.executable, "-I", "-m", "evidence_wiki.cli", "upgrade", "--target", str(workspace.root)],
                              capture_output=True, text=True, timeout=60, check=False)
    assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
    assert (workspace.root / "scripts/_html_usability_profile.py").is_file()
    assert "html_usability_version" not in NORMALIZE.read_output_frontmatter(workspace.paths["study"])
    refreshed = subprocess.run(arguments, cwd=workspace.root, capture_output=True, text=True, timeout=60, check=False)
    assert refreshed.returncode == 0, refreshed.stdout + refreshed.stderr
    assert NORMALIZE.read_output_frontmatter(workspace.paths["study"])["html_usability_version"] == CURRENT


@pytest.mark.parametrize("fault", ["classification", "temporary_write", "publication"])
def test_failed_html_refresh_preserves_the_published_record_and_retries(html_workspace, monkeypatch, fault):
    workspace = html_workspace
    path = workspace.paths["study"]
    source_id = workspace.records["study"]["id"]
    metadata = edit_metadata(path, remove=("html_usability_version",))
    temporary = path.with_name("." + path.name + ".tmp")
    before = workspace_bytes(workspace.root)
    original_write, original_replace = Path.write_text, Path.replace

    def fail_classification(*args, **kwargs):
        raise OSError("Classification interrupted before an observation was returned")

    def fail_write(target, data, *args, **kwargs):
        if target == temporary:
            original_write(target, data[:40], *args, **kwargs)
            raise OSError("Temporary record write failed")
        return original_write(target, data, *args, **kwargs)

    def fail_replace(target, destination):
        if target == temporary:
            raise OSError("Record publication interrupted")
        return original_replace(target, destination)

    with monkeypatch.context() as patch:
        if fault == "classification":
            patch.setattr(NORMALIZE, "normalize_html_record", fail_classification)
        elif fault == "temporary_write":
            patch.setattr(Path, "write_text", fail_write)
        else:
            patch.setattr(Path, "replace", fail_replace)
        code, result = normalize_command(workspace.root, "--source-id", source_id)
    assert code == 1 and result["summary"]["failed"] == 1
    assert path.read_bytes() == before[path.relative_to(workspace.root).as_posix()]
    after = workspace_bytes(workspace.root)
    assert {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)} <= {
        temporary.relative_to(workspace.root).as_posix()}
    row = inspect(target=workspace.root, source_ids=[source_id])["sources"][0]
    assert row["usability"] == "not_ready" and "html_usability_recheck_required" in row["reasons"]
    code, result = normalize_command(workspace.root, "--source-id", source_id)
    assert code == 0 and result["summary"]["updated"] == 1
    fresh = NORMALIZE.read_output_frontmatter(path)
    assert fresh["html_usability_version"] == CURRENT and fresh["content_hash"] == metadata["content_hash"]
    assert not temporary.exists()
    settled = workspace_bytes(workspace.root)
    code, result = normalize_command(workspace.root, "--source-id", source_id)
    assert code == 0 and result["summary"]["skipped_existing"] == 1
    assert workspace_bytes(workspace.root) == settled


def test_interrupted_selected_batch_recovers_only_unfinished_html(html_workspace, monkeypatch):
    workspace = html_workspace
    for name in ("gateway", "study"):
        edit_metadata(workspace.paths[name], remove=("html_usability_version",))
    before = workspace_bytes(workspace.root)
    temporary = workspace.paths["study"].with_name("." + workspace.paths["study"].name + ".tmp")
    original_replace = Path.replace

    def interrupt(target, destination):
        if target == temporary:
            raise KeyboardInterrupt("Interrupted before publishing the second record")
        return original_replace(target, destination)

    selected = ["--source-id", workspace.records["gateway"]["id"], "--source-id", workspace.records["study"]["id"]]
    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", interrupt)
        with pytest.raises(KeyboardInterrupt):
            normalize_command(workspace.root, *selected)
    assert NORMALIZE.read_output_frontmatter(workspace.paths["gateway"])["html_usability_version"] == CURRENT
    study = workspace.paths["study"].relative_to(workspace.root).as_posix()
    assert workspace.paths["study"].read_bytes() == before[study]
    completed = workspace.paths["gateway"].read_bytes()
    code, report = normalize_command(workspace.root, *selected)
    assert code == 0 and report["summary"]["skipped_existing"] == report["summary"]["updated"] == 1
    assert workspace.paths["gateway"].read_bytes() == completed
    after = workspace_bytes(workspace.root)
    changed = {key for key in before if before[key] != after[key]}
    assert changed == {p.relative_to(workspace.root).as_posix() for key, p in workspace.paths.items() if key in {"gateway", "study"}}
    assert not temporary.exists()


def test_missing_original_never_receives_a_current_stamp_and_can_recover(html_workspace):
    workspace = html_workspace
    record = workspace.records["study"]
    path = workspace.paths["study"]
    old = edit_metadata(path, remove=("html_usability_version",))
    original = workspace.root / record["raw_paths"][0]
    retained = original.read_bytes()
    original.unlink()
    before = workspace_bytes(workspace.root)
    code, report = normalize_command(workspace.root, "--source-id", record["id"])
    assert code == 1 and report["summary"]["failed"] == 1
    failed = NORMALIZE.read_output_frontmatter(path)
    assert failed["status"] == "failed" and "html_usability_version" not in failed
    row = inspect(target=workspace.root, source_ids=[record["id"]])["sources"][0]
    assert row["usability"] == "not_ready" and "raw_source_missing" in row["reasons"]
    after = workspace_bytes(workspace.root)
    assert {key for key in before if before[key] != after[key]} == {path.relative_to(workspace.root).as_posix()}
    original.write_bytes(retained)
    code, report = normalize_command(workspace.root, "--source-id", record["id"])
    assert code == 0 and report["summary"]["updated"] == 1
    fresh = NORMALIZE.read_output_frontmatter(path)
    assert fresh["html_usability_version"] == CURRENT and fresh["content_hash"] == old["content_hash"]
    assert fresh["raw_fingerprint"] == old["raw_fingerprint"]
    assert inspect(target=workspace.root, source_ids=[record["id"]])["sources"][0]["usability"] == "usable"
