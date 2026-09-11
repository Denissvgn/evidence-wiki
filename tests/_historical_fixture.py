"""Later computations over host-accepted historical laboratory inputs."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
from datetime import datetime, timedelta

from tests._execution_fixture import KEYS, authenticate, canonical
from tests._snapshot_fixture import SnapshotFixture
from tests._temporal_fixture import TemporalFixture, clock_claim


def late_signature(payload, principal, role):
    envelope = authenticate(payload, principal, role)
    auth = envelope["authentication"]
    auth["issued_at"] = "2026-09-10T05:00:00Z"
    del auth["signature"]
    auth["signature"] = hmac.new(bytes.fromhex(KEYS[principal]), b"evidence-attestation/v1\0" + canonical({
        "payload": payload, "authentication": auth}), hashlib.sha256).hexdigest()
    return envelope


class HistoricalFixture(TemporalFixture):
    selection = SnapshotFixture.selection
    register = SnapshotFixture.register
    export = SnapshotFixture.export

    def __init__(self, directory, monkeypatch):
        super().__init__(directory, monkeypatch)
        self.input_context = self.workspace._script("_execution_temporal")
        monkeypatch.setitem(self.input_context.current_view.__wrapped__.__globals__, "datetime", self.clock)
        execution = self.workspace._script("_execution_evidence")
        original = execution.load_workspace_module

        def load(script_dir, stem, **kwargs):
            return self.input_context if stem == "_execution_temporal" else original(script_dir, stem, **kwargs)

        monkeypatch.setitem(execution.__dict__, "load_workspace_module", load)

    def history(self, *, mode="historical-audit", parent_times=None, parent_parents=None, edit=None, outcome="passed",
                available=True, sign=late_signature):
        self.parent, self.parent_files = self.captured("lab:measurements", evidence={"inputs.txt": b"2\n3\n"},
                                                       temporal=parent_times, parents=parent_parents)
        if available:
            self.availability(self.parent, self.parent_files)
        self.set_time("2026-09-10T05:00:00Z")

        def changes(files, record):
            for envelope in record["records"] + record["receipts"]:
                for field in ("started_at", "finished_at"):
                    original = datetime.fromisoformat(envelope["payload"][field].replace("Z", "+00:00"))
                    envelope["payload"][field] = (original + timedelta(hours=4)).isoformat()
            for envelope in record["records"]:
                envelope["payload"]["temporal"] = {"mode": mode, "cutoff": "2026-09-10T01:00:00Z", "limitations": []}
            if edit is not None:
                edit(files, record)

        body, files = SnapshotFixture.add_execution(self, edit=changes, sign=sign, outcome=outcome)
        descriptor = json.loads(files["source-record.json"])
        times = copy.deepcopy(json.loads(self.parent_files["source-record.json"])["temporal"])
        times.update(measurement=None, published_at=clock_claim("2026-09-10T05:00:00Z"),
                     available_at=clock_claim("2026-09-10T05:00:00Z"), expires_at=None, record_path=None, provenance_path=None)
        descriptor["temporal"] = times
        files["source-record.json"] = canonical(descriptor)
        files["publication-proof.json"] = canonical({"available_at": "2026-09-10T05:00:00Z"})
        body = self.deposit_files(body, files)
        self.availability(body, files)
        return body, files

    def assessment(self, body, files):
        execution = self.workspace._script("_execution_evidence")
        prefix = json.loads(files["source-record.json"])["evidence_root"]
        report = execution.validate_closure(body["source_id"], {path[len(prefix) + 1:]: raw for path, raw in files.items()
                                                              if path.startswith(prefix + "/")})
        assessment = execution.assess_verification(report, self.root, self.config, self.clock.value,
                                                   source_record={"usage_revision_id": body["source_revision"]})
        return report, assessment
