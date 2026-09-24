"""Native filesystem invariants shared by package and standalone publishers."""

import os as standard_os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from evidence_wiki._filesystem import metadata_fstat, os
from evidence_wiki._pack_io import read_file, signature
from evidence_wiki.pack_catalog import initialize, register
from evidence_wiki.pack_discovery import owner


def test_adapter_does_not_modify_the_standard_os_module():
    if standard_os.name == "nt":
        assert os is not standard_os
        assert os.open in os.supports_dir_fd and standard_os.open not in standard_os.supports_dir_fd
    else:
        assert os is standard_os


def test_batched_reads_observe_current_bytes_and_reject_links(tmp_path):
    from evidence_wiki._pack_io import file_reader
    from evidence_wiki.errors import UsageError

    path = tmp_path / "input.txt"
    path.write_bytes(b"original")
    with file_reader() as read:
        assert read(tmp_path, "input.txt") == b"original"
        path.write_bytes(b"changed")
        assert read(tmp_path, "input.txt") == b"changed"
        path.unlink()
        path.symlink_to(tmp_path / "absent")
        with pytest.raises((UsageError, ValueError)):
            read(tmp_path, "input.txt")


@pytest.mark.skipif(standard_os.name != "nt", reason="Exercises native reader generation revalidation")
def test_batched_reads_refuse_a_changed_reader_generation(tmp_path, monkeypatch):
    from evidence_wiki import _pack_io as pack_io
    from evidence_wiki.errors import UsageError

    (tmp_path / "input.txt").write_bytes(b"current")
    with pytest.raises(UsageError) as error:
        with pack_io.file_reader() as read:
            assert read(tmp_path, "input.txt") == b"current"
            monkeypatch.setattr(pack_io, "_windows_reader", lambda: (object(), object()))
    assert error.value.details["field"] == "pack_reader_changed_during_read"


