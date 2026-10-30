"""Actual native packet boundaries do not grant missing delivery authority."""

import pytest

from evidence_wiki import Workspace
from evidence_wiki import execution_session as native
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.mutation_contracts import digest
from evidence_wiki.review_page_session import assembled
from tests import test_review_packet_execution as configured


@pytest.fixture
def packet_case(tmp_path, monkeypatch):
    return configured.configured.__wrapped__(tmp_path, monkeypatch)


def test_profile_requires_delivery_for_direct_reserve_and_public_host_job_without_attempt(packet_case):
    root, policy, execution, context, claim, _capture = packet_case
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="direct-delivery", execution=execution) as session:
            assert execution.profile.document()["schema_version"] == "research-execution-profile/v7"
            with native.gate(session):
                native._begin(session)
                before = native.retained(session)
                current = native.current(session)
                with pytest.raises(EvidenceWikiError) as direct:
                    native.reserve(session, "missing-delivery", {"operation":"evaluate", "purpose":"evaluate",
                        "original_id":"q", "claim_id":claim["id"], "config_ref":"profile-a"}, current)
                assert direct.value.error_code == "RESEARCH_COMPLETE_REVIEW_DELIVERY_REQUIRED"
                assert native.retained(session) == before
            prepared = workspace.strict.prepare_review(claim["id"])
            with pytest.raises(EvidenceWikiError) as public:
                session.host_job(request_id="public-missing", operation="evaluate",
                    payload={"claim_id":claim["id"], "packet_id":digest(prepared["snapshot"])})
            assert public.value.error_code == "RESEARCH_COMPLETE_REVIEW_DELIVERY_REQUIRED"
            assert not any(row["kind"] == "execution_attempt" for row in native.retained(session))


def test_legitimate_partial_packet_reports_missing_required_page_and_handle_lifetime(packet_case):
    root, _policy, _execution, context, claim, _capture = packet_case
    with context.activate():
        workspace = Workspace.open(root)
        handle = workspace.strict.prepare_packet(claim["id"], context_id="generator-alice-prior99",
            source_ids=[claim["evidence"][0]["source_id"]], page_chars=256)
        header = handle.header()
        assert "generator-alice-prior99" not in str(header)
        pages = [handle.page(row["page_id"]) for row in header["pages"]]
        assert len(pages) >= 2 and handle.complete(pages)["complete"]
        partial = handle.complete(pages[:-1])
        assert not partial["complete"] and partial["missing_page_ids"] == [pages[-1]["page_id"]]
        assert partial["accepted"] is False and partial["semantic_approval"] == "not_evaluated"
        workspace.close()
        with pytest.raises(EvidenceWikiError):
            handle.page(header["pages"][0]["page_id"])


def test_terminal_new_assembly_is_refused_before_any_native_intent(packet_case):
    root, policy, execution, context, claim, _capture = packet_case
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="terminal-pages", execution=execution) as session:
            with native.gate(session):
                native._begin(session)
                session._finish(native._document(session), "No additional review work")
            before = native.retained(session)
            with pytest.raises(EvidenceWikiError) as refused:
                assembled(session, request_id="new-terminal-pages", claim_id=claim["id"])
            assert refused.value.details["reason"] == "packet_run_terminal"
            assert native.retained(session) == before


def test_current_packet_and_native_prewrite_do_not_call_external_host_authentication(packet_case, monkeypatch):
    from evidence_wiki.review_packet_execution import require_current, reserve_evaluator
    root, policy, execution, context, claim, _capture = packet_case
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="pure-page-proof", execution=execution) as session:
            assembled(session, request_id="complete-pages", claim_id=claim["id"])
            attempt = reserve_evaluator(session, request_id="reserved-review", assembly_request_id="complete-pages",
                original_id="q", claim_id=claim["id"], config_ref="profile-a")
            row = native._attempt_rows(native.retained(session))[attempt.attempt_id]
            def forbidden(*_args, **_kwargs):
                pytest.fail("Retained current page proof called the external host")
            monkeypatch.setattr(execution.executor, "authenticate", forbidden)
            with native.gate(session):
                require_current(session, attempt, row)
                current = session.current()
                assert current["execution"]["pending"]["attempt_id"] == attempt.attempt_id
            assert attempt.input_document["packet"]["context_id"] == attempt.job["context_id"]
            assert attempt.input_document["packet"]["context_id"] == attempt.input_document["complete_review_delivery"]["packet"]["header"]["context_id"]
