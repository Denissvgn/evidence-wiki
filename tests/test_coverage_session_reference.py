"""Configured local commands use the existing attempts and canonical owners."""

import ast
from dataclasses import replace
from types import SimpleNamespace

import pytest

from evidence_wiki import Workspace
from evidence_wiki import coverage_gate as gate
from evidence_wiki import coverage_session as coverage_driver
from evidence_wiki._pack_io import json_document
from evidence_wiki.agent_qualification import HostReviewSigner, QualificationError
from evidence_wiki.errors import UsageError
from evidence_wiki.pack_discovery import owner
from tests import test_execution_session as mechanics
from tests._execution_fixture import authenticate


@pytest.fixture
def configured(tmp_path, monkeypatch, request):
    changes = getattr(request, "param", {})
    original = mechanics.fixture.__wrapped__(tmp_path, monkeypatch,
        SimpleNamespace(param={"role_isolation": True, "coverage_examination": True, "simulated_model_usage": True, **changes}))
    scopes = original[2].profile.role_scopes
    assert scopes is not None and (changes.get("counter_only") or any(scope.task_role == "coverage" for scope in scopes.scopes))
    root, _policy, execution, context = original
    context = replace(context, grants=context.grants | {"coverage_session.examine", "coverage_observation.observe", "retrieval_operations.search", "research_session.examine_coverage", "research_session.examine_counterevidence", "research_session.run_cycle"})
    return root, original[1], execution, context


def seed_admitted_claims(session):
    for index in range(8):
        if (session.root / "docs/research-claims.json").exists():
            return
        result = session.advance(request_id="seed-" + str(index), expected_basis_id=session.current()["basis_id"])
        assert not result["terminal"], result
    pytest.fail("The actual worker did not admit an inventory within the bound.")


def seed_current_review(session):
    seed_admitted_claims(session)
    for index in range(8):
        if any(row["kind"] == "execution_applied" and row["data"].get("operation") == "evaluate"
                for row in session.execution.records(session)):
            return
        result = session.advance(request_id="review-seed-" + str(index), expected_basis_id=session.current()["basis_id"])
        assert not result["terminal"], result
    pytest.fail("No actual native evaluator review was retained within the bound.")


def test_coverage_worker_simulated_protocol_has_exact_scope_and_no_review_registration(configured, monkeypatch):
    root, policy, execution, context = configured
    config_ref = next(scope.config_ref for scope in execution.profile.role_scopes.scopes if scope.task_role == "coverage")
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="coverage-reference", execution=execution) as session:
            seed_admitted_claims(session)
            monkeypatch.setattr(HostReviewSigner, "registration", lambda *_args, **_kwargs: pytest.fail("Coverage attempted review registration"))
            value = session.examine_coverage(request_id="coverage", question_slug="q", config_ref=config_ref)
            assert value["status"] == "observed" and not value["accepted"], value
            assert value["findings"]["link_proposals"]
            attempts = [row["data"] for row in execution.records(session) if row["kind"] == "execution_attempt" and row["data"].get("purpose") == "coverage"]
            assert len(attempts) == 1
            assert attempts[0]["attempt"]["job"]["required_capabilities"] == ["model", "coverage.propose"]
            assert session.current()["execution"]["pending"]["applied"]
            before = len(execution.records(session))
            monkeypatch.setattr(execution.executor, "invoke", lambda *_: pytest.fail("Historical coverage replay reinvoked a model"))
            assert session.examine_coverage(request_id="coverage", question_slug="q", config_ref=config_ref) == value
            assert len(execution.records(session)) == before


