"""Retirement excludes live writers and preserves audit evidence through cleanup."""

import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import pytest

from evidence_wiki import Workspace
from evidence_wiki.errors import LockError, OrchestrationError
from tests._script_loader import load_script
from tests.test_library_orchestrate import AGENT_ID, ORCHESTRATION_ID, WorkspaceBuilder

CONTROLLER = load_script("retention_controller", "orchestration_controller.py")
CLAIMS = CONTROLLER.load_sibling_module("_order_claims")
RETENTION = CONTROLLER.load_sibling_module("_order_retention")


class Builder(WorkspaceBuilder, unittest.TestCase):
    pass


@pytest.fixture
def session_data(tmp_path):
    builder = Builder()
    root = builder.init_workspace(tmp_path)
    with Workspace.open(root) as workspace:
        session = workspace.orchestrate.start(AGENT_ID, orchestration_id=ORCHESTRATION_ID)
        order = session.next()
        CLAIMS.record_fulfilment_claim(root, ORCHESTRATION_ID, order["action_id"],
                                     request_id="request-1", source_id="source-1", claimed_at="2026-01-01T00:00:00Z")
        yield root, session, order, builder


def finish(data):
    root, session, order, builder = data
    result = session.submit(order["action_id"], builder.result_document(order["action_id"], outcome="failed"))
    assert result["status"] == "failed"
    return root, session, CLAIMS.claims_path(root, ORCHESTRATION_ID, order["action_id"])


