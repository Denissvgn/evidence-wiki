"""Source-owned child births and durable, prompt process-group completion."""

from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import threading
import time

from evidence_wiki._process_groups import ProcessGroupOwnership, ownership_for
from tools._integration_owner_pass import _defer_signals, _group_alive
from tools._release_decision import _original, canonical

REGISTRY_LIMIT = 8_388_608
MAX_CHILDREN = 4096
_POPEN_TYPE = subprocess.Popen


def _require(value, reason):
    if not value:
        raise ValueError(reason)


def _pin(raw):
    return hashlib.sha256(raw).hexdigest()


class ChildTracker:
    """Retain exact Popen objects; numeric IDs are attributes of each birth."""

    def __init__(self, descriptor, binding):
        self.descriptor = descriptor
        self.binding = binding
        self.lock = threading.RLock()
        self.real_popen = subprocess.Popen
        self.births = []
        self.sequence = 0
        self.head = "0" * 64
        self.bytes = 0
        self.closed = False
        self.failed = False
        self.append("open", {"maximum_children": MAX_CHILDREN})

    def append(self, kind, data):
        raw = canonical(self.binding | {"sequence": self.sequence, "previous_sha256": self.head,
                                        "kind": kind, "data": data})
        _require(self.bytes + len(raw) <= REGISTRY_LIMIT, "native_child_registry_bound")
        offset = 0
        while offset < len(raw):
            written = os.write(self.descriptor, raw[offset:])
            _require(written > 0, "native_child_registry_write")
            offset += written
        os.fsync(self.descriptor)
        self.sequence += 1
        self.head = _pin(raw)
        self.bytes += len(raw)
        return self.head

    def popen(self, *args, **kwargs):
        with self.lock, _defer_signals():
            _require(not self.closed and len(self.births) < MAX_CHILDREN, "native_child_launch_bound")
            _require(kwargs.get("start_new_session") is True, "native_child_owned_session")
            self.confirm_completed()
            process = self.real_popen(*args, **kwargs)
            if type(process) is _POPEN_TYPE:
                ProcessGroupOwnership(process, _group_alive)
            birth = {"process": process, "ordinal": len(self.births), "completed": False}
            self.births.append(birth)
            try:
                _require(os.getpgid(process.pid) == process.pid, "native_child_group_identity")
                _require(all(old["process"].pid != process.pid or old["completed"]
                    for old in self.births[:-1]), "native_child_registry_reused_before_completion")
                birth["spawn_sha256"] = self.append("spawn", {
                    "ordinal": birth["ordinal"], "pid": process.pid, "pgid": process.pid})
            except BaseException:
                self.failed = True
                raise
            return process

    def confirm_completed(self, process=None):
        """Certify only waited leaders with an actual, literal absence result.

        Callers invoke this immediately after their own wait/cleanup boundary.
        A certified birth is never polled, waited, probed, or signalled again.
        """
        with self.lock, _defer_signals():
            selected = [birth for birth in self.births if process is None or birth["process"] is process]
            _require(process is None or len(selected) == 1, "native_child_object_not_owned")
            for birth in selected:
                if birth["completed"]:
                    continue
                actual = birth["process"]
                if type(actual.returncode) is not int:
                    continue
                try:
                    guard = ownership_for(actual)
                    absent = guard.confirm_completed() if guard is not None else _group_alive(actual) is False
                except OSError:
                    absent = False
                if not absent:
                    continue
                _require("spawn_sha256" in birth, "native_child_spawn_unrecorded")
                self.append("completion", {"ordinal": birth["ordinal"], "pid": actual.pid,
                    "pgid": actual.pid, "spawn_sha256": birth["spawn_sha256"],
                    "returncode": actual.returncode, "group_absent": True})
                birth["completed"] = True
            return all(birth["completed"] for birth in selected)

    def close(self, cleanup_seconds):
        with self.lock, _defer_signals():
            self.closed = True
            deadline = time.monotonic() + cleanup_seconds
            for birth in self.births:
                if birth["completed"]:
                    continue
                process = birth["process"]
                try:
                    guard = ownership_for(process)
                    if guard is not None:
                        if process.returncode is None:
                            _require(time.monotonic() + 17 <= deadline, "native_child_cleanup_budget")
                        guard.stop()
                    # Poll only a still-owned leader. A reaped leader can never
                    # authorize signalling a later occupant of its numeric ID.
                    elif process.returncode is None and process.poll() is None:
                        _require(time.monotonic() + 17 <= deadline, "native_child_cleanup_budget")
                        _stop(process)
                    _require(self.confirm_completed(process), "native_child_cleanup_unconfirmed")
                except BaseException:
                    self.failed = True
            try:
                self.append("closed", {"spawn_count": len(self.births),
                    "completion_count": sum(birth["completed"] for birth in self.births),
                    "cleanup_confirmed": not self.failed and all(birth["completed"] for birth in self.births)})
            finally:
                os.close(self.descriptor)
            if self.failed:
                raise RuntimeError("native_child_cleanup_unconfirmed")