def test_authenticated_simulated_critic_blocks_terminal_without_native_review(configured, monkeypatch):
    root, policy, execution, context = configured
    config_ref = next(scope.config_ref for scope in execution.profile.role_scopes.scopes if scope.task_role == "critic")
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="counter-reference", execution=execution) as session:
            seed_admitted_claims(session)
            coverage_ref = next(scope.config_ref for scope in execution.profile.role_scopes.scopes if scope.task_role == "coverage")
            covered = session.examine_coverage(request_id="coverage-before-counter", question_slug="q", config_ref=coverage_ref)
            assert covered["status"] == "observed", covered
            seed_current_review(session)
            monkeypatch.setattr(HostReviewSigner, "registration", lambda *_args, **_kwargs: pytest.fail("Counter finding attempted review registration"))
            inventory = json_document((root / "docs/research-claims.json").read_bytes())
            claim_id = next(claim["id"] for claim in inventory["claims"] if claim["question_slug"] == "q")
            value = session.examine_counterevidence(request_id="counter", question_slug="q", config_ref=config_ref, claim_id=claim_id)
            assert value["status"] == "observed" and not value["accepted"], value
            assert value["findings"]["material_objection_candidates"]
            monkeypatch.setattr(execution.executor, "authenticate", lambda *_: pytest.fail("Terminal gate invoked host authentication"))
            stop = gate.current_objections(session, question_slug="q")
            assert stop["state"] == "blocked" and not stop["accepted"]
            with pytest.raises(UsageError) as refused:
                gate.stop(session, question_slug="q")
            assert getattr(refused.value, "error_code", None) == "COVERAGE_EXAMINATION_INVALID"


@pytest.mark.parametrize("configured", [{"simulated_model_usage": False}], indirect=True)
def test_actual_zero_model_reference_cannot_be_semantic_coverage(configured):
    root, policy, execution, context = configured
    config_ref = next(scope.config_ref for scope in execution.profile.role_scopes.scopes if scope.task_role == "coverage")
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="zero-model", execution=execution) as session:
            seed_admitted_claims(session)
            value = session.examine_coverage(request_id="coverage", question_slug="q", config_ref=config_ref)
            assert value["status"] == "blocked_external" and value["reason"] == "examination_model_provenance_unknown"
            assert not value["accepted"]
            assert session.current()["execution"]["pending"]["applied"]


@pytest.mark.parametrize("configured", [{"counter_only": True}], indirect=True)
def test_authenticated_critic_only_objection_activates_stop(configured):
    root, policy, execution, context = configured
    assert not any(scope.task_role == "coverage" for scope in execution.profile.role_scopes.scopes)
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="critic-only", execution=execution) as session:
            seed_current_review(session)
            claim = json_document((root / "docs/research-claims.json").read_bytes())["claims"][0]
            result = session.examine_counterevidence(request_id="counter", question_slug="q", claim_id=claim["id"], config_ref="profile-b")
            assert result["status"] == "observed", result
            with pytest.raises(UsageError) as stopped:
                gate.stop(session, question_slug="q")
            assert stopped.value.details["reason"] == "material_counterevidence_unresolved"


def test_offline_critic_cannot_obtain_current_semantic_qualification(tmp_path, monkeypatch):
    with pytest.raises(QualificationError, match="^qualification_offline_fixture_unsupported$"):
        mechanics.fixture.__wrapped__(tmp_path, monkeypatch, SimpleNamespace(param={
            "role_isolation": True, "coverage_examination": True, "counter_only": True,
            "simulated_model_usage": True, "counter_offline": True}))


@pytest.mark.parametrize("configured", [{"counter_disposition": "examined_no_unresolved_objection"}], indirect=True)
def test_simulated_scoped_examination_has_reachable_current_nonblocking_gate(configured):
    root, policy, execution, context = configured
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="examined-positive", execution=execution) as session:
            seed_admitted_claims(session)
            covered = session.examine_coverage(request_id="coverage", question_slug="q", config_ref="coverage-worker")
            assert covered["status"] == "observed", covered
            seed_current_review(session)
            claim = json_document((root / "docs/research-claims.json").read_bytes())["claims"][0]
            examined = session.examine_counterevidence(request_id="counter", question_slug="q", claim_id=claim["id"], config_ref="profile-b")
            assert examined["status"] == "observed" and not examined["accepted"], examined
            result = gate.current_objections(session, question_slug="q")
            assert result["state"] == "not_blocked" and not result["accepted"], result
            assert result["semantic_override"] is False


