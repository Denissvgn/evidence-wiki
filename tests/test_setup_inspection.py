"""Read-only anchored setup inspection and bounded native journal ownership."""

from __future__ import annotations

import copy
from contextlib import contextmanager

import pytest

from evidence_wiki import setup_store as module
from evidence_wiki._pack_io import canonical, identity
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.pack_discovery import owner
from evidence_wiki.planning import compile_plan
from evidence_wiki.setup_store import SetupStore
from tests.test_research_planning import request


@pytest.fixture
def plan(tmp_path):
    return compile_plan(canonical(request(tmp_path)))


def seed(plan):
    store = SetupStore(plan)
    with store.locked():
        checkpoint = store.open_transaction()
        transaction = store.transaction_path
    return checkpoint, transaction


def files(root):
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file() and not path.is_symlink()}


def test_missing_journal_yields_absent_without_creating_setup_or_target(plan):
    store = SetupStore(plan)
    before = files(store.root)
    with store.inspected() as observed:
        assert observed is store and observed.transaction is None
        assert observed.transaction_id is None and observed.transaction_path is None
        assert not observed.target.exists() and not observed.path.exists()
    assert files(store.root) == before and not (store.root / ".evidence-wiki").exists()


@pytest.mark.parametrize("levels", [1, 2, 3, 4])
def test_partial_existing_hierarchy_stays_unchanged_when_journal_is_absent(plan, levels):
    store = SetupStore(plan)
    path = store.root
    for name in (".evidence-wiki", "setup", "targets", store.path.name)[:levels]:
        path /= name
        module.os.mkdir(path, mode=0o700)
    before = {child.relative_to(store.root).as_posix() for child in store.root.rglob("*")}
    with store.inspected():
        assert store.transaction is None and store.transaction_id is None and store.transaction_path is None
    assert {child.relative_to(store.root).as_posix() for child in store.root.rglob("*")} == before


def test_existing_empty_transactions_returns_absent_under_existing_lock(plan):
    store = SetupStore(plan)
    with store.locked():
        pass
    before = files(store.root)
    with store.inspected():
        assert store.transaction is None and store.transaction_id is None and store.transaction_path is None
    assert files(store.root) == before


def test_existing_transaction_exact_binding_and_read_getters_have_no_write_route(plan, monkeypatch):
    checkpoint, path = seed(plan)
    before = files(SetupStore(plan).root)
    original_open = module.os.open
    def read_only_open(value, flags, *args, **kwargs):
        assert not flags & (module.os.O_CREAT | module.os.O_TRUNC | module.os.O_WRONLY | module.os.O_RDWR)
        return original_open(value, flags, *args, **kwargs)
    monkeypatch.setattr(module.os, "open", read_only_open)
    monkeypatch.setattr(module.os, "supports_dir_fd", module.os.supports_dir_fd | {read_only_open})
    def forbidden(*args, **kwargs):
        pytest.fail("inspection attempted a filesystem mutation")
    for name in ("mkdir", "unlink", "write", "replace", "link"):
        monkeypatch.setattr(module.os, name, forbidden)
    with SetupStore(plan).inspected() as store:
        assert store.transaction_path == path and store.transaction_id == path.name
        assert store.read("plan.json") == plan
        assert store.read("request.json") == plan["request"]
        assert store.read("checkpoint.json") == checkpoint
        for operation in (lambda: store.write("receipt.json", {}), store.open_transaction):
            with pytest.raises(EvidenceWikiError) as error:
                operation()
            assert error.value.error_code == "ONBOARDING_OWNERSHIP_CONFLICT"
    assert files(SetupStore(plan).root) == before


def test_transaction_scan_consumes_only_first_two_entries_before_refusal(plan, monkeypatch):
    _checkpoint, path = seed(plan)
    parent = path.parent
    for index in range(100):
        module.os.makedirs(parent / f"{index:032x}", mode=0o700, exist_ok=True)
    original = module.os.scandir
    observed = []
    class Bounded:
        def __init__(self, descriptor):
            self.iterator = original(descriptor)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.iterator.close()
        def __next__(self):
            observed.append(1)
            assert len(observed) <= 2
            return next(self.iterator)
    def bounded(value):
        return Bounded(value) if isinstance(value, int) and identity(value) == identity(parent) else original(value)
    monkeypatch.setattr(module.os, "scandir", bounded)
    with pytest.raises(EvidenceWikiError) as error, SetupStore(plan).inspected():
        pass
    assert error.value.error_code == "ONBOARDING_OWNERSHIP_CONFLICT" and len(observed) == 2


