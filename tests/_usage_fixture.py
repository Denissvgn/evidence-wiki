"""Independent host-authored permission, scrub, and revision examples."""

from __future__ import annotations

import copy
from pathlib import Path

from tests._execution_fixture import authenticate, canonical, closure, host_policy, identifier


class UsageFixture:
    def __init__(self, directory: Path, monkeypatch):
        self.root = (directory / "workspace").resolve()
        self.root.mkdir()
        self.host = (directory / "host").resolve()
        self.host.mkdir(mode=0o700)
        self.policy_path = self.host / "authority.json"
        self.policy = host_policy(self.policy_path)
        self.policy["principals"]["owner"]["roles"].append("revocation")
        self.policy["principals"]["runner"]["roles"].append("scrubber")
        self.save_policy()
        monkeypatch.setenv("EVIDENCE_WIKI_AUTHORITY_FILE", str(self.policy_path))
        monkeypatch.setenv("EVIDENCE_WIKI_STATE_DIR", str(self.host))
        self.config = {"evidence_trust": {"policy_id": "lab-authority", "policy_revision": "1"},
                       "evidence_usage": {"state_id": "laboratory"}}
        self.binding = identifier("evidence-host-workspace/v1", {"root": str(self.root)})
        self.counter = 0
        self.checkpoint = None

    def save_policy(self):
        self.policy_path.write_bytes(canonical(self.policy))
        self.policy_path.chmod(0o600)

    def command(self, action, body=None, *, request_id=None):
        self.counter += 1
        payload = {"schema_version": "evidence-usage-command/v1", "state_id": "laboratory",
                   "workspace_binding": self.binding, "request_id": request_id or f"request-{self.counter}",
                   "expected_checkpoint": self.checkpoint, "action": action, "body": body or {}}
        return authenticate(payload, "owner", "revocation" if action == "revoke" else "usage")

    def transact(self, module, action, body=None, files=None):
        envelope = self.command(action, body)
        result = module.transact(self.root, self.config, envelope, files)
        self.checkpoint = result["checkpoint"]
        return result

    def source(self, source_id="lab:sample", *, parents=None, retrieval=True, training=True, export=True,
               normalized=None, evidence=None):
        descriptor = {"schema_version": "evidence-source-revision/v1", "source_id": source_id,
                      "parents": parents or [], "normalized_path": "normalized.md", "evidence_root": None, "temporal": {}}
        files = {"normalized.md": normalized if normalized is not None else
                 f"---\nsource_id: {source_id}\ntype: normalized_source\ntitle: Laboratory observations\n---\nSanitized laboratory measurements.\n".encode()}
        if evidence:
            descriptor["evidence_root"] = "evidence"
            files.update({"evidence/" + path: data for path, data in evidence.items()})
        files["source-record.json"] = canonical(descriptor)
        revision = closure(files)
        grant = {"schema_version": "evidence-usage-grant/v1", "source_id": source_id, "source_revision": revision,
                 "permissions": {"retrieval": retrieval, "training": training, "export": export},
                 "purposes": ["research", "qa-export", "training-snapshot"], "consumers": ["evidence-wiki"],
                 "not_before": "2026-01-01T00:00:00Z", "expires_at": "2027-01-01T00:00:00Z",
                 "redaction_policy": {"id": "laboratory-scrub", "revision": "1"}, "retention": "host-managed"}
        scrub = {"schema_version": "evidence-scrub-receipt/v1", "sanitized_revision": revision,
                 "redaction_policy": copy.deepcopy(grant["redaction_policy"]), "tool": {"name": "host-scrubber", "version": "1"},
                 "outcome": "passed", "completed_at": "2026-09-10T00:00:00Z"}
        return {"source_id": source_id, "source_revision": revision,
                "grant": authenticate(grant, "owner", "usage"), "scrub": authenticate(scrub, "runner", "scrubber")}, files

    def revoke(self, module, body, *, whole=False):
        return self.transact(module, "revoke", {"source_id": body["source_id"], "scope": "source" if whole else "revision",
                                              "source_revision": None if whole else body["source_revision"], "reason": "owner-withdrawal"})
