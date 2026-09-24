"""Portable captures, Windows handle epochs and unchanged refusal boundaries."""

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests._script_loader import load_isolated_module

SCRIPTS = Path(__file__).resolve().parents[1] / "workspace-template/scripts"


@pytest.fixture
def revision():
    return load_isolated_module("portable_revision", SCRIPTS / "_evidence_revision.py")


def test_capture_includes_hidden_controls_and_materializes_exact_bytes(tmp_path, revision):
    controls = tmp_path / "domain-packs/.evidence-wiki-state.yml"
    controls.parent.mkdir()
    controls.write_bytes(b"state: retained\r\n")
    (tmp_path / "research.yml").write_bytes(b"project: {}\n")
    captured = revision.capture_workspace(tmp_path)
    assert captured.files["domain-packs/.evidence-wiki-state.yml"] == controls.read_bytes()
    with captured.materialize() as root:
        assert (root / "domain-packs/.evidence-wiki-state.yml").read_bytes() == controls.read_bytes()
    assert not root.exists()
    controls.write_bytes(b"state: changed\n")
    assert revision.capture_workspace(tmp_path).revision_id != captured.revision_id


def test_windows_capture_pins_every_file_through_the_closing_scan(tmp_path, revision, monkeypatch):
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_bytes(name.encode())
    before = revision.observe_tree(tmp_path)
    held = set()
    read = os.read
    observed = revision.observe_tree

    @contextmanager
    def pin(root, relative, expected, maximum):
        with (root / relative).open("rb") as stream:
            held.add(relative)
            try:
                yield stream.fileno()
            finally:
                held.remove(relative)

    def pinned_read(descriptor, size):
        assert held == {"a.txt", "b.txt"}
        return read(descriptor, size)

    def closing_scan(root):
        assert held == {"a.txt", "b.txt"}
        return observed(root)

    native = SimpleNamespace(open_observed_file=pin, EvidenceInvalid=ValueError)
    monkeypatch.setattr(revision, "load_workspace_module", lambda *_args: native)
    monkeypatch.setattr(os, "read", pinned_read)
    monkeypatch.setattr(revision, "observe_tree", closing_scan)
    assert revision.capture_windows_files(tmp_path, before) == {"a.txt": b"a.txt", "b.txt": b"b.txt"}
    assert not held


def test_windows_capture_releases_earlier_pins_when_a_later_file_refuses(tmp_path, revision, monkeypatch):
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_bytes(b"data")
    held = set()

    @contextmanager
    def pin(root, relative, expected, maximum):
        if relative == "b.txt":
            raise ValueError("usage_workspace_changed")
        held.add(relative)
        try:
            yield -1
        finally:
            held.remove(relative)

    monkeypatch.setattr(revision, "load_workspace_module", lambda *_args: SimpleNamespace(
        open_observed_file=pin, EvidenceInvalid=ValueError))
    with pytest.raises(revision.ScriptRefusal) as error:
        revision.capture_windows_files(tmp_path, revision.observe_tree(tmp_path))
    assert error.value.error_code == "EVIDENCE_REVISION_CHANGED" and not held


@pytest.mark.parametrize("relative", ["../private", "folder/../private", "a\\b", "C:/private", "a:stream", "CON.txt", "a/", "a."])
def test_windows_control_paths_do_not_relax_traversal_or_alias_rules(relative):
    native = load_isolated_module("portable_windows_paths", SCRIPTS / "_windows_files.py")
    with pytest.raises(native.EvidenceInvalid):
        native.validate_relative_path(relative)
    native.validate_relative_path("domain-packs/.evidence-wiki-state.yml")


@pytest.mark.skipif(sys.platform != "win32", reason="Requires native Windows sharing and path metadata")
def test_windows_native_capture_holds_other_files_and_ancestors_while_reading(tmp_path, revision, monkeypatch):
    for name in ("a.txt", "b.exe"):
        (tmp_path / name).write_bytes(b"retained")
    original_read = os.read
    attempts = []

    def read(descriptor, size):
        for name in ("a.txt", "b.exe"):
            with pytest.raises(OSError):
                (tmp_path / name).write_bytes(b"changed")
        with pytest.raises(OSError):
            tmp_path.rename(tmp_path.with_name(tmp_path.name + "-moved"))
        attempts.append(True)
        return original_read(descriptor, size)

    monkeypatch.setattr(os, "read", read)
    assert dict(revision.capture_workspace(tmp_path).files) == {"a.txt": b"retained", "b.exe": b"retained"}
    assert attempts
    (tmp_path / "b.exe").write_bytes(b"released")
