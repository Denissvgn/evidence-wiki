"""Private explicit-operation journals built on the existing lock and file owners."""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
from pathlib import Path

from ._pack_io import canonical, identity, json_document, read_file
from .pack_authoring_store import child, protect, publish
from .pack_catalog import _writer_flags
from .pack_discovery import owner
from .planning_contracts import refuse


class Journal:
    def __init__(self, root, namespace, identifier, intent):
        if re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,95}", identifier) is None:
            refuse("journal_identifier_invalid")
        self.root = Path(root).expanduser().absolute()
        self.path = self.root / namespace / identifier
        self.namespace, self.identifier, self.intent = namespace, identifier, intent
        protect(self.path)

    @contextlib.contextmanager
    def locked(self):
        before = identity(self.root)
        root = os.open(self.root, _writer_flags())
        directory = None
        try:
            directory = child(root, self.namespace + "/" + self.identifier)
            held = identity(directory)
            entries = set(os.listdir(directory))
            if (entries - {"intent.json", "operation.lock", "state.json"}
                    or "state.json" in entries and "intent.json" not in entries):
                refuse("journal_contains_unowned_state", "ONBOARDING_TARGET_CONFLICT")
            publish(directory, "operation.lock", b"")
            with owner("_workspace_locks").workspace_lock(self.path / "operation.lock", purpose="explicit host operation"):
                if identity(self.root) != before or identity(self.path) != held:
                    refuse("journal_root_changed", "ONBOARDING_PLAN_STALE")
                publish(directory, "intent.json", canonical(self.intent))
                yield self
                if identity(self.root) != before or identity(self.path) != held:
                    refuse("journal_root_changed", "ONBOARDING_PLAN_STALE")
        finally:
            if directory is not None:
                os.close(directory)
            os.close(root)

    def read(self):
        path = self.path / "state.json"
        return json_document(read_file(self.path, "state.json")) if path.exists() or path.is_symlink() else None

    def write(self, value):
        path = self.path / "state.json"
        before = read_file(self.path, "state.json") if path.exists() or path.is_symlink() else None
        owner("_usage_materialization").publish_file(self.path, "state.json", canonical(value),
            "sha256:" + hashlib.sha256(before).hexdigest() if before is not None else None)