@pytest.mark.parametrize("configured, expected", [
    ({"counter_disposition": "examined_no_unresolved_objection"}, "accepted"),
    ({"counter_disposition": "opposing"}, "abstained"),
], indirect=["configured"], ids=["simulated-examined", "simulated-opposing"])
def test_simulated_automatic_drive_preserves_counter_gate(configured, expected):
    root, policy, execution, context = configured
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="examination-drive", execution=execution) as session:
            result = session.run_cycle(request_id="drive", max_steps=48)
            assert result["terminal"] and result["outcome"] == expected, result
            records = execution.records(session)
            purposes = {row["data"].get("purpose") for row in records if row["kind"] == "execution_attempt"}
            assert {"coverage", "counterevidence"} <= purposes
            assert not result["evidence_accepted"]
            assert all(role.model_ref.startswith("simulated/") for role in execution.profile.roles)
            replay = session.run_cycle(request_id="drive", max_steps=48)
            assert replay["historical"] and replay["outcome"] == expected


def test_premise_examination_cannot_credit_requested_primary_or_missing_facet():
    """Mechanical subject accounting, without claiming signed execution proof."""
    from evidence_wiki.coverage_examination import examined_subject
    findings = [{"claim_id": "premise", "facet_id": "primary", "kind": "examined_no_unresolved_objection"}]
    assert not examined_subject(findings, "inference", {"primary"})
    findings.append({"claim_id": "inference", "facet_id": "primary", "kind": "examined_no_unresolved_objection"})
    assert examined_subject(findings, "inference", {"primary"})
    assert not examined_subject(findings, "inference", {"primary", "additional"})


@pytest.mark.parametrize("configured, change", [
    ({"counter_only": True}, "withdrawal"),
    ({"counter_only": True}, "material"),
], indirect=["configured"], ids=["native-withdrawal", "changed-material"])
def test_completed_counter_replay_withholds_stale_content_without_new_work(configured, change, monkeypatch):
    root, policy, execution, context = configured
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="replay-current-rights", execution=execution) as session:
            seed_current_review(session)
            claim = json_document((root / "docs/research-claims.json").read_bytes())["claims"][0]
            initial = session.examine_counterevidence(request_id="counter", question_slug="q", claim_id=claim["id"], config_ref="profile-b")
            assert initial["status"] == "observed", initial
            records = execution.records(session)
            assert initial["findings"]["findings"][0]["evidence"]
            if change == "withdrawal":
                config = owner("verify_quotes").load_config(root)
                usage = owner("_evidence_usage")
                with usage.current_view(root, config) as live:
                    payload = {"schema_version": "evidence-usage-command/v1", "state_id": live.state.state_id,
                        "workspace_binding": live.state.binding, "request_id": "withdraw-current-source",
                        "expected_checkpoint": live.state.checkpoint, "action": "revoke",
                        "body": {"source_id": claim["evidence"][0]["source_id"], "scope": "source", "source_revision": None, "reason": "owner-withdrawal"}}
                workspace.usage.transact(authenticate(payload, "owner", "revocation"))
            else:
                config = owner("verify_quotes").load_config(root)
                normalized, _label = owner("verify_quotes").normalized_record_path(root, config, claim["evidence"][0]["source_id"])
                normalized.write_bytes(normalized.read_bytes() + b"\nChanged current source context.\n")
            monkeypatch.setattr(execution.executor, "invoke", lambda *_: pytest.fail("Completed replay reinvoked model"))
            monkeypatch.setattr(execution.executor, "poll", lambda *_: pytest.fail("Completed replay polled model"))
            monkeypatch.setattr(coverage_driver, "apply_findings", lambda *_: pytest.fail("Completed replay reapplied effects"))
            replay = session.examine_counterevidence(request_id="counter", question_slug="q", claim_id=claim["id"], config_ref="profile-b")
            assert replay["status"] == "blocked_external" and replay["findings"] is None, replay
            assert replay.get("searches", []) == [] and not replay["accepted"]
            assert execution.records(session) == records


