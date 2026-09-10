"""Independently authored snapshots with host-owned input revisions and signatures."""

from __future__ import annotations

import copy
from types import SimpleNamespace

import yaml

from evidence_wiki import Workspace
from tests._execution_fixture import authenticate, canonical, closure, example, identifier
from tests._usage_fixture import UsageFixture


class SnapshotFixture(UsageFixture):
    def __init__(self, directory, monkeypatch):
        super().__init__(directory, monkeypatch)
        (self.root / "research.yml").write_text(yaml.safe_dump(self.config), encoding="utf-8")
        self.workspace = Workspace.open(self.root)
        self.module = SimpleNamespace(transact=self._transact)
        self.transact(self.module, "initialize")
        self.parent, self.parent_files = self.source("lab:measurements", evidence={"inputs.txt": b"2\n3\n"})
        self.transact(self.module, "deposit", self.parent, self.parent_files)

    def _transact(self, root, config, command, artifacts):
        return self.workspace.usage.transact(command, artifacts=artifacts)

    def add_execution(self, *, source_id="execution:lab", outcome="passed", history=True, training=True,
                      export=True, normalized=None, edit=None, extra_parents=None):
        files, record = example(history=history, outcome=outcome)
        original_records = [identifier("evidence-execution-record/v1", value["payload"]) for value in record["records"]]
        original_receipts = [identifier("evidence-verification-receipt/v1", value["payload"]) for value in record["receipts"]]
        record["source_id"] = source_id
        if edit is not None:
            edit(files, record)
        ids, records, receipts = {}, [], []
        for previous_id, envelope in zip(original_records, record["records"], strict=True):
            payload = copy.deepcopy(envelope["payload"])
            payload["inputs"][0]["revision_id"] = self.parent["source_revision"]
            for key in ("predecessor", "hypothesis"):
                if payload[key] is not None:
                    payload[key] = ids[payload[key]]
            ids[previous_id] = identifier("evidence-execution-record/v1", payload)
            records.append(authenticate(payload, "runner", "generator"))
        selected_receipt = None
        for old_id, envelope in zip(original_receipts, record["receipts"], strict=True):
            payload = copy.deepcopy(envelope["payload"])
            payload["target_record_id"] = ids[payload["target_record_id"]]
            receipts.append(authenticate(payload, "evaluator", "evaluator"))
            if old_id == record["selected_receipt_id"]:
                selected_receipt = identifier("evidence-verification-receipt/v1", payload)
        record.update(records=records, receipts=receipts, selected_record_id=ids[record["selected_record_id"]],
                      selected_receipt_id=selected_receipt)
        files["execution-record.json"] = canonical(record)
        body, originals = self.source(source_id, parents=[self.parent["source_revision"], *(extra_parents or [])],
                                      training=training, export=export, normalized=normalized, evidence=files)
        self.transact(self.module, "deposit", body, originals)
        return body, originals

    def selection(self, *bodies, negatives=False):
        return {"schema_version": "evidence-snapshot-selection/v1", "source_revisions": [body["source_revision"] for body in bodies],
                "include_negative_examples": negatives, "purpose": "training-snapshot", "consumer": "evidence-wiki"}

    def register(self, request):
        preparation = self.workspace.snapshots.prepare(request)
        command = self.command("register", preparation["registration"]["body"])
        assert command["payload"]["expected_checkpoint"] == preparation["registration"]["expected_checkpoint"]
        result = self.workspace.usage.transact(command)
        self.checkpoint = result["checkpoint"]
        return preparation, command

    def export(self, request):
        preparation, command = self.register(request)
        result = self.workspace.snapshots.export(request, registration_request_id=command["payload"]["request_id"])
        return (self.root / result["path"]).read_bytes(), preparation, command, result

    def deposit_changed(self, body, files):
        """The host reattests to changed sanitized bytes; hashes alone grant nothing."""
        body = copy.deepcopy(body)
        body["source_revision"] = closure(files)
        body["grant"]["payload"]["source_revision"] = body["source_revision"]
        body["scrub"]["payload"]["sanitized_revision"] = body["source_revision"]
        body["grant"] = authenticate(body["grant"]["payload"], "owner", "usage")
        body["scrub"] = authenticate(body["scrub"]["payload"], "runner", "scrubber")
        self.transact(self.module, "deposit", body, files)
        return body
