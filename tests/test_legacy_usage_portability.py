"""Legacy consumers retain permission checks on every supported desktop platform."""

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from evidence_wiki import Workspace
from evidence_wiki.errors import SourceError
from tests._script_loader import load_isolated_module

SCRIPTS = Path(__file__).resolve().parents[1] / "workspace-template/scripts"


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    monkeypatch.delenv("EVIDENCE_WIKI_STATE_DIR", raising=False)
    (tmp_path / "research.yml").write_bytes(b"project: {}\n")
    source = tmp_path / "sources/normalized/sample.md"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"---\r\nsource_id: sample\r\n---\r\nOrdinary research.\r\n")
    return tmp_path, source


def test_legacy_export_and_cache_accept_unrestricted_source_bytes(legacy):
    root, source = legacy
    original = source.read_bytes()
    assert Workspace.open(root).export_answers()["questions"] == []
    query = load_isolated_module("portable_legacy_query", SCRIPTS / "query_index.py")
    query.write_fts_index(root, {}, "all", root / "index.sqlite")
    assert (root / "index.sqlite").is_file()
    assert source.read_bytes() == original


@pytest.mark.parametrize("declaration", ["export_eligible: false", "retrieval_eligible: false", "market_profile: demo"])
def test_legacy_export_and_cache_still_refuse_explicit_declarations(legacy, declaration):
    root, source = legacy
    source.write_bytes(f"---\r\nsource_id: sample\r\n{declaration}\r\n---\r\nRestricted.\r\n".encode())
    with pytest.raises(SourceError) as caught:
        Workspace.open(root).export_answers()
    assert caught.value.details["reason"] == "explicit_usage_requires_host_authorization"
    query = load_isolated_module("portable_restricted_query", SCRIPTS / "query_index.py")
    with pytest.raises(SystemExit):
        query.write_fts_index(root, {}, "all", root / "index.sqlite")
    assert not (root / "index.sqlite").exists()


def test_legacy_reader_does_not_enable_unsupported_coherent_capture(legacy, monkeypatch):
    root, source = legacy
    revision = load_isolated_module("portable_capture_boundary", SCRIPTS / "_evidence_revision.py")
    monkeypatch.setattr(os, "supports_dir_fd", set())
    with pytest.raises(revision.ScriptRefusal) as caught:
        revision.read_observed_file(root, "sources/normalized/sample.md", revision.observation(source.stat()))
    assert caught.value.error_code == "EVIDENCE_REVISION_UNSUPPORTED"


def test_windows_legacy_read_failure_never_falls_back_to_a_different_reader(legacy, monkeypatch):
    root, _source = legacy
    gate = load_isolated_module("portable_usage_gate", SCRIPTS / "_usage_gate.py")
    monkeypatch.setattr(gate, "sys", SimpleNamespace(platform="win32"))

    def unavailable(*args):
        raise OSError("sharing violation")

    def unexpected(*args):
        pytest.fail("A failed Windows read must not fall back to another reader")

    monkeypatch.setattr(gate, "read_legacy_file", unavailable)
    monkeypatch.setattr(gate, "read_observed_file", unexpected)
    with pytest.raises(gate.EvidenceInvalid, match="usage_workspace_file_unavailable"):
        gate.read_workspace_file(root, "sources/normalized/sample.md", legacy=True)

    # Protected consumers still select the coherent reader even on Windows.
    monkeypatch.setattr(gate, "read_legacy_file", unexpected)
    monkeypatch.setattr(gate, "read_observed_file", unavailable)
    with pytest.raises(gate.EvidenceInvalid, match="usage_workspace_file_unavailable"):
        gate.read_workspace_file(root, "sources/normalized/sample.md")


@pytest.mark.parametrize("unsafe", ["hardlink", "oversized"])
def test_legacy_exports_refuse_unsafe_source_files(legacy, unsafe):
    root, source = legacy
    if unsafe == "hardlink":
        (root / "duplicate.md").hardlink_to(source)
    else:
        with source.open("ab") as handle:
            handle.truncate(16 * 1024 * 1024 + 1)
    with pytest.raises(SourceError) as caught:
        Workspace.open(root).export_answers()
    assert caught.value.details["reason"] == "unsafe_usage_workspace_file"


@pytest.mark.parametrize("declaration", [b'{"export_eligible":false}', b'{"retrieval_eligible":false}'])
def test_legacy_exports_inspect_crlf_manifest_declarations(legacy, declaration):
    root, _source = legacy
    (root / "sources/manifest.jsonl").write_bytes(declaration + b"\r\n")
    with pytest.raises(SourceError) as caught:
        Workspace.open(root).export_answers()
    assert caught.value.details["reason"] == "explicit_usage_requires_host_authorization"


