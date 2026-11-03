"""Settled unchanged files reuse earlier capture bytes only behind their complete observation."""

import os
import time
from types import SimpleNamespace

import pytest

from evidence_wiki.pack_discovery import owner

FIELDS = ("device", "inode", "mode", "links", "size", "mtime", "ctime")


@pytest.fixture
def revision():
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        pytest.skip("No-follow descriptor capture unavailable")
    return owner("_evidence_revision")


def workspace(root):
    (root / "nested").mkdir(parents=True)
    (root / "nested/one.txt").write_bytes(b"one\n")
    (root / "two.txt").write_bytes(b"two\n")
    return root


def counted_reads(monkeypatch, revision):
    reads = []
    original = revision.read_observed_file

    def read(root, relative, *args):
        reads.append(relative)
        return original(root, relative, *args)

    monkeypatch.setattr(revision, "read_observed_file", read)
    return reads


def capture_started_at(monkeypatch, revision, started):
    """Start one capture at a chosen wall time; file times stay the kernel's own."""
    monkeypatch.setattr(revision, "time", SimpleNamespace(time_ns=lambda: started))


def later_ctime(path, before, rewrite):
    """Coarse kernel clocks repeat a ctime within one tick; rewrite until it moves."""
    deadline = time.monotonic() + 5
    while True:
        rewrite()
        if path.stat().st_ctime_ns != before.st_ctime_ns or time.monotonic() > deadline:
            return path.stat()
        time.sleep(0.01)


def test_settled_unchanged_files_reuse_earlier_bytes(tmp_path, revision, monkeypatch):
    monkeypatch.setattr(revision, "SETTLED_NS", 0)
    root = workspace(tmp_path / "workspace")
    first = revision.capture_workspace(root)
    reads = counted_reads(monkeypatch, revision)
    second = revision.capture_workspace(root)
    assert reads == []
    assert dict(second.files) == dict(first.files) == {"nested/one.txt": b"one\n", "two.txt": b"two\n"}
    assert second.revision_id == first.revision_id


def test_same_size_rewrite_with_restored_mtime_is_reread(tmp_path, revision, monkeypatch):
    monkeypatch.setattr(revision, "SETTLED_NS", 0)
    root = workspace(tmp_path / "workspace")
    path = root / "nested/one.txt"
    revision.capture_workspace(root)
    original = path.stat()

    def rewrite():
        path.write_bytes(b"ONE\n")
        os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))

    changed = later_ctime(path, original, rewrite)
    assert (changed.st_size, changed.st_mtime_ns, changed.st_ino) == (original.st_size, original.st_mtime_ns, original.st_ino)
    assert changed.st_ctime_ns != original.st_ctime_ns
    reads = counted_reads(monkeypatch, revision)
    captured = revision.capture_workspace(root)
    assert reads == ["nested/one.txt"]
    assert captured.files["nested/one.txt"] == b"ONE\n"


def test_inode_swap_with_restored_size_and_mtime_is_reread(tmp_path, revision, monkeypatch):
    monkeypatch.setattr(revision, "SETTLED_NS", 0)
    root = workspace(tmp_path / "workspace")
    path = root / "nested/one.txt"
    revision.capture_workspace(root)
    original = path.stat()
    replacement = root / "nested/replacement"
    replacement.write_bytes(b"ONE\n")
    os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
    os.replace(replacement, path)
    swapped = path.stat()
    assert (swapped.st_size, swapped.st_mtime_ns) == (original.st_size, original.st_mtime_ns)
    assert swapped.st_ino != original.st_ino
    reads = counted_reads(monkeypatch, revision)
    captured = revision.capture_workspace(root)
    assert reads == ["nested/one.txt"]
    assert captured.files["nested/one.txt"] == b"ONE\n"


@pytest.mark.parametrize("field", range(len(FIELDS)), ids=FIELDS)
def test_any_differing_observation_field_forces_a_read(tmp_path, revision, monkeypatch, field):
    monkeypatch.setattr(revision, "SETTLED_NS", 0)
    root = workspace(tmp_path / "workspace")
    revision.capture_workspace(root)
    actual = revision.observe_tree

    def observed(target):
        result = actual(target)
        info = list(result["two.txt"])
        info[field] += 1
        result["two.txt"] = tuple(info)
        return result

    monkeypatch.setattr(revision, "observe_tree", observed)
    reads = counted_reads(monkeypatch, revision)
    # The live file no longer matches the altered observation, so the forced read refuses.
    with pytest.raises(revision.ScriptRefusal) as refused:
        revision.capture_workspace(root)
    assert refused.value.error_code == "EVIDENCE_REVISION_CHANGED"
    assert "two.txt" in reads


@pytest.mark.parametrize("remembered", [False, True], ids=["inside", "outside"])
def test_files_changed_inside_the_settle_window_are_never_remembered(tmp_path, revision, monkeypatch, remembered):
    assert revision.SETTLED_NS == 2_000_000_000
    root = workspace(tmp_path / "workspace")
    times = [max(info.st_mtime_ns, info.st_ctime_ns) for info in ((root / "nested/one.txt").stat(), (root / "two.txt").stat())]
    # Just outside the window for the newest file, or just inside it for the oldest one.
    started = max(times) + revision.SETTLED_NS + 1 if remembered else min(times) + revision.SETTLED_NS - 1
    with monkeypatch.context() as clock:
        capture_started_at(clock, revision, started)
        revision.capture_workspace(root)
    reads = counted_reads(monkeypatch, revision)
    revision.capture_workspace(root)
    assert sorted(reads) == ([] if remembered else ["nested/one.txt", "two.txt"])


def test_remembered_roots_are_bounded(tmp_path, revision, monkeypatch):
    monkeypatch.setattr(revision, "SETTLED_NS", 0)
    monkeypatch.setattr(revision, "SETTLED_MAX_ROOTS", 2)
    roots = [workspace(tmp_path / name) for name in ("first", "second", "third")]
    for root in roots:
        revision.capture_workspace(root)
    reads = counted_reads(monkeypatch, revision)
    revision.capture_workspace(roots[2])
    assert reads == []
    revision.capture_workspace(roots[0])
    assert sorted(reads) == ["nested/one.txt", "two.txt"]
