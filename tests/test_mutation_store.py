"""Private native mutation gates, exact intent replay and durable checkpoints."""

from __future__ import annotations

import copy
import hashlib
import stat
from concurrent.futures import ThreadPoolExecutor

import pytest

from evidence_wiki import mutation_store as module
from evidence_wiki._filesystem import os as native_os
from evidence_wiki.mutation_contracts import RECEIPT, REQUEST, STATE, arguments, make_intent
from evidence_wiki.mutation_store import MutationStore, MutationStoreError, capture_owner_generation, capture_revision


@pytest.fixture
def roots(tmp_path):
    workspace, control = tmp_path / "workspace", tmp_path / "control"
    workspace.mkdir()
    native_os.mkdir(control, mode=0o700)
    (workspace / "input.yml").write_bytes(b"schema_version: '1.0'\nquestions: []\n")
    return workspace, control


def intent(root, request_id="request", *, operation="coverage.init", values=None):
    snapshot = capture_revision(root, input_paths=["input.yml"])
    request = {"schema_version": REQUEST, "request_id": request_id, "operation": operation,
        "arguments": arguments(operation, values or {"slug": "sample"}),
        "expected_revision": snapshot["revision_id"], "input_files": snapshot["input_files"],
        "host_context_sha256": "sha256:" + "5" * 64,
        "owner_generation": {"script_root": "/synthetic/scripts", "script_sha256": "sha256:" + "1" * 64,
                             "package_sha256": "sha256:" + "2" * 64}}
    return make_intent(request, snapshot["workspace_binding"])


def state(value, status="executing", *, result=None):
    return {"schema_version": STATE, "request_id": value["request_id"], "input_sha256": value["input_sha256"],
        "status": status, "before_revision": value["expected_revision"], "after_revision": None,
        "result": result, "error": None, "reconciliation": None, "owner_basis": None}


def receipt(value):
    return {"schema_version": RECEIPT, "request_id": value["request_id"], "input_sha256": value["input_sha256"],
        "operation": value["operation"], "status": "committed", "before_revision": value["expected_revision"],
        "after_revision": "sha256:" + "3" * 64, "result": {"owner_result": "synthetic"}, "error": None,
        "historical": True, "authorized": False, "evidence_accepted": False}


def request_directory(root, workspace, request_id="request"):
    return root / "mutations/workspaces" / hashlib.sha256(str(workspace.resolve()).encode()).hexdigest() / "requests" / hashlib.sha256(request_id.encode()).hexdigest()


@pytest.mark.parametrize("missing", ["namespace", "request", "workspace_lock", "request_lock"])
def test_historical_reads_never_recreate_deleted_custody(roots, missing):
    workspace, control = roots
    if missing != "namespace":
        original = intent(workspace)
        with MutationStore(workspace, control, original).locked() as store:
            store.prepare(original)
        request = request_directory(control, workspace)
        if missing == "request":
            import shutil
            shutil.rmtree(request)
        else:
            selected = request / "request.lock" if missing == "request_lock" else request.parent.parent / "workspace.lock"
            selected.unlink()
    before = {p.relative_to(control).as_posix(): p.read_bytes() if p.is_file() else None for p in control.rglob("*")}
    with pytest.raises(MutationStoreError):
        with MutationStore(workspace, control).historical_locked("request"):
            pytest.fail("Missing historical custody was recreated.")
    assert {p.relative_to(control).as_posix(): p.read_bytes() if p.is_file() else None for p in control.rglob("*")} == before


def test_retained_historical_read_preserves_bytes_and_refuses_publication(roots):
    workspace, control = roots
    original = intent(workspace)
    with MutationStore(workspace, control, original).locked() as store:
        store.prepare(original)
    before = {p.relative_to(control).as_posix(): p.read_bytes() if p.is_file() else None for p in control.rglob("*")}
    with MutationStore(workspace, control).historical_locked("request") as store:
        assert store._read("intent.json") == original
        with pytest.raises(MutationStoreError) as caught:
            store.write(state(original))
        assert caught.value.details["reason"] == "historical_read_only"
    assert {p.relative_to(control).as_posix(): p.read_bytes() if p.is_file() else None for p in control.rglob("*")} == before


