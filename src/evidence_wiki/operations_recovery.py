"""Preauthorized host recovery with independent custody and canonical owners.

This boundary never receives backup bytes, keys, a replacement signer or worker
callbacks. A host backup service owns its storage and finite execution deadline.
Its independent latest-head commitment must survive every restorable root.
"""

from __future__ import annotations

import hashlib
import re
from contextvars import copy_context
from copy import deepcopy
from functools import wraps
from pathlib import Path
from typing import Protocol

from ._pack_io import bounded, canonical, identity, read_file
from .errors import EvidenceWikiError, UsageError
from .host_context import HostContext, _root_descriptor
from .mutation_contracts import digest, validate_request_id
from .mutation_store import MAX_RECORD_BYTES, MutationStore, capture_owner_generation
from .operation_control import OperationControl
from .pack_discovery import owner
from .research_execution import bounded_host_call
from .runtime_identity import generation as runtime_generation


class ProtectedBackupService(Protocol):
    """Trusted host service; paths and capabilities never come from worker JSON.

    ``latest_head`` is an independently preserved commitment, not a head read
    from a backup. Backup/restore must hold the service's own consistent custody
    across workspace, trust, state and original journals. Restore publishes only
    an exact latest complete generation through the native protected owners.
    Credentials remain in the service; unavailable capabilities raise or refuse.
    """

    anchor_directory: Path
    backup_directory: Path
    artifact_directories: tuple[Path, ...]
    service_ref: str

    def latest_head(self, *, timeout_seconds: float) -> str: ...
    def backup(self, request_id: str, head_sha256: str, *, timeout_seconds: float) -> dict: ...
    def inspect(self, reference: str, backup_sha256: str, head_sha256: str, *, timeout_seconds: float) -> dict: ...
    def restore(self, reference: str, backup_sha256: str, head_sha256: str, *, timeout_seconds: float) -> None: ...


def _refuse(reason):
    raise UsageError("HOST_RECOVERY_REFUSED", "The selected host recovery cannot proceed.",
                     recoverable=True, details={"reason": reason},
                     remediation="Retain original journals and use the preauthorized latest complete host backup.") from None