def tree_bytes(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def command(root, name, *, apply=True):
    arguments = ["--project-root", str(root), name, "--orchestration-id", ORCHESTRATION_ID, "--format", "json"]
    if name == "retire":
        arguments.extend(["--reason", "Research closed"])
    if apply:
        arguments.append("--apply")
    return CONTROLLER.command_document(root, CONTROLLER.parse_args(arguments))


def test_dry_run_retirement_cleanup_and_repetition_through_deployed_api(session_data):
    root, session, ledger = finish(session_data)
    before = tree_bytes(root)
    plan = session.retire("Research closed")
    assert plan["status"] == "retirement_planned" and not plan["applied"]
    assert tree_bytes(root) == before
    retired = session.retire("Research closed", apply=True)
    assert retired["status"] == "retired"
    archived = root / "runs/order-claim-archives" / ORCHESTRATION_ID / retired["evidence"][ledger.relative_to(root).as_posix()]
    assert archived.read_bytes() == ledger.read_bytes()
    before = tree_bytes(root)
    assert session.cleanup_claims()["eligible"] == [ledger.relative_to(root).as_posix()]
    assert tree_bytes(root) == before
    assert session.cleanup_claims(apply=True)["status"] == "cleaned"
    assert not ledger.exists() and archived.exists()
    assert session.cleanup_claims(apply=True)["eligible"] == []
    assert session.retire("Research closed", apply=True)["status"] == "already_retired"
    assert CLAIMS.retirement_path(root, ORCHESTRATION_ID).is_file()
    assert CLAIMS.claims_lock_path(root, ORCHESTRATION_ID, ledger.stem).exists()
    assert session.status()["status"] == "failed"
    with pytest.raises(OrchestrationError) as error:
        session.next()
    assert error.value.error_code == "ORCHESTRATION_RETIRED"
    assert error.value.recoverable is False
    with pytest.raises(CLAIMS.OrderClaimError, match="retired"):
        CLAIMS.record_reopen_claim(root, ORCHESTRATION_ID, "new-action", question_slug="q",
                                  source_ids=[], request_ids=[], claimed_at="now")


def test_active_pending_unknown_and_payload_deletion_requests_are_preserved(session_data):
    root, session, _, _ = session_data
    before = tree_bytes(root)
    with pytest.raises(OrchestrationError, match="terminal"):
        session.retire("Still active")
    assert tree_bytes(root) == before
    finish(session_data)
    with pytest.raises(OrchestrationError, match="deletion is required"):
        session.retire("Erase data", payload_policy="delete-required", apply=True)
    assert not CLAIMS.retirement_path(root, ORCHESTRATION_ID).exists()
    with pytest.raises(OrchestrationError, match="explicit retirement"):
        session.cleanup_claims(apply=True)
    path = CONTROLLER.session_path(root, ORCHESTRATION_ID)
    document = json.loads(path.read_text())
    document["pending_action_id"] = "another-action"
    path.write_text(json.dumps(document))
    with pytest.raises(OrchestrationError, match="pending work"):
        session.retire("Pending", apply=True)


def test_unknown_ambiguous_and_incomplete_ledgers_survive(session_data):
    root, session, ledger = finish(session_data)
    directory = ledger.parent
    unknowns = {".writing.tmp": b"partial", "unknown.json": b"{}", "notes.txt": b"keep"}
    ambiguous = json.loads(ledger.read_text())
    ambiguous["orchestration_id"] = "someone-else"
    unknowns["ambiguous.json"] = json.dumps(ambiguous).encode()
    for name, content in unknowns.items():
        (directory / name).write_bytes(content)
    session.retire("Closed", apply=True)
    assert len(session.cleanup_claims(apply=True)["retained"]) == 4
    assert not ledger.exists()
    for name, content in unknowns.items():
        assert (directory / name).read_bytes() == content


@pytest.mark.parametrize("lock_kind", ["session", "retention", "action"])
def test_held_locks_prevent_retirement(session_data, lock_kind):
    root, session, ledger = finish(session_data)
    lock = {"session": CONTROLLER.session_lock_path(root, ORCHESTRATION_ID),
            "retention": CLAIMS.retention_lock_path(root, ORCHESTRATION_ID),
            "action": CLAIMS.claims_lock_path(root, ORCHESTRATION_ID, ledger.stem)}[lock_kind]
    with CONTROLLER.workspace_lock(lock, timeout_seconds=0, purpose="active caller"):
        with pytest.raises((OrchestrationError, LockError)) as refusal:
            session.retire("Closed", apply=True)
        assert refusal.value.error_code in {"ORCHESTRATION_DRIVER_BUSY", "LOCK_UNAVAILABLE"}
    assert ledger.exists() and not CLAIMS.retirement_path(root, ORCHESTRATION_ID).exists()


def test_waiting_claim_writer_rechecks_retirement_after_lock_release(session_data):
    root, _, ledger = finish(session_data)
    ready = threading.Event()
    original_lock, original_publish = CLAIMS.workspace_lock, RETENTION.publish_bytes
    future = None

    @contextmanager
    def observed_lock(path, **kwargs):
        if path == CLAIMS.retention_lock_path(root, ORCHESTRATION_ID):
            ready.set()
        with original_lock(path, **kwargs) as held:
            yield held

    with ThreadPoolExecutor(max_workers=1) as pool:
        def publish(path, payload):
            nonlocal future
            if path == CLAIMS.retirement_path(root, ORCHESTRATION_ID):
                future = pool.submit(CLAIMS.record_reopen_claim, root, ORCHESTRATION_ID, ledger.stem,
                                     question_slug="late-question", source_ids=[], request_ids=[], claimed_at="now")
                assert ready.wait(5)
                assert not future.done()
            return original_publish(path, payload)

        with mock.patch.object(CLAIMS, "workspace_lock", observed_lock), mock.patch.object(RETENTION, "publish_bytes", publish):
            command(root, "retire")
            with pytest.raises(CLAIMS.OrderClaimError, match="retired"):
                future.result(timeout=5)
    assert CLAIMS.load_claims(ledger)["reopens"] == {}


def test_interrupted_retirement_leaves_live_claims_and_can_be_repeated(session_data):
    root, _, ledger = finish(session_data)
    publish = RETENTION.publish_bytes

    def interrupt_marker(path, payload):
        if path.name == ".retired.json":
            raise OSError("interrupted before retirement publication")
        publish(path, payload)

    with mock.patch.object(RETENTION, "publish_bytes", side_effect=interrupt_marker):
        with pytest.raises(CONTROLLER.OrchestrationControllerError, match="interrupted"):
            command(root, "retire")
    assert ledger.exists() and not CLAIMS.retirement_path(root, ORCHESTRATION_ID).exists()
    assert command(root, "retire")["status"] == "retired"
    assert command(root, "cleanup-claims")["status"] == "cleaned"


def test_interrupted_cleanup_and_changed_archive_never_erase_unproven_payload(session_data):
    root, session, ledger = finish(session_data)
    session.retire("Closed", apply=True)
    with mock.patch.object(Path, "unlink", side_effect=OSError("interrupted unlink")):
        with pytest.raises(CONTROLLER.OrchestrationControllerError, match="interrupted"):
            command(root, "cleanup-claims")
    assert ledger.exists()
    marker = json.loads(CLAIMS.retirement_path(root, ORCHESTRATION_ID).read_text())
    sha = marker["evidence"][ledger.relative_to(root).as_posix()]
    archive = RETENTION.archive_path(root, ORCHESTRATION_ID, sha)
    archive.write_bytes(b"changed")
    with pytest.raises(OrchestrationError, match="archive.*changed"):
        session.cleanup_claims(apply=True)
    assert ledger.exists()


def test_retirement_revalidates_after_archive_creation(session_data):
    root, _, ledger = finish(session_data)
    archive = RETENTION.write_archive

    def changed_after_archive(*args):
        archive(*args)
        ledger.write_text(ledger.read_text() + "\n")

    with mock.patch.object(RETENTION, "write_archive", side_effect=changed_after_archive):
        with pytest.raises(CONTROLLER.OrchestrationControllerError, match="inventory changed"):
            command(root, "retire")
    assert ledger.exists() and not CLAIMS.retirement_path(root, ORCHESTRATION_ID).exists()


def test_cleanup_preserves_ledger_changed_since_retirement(session_data):
    root, session, ledger = finish(session_data)
    session.retire("Closed", apply=True)
    ledger.write_text(ledger.read_text() + "\n")
    result = session.cleanup_claims(apply=True)
    assert result["eligible"] == [] and result["retained"]
    assert ledger.exists()


def test_partial_cleanup_resumes_without_recreating_deleted_ledgers(session_data):
    root, session, ledger = finish(session_data)
    order_path = CONTROLLER.work_order_path(root, ORCHESTRATION_ID, ledger.stem)
    order = json.loads(order_path.read_text())
    order["action_id"] = "action-0002"
    CONTROLLER.work_order_path(root, ORCHESTRATION_ID, "action-0002").write_text(json.dumps(order))
    CLAIMS.record_fulfilment_claim(root, ORCHESTRATION_ID, "action-0002", request_id="r2",
                                  source_id="s2", claimed_at="2026-01-01T00:00:00Z")
    second = CLAIMS.claims_path(root, ORCHESTRATION_ID, "action-0002")
    session.retire("Closed", apply=True)
    unlink = Path.unlink

    def interrupt_second(path, *args, **kwargs):
        if path == second:
            raise OSError("interrupted second unlink")
        return unlink(path, *args, **kwargs)

    with mock.patch.object(Path, "unlink", interrupt_second):
        with pytest.raises(CONTROLLER.OrchestrationControllerError, match="interrupted"):
            command(root, "cleanup-claims")
    assert not ledger.exists() and second.exists()
    result = session.cleanup_claims(apply=True)
    assert result["already_absent"] == [ledger.relative_to(root).as_posix()]
    assert not second.exists()


@pytest.mark.parametrize("damage", ["retained-type", "ownership", "session", "symlink"])
def test_damaged_retirement_evidence_refuses_cleanup(session_data, damage):
    root, session, ledger = finish(session_data)
    session.retire("Closed", apply=True)
    path = CLAIMS.retirement_path(root, ORCHESTRATION_ID)
    marker = json.loads(path.read_text())
    if damage == "retained-type":
        marker["retained"] = [None]
        path.write_text(json.dumps(marker))
    elif damage == "ownership":
        marker["eligible"].append("runs/order-claims/another/action.json")
        path.write_text(json.dumps(marker))
    elif damage == "session":
        state = CONTROLLER.session_path(root, ORCHESTRATION_ID)
        state.write_text(state.read_text() + "\n")
    else:
        saved = ledger.with_suffix(".saved")
        ledger.rename(saved)
        try:
            ledger.symlink_to(saved)
        except OSError:
            pytest.skip("symlink creation unavailable")
    with pytest.raises(OrchestrationError) as refusal:
        session.cleanup_claims(apply=True)
    assert refusal.value.error_code == "ORCHESTRATION_RETENTION_UNSAFE"
    assert ledger.exists()
