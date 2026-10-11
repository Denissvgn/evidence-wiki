"""One byte capture pins bounded directory handles without retaining live trust."""

import os
import stat

import pytest

from evidence_wiki.pack_discovery import owner


@pytest.fixture
def revision():
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        pytest.skip("No-follow descriptor capture unavailable")
    return owner("_evidence_revision")


def tree(root, *, directories=3, files=4):
    root.mkdir()
    for number in range(directories):
        parent = root / str(number) / "nested"
        parent.mkdir(parents=True)
        for index in range(files):
            (parent / f"{index}.txt").write_bytes(f"{number}/{index}\r\n".encode())
    return root


def test_pinned_capture_preserves_all_original_bytes_and_revision(tmp_path, revision):
    root = tree(tmp_path / "workspace")
    before = revision.observe_tree(root)
    expected = {path: revision.read_observed_file(root, path, info)
                for path, info in before.items() if stat.S_ISREG(info[2])}
    capture = revision.capture_workspace(root)
    assert dict(capture.files) == expected
    assert capture.revision_id == revision.capture_workspace(root).revision_id
    (root / "0/nested/0.txt").write_bytes(b"Current replacement")
    assert revision.capture_workspace(root).revision_id != capture.revision_id


def test_many_files_reuse_directory_opens_with_bounded_descriptor_peak(tmp_path, revision, monkeypatch):
    root = tree(tmp_path / "workspace", directories=45, files=6)
    actual_open, actual_close = os.open, os.close
    held, counts = set(), {"opened": 0, "peak": 0}

    def opened(path, flags, *args, **kwargs):
        descriptor = actual_open(path, flags, *args, **kwargs)
        if flags & os.O_DIRECTORY:
            held.add(descriptor)
            counts["opened"] += 1
            counts["peak"] = max(counts["peak"], len(held))
        return descriptor

    def closed(descriptor):
        held.discard(descriptor)
        return actual_close(descriptor)

    with monkeypatch.context() as scoped:
        scoped.setattr(os, "supports_dir_fd", os.supports_dir_fd | {opened})
        scoped.setattr(os, "open", opened)
        scoped.setattr(os, "close", closed)
        captured = revision.capture_workspace(root)
    assert len(captured.files) == 270
    assert counts["opened"] == 91
    # Opening the next directory briefly holds it before the LRU eviction.
    assert counts["peak"] <= revision.MAX_PINNED_DIRECTORIES + 2
    assert held == set()


@pytest.mark.parametrize("change", ["file", "ancestor", "root", "symlink", "membership"])
def test_capture_refuses_live_tree_changes_with_pinned_handles(tmp_path, revision, monkeypatch, change):
    root = tree(tmp_path / "workspace", directories=1, files=3)
    original = revision.read_observed_file
    changed = False

    def read(*args):
        nonlocal changed
        result = original(*args)
        if not changed:
            changed = True
            if change == "file":
                (root / "0/nested/0.txt").write_bytes(b"Different bytes after read")
            elif change == "membership":
                (root / "0/nested/added.txt").write_bytes(b"New member")
            elif change == "root":
                root.rename(tmp_path / "old-root")
                tree(root, directories=1, files=3)
            else:
                (root / "0/nested").rename(root / "0/old-nested")
                if change == "symlink":
                    (root / "0/nested").symlink_to(root / "0/old-nested", target_is_directory=True)
                else:
                    (root / "0/nested").mkdir()
                    (root / "0/nested/1.txt").write_bytes(b"Replacement directory")
        return result

    monkeypatch.setattr(revision, "read_observed_file", read)
    with pytest.raises(Exception) as refused:
        revision.capture_workspace(root, max_attempts=1)
    assert refused.value.error_code in {"EVIDENCE_REVISION_CHANGED", "EVIDENCE_REVISION_UNSAFE"}


def test_leaf_changed_during_read_refuses_and_closes_capture_handles(tmp_path, revision, monkeypatch):
    root = tree(tmp_path / "workspace", directories=1, files=1)
    actual = os.read
    changed = False

    def read(descriptor, maximum):
        nonlocal changed
        result = actual(descriptor, maximum)
        if result and not changed:
            changed = True
            (root / "0/nested/0.txt").write_bytes(b"Changed while descriptor was open")
        return result

    monkeypatch.setattr(os, "read", read)
    with pytest.raises(Exception) as refused:
        revision.capture_workspace(root, max_attempts=1)
    assert refused.value.error_code == "EVIDENCE_REVISION_CHANGED"


def test_evicted_directory_change_is_still_in_complete_closing_scan(tmp_path, revision, monkeypatch):
    root = tree(tmp_path / "workspace", directories=45, files=1)
    original = revision.read_observed_file
    calls = 0

    def read(*args):
        nonlocal calls
        result = original(*args)
        calls += 1
        if calls == 40:
            (root / "0/nested/new.txt").write_bytes(b"Change to an evicted directory")
        return result

    monkeypatch.setattr(revision, "read_observed_file", read)
    with pytest.raises(Exception) as refused:
        revision.capture_workspace(root, max_attempts=1)
    assert refused.value.error_code == "EVIDENCE_REVISION_CHANGED"