def _terminal_boundary(function):
    @wraps(function)
    def bounded(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (Exception, SystemExit):
            used = getattr(args[0], "_observed_attempts", None) if args else None
            return {"status": "blocked_external", "terminal": True, "recoverable": True,
                    "reason": "recovery_capability_unavailable", "attempts_used": used,
                    "authorized": False, "evidence_accepted": False}
    return bounded


def _tree(root):
    revision = owner("_evidence_revision").capture_workspace(root, max_attempts=1)
    return revision.revision_id


def _sha(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _record_digest(value):
    """Hash bounded private recovery metadata without changing mutation shapes."""
    bounded(value)
    raw = canonical(value)
    if len(raw) > MAX_RECORD_BYTES:
        _refuse("recovery_record_bound")
    return _sha(raw)


def protected_head(root: Path, context: HostContext) -> str:
    """Bind complete current workspace, protected state and cumulative journals.

    The return is a content commitment, never an authorization or a key export.
    This observation must be called in the host service's consistent custody.
    """
    if context.authority_file is None or context.state_directory is None or context.mutation_control_directory is None:
        _refuse("protected_selection_unavailable")
    trust = context.authority_file
    with _root_descriptor(trust.parent) as descriptor:
        owner("_host_evidence_store").private_entry(trust.lstat())
        trust_sha256 = _sha(read_file(descriptor, trust.name, 1_048_576))
    return digest({"workspace_binding": {"path": str(root), **identity(root)},
                   "workspace": _tree(root), "trust": trust_sha256,
                   "state": _tree(context.state_directory), "control": _tree(context.mutation_control_directory),
                   "generation": {"owners": capture_owner_generation(), "runtime": runtime_generation()}})


class RecoveryJournal:
    """Fixed private CAS slots under the existing native mutation store gates."""

    def __init__(self, store, *, predecessors=None, max_attempts=3, operation_control=None):
        self.store = store
        self.predecessors = deepcopy(predecessors or {})
        self.max_attempts = max_attempts
        self.operation_control = operation_control
        if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
            _refuse("attempt_limit_invalid")

    def load_plan(self):
        return self.store._read("recovery-plan.json")

    def save_plan(self, plan):
        self.store._publish("recovery-plan.json", canonical(plan), immutable=True)

    def load_journal(self):
        return self.store._read("recovery-journal.json")

    def save_journal(self, journal):
        previous = self.load_journal()
        if (previous is not None and (journal["attempts_used"] < previous["attempts_used"]
                or not set(previous.get("completed", [])).issubset(journal.get("completed", [])))):
            _refuse("journal_regressed")
        self.store._publish("recovery-journal.json", canonical(journal),
                            expected=None if previous is None else _record_digest(previous))

    def plan_sha256(self):
        return _record_digest(self.load_plan())

    def generation(self):
        return {"owners": capture_owner_generation(), "runtime": runtime_generation()}

    def checkpoint(self):
        if self.operation_control is not None:
            self.operation_control.checkpoint("recovery_publication")


@_terminal_boundary
def recover_upgrade(root, *, context, request_id, predecessors, operation_control, max_attempts=3, paths=("scripts",)):
    """Trusted host route to the canonical initializer's journaled full upgrade.

    Predecessors are exact hashes from a permitted installed generation or an
    explicitly approved host inventory. Observing unknown live bytes does not
    establish predecessor provenance. The original deadline is never renewed.
    """
    from ._script_host import shared_assets_root

    request_id = validate_request_id(request_id)
    if type(operation_control) is not OperationControl:
        _refuse("native_operation_control_required")
    if type(context) is not HostContext or context.mutation_control_directory is None:
        _refuse("control_unavailable")
    context.require_grant("operations.recover_upgrade")
    root = context.require_path(Path(root).absolute(), write=True)
    if type(predecessors) is not dict or len(predecessors) > owner("init_research_workspace").UPGRADE_RECOVERY_MAX_FILES:
        _refuse("predecessors_invalid")
    for path, value in predecessors.items():
        if (type(path) is not str or Path(path).is_absolute() or ".." in Path(path).parts
                or value is not None and (type(value) is not str or re.fullmatch(r"sha256:[a-f0-9]{64}", value) is None)):
            _refuse("predecessors_invalid")
    with context.activate(), MutationStore(root, context.mutation_control_directory).locked(request_id) as store:
        journal = RecoveryJournal(store, predecessors=predecessors, max_attempts=max_attempts, operation_control=operation_control)
        operation_control.checkpoint("recovery_upgrade")
        plan = journal.load_plan()
        if plan is not None and (plan.get("max_attempts") != max_attempts or plan.get("predecessors") != predecessors):
            _refuse("frozen_request_changed")
        try:
            return owner("init_research_workspace").upgrade_workspace(shared_assets_root() / "workspace-template",
                root, list(paths), False, transaction=journal)
        except (Exception, SystemExit):
            state = journal.load_journal()
            retained_plan = journal.load_plan()
            invalid_history = state is None and retained_plan is not None
            try:
                owner("init_research_workspace")._validate_upgrade_recovery(retained_plan, state,
                    starter_root=shared_assets_root() / "workspace-template", target=root, paths=list(paths))
            except (Exception, SystemExit):
                invalid_history = True
            if invalid_history:
                return {"status": "blocked_external", "terminal": True, "recoverable": True,
                        "reason": "attempt_history_unavailable", "attempts_used": None,
                        "authorized": False, "evidence_accepted": False}
            state = state or {"attempts_used": 1, "completed": [], "pending": None}
            state.update(status="blocked_external", terminal=True, reason="upgrade_preflight_refused")
            journal.save_journal(state)
            return {"status": "blocked_external", "terminal": True, "recoverable": True,
                    "reason": "upgrade_preflight_refused", "attempts_used": state["attempts_used"],
                    "authorized": False, "evidence_accepted": False}


class HostBackupRecovery:
    """Exact latest complete backup selection; no model-visible protected bytes."""

    def __init__(self, root, *, context, service, request_id, operation_control, max_attempts=3):
        if type(context) is not HostContext:
            _refuse("host_context_required")
        if type(operation_control) is not OperationControl:
            _refuse("native_operation_control_required")
        self.context, self.service, self.control = context, service, operation_control
        self._observed_attempts = None
        self.root = context.require_path(Path(root).absolute(), write=True)
        self.request_id = validate_request_id(request_id)
        self.max_attempts = max_attempts
        if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
            _refuse("attempt_limit_invalid")
        self.anchor = None
        if service is not None:
            self.anchor = Path(service.anchor_directory)
            backup = Path(service.backup_directory)
            if type(service.artifact_directories) not in (tuple, list) or len(service.artifact_directories) > 16:
                _refuse("artifact_roots_invalid")
            roots = [*context.allowed_roots, *service.artifact_directories, context.state_directory, context.mutation_control_directory,
                     context.authority_file.parent if context.authority_file else None, backup]
            self.service_roots = []
            for path in roots:
                if path is None:
                    continue
                path = Path(path)
                if not path.exists() and not path.is_symlink():
                    self.anchor = None
                    return
                if not path.is_absolute() or path.is_symlink() or path.resolve(strict=True) != path:
                    _refuse("service_root_alias_or_unavailable")
                with _root_descriptor(path):
                    self.service_roots.append((path, identity(path)))
            if any(backup.is_relative_to(path) or Path(path).is_relative_to(backup)
                   for path in (*context.allowed_roots, *service.artifact_directories)):
                _refuse("backup_overlaps_model_or_artifact_root")
            try:
                owner("_host_evidence_store").private_entry(backup.lstat(), directory=True)
            except (ValueError, OSError):
                self.anchor = None
                return
            if (not self.anchor.is_absolute() or self.anchor.is_symlink() or self.anchor.resolve() != self.anchor
                    or any(path is not None and (self.anchor.is_relative_to(path) or Path(path).is_relative_to(self.anchor)) for path in roots)):
                _refuse("independent_anchor_overlaps_restorable_root")
            if not self.anchor.exists():
                self.anchor = None
                return
            with _root_descriptor(self.anchor) as fd:
                try:
                    owner("_host_evidence_store").private_entry(owner("_host_evidence_store").os.fstat(fd), directory=True)
                except (ValueError, OSError):
                    self.anchor = None
                    return
            self.anchor_identity = identity(self.anchor)
            self.service_ref = service.service_ref
            if type(self.service_ref) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", self.service_ref) is None:
                _refuse("service_reference_invalid")

    def _latest(self):
        if self.anchor is None or self.anchor.is_symlink() or identity(self.anchor) != self.anchor_identity:
            _refuse("independent_anchor_unavailable")
        if any(path.is_symlink() or identity(path) != held for path, held in self.service_roots):
            _refuse("service_root_changed")
        for path, held in self.service_roots:
            with _root_descriptor(path) as descriptor:
                if identity(descriptor) != held:
                    _refuse("service_root_changed")
        with _root_descriptor(self.anchor) as descriptor:
            if identity(descriptor) != self.anchor_identity:
                _refuse("independent_anchor_changed")
            owner("_host_evidence_store").private_entry(owner("_host_evidence_store").os.fstat(descriptor), directory=True)
        value = self._call(self.service.latest_head)
        if type(value) is not str or re.fullmatch(r"sha256:[a-f0-9]{64}", value) is None:
            _refuse("latest_head_unavailable")
        return value

    def _call(self, function, *arguments):
        allowance = self.control.remaining_seconds("protected_recovery_service")
        selected = copy_context()
        value, reason = bounded_host_call(lambda: selected.run(function, *arguments, timeout_seconds=allowance), (), allowance)
        if reason is not None:
            _refuse("service_effect_or_wait_unknown")
        self.control.checkpoint("protected_recovery_service_returned")
        return value

    def _checked(self, plan, state, binding, generation):
        if (type(plan) is not dict or set(plan) != {"schema_version", "service_ref", "workspace_binding", "generation", "deadline_utc", "max_attempts", "backup"}
                or plan["schema_version"] != "host-backup-recovery/v1" or plan["workspace_binding"] != binding
                or plan["generation"] != generation or plan["deadline_utc"] != self.control.deadline_utc
                or type(plan["max_attempts"]) is not int or plan["max_attempts"] != self.max_attempts or plan["service_ref"] != self.service_ref):
            _refuse("frozen_backup_plan_changed")
        descriptor = plan["backup"]
        if (type(descriptor) is not dict or set(descriptor) != {"reference", "backup_sha256", "head_sha256"}
                or type(descriptor["reference"]) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", descriptor["reference"]) is None
                or any(type(descriptor[name]) is not str or re.fullmatch(r"sha256:[a-f0-9]{64}", descriptor[name]) is None for name in ("backup_sha256", "head_sha256"))):
            _refuse("backup_descriptor_invalid")
        if (type(state) is not dict or set(state) != {"status", "terminal", "attempts_used", "reason"}
                or type(state["attempts_used"]) is not int or not 0 <= state["attempts_used"] <= self.max_attempts
                or state["status"] not in {"prepared", "restoring", "restored", "partial", "blocked_external"}
                or type(state["terminal"]) is not bool or state["terminal"] != (state["status"] in {"restored", "blocked_external"})
                or state["reason"] not in {None, "latest_authorized_recovery_unavailable", "host_restore_unknown"}):
            _refuse("backup_journal_invalid_or_missing")

    def _result(self, state, plan=None):
        used = state.get("attempts_used")
        used = used if type(used) is int and 0 <= used <= self.max_attempts else None
        if used is not None:
            self._observed_attempts = used
        return {"status": state["status"], "terminal": state["terminal"], "recoverable": state["status"] != "restored",
                "reason": state.get("reason"), "attempts_used": used,
                "plan_sha256": _record_digest(plan) if plan else None, "authorized": False, "evidence_accepted": False}

    @_terminal_boundary
    def backup(self):
        """Freeze one opaque complete backup under host-selected consistent custody."""
        self.context.require_grant("operations.backup")
        if self.service is None:
            return self._result({"status": "blocked_external", "terminal": True, "reason": "backup_service_unavailable", "attempts_used": None})
        with self.context.activate(), MutationStore(self.root, self.anchor).locked(self.request_id) as store:
            journal = RecoveryJournal(store, operation_control=self.control, max_attempts=self.max_attempts)
            plan = journal.load_plan()
            state = journal.load_journal()
            if plan is None:
                try:
                    if state is not None:
                        return self._result({"status": "blocked_external", "terminal": True,
                                             "reason": "backup_intent_or_history_incomplete", "attempts_used": state.get("attempts_used")})
                    state = {"status": "prepared", "terminal": False, "attempts_used": 0, "reason": None}
                    journal.save_journal(state)
                    latest = self._latest()
                    if protected_head(self.root, self.context) != latest:
                        _refuse("current_head_not_anchored")
                    descriptor = self._call(self.service.backup, self.request_id, latest)
                    if (type(descriptor) is not dict or set(descriptor) != {"reference", "backup_sha256", "head_sha256"}
                            or type(descriptor["reference"]) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", descriptor["reference"]) is None
                            or type(descriptor["backup_sha256"]) is not str or re.fullmatch(r"sha256:[a-f0-9]{64}", descriptor["backup_sha256"]) is None
                            or descriptor["head_sha256"] != latest or protected_head(self.root, self.context) != latest or self._latest() != latest):
                        _refuse("backup_descriptor_invalid_or_changed")
                    plan = {"schema_version": "host-backup-recovery/v1", "service_ref": self.service_ref,
                            "workspace_binding": store.workspace_binding, "generation": journal.generation(),
                            "deadline_utc": self.control.deadline_utc, "max_attempts": self.max_attempts, "backup": descriptor}
                    journal.save_plan(plan)
                except (Exception, SystemExit):
                    state.update(status="blocked_external", terminal=True, reason="latest_authorized_recovery_unavailable")
                    journal.save_journal(state)
                    return self._result({"status": "blocked_external", "terminal": True, "reason": "complete_backup_unavailable", "attempts_used": 0})
            try:
                self._checked(plan, state, store.workspace_binding, journal.generation())
                if self._latest() != plan["backup"]["head_sha256"]:
                    _refuse("backup_not_latest")
            except (Exception, SystemExit):
                return self._result({"status": "blocked_external", "terminal": True, "reason": "frozen_backup_unavailable", "attempts_used": state.get("attempts_used") if type(state) is dict else None}, plan)
            return self._result(state, plan)

    @_terminal_boundary
    def restore(self):
        """Restore only the independently anchored latest complete generation.

        Lost returns remain partial and use the same opaque backup on a finite
        retry. The independent journal and original budget are never restored.
        Current canonical eligibility must be checked by the consuming operation.
        """
        from .workspace import Workspace

        self.context.require_grant("operations.restore")
        if self.service is None:
            return self._result({"status": "blocked_external", "terminal": True, "reason": "backup_service_unavailable", "attempts_used": None})
        with self.context.activate(), MutationStore(self.root, self.anchor).locked(self.request_id) as store:
            journal = RecoveryJournal(store, operation_control=self.control, max_attempts=self.max_attempts)
            plan = journal.load_plan()
            state = journal.load_journal()
            try:
                self._checked(plan, state, store.workspace_binding, journal.generation())
            except (Exception, SystemExit):
                return self._result({"status": "blocked_external", "terminal": True, "reason": "frozen_backup_unavailable", "attempts_used": state.get("attempts_used") if type(state) is dict else None}, plan)
            if state["terminal"]:
                if state["status"] == "restored":
                    try:
                        if self._latest() != plan["backup"]["head_sha256"] or protected_head(self.root, self.context) != self._latest():
                            _refuse("historical_restore_not_current")
                    except (Exception, SystemExit):
                        return self._result({**state, "status": "blocked_external", "reason": "historical_restore_not_current"}, plan)
                return self._result(state, plan)
            state["attempts_used"] += 1
            self._observed_attempts = state["attempts_used"]
            state.update(status="restoring", reason=None)
            journal.save_journal(state)
            result = None
            try:
                if (plan is None or plan["workspace_binding"] != store.workspace_binding or plan["service_ref"] != self.service_ref
                        or plan["generation"] != journal.generation() or plan["deadline_utc"] != self.control.deadline_utc
                        or plan["max_attempts"] != self.max_attempts or state["attempts_used"] > self.max_attempts):
                    _refuse("frozen_plan_changed_or_attempt_limit")
                latest = self._latest()
                descriptor = plan["backup"]
                if descriptor["head_sha256"] != latest:
                    _refuse("backup_is_not_latest_complete_head")
                inspection = self._call(self.service.inspect, descriptor["reference"], descriptor["backup_sha256"], latest)
                if type(inspection) is not dict or inspection != {"status": "restorable", "head_sha256": latest}:
                    _refuse("unexplained_edits_or_incomplete_backup")
                self._call(self.service.restore, descriptor["reference"], descriptor["backup_sha256"], latest)
                if self._latest() != latest or protected_head(self.root, self.context) != latest:
                    _refuse("restored_head_or_authority_changed")
                with Workspace.open(self.root) as workspace:
                    status = workspace.usage.status()
                    if not status["initialized"]:
                        _refuse("canonical_state_unavailable")
                if plan["generation"] != journal.generation() or self._latest() != latest or protected_head(self.root, self.context) != latest:
                    _refuse("closing_generation_or_head_changed")
                success = {**state, "status": "restored", "terminal": True}
                result = self._result(success, plan)
                self.control.checkpoint("protected_recovery_closing")
                state.update(status="restored", terminal=True)
            except (EvidenceWikiError, ValueError, OSError, SystemExit):
                state.update(status="blocked_external", terminal=True, reason="latest_authorized_recovery_unavailable")
            except Exception:
                state.update(status="partial", terminal=state["attempts_used"] >= self.max_attempts, reason="host_restore_unknown")
                if state["terminal"]:
                    state["status"] = "blocked_external"
            if state["status"] != "restored":
                result = self._result(state, plan)
            journal.save_journal(state)
            return result
