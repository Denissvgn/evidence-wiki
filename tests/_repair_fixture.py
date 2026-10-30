"""Real source capture/deposit precedes immutable reference profile calibration."""

import copy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from evidence_wiki import Workspace
from evidence_wiki.host_context import HostContext
from evidence_wiki.mutation_contracts import canonical
from evidence_wiki.pack_discovery import owner
from evidence_wiki.source_capture_operations import GRANT as CAPTURE_GRANT
from tests import test_execution_session as reference
from tests._execution_fixture import authenticate, closure
from tests._usage_fixture import UsageFixture
from tests.test_mutation_operations import GRANTS
from tests.test_native_capture_revision import metadata

PEER_DETAIL = "PERMITTED_PEER_SCIENTIFIC_DETAIL_9f731"


@pytest.fixture
def two_sources(tmp_path, monkeypatch):
    original = reference.draft_fixture.__wrapped__
    added = {}
    def capture_second(folder, patched):
        root, control, proposal, normalized = original(folder, patched)
        context = HostContext([root], GRANTS | {CAPTURE_GRANT}, "caller", [root], mutation_control_directory=control)
        with context.activate(), Workspace.open(root) as workspace:
            retrieved_at = datetime.now(timezone.utc).isoformat()
            delivery = workspace.sources.capture_bytes_document((PEER_DETAIL + " was 0.61 ratio.\n").encode(),
                metadata("plain_text", capture_id="peer", title="Peer retained study",
                    retrieved_at=retrieved_at, scope={"question_id": "q"},
                    rights={"status": "allowed", "license": "CC0-1.0", "terms_url": None,
                        "note": "The test author dedicates this newly constructed synthetic text to CC0-1.0."}))
            request = workspace.sources.prepare_capture(request_id="capture-peer", path="raw/web/peer.txt", delivery=delivery)
            captured = workspace.sources.capture_revision(request)
            assert captured["status"] == "committed"
            request = workspace.mutations.prepare(request_id="inventory-peer", operation="sources.inventory", arguments={})
            assert workspace.mutations.submit(request)["status"] == "committed"
            sid = captured["result"]["source_id"]
            request = workspace.mutations.prepare(request_id="normalize-peer", operation="normalize.sources",
                arguments={"source_ids": [sid], "force": False, "dry_run": False})
            assert workspace.mutations.submit(request)["status"] == "committed"
            path = root / "sources/normalized" / (owner("normalize_sources").safe_source_id(sid) + ".md")
            digest = "sha256:" + __import__("hashlib").sha256(path.read_bytes()).hexdigest()
            peer = copy.deepcopy(proposal["inventory"]["claims"][0])
            peer.update(id="unrelated-peer", text=PEER_DETAIL + " was 0.61 ratio.", premises=[])
            peer["evidence"] = [{**peer["evidence"][0], "source_id": sid, "record_sha256": digest,
                "observed_at": retrieved_at, "quote": PEER_DETAIL + " was 0.61 ratio.", "location_hint": None, "anchor": None}]
            proposal["inventory"]["claims"].append(peer)
            note = root / "wiki/sources/peer.md"
            note.parent.mkdir(parents=True, exist_ok=True)
            note.write_text("---\ntype: source\ncreated: '2026-10-05'\nupdated: '2026-10-05'\nsource_ids:\n  - " + sid + "\n---\n# Peer study\n\n" + PEER_DETAIL + " was 0.61 ratio.\n")
            with (root / "index.md").open("a") as stream:
                stream.write("\n- [Peer study](wiki/sources/peer.md)\n")
            added.update(root=root, sid=sid, normalized=path, proposal=copy.deepcopy(proposal))
        return root, control, proposal, normalized
    monkeypatch.setattr(reference, "draft_fixture", SimpleNamespace(__wrapped__=capture_second))
    actual = UsageFixture.transact
    def deposit_peer(self, module, action, body=None, files=None):
        receipt = actual(self, module, action, body, files)
        if action == "deposit" and self.root == added.get("root") and body["source_id"] != added["sid"]:
            sid, path = added["sid"], added["normalized"]
            descriptor = {"schema_version": "evidence-source-revision/v1", "source_id": sid, "parents": [],
                "normalized_path": path.relative_to(self.root).as_posix(), "evidence_root": "evidence", "temporal": {}}
            retained = {"source-record.json": canonical(descriptor), descriptor["normalized_path"]: path.read_bytes()}
            for relative in ("raw/web/peer.txt", "raw/web/peer.txt.provenance.yml"):
                retained["evidence/" + relative] = (self.root / relative).read_bytes()
            revision = closure(retained)
            supplied, _files = self.source(sid, normalized=path.read_bytes())
            supplied["source_revision"] = revision
            supplied["grant"] = authenticate({**supplied["grant"]["payload"], "source_revision": revision}, "owner", "usage")
            supplied["scrub"] = authenticate({**supplied["scrub"]["payload"], "sanitized_revision": revision}, "runner", "scrubber")
            actual(self, module, "deposit", supplied, retained)
        return receipt
    monkeypatch.setattr(UsageFixture, "transact", deposit_peer)
    bound = reference.fixture.__wrapped__(tmp_path, monkeypatch, SimpleNamespace(param={
        "role_isolation": True, "simulated_model_usage": True, "review_services": True,
        "disagreement": True, "repair": True, "service_repair": True}))
    return (*bound, added)
