"""Private mutation checkpoints and receipts, independent of research authority.

The trusted host selects a pre-existing private control directory outside the
workspace. Workspace and request gates use native descriptor locks. No stored
record grants permissions, accepts evidence or owns a research lifecycle.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
import secrets
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NoReturn

from ._filesystem import os
from ._pack_io import bounded, canonical, identity, json_document, read_file, signature
from ._script_host import owner_generation
from .errors import EvidenceWikiError, UsageError
from .pack_discovery import owner

MAX_RECORD_BYTES = 1_048_576
MAX_REQUEST_ID_BYTES = 1024
MAX_STAGE_BLOBS = 4096
MAX_STAGE_BLOB_BYTES = 16 * 1024 * 1024
MAX_STAGE_BYTES = 256 * 1024 * 1024
STAGE_SCHEMA = "research-mutation-stage/v1"
_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_TEMPORARY = re.compile(r"^\.mutation-[a-f0-9]{32}$")
_STAGED = re.compile(r"^\.blob-[a-f0-9]{64}$")
_STAGE_METADATA = re.compile(r"^\.blob-[a-f0-9]{64}\.json$")
_STAGE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class MutationStoreError(UsageError):
    """A bounded content-free store, conflict or contention refusal."""


def _refuse(reason: str, code: str = "MUTATION_STORE_REFUSED") -> NoReturn:
    raise MutationStoreError(code, "Mutation metadata cannot be used for this request.",
        recoverable=code == "MUTATION_LOCK_BUSY", details={"reason": reason},
        remediation="Inspect the recorded request and canonical owner effects before retrying; preserve existing receipts.") from None


def _json(value: Any) -> tuple[dict[str, Any], bytes]:
    try:
        bounded(value)
        raw = canonical(value)
        if len(raw) > MAX_RECORD_BYTES:
            _refuse("record_bound")
        detached = json_document(raw)
        if type(detached) is not dict:
            _refuse("record_invalid")
        return detached, raw
    except MutationStoreError:
        raise
    except (EvidenceWikiError, ValueError, TypeError, UnicodeError, RecursionError):
        _refuse("record_invalid_or_bound")


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _checked(value: dict[str, Any], schema: str) -> dict[str, Any]:
    from .mutation_contracts import validate_document

    try:
        return validate_document(schema, value)
    except (EvidenceWikiError, ValueError, TypeError, KeyError):
        _refuse("stored_contract_invalid")


def _identifier(value: Any) -> str:
    if type(value) is not str or not value or "\0" in value:
        _refuse("request_identifier_invalid")
    try:
        if len(value.encode("utf-8")) > MAX_REQUEST_ID_BYTES:
            _refuse("request_identifier_bound")
    except UnicodeError:
        _refuse("request_identifier_invalid")
    return value


def _root(value: Any) -> Path:
    if not isinstance(value, (str, Path)) or not str(value) or "\0" in str(value):
        _refuse("root_invalid")
    supplied = Path(value).expanduser().absolute()
    try:
        if supplied.is_symlink() or ".." in supplied.parts:
            _refuse("root_link_or_invalid")
        selected = supplied.resolve(strict=True)
        if not stat.S_ISDIR(selected.lstat().st_mode):
            _refuse("root_not_directory")
        return selected
    except (OSError, RuntimeError, ValueError):
        _refuse("root_unavailable")


def _private(info, *, directory: bool = False) -> None:
    try:
        owner("_host_evidence_store").private_entry(info, directory=directory)
    except (ValueError, OSError):
        _refuse("control_entry_not_private")


def _relative(value: Any) -> str:
    if (type(value) is not str or not value or Path(value).is_absolute() or "\\" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
            or "\0" in value):
        _refuse("snapshot_path_invalid")
    return value


def capture_revision(workspace_root: str | Path, *, excluded=(), input_paths=()) -> dict[str, Any]:
    """Capture bounded canonical bytes; exclusions come only from trusted host code.

    Runtime caches use the existing revision owner's exclusions. Additional
    control exclusions are explicit and bound into the resulting revision.
    Requested input bytes are detached private captures, never signing material.
    """
    root = _root(workspace_root)
    if not isinstance(excluded, (tuple, list)) or len(excluded) > 64:
        _refuse("snapshot_exclusions_invalid")
    if not isinstance(input_paths, (tuple, list)) or len(input_paths) > 256:
        _refuse("snapshot_inputs_invalid")
    exclusions = tuple(sorted({_relative(value) for value in excluded}))
    inputs = tuple(sorted({_relative(value) for value in input_paths}))
    revision = owner("_evidence_revision")
    try:
        before = revision.observe_tree(root)
        capture = revision.capture_workspace(root, max_attempts=1)
        after = revision.observe_tree(root)
    except (Exception, SystemExit):
        _refuse("workspace_capture_refused", "MUTATION_REVISION_CONFLICT")
    if before != after:
        _refuse("workspace_changed_during_capture", "MUTATION_REVISION_CONFLICT")
    omitted = lambda name: any(name == value or name.startswith(value + "/") for value in exclusions)
    files = {name: raw for name, raw in capture.files.items() if not omitted(name)}
    if any(name not in files for name in inputs):
        _refuse("input_file_missing_or_excluded", "MUTATION_REVISION_CONFLICT")
    root_info = after["."]
    binding = {"path": str(root), "device": str(root_info[0]), "inode": str(root_info[1])}
    manifest = {"schema_version": "research-workspace-revision/v1", "workspace_binding": binding,
        "files": [{"path": name, "sha256": _digest(raw), "bytes": len(raw), "mode": stat.S_IMODE(after[name][2])}
                  for name, raw in sorted(files.items())],
        "directories": [{"path": name, "mode": stat.S_IMODE(after[name][2])}
                        for name in capture.directories if not omitted(name)],
        "excluded_control_paths": list(exclusions), "runtime_exclusions": sorted(revision.EXCLUDED_ROOTS)}
    return {"revision_id": _digest(canonical(manifest)), "workspace_binding": binding,
            "input_files": [{"path": name, "sha256": _digest(files[name]), "bytes": len(files[name])} for name in inputs],
            "input_bytes": {name: files[name] for name in inputs}}


def capture_owner_generation() -> dict[str, str]:
    """Bind trusted script and package source bytes, including namespace helpers."""
    script_root, script_hash = owner_generation()
    package = Path(__file__).resolve().parent
    revision = owner("_evidence_revision")
    try:
        observed = revision.observe_tree(package)
    except (Exception, SystemExit):
        _refuse("owner_generation_unavailable")
    paths = sorted(name for name, info in observed.items()
                   if name.endswith(".py") and stat.S_ISREG(info[2]))
    if len(paths) > 512:
        _refuse("owner_generation_bound")
    rows, total = [], 0
    for relative in paths:
        try:
            if observed[relative][4] > 2_097_152:
                _refuse("owner_generation_bound")
            raw = revision.read_observed_file(package, relative, observed[relative])
        except MutationStoreError:
            raise
        except (Exception, SystemExit):
            _refuse("owner_generation_unavailable")
        total += len(raw)
        if total > 32 * 1024 * 1024:
            _refuse("owner_generation_bound")
        rows.append({"path": relative, "sha256": _digest(raw)})
    try:
        unchanged = revision.observe_tree(package) == observed
    except (Exception, SystemExit):
        _refuse("owner_generation_changed", "MUTATION_REVISION_CONFLICT")
    if not unchanged or owner_generation() != (script_root, script_hash):
        _refuse("owner_generation_changed", "MUTATION_REVISION_CONFLICT")
    return {"script_root": str(script_root), "script_sha256": "sha256:" + script_hash,
            "package_sha256": _digest(canonical(rows))}


class MutationStore:
    """Native workspace gate followed by a native request gate in private custody."""

    def __init__(self, workspace_root: str | Path, control_root: str | Path, intent: dict[str, Any] | None = None):
        self.workspace_root, self.control_root = _root(workspace_root), _root(control_root)
        if (self.control_root.is_relative_to(self.workspace_root)
                or self.workspace_root.is_relative_to(self.control_root)):
            _refuse("control_root_overlaps_workspace")
        self.workspace_binding = {"path": str(self.workspace_root), **identity(self.workspace_root)}
        self.control_identity = identity(self.control_root)
        self.intent = _json(intent)[0] if intent is not None else None
        self._fds, self._links = [], []
        self._request = None
        self._workspace = None
        self._request_id = None
        self._active = False
        self._historical_readonly = False
        self._stage_sizes = {}
        self._stage_slots = set()
        self._stage_temporary_bytes = 0

    def _verify(self) -> None:
        if identity(self.control_root) != self.control_identity or self.control_root.is_symlink():
            _refuse("control_root_changed")
        _private(os.stat(self.control_root, follow_symlinks=False), directory=True)
        if (self.workspace_root.is_symlink() or self.workspace_root.resolve() != self.workspace_root
                or {"path": str(self.workspace_root), **identity(self.workspace_root)} != self.workspace_binding):
            _refuse("workspace_root_changed", "MUTATION_REVISION_CONFLICT")
        for parent, name, descriptor, private in self._links:
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            held = os.fstat(descriptor)
            if (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino):
                _refuse("control_namespace_changed")
            if private:
                _private(current, directory=stat.S_ISDIR(held.st_mode))

    def _directory(self, parent: int, name: str) -> int:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
            os.fsync(parent)
        except FileExistsError:
            pass
        descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        self._fds.append(descriptor)
        _private(os.fstat(descriptor), directory=True)
        self._links.append((parent, name, descriptor, True))
        return descriptor

    def _lock_file(self, directory: int, name: str) -> int:
        try:
            descriptor = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            os.fsync(directory)
        except FileExistsError:
            descriptor = os.open(name, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        self._fds.append(descriptor)
        info = os.fstat(descriptor)
        _private(info)
        if info.st_size:
            _refuse("lock_contains_unowned_data")
        self._links.append((directory, name, descriptor, True))
        return descriptor

    def _open_control_root(self) -> int:
        descriptor = os.open(self.control_root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self._fds.append(descriptor)
        for part in self.control_root.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            self._fds.append(child)
            self._links.append((descriptor, part, child, False))
            descriptor = child
        _private(os.fstat(descriptor), directory=True)
        if identity(descriptor) != self.control_identity:
            _refuse("control_root_changed")
        return descriptor

    @contextlib.contextmanager
    def workspace_locked(self, *, borrow: bool = False) -> Iterator[MutationStore]:
        """Hold the workspace gate without creating a fake request identity."""
        if self._workspace is not None:
            _refuse("store_lock_nested")
        if type(borrow) is not bool:
            _refuse("historical_read_scope_required")
        if borrow:
            from .host_context import _require_borrowed_mutation_workspace_gate
            if not self._historical_readonly:
                _refuse("historical_read_scope_required")
            _require_borrowed_mutation_workspace_gate(self.workspace_root, self.control_root)
        if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd or os.link not in os.supports_dir_fd:
            _refuse("native_storage_unavailable", "MUTATION_PLATFORM_UNAVAILABLE")
        locks = owner("_workspace_locks")
        yielded_error = False
        try:
            root = self._open_control_root()
            namespace = self._directory(root, "mutations")
            workspaces = self._directory(namespace, "workspaces")
            workspace = self._directory(workspaces, hashlib.sha256(str(self.workspace_root).encode()).hexdigest())
            gate = self._lock_file(workspace, "workspace.lock")
            with contextlib.ExitStack() as custody:
                from .host_context import _mutation_workspace_gate, _require_borrowed_mutation_workspace_gate
                if borrow:
                    if not self._historical_readonly:
                        _refuse("historical_read_scope_required")
                    _require_borrowed_mutation_workspace_gate(self.workspace_root, self.control_root)
                else:
                    custody.enter_context(locks.descriptor_lock(gate, timeout_seconds=0))
                self._workspace = workspace
                self._verify()
                if not borrow:
                    custody.enter_context(_mutation_workspace_gate(self.workspace_root, self.control_root,
                        workspace_binding=self.workspace_binding, control_identity=self.control_identity,
                        gate_descriptor=gate, read_only=self._historical_readonly))
                try:
                    yield self
                except BaseException:
                    yielded_error = True
                    raise
                self._verify()
                if borrow:
                    _require_borrowed_mutation_workspace_gate(self.workspace_root, self.control_root)
        except locks.LockUnavailableError as exc:
            if yielded_error:
                raise
            _refuse("workspace_or_request_gate_busy" if exc.contended else "native_lock_unavailable",
                    "MUTATION_LOCK_BUSY" if exc.contended else "MUTATION_PLATFORM_UNAVAILABLE")
        except OSError:
            if yielded_error:
                raise
            _refuse("control_io_unavailable")
        finally:
            self._active, self._request, self._request_id, self._workspace = False, None, None, None
            for descriptor in reversed(self._fds):
                os.close(descriptor)
            self._fds, self._links = [], []
            self._stage_sizes = {}
            self._stage_slots = set()
            self._stage_temporary_bytes = 0

    @contextlib.contextmanager
    def locked(self, request_id: str | None = None, *, borrow_workspace: bool = False) -> Iterator[MutationStore]:
        identifier = request_id if request_id is not None else self.intent.get("request_id") if self.intent else None
        identifier = _identifier(identifier)
        locks = owner("_workspace_locks")
        with self.workspace_locked(borrow=borrow_workspace):
            yielded_error = False
            try:
                requests = self._directory(self._workspace, "requests")
                self._request = self._directory(requests, hashlib.sha256(identifier.encode()).hexdigest())
                request_lock = self._lock_file(self._request, "request.lock")
                with locks.descriptor_lock(request_lock, timeout_seconds=0):
                    self._active, self._request_id = True, identifier
                    self._verify()
                    allowed = {"request.lock", "intent.json", "state.json", "receipt.json"}
                    entries = os.listdir(self._request)
                    if len(entries) > 2 * MAX_STAGE_BLOBS + 32 or any(name not in allowed and not _TEMPORARY.fullmatch(name)
                            and not _STAGED.fullmatch(name) and not _STAGE_METADATA.fullmatch(name) for name in entries):
                        _refuse("request_directory_contains_unowned_data")
                    if sum(bool(_TEMPORARY.fullmatch(name)) for name in entries) > 32:
                        _refuse("stage_temporary_bound")
                    for name in entries:
                        if _STAGED.fullmatch(name) or _STAGE_METADATA.fullmatch(name) or _TEMPORARY.fullmatch(name):
                            info = os.stat(name, dir_fd=self._request, follow_symlinks=False)
                            _private(info)
                            if info.st_size > (MAX_RECORD_BYTES if _STAGE_METADATA.fullmatch(name) else MAX_STAGE_BLOB_BYTES):
                                _refuse("stage_blob_bound")
                            if _TEMPORARY.fullmatch(name):
                                self._stage_temporary_bytes += info.st_size
                            else:
                                self._stage_sizes[name] = info.st_size
                                self._stage_slots.add(name.removesuffix(".json"))
                    if (len(self._stage_slots) > MAX_STAGE_BLOBS
                            or sum(self._stage_sizes.values()) + self._stage_temporary_bytes > MAX_STAGE_BYTES):
                        _refuse("stage_aggregate_bound")
                    try:
                        yield self
                    except BaseException:
                        yielded_error = True
                        raise
                    self._verify()
            except locks.LockUnavailableError as exc:
                if yielded_error:
                    raise
                _refuse("workspace_or_request_gate_busy" if exc.contended else "native_lock_unavailable",
                        "MUTATION_LOCK_BUSY" if exc.contended else "MUTATION_PLATFORM_UNAVAILABLE")
            except OSError:
                if yielded_error:
                    raise
                _refuse("control_io_unavailable")

    @contextlib.contextmanager
    def historical_locked(self, request_id: str, *, borrow_workspace: bool = False) -> Iterator[MutationStore]:
        """Read protected history without nesting or enlarging the held workspace gate."""
        if self._active or self._workspace is not None:
            _refuse("store_lock_nested")
        self._historical_readonly = True
        try:
            with self.locked(request_id, borrow_workspace=borrow_workspace):
                yield self
        finally:
            self._historical_readonly = False

    def _require_locked(self) -> None:
        if not self._active or self._request is None:
            _refuse("store_gate_required")
        self._verify()

    def _read_raw(self, name: str, maximum: int = MAX_RECORD_BYTES) -> bytes | None:
        self._require_locked()
        try:
            info = os.stat(name, dir_fd=self._request, follow_symlinks=False)
        except FileNotFoundError:
            return None
        _private(info)
        try:
            raw = read_file(self._request, name, maximum, signature(info))
        except EvidenceWikiError:
            _refuse("record_unreadable")
        after = os.stat(name, dir_fd=self._request, follow_symlinks=False)
        if signature(info) != signature(after):
            _refuse("record_changed_during_read")
        return raw

    def _read(self, name: str) -> dict[str, Any] | None:
        raw = self._read_raw(name)
        if raw is None:
            return None
        try:
            return _json(json_document(raw))[0]
        except EvidenceWikiError:
            _refuse("stored_record_invalid")

    def _publish(self, name: str, raw: bytes, *, expected: str | None = None, immutable: bool = False,
                 maximum: int = MAX_RECORD_BYTES) -> str:
        self._require_locked()
        if self._historical_readonly:
            _refuse("historical_read_only")
        if len(raw) > maximum:
            _refuse("record_bound")
        previous = self._read_raw(name, maximum)
        if immutable and previous is not None:
            if previous != raw:
                _refuse("immutable_record_conflict", "MUTATION_REQUEST_CONFLICT")
            return _digest(previous)
        if not immutable and (expected != (_digest(previous) if previous is not None else None)):
            _refuse("state_compare_and_swap_conflict", "MUTATION_STATE_CONFLICT")
        temporary = ".mutation-" + secrets.token_hex(16)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self._request)
        published = False
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            self._verify()
            if immutable:
                os.link(temporary, name, src_dir_fd=self._request, dst_dir_fd=self._request, follow_symlinks=False)
            else:
                latest = self._read_raw(name)
                if latest != previous:
                    _refuse("state_compare_and_swap_conflict", "MUTATION_STATE_CONFLICT")
                os.replace(temporary, name, src_dir_fd=self._request, dst_dir_fd=self._request)
            published = True
            if immutable:
                os.unlink(temporary, dir_fd=self._request)
            os.fsync(self._request)
            return _digest(raw)
        except OSError:
            _refuse("record_publication_uncertain" if published else "record_publication_failed",
                    "MUTATION_COMMIT_UNKNOWN" if published else "MUTATION_STORE_REFUSED")
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=self._request)

    def prepare(self, intent: dict[str, Any]) -> dict[str, Any]:
        value, raw = _json(_checked(intent, "research-mutation-intent/v1"))
        if value.get("schema_version") != "research-mutation-intent/v1" or value.get("request_id") != self._request_id:
            _refuse("intent_binding_invalid")
        if value.get("workspace_binding") != self.workspace_binding:
            _refuse("intent_workspace_changed", "MUTATION_REQUEST_CONFLICT")
        if not _DIGEST.fullmatch(value.get("input_sha256", "")):
            _refuse("intent_digest_invalid")
        self._publish("intent.json", raw, immutable=True)
        return value

    def read(self) -> dict[str, Any] | None:
        value = self._read("state.json")
        if value is not None:
            value = _checked(value, "research-mutation-state/v1")
            self._binding(value, "research-mutation-state/v1")
        return value

    def state_sha256(self) -> str | None:
        raw = self._read_raw("state.json")
        return _digest(raw) if raw is not None else None

    def _binding(self, value: dict[str, Any], schema: str) -> None:
        intent = self._read("intent.json")
        if intent is not None:
            intent = _checked(intent, "research-mutation-intent/v1")
            if intent.get("workspace_binding") != self.workspace_binding:
                _refuse("stored_workspace_binding_changed", "MUTATION_REQUEST_CONFLICT")
        if (intent is None or value.get("schema_version") != schema or value.get("request_id") != self._request_id
                or value.get("input_sha256") != intent.get("input_sha256")
                or value.get("before_revision") != intent.get("expected_revision")
                or schema == "research-mutation-receipt/v1" and value.get("operation") != intent.get("operation")):
            _refuse("checkpoint_or_receipt_binding_invalid")

    def write(self, state: dict[str, Any], *, expected_sha256: str | None = None) -> str:
        value, raw = _json(_checked(state, "research-mutation-state/v1"))
        self._binding(value, "research-mutation-state/v1")
        return self._publish("state.json", raw, expected=expected_sha256)

    def receipt(self) -> dict[str, Any] | None:
        value = self._read("receipt.json")
        if value is not None:
            value = _checked(value, "research-mutation-receipt/v1")
            self._binding(value, "research-mutation-receipt/v1")
        return value

    def commit_receipt(self, receipt: dict[str, Any]) -> str:
        value, raw = _json(_checked(receipt, "research-mutation-receipt/v1"))
        self._binding(value, "research-mutation-receipt/v1")
        return self._publish("receipt.json", raw, immutable=True)

    def inspect(self, request_id: str) -> dict[str, Any] | None:
        with self.locked(request_id):
            intent, state, receipt = self._read("intent.json"), self.read(), self.receipt()
            if intent is not None:
                intent = _checked(intent, "research-mutation-intent/v1")
                if intent.get("request_id") != self._request_id or intent.get("workspace_binding") != self.workspace_binding:
                    _refuse("stored_intent_binding_changed", "MUTATION_REQUEST_CONFLICT")
            if intent is None and (state is not None or receipt is not None):
                _refuse("orphaned_checkpoint_or_receipt")
            return {"intent": intent, "state": state, "receipt": receipt} if intent is not None else None

    def _stage_descriptor(self, value: Any) -> dict[str, Any]:
        fields = {"schema_version", "request_id", "input_sha256", "name", "artifact_id", "sha256", "bytes"}
        if (type(value) is not dict or set(value) != fields or value["schema_version"] != STAGE_SCHEMA
                or type(value["name"]) is not str or _STAGE_NAME.fullmatch(value["name"]) is None
                or type(value["bytes"]) is not int or not 0 <= value["bytes"] <= MAX_STAGE_BLOB_BYTES
                or any(type(value[key]) is not str or _DIGEST.fullmatch(value[key]) is None
                       for key in ("artifact_id", "sha256", "input_sha256"))):
            _refuse("stage_descriptor_invalid")
        self._require_locked()
        intent = self._read("intent.json")
        if intent is None:
            _refuse("stage_intent_required")
        intent = _checked(intent, "research-mutation-intent/v1")
        if (value["request_id"] != self._request_id or intent["request_id"] != self._request_id
                or value["input_sha256"] != intent["input_sha256"] or intent["workspace_binding"] != self.workspace_binding):
            _refuse("stage_intent_binding_invalid")
        basis = {key: value[key] for key in ("schema_version", "request_id", "input_sha256", "name")}
        if _digest(canonical(basis)) != value["artifact_id"]:
            _refuse("stage_descriptor_digest_invalid")
        return dict(value)

    def stage_blob(self, name: str, raw: bytes) -> dict[str, Any]:
        """Durably retain immutable execution bytes, never authority or evidence state."""
        self._require_locked()
        if self._historical_readonly:
            _refuse("historical_read_only")
        if (type(raw) is not bytes or len(raw) > MAX_STAGE_BLOB_BYTES
                or type(name) is not str or _STAGE_NAME.fullmatch(name) is None):
            _refuse("stage_blob_bound_or_invalid")
        intent = self._read("intent.json")
        if intent is None:
            _refuse("stage_intent_required")
        intent = _checked(intent, "research-mutation-intent/v1")
        value = {"schema_version": STAGE_SCHEMA, "request_id": self._request_id, "input_sha256": intent["input_sha256"],
            "name": name, "sha256": _digest(raw), "bytes": len(raw)}
        value["artifact_id"] = _digest(canonical({key: value[key] for key in
            ("schema_version", "request_id", "input_sha256", "name")}))
        value = self._stage_descriptor(value)
        leaf = ".blob-" + value["artifact_id"][7:]
        metadata = canonical(value)
        metadata_leaf = leaf + ".json"
        addition = (len(raw) if leaf not in self._stage_sizes else 0) + (len(metadata) if metadata_leaf not in self._stage_sizes else 0)
        if (leaf not in self._stage_slots and len(self._stage_slots) >= MAX_STAGE_BLOBS
                or sum(self._stage_sizes.values()) + self._stage_temporary_bytes + addition > MAX_STAGE_BYTES):
            _refuse("stage_aggregate_bound")
        try:
            self._publish(metadata_leaf, metadata, immutable=True)
            self._publish(leaf, raw, immutable=True, maximum=MAX_STAGE_BLOB_BYTES)
        finally:
            for name in (metadata_leaf, leaf):
                with contextlib.suppress(FileNotFoundError):
                    info = os.stat(name, dir_fd=self._request, follow_symlinks=False)
                    self._stage_sizes[name] = info.st_size
                    self._stage_slots.add(leaf)
        try:
            os.fsync(self._request)
        except OSError:
            _refuse("stage_durability_unknown", "MUTATION_COMMIT_UNKNOWN")
        return value

    def read_stage(self, descriptor: dict[str, Any]) -> bytes:
        """Read one intent-bound private artifact with exact custody and hash checks."""
        value = self._stage_descriptor(descriptor)
        leaf = ".blob-" + value["artifact_id"][7:]
        retained = self._read(leaf + ".json")
        if retained is None or self._stage_descriptor(retained) != value:
            _refuse("stage_descriptor_missing_or_changed")
        raw = self._read_raw(leaf, MAX_STAGE_BLOB_BYTES)
        if raw is None or len(raw) != value["bytes"] or _digest(raw) != value["sha256"]:
            _refuse("stage_blob_missing_or_changed")
        return raw

    def staged(self, name: str) -> dict[str, Any] | None:
        """Recover an exact durable named slot; incomplete publication never replans."""
        self._require_locked()
        if type(name) is not str or _STAGE_NAME.fullmatch(name) is None:
            _refuse("stage_name_invalid")
        intent = self._read("intent.json")
        if intent is None:
            _refuse("stage_intent_required")
        intent = _checked(intent, "research-mutation-intent/v1")
        slot = {"schema_version": STAGE_SCHEMA, "request_id": self._request_id,
                "input_sha256": intent["input_sha256"], "name": name}
        leaf = ".blob-" + _digest(canonical(slot))[7:]
        retained = self._read(leaf + ".json")
        if retained is None:
            if self._read_raw(leaf, MAX_STAGE_BLOB_BYTES) is not None:
                _refuse("stage_slot_incomplete")
            return None
        value = self._stage_descriptor(retained)
        if value["name"] != name or ".blob-" + value["artifact_id"][7:] != leaf:
            _refuse("stage_slot_binding_invalid")
        self.read_stage(value)
        try:
            os.fsync(self._request)
        except OSError:
            _refuse("stage_durability_unknown", "MUTATION_COMMIT_UNKNOWN")
        return value
