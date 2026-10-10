"""Disposable preauthorized backup mechanics; no production secret service claim."""

import base64
import json
import stat
from pathlib import Path

from evidence_wiki import Workspace
from evidence_wiki._pack_io import canonical
from evidence_wiki.mutation_contracts import digest
from evidence_wiki.operations_recovery import protected_head
from evidence_wiki.pack_discovery import owner


class DisposableBackupService:
    """Independent append-preserving head and fixed missing-file-only recovery."""

    service_ref = "disposable-backup"
    artifact_directories = ()

    def __init__(self, root, context, directory, *, runs=(), create=True):
        self.root, self.context = root, context
        self.anchor_directory = directory / "independent-anchor"
        self.backup_directory = directory / "protected-backups"
        if create:
            self.anchor_directory.mkdir(mode=0o700)
            self.backup_directory.mkdir(mode=0o700)
        elif not (self.anchor_directory / "current-head.json").is_file():
            raise ValueError("The independent current anchor is lost")
        self.roots = (root, context.state_directory, context.mutation_control_directory)
        self.history = None
        self.trust = None
        self.runs = tuple(runs)
        self.run_history = {}
        self.receipts = {}
        self.checkpoint()

    def checkpoint(self):
        """Only a canonical valid extension can advance the retained latest head."""
        previous_path = self.anchor_directory / "current-head.json"
        previous_head = json.loads(previous_path.read_bytes()) if previous_path.exists() else None
        runs = {}
        with self.context.activate(), Workspace.open(self.root) as workspace:
            assert workspace.usage.status()["initialized"]
            for policy, execution, run_id in self.runs:
                with workspace.research.session(policy, run_id=run_id, execution=execution) as session:
                    session.current()
                    records = execution.records(session)
                    retained = self.run_history.get(run_id, [])
                    if records[:len(retained)] != retained:
                        raise ValueError("Native journal history regressed")
                    self.run_history[run_id] = records
                    runs[run_id] = [digest(record) for record in records]
        trust = json.loads(self.context.authority_file.read_bytes())
        if self.trust is not None and (trust["principals"] != self.trust["principals"]
                or not set(self.trust["revoked_keys"]).issubset(trust["revoked_keys"])
                or not set(self.trust["revoked_envelopes"]).issubset(trust["revoked_envelopes"])):
            raise ValueError("The retained trust history regressed")
        self.trust = trust
        for path, raw in self.receipts.items():
            if not path.exists() or path.read_bytes() != raw:
                raise ValueError("An original mutation receipt regressed")
        for name in ("intent.json", "receipt.json"):
            self.receipts.update({path: path.read_bytes() for path in self.context.mutation_control_directory.rglob(name)})
        state = json.loads((self.context.state_directory / "evidence-state.json").read_bytes())
        if self.history is not None and state["events"][:len(self.history)] != self.history:
            raise ValueError("The protected event chain regressed")
        self.history = state["events"]
        event_refs = [digest(event) for event in self.history]
        receipts = {path.relative_to(self.context.mutation_control_directory).as_posix():
                    owner("_usage_materialization").file_digest(raw) for path, raw in self.receipts.items()}
        if previous_head is not None:
            if (event_refs[:len(previous_head["events"])] != previous_head["events"]
                    or previous_head["principals_sha256"] != digest(trust["principals"])
                    or not set(previous_head["revoked_keys"]).issubset(trust["revoked_keys"])
                    or not set(previous_head["revoked_envelopes"]).issubset(trust["revoked_envelopes"])
                    or any(runs.get(name, [])[:len(records)] != records for name, records in previous_head["runs"].items())
                    or any(receipts.get(name) != value for name, value in previous_head["receipts"].items())):
                raise ValueError("The independently retained chain regressed")
        head = protected_head(self.root, self.context)
        if (previous_head is not None and head != previous_head["head_sha256"]
                and event_refs == previous_head["events"] and runs == previous_head["runs"] and receipts == previous_head["receipts"]
                and trust["revoked_keys"] == previous_head["revoked_keys"] and trust["revoked_envelopes"] == previous_head["revoked_envelopes"]):
            raise ValueError("Unexplained bytes cannot advance the independent anchor")
        publisher = owner("_usage_materialization")
        previous = self.anchor_directory / "current-head.json"
        expected = publisher.file_digest(previous.read_bytes()) if previous.exists() else None
        publisher.publish_file(self.anchor_directory, "current-head.json", canonical({"head_sha256": head,
            "events": event_refs, "principals_sha256": digest(trust["principals"]),
            "revoked_keys": trust["revoked_keys"], "revoked_envelopes": trust["revoked_envelopes"],
            "runs": runs, "receipts": receipts}), expected)

    def latest_head(self, *, timeout_seconds):
        return json.loads((self.anchor_directory / "current-head.json").read_bytes())["head_sha256"]

    def backup(self, request_id, head_sha256, *, timeout_seconds):
        if self.latest_head(timeout_seconds=timeout_seconds) != head_sha256 or protected_head(self.root, self.context) != head_sha256:
            raise ValueError("The latest complete generation changed")
        captures = [owner("_evidence_revision").capture_workspace(root, max_attempts=1) for root in self.roots]
        observations = [owner("_evidence_revision").observe_tree(root) for root in self.roots]
        document = {"head_sha256": head_sha256, "roots": [{"path": str(root),
            "files": {name: base64.b64encode(raw).decode() for name, raw in capture.files.items()},
            "directories": list(capture.directories), "modes": {name: stat.S_IMODE(info[2]) for name, info in observed.items()}}
            for root, capture, observed in zip(self.roots, captures, observations, strict=True)]}
        raw = canonical(document)
        owner("_usage_materialization").publish_file(self.backup_directory, request_id + ".json", raw, None)
        return {"reference": request_id, "backup_sha256": owner("_usage_materialization").file_digest(raw), "head_sha256": head_sha256}

    def _document(self, reference, backup_sha256, head_sha256):
        path = self.backup_directory / (reference + ".json")
        raw = path.read_bytes()
        if owner("_usage_materialization").file_digest(raw) != backup_sha256 or self.latest_head(timeout_seconds=1) != head_sha256:
            raise ValueError("The exact latest backup is unavailable")
        document = json.loads(raw)
        if document["head_sha256"] != head_sha256 or [row["path"] for row in document["roots"]] != list(map(str, self.roots)):
            raise ValueError("The backup binding changed")
        return document

    def inspect(self, reference, backup_sha256, head_sha256, *, timeout_seconds):
        document = self._document(reference, backup_sha256, head_sha256)
        for row in document["roots"]:
            root = Path(row["path"])
            capture = owner("_evidence_revision").capture_workspace(root, max_attempts=1)
            if any(name not in row["files"] or raw != base64.b64decode(row["files"][name], validate=True)
                   for name, raw in capture.files.items()):
                return {"status": "conflict", "head_sha256": head_sha256}
            if any(name not in row["directories"] for name in capture.directories):
                return {"status": "conflict", "head_sha256": head_sha256}
            if any(row["modes"].get(name) != stat.S_IMODE(info[2]) for name, info in owner("_evidence_revision").observe_tree(root).items()):
                return {"status": "conflict", "head_sha256": head_sha256}
        return {"status": "restorable", "head_sha256": head_sha256}

    def restore(self, reference, backup_sha256, head_sha256, *, timeout_seconds):
        if self.inspect(reference, backup_sha256, head_sha256, timeout_seconds=timeout_seconds)["status"] != "restorable":
            raise ValueError("Unknown edits must be preserved")
        document = self._document(reference, backup_sha256, head_sha256)
        publisher = owner("_usage_materialization")
        for row in document["roots"]:
            root = Path(row["path"])
            for name, encoded in row["files"].items():
                raw = base64.b64decode(encoded, validate=True)
                if root == self.context.state_directory and name == "evidence-state.json":
                    store = owner("_host_evidence_store")
                    with self.context.activate(), store.locked_state(self.root, write=True) as directory:
                        current = store.read_state(directory, allow_missing=True)
                        if current not in (None, raw):
                            raise ValueError("An unexplained protected edit must be preserved")
                        if current is None:
                            store.write_state(directory, raw)
                else:
                    publisher.publish_file(root, name, raw, None)