def test_windows_observation_accepts_distinct_path_and_descriptor_clocks(legacy, monkeypatch):
    root, source = legacy
    native = load_isolated_module("portable_windows_clocks", SCRIPTS / "_windows_files.py")
    expected = native.observation(source.lstat())
    original_fstat = os.fstat

    def descriptor_change_time(descriptor):
        info = original_fstat(descriptor)
        fields = {name: getattr(info, name) for name in (
            "st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns",
        )}
        return SimpleNamespace(**fields, st_ctime_ns=expected[-1] + 100)

    monkeypatch.setattr(os, "fstat", descriptor_change_time)
    with source.open("rb") as handle:
        observed = native.checked_file_observation(source, handle.fileno(), expected)
    assert observed[-1] == expected[-1] + 100


def test_windows_observation_refuses_a_different_file_descriptor(legacy):
    root, source = legacy
    native = load_isolated_module("portable_windows_descriptor", SCRIPTS / "_windows_files.py")
    replacement = root / "replacement.md"
    replacement.write_bytes(source.read_bytes())
    expected = native.observation(source.lstat())
    with replacement.open("rb") as handle:
        with pytest.raises(native.EvidenceInvalid, match="usage_workspace_changed"):
            native.checked_file_observation(source, handle.fileno(), expected)


def test_windows_observation_still_checks_path_change_time(legacy, monkeypatch):
    root, source = legacy
    native = load_isolated_module("portable_windows_path_clock", SCRIPTS / "_windows_files.py")
    info = source.lstat()
    expected = native.observation(info)
    fields = {name: getattr(info, name) for name in (
        "st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns",
    )}
    monkeypatch.setattr(Path, "lstat", lambda self: SimpleNamespace(**fields, st_ctime_ns=expected[-1] + 100))
    with source.open("rb") as handle:
        with pytest.raises(native.EvidenceInvalid, match="usage_workspace_changed"):
            native.checked_file_observation(source, handle.fileno(), expected)


@pytest.mark.skipif(sys.platform != "win32", reason="Exercises native Windows handle sharing")
def test_windows_reader_holds_files_and_ancestors_until_read_finishes(legacy, monkeypatch):
    root, source = legacy
    native = load_isolated_module("portable_windows_files", SCRIPTS / "_windows_files.py")
    original_read = os.read
    attempts = []

    def competing_changes(descriptor, count):
        for path in (source, source.parent, root):
            with pytest.raises(OSError):
                path.rename(path.with_name(path.name + "-moved"))
            attempts.append(path)
        with pytest.raises(OSError):
            with source.open("wb"):
                pass
        return original_read(descriptor, count)

    monkeypatch.setattr(os, "read", competing_changes)
    expected = source.read_bytes()
    data = native.read_legacy_file(root, "sources/normalized/sample.md", native.observation(source.stat()), 1024)
    assert data == expected
    assert attempts
    # Every handle is released after success; later workspace edits can proceed.
    source.write_bytes(b"replacement")
    source.parent.rename(source.parent.with_name("renamed"))


@pytest.mark.skipif(sys.platform != "win32", reason="Exercises native Windows file identity")
@pytest.mark.parametrize("failure", ["changed", "oversized", "writer"])
def test_windows_reader_refuses_unsafe_reads_and_releases_handles(legacy, failure):
    root, source = legacy
    native = load_isolated_module("portable_windows_files", SCRIPTS / "_windows_files.py")
    expected = native.observation(source.stat())
    if failure == "changed":
        source.write_bytes(b"changed")
    if failure == "writer":
        with source.open("ab"):
            with pytest.raises(OSError):
                native.read_legacy_file(root, "sources/normalized/sample.md", expected, 1024)
    else:
        with pytest.raises(native.EvidenceInvalid):
            native.read_legacy_file(root, "sources/normalized/sample.md", expected, 1 if failure == "oversized" else 1024)
    source.unlink()
    source.parent.rename(source.parent.with_name("renamed"))


@pytest.mark.skipif(sys.platform != "win32", reason="Exercises native Windows junction refusal")
def test_windows_legacy_readers_refuse_junction_ancestors(legacy):
    root, source = legacy
    native = load_isolated_module("portable_windows_files", SCRIPTS / "_windows_files.py")
    junction = root / "linked"
    subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(junction), str(source.parent)],
                   check=True, capture_output=True)
    try:
        with pytest.raises(native.EvidenceInvalid):
            native.read_legacy_file(root, "linked/sample.md", native.observation(source.stat()), 1024)
        (root / "research.yml").write_bytes(b"sources:\n  normalized_dir: linked\n")
        with pytest.raises(SourceError) as caught:
            Workspace.open(root).export_answers()
        assert caught.value.details["reason"] == "unsafe_usage_workspace_file"
    finally:
        junction.rmdir()