def test_exact_intent_and_receipt_replay_precede_current_workspace_preconditions(roots):
    workspace, control = roots
    original = intent(workspace)
    stored = MutationStore(workspace, control, original)
    with stored.locked():
        assert stored.prepare(original) == original
        checkpoint = state(original)
        digest = stored.write(checkpoint)
        assert stored.read() == checkpoint and stored.state_sha256() == digest
        stored.commit_receipt(receipt(original))
    (workspace / "input.yml").write_bytes(b"changed after the recorded effect\n")
    with MutationStore(workspace, control, original).locked() as replay:
        assert replay.prepare(original) == original
        assert replay.receipt() == receipt(original)
    observed = MutationStore(workspace, control).inspect("request")
    assert observed["intent"] == original and observed["receipt"]["historical"]
    observed["state"]["status"] = "refused"
    assert MutationStore(workspace, control).inspect("request")["state"]["status"] == "executing"
    assert not (workspace / "mutations").exists()


def test_request_id_conflict_preserves_exact_original_bytes(roots):
    workspace, control = roots
    original = intent(workspace)
    with MutationStore(workspace, control, original).locked() as store:
        store.prepare(original)
    changed = intent(workspace, values={"slug": "other"})
    with MutationStore(workspace, control, changed).locked() as store, pytest.raises(MutationStoreError) as error:
        store.prepare(changed)
    assert error.value.error_code == "MUTATION_REQUEST_CONFLICT"
    assert MutationStore(workspace, control).inspect("request")["intent"] == original


def test_changed_host_selection_cannot_reuse_an_existing_intent(roots):
    workspace, control = roots
    original = intent(workspace)
    with MutationStore(workspace, control, original).locked() as store:
        store.prepare(original)
        store.commit_receipt(receipt(original))
    changed = copy.deepcopy(original)
    changed["host_context_sha256"] = "sha256:" + "6" * 64
    from evidence_wiki.mutation_contracts import intent_digest
    changed["input_sha256"] = intent_digest(changed)
    with MutationStore(workspace, control, changed).locked() as store, pytest.raises(MutationStoreError) as error:
        store.prepare(changed)
    assert error.value.error_code == "MUTATION_REQUEST_CONFLICT"
    assert MutationStore(workspace, control).inspect("request")["receipt"] == receipt(original)


def test_state_cas_prevents_lost_updates_and_receipts_are_immutable(roots):
    workspace, control = roots
    value = intent(workspace)
    with MutationStore(workspace, control, value).locked() as store:
        store.prepare(value)
        first = store.write(state(value, "prepared"))
        second = store.write(state(value), expected_sha256=first)
        with pytest.raises(MutationStoreError) as error:
            store.write(state(value, "partial"), expected_sha256=first)
        assert error.value.error_code == "MUTATION_STATE_CONFLICT"
        assert store.state_sha256() == second
        final = receipt(value)
        store.commit_receipt(final)
        modified = copy.deepcopy(final)
        modified["result"] = {"changed": True}
        with pytest.raises(MutationStoreError):
            store.commit_receipt(modified)
        assert store.receipt() == final


def test_executing_checkpoint_survives_interruption_without_dispatch(roots):
    workspace, control = roots
    value = intent(workspace)
    with pytest.raises(RuntimeError), MutationStore(workspace, control, value).locked() as store:
        store.prepare(value)
        store.write(state(value))
        raise RuntimeError("synthetic interruption")
    inspected = MutationStore(workspace, control).inspect("request")
    assert inspected["state"]["status"] == "executing" and inspected["receipt"] is None
    assert (workspace / "input.yml").read_bytes() == b"schema_version: '1.0'\nquestions: []\n"


def test_workspace_only_gate_does_not_reserve_a_fake_request(roots):
    workspace, control = roots
    with MutationStore(workspace, control).workspace_locked():
        captured = capture_revision(workspace)
        assert captured["workspace_binding"]["path"] == str(workspace.resolve())
        assert not list(control.rglob("intent.json"))
        assert not list(control.rglob("requests"))


def test_same_workspace_contends_and_independent_workspaces_progress(roots, tmp_path):
    workspace, control = roots
    other = tmp_path / "other"
    other.mkdir()
    value = intent(workspace)
    with MutationStore(workspace, control, value).locked() as held:
        held.prepare(value)

        def same():
            with MutationStore(workspace, control).workspace_locked():
                return "unexpected"

        def different():
            with MutationStore(other, control).workspace_locked():
                return "progress"

        with ThreadPoolExecutor(max_workers=2) as pool:
            blocked, progress = pool.submit(same), pool.submit(different)
            with pytest.raises(MutationStoreError) as error:
                blocked.result(timeout=5)
            assert error.value.error_code == "MUTATION_LOCK_BUSY"
            assert progress.result(timeout=5) == "progress"


