"""Private telemetry outbox metadata and explicitly selected host delivery."""

from __future__ import annotations

import contextlib
import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ._filesystem import os
from ._pack_io import canonical
from .host_context import current_context
from .mutation_store import MutationStore
from .pack_discovery import owner
from .telemetry import _SECRET, _contained, _sha, _time, project, refuse, validate_detached
from .telemetry_contracts import ACK, PROJECTION, RECEIPT, validate_document

OUTBOX = "research-telemetry-outbox/v1"


def current_time():
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class DeadlineSink:
    """A host-selected bounded executor; the core cannot preempt a Python call.

    The host must implement the finite deadline outside an unbounded synchronous
    callable before declaring ``host_deadline_enforced``. Reference strings and
    callbacks do not authenticate measurements or grant delivery authority.
    """

    sink_ref: str
    callback: object = field(repr=False, compare=False)
    deadline_seconds: int
    host_deadline_enforced: bool = False

    def __post_init__(self):
        if (type(self.sink_ref) is not str or not 1 <= len(self.sink_ref) <= 256
                or _SECRET.search(self.sink_ref) or self.sink_ref.startswith(("/", "~", "\\"))
                or not callable(self.callback) or type(self.deadline_seconds) is not int
                or not 1 <= self.deadline_seconds <= 60 or type(self.host_deadline_enforced) is not bool):
            refuse("SINK_INVALID")


def _identifier(value):
    if type(value) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", value) is None:
        refuse("DELIVERY_ID_INVALID")
    return value


def _scope(root, operation):
    context = current_context()
    if context is None or context.mutation_control_directory is None:
        refuse("CONTROL_REQUIRED")
    context.require_grant("telemetry_delivery." + operation)
    root = context.require_path(Path(root).expanduser().absolute())
    control = context.mutation_control_directory
    for path in (context.state_directory, context.authority_file.parent if context.authority_file is not None else None):
        if path is not None and (control.is_relative_to(path) or path.is_relative_to(control)):
            refuse("CONTROL_OVERLAP")
    return root, control


def _selection():
    from .mutation_operations import _host_selection
    return _host_selection()


class _Outbox(MutationStore):
    @contextlib.contextmanager
    def delivery_locked(self, identifier):
        with self.workspace_locked():
            directory = self._directory(self._workspace, "telemetry-deliveries")
            self._request = self._directory(directory, hashlib.sha256(identifier.encode()).hexdigest())
            descriptor = self._lock_file(self._request, "request.lock")
            with owner("_workspace_locks").descriptor_lock(descriptor, timeout_seconds=0):
                self._active, self._request_id = True, identifier
                allowed = {"request.lock", "intent.json", "state.json", "attempt-1.json", "attempt-2.json", "attempt-3.json"}
                entries = os.listdir(self._request)
                if len(entries) > 38 or any(name not in allowed and re.fullmatch(r"\.mutation-[a-f0-9]{32}", name) is None for name in entries):
                    refuse("OUTBOX_UNOWNED")
                for name in entries:
                    self._read_raw(name)
                self._verify()
                yield self
                self._verify()

    def immutable(self, name, value):
        self._publish(name, canonical(value), immutable=True)
        os.fsync(self._request)

    def load_intent(self):
        value = self._read("intent.json")
        if value is None:
            if self._read_raw("state.json") is not None:
                refuse("OUTBOX_INVALID")
            return None
        fields = {"schema_version", "delivery_id", "run_id", "projection", "projection_sha256", "sink_ref", "host_context_sha256", "max_attempts"}
        if (type(value) is not dict or set(value) != fields or value["schema_version"] != OUTBOX
                or value["delivery_id"] != self._request_id or value["host_context_sha256"] != _selection()
                or type(value["max_attempts"]) is not int or not 1 <= value["max_attempts"] <= 3):
            refuse("OUTBOX_CONFLICT")
        projection = validate_document(PROJECTION, value["projection"])
        if value["projection_sha256"] != _sha(projection) or value["run_id"] != projection["run_id"]:
            refuse("OUTBOX_CONFLICT")
        return value

    def checkpoint(self, value):
        value = validate_document(RECEIPT, value)
        previous = self._read_raw("state.json")
        self._publish("state.json", canonical(value), expected="sha256:" + hashlib.sha256(previous).hexdigest() if previous is not None else None)
        return value

    def binding(self, intent, value):
        value = validate_document(RECEIPT, value)
        if any(value[key] != intent[key] for key in ("delivery_id", "run_id", "projection_sha256", "sink_ref", "max_attempts")):
            refuse("OUTBOX_CONFLICT")
        return value

    def observation(self, intent):
        value = self._read("state.json")
        if value is None:
            return None
        value = self.binding(intent, value)
        if value["status"] in {"acknowledged", "failed", "unknown"}:
            retained = self._read(f"attempt-{value['attempts']}.json")
            if retained is None or self.binding(intent, retained) != value:
                refuse("OUTBOX_CONFLICT")
        if value["status"] == "sending":
            retained = self._read(f"attempt-{value['attempts']}.json")
            if retained is not None:
                retained = self.binding(intent, retained)
                if retained["attempts"] != value["attempts"] or retained["deadline_utc"] != value["deadline_utc"] or retained["status"] not in {"acknowledged", "failed", "unknown"}:
                    refuse("OUTBOX_CONFLICT")
                return self.checkpoint(retained)
            if current_time() >= _time(value["deadline_utc"]):
                value = {**value, "status": "unknown", "error_code": "TELEMETRY_DEADLINE_UNKNOWN", "ack_ref": None}
                self.immutable(f"attempt-{value['attempts']}.json", value)
                return self.checkpoint(value)
        return value


