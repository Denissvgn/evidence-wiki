"""Host-authored, domain-neutral assessment inputs for API and installed callers."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import yaml

from tests._execution_fixture import authenticate, canonical, closure
from tests._publication_fixture import write_ship_ready_vendor_fixture
from tests._usage_fixture import UsageFixture

SOURCE_ID = "web:vendor-official-product-spec"
QUESTION = "vendor-product-spec"


class AssessmentFixture(UsageFixture):
    def __init__(self, directory, monkeypatch, initialize):
        super().__init__(directory, monkeypatch)
        self.policy["principals"]["owner"]["roles"].append("assessment")
        self.save_policy()
        profile = yaml.safe_load((Path(__file__).parent / "fixtures/workspace-init-profile.yml").read_text())
        profile["workspace_init"]["target_path"] = str(self.root)
        profile["workspace_init"]["questions"] = [{"id": QUESTION, "question": "What is the vendor product spec?", "priority": "high"}]
        profile_path = directory / "profile.yml"
        profile_path.write_text(yaml.safe_dump(profile))
        self.root.rmdir()
        initialize(profile_path)
        write_ship_ready_vendor_fixture(self.root)
        config_path = self.root / "research.yml"
        self.config = {**yaml.safe_load(config_path.read_text()), **self.config}
        config_path.write_text(yaml.safe_dump(self.config, sort_keys=False))
        self.normalized_path = self.root / "sources/normalized/web--vendor-official-product-spec.md"
        self.raw_path = self.root / "raw/web/vendor-product.html"

    def temporal_source(self, *, expires="2026-12-01T00:00:00Z", supersedes=None, normalized=None):
        body, files = self.source(SOURCE_ID, normalized=normalized or self.normalized_path.read_bytes(),
                                  evidence={"raw/web/vendor-product.html": self.raw_path.read_bytes()})
        descriptor = json.loads(files["source-record.json"])
        def claim(value):
            return {"value": value, "basis": "source-declared" if value else "unknown"}
        descriptor["temporal"] = {
            "schema_version": "evidence-temporal-source/v1", "asserting_principal": "runner",
            "measurement": None, "published_at": claim("2026-07-02T12:00:00Z"),
            "available_at": claim("2026-07-02T12:00:00Z"),
            "claimed_retrieved_at": claim("2026-09-10T00:00:00Z"), "effective": None,
            "expires_at": None if expires is None else claim(expires), "supersedes": supersedes,
            "record_path": None, "provenance_path": None,
        }
        files["source-record.json"] = canonical(descriptor)
        revision = closure(files)
        grant, scrub = copy.deepcopy(body["grant"]["payload"]), copy.deepcopy(body["scrub"]["payload"])
        grant["source_revision"], scrub["sanitized_revision"] = revision, revision
        return {"source_id": SOURCE_ID, "source_revision": revision,
                "grant": authenticate(grant, "owner", "usage"), "scrub": authenticate(scrub, "runner", "scrubber")}, files

    def request(self, **changes):
        return {"schema_version": "evidence-assessment-request/v1", "question_slugs": [QUESTION],
                "temporal": {"mode": "current", "cutoff": None}, "purpose": "research", "consumer": "evidence-wiki",
                "expires_at": None, "review": "approved", **changes}

    def sign(self, command):
        self.counter += 1
        command = copy.deepcopy(command)
        command["request_id"] = f"assessment-request-{self.counter}"
        return authenticate(command, "owner", "assessment")

    def refresh(self, **changes):
        return {"schema_version": "evidence-assessment-refresh-request/v1", "changed_sources": [],
                "evaluation_time": None, "limit": 32, "cursor": None, **changes}
