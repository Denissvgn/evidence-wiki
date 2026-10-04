"""Explicit host selections for in-process calls; references do not authenticate.

Activate a context around calls on existing handles. Each supported operation
checks its actual grant and paths automatically. Only calls with no context use
the legacy authority/state environment variables. Signing and attestation remain
owned by the protected host and the canonical evidence verifier.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError

_ACTIVE: ContextVar[HostContext | None] = ContextVar("evidence_wiki_host_context", default=None)
_GENERATION: ContextVar[tuple[Path, str] | None] = ContextVar("evidence_wiki_host_generation", default=None)
_MUTATION_GATE: ContextVar[_MutationWorkspaceGate | None] = ContextVar("evidence_wiki_mutation_workspace_gate", default=None)
_HISTORICAL_READ: ContextVar[bool] = ContextVar("evidence_wiki_historical_read_scope", default=False)
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}$")
_AUTHORITY_ENV = "EVIDENCE_WIKI_AUTHORITY_FILE"
_STATE_ENV = "EVIDENCE_WIKI_STATE_DIR"


def _refuse(reason: str) -> None:
    raise ConfigError("HOST_CONTEXT_REFUSED", "The host context does not permit this call.",
                      recoverable=False, details={"reason": reason},
                      remediation="Supply current host selections and grants for the requested operation.")


def _reference(value: Any) -> str:
    if not isinstance(value, str) or _IDENTITY.fullmatch(value) is None:
        _refuse("host_reference_invalid")
    return value


def _references(values: Any) -> tuple[tuple[str, str], ...]:
    if isinstance(values, Mapping):
        values = tuple(values.items())
    if not isinstance(values, (list, tuple)) or len(values) > 128:
        _refuse("host_references_invalid")
    rows = []
    for row in values:
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            _refuse("host_references_invalid")
        rows.append((_reference(row[0]), _reference(row[1])))
    if len({key for key, _ in rows}) != len(rows):
        _refuse("host_references_duplicate")
    return tuple(sorted(rows))


def _path(value: Any) -> Path:
    if not isinstance(value, (str, Path)) or not str(value) or "\0" in str(value):
        _refuse("host_path_invalid")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        _refuse("host_path_invalid")
    return path


def _host_os():
    # The native Windows owner uses the package loader, so resolve it only after
    # the context module and loader have completed their imports.
    from ._filesystem import os as filesystem_os

    return filesystem_os


@contextmanager
def _root_descriptor(root: Path) -> Iterator[int]:
    os = _host_os()
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        _refuse("host_root_capture_unsupported")
    descriptors = []
    try:
        try:
            descriptor = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            descriptors.append(descriptor)
            for part in root.parts[1:]:
                descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                descriptors.append(descriptor)
        except OSError:
            _refuse("host_root_unavailable")
        yield descriptor
        try:
            for parent, child, part in zip(descriptors[:-1], descriptors[1:], root.parts[1:], strict=True):
                actual = os.stat(part, dir_fd=parent, follow_symlinks=False)
                held = os.fstat(child)
                if (actual.st_dev, actual.st_ino) != (held.st_dev, held.st_ino):
                    _refuse("host_root_changed")
        except OSError:
            _refuse("host_root_unavailable")
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _roots(values: Any) -> tuple[tuple[Path, tuple[int, int]], ...]:
    if not isinstance(values, (list, tuple)) or len(values) > 16:
        _refuse("host_roots_invalid")
    result = []
    for value in values:
        supplied = _path(value)
        try:
            if supplied.is_symlink():
                _refuse("host_root_link")
            root = supplied.resolve(strict=True)
            with _root_descriptor(root) as descriptor:
                info = _host_os().fstat(descriptor)
                if not stat.S_ISDIR(info.st_mode):
                    _refuse("host_root_invalid")
                result.append((root, (info.st_dev, info.st_ino)))
        except (OSError, ValueError, RuntimeError):
            _refuse("host_root_unavailable")
    if len({root for root, _ in result}) != len(result):
        _refuse("host_root_duplicate")
    return tuple(result)


@dataclass(frozen=True)
class HostContext:
    """Trusted application selections, independent of worker-supplied envelopes.

    Grants use the actual canonical ``owner.seam`` name, or an
    ``onboarding.operation`` name. Writable roots must be contained in allowed
    roots. Controller and credential references are opaque declarations; this
    class never signs or treats those strings as authenticated principals.
    """

    allowed_roots: tuple[Path, ...]
    grants: frozenset[str]
    actor_ref: str
    writable_roots: tuple[Path, ...] = ()
    controller_refs: tuple[tuple[str, str], ...] = ()
    authority_file: Path | None = field(default=None, repr=False)
    state_directory: Path | None = field(default=None, repr=False)
    credential_refs: tuple[tuple[str, str], ...] = field(default=(), repr=False)
    mutation_control_directory: Path | None = field(default=None, repr=False)
    role_controller_refs: tuple[tuple[str, str, str], ...] = ()
    _root_bindings: tuple[tuple[Path, tuple[int, int]], ...] = field(init=False, repr=False)
    _write_bindings: tuple[tuple[Path, tuple[int, int]], ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        roots, writes = _roots(self.allowed_roots), _roots(self.writable_roots)
        if not isinstance(self.grants, (list, tuple, set, frozenset)) or len(self.grants) > 256:
            _refuse("host_grants_invalid")
        grants = frozenset(_reference(value) for value in self.grants)
        if any(not any(root.is_relative_to(parent) for parent, _ in roots) for root, _ in writes):
            _refuse("host_write_root_outside_scope")
        object.__setattr__(self, "actor_ref", _reference(self.actor_ref))
        object.__setattr__(self, "grants", grants)
        object.__setattr__(self, "controller_refs", _references(self.controller_refs))
        selected = self.role_controller_refs
        if type(selected) not in (list, tuple) or len(selected) > 128:
            _refuse("host_role_controllers_invalid")
        rows = []
        for row in selected:
            if type(row) not in (list, tuple) or len(row) != 3 or type(row[1]) is not str or row[1] not in {"host_worker", "evaluator"}:
                _refuse("host_role_controllers_invalid")
            rows.append(tuple(_reference(value) for value in row))
        if len({row[0] for row in rows}) != len(rows):
            _refuse("host_role_controllers_duplicate")
        object.__setattr__(self, "role_controller_refs", tuple(sorted(rows)))
        object.__setattr__(self, "credential_refs", _references(self.credential_refs))
        object.__setattr__(self, "allowed_roots", tuple(root for root, _ in roots))
        object.__setattr__(self, "writable_roots", tuple(root for root, _ in writes))
        object.__setattr__(self, "_root_bindings", roots)
        object.__setattr__(self, "_write_bindings", writes)
        for field_name in ("authority_file", "state_directory", "mutation_control_directory"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, _path(value))
        for root, _ in roots:
            self._protect(root)

    def controller_for(self, role: str, config_ref: str) -> str | None:
        """Select an exact configured controller; explicit mappings fail closed."""
        if self.role_controller_refs:
            return next((controller for config, selected_role, controller in self.role_controller_refs
                         if config == config_ref and selected_role == role), None)
        return dict(self.controller_refs).get(role)

    def _protect(self, path: Path) -> None:
        for selected in (self.authority_file, self.state_directory, self.mutation_control_directory):
            if selected is not None:
                protected = selected.resolve()
                if path.is_relative_to(protected) or protected.is_relative_to(path):
                    _refuse("host_path_overlaps_authority")

    def require_grant(self, operation: str) -> None:
        if operation not in self.grants:
            _refuse("host_operation_not_granted")
        if self.mutation_control_directory is not None and operation == "onboarding.research.ingest":
            _refuse("host_mutation_compound_writer_unsupported")

    def require_path(self, value: Any, *, write: bool = False) -> Path:
        supplied = _path(value)
        try:
            if supplied.is_symlink():
                _refuse("host_path_link")
            path = supplied.resolve()
        except (OSError, ValueError, RuntimeError):
            _refuse("host_path_unavailable")
        bindings = self._write_bindings if write else self._root_bindings
        if not any(path.is_relative_to(root) for root, _ in bindings):
            _refuse("host_path_outside_scope")
        self._protect(path)
        for root, expected in bindings:
            if path.is_relative_to(root):
                with _root_descriptor(root) as descriptor:
                    info = _host_os().fstat(descriptor)
                    if (info.st_dev, info.st_ino) != expected:
                        _refuse("host_root_changed")
        return path

    @contextmanager
    def activate(self) -> Iterator[HostContext]:
        """Select this immutable context; operations determine their own grants."""
        token = _ACTIVE.set(self)
        generation_token = _GENERATION.set(None)
        try:
            yield self
        finally:
            _GENERATION.reset(generation_token)
            _ACTIVE.reset(token)


def current_context() -> HostContext | None:
    return _ACTIVE.get()


def _execution_owner() -> tuple[int, object | None]:
    import asyncio
    import threading

    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return threading.get_ident(), task


@dataclass(repr=False)
class _MutationWorkspaceGate:
    context: HostContext | None
    workspace_root: Path
    control_root: Path
    workspace_identity: tuple[str, str]
    control_identity: tuple[str, str]
    descriptor: int
    descriptor_identity: tuple[int, int]
    execution_owner: tuple[int, object | None]
    active: bool = True
    read_only: bool = False


@contextmanager
def _mutation_workspace_gate(workspace_root: Path, control_root: Path, *, workspace_binding: Mapping[str, str],
                             control_identity: Mapping[str, str], gate_descriptor: int, read_only: bool = False) -> Iterator[None]:
    """Bind one acquired native store gate to its current execution and selection."""
    if _MUTATION_GATE.get() is not None:
        _refuse("host_mutation_gate_nested")
    info = _host_os().fstat(gate_descriptor)
    gate = _MutationWorkspaceGate(current_context(), workspace_root, control_root,
        (workspace_binding["device"], workspace_binding["inode"]),
        (control_identity["device"], control_identity["inode"]), gate_descriptor,
        (info.st_dev, info.st_ino), _execution_owner())
    gate.read_only = read_only
    token = _MUTATION_GATE.set(gate)
    try:
        yield
    finally:
        gate.active = False
        _MUTATION_GATE.reset(token)


def _require_mutation_workspace_gate(root: Path, *, allow_read_only: bool = False) -> None:
    """Refuse direct serialized writers without the exact live private native gate."""
    import hashlib

    if _HISTORICAL_READ.get() and not allow_read_only:
        _refuse("host_mutation_workspace_gate_read_only")
    context, gate = current_context(), _MUTATION_GATE.get()
    if context is None or context.mutation_control_directory is None:
        return
    execution = _execution_owner()
    if (gate is None or not gate.active or gate.context is not context or gate.workspace_root != root
            or gate.execution_owner[0] != execution[0] or gate.execution_owner[1] is not execution[1]):
        _refuse("host_mutation_workspace_gate_required")
    if gate.read_only and not allow_read_only:
        _refuse("host_mutation_workspace_gate_read_only")
    os = _host_os()
    try:
        from .pack_discovery import owner

        private_entry = owner("_host_evidence_store").private_entry
        selected = context.mutation_control_directory
        if selected.is_symlink() or selected.resolve(strict=True) != gate.control_root:
            _refuse("host_mutation_workspace_gate_changed")
        for path, expected in ((root, gate.workspace_identity), (gate.control_root, gate.control_identity)):
            with _root_descriptor(path) as descriptor:
                info = os.fstat(descriptor)
                if (str(info.st_dev), str(info.st_ino)) != expected:
                    _refuse("host_mutation_workspace_gate_changed")
                if path == gate.control_root:
                    private_entry(info, directory=True)
        parent = gate.control_root / "mutations/workspaces" / hashlib.sha256(str(root).encode()).hexdigest()
        with _root_descriptor(parent) as descriptor:
            private_entry(os.fstat(descriptor), directory=True)
            named = os.stat("workspace.lock", dir_fd=descriptor, follow_symlinks=False)
            held = os.fstat(gate.descriptor)
            private_entry(named)
            private_entry(held)
            if any(not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size
                   or (info.st_dev, info.st_ino) != gate.descriptor_identity for info in (named, held)):
                _refuse("host_mutation_workspace_gate_changed")
    except (OSError, ValueError, RuntimeError):
        _refuse("host_mutation_workspace_gate_changed")


def _require_borrowed_mutation_workspace_gate(root: Path, control: Path) -> None:
    """Verify read-only custody borrowed from the exact current execution."""
    context = current_context()
    if (context is None or context.mutation_control_directory is None
            or context.mutation_control_directory.resolve(strict=True) != control):
        _refuse("host_mutation_workspace_gate_required")
    _require_mutation_workspace_gate(root, allow_read_only=True)


def require_actor(value: Any) -> None:
    context = current_context()
    if context is not None and value is not None and value != context.actor_ref:
        _refuse("host_actor_mismatch")


def reject_unsupported(operation: str) -> None:
    """Refuse an unavailable explicit-context route before legacy effects."""
    if current_context() is not None:
        _refuse("host_operation_unsupported")


@contextmanager
def checked_workspace(operation: str, root: Path, *, paths: tuple[Path, ...] = ()) -> Iterator[None]:
    """Check direct handle reads that do not go through a canonical seam."""
    context = current_context()
    if context is None:
        yield
        return
    context.require_grant(operation)
    selected = context.require_path(root)
    for path in paths:
        context.require_path(path)
    with _root_descriptor(selected):
        yield
        context.require_path(root)
        for path in paths:
            context.require_path(path)


def selection_identity() -> object:
    """Private selection binding for retained views; never a serialized result."""
    context = current_context()
    if context is not None:
        return context
    return (_AUTHORITY_ENV, os.environ.get(_AUTHORITY_ENV), _STATE_ENV, os.environ.get(_STATE_ENV))


def authority_path(project_root: Path | None = None) -> str | None:
    context = current_context()
    if context is None:
        return os.environ.get(_AUTHORITY_ENV)
    if project_root is not None:
        context.require_path(project_root)
    return str(context.authority_file) if context.authority_file is not None else None


def state_path(project_root: Path | None = None) -> str | None:
    context = current_context()
    if context is None:
        return os.environ.get(_STATE_ENV)
    if project_root is not None:
        context.require_path(project_root)
    return str(context.state_directory) if context.state_directory is not None else None


def protected_paths() -> tuple[Path, ...]:
    context = current_context()
    control = context.mutation_control_directory if context is not None else None
    return tuple(Path(value) for value in (authority_path(), state_path(), control) if value)


def controller_allowed(role: str, controller: str) -> bool:
    context = current_context()
    if context is not None and context.role_controller_refs and role == "evaluator":
        return any(selected_role == role and selected == controller
                   for _, selected_role, selected in context.role_controller_refs)
    expected = dict(context.controller_refs).get(role) if context is not None else None
    return expected is None or expected == controller


def check_generation(root: Path, tree_hash: str) -> None:
    """Called by the trusted loader before cached or new owners are returned."""
    if current_context() is None:
        return
    pinned = _GENERATION.get()
    if pinned is not None and pinned != (root, tree_hash):
        _refuse("host_owner_generation_changed")


def loading_lock():
    """Share the packaged import lock with every indirectly imported loader."""
    from ._script_host import _LOAD_LOCK

    return _LOAD_LOCK


def compiled_source_code(source: bytes, filename: str):
    """Share exact-byte compilation while every loader preserves module isolation."""
    from ._script_host import compiled_source_code as compile_source

    return compile_source(source, filename)


@contextmanager
def call_binding() -> Iterator[None]:
    """Pin one installed owner generation across a complete operation."""
    if current_context() is None:
        yield
        return
    from ._script_host import owner_generation

    selected = owner_generation()
    check_generation(*selected)
    token = _GENERATION.set(selected)
    try:
        with ExitStack() as stack:
            context = current_context()
            for root, expected in context._root_bindings:
                descriptor = stack.enter_context(_root_descriptor(root))
                info = _host_os().fstat(descriptor)
                if (info.st_dev, info.st_ino) != expected:
                    _refuse("host_root_changed")
            yield
            check_generation(*owner_generation())
    finally:
        _GENERATION.reset(token)


# Only known seam effects may run under an explicit context. The canonical
# owners still validate their arguments and authenticate evidence independently.
_READ_SEAMS = frozenset({
    ("review_page_operations", "prepare"),
    ("claim_diagnostics", "current"),
    ("acquisition_operations", "prepare"), ("acquisition_operations", "reconcile"),
    ("retrieval_operations", "search"),
    ("source_usability_operations", "current"),
    ("request_lifecycle_operations", "current"),
    ("requirements_operations", "current"), ("requirements_operations", "prepare_revision"),
    ("run_controller", "run_telemetry_events"),
    ("export_answers", "run_export"),
    ("publication_readiness", "run_selected_publication"), ("workspace_status", "run_status_report"),
    ("normalize_verify", "run_verify"),
    ("qualified_packet", "validate_source"), ("qualified_packet", "validate_execution"),
    ("qualified_packet", "validate_market"), ("evidence_usage", "run_status"),
    ("evidence_usage", "run_check"), ("evidence_usage", "run_lineage"),
    ("evidence_temporal", "run_evaluate"), ("evidence_snapshots", "run_prepare"),
    ("evidence_snapshots", "run_check"),
    ("source_requests", "run_list_requests"),
})
_WRITE_SEAMS = frozenset({
    ("acquisition_operations", "fulfill"),
    ("retrieval_operations", "build_index"),
    ("request_lifecycle_operations", "link_current"), ("request_lifecycle_operations", "fulfill_current"),
    ("request_lifecycle_operations", "block_current"), ("request_lifecycle_operations", "reopen_current"),
    ("mutation_capture_recovery", "resume"),
    ("requirements_operations", "revise"),
    ("run_controller", "run_telemetry_record"),
    ("run_controller", "run_session_start"), ("run_controller", "run_session_record"),
    ("run_controller", "run_session_transition"), ("run_controller", "run_session_finish"),
    ("strict_evidence", "run_admit_document"),
    ("coverage_manifest", "run_evaluate"), ("doctor", "run_doctor"),
    ("coverage_manifest", "run_evaluate_checked"),
    ("evidence_snapshots", "run_export"),
    ("coverage_manifest", "run_init_inline"), ("coverage_manifest", "run_link"),
    ("coverage_manifest", "run_revision"),
    ("source_requests", "run_add_request"), ("source_requests", "run_fulfill_request"),
    ("source_requests", "run_link_request"),
    ("source_requests", "run_fulfill_current_request"),
    ("question_claim", "run_claim"), ("question_claim", "run_release"),
    ("question_resolve", "run_answer"), ("question_resolve", "run_block"),
    ("question_resolve", "run_defer"), ("question_resolve", "run_reject"),
    ("question_resolve", "run_reopen"), ("question_resolve", "run_grounding_set"),
    ("evidence_usage", "run_transact"), ("evidence_usage", "run_materialize"),
})
_SERIALIZED_WRITERS = frozenset({
    ("retrieval_operations", "build_index"),
    ("request_lifecycle_operations", "link_current"), ("request_lifecycle_operations", "fulfill_current"),
    ("request_lifecycle_operations", "block_current"), ("request_lifecycle_operations", "reopen_current"),
    ("run_controller", "run_session_start"), ("run_controller", "run_session_record"),
    ("run_controller", "run_session_transition"), ("run_controller", "run_session_finish"),
    ("run_controller", "run_telemetry_record"),
    ("strict_evidence", "run_admit_document"),
    ("coverage_manifest", "run_init_inline"), ("coverage_manifest", "run_link"),
    ("coverage_manifest", "run_evaluate"), ("coverage_manifest", "run_evaluate_checked"),
    ("coverage_manifest", "run_revision"),
    ("intake_questions", "run_intake"), ("intake_questions", "run_intake_document"),
})


def _historical_read_allowed(key: tuple[str, str], kwargs: Mapping[str, Any]) -> bool:
    """Narrow every checked owning call while historical custody is active."""
    if key in {("fleet_status", "run_fleet_status"), ("workspace_status", "run_status_report")}:
        return kwargs.get("no_cache", False) is True
    if key == ("verify_quotes", "run_verify"):
        return kwargs.get("write", False) is False
    if key in {("intake_questions", "run_intake"), ("intake_questions", "run_intake_document"),
               ("source_inventory", "run_inventory_report"), ("normalize_sources", "run_normalization_report")}:
        return kwargs.get("dry_run", False) is True
    if key == ("strict_evidence", "run_operation"):
        return kwargs.get("operation") in {"check", "prepare-review", "export"}
    if key in _READ_SEAMS or key in {("qualified_packet", "profiles"), ("evidence_snapshots", "run_verify")}:
        return True
    stem, seam = key
    if stem == "mutation_operations":
        return seam in {"revision", "inspect"}
    if stem == "research_session":
        return seam in {"current", "next_work", "session", "pending", "report", "prepare_acquisition", "recovery_status"}
    if stem == "telemetry":
        return seam in {"report", "project"}
    if key == ("evidence_assessments", "run_operation"):
        return kwargs.get("operation") in {"prepare", "check", "plan-refresh"}
    return False


@contextmanager
def checked_operation(stem: str, seam: str, args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> Iterator[Any]:
    """Authorize actual owner arguments before loading or invoking its seam."""
    if _HISTORICAL_READ.get() and not _historical_read_allowed((stem, seam), kwargs):
        _refuse("host_mutation_workspace_gate_read_only")
    context = current_context()
    if context is None:
        yield lambda: None
        return
    key, operation = (stem, seam), f"{stem}.{seam}"
    if key == ("coverage_manifest", "run_evaluate_checked"):
        operation = "coverage_manifest.run_evaluate"
    elif key == ("mutation_capture_recovery", "resume"):
        operation = "source_capture_operations.resume"
    write = key in _WRITE_SEAMS
    roots = args[:1]
    rootless = key in {("qualified_packet", "profiles"), ("evidence_snapshots", "run_verify")}
    if rootless:
        roots = ()
    elif key == ("fleet_status", "run_fleet_status"):
        roots = args[0] if args and isinstance(args[0], (list, tuple)) else ()
        if not roots:
            _refuse("host_operation_roots_invalid")
        if type(kwargs.get("no_cache", False)) is not bool:
            _refuse("host_operation_effect_invalid")
        write = not kwargs.get("no_cache", False)
    elif key == ("workspace_status", "run_status_report"):
        if type(kwargs.get("no_cache", False)) is not bool:
            _refuse("host_operation_effect_invalid")
        write = not kwargs.get("no_cache", False)
    elif key == ("verify_quotes", "run_verify"):
        if type(kwargs.get("write", False)) is not bool:
            _refuse("host_operation_effect_invalid")
        write = kwargs.get("write", False)
    elif key == ("intake_questions", "run_intake"):
        if type(kwargs.get("dry_run", False)) is not bool:
            _refuse("host_operation_effect_invalid")
        write = not kwargs.get("dry_run", False)
    elif key in {("source_inventory", "run_inventory_report"), ("normalize_sources", "run_normalization_report")}:
        flags = ("dry_run", "append_log", "require_checksum", "reject_mismatch") if stem == "source_inventory" else (
            "all_sources", "dry_run", "force", "append_log", "allow_external")
        if any(type(kwargs.get(flag, False)) is not bool for flag in flags):
            _refuse("host_operation_effect_invalid")
        write = not kwargs.get("dry_run", False)
        if stem == "normalize_sources" and kwargs.get("allow_external", False) and write:
            context.require_grant("normalize_sources.external")
    elif key == ("strict_evidence", "run_operation"):
        effect = kwargs.get("operation")
        if effect not in {"check", "prepare-review", "review", "export"}:
            _refuse("host_operation_unknown")
        operation += "." + effect
        write = effect == "review"
    elif stem == "mutation_operations" and seam in {"revision", "prepare_request", "submit", "inspect", "reconcile"}:
        write = seam in {"submit", "reconcile"}
    elif stem == "research_session" and seam in {"current", "next_work", "session", "advance", "run_until_terminal", "run_cycle", "acquire", "acquire_evidence", "choose_acquisition", "run_acquisition", "examine_coverage", "examine_counterevidence", "calculate", "propose_calculation", "assemble_review_packet", "recovery_status", "reconcile_recovery", "host_job",
                                               "pending", "reconcile_pending", "cancel_pending", "intake_observation", "report", "prepare_acquisition"}:
        write = seam in {"advance", "run_until_terminal", "run_cycle", "acquire", "acquire_evidence", "choose_acquisition", "run_acquisition", "examine_coverage", "examine_counterevidence", "calculate", "propose_calculation", "assemble_review_packet", "reconcile_recovery", "host_job", "reconcile_pending", "cancel_pending", "intake_observation"}
    elif stem == "telemetry" and seam in {"record", "report", "project"}:
        write = seam == "record"
    elif stem == "telemetry_delivery" and seam in {"deliver", "inspect", "retry"}:
        write = False
    elif key == ("intake_questions", "run_intake_document"):
        operation = "intake_questions.run_intake"
        if type(kwargs.get("dry_run", False)) is not bool:
            _refuse("host_operation_effect_invalid")
        write = not kwargs.get("dry_run", False)
    elif key == ("evidence_assessments", "run_operation"):
        effect = kwargs.get("operation")
        if effect not in {"prepare", "issue", "check", "plan-refresh", "apply-refresh"}:
            _refuse("host_operation_unknown")
        operation += "." + effect
        write = effect in {"issue", "apply-refresh"}
    elif key not in _READ_SEAMS | _WRITE_SEAMS:
        _refuse("host_operation_unknown")
    context.require_grant(operation)
    if not rootless and not roots:
        _refuse("host_operation_roots_invalid")
    selected_roots = [context.require_path(root, write=write) for root in roots]
    serialized_writer = write and key in _SERIALIZED_WRITERS
    if serialized_writer:
        for root in selected_roots:
            _require_mutation_workspace_gate(root)
    for root in selected_roots:
        context.require_path(root / "research.yml")
        for relative in ("sources", "wiki", "runs", ".locks", "log.md"):
            context.require_path(root / relative, write=write)
    for actor_key in ("agent_id", "verified_by"):
        require_actor(kwargs.get(actor_key))
    for path_key in ("from_file", "coverage_manifest", "grounding_file"):
        supplied = kwargs.get(path_key)
        if supplied is not None:
            if supplied == "-" or not roots:
                _refuse("host_input_path_unavailable")
            path = Path(supplied)
            context.require_path(path if path.is_absolute() else Path(roots[0]) / path)
    with call_binding(), ExitStack() as stack:
        held_roots = [(root, stack.enter_context(_root_descriptor(selected)))
                      for root, selected in zip(roots, selected_roots, strict=True)]

        def revalidate() -> None:
            from ._script_host import owner_generation

            check_generation(*owner_generation())
            for root, descriptor in held_roots:
                selected = context.require_path(root, write=write)
                if serialized_writer:
                    _require_mutation_workspace_gate(selected)
                context.require_path(selected / "research.yml")
                for relative in ("sources", "wiki", "runs", ".locks", "log.md"):
                    context.require_path(selected / relative, write=write)
                current, held = _host_os().stat(selected, follow_symlinks=False), _host_os().fstat(descriptor)
                if (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino):
                    _refuse("host_operation_target_changed")
            for path_key in ("from_file", "coverage_manifest", "grounding_file"):
                supplied = kwargs.get(path_key)
                if supplied is not None:
                    path = Path(supplied)
                    context.require_path(path if path.is_absolute() else Path(roots[0]) / path)

        yield revalidate
        revalidate()
