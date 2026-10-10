"""Real canonical publications, protected restore and adversarial host recovery."""

import json
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from evidence_wiki import Workspace, cli
from evidence_wiki.host_context import HostContext
from evidence_wiki.mutation_store import MutationStore
from evidence_wiki.operation_control import OperationControl
from evidence_wiki.operations_recovery import HostBackupRecovery, recover_upgrade
from evidence_wiki.pack_discovery import owner
from tests._operations_fixture import DisposableBackupService
from tests._usage_fixture import UsageFixture


def control(seconds=60):
    return OperationControl(deadline_utc=(datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(), timeout_seconds=seconds)


@pytest.fixture
def upgrade(tmp_path):
    root = tmp_path / "workspace"
    assert cli.main(["init", "--target", str(root), "--project-name", "Retained upgrade", "--project-description", "Keep approved tooling"]) == 0
    private = tmp_path / "control"
    private.mkdir(mode=0o700)
    context = HostContext((root,), {"operations.recover_upgrade"}, "operations-host", (root,), mutation_control_directory=private)
    path = root / "scripts/query_index.py"
    path.write_bytes(b"# permitted previous generation\n")
    predecessors = {"scripts/query_index.py": owner("_usage_materialization").file_digest(path.read_bytes()),
                    "log.md": owner("_usage_materialization").file_digest((root / "log.md").read_bytes())}
    return root, context, predecessors, control()


def test_upgrade_unknown_predecessor_preserves_bytes_and_returns_terminal(upgrade):
    root, context, predecessors, clock = upgrade
    path = root / "scripts/query_index.py"
    path.write_bytes(b"# unexplained local edit\n")
    value = recover_upgrade(root, context=context, request_id="unknown-predecessor", predecessors=predecessors, operation_control=clock)
    assert value["status"] == "blocked_external" and value["terminal"] and value["recoverable"]
    assert path.read_bytes() == b"# unexplained local edit\n"


@pytest.mark.parametrize("stage", ["before", "after", "log", "journal", "version"])
def test_upgrade_partial_publication_reopens_exact_original_plan(upgrade, monkeypatch, stage):
    root, context, predecessors, clock = upgrade
    initializer = owner("init_research_workspace")
    original = initializer.publish_file
    fired = False
    if stage == "version":
        path = root / "workspace-system.yml"
        metadata = yaml.safe_load(path.read_bytes())
        metadata["workspace_system"]["starter_version"] = "0.0.1"
        path.write_text(yaml.safe_dump(metadata))
        predecessors["workspace-system.yml"] = owner("_usage_materialization").file_digest(path.read_bytes())
    def fault(root_, relative, raw, expected, **kwargs):
        nonlocal fired
        selected = relative == ("log.md" if stage == "log" else "workspace-system.yml" if stage == "version" else "scripts/query_index.py")
        if selected and not fired:
            fired = True
            if stage != "before":
                original(root_, relative, raw, expected, **kwargs)
            raise OSError("Interrupted native publication")
        return original(root_, relative, raw, expected, **kwargs)
    from evidence_wiki.operations_recovery import RecoveryJournal
    original_journal = RecoveryJournal.save_journal
    def journal_fault(self, value):
        nonlocal fired
        original_journal(self, value)
        if not fired and "scripts/query_index.py" in value.get("completed", []):
            fired = True
            raise OSError("Interrupted completion observation")
    with monkeypatch.context() as faults:
        if stage == "journal":
            faults.setattr(RecoveryJournal, "save_journal", journal_fault)
        else:
            faults.setattr(initializer, "publish_file", fault)
        first = recover_upgrade(root, context=context, request_id="recover-tooling", predecessors=predecessors, operation_control=clock)
    assert fired and first["status"] == "partial"
    second = recover_upgrade(root, context=context, request_id="recover-tooling", predecessors=predecessors, operation_control=clock)
    assert second["status"] == "applied" and second["attempts_used"] == 2 and second["plan_sha256"] == first["plan_sha256"]
    assert (root / "log.md").read_text().count("] upgrade |") == 1
    third = recover_upgrade(root, context=context, request_id="recover-tooling", predecessors=predecessors, operation_control=clock)
    assert third == second


def test_upgrade_saved_success_is_not_current_after_user_edit(upgrade):
    root, context, predecessors, clock = upgrade
    assert recover_upgrade(root, context=context, request_id="upgrade", predecessors=predecessors, operation_control=clock)["status"] == "applied"
    (root / "scripts/query_index.py").write_bytes(b"new user content")
    result = recover_upgrade(root, context=context, request_id="upgrade", predecessors=predecessors, operation_control=clock)
    assert result["status"] == "blocked_external" and result["reason"] == "historical_upgrade_not_current"


@pytest.mark.parametrize("attempts", [-1, "unreadable", 4])
def test_upgrade_malformed_retained_journal_is_preserved_with_unknown_count(upgrade, attempts):
    from evidence_wiki._pack_io import canonical
    root, context, predecessors, clock = upgrade
    request_id = "malformed-history"
    assert recover_upgrade(root, context=context, request_id=request_id, predecessors=predecessors, operation_control=clock)["status"] == "applied"
    with context.activate(), MutationStore(root, context.mutation_control_directory).locked(request_id) as store:
        state = store._read("recovery-journal.json")
        state["attempts_used"] = attempts
        original = canonical(state)
        store._publish("recovery-journal.json", original,
                       expected=owner("_usage_materialization").file_digest(store._read_raw("recovery-journal.json")))
    result = recover_upgrade(root, context=context, request_id=request_id, predecessors=predecessors, operation_control=clock)
    assert result["status"] == "blocked_external" and result["attempts_used"] is None
    with context.activate(), MutationStore(root, context.mutation_control_directory).locked(request_id) as store:
        assert store._read_raw("recovery-journal.json") == original


@pytest.mark.parametrize("stop", ["expiry", "cancellation"])
def test_upgrade_closing_capture_cannot_commit_success_after_control_stops(upgrade, monkeypatch, stop):
    root, context, predecessors, original_clock = upgrade
    cancelled = threading.Event()
    clock = OperationControl(deadline_utc=(datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat(),
                             timeout_seconds=2, cancel_requested=cancelled.is_set)
    initializer = owner("init_research_workspace")
    original = initializer.observe_tree
    captures = 0
    def delayed(selected):
        nonlocal captures
        result = original(selected)
        if selected == root:
            captures += 1
            if captures == 4:
                if stop == "expiry":
                    time.sleep(clock.remaining_seconds("closing_capture_probe") + 0.03)
                else:
                    cancelled.set()
        return result
    monkeypatch.setattr(initializer, "observe_tree", delayed)
    result = recover_upgrade(root, context=context, request_id="closing-upgrade", predecessors=predecessors, operation_control=clock)
    assert captures == 4 and result["status"] != "applied" and result["attempts_used"] == 1
    assert (root / "scripts/query_index.py").read_bytes() == (owner("init_research_workspace")._SCRIPT_DIR / "query_index.py").read_bytes()
    with context.activate(), MutationStore(root, context.mutation_control_directory).locked("closing-upgrade") as store:
        assert store._read("recovery-journal.json")["attempts_used"] == 1
        assert store._read("recovery-journal.json")["status"] != "applied"
    clock.close()


@pytest.mark.parametrize("placeholder", ["missing", "changed"])
def test_composed_upgrade_covers_complete_installed_assets_and_replays(upgrade, placeholder):
    root, context, predecessors, clock = upgrade
    from evidence_wiki._script_host import shared_assets_root
    initializer = owner("init_research_workspace")
    starter = shared_assets_root() / "workspace-template"
    paths = ("scripts", "docs", "skills")
    expected = {path.relative_to(starter).as_posix()
                for directory in paths for path in initializer.iter_managed_files(starter, directory)}
    assert 256 < len(expected) <= initializer.UPGRADE_RECOVERY_MAX_FILES - 2
    # Initialization customizes guidance; this selected predecessor fixture uses
    # the exact installed optional assets as the approved no-op baseline.
    for relative in expected:
        if relative.split("/")[0] in {"docs", "skills"}:
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((starter / relative).read_bytes())
    retained = root / "skills/.gitkeep"
    if placeholder == "missing":
        retained.unlink()
        predecessors["skills/.gitkeep"] = None
    else:
        retained.write_bytes(b"Permitted previous placeholder bytes.\n")
        predecessors["skills/.gitkeep"] = owner("_usage_materialization").file_digest(retained.read_bytes())
    for relative in expected | {"workspace-system.yml", "log.md"}:
        selected = root / relative
        predecessors[relative] = owner("_usage_materialization").file_digest(selected.read_bytes()) if selected.exists() else None
    assert len(predecessors) == len(expected) + 2 > 128
    first = recover_upgrade(root, context=context, request_id="composed-assets", predecessors=predecessors,
                            operation_control=clock, paths=paths)
    assert first["status"] == "applied"
    with context.activate(), MutationStore(root, context.mutation_control_directory).locked("composed-assets") as store:
        plan = store._read("recovery-plan.json")
        assert {row["path"] for row in plan["rows"]} == expected | {"workspace-system.yml", "log.md"}
    assert (root / "log.md").read_text().count("] upgrade |") == 1
    second = recover_upgrade(root, context=context, request_id="composed-assets", predecessors=predecessors,
                             operation_control=clock, paths=paths)
    assert second == first
    assert all((root / relative).read_bytes() == (starter / relative).read_bytes() for relative in expected)


@pytest.mark.parametrize("fault", ["result-construction", "terminal-journal-return"])
def test_upgrade_postpublication_failure_preserves_valid_history_and_recovers(upgrade, monkeypatch, fault):
    from evidence_wiki.operations_recovery import RecoveryJournal
    root, context, predecessors, clock = upgrade
    initializer = owner("init_research_workspace")
    original_result = initializer._upgrade_recovery_result
    original_save = RecoveryJournal.save_journal
    request_id = "postpublication-result"
    fired = False
    def result_fault(*args):
        nonlocal fired
        if not fired and args[0] == "applied":
            fired = True
            assert (root / "log.md").read_text().count("] upgrade |") == 1
            raise ValueError("Interrupted result construction")
        return original_result(*args)
    def journal_fault(self, state):
        nonlocal fired
        original_save(self, state)
        if not fired and state["status"] == "applied":
            fired = True
            raise OSError("Lost terminal journal publication return")
    with monkeypatch.context() as faults:
        if fault == "result-construction":
            faults.setattr(initializer, "_upgrade_recovery_result", result_fault)
        else:
            faults.setattr(RecoveryJournal, "save_journal", journal_fault)
        first = recover_upgrade(root, context=context, request_id=request_id, predecessors=predecessors, operation_control=clock)
    assert fired and first["status"] != "applied" and first["attempts_used"] == 1
    with context.activate(), MutationStore(root, context.mutation_control_directory).locked(request_id) as store:
        state, plan = store._read("recovery-journal.json"), store._read("recovery-plan.json")
        initializer._validate_upgrade_recovery(plan, state, starter_root=initializer._SCRIPT_DIR.parent, target=root, paths=["scripts"])
        assert state["attempts_used"] == 1 and {"scripts/query_index.py", "log.md"}.issubset(state["completed"])
        assert state["terminal"] == (fault == "terminal-journal-return")
    second = recover_upgrade(root, context=context, request_id=request_id, predecessors=predecessors, operation_control=clock)
    assert second["status"] == "applied" and second["attempts_used"] == (1 if fault == "terminal-journal-return" else 2)
    assert second["plan_sha256"] == first["plan_sha256"]
    assert (root / "log.md").read_text().count("] upgrade |") == 1


@pytest.mark.parametrize("relative,flag", [("skills/.private", True), ("../outside", True), ("skills/.gitkeep", 1)])
def test_managed_placeholder_allowance_cannot_publish_other_hidden_or_invalid_paths(upgrade, relative, flag):
    root, context, predecessors, clock = upgrade
    with pytest.raises(ValueError, match="managed_upgrade_placeholder_invalid"):
        owner("_usage_materialization").publish_file(root, relative, b"unapproved bytes", None, managed_placeholder=flag)
    assert not (root / "skills/.private").exists() and not (root.parent / "outside").exists()


@pytest.fixture
def protected(tmp_path, monkeypatch):
    fixture = UsageFixture(tmp_path, monkeypatch)
    (fixture.root / "research.yml").write_text(yaml.safe_dump(fixture.config))
    module = owner("_evidence_usage")
    fixture.transact(module, "initialize")
    body, files = fixture.source()
    fixture.transact(module, "deposit", body, files)
    private = tmp_path / "control"
    private.mkdir(mode=0o700)
    context = HostContext((fixture.root,), {"workspace.open", "evidence_usage.run_status", "evidence_usage.run_check", "evidence_usage.run_materialize",
        "operations.backup", "operations.restore"}, "operations-host", (fixture.root,), authority_file=fixture.policy_path,
        state_directory=fixture.host, mutation_control_directory=private)
    service = DisposableBackupService(fixture.root, context, tmp_path)
    clock = control()
    return fixture, body, service, context, clock


def recovery(protected, request_id="retained-backup", service_selected=True):
    fixture, body, service, context, clock = protected
    return HostBackupRecovery(fixture.root, context=context, service=service if service_selected else None,
                              request_id=request_id, operation_control=clock)


def test_protected_missing_file_restore_has_same_head_no_keys_export(protected):
    fixture, body, service, context, clock = protected
    before = Workspace.open(fixture.root).usage.status()
    operation = recovery(protected)
    prepared = operation.backup()
    assert prepared["status"] == "prepared"
    (fixture.host / "evidence-state.json").unlink()
    result = recovery(protected).restore()
    assert result["status"] == "restored"
    assert Workspace.open(fixture.root).usage.status() == before
    with context.activate(), Workspace.open(fixture.root) as workspace:
        materialized = workspace.usage.materialize(body["source_revision"])
        assert materialized["changed"]
        assert workspace.usage.check(body["source_revision"], uses=["retrieval"], purpose="research", consumer="evidence-wiki")["eligible"]
    assert json.dumps(result).find("keys") == -1


@pytest.mark.parametrize("stop", ["expiry", "cancellation"])
def test_protected_closing_capture_cannot_commit_success_after_control_stops(protected, monkeypatch, stop):
    from evidence_wiki import operations_recovery
    fixture, body, service, context, original_clock = protected
    cancelled = threading.Event()
    clock = OperationControl(deadline_utc=(datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat(),
                             timeout_seconds=2, cancel_requested=cancelled.is_set)
    operation = HostBackupRecovery(fixture.root, context=context, service=service, request_id="closing-backup", operation_control=clock)
    assert operation.backup()["status"] == "prepared"
    (fixture.host / "evidence-state.json").unlink()
    original = operations_recovery.protected_head
    captures = 0
    def delayed(*args):
        nonlocal captures
        result = original(*args)
        captures += 1
        if captures == 2:
            if stop == "expiry":
                time.sleep(clock.remaining_seconds("closing_capture_probe") + 0.03)
            else:
                cancelled.set()
        return result
    monkeypatch.setattr(operations_recovery, "protected_head", delayed)
    result = operation.restore()
    assert captures == 2 and result["status"] == "blocked_external" and result["attempts_used"] == 1
    assert (fixture.host / "evidence-state.json").exists()
    with context.activate(), MutationStore(fixture.root, service.anchor_directory).locked("closing-backup") as store:
        assert store._read("recovery-journal.json")["attempts_used"] == 1
        assert store._read("recovery-journal.json")["status"] == "blocked_external"
    clock.close()


def test_protected_old_valid_prefix_and_new_revocation_cannot_revive(protected):
    fixture, body, service, context, clock = protected
    assert recovery(protected).backup()["status"] == "prepared"
    old = (fixture.host / "evidence-state.json").read_bytes()
    fixture.revoke(owner("_evidence_usage"), body)
    service.checkpoint()
    result = recovery(protected).restore()
    assert result["status"] == "blocked_external"
    (fixture.host / "evidence-state.json").write_bytes(old)
    with pytest.raises(ValueError, match="regressed"):
        service.checkpoint()
    assert recovery(protected).restore()["status"] == "blocked_external"


def test_protected_user_edit_is_preserved(protected):
    fixture, body, service, context, clock = protected
    assert recovery(protected).backup()["status"] == "prepared"
    path = fixture.root / "research.yml"
    path.write_bytes(path.read_bytes() + b"# user content\n")
    before = path.read_bytes()
    assert recovery(protected).restore()["status"] == "blocked_external"
    assert path.read_bytes() == before


def test_protected_missing_service_is_noninteractive_terminal(protected):
    assert recovery(protected, service_selected=False).restore()["status"] == "blocked_external"


def test_protected_missing_recovery_journal_cannot_reset_attempts(protected):
    fixture, body, service, context, clock = protected
    assert recovery(protected).backup()["status"] == "prepared"
    with context.activate(), MutationStore(fixture.root, service.anchor_directory).locked("retained-backup") as store:
        owner("_host_evidence_store").os.unlink("recovery-journal.json", dir_fd=store._request)
    assert recovery(protected).restore()["status"] == "blocked_external"


def test_protected_unknown_callback_wait_is_terminal_and_never_redispatched(protected, monkeypatch):
    fixture, body, service, context, original_clock = protected
    clock = control(2)
    operation = HostBackupRecovery(fixture.root, context=context, service=service, request_id="bounded-service", operation_control=clock)
    assert operation.backup()["status"] == "prepared"
    (fixture.host / "evidence-state.json").unlink()
    release, finished = threading.Event(), threading.Event()
    original = service.restore
    calls = []
    def delayed(*args, **kwargs):
        calls.append(1)
        release.wait(5)
        try:
            return original(*args, **kwargs)
        finally:
            finished.set()
    monkeypatch.setattr(service, "restore", delayed)
    try:
        first = operation.restore()
        assert first["status"] == "blocked_external" and first["terminal"]
        second = operation.restore()
        assert second["attempts_used"] == first["attempts_used"] == 1 and len(calls) == 1
        assert not finished.is_set()
    finally:
        release.set()
        assert finished.wait(5)


def test_protected_permission_loss_is_noninteractive_terminal(protected):
    fixture, body, service, context, clock = protected
    restricted = replace(context, grants=context.grants - {"operations.restore"})
    operation = HostBackupRecovery(fixture.root, context=restricted, service=service, request_id="no-permission", operation_control=clock)
    assert operation.restore()["status"] == "blocked_external"


def test_protected_alias_backup_root_cannot_overlap_independent_anchor(protected):
    fixture, body, service, context, clock = protected
    alias = service.backup_directory.parent / "alias"
    alias.symlink_to(service.anchor_directory, target_is_directory=True)
    service.backup_directory = alias
    from evidence_wiki.errors import EvidenceWikiError
    with pytest.raises(EvidenceWikiError):
        recovery(protected)


def test_reopened_fixture_independent_chain_rejects_old_prefix(protected):
    fixture, body, service, context, clock = protected
    old = (fixture.host / "evidence-state.json").read_bytes()
    fixture.revoke(owner("_evidence_usage"), body)
    service.checkpoint()
    (fixture.host / "evidence-state.json").write_bytes(old)
    with pytest.raises(ValueError, match="regressed"):
        DisposableBackupService(fixture.root, context, service.anchor_directory.parent, create=False)


@pytest.mark.parametrize("change", ["missing-predecessor", "known-optional", "replacement-history"])
def test_full_upgrade_assets_require_predecessor_and_preserve_replacement_history(upgrade, change):
    root, context, predecessors, clock = upgrade
    paths = ("scripts",)
    if change == "missing-predecessor":
        path = root / "scripts/query_index.py"
        path.unlink()
        predecessors.pop("scripts/query_index.py")
    else:
        paths = ("scripts", "skills")
        path = root / "skills/research-query.md"
        path.write_bytes(b"Permitted prior installed guidance.\n")
        predecessors["skills/research-query.md"] = owner("_usage_materialization").file_digest(path.read_bytes())
        if change == "replacement-history":
            retained = root / ".replaced/skills/research-query.md"
            retained.parent.mkdir(parents=True)
            retained.write_bytes(b"Existing owner history\n")
    value = recover_upgrade(root, context=context, request_id="assets", predecessors=predecessors, operation_control=clock, paths=paths)
    assert value["status"] == ("applied" if change == "known-optional" else "blocked_external")
    if change == "replacement-history":
        assert retained.read_bytes() == b"Existing owner history\n" and path.read_bytes() == b"Permitted prior installed guidance.\n"