def test_native_lock_route_does_not_use_single_writer_bypass(roots, monkeypatch):
    workspace, control = roots
    monkeypatch.setenv("EVIDENCE_WIKI_SINGLE_WRITER", "1")
    locks = module.owner("_workspace_locks")

    def unavailable(*args, **kwargs):
        raise locks.LockUnavailableError("unavailable")

    monkeypatch.setattr(locks, "descriptor_lock", unavailable)
    with pytest.raises(MutationStoreError) as error, MutationStore(workspace, control).workspace_locked():
        pass
    assert error.value.error_code == "MUTATION_PLATFORM_UNAVAILABLE"


@pytest.mark.parametrize("kind", ["control_link", "overlap", "public_control", "lock_link", "intent_link", "state_hardlink"])
def test_unowned_or_escaped_control_entries_refuse(roots, tmp_path, kind):
    workspace, control = roots
    original = intent(workspace)
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside-marker")
    if kind == "control_link":
        alias = tmp_path / "linked-control"
        alias.symlink_to(control, target_is_directory=True)
        with pytest.raises(MutationStoreError):
            MutationStore(workspace, alias)
        return
    if kind == "overlap":
        with pytest.raises(MutationStoreError):
            MutationStore(workspace, workspace)
        return
    if kind == "public_control":
        native_os.chmod(control, 0o755)
        with pytest.raises(MutationStoreError), MutationStore(workspace, control).workspace_locked():
            pass
        return
    with MutationStore(workspace, control, original).locked() as store:
        store.prepare(original)
    directory = request_directory(control, workspace)
    name = {"lock_link": "request.lock", "intent_link": "intent.json", "state_hardlink": "state.json"}[kind]
    path = directory / name
    if path.exists():
        path.unlink()
    if kind == "state_hardlink":
        path.hardlink_to(outside)
    else:
        path.symlink_to(outside)
    with pytest.raises(MutationStoreError):
        if kind == "lock_link":
            with MutationStore(workspace, control, original).locked():
                pass
        else:
            MutationStore(workspace, control).inspect("request")
    assert outside.read_bytes() == b"outside-marker"


@pytest.mark.parametrize("selected", ["workspace", "control"])
def test_file_valued_roots_refuse_before_creating_gate_metadata(roots, tmp_path, selected):
    workspace, control = roots
    file = tmp_path / "file"
    file.write_bytes(b"outside-marker")
    with pytest.raises(MutationStoreError) as error:
        MutationStore(file if selected == "workspace" else workspace,
                      file if selected == "control" else control)
    assert error.value.details == {"reason": "root_not_directory"}
    assert file.read_bytes() == b"outside-marker"
    assert not (control / "mutations").exists()


def test_checkpoint_publication_failure_and_unknown_durability_remain_inspectable(roots, monkeypatch):
    workspace, control = roots
    value = intent(workspace)
    with MutationStore(workspace, control, value).locked() as store:
        store.prepare(value)
        original_replace = module.os.replace

        def failed_replace(*args, **kwargs):
            raise OSError("synthetic pre-publication failure")

        monkeypatch.setattr(module.os, "replace", failed_replace)
        with pytest.raises(MutationStoreError):
            store.write(state(value))
        assert store.read() is None
        monkeypatch.setattr(module.os, "replace", original_replace)
        original_fsync = module.os.fsync

        def failed_directory_sync(descriptor):
            if descriptor == store._request and stat.S_ISDIR(module.os.fstat(descriptor).st_mode):
                raise OSError("synthetic post-publication uncertainty")
            return original_fsync(descriptor)

        monkeypatch.setattr(module.os, "fsync", failed_directory_sync)
        with pytest.raises(MutationStoreError) as error:
            store.write(state(value))
        assert error.value.error_code == "MUTATION_COMMIT_UNKNOWN"
        monkeypatch.setattr(module.os, "fsync", original_fsync)
        assert store.read()["status"] == "executing"
    assert MutationStore(workspace, control).inspect("request")["state"]["status"] == "executing"