@pytest.mark.parametrize("artifact", ["setup.lock", "transaction", "plan.json", "request.json", "checkpoint.json", "observations"])
@pytest.mark.parametrize("kind", ["symlink", "permission"])
def test_linked_or_nonprivate_custody_refuses_without_effect(plan, tmp_path, artifact, kind):
    _checkpoint, path = seed(plan)
    selected = path.parent.parent / artifact if artifact == "setup.lock" else path if artifact == "transaction" else path / artifact
    before = files(SetupStore(plan).root)
    if kind == "permission":
        module.os.chmod(selected, 0o755 if selected.is_dir() else 0o644)
    else:
        outside = tmp_path / "outside-marker"
        if selected.is_dir():
            module.os.mkdir(outside, mode=0o700)
            selected.rename(selected.with_name(selected.name + "-retained"))
        else:
            outside.write_bytes(b"preserve-outside")
            selected.unlink()
        selected.symlink_to(outside, target_is_directory=outside.is_dir())
    with pytest.raises(EvidenceWikiError), SetupStore(plan).inspected():
        pass
    assert all(files(SetupStore(plan).root).get(name) == raw for name, raw in before.items()
               if name != selected.relative_to(SetupStore(plan).root).as_posix() and artifact != "transaction")


def test_hardlinked_existing_record_and_missing_existing_lock_refuse(plan, tmp_path):
    _checkpoint, path = seed(plan)
    outside = tmp_path / "outside"
    outside.write_bytes((path / "plan.json").read_bytes())
    module.os.chmod(outside, 0o600)
    (path / "plan.json").unlink()
    (path / "plan.json").hardlink_to(outside)
    with pytest.raises(EvidenceWikiError), SetupStore(plan).inspected():
        pass
    (path / "plan.json").unlink()
    (path / "plan.json").write_bytes(canonical(plan))
    module.os.chmod(path / "plan.json", 0o600)
    (path.parent.parent / "setup.lock").unlink()
    with pytest.raises(EvidenceWikiError) as error, SetupStore(plan).inspected():
        pass
    assert error.value.error_code == "ONBOARDING_OWNERSHIP_CONFLICT"


@pytest.mark.parametrize("artifact", ["plan.json", "request.json", "checkpoint.json"])
def test_malformed_or_conflicting_retained_binding_is_typed_and_not_repaired(plan, artifact):
    _checkpoint, path = seed(plan)
    (path / artifact).write_bytes(b'{"unexpected":"retained-content"}')
    before = files(SetupStore(plan).root)
    with pytest.raises(EvidenceWikiError) as error, SetupStore(plan).inspected():
        pass
    assert error.value.error_code in {"ONBOARDING_OWNERSHIP_CONFLICT", "ONBOARDING_PLAN_STALE"}
    assert files(SetupStore(plan).root) == before


def test_inspection_contends_with_native_writer_before_reading_transaction(plan):
    _checkpoint, _path = seed(plan)
    with SetupStore(plan).locked():
        with pytest.raises(EvidenceWikiError) as error, SetupStore(plan).inspected():
            pass
    assert error.value.error_code == "ONBOARDING_LOCK_BUSY"


@pytest.mark.parametrize("operation_error", [OSError("owner I/O operation"), TypeError("owner programming operation"), SystemExit(0)])
def test_yielded_operation_errors_are_preserved_without_store_reclassification(plan, operation_error):
    _checkpoint, _path = seed(plan)
    with pytest.raises(type(operation_error)) as observed:
        with SetupStore(plan).inspected():
            raise operation_error
    assert observed.value is operation_error


def test_store_acquisition_and_exit_errors_are_classified_separately_from_owner_body(plan, monkeypatch):
    _checkpoint, _path = seed(plan)
    locks = owner("_workspace_locks")
    original = locks.descriptor_lock
    @contextmanager
    def broken_exit(*args, **kwargs):
        with original(*args, **kwargs):
            yield
        raise OSError("private exit message must not escape")
    monkeypatch.setattr(locks, "descriptor_lock", broken_exit)
    with pytest.raises(EvidenceWikiError) as error, SetupStore(plan).inspected():
        pass
    assert error.value.error_code == "ONBOARDING_OWNERSHIP_CONFLICT"
    assert "private exit" not in str(error.value)
    body = OSError("owner operation remains its own error")
    with pytest.raises(OSError) as observed, SetupStore(plan).inspected():
        raise body
    assert observed.value is body


def test_target_replacement_while_observed_refuses_closing_without_journal_write(plan):
    _checkpoint, _path = seed(plan)
    store = SetupStore(plan)
    before = files(store.root)
    with pytest.raises(EvidenceWikiError) as error:
        with store.inspected():
            store.target.mkdir()
    assert error.value.error_code == "ONBOARDING_OWNERSHIP_CONFLICT"
    assert files(store.root) == before


def test_changed_exact_plan_for_same_selected_target_is_not_adopted(plan):
    _checkpoint, _path = seed(plan)
    different = copy.deepcopy(plan)
    different["plan_id"] = "2" * 64
    with pytest.raises(EvidenceWikiError) as error, SetupStore(different).inspected():
        pass
    assert error.value.error_code == "ONBOARDING_PLAN_STALE"