def test_native_directory_operations_and_exclusive_publication(tmp_path):
    directory = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.mkdir("private", 0o700, dir_fd=directory)
        nested = os.open("private", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
        try:
            descriptor = os.open("temporary", os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=nested)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(b"retained bytes\r\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.link("temporary", "published", src_dir_fd=nested, dst_dir_fd=nested, follow_symlinks=False)
            with pytest.raises(FileExistsError):
                os.link("temporary", "published", src_dir_fd=nested, dst_dir_fd=nested, follow_symlinks=False)
            os.unlink("temporary", dir_fd=nested)
            os.fsync(nested)
            assert read_file(nested, "published") == b"retained bytes\r\n"
            assert os.listdir(nested) == ["published"]
            with os.scandir(nested) as entries:
                assert [entry.name for entry in entries] == ["published"]
            os.replace("published", "renamed", src_dir_fd=nested, dst_dir_fd=nested)
            assert read_file(nested, "renamed") == b"retained bytes\r\n"
            os.unlink("renamed", dir_fd=nested)
        finally:
            os.close(nested)
        os.rmdir("private", dir_fd=directory)
    finally:
        os.close(directory)


def test_native_private_permissions_are_enforced(tmp_path):
    root = tmp_path / "authority"
    os.mkdir(root, 0o700)
    descriptor = os.open(root / "policy.json", os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.close(descriptor)
    store = owner("_host_evidence_store")
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        store.private_entry(os.fstat(directory), directory=True)
        descriptor = os.open("policy.json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        try:
            store.private_entry(os.fstat(descriptor))
            if standard_os.name == "nt":
                metadata = metadata_fstat(descriptor)
                assert metadata.native_private is None
                with pytest.raises(ValueError, match="unsafe_host_state_entry"):
                    store.private_entry(metadata)
        finally:
            os.close(descriptor)
        os.chmod(root / "policy.json", 0o644)
        descriptor = os.open("policy.json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        try:
            with pytest.raises(ValueError, match="unsafe_host_state_entry"):
                store.private_entry(os.fstat(descriptor))
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)


def test_metadata_signature_retains_changes_when_size_and_mtime_are_restored(tmp_path):
    path = tmp_path / "observed.txt"
    path.write_bytes(b"before")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = metadata_fstat(descriptor)
        # Timestamp precision does not imply an update on every rapid write.
        time.sleep(0.05)
        path.write_bytes(b"after!")
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = metadata_fstat(descriptor)
        assert before.st_ino == after.st_ino
        assert before.st_size == after.st_size
        assert before.st_mtime_ns == after.st_mtime_ns
        assert signature(before) != signature(after)
    finally:
        os.close(descriptor)


@pytest.mark.skipif(standard_os.name != "nt", reason="Exercises cold native metadata initialization")
def test_metadata_stat_initializes_a_fresh_native_owner(tmp_path):
    filesystem = owner("_windows_fs").WindowsFilesystem()
    path = tmp_path / "observed.txt"
    path.write_bytes(b"observed")
    info = filesystem.stat(path, follow_symlinks=False, check_private=False)
    assert info.st_size == 8 and info.native_private is None


def test_native_descriptor_lock_has_no_recursive_writer_bypass(tmp_path):
    locks = owner("_workspace_locks")
    first = os.open(tmp_path / "coordination.lock", os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    second = os.open(tmp_path / "coordination.lock", os.O_RDONLY | os.O_NOFOLLOW)
    try:
        with locks.descriptor_lock(first):
            with pytest.raises(locks.LockUnavailableError) as error, locks.descriptor_lock(second, timeout_seconds=0):
                pytest.fail("A second descriptor acquired the writer lock")
            assert error.value.contended
        with locks.descriptor_lock(second, timeout_seconds=0):
            pass
    finally:
        os.close(second)
        os.close(first)


def test_existing_lock_probe_observes_contention_without_writes(tmp_path):
    locks = owner("_workspace_locks")
    path = tmp_path / "coordination.lock"
    assert not locks.workspace_lock_contended(path)
    assert not path.exists()
    with locks.workspace_lock(path):
        before = {item.name: item.read_bytes() for item in tmp_path.iterdir()}
        metadata = path.stat()
        assert locks.workspace_lock_contended(path)
        assert {item.name: item.read_bytes() for item in tmp_path.iterdir()} == before
        observed = path.stat()
        assert (observed.st_ino, observed.st_size, observed.st_mtime_ns) == (
            metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)
    assert not locks.workspace_lock_contended(path)


@pytest.mark.skipif(standard_os.name != "nt", reason="Exercises native Windows lock/capture interaction")
def test_native_capture_can_observe_a_held_coordination_file(tmp_path):
    locks = owner("_workspace_locks")
    (tmp_path / "research.yml").write_bytes(b"project: {}\n")
    with locks.workspace_lock(tmp_path / "coordination.lock") as lock:
        assert lock.backend == "win32"
        captured = owner("_evidence_revision").capture_workspace(tmp_path)
        assert captured.files["coordination.lock"] == b""
        assert captured.files["research.yml"] == b"project: {}\n"


@pytest.mark.skipif(standard_os.name != "nt", reason="Exercises native Windows process ownership")
def test_native_mutex_recovers_after_process_death(tmp_path):
    locked = tmp_path / "coordination.lock"
    source = (
        "from pathlib import Path; import time; from evidence_wiki.pack_discovery import owner; "
        "locks=owner('_workspace_locks'); "
        "context=locks.workspace_lock(Path(" + repr(str(locked)) + ")); "
        "context.__enter__(); print('locked', flush=True); time.sleep(30)"
    )
    child = subprocess.Popen([sys.executable, "-c", source], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "locked"
        child.terminate()
        child.wait(timeout=10)
        with owner("_workspace_locks").workspace_lock(locked, timeout_seconds=1) as result:
            assert result.backend == "win32"
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)


def test_catalog_uses_the_native_publisher(tmp_path):
    catalog = tmp_path / "catalog"
    packs = Path(__file__).resolve().parents[1] / "domain-packs"
    # Catalog roots must be caller-local, so use a copied bounded pack.
    import shutil

    copies = tmp_path / "packs"
    copies.mkdir()
    shutil.copytree(packs / "general-science", copies / "general-science")
    assert initialize(catalog, {"local": str(copies)})["status"] == "created"
    result = register(catalog, revision="science", root_id="local", relative="general-science", scope="Scientific observations")
    assert result["status"] == "registered"