def test_receipts_cannot_assert_authority_or_acceptance(roots):
    workspace, control = roots
    value = intent(workspace)
    with MutationStore(workspace, control, value).locked() as store:
        store.prepare(value)
        for field in ("authorized", "evidence_accepted"):
            forged = receipt(value)
            forged[field] = True
            with pytest.raises(MutationStoreError):
                store.commit_receipt(forged)
        assert store.receipt() is None


@pytest.mark.parametrize("record, field, replacement", [
    ("state", "before_revision", "sha256:" + "4" * 64),
    ("receipt", "before_revision", "sha256:" + "4" * 64),
    ("receipt", "operation", "coverage.link"),
])
def test_checkpoint_and_receipt_cannot_substitute_immutable_intent_basis(roots, record, field, replacement):
    workspace, control = roots
    value = intent(workspace)
    with MutationStore(workspace, control, value).locked() as store:
        store.prepare(value)
        changed = state(value) if record == "state" else receipt(value)
        changed[field] = replacement
        with pytest.raises(MutationStoreError):
            store.write(changed) if record == "state" else store.commit_receipt(changed)
        assert store.read() is None and store.receipt() is None


def test_snapshot_is_bounded_lossless_and_control_exclusions_are_explicit(roots):
    workspace, control = roots
    before = capture_revision(workspace, input_paths=["input.yml"])
    assert before["input_bytes"]["input.yml"] == (workspace / "input.yml").read_bytes()
    assert before["input_files"][0]["sha256"] == "sha256:" + hashlib.sha256(before["input_bytes"]["input.yml"]).hexdigest()
    with MutationStore(workspace, control).workspace_locked():
        assert capture_revision(workspace)["revision_id"] == before["revision_id"]
    (workspace / ".locks").mkdir()
    (workspace / ".locks/owner.lock").write_bytes(b"")
    with_control = capture_revision(workspace)
    assert with_control["revision_id"] != before["revision_id"]
    trusted = capture_revision(workspace, excluded=[".locks"])
    (workspace / ".locks/owner.lock").write_bytes(b"owner control changed")
    assert capture_revision(workspace, excluded=[".locks"])["revision_id"] == trusted["revision_id"]
    with pytest.raises(MutationStoreError):
        capture_revision(workspace, excluded=["input.yml"], input_paths=["input.yml"])


def test_snapshot_rejects_links_and_changed_inputs(roots, tmp_path):
    workspace, _control = roots
    original = capture_revision(workspace)
    (workspace / "input.yml").write_bytes(b"changed\n")
    assert capture_revision(workspace)["revision_id"] != original["revision_id"]
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside-marker")
    (workspace / "linked").symlink_to(outside)
    with pytest.raises(MutationStoreError):
        capture_revision(workspace)


def test_owner_generation_is_repeatable_and_detached():
    first, second = capture_owner_generation(), capture_owner_generation()
    assert first == second
    assert set(first) == {"script_root", "script_sha256", "package_sha256"}
    assert first["script_sha256"].startswith("sha256:")
    first["package_sha256"] = "modified result"
    assert capture_owner_generation() == second


def test_owner_generation_rejects_source_changes_during_capture(roots, monkeypatch):
    workspace, _control = roots
    package = workspace / "package"
    package.mkdir()
    source = package / "owner.py"
    source.write_bytes(b"VALUE = 1\n")
    monkeypatch.setattr(module, "__file__", str(package / "__init__.py"))
    monkeypatch.setattr(module, "owner_generation", lambda: (package, "1" * 64))
    revision = module.owner("_evidence_revision")
    original_read = revision.read_observed_file

    def changing_read(root, relative, observation):
        raw = original_read(root, relative, observation)
        source.write_bytes(b"VALUE = 2\n")
        return raw

    monkeypatch.setattr(revision, "read_observed_file", changing_read)
    with pytest.raises(MutationStoreError) as error:
        capture_owner_generation()
    assert error.value.error_code == "MUTATION_REVISION_CONFLICT"


def test_stored_invalid_json_and_unknown_state_are_not_admitted(roots):
    workspace, control = roots
    value = intent(workspace)
    with MutationStore(workspace, control, value).locked() as store:
        store.prepare(value)
        invalid = state(value)
        invalid["status"] = "accepted"
        with pytest.raises(MutationStoreError):
            store.write(invalid)
    path = request_directory(control, workspace) / "state.json"
    path.write_text("not-json")
    native_os.chmod(path, 0o600)
    with pytest.raises(MutationStoreError):
        MutationStore(workspace, control).inspect("request")
