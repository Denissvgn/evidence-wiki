"""Actual native historical-gate borrowing and pre-write custody attacks."""

import asyncio
import contextvars
import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from evidence_wiki import host_context as host
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.host_context import HostContext
from evidence_wiki.mutation_store import MutationStore, MutationStoreError
from tests.test_mutation_store import intent, receipt, state


@pytest.fixture
def stored(tmp_path):
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd or os.link not in os.supports_dir_fd:
        pytest.skip("The actual native storage boundary is unavailable on this platform.")
    workspace, control = tmp_path / "workspace", tmp_path / "control"
    workspace.mkdir()
    control.mkdir(mode=0o700)
    (workspace / "input.yml").write_bytes(b"schema_version: '1.0'\nquestions: []\n")
    expected = intent(workspace, "historical")
    with MutationStore(workspace, control, expected).locked() as owner:
        owner.prepare(expected)
        owner.write(state(expected))
        owner.commit_receipt(receipt(expected))
    context = HostContext([workspace], [], "host", mutation_control_directory=control)
    return workspace, control, expected, context


def snapshot(control):
    return {path.relative_to(control).as_posix(): path.read_bytes() for path in control.rglob("*") if path.is_file()}


def test_exact_current_context_borrows_real_workspace_gate_for_historical_receipt(stored):
    root, control, expected, context = stored
    before = snapshot(control)
    with context.activate(), MutationStore(root, control).workspace_locked():
        with MutationStore(root, control).historical_locked("historical", borrow_workspace=True) as reader:
            assert reader.receipt() == receipt(expected)
            assert reader.read() == state(expected)
    assert snapshot(control) == before


def test_standalone_historical_custody_cannot_supply_native_workspace_write_gate(stored):
    root, control, _, context = stored
    before = snapshot(control)
    with context.activate(), MutationStore(root, control).historical_locked("historical"):
        host._require_mutation_workspace_gate(root, allow_read_only=True)
        with pytest.raises(EvidenceWikiError) as error:
            host._require_mutation_workspace_gate(root)
        assert error.value.details["reason"] == "host_mutation_workspace_gate_read_only"
    assert snapshot(control) == before


@pytest.mark.parametrize("case", ["no-context", "no-gate", "other-workspace", "other-control", "other-context"])
def test_borrow_requires_exact_live_context_workspace_and_control(stored, tmp_path, case):
    root, control, _, context = stored
    if case == "no-context":
        with pytest.raises(EvidenceWikiError), MutationStore(root, control).historical_locked("historical", borrow_workspace=True):
            pass
        return
    if case == "no-gate":
        with context.activate(), pytest.raises(EvidenceWikiError), MutationStore(root, control).historical_locked("historical", borrow_workspace=True):
            pass
        return
    other_root, other_control = tmp_path / "other-workspace", tmp_path / "other-control"
    other_root.mkdir()
    other_control.mkdir(mode=0o700)
    with context.activate(), MutationStore(root, control).workspace_locked():
        if case == "other-context":
            other = HostContext([root], [], "host", mutation_control_directory=control)
            with other.activate(), pytest.raises(EvidenceWikiError), MutationStore(root, control).historical_locked("historical", borrow_workspace=True):
                pass
        else:
            selected_root = other_root if case == "other-workspace" else root
            selected_control = other_control if case == "other-control" else control
            with pytest.raises(EvidenceWikiError), MutationStore(selected_root, selected_control).historical_locked("historical", borrow_workspace=True):
                pass


def test_copied_context_does_not_transfer_borrow_to_other_thread(stored):
    root, control, _, context = stored
    with context.activate(), MutationStore(root, control).workspace_locked():
        copied = contextvars.copy_context()
        def other_thread():
            with pytest.raises(EvidenceWikiError), MutationStore(root, control).historical_locked("historical", borrow_workspace=True):
                pass
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(copied.run, other_thread).result(timeout=5)


