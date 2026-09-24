"""Private setup journals and bounded ownership snapshots under a target lock."""

from __future__ import annotations

import contextlib
import hashlib
import stat
import unicodedata
import uuid
from pathlib import Path

from ._filesystem import metadata_fstat, metadata_stat, os
from ._pack_io import canonical, identity, json_document, read_file, signature
from .errors import EvidenceWikiError
from .pack_discovery import owner
from .planning_contracts import MAX_BYTES, refuse
from .planning_inputs import target_basis
from .setup_contracts import CHECKPOINT


def target_key(selection):
    value = "\0".join(unicodedata.normalize("NFC", selection[key]).casefold()
                      for key in ("writable_root", "relative_path"))
    return hashlib.sha256(value.encode()).hexdigest()


def snapshot(target, *, durable=False):
    """Include every entry; extra files, inode changes and permission edits conflict."""
    if not target.exists() and not target.is_symlink():
        return None
    descriptors, files, directories = [], [], []
    total = 0
    try:
        root = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(root)
        pending = [(root, "")]
        while pending:
            fd, relative = pending.pop()
            info = metadata_fstat(fd)
            directories.append({"path": relative, "identity": identity(fd), "mode": stat.S_IMODE(info.st_mode)})
            for name in sorted(os.listdir(fd)):
                if len(files) >= 1024 or len(directories) + len(pending) >= 1024:
                    refuse("setup_tree_entry_bound", "ONBOARDING_LIMIT")
                path = relative + "/" + name if relative else name
                before = metadata_stat(name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISDIR(before.st_mode):
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    descriptors.append(child)
                    pending.append((child, path))
                elif stat.S_ISREG(before.st_mode) and before.st_nlink == 1:
                    raw = read_file(fd, name, 16_777_216)
                    if signature(before) != signature(metadata_stat(name, dir_fd=fd, follow_symlinks=False)):
                        refuse("setup_file_changed_during_observation", "ONBOARDING_OWNERSHIP_CONFLICT")
                    if durable:
                        file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
                        try:
                            os.fsync(file_fd)
                        finally:
                            os.close(file_fd)
                    total += len(raw)
                    if total > 100_663_296:
                        refuse("setup_tree_bytes_bound", "ONBOARDING_LIMIT")
                    files.append({"path": path, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
                                  "mode": stat.S_IMODE(before.st_mode)})
                else:
                    refuse("setup_unowned_special_entry", "ONBOARDING_OWNERSHIP_CONFLICT")
            if durable:
                os.fsync(fd)
        result = {"files": sorted(files, key=lambda row: row["path"]),
                  "directories": sorted(directories, key=lambda row: row["path"])}
        if len(canonical(result)) > 850_000:
            refuse("setup_snapshot_bound", "ONBOARDING_LIMIT")
        # Anchored reads must still belong to the same named directory tree.
        for row in directories:
            path = target / row["path"]
            if path.is_symlink() or identity(path) != row["identity"]:
                refuse("setup_directory_replaced", "ONBOARDING_OWNERSHIP_CONFLICT")
        return result
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


class SetupStore:
    """No lock stealing or single-writer bypass; artifacts never enter the workspace."""

    def __init__(self, plan):
        self.plan = plan
        self.basis = plan["bindings"]["target"]
        self.target, _ = target_basis(self.basis["target"], owned_basis=self.basis)
        self.root = Path(self.basis["target"]["writable_root"])
        self.path = self.root / ".evidence-wiki/setup/targets" / target_key(self.basis["target"])
        self.fds, self.links = [], []
        self.transaction = None

    def directory(self, parent, name):
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
            os.fsync(parent)
        except FileExistsError:
            pass
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        self.fds.append(fd)
        owner("_host_evidence_store").private_entry(os.fstat(fd), directory=True)
        self.links.append((parent, name, fd))
        return fd

    def verify(self):
        target_basis(self.basis["target"], owned_basis=self.basis)
        for parent, name, fd in self.links:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            held = os.fstat(fd)
            if (info.st_dev, info.st_ino) != (held.st_dev, held.st_ino):
                refuse("setup_state_replaced", "ONBOARDING_OWNERSHIP_CONFLICT")
            owner("_host_evidence_store").private_entry(info, directory=stat.S_ISDIR(held.st_mode))

    @contextlib.contextmanager
    def locked(self):
        if (not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd
                or not {"fcntl", "win32"}.intersection(owner("_workspace_locks").available_lock_backends())):
            refuse("setup_platform_unsupported", "ONBOARDING_ENVIRONMENT_INCOMPATIBLE")
        try:
            fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            self.fds.append(fd)
            if identity(fd) != self.basis["root_identity"]:
                refuse("setup_root_changed", "ONBOARDING_OWNERSHIP_CONFLICT")
            for name in (".evidence-wiki", "setup", "targets", self.path.name):
                fd = self.directory(fd, name)
            try:
                lock = os.open("setup.lock", os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=fd)
                os.fsync(fd)
            except FileExistsError:
                lock = os.open("setup.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            self.fds.append(lock)
            owner("_host_evidence_store").private_entry(os.fstat(lock))
            if os.fstat(lock).st_size:
                refuse("setup_lock_unowned_content", "ONBOARDING_OWNERSHIP_CONFLICT")
            self.links.append((fd, "setup.lock", lock))
            try:
                with owner("_workspace_locks").descriptor_lock(lock, timeout_seconds=0):
                    self.verify()
                    self.transactions = self.directory(fd, "transactions")
                    yield self
                    self.verify()
            except owner("_workspace_locks").LockUnavailableError as error:
                refuse("setup_lock_busy" if error.contended else "setup_lock_unavailable",
                       "ONBOARDING_LOCK_BUSY" if error.contended else "ONBOARDING_ENVIRONMENT_INCOMPATIBLE")
        finally:
            for fd in reversed(self.fds):
                os.close(fd)

    def read(self, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.transaction)
        try:
            owner("_host_evidence_store").private_entry(os.fstat(fd))
        finally:
            os.close(fd)
        return json_document(read_file(self.transaction, name, MAX_BYTES))

    def write(self, name, value, *, exclusive=False):
        self.verify()
        raw = canonical(value) + b"\n"
        if len(raw) > MAX_BYTES or name.startswith("observations/") and len(raw) > 65536:
            refuse("setup_artifact_bound", "ONBOARDING_LIMIT")
        parent = self.transaction
        if name.startswith("observations/"):
            parent, name = self.observations, name.split("/")[1]
        temporary = ".setup-" + uuid.uuid4().hex
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            self.verify()
            if exclusive:
                os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
            else:
                if name in os.listdir(parent):
                    owner("_host_evidence_store").private_entry(os.stat(name, dir_fd=parent, follow_symlinks=False))
                os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=parent)

    def open_transaction(self):
        entries = os.listdir(self.transactions)
        if len(entries) > 1:
            refuse("setup_transactions_ambiguous", "ONBOARDING_OWNERSHIP_CONFLICT")
        if entries:
            name = entries[0]
            if len(name) != 32 or any(c not in "0123456789abcdef" for c in name):
                refuse("setup_transaction_unowned", "ONBOARDING_OWNERSHIP_CONFLICT")
            self.transaction = self.directory(self.transactions, name)
            try:
                if self.read("plan.json") != self.plan or self.read("request.json") != self.plan["request"]:
                    refuse("setup_transaction_plan_changed", "ONBOARDING_PLAN_STALE")
                checkpoint = self.read("checkpoint.json")
                if (checkpoint["schema_version"] != CHECKPOINT or checkpoint["plan_id"] != self.plan["plan_id"]
                        or checkpoint["transaction_id"] != name):
                    refuse("setup_checkpoint_binding_changed", "ONBOARDING_OWNERSHIP_CONFLICT")
            except EvidenceWikiError as error:
                if error.error_code == "ONBOARDING_INVALID":
                    refuse("setup_checkpoint_missing_or_corrupt", "ONBOARDING_OWNERSHIP_CONFLICT")
                raise
            except (OSError, KeyError, ValueError, TypeError):
                refuse("setup_checkpoint_missing_or_corrupt", "ONBOARDING_OWNERSHIP_CONFLICT")
        else:
            # Reserve the journal before the first target effect.
            if target_basis(self.basis["target"])[1] != self.basis:
                refuse("setup_target_changed", "ONBOARDING_TARGET_CONFLICT")
            name = uuid.uuid4().hex
            self.transaction = self.directory(self.transactions, name)
            self.write("request.json", self.plan["request"], exclusive=True)
            self.write("plan.json", self.plan, exclusive=True)
            checkpoint = {"schema_version": CHECKPOINT, "plan_id": self.plan["plan_id"], "transaction_id": name,
                          "state": "prepared", "pending": None, "completed": [], "snapshot": snapshot(self.target),
                          "observations": {}, "results": {}, "clock": None}
            self.write("checkpoint.json", checkpoint, exclusive=True)
        self.transaction_id = name
        self.transaction_path = self.path / "transactions" / name
        self.observations = self.directory(self.transaction, "observations")
        return checkpoint
