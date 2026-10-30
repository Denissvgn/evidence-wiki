"""Opt-in process-group ownership tied to one exact, retained Popen birth."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time

_POPEN_TYPE = subprocess.Popen
_MARKER = "_evidence_wiki_process_group_ownership"
_MISSING = object()


class ProcessGroupOwnershipError(RuntimeError):
    """The original process group's completion or ownership is unknown."""


def ownership_for(process):
    """Return an installed guard, refusing malformed or transplanted markers."""
    guard = getattr(process, _MARKER, _MISSING)
    if guard is _MISSING:
        return None
    if type(guard) is not ProcessGroupOwnership or guard.process is not process:
        raise ProcessGroupOwnershipError("process_group_ownership_unknown")
    guard._check_identity()
    return guard


class ProcessGroupOwnership:
    """Keep the leader unreaped while signalling; cache first proven absence."""

    def __init__(self, process, group_alive):
        if (type(process) is not _POPEN_TYPE or type(process.pid) is not int or process.pid <= 1
                or getattr(process, _MARKER, _MISSING) is not _MISSING):
            raise ProcessGroupOwnershipError("process_group_ownership_unknown")
        self.process = process
        self.pid = process.pid
        self._group_alive = group_alive
        self._lock = threading.RLock()
        self._absent = False
        setattr(process, _MARKER, self)

    def _check_identity(self):
        if (getattr(self.process, _MARKER, None) is not self
                or type(self.process.pid) is not int or self.process.pid != self.pid):
            raise ProcessGroupOwnershipError("process_group_ownership_unknown")

    def confirm_completed(self):
        """Never reap; only literal absence after an observed wait is proof."""
        with self._lock:
            self._check_identity()
            if self._absent:
                return True
            if type(self.process.returncode) is not int:
                return False
            try:
                absent = self._group_alive(self.process) is False
            except Exception:
                absent = False
            if absent:
                self._absent = True
            return absent

    def signal_if_owned(self, selected_signal):
        """Signal only while Popen's wait lock still pins the original PID."""
        with self._lock:
            self._check_identity()
            if self._absent:
                return
            process = self.process
            acquired = process._waitpid_lock.acquire(timeout=1)
            if not acquired:
                raise ProcessGroupOwnershipError("process_group_ownership_unknown")
            try:
                self._check_identity()
                if process.returncode is not None:
                    if not self.confirm_completed():
                        raise ProcessGroupOwnershipError("process_group_ownership_unknown")
                    return
                try:
                    if os.getpgid(self.pid) != self.pid:
                        raise ProcessGroupOwnershipError("process_group_ownership_unknown")
                    os.killpg(self.pid, selected_signal)
                except ProcessLookupError:
                    # Only a later waited leader plus actual group absence can
                    # establish completion, even when a live signal finds none.
                    pass
                except OSError as error:
                    raise ProcessGroupOwnershipError("process_group_ownership_unknown") from error
            finally:
                process._waitpid_lock.release()

    def stop(self):
        """Bound cleanup without escalating after another waiter reaps."""
        with self._lock:
            self._check_identity()
            if self._absent:
                return
        process = self.process
        if process.returncode is None:
            self.signal_if_owned(signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.signal_if_owned(signal.SIGKILL)
                process.wait(timeout=5)
        deadline = time.monotonic() + 5
        while not self.confirm_completed():
            if time.monotonic() >= deadline:
                raise ProcessGroupOwnershipError("process_group_ownership_unknown")
            time.sleep(0.01)