def _signal(process, selected_signal):
    acquired = process._waitpid_lock.acquire(timeout=1)
    _require(acquired, "native_child_wait_lock_unavailable")
    try:
        if process.returncode is not None:
            return
        try:
            _require(os.getpgid(process.pid) == process.pid, "native_child_group_identity")
            os.killpg(process.pid, selected_signal)
        except ProcessLookupError:
            pass
    finally:
        process._waitpid_lock.release()


def _stop(process):
    _signal(process, signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _signal(process, signal.SIGKILL)
        process.wait(timeout=5)
    _require(type(process.returncode) is int, "native_child_cleanup_unconfirmed")


def verify_registry(raw, binding):
    """Consume certified births without any historical numeric-ID probes."""
    _require(len(raw) <= REGISTRY_LIMIT, "native_child_registry_bound")
    rows = raw.splitlines(keepends=True)
    _require(2 <= len(rows) <= 2 * MAX_CHILDREN + 2, "native_child_registry_rows")
    head, spawned, completed = "0" * 64, {}, set()
    for sequence, original in enumerate(rows):
        row = _original(original)
        _require(canonical(row) == original
            and set(row) == set(binding) | {"sequence", "previous_sha256", "kind", "data"}
            and all(type(row[key]) is type(value) and row[key] == value for key, value in binding.items())
            and type(row["sequence"]) is int and row["sequence"] == sequence
            and row["previous_sha256"] == head, "native_child_registry_binding")
        data = row["data"]
        _require(type(data) is dict, "native_child_registry_data")
        if sequence == 0:
            _require(row["kind"] == "open" and data == {"maximum_children": MAX_CHILDREN},
                     "native_child_registry_open")
        elif sequence == len(rows) - 1:
            _require(row["kind"] == "closed" and data == {"spawn_count": len(spawned),
                "completion_count": len(completed), "cleanup_confirmed": True}
                and type(data.get("spawn_count")) is int and type(data.get("completion_count")) is int
                and data.get("cleanup_confirmed") is True and set(spawned) == completed, "native_child_registry_seal")
        elif row["kind"] == "spawn":
            _require(set(data) == {"ordinal", "pid", "pgid"}
                and type(data["ordinal"]) is int and data["ordinal"] == len(spawned)
                and len(spawned) < MAX_CHILDREN and type(data["pid"]) is int and data["pid"] > 1
                and type(data["pgid"]) is int and data["pgid"] == data["pid"]
                and data["pid"] != binding["worker_pid"], "native_child_registry_spawn")
            # A reused number is acceptable only after the earlier birth closed.
            _require(all(old["pid"] != data["pid"] or ordinal in completed
                for ordinal, old in spawned.items()), "native_child_registry_reused_before_completion")
            spawned[data["ordinal"]] = data | {"spawn_sha256": _pin(original)}
        elif row["kind"] == "completion":
            _require(set(data) == {"ordinal", "pid", "pgid", "spawn_sha256", "returncode", "group_absent"}
                and type(data["ordinal"]) is int and data["ordinal"] in spawned
                and data["ordinal"] not in completed and type(data["returncode"]) is int
                and data["group_absent"] is True, "native_child_registry_completion")
            birth = spawned[data["ordinal"]]
            _require(all(type(data[key]) is type(birth[key]) and data[key] == birth[key]
                for key in ("pid", "pgid", "spawn_sha256")), "native_child_registry_completion_binding")
            completed.add(data["ordinal"])
        else:
            raise ValueError("native_child_registry_kind")
        head = _pin(original)
    return {"raw_sha256": _pin(raw), "head_sha256": head,
            "spawn_count": len(spawned), "cleanup_confirmed": True}