@pytest.mark.parametrize("configured", [{"counter_only": True}], indirect=True)
def test_completed_history_can_repair_applied_marker_after_expiry_without_effect(configured, monkeypatch):
    from datetime import datetime, timedelta

    from evidence_wiki import execution_session as native
    root, policy, execution, context = configured
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="expired-history", execution=execution) as session:
            seed_current_review(session)
            claim = json_document((root / "docs/research-claims.json").read_bytes())["claims"][0]
            append = native.append
            def interrupted(bound, kind, request_id, data):
                if kind == "execution_applied" and data.get("operation") == "examination":
                    raise RuntimeError("Interrupted before historical applied marker")
                return append(bound, kind, request_id, data)
            with monkeypatch.context() as fault:
                fault.setattr(native, "append", interrupted)
                with pytest.raises(RuntimeError, match="historical applied marker"):
                    session.examine_counterevidence(request_id="counter", question_slug="q", claim_id=claim["id"], config_ref="profile-b")
            records = execution.records(session)
            counter = next(row["data"] for row in records if row["kind"] == "execution_attempt" and row["data"].get("purpose") == "counterevidence")
            final = datetime.fromisoformat(counter["attempt"]["job"]["deadline_utc"]) + timedelta(seconds=1)
            class ExpiredClock(datetime):
                @classmethod
                def now(cls, tz=None):
                    return final if tz is not None else final.replace(tzinfo=None)
            monkeypatch.setattr(coverage_driver, "datetime", ExpiredClock)
            monkeypatch.setattr(execution.executor, "invoke", lambda *_: pytest.fail("Expired completed history invoked model"))
            monkeypatch.setattr(execution.executor, "poll", lambda *_: pytest.fail("Expired completed history polled model"))
            monkeypatch.setattr(coverage_driver, "apply_findings", lambda *_: pytest.fail("Expired completed history reapplied native effects"))
            replay = session.examine_counterevidence(request_id="counter", question_slug="q", claim_id=claim["id"], config_ref="profile-b")
            assert replay["status"] == "observed" and not replay["accepted"], replay
            updated = execution.records(session)
            assert updated[:-1] == records
            assert updated[-1]["kind"] == "execution_applied"
            assert updated[-1]["data"]["attempt_id"] == counter["attempt"]["attempt_id"]


@pytest.mark.parametrize("configured", [{"counter_only": True}], indirect=True)
def test_imported_inventory_and_fresh_authorship_are_not_native_generation_proof(configured):
    """An actual signed review does not prove that a model generated imported text."""
    from datetime import datetime, timezone
    root, policy, execution, context = configured
    tree = ast.parse(execution.executor._commands["researcher"][-1])
    printed = next(node for node in ast.walk(tree) if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name) and node.func.id == "print")
    generated = json_document(ast.literal_eval(printed.args[0]).encode())
    proposal = generated["proposal"]
    with context.activate(), Workspace.open(root) as workspace:
        request = workspace.mutations.prepare(request_id="imported-admission", operation="strict.admit", arguments={"proposal": proposal})
        receipt = workspace.mutations.submit(request)
        assert receipt["status"] == "committed", receipt
        for index, link in enumerate(generated["coverage_links"]):
            linked = workspace.mutations.prepare(request_id="imported-link-" + str(index), operation="coverage.link",
                arguments={"slug": link["slug"], "facet_id": link["facet_id"], "accepted_source_ids": link["source_ids"],
                    "blocking_request_ids": None, "clear_accepted_source_ids": False, "clear_blocking_request_ids": False})
            assert workspace.mutations.submit(linked)["status"] == "committed"
            evaluated = workspace.mutations.prepare(request_id="imported-evaluate-" + str(index), operation="coverage.evaluate", arguments={"slug": link["slug"]})
            assert workspace.mutations.submit(evaluated)["status"] == "committed"
        claim = proposal["inventory"]["claims"][0]
        prepared = workspace.strict.prepare_review(claim["id"])
        envelope = execution.review_signer.registration(prepared,
            dict.fromkeys(("support", "source_suitability", "scope", "time", "units", "counterevidence"), "pass"),
            "A host-created review cannot establish prior model authorship.", "imported-review", datetime.now(timezone.utc).isoformat(), config_ref="profile-a")
        registration = workspace.mutations.prepare(request_id="imported-registration", operation="strict.register_review", arguments={"envelope": envelope})
        registered = workspace.mutations.submit(registration)
        assert registered["status"] == "committed", registered
        with workspace.research.session(policy, run_id="imported-claim", execution=execution) as session:
            result = session.examine_counterevidence(request_id="counter", question_slug="q", claim_id=claim["id"], config_ref="profile-b")
            assert result["status"] == "blocked_external" and not result["accepted"], result
            assert result["reason"] == "counter_actual_generator_provenance_unknown", result
            records = execution.records(session)
            assert not any(row["kind"] == "execution_application" and row["data"].get("operation") == "generate" for row in records)
