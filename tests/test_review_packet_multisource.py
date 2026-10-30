"""Native source owners preserve namespaces; meters simulate host protocol only."""

import ast
import json
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

from evidence_wiki import Workspace
from evidence_wiki import execution_session as native
from evidence_wiki.counter_context import packet as counter_packet
from evidence_wiki.coverage_observation import observe
from evidence_wiki.host_context import HostContext
from evidence_wiki.mutation_contracts import canonical, digest
from evidence_wiki.pack_discovery import owner
from evidence_wiki.review_context import ReviewContext
from evidence_wiki.review_packet_execution import reserve_evaluator
from evidence_wiki.review_page_session import assembled
from evidence_wiki.source_capture_operations import GRANT as CAPTURE_GRANT
from tests import test_execution_session as reference
from tests._execution_fixture import authenticate, closure
from tests._usage_fixture import UsageFixture
from tests.test_mutation_operations import GRANTS
from tests.test_native_capture_revision import metadata


def test_actual_two_native_sources_bind_regular_pages_and_counter_alternatives(tmp_path, monkeypatch):
    original_draft = reference.draft_fixture.__wrapped__
    added = {}
    def create_two_sources(folder, patched):
        root, control, proposal, normalized = original_draft(folder, patched)
        context = HostContext([root], GRANTS | {CAPTURE_GRANT}, "caller", [root], mutation_control_directory=control)
        with context.activate(), Workspace.open(root) as workspace:
            delivery = workspace.sources.capture_bytes_document(
                b"Alternate retained measurement was 0.61 ratio. Its source context differs from the primary study.\n",
                metadata("plain_text", capture_id="secondary", title="Alternate retained study",
                    retrieved_at=datetime.now(timezone.utc).isoformat(), scope={"question_id":"q"}))
            request = workspace.sources.prepare_capture(request_id="capture-secondary", path="raw/web/secondary.txt", delivery=delivery)
            result = workspace.sources.capture_revision(request)
            assert result["status"] == "committed"
            request = workspace.mutations.prepare(request_id="inventory-secondary", operation="sources.inventory", arguments={})
            assert workspace.mutations.submit(request)["status"] == "committed"
            sid = result["result"]["source_id"]
            request = workspace.mutations.prepare(request_id="normalize-secondary", operation="normalize.sources",
                arguments={"source_ids":[sid], "force":False, "dry_run":False})
            assert workspace.mutations.submit(request)["status"] == "committed"
            path = root / "sources/normalized" / (owner("normalize_sources").safe_source_id(sid) + ".md")
            assert path.is_file()
            added.update(root=root, sid=sid, normalized=path)
        return root, control, proposal, normalized
    monkeypatch.setattr(reference, "draft_fixture", SimpleNamespace(__wrapped__=create_two_sources))
    transact = UsageFixture.transact
    def deposit_both(self, module, action, body=None, files=None):
        receipt = transact(self, module, action, body, files)
        if action == "deposit" and self.root == added.get("root") and body["source_id"] != added["sid"]:
            sid, normalized = added["sid"], added["normalized"]
            descriptor = {"schema_version":"evidence-source-revision/v1", "source_id":sid, "parents":[],
                "normalized_path":normalized.relative_to(self.root).as_posix(), "evidence_root":"evidence", "temporal":{}}
            protected = {"source-record.json":canonical(descriptor), descriptor["normalized_path"]:normalized.read_bytes()}
            for relative in ["raw/web/secondary.txt", "raw/web/secondary.txt.provenance.yml"]:
                protected["evidence/" + relative] = (self.root / relative).read_bytes()
            revision = closure(protected)
            supplied, _ = self.source(sid, normalized=normalized.read_bytes())
            supplied["source_revision"] = revision
            supplied["grant"] = authenticate({**supplied["grant"]["payload"], "source_revision":revision}, "owner", "usage")
            supplied["scrub"] = authenticate({**supplied["scrub"]["payload"], "sanitized_revision":revision}, "runner", "scrubber")
            transact(self, module, "deposit", supplied, protected)
        return receipt
    monkeypatch.setattr(UsageFixture, "transact", deposit_both)
    root, policy, execution, context = reference.fixture.__wrapped__(tmp_path, monkeypatch,
        SimpleNamespace(param={"role_isolation":True, "coverage_examination":True, "simulated_model_usage":True,
            "review_delivery":True, "complete_packet_capture":True}))
    context = replace(context, grants=context.grants | {"review_page_operations.prepare", "review_page_operations.page",
        "review_page_session.assembled", "review_packet_execution.bind", "review_packet_execution.reserve", "coverage_observation.observe"})
    code = ast.parse(execution.executor._commands["researcher"][-1])
    printed = next(item for item in ast.walk(code) if isinstance(item, ast.Call) and isinstance(item.func, ast.Name) and item.func.id == "print")
    generated = json.loads(ast.literal_eval(printed.args[0]))
    proposal = generated["proposal"]
    claim = proposal["inventory"]["claims"][0]
    primary = claim["evidence"][0]["source_id"]
    assert primary != added["sid"]
    with context.activate(), Workspace.open(root) as workspace:
        request = workspace.mutations.prepare(request_id="admit-primary", operation="strict.admit", arguments={"proposal":proposal})
        assert workspace.mutations.submit(request)["status"] == "committed"
        with workspace.research.session(policy, run_id="two-native-sources", execution=execution) as session:
            value = assembled(session, request_id="generator-alice-prior99", claim_id=claim["id"], task_role="critic", page_chars=512)
            assert value["packet"]["complete"]["complete"]
            attempt = reserve_evaluator(session, request_id="critic-two-sources", assembly_request_id="generator-alice-prior99",
                original_id="q", claim_id=claim["id"], config_ref="profile-b")
            document = attempt.input_document
            header = document["complete_review_delivery"]["packet"]["header"]
            assert document["packet"]["context_id"] == header["context_id"] == attempt.job["context_id"]
            assert "generator-alice-prior99" not in json.dumps(attempt.worker_document())
            assert header["context_sha256"] == digest(document["packet"])
            assert attempt.input_sha256 == attempt.job["packet_sha256"]
            assert header["sources"] == [{key: row[key] for key in ["source_id", "source_revision", "record_sha256", "metadata"]}
                for row in document["packet"]["sources"]]
            pages = document["complete_review_delivery"]["packet"]["pages"]
            for row in document["packet"]["sources"]:
                slices = [page for page in pages if page.get("source_id") == row["source_id"]]
                assert "".join(page["text"] for page in slices) == row["content"]
            supplement = json.loads("".join(page["text"] for page in pages if page.get("section_id") == "mechanical-counter"))
            alternatives = supplement["counter_context"]["alternative_sources"]
            assert len(alternatives) == 1
            assert alternatives[0]["source_id"].startswith("alternative-")
            assert "0.61" in alternatives[0]["content"]
            assert "0.61" not in document["packet"]["sources"][0]["content"]
            with native.gate(session):
                retained = next(row["data"] for row in native.retained(session) if row["kind"] == "execution_attempt"
                    and row["data"]["attempt"]["attempt_id"] == attempt.attempt_id)
                instruction = observe(root, question_slug="q", source_ids=[primary, added["sid"]])
            exact_context = ReviewContext.restore(document["packet"], retained["review_context"])
            counter = counter_packet(instruction, exact_context)["examination"]
            assert [{key: row[key] for key in ["source_id", "record_sha256", "content"]} for row in alternatives] == counter["alternative_sources"]
            assert supplement["counter_context"]["criteria"] == counter["criteria"]
            assert counter["basis_id"] == exact_context.sha256
            assert supplement["counter_search_completeness"] == "not_established"
            assert not value["accepted"] and not document["complete_review_delivery"]["accepted"]
