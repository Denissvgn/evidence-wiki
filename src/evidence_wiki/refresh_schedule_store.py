"""Private host schedule slots share native descriptor custody, not mutation state."""

import hashlib
import os
from contextlib import contextmanager

from ._pack_io import canonical, json_document
from .errors import UsageError
from .host_context import _HISTORICAL_READ
from .mutation_store import MAX_RECORD_BYTES, MutationStore
from .pack_discovery import owner
from .research_session_contracts import refuse


class ScheduleStore:
    """Separate bounded namespace with atomic state CAS and a native dispatch lease."""

    def __init__(self, root, control, schedule_id):
        self.root, self.control, self.schedule_id = root, control, schedule_id
        self.store = None
        self.new = False
        self.state_sha256 = None

    def exists(self):
        store = MutationStore(self.root, self.control)
        store._historical_readonly = True
        token = _HISTORICAL_READ.set(True)
        try:
            with store.workspace_locked():
                try:
                    os.stat("refresh-schedules", dir_fd=store._workspace, follow_symlinks=False)
                except FileNotFoundError:
                    return False
                schedules = store._directory(store._workspace, "refresh-schedules")
                try:
                    os.stat(hashlib.sha256(self.schedule_id.encode()).hexdigest(), dir_fd=schedules, follow_symlinks=False)
                    return True
                except FileNotFoundError:
                    return False
        finally:
            _HISTORICAL_READ.reset(token)

    @contextmanager
    def locked(self, *, write):
        store = MutationStore(self.root, self.control)
        store._historical_readonly = not write
        token = _HISTORICAL_READ.set(True) if not write else None
        try:
            with store.workspace_locked():
                schedules = store._directory(store._workspace, "refresh-schedules")
                name = hashlib.sha256(self.schedule_id.encode()).hexdigest()
                try:
                    os.stat(name, dir_fd=schedules, follow_symlinks=False)
                    self.new = False
                except FileNotFoundError:
                    self.new = True
                    if not write:
                        refuse("SCHEDULE_STATE_UNAVAILABLE")
                directory = store._directory(schedules, name)
                entries = os.listdir(directory)
                if len(entries) > 16 or any(name not in {"state.json", "dispatch.lock"}
                        and not name.startswith(".mutation-") for name in entries):
                    refuse("SCHEDULE_STORE_INVALID")
                store._request, store._active, store._request_id = directory, True, self.schedule_id
                self.store = store
                yield self
                store._verify()
        finally:
            self.store = None
            if token is not None:
                _HISTORICAL_READ.reset(token)

    def read(self):
        raw = self.store._read_raw("state.json", MAX_RECORD_BYTES)
        self.state_sha256 = None if raw is None else "sha256:" + hashlib.sha256(raw).hexdigest()
        return None if raw is None else json_document(raw)

    def write(self, value):
        self.state_sha256 = self.store._publish("state.json", canonical(value), expected=self.state_sha256)

    @contextmanager
    def dispatch_lease(self):
        # Keep a separate descriptor lease through dispatch, releasing the native
        # workspace gate before any research owner or external worker is called.
        with self.locked(write=True):
            descriptor = os.dup(self.store._lock_file(self.store._request, "dispatch.lock"))
        locks = owner("_workspace_locks")
        def revalidate():
            with self.locked(write=False):
                current = os.stat("dispatch.lock", dir_fd=self.store._request, follow_symlinks=False)
                held = os.fstat(descriptor)
                if (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino):
                    refuse("SCHEDULE_DISPATCH_LEASE_CHANGED")
        try:
            with locks.descriptor_lock(descriptor, timeout_seconds=0):
                revalidate()
                yield
                revalidate()
        except locks.LockUnavailableError as error:
            raise UsageError("RESEARCH_SCHEDULE_BUSY", "The host schedule dispatch lease is unavailable.",
                recoverable=error.contended) from None
        finally:
            os.close(descriptor)
