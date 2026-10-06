"""Bounded live inode custody supports reconciliation without adopting copies."""

from __future__ import annotations

import hashlib
import stat
import threading

from ._filesystem import os
from .artifact_delivery_contracts import MAX_BYTES, refuse
from .artifact_delivery_input import _ACTIVE
from .mutation_contracts import canonical
from .mutation_store import MutationStore

_LOCK = threading.Lock()
_LIVE = {}
MAX_LIVE_WRITERS = 16


def _identity(info):
    return {"device": str(info.st_dev), "inode": str(info.st_ino)}


class _PublicationWitness:
    """Only the actual held native input and executing staged intent may link."""

    def __init__(self, session, observed, store, path, raw):
        from .resolution_operations import _OwnedArtifactInput
        if (type(observed) is not _OwnedArtifactInput or type(store) is not MutationStore
                or not store._active or store._historical_readonly or store.workspace_root != session.root
                or session.context.mutation_control_directory is None
                or store.control_root != session.context.mutation_control_directory.resolve(strict=True)
                or store.intent is None or store.intent["operation"] != "artifact.deliver"
                or store.read()["status"] != "executing" or type(raw) is not bytes or len(raw) > MAX_BYTES):
            refuse("publication_owned_intent_required")
        admission = _ACTIVE.get()
        if admission is None:
            refuse("publication_native_scope_required")
        admission.held(session)
        observed.require(session)
        plan = session.execution.profile.artifact_delivery
        arguments = store.intent["arguments"]
        destination = plan.destination(arguments["proposal"]["destination_ref"])
        expected = "runs/" + session.run_id + "/deliveries/" + destination.filename
        if (path != expected or store.intent["request_id"] != admission.identifier
                or arguments["plan_sha256"] != plan.sha256
                or arguments["proposal"]["artifact_sha256"] != "sha256:" + hashlib.sha256(raw).hexdigest()
                or arguments["proposal"]["artifact_bytes"] != len(raw)):
            refuse("publication_exact_binding_invalid")
        session.context.require_path(session.root / path, write=True)
        self.session, self.observed, self.store, self.path, self.raw = session, observed, store, path, raw
        self.admission, self.key = admission, (str(store.control_root), store.intent["request_id"], store.intent["input_sha256"])
        self.descriptor, self.directory, self.temporary = None, None, None
        self.name = destination.filename
        with _LOCK:
            if self.key in _LIVE:
                refuse("publication_writer_already_retained")
            if len(_LIVE) >= MAX_LIVE_WRITERS:
                refuse("publication_writer_bound")
            _LIVE[self.key] = self

    def retain(self, directory, temporary):
        """Seal the actual temporary inode before the possible publication link."""
        self.admission.held(self.session)
        self.observed.require(self.session)
        if self.descriptor is not None:
            refuse("publication_writer_not_fresh")
        descriptor = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != len(self.raw):
            os.close(descriptor)
            refuse("publication_temporary_unsafe")
        self.descriptor, self.directory, self.temporary = descriptor, os.dup(directory), temporary
        if self.read() != self.raw:
            refuse("publication_temporary_changed")
        proof = {"schema_version": "research-artifact-publication-inode/v1", "request_id": self.store.intent["request_id"],
            "input_sha256": self.store.intent["input_sha256"], "path": self.path,
            "temporary": temporary, "inode": _identity(info), "parent": _identity(os.fstat(directory)),
            "sha256": "sha256:" + hashlib.sha256(self.raw).hexdigest(), "bytes": len(self.raw),
            "writer_proof": "live_native_descriptor_required"}
        self.proof = self.store.stage_blob("publication-inode", canonical(proof))
        if self.store.read_stage(self.proof) != canonical(proof):
            refuse("publication_inode_not_durable")
        if self.observed._usage_view is not None:
            self.observed._usage_view.revalidate(self.session.root, self.observed._config)
        self.session._check("advance", write=True)
        self.session.context.require_path(self.session.root / self.path, write=True)
        self.check_parent()
        self.admission.held(self.session)

    def read(self):
        if self.descriptor is None:
            refuse("publication_descriptor_unavailable")
        before = os.fstat(self.descriptor)
        result = os.pread(self.descriptor, MAX_BYTES + 1, 0)
        after = os.fstat(self.descriptor)
        if (before.st_size > MAX_BYTES or len(result) != before.st_size or len(result) > MAX_BYTES
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            refuse("publication_descriptor_changed")
        return result

    def verify_link(self):
        """No matching bytes or missing name establish link ownership alone."""
        self.admission.held(self.session)
        self.observed.require(self.session)
        self.inspect_link()

    def check_parent(self):
        """Check the named destination namespace against the actual held parent."""
        if self.directory is None:
            refuse("publication_descriptor_unavailable")
        self.session.context.require_path(self.session.root / self.path)
        root = os.open(self.session.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        current = root
        opened = [root]
        try:
            held_root = _identity(os.fstat(root))
            expected_root = {key: self.session.binding["workspace_binding"][key] for key in ("device", "inode")}
            if held_root != expected_root:
                refuse("publication_root_changed")
            for component in self.path.split("/")[:-1]:
                current = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                opened.append(current)
            if _identity(os.fstat(current)) != _identity(os.fstat(self.directory)):
                refuse("publication_parent_changed")
        finally:
            for descriptor in reversed(opened):
                os.close(descriptor)
    def inspect_link(self):
        """Internal metadata inspection grants no content or current acceptance."""
        if self.descriptor is None or self.directory is None:
            refuse("publication_descriptor_unavailable")
        self.check_parent()
        info = os.stat(self.name, dir_fd=self.directory, follow_symlinks=False)
        held = os.fstat(self.descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or _identity(info) != _identity(held)
                or self.read() != self.raw):
            refuse("publication_link_not_owned")

    def close_committed(self):
        """Called only after the protected owning receipt is durable."""
        current = MutationStore(self.session.root, self.store.control_root, self.store.intent)
        with current.historical_locked(self.store.intent["request_id"]):
            receipt = current.receipt()
            if (receipt is None or receipt["status"] not in {"committed", "reconciled"}
                    or receipt["input_sha256"] != self.store.intent["input_sha256"]
                    or type(receipt["result"]) is not dict
                    or receipt["result"].get("path") != self.path
                    or receipt["result"].get("artifact_sha256") != "sha256:" + hashlib.sha256(self.raw).hexdigest()
                    or receipt["result"].get("bytes") != len(self.raw)):
                refuse("publication_committed_receipt_required")
        with _LOCK:
            if _LIVE.get(self.key) is not self:
                refuse("publication_writer_custody_changed")
            _LIVE.pop(self.key)
        for descriptor in (self.directory, self.descriptor):
            if descriptor is not None:
                os.close(descriptor)
        self.directory = self.descriptor = None


def retained_writer(control_root, request_id, input_sha256):
    """A process restart or exhausted custody remains explicitly unavailable."""
    with _LOCK:
        return _LIVE.get((str(control_root), request_id, input_sha256))