def test_failed_directory_open_releases_every_retained_descriptor(tmp_path, revision, monkeypatch):
    root = tree(tmp_path / "workspace", directories=3, files=1)
    original = revision._DirectoryPins.parent
    captured = []

    def parent(pins, relative):
        captured.append(pins)
        if relative == "1/nested":
            raise OSError("Directory capture refused")
        return original(pins, relative)

    monkeypatch.setattr(revision._DirectoryPins, "parent", parent)
    with pytest.raises(Exception) as refused:
        revision.capture_workspace(root, max_attempts=1)
    assert refused.value.error_code == "EVIDENCE_REVISION_UNSAFE"
    pins = captured[0]
    assert not pins.handles
    with pytest.raises(OSError):
        os.fstat(pins.root_fd)


@pytest.mark.parametrize("change", ["ancestor_mode", "ancestor_restore", "root_restore"])
def test_cached_child_does_not_hide_changed_ancestor_or_restored_path(tmp_path, revision, change):
    root = tree(tmp_path / "workspace", directories=1, files=1)
    before = revision.observe_tree(root)
    pins = revision._DirectoryPins(root, before)
    try:
        pins.parent("0/nested")
        if change == "ancestor_mode":
            (root / "0").chmod(0o700 if stat.S_IMODE((root / "0").stat().st_mode) != 0o700 else 0o755)
        elif change == "ancestor_restore":
            (root / "0").rename(root / "moved")
            (root / "moved").rename(root / "0")
        else:
            root.rename(tmp_path / "moved-root")
            (tmp_path / "moved-root").rename(root)
        with pytest.raises(Exception) as refused:
            pins.parent("0/nested")
        assert refused.value.error_code == "EVIDENCE_REVISION_CHANGED"
    finally:
        pins.close()


def test_deep_capture_evicts_handles_without_losing_leaf_or_ancestor_binding(tmp_path, revision):
    root = tmp_path / "workspace"
    leaf = root.joinpath(*[str(index) for index in range(40)])
    leaf.mkdir(parents=True)
    (leaf / "observed.txt").write_bytes(b"Exact bounded original")
    captured = revision.capture_workspace(root)
    assert captured.files["/".join(str(index) for index in range(40)) + "/observed.txt"] == b"Exact bounded original"


def test_close_failure_attempts_all_handles_without_retrying_reused_descriptor(tmp_path, revision, monkeypatch):
    root = tree(tmp_path / "workspace", directories=3, files=1)
    pins = revision._DirectoryPins(root, revision.observe_tree(root))
    for index in range(3):
        pins.parent(f"{index}/nested")
    retained = {*pins.handles.values(), pins.root_fd}
    actual_close = os.close
    calls = []
    original_error = OSError("Close failed after releasing descriptor")

    def close(descriptor):
        calls.append(descriptor)
        actual_close(descriptor)
        if len(calls) == 1:
            raise original_error

    monkeypatch.setattr(os, "close", close)
    with pytest.raises(OSError) as refused:
        pins.close()
    assert refused.value is original_error
    assert set(calls) == retained and len(calls) == len(retained)
    pins.close()
    assert len(calls) == len(retained)
    assert not pins.handles and pins.closed


def test_eviction_failure_removes_closed_descriptor_before_final_cleanup(tmp_path, revision, monkeypatch):
    root = tree(tmp_path / "workspace", directories=18, files=1)
    pins = revision._DirectoryPins(root, revision.observe_tree(root))
    for index in range(16):
        pins.parent(f"{index}/nested")
    evicted = next(iter(pins.handles.values()))
    actual_close = os.close
    calls = []

    def close(descriptor):
        calls.append(descriptor)
        actual_close(descriptor)
        if descriptor == evicted:
            raise OSError("Eviction close failed after release")

    monkeypatch.setattr(os, "close", close)
    try:
        with pytest.raises(OSError):
            pins.parent("16/nested")
        assert evicted not in pins.handles.values()
    finally:
        pins.close()
    assert calls.count(evicted) == 1


def test_capture_drift_remains_primary_when_directory_cleanup_also_fails(tmp_path, revision, monkeypatch):
    root = tree(tmp_path / "workspace", directories=1, files=1)
    original_read = revision.read_observed_file
    original_close = revision._DirectoryPins.close

    def read(*args):
        result = original_read(*args)
        (root / "0/nested/new.txt").write_bytes(b"Changed tree")
        return result

    def close(pins):
        original_close(pins)
        raise OSError("Cleanup also failed after releasing all handles")

    monkeypatch.setattr(revision, "read_observed_file", read)
    monkeypatch.setattr(revision._DirectoryPins, "close", close)
    with pytest.raises(Exception) as refused:
        revision.capture_workspace(root, max_attempts=1)
    assert refused.value.error_code == "EVIDENCE_REVISION_CHANGED"
    assert isinstance(refused.value.__cause__, OSError)
