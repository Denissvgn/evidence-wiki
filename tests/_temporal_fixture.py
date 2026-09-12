"""Host-authored temporal claims and independently attested public availability."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import yaml

from evidence_wiki import Workspace
from tests._execution_fixture import KEYS, authenticate, binding, canonical, closure
from tests._snapshot_fixture import SnapshotFixture
from tests._usage_fixture import UsageFixture


def clock_claim(value):
    return {"value": value, "basis": "unknown" if value is None else "provider-asserted"}


class TemporalFixture(UsageFixture):
    def __init__(self, directory, monkeypatch):
        super().__init__(directory, monkeypatch)
        self.policy["principals"]["evaluator"]["roles"].append("availability")
        self.save_policy()
        (self.root / "research.yml").write_text(yaml.safe_dump(self.config), encoding="utf-8")
        self.workspace = Workspace.open(self.root)
        self.script = self.workspace._script("evidence_temporal")
        self.module = SimpleNamespace(transact=self._transact)

        class Clock:
            value = datetime(2026, 9, 10, 1, tzinfo=timezone.utc)

            @classmethod
            def now(cls, zone):
                return cls.value

        self.clock = Clock
        for function in (self.workspace._script("evidence_usage").transact, self.script.evaluate.__globals__["current_view"].__wrapped__):
            monkeypatch.setitem(function.__globals__, "datetime", Clock)
        self.transact(self.module, "initialize")

    def _transact(self, root, config, command, artifacts):
        return self.workspace.usage.transact(command, artifacts=artifacts)

    def set_time(self, value):
        self.clock.value = datetime.fromisoformat(value.replace("Z", "+00:00"))

    def captured(self, source_id="lab:sample", *, value="17.50", published="2026-09-09T10:00:00Z",
                 available="2026-09-09T10:00:00Z", supersedes=None, temporal=None, evidence=None,
                 normalized=None, parents=None, retrieval=True, metadata=None):
        body, files = self.source(source_id, parents=parents, retrieval=retrieval, normalized=normalized, evidence=evidence)
        descriptor = json.loads(files["source-record.json"])
        times = {"schema_version": "evidence-temporal-source/v1", "asserting_principal": "runner",
                 "measurement": {"start": clock_claim("2026-09-01T00:00:00Z"), "end": clock_claim("2026-09-02T00:00:00Z")},
                 "published_at": clock_claim(published), "available_at": clock_claim(available),
                 "claimed_retrieved_at": clock_claim(None), "effective": None, "expires_at": None,
                 "supersedes": supersedes, "record_path": "structured.json", "provenance_path": "provenance.json"}
        if temporal is not None:
            times.update(copy.deepcopy(temporal))
        descriptor["temporal"] = times
        files.update({"source-record.json": canonical(descriptor),
                      "structured.json": canonical({"source_id": source_id, "measurement": {"value": value}, **(metadata or {})}),
                      "provenance.json": canonical({"source_id": source_id, "retrieved_at": available,
                                                    "origin_url": "https://example.invalid/observations", "provider_registration": {"id": "laboratory"}}),
                      "publication-proof.json": canonical({"archive_record": "independently checked by host", "available_at": available})})
        return self.deposit_files(body, files), files

    def deposit_files(self, body, files):
        body = copy.deepcopy(body)
        body["source_revision"] = closure(files)
        body["grant"]["payload"]["source_revision"] = body["source_revision"]
        body["scrub"]["payload"]["sanitized_revision"] = body["source_revision"]
        body["grant"] = authenticate(body["grant"]["payload"], "owner", "usage")
        body["scrub"] = authenticate(body["scrub"]["payload"], "runner", "scrubber")
        self.transact(self.module, "deposit", body, files)
        return body

    def availability(self, body, files, *, principal="evaluator", edit=None, commit=True):
        temporal = json.loads(files["source-record.json"])["temporal"]
        payload = {"schema_version": "evidence-availability-receipt/v1", "source_id": body["source_id"],
                   "source_revision": body["source_revision"], "asserting_principal": temporal["asserting_principal"],
                   "published_at": temporal["published_at"]["value"], "available_at": temporal["available_at"]["value"],
                   "method": "public-archive", "proof_artifacts": [{"path": "publication-proof.json",
                      "content_hash": binding(files["publication-proof.json"])["content_hash"]}]}
        receipt = authenticate(payload, principal, "availability")
        receipt["authentication"]["issued_at"] = self.clock.value.isoformat()
        auth = {key: value for key, value in receipt["authentication"].items() if key != "signature"}
        receipt["authentication"]["signature"] = hmac.new(bytes.fromhex(KEYS[principal]), b"evidence-attestation/v1\0" +
             canonical({"payload": payload, "authentication": auth}), hashlib.sha256).hexdigest()
        body = {"source_id": body["source_id"], "source_revision": body["source_revision"], "receipt": receipt}
        if edit is not None:
            edit(body)
        if commit:
            self.transact(self.module, "attest-availability", body)
        return body

    def request(self, *sources, mode="historical-audit", cutoff="2026-09-10T01:00:00Z", checkpoint=None, analysis=None):
        return {"schema_version": "evidence-temporal-request/v1", "mode": mode, "cutoff": None if mode == "current" else cutoff,
                "checkpoint": None if mode == "current" else checkpoint or self.checkpoint,
                "source_ids": list(sources) or ["lab:sample"], "purpose": "research", "consumer": "evidence-wiki",
                "analysis": analysis or {"query": "observations", "facets": [], "grounding": [], "domain_pack": {}, "question_frontmatter": {}}}

    def evaluate(self, request=None):
        return self.workspace.temporal.evaluate(request or self.request())

    def execution_source(self, *, mode="historical-audit", cutoff="2026-09-10T01:00:00Z", parent_times=None):
        self.parent, self.parent_files = self.captured("lab:measurements", evidence={"inputs.txt": b"2\n3\n"}, temporal=parent_times)
        body, files = SnapshotFixture.add_execution(self)
        descriptor = json.loads(files["source-record.json"])
        descriptor["temporal"] = copy.deepcopy(json.loads(self.parent_files["source-record.json"])["temporal"])
        descriptor["temporal"].update(measurement=None, published_at=clock_claim("2026-09-10T00:10:00Z"),
                                      available_at=clock_claim("2026-09-10T00:10:00Z"), record_path=None, provenance_path=None)
        files["source-record.json"] = canonical(descriptor)
        files["publication-proof.json"] = canonical({"available_at": "2026-09-10T00:10:00Z"})
        body = self.deposit_files(body, files)
        if mode == "historical-available":
            self.availability(self.parent, self.parent_files)
            self.availability(body, files)
        selection = {**SnapshotFixture.selection(self, body), "schema_version": "evidence-snapshot-selection/v2",
                     "temporal": {"mode": mode, "cutoff": cutoff, "checkpoint": self.checkpoint}}
        return body, files, selection

    register = SnapshotFixture.register
    export = SnapshotFixture.export