def _sink(sink):
    if type(sink) is not DeadlineSink or not sink.host_deadline_enforced:
        refuse("BOUNDED_SINK_REQUIRED")
    return sink


def _base(intent):
    return {"schema_version": RECEIPT, **{key: intent[key] for key in ("delivery_id", "run_id", "projection_sha256", "sink_ref", "max_attempts")},
        "status": "prepared", "attempts": 0, "deadline_utc": None, "ack_ref": None, "error_code": None,
        "historical": True, "authorized": False, "evidence_accepted": False}


def _start(store, intent, sink, value):
    if value["attempts"] >= value["max_attempts"]:
        refuse("DELIVERY_ATTEMPTS_EXHAUSTED")
    deadline = current_time() + timedelta(seconds=sink.deadline_seconds)
    sending = {**value, "status": "sending", "attempts": value["attempts"] + 1,
        "deadline_utc": deadline.isoformat(), "ack_ref": None, "error_code": None}
    store.checkpoint(sending)
    return sending


def _dispatch(root, control, intent, sending, sink):
    request = {"delivery_id": intent["delivery_id"], "run_id": intent["run_id"], "projection_sha256": intent["projection_sha256"],
        "attempt": sending["attempts"], "deadline_utc": sending["deadline_utc"], "projection": intent["projection"]}
    status, ack_ref, error = "unknown", None, "TELEMETRY_CALLBACK_UNKNOWN"
    try:
        ack = validate_document(ACK, sink.callback(validate_detached(request)))
        if ack["delivery_id"] == intent["delivery_id"] and ack["projection_sha256"] == intent["projection_sha256"]:
            status = ack["status"]
            ack_ref = _sha({"ack_ref": ack["ack_ref"]}) if ack["ack_ref"] is not None else None
            error = None if status == "acknowledged" else "TELEMETRY_SINK_FAILED"
    except (Exception, SystemExit):
        status, ack_ref, error = "unknown", None, "TELEMETRY_CALLBACK_UNKNOWN"
    if current_time() >= _time(sending["deadline_utc"]):
        status, ack_ref, error = "unknown", None, "TELEMETRY_DEADLINE_UNKNOWN"
    terminal = {**sending, "status": status, "ack_ref": ack_ref, "error_code": error}
    _scope(root, "deliver")
    with _Outbox(root, control).delivery_locked(intent["delivery_id"]) as store:
        actual = store.load_intent()
        if actual != intent:
            refuse("OUTBOX_CONFLICT")
        current = store.observation(intent)
        if current != sending:
            if current["status"] == "unknown":
                return current
            refuse("OUTBOX_CONFLICT")
        store.immutable(f"attempt-{sending['attempts']}.json", terminal)
        return store.checkpoint(terminal)


@_contained
def deliver(root, *, run_id, delivery_id, projection, sink, max_attempts=3):
    """Checkpoint a declared redacted projection before a bounded host callback."""
    identifier = _identifier(delivery_id)
    root, control = _scope(root, "deliver")
    sink = _sink(sink)
    projection = validate_document(PROJECTION, projection)
    if projection["run_id"] != run_id or project(projection["report"])["report"] != projection["report"]:
        refuse("PROJECTION_UNREDACTED_OR_CONFLICT")
    if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
        refuse("DELIVERY_ATTEMPTS_INVALID")
    intent = {"schema_version": OUTBOX, "delivery_id": identifier, "run_id": run_id, "projection": projection,
        "projection_sha256": _sha(projection), "sink_ref": sink.sink_ref, "host_context_sha256": _selection(), "max_attempts": max_attempts}
    with _Outbox(root, control).delivery_locked(identifier) as store:
        prior = store.load_intent()
        if prior is not None and prior != intent:
            refuse("OUTBOX_CONFLICT")
        store.immutable("intent.json", intent)
        value = store.observation(intent)
        if value is not None and value["status"] != "prepared":
            return value
        value = value if value is not None else store.checkpoint(_base(intent))
        sending = _start(store, intent, sink, value)
    return _dispatch(root, control, intent, sending, sink)


@_contained
def inspect(root, *, delivery_id):
    """Observe pending delivery; only expired leases become unknown."""
    identifier = _identifier(delivery_id)
    root, control = _scope(root, "inspect")
    with _Outbox(root, control).delivery_locked(identifier) as store:
        intent = store.load_intent()
        return store.observation(intent) if intent is not None else None


@_contained
def retry(root, *, delivery_id, sink):
    """Retry a known failed attempt; uncertain or active attempts refuse."""
    identifier = _identifier(delivery_id)
    root, control = _scope(root, "retry")
    context = current_context()
    context.require_grant("telemetry_delivery.deliver")
    sink = _sink(sink)
    with _Outbox(root, control).delivery_locked(identifier) as store:
        intent = store.load_intent()
        if intent is None or intent["sink_ref"] != sink.sink_ref:
            refuse("OUTBOX_CONFLICT")
        value = store.observation(intent)
        if value is None or value["status"] != "failed":
            refuse("DELIVERY_RETRY_UNSAFE")
        sending = _start(store, intent, sink, value)
    return _dispatch(root, control, intent, sending, sink)
