"""Canonical original scope and historical bookkeeping versus current evidence."""

import json
import os

import pytest

from evidence_wiki import Workspace
from evidence_wiki.host_context import HostContext
from tests.test_mutation_operations import roots as roots
from tests.test_native_capture_revision import metadata
from tests.test_requirements_owner import setup as setup

pytestmark = pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd,
    reason="Native descriptor-relative lifecycle custody is unavailable.")


def selected(setup):
    root, control, _proposal, _envelope, context, _raw = setup
    return HostContext([root], context.grants | {"request_lifecycle_operations.current"}, "caller",
        [root], mutation_control_directory=control)


def writer_context(setup):
    root, control, *_ = setup
    context = selected(setup)
    return HostContext([root], context.grants | {"request_lifecycle_operations.fulfill_current",
        "request_lifecycle_operations.link_current", "request_lifecycle_operations.block_current",
        "request_lifecycle_operations.reopen_current", "source_requests.run_link_request",
        "source_requests.run_fulfill_current_request",
        "source_capture_operations.deliver",
        "question_resolve.run_block", "question_resolve.run_reopen", "question_claim.run_claim"},
        "caller", [root], mutation_control_directory=control)


def ready_source(root):
    path = root / "sources/manifest.jsonl"
    record = json.loads(path.read_bytes())
    record["provenance"]["scope"]["jurisdiction"] = "Spain"
    path.write_text(json.dumps(record) + "\n")


def test_current_fulfillment_checks_actual_normalization_then_keeps_historical_replay(setup):
    root, *_ = setup
    ready_source(root)
    with writer_context(setup).activate(), Workspace.open(root) as workspace:
        created = workspace.source_requests.add(kind="web", query_or_identifier="https://example.org/study",
            rationale="Need scoped current evidence", question_slugs=["q1"], scope={"facet_id": "primary"})
        identifier = created["request"]["request_id"]
        workspace.normalize.sources(source_ids=["sample-source"])
        request = workspace.mutations.prepare(request_id="fulfill", operation="source_requests.fulfill_current",
            arguments={"request_id": identifier, "source_id": "sample-source"})
        receipt = workspace.source_requests.fulfill_current(request)
        assert receipt["status"] == "committed", receipt
        assert receipt["result"]["current_ready"] and not receipt["evidence_accepted"]
        (root / "raw/web/source.html").unlink()
        assert workspace.source_requests.fulfill_current(request) == receipt
        actual = workspace.source_requests.current(request_ids=[identifier])
        assert actual["requests"][0]["recorded_status"] == "fulfilled"
        assert not actual["requests"][0]["current_ready"]


def test_strict_native_block_keeps_missing_request_until_complete_evidence(setup):
    root, *_ = setup
    with writer_context(setup).activate(), Workspace.open(root) as workspace:
        created = workspace.source_requests.add(kind="web", query_or_identifier="https://example.org/study",
            rationale="Need evidence", question_slugs=["q1"], scope={"facet_id": "primary"})
        identifier = created["request"]["request_id"]
        workspace.questions.claim(slug="q1", agent_id="caller")
        request = workspace.mutations.prepare(request_id="block", operation="questions.block_current",
            arguments={"slug": "q1", "blocking_request_ids": [identifier], "reason": "Missing current scoped source"})
        receipt = workspace.questions.block_current(request)
        assert receipt["status"] == "committed", receipt
        request = workspace.mutations.prepare(request_id="reopen", operation="questions.reopen_current",
            arguments={"slug": "q1", "needs_complete": False})
        refused = workspace.questions.reopen_current(request)
        assert refused["status"] in {"refused", "partial", "commit_unknown"}, refused
        assert refused["error"]["error_code"] == "MUTATION_REQUEST_BLOCKERS_NOT_READY"
        assert "status: blocked" in (root / "wiki/questions/q1.md").read_text()


def test_native_reopen_requires_every_current_blocker_then_returns_open(setup):
    root, *_ = setup
    ready_source(root)
    with writer_context(setup).activate(), Workspace.open(root) as workspace:
        created = workspace.source_requests.add(kind="web", query_or_identifier="https://example.org/study",
            rationale="Need evidence", question_slugs=["q1"], scope={"facet_id": "primary"})
        identifier = created["request"]["request_id"]
        workspace.questions.claim(slug="q1", agent_id="caller")
        blocked = workspace.mutations.prepare(request_id="block", operation="questions.block_current",
            arguments={"slug": "q1", "blocking_request_ids": [identifier], "reason": "Missing scoped original"})
        assert workspace.questions.block_current(blocked)["status"] == "committed"
        workspace.normalize.sources(source_ids=["sample-source"])
        fulfilled = workspace.mutations.prepare(request_id="fulfill", operation="source_requests.fulfill_current",
            arguments={"request_id": identifier, "source_id": "sample-source"})
        assert workspace.source_requests.fulfill_current(fulfilled)["status"] == "committed"
        observed = workspace.source_requests.current(question_slugs=["q1"])
        assert observed["questions"][0]["current_ready"]
        request = workspace.mutations.prepare(request_id="reopen", operation="questions.reopen_current",
            arguments={"slug": "q1"})
        result = workspace.questions.reopen_current(request)
        assert result["status"] == "committed", result
        assert result["result"]["status"] == "open"
        assert workspace.questions.reopen_current(request) == result


