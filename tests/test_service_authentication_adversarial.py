"""Actual owned service mechanics refuse missing credentials and invented production."""

import ast
import json
from types import SimpleNamespace

import pytest

from evidence_wiki import Workspace
from evidence_wiki import execution_session as native
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.service_authentication import require_output
from tests import test_execution_session as reference


@pytest.fixture
def configured(tmp_path, monkeypatch):
    return reference.fixture.__wrapped__(tmp_path, monkeypatch, SimpleNamespace(param={
        "role_isolation": True, "simulated_model_usage": True, "review_services": True}))


def proposal(execution):
    tree = ast.parse(execution.executor._commands["researcher"][-1])
    printed = next(node for node in ast.walk(tree) if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name) and node.func.id == "print")
    return json.loads(ast.literal_eval(printed.args[0]))["proposal"]


def final_observation(session, execution, attempt):
    observed, reason = native._external(session, "invoke", attempt)
    assert reason is None
    for index in range(16):
        native.intake(session, observed, request_id=f"observation-{index}")
        if observed["final_accounting"]:
            return native._latest_observation(execution.records(session), attempt.attempt_id)
        observed, reason = native._external(session, "poll", attempt)
        assert reason is None
    pytest.fail("Bounded reference service did not finish")


def test_all_credentials_in_required_slot_revoked_stops_before_dispatch(configured, monkeypatch):
    root, policy, execution, context = configured
    authority = json.loads(context.authority_file.read_bytes())
    authority["revoked_keys"] = [identity.key_id for identity in execution.review_signer.reviewers
        if identity.config_ref in {"profile-a", "profile-a-backup"}]
    context.authority_file.write_text(json.dumps(authority, sort_keys=True, separators=(",", ":")))
    monkeypatch.setattr(execution.executor, "invoke", lambda *_args: pytest.fail("Missing credentials dispatched work"))
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="credential-loss", execution=execution) as session:
            report = session.review_services()
            assert report["state"] == "blocked"
            assert all(row["credential_state"] == "unavailable" for row in report["services"]
                if row["slot_ref"] == "profile-a")
            current = session.current()
            stopped = session.advance(request_id="stop", expected_basis_id=current["basis_id"])
            assert stopped["terminal"] and stopped["outcome"] == "blocked_external"
            assert not any(row["kind"] == "execution_attempt" for row in execution.records(session))


def test_imported_claim_cannot_acquire_generator_provenance_from_credential_challenge(configured, monkeypatch):
    root, policy, execution, context = configured
    supplied = proposal(execution)
    with context.activate(), Workspace.open(root) as workspace:
        prepared = workspace.mutations.prepare(request_id="imported-claim", operation="strict.admit", arguments={"proposal": supplied})
        assert workspace.mutations.submit(prepared)["status"] == "committed"
        claim_id = supplied["inventory"]["claims"][0]["id"]
        with workspace.research.session(policy, run_id="imported-production", execution=execution) as session:
            prepared = workspace.strict.prepare_review(claim_id)
            job = session.host_job(request_id="review-import", operation="evaluate", payload={
                "claim_id": claim_id, "packet_id": native.digest(prepared["snapshot"])})
            row = next(item["data"] for item in execution.records(session) if item["kind"] == "execution_attempt"
                and item["data"]["attempt"]["job"]["id"] == job["id"])
            attempt = execution.attempt(row["attempt"])
            observed = final_observation(session, execution, attempt)
            monkeypatch.setattr(type(execution.review_signer), "registration", lambda *_args, **_kwargs: pytest.fail("Invented authorship reached signing"))
            with pytest.raises(EvidenceWikiError) as caught:
                require_output(session, attempt, observed, prepared)
            assert caught.value.details["reason"] == "counter_actual_generator_provenance_unknown"
            assert not any(item["kind"] == "execution_application" for item in execution.records(session))


def test_caller_correlated_observation_cannot_replace_exact_owned_intake(configured):
    root, policy, execution, context = configured
    supplied = proposal(execution)
    with context.activate(), Workspace.open(root) as workspace:
        prepared = workspace.mutations.prepare(request_id="claim", operation="strict.admit", arguments={"proposal": supplied})
        assert workspace.mutations.submit(prepared)["status"] == "committed"
        claim_id = supplied["inventory"]["claims"][0]["id"]
        with workspace.research.session(policy, run_id="owned-output", execution=execution) as session:
            prepared = workspace.strict.prepare_review(claim_id)
            job = session.host_job(request_id="review", operation="evaluate", payload={
                "claim_id": claim_id, "packet_id": native.digest(prepared["snapshot"])})
            row = next(item["data"] for item in execution.records(session) if item["kind"] == "execution_attempt"
                and item["data"]["attempt"]["job"]["id"] == job["id"])
            attempt = execution.attempt(row["attempt"])
            observed = final_observation(session, execution, attempt)
            spoofed = json.loads(json.dumps(observed))
            spoofed["observed_usage"]["model_calls"] = 0
            with pytest.raises(EvidenceWikiError) as caught:
                require_output(session, attempt, spoofed, prepared)
            assert caught.value.details["reason"] == "service_owned_output_changed"
