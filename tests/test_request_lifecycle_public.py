"""Real onboarding and shared evidence through the core lifecycle interface."""

import os

import pytest

from evidence_wiki import Onboarding, Workspace
from evidence_wiki.host_context import HostContext
from tests.test_mutation_operations import GRANTS
from tests.test_native_capture_revision import metadata
from tests.test_research_planning import question_plan, request

pytestmark = pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd,
    reason="Native descriptor-relative lifecycle custody is unavailable.")


def test_public_setup_one_source_two_questions_all_blockers_and_historical_state(tmp_path, monkeypatch):
    monkeypatch.delenv("EVIDENCE_WIKI_AUTHORITY_FILE", raising=False)
    monkeypatch.delenv("EVIDENCE_WIKI_STATE_DIR", raising=False)
    declared = request(tmp_path)
    root = tmp_path / "workspace"
    declared["request"]["payload"]["authority"]["source_scope"] = [str(root / "raw")]
    declared["request"]["payload"]["derived_questions"] = [{"id": "q2", "text": "What related evidence addresses the original scope?",
        "original_ids": ["q1"]}]
    declared["decisions"]["question_plans"].append(question_plan("q2"))
    with Onboarding.open(allowed_roots=[tmp_path], allow=["apply"]) as onboarding:
        result = onboarding.apply(onboarding.plan(declared))
    assert result["setup_ready"]
    control = tmp_path / "mutation-control"
    control.mkdir(mode=0o700)
    grants = GRANTS | {"request_lifecycle_operations.current", "request_lifecycle_operations.link_current",
        "request_lifecycle_operations.fulfill_current", "request_lifecycle_operations.block_current",
        "request_lifecycle_operations.reopen_current", "source_requests.run_link_request",
        "source_requests.run_fulfill_current_request", "source_capture_operations.deliver",
        "question_claim.run_claim", "question_resolve.run_block", "question_resolve.run_reopen"}
    selected = HostContext([root], grants, "caller", [root], mutation_control_directory=control)
    with selected.activate(), Workspace.open(root) as workspace:
        created = workspace.source_requests.add(kind="web", query_or_identifier="https://example.org/original",
            rationale="Need full scoped original", question_slugs=["q1"], scope={"facet_id": "primary"})
        request_id = created["request"]["request_id"]
        link = workspace.mutations.prepare(request_id="link", operation="source_requests.link_current",
            arguments={"request_id": request_id, "question_slugs": ["q2"], "scope": {"facet_id": "primary"}})
        linked = workspace.source_requests.link_current(link)
        assert linked["status"] == "committed" and linked["result"]["updated"], linked
        assert linked["result"]["request"]["question_slugs"] == ["q1", "q2"]
        for slug in ["q1", "q2"]:
            workspace.questions.claim(slug=slug, agent_id="caller")
            blocked = workspace.mutations.prepare(request_id="block-" + slug, operation="questions.block_current",
                arguments={"slug": slug, "blocking_request_ids": [request_id], "reason": "Missing scoped original"})
            assert workspace.questions.block_current(blocked)["status"] == "committed"
        content = ("Original measured observation for the stated jurisdiction, with context and limitations. " * 12).encode()
        delivery = workspace.sources.capture_bytes_document(content,
            metadata(scope={"jurisdiction": "Spain", "facet_id": "primary"}, request_id=request_id))
        capture = workspace.sources.prepare_capture(request_id="capture", path="raw/papers/original.txt", delivery=delivery)
        captured = workspace.sources.capture_revision(capture)
        assert captured["status"] == "committed", captured
        source_id = captured["result"]["source_id"]
        inventory = workspace.mutations.prepare(request_id="inventory", operation="sources.inventory", arguments={})
        assert workspace.mutations.submit(inventory)["status"] == "committed"
        normalization = workspace.mutations.prepare(request_id="normalize", operation="normalize.sources",
            arguments={"source_ids": [source_id]})
        normalized = workspace.mutations.submit(normalization)
        assert normalized["status"] == "committed", normalized
        fulfilled = workspace.mutations.prepare(request_id="fulfill", operation="source_requests.fulfill_current",
            arguments={"request_id": request_id, "source_id": source_id})
        receipt = workspace.source_requests.fulfill_current(fulfilled)
        assert receipt["status"] == "committed", receipt
        view = workspace.source_requests.current(request_ids=[request_id], question_slugs=["q1", "q2"])
        assert all(row["current_ready"] for row in view["questions"])
        for slug in ["q1", "q2"]:
            reopened = workspace.mutations.prepare(request_id="reopen-" + slug, operation="questions.reopen_current",
                arguments={"slug": slug})
            result = workspace.questions.reopen_current(reopened)
            assert result["status"] == "committed" and result["result"]["status"] == "open", result
        (root / "raw/papers/original.txt").unlink()
        historical = workspace.source_requests.fulfill_current(fulfilled)
        assert historical == receipt
        current = workspace.source_requests.current(request_ids=[request_id])
        assert current["requests"][0]["recorded_status"] == "fulfilled"
        assert not current["requests"][0]["current_ready"]
        assert not current["evidence_accepted"] and not current["authorized"]