def test_explicit_same_scope_link_is_owned_and_scope_changes_refuse(setup):
    root, *_ = setup
    with writer_context(setup).activate(), Workspace.open(root) as workspace:
        created = workspace.source_requests.add(kind="web", query_or_identifier="https://example.org/study",
            rationale="Need evidence", question_slugs=["q1"], scope={"facet_id": "primary"})
        identifier = created["request"]["request_id"]
        request = workspace.mutations.prepare(request_id="link", operation="source_requests.link_current",
            arguments={"request_id": identifier, "question_slugs": ["q1"], "scope": {"facet_id": "primary"}})
        result = workspace.source_requests.link_current(request)
        assert result["status"] == "committed" and not result["result"]["updated"], result
        assert workspace.source_requests.link_current(request) == result
        request = workspace.mutations.prepare(request_id="other", operation="source_requests.link_current",
            arguments={"request_id": identifier, "question_slugs": ["q1"], "scope": {"facet_id": "counterevidence"}})
        refused = workspace.source_requests.link_current(request)
        assert refused["status"] != "committed"
        assert refused["error"]["error_code"] == "MUTATION_REQUEST_LINK_SCOPE_CONFLICT"


def test_partial_captured_text_cannot_fulfill_a_complete_request(setup):
    root, *_ = setup
    with writer_context(setup).activate(), Workspace.open(root) as workspace:
        created = workspace.source_requests.add(kind="web", query_or_identifier="https://example.org/partial",
            rationale="Need complete context", question_slugs=["q1"], scope={"facet_id": "primary"})
        request_id = created["request"]["request_id"]
        delivery = workspace.sources.capture_bytes_document(b"Retained partial excerpt with relevant observed content. " * 10,
            metadata(completeness="partial", scope={"jurisdiction": "Spain", "facet_id": "primary"}, request_id=request_id))
        capture = workspace.sources.prepare_capture(request_id="capture", path="raw/web/partial.txt", delivery=delivery)
        captured = workspace.sources.capture_revision(capture)
        assert captured["status"] == "committed", captured
        source_id = captured["result"]["source_id"]
        workspace.sources.inventory()
        workspace.normalize.sources(source_ids=[source_id])
        path = root / "sources/source-requests.jsonl"
        before = path.read_bytes()
        request = workspace.mutations.prepare(request_id="fulfill", operation="source_requests.fulfill_current",
            arguments={"request_id": request_id, "source_id": source_id, "needs_complete": False})
        result = workspace.source_requests.fulfill_current(request)
        assert result["status"] != "committed"
        assert result["error"]["error_code"] == "MUTATION_REQUEST_EVIDENCE_NOT_READY"
        assert path.read_bytes() == before


def test_current_request_retains_gap_and_true_frozen_question_scope(setup):
    root, _control, _proposal, _envelope, _context, _raw = setup
    with selected(setup).activate(), Workspace.open(root) as workspace:
        created = workspace.source_requests.add(kind="web", query_or_identifier="https://example.org/study",
            rationale="Need primary original", question_slugs=["q1"], scope={"facet_id": "primary"})
        view = workspace.source_requests.current(request_ids=[created["request"]["request_id"]], question_slugs=["q1"])
    assert view["requests"][0]["recorded_status"] == "open"
    assert not view["requests"][0]["current_ready"]
    assert view["requests"][0]["required_scope"] == {"jurisdiction": "Spain"}
    assert "current_source_unassigned" in view["requests"][0]["gaps"]
    assert not view["authorized"] and not view["evidence_accepted"]


def test_historical_fulfillment_does_not_hide_missing_original_or_normalization(setup):
    root, _control, _proposal, _envelope, _context, _raw = setup
    with selected(setup).activate(), Workspace.open(root) as workspace:
        created = workspace.source_requests.add(kind="web", query_or_identifier="https://example.org/study",
            rationale="Need current evidence", question_slugs=["q1"], scope={"facet_id": "primary"})
        identifier = created["request"]["request_id"]
        workspace.source_requests.fulfill(request_id=identifier, source_id="sample-source", require_scope=True)
        (root / "raw/web/source.html").unlink()
        view = workspace.source_requests.current(request_ids=[identifier], question_slugs=["q1"])
    row = view["requests"][0]
    assert row["recorded_status"] == "fulfilled" and row["recorded_source_id"] == "sample-source"
    assert not row["current_ready"] and "current_original_unavailable" in row["gaps"]
    assert "source_scope_missing" in row["gaps"]


@pytest.mark.parametrize("selector", ["foreign", "sample"])
def test_unbound_question_selector_cannot_become_original_scope(setup, selector):
    root, *_ = setup
    with selected(setup).activate(), Workspace.open(root) as workspace:
        from evidence_wiki.errors import EvidenceWikiError
        with pytest.raises(EvidenceWikiError):
            workspace.source_requests.current(question_slugs=[selector])
