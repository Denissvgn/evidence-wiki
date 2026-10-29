"""Native terminal observations preserve exact sources and original obligations."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from evidence_wiki import Workspace
from evidence_wiki.errors import EvidenceWikiError
from tests._semantic_fixture import native_fixture
from tests.test_execution_session import fixture
from tests.test_semantic_judging import journal_digest, record_acceptance_owner


def test_retained_exact_quote_cannot_support_the_opposite_finite_value(tmp_path, monkeypatch, request):
    root, policy, execution, context = native_fixture(tmp_path, monkeypatch, semantic_reversed_value=True)
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="opposite-observation", execution=execution) as session:
            result = session.run_until_terminal(request_id="drive", expected_basis_id=session.current()["basis_id"], max_steps=64)
            assert result["terminal"] and result["outcome"] != "accepted"
            records = execution.records(session)
            proposals = [row["data"]["observation"]["output"] for row in records if row["kind"] == "execution_observation"
                and (row["data"]["observation"].get("output") or {}).get("proposal")]
            assert proposals
            claim = proposals[0]["proposal"]["inventory"]["claims"][0]
            assert "value=-5" in claim["text"]
            quote = claim["evidence"][0]["quote"]
            assert quote and "-5" not in quote
            normalized = list((root / "sources/normalized").glob("*.md"))
            assert any(quote in path.read_text() for path in normalized)
            assert session.current()["accounting"]["accepted_count"] == 0
            record_acceptance_owner(request, "ResearchSession.run_until_terminal", {
                "terminal": True, "outcome": result["outcome"], "exact_quote_retained": True,
                "opposite_value": "-5", "accepted_count": 0, "records_sha256": journal_digest(records),
                "observation_class": "fixture_mechanics"})


def test_missing_original_evidence_finishes_partial_without_dropping_original(tmp_path, monkeypatch, request):
    root, policy, execution, context = fixture.__wrapped__(tmp_path, monkeypatch, SimpleNamespace(param={'partial': True}))
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="missing-original", execution=execution) as session:
            before = session.current()
            initial_originals = before["accounting"]["original_outcomes"]
            assert len(initial_originals) == 2 and len({row["original_id"] for row in initial_originals}) == 2
            initial_texts = {row["original_id"]: row["original_text"] for row in initial_originals}
            result = session.run_until_terminal(request_id="drive", expected_basis_id=before["basis_id"], max_steps=64)
            assert result["terminal"] and result["outcome"] == "partial", result
            originals = session.current()["accounting"]["original_outcomes"]
            assert len(originals) == 2 and len({row["original_id"] for row in originals}) == 2
            by_id = {row["original_id"]: row for row in originals}
            assert set(by_id) == {"q", "unavailable"}
            assert by_id["q"]["accepted"] and not by_id["unavailable"]["accepted"]
            assert {row["original_id"]: row["original_text"] for row in originals} == initial_texts
            assert by_id["unavailable"]["original_text"] == "What unavailable independent observation was retained?"
            record_acceptance_owner(request, "ResearchSession.run_until_terminal", {
                "terminal": True, "outcome": result["outcome"], "original_outcomes": originals,
                "records_sha256": journal_digest(execution.records(session)), "observation_class": "fixture_mechanics"})


def test_ungranted_delivery_refuses_without_creating_publication(tmp_path, monkeypatch, request):
    root, policy, execution, context = fixture.__wrapped__(tmp_path, monkeypatch, SimpleNamespace(param={'artifact_delivery': True}))
    restricted = replace(context, grants=context.grants - {'research_session.deliver'})
    with restricted.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="ungranted-output", execution=execution) as session:
            with pytest.raises(EvidenceWikiError) as caught:
                session.deliver(request_id="publish", proposal={})
            assert caught.value.error_code == "HOST_CONTEXT_REFUSED"
            assert caught.value.details == {"reason": "host_operation_not_granted"}
            assert not (root / 'runs/ungranted-output/deliveries').exists()
            record_acceptance_owner(request, "ResearchSession.deliver", {
                "terminal": True, "outcome": "blocked_external", "error_code": caught.value.error_code,
                "published": False, "authorized": False, "evidence_accepted": False})