def test_inherited_async_context_does_not_transfer_borrow_to_other_task(stored):
    root, control, _, context = stored
    async def run():
        with context.activate(), MutationStore(root, control).workspace_locked():
            async def other():
                with pytest.raises(EvidenceWikiError), MutationStore(root, control).historical_locked("historical", borrow_workspace=True):
                    pass
            await asyncio.create_task(other())
    asyncio.run(run())


@pytest.mark.parametrize("operation", ["publish", "stage", "prepare", "state", "receipt"])
def test_historical_reader_refuses_all_publication_before_writes(stored, operation):
    root, control, expected, context = stored
    before = snapshot(control)
    with context.activate(), MutationStore(root, control).workspace_locked():
        with MutationStore(root, control).historical_locked("historical", borrow_workspace=True) as reader:
            with pytest.raises(MutationStoreError) as refused:
                if operation == "publish":
                    reader._publish("unowned.json", b"{}")
                elif operation == "stage":
                    reader.stage_blob("unowned", b"private-content")
                elif operation == "prepare":
                    reader.prepare(expected)
                elif operation == "state":
                    reader.write(state(expected))
                else:
                    reader.commit_receipt(receipt(expected))
            assert refused.value.details["reason"] == "historical_read_only"
    assert snapshot(control) == before


def test_reader_failure_releases_only_reader_custody_and_parent_gate_remains_valid(stored):
    root, control, expected, context = stored
    with context.activate(), MutationStore(root, control).workspace_locked():
        reader = MutationStore(root, control)
        with pytest.raises(RuntimeError), reader.historical_locked("historical", borrow_workspace=True):
            raise RuntimeError("Reader interrupted after acquiring request gate.")
        assert not reader._fds and reader._workspace is None and reader._historical_readonly is False
        with MutationStore(root, control).historical_locked("historical", borrow_workspace=True) as retry:
            assert retry.receipt() == receipt(expected)
        host._require_mutation_workspace_gate(root)
    with context.activate(), MutationStore(root, control).workspace_locked():
        pass


def test_default_historical_read_acquires_native_gate_and_preserves_legacy_lock_contention(stored):
    root, control, expected, context = stored
    with MutationStore(root, control).historical_locked("historical") as reader:
        assert reader.receipt() == receipt(expected)
    with context.activate(), MutationStore(root, control).workspace_locked():
        with pytest.raises(MutationStoreError) as refused, MutationStore(root, control).historical_locked("historical"):
            pass
        assert refused.value.error_code == "MUTATION_LOCK_BUSY"


def test_existing_request_conflict_still_cannot_replace_protected_intent(stored):
    root, control, expected, _ = stored
    changed = intent(root, "historical", values={"slug": "different"})
    with MutationStore(root, control, changed).locked() as writer:
        with pytest.raises(MutationStoreError) as refused:
            writer.prepare(changed)
        assert refused.value.error_code == "MUTATION_REQUEST_CONFLICT"
    assert MutationStore(root, control).inspect("historical")["intent"] == expected


@pytest.mark.parametrize("change", ["size", "mode", "name"])
def test_borrow_refuses_tampered_physical_native_gate(stored, change):
    root, control, _, context = stored
    with context.activate(), MutationStore(root, control).workspace_locked():
        gate = host._MUTATION_GATE.get()
        from evidence_wiki._filesystem import os
        path = control / "mutations" / "workspaces" / __import__("hashlib").sha256(str(root).encode()).hexdigest() / "workspace.lock"
        if change == "size":
            os.write(gate.descriptor, b"tampered")
        elif change == "mode":
            os.chmod(path, 0o644)
        else:
            path.rename(path.with_name("replaced.lock"))
            descriptor = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
            os.close(descriptor)
        try:
            with pytest.raises(EvidenceWikiError), MutationStore(root, control).historical_locked("historical", borrow_workspace=True):
                pass
        finally:
            if change == "size":
                os.ftruncate(gate.descriptor, 0)
            elif change == "mode":
                os.chmod(path, 0o600)
            else:
                path.unlink()
                path.with_name("replaced.lock").rename(path)
