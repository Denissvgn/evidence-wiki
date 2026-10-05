"""Owned host protocol checks use simulated meters, never live model semantics."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from evidence_wiki import Workspace
from evidence_wiki import execution_session as native
from evidence_wiki.errors import EvidenceWikiError
from tests import test_execution_session as reference


@pytest.fixture
def configured(tmp_path, monkeypatch):
    return reference.fixture.__wrapped__(tmp_path, monkeypatch, SimpleNamespace(param={
        "role_isolation": True, "simulated_model_usage": True, "review_services": True}))


def test_automatic_host_service_generation_review_and_exact_terminal_replay(configured):
    root, policy, execution, context = configured
    assert policy.to_dict()["schema_version"] == "research-run-policy/v3"
    assert execution.profile.document()["schema_version"] == "research-execution-profile/v8"
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="authenticated-services", execution=execution) as session:
            status = session.review_services()
            assert status["state"] == "ready", status
            assert all(row["execution_state"] == "not_observed" for row in status["services"])
            last = session.run_until_terminal(request_id="service", expected_basis_id=session.current()["basis_id"], max_steps=32)
            assert last["outcome"] == "accepted", last
            records = execution.records(session)
            votes = [row["data"] for row in records if row["kind"] == "execution_applied" and row["data"].get("operation") == "evaluate"]
            assert {row["service_slot_ref"] for row in votes} == {"profile-a", "profile-b"}
            assert all(row["service_identity"]["controller_ref"] == execution.profile.role(row["profile_ref"]).controller_ref for row in votes)
            assert all(row["service_identity"]["policy_id"] == policy.policy_id for row in votes)
            replay = session.advance(request_id=last["request_id"], expected_basis_id=last["before_basis_id"])
            assert replay["historical"] and replay["outcome"] == "accepted"
            assert execution.records(session) == records


def test_predispatch_missing_runtime_service_uses_approved_actual_alternate(configured):
    root, policy, execution, context = configured
    # Credentials/qualification remain frozen; the primary configured runtime is
    # unavailable before dispatch, producing a genuine signed zero-use receipt.
    execution.executor._commands.pop("profile-a")
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="service-alternative", execution=execution) as session:
            last = session.run_until_terminal(request_id="alternate", expected_basis_id=session.current()["basis_id"], max_steps=32)
            assert last["outcome"] == "accepted", last
            records = execution.records(session)
            attempts = [row["data"] for row in records if row["kind"] == "execution_attempt" and row["data"].get("service_slot_ref") == "profile-a"]
            assert [row["attempt"]["config_ref"] for row in attempts] == ["profile-a", "profile-a-backup"]
            applied = next(row["data"] for row in records if row["kind"] == "execution_applied" and row["data"].get("profile_ref") == "profile-a-backup")
            assert applied["service_slot_ref"] == "profile-a"
            assert applied["service_identity"]["controller_ref"] == "backup-controller"
            assert applied["profile_ref"] != applied["service_slot_ref"]
            primary = native._latest_observation(records, attempts[0]["attempt"]["attempt_id"])
            assert primary["state"] == "unavailable" and primary["final_accounting"]
            assert primary["observed_usage"]["model_calls"] == 0


@pytest.mark.parametrize("unavailable", ["wrong-key", "deleted-authority"])
def test_actual_missing_credentials_stop_finitely_without_model_or_human_queue(tmp_path, monkeypatch, unavailable):
    root, policy, execution, context = reference.fixture.__wrapped__(tmp_path, monkeypatch, SimpleNamespace(param={
        "role_isolation": True, "simulated_model_usage": True, "review_services": True,
        "service_wrong_key": unavailable == "wrong-key"}))
    monkeypatch.setattr(execution.executor, "invoke", lambda *_args: pytest.fail("Unusable credential dispatched a model"))
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="unavailable-service", execution=execution) as session:
            if unavailable == "deleted-authority":
                before = execution.records(session)
                context.authority_file.unlink()
                with pytest.raises(EvidenceWikiError) as refused:
                    session.review_services()
                assert refused.value.error_code == "MUTATION_HOST_CONTEXT_CONFLICT"
                assert execution.records(session) == before
                return
            value = session.review_services()
            assert value["state"] == "blocked", value
            step = session.advance(request_id="blocked-service", expected_basis_id=session.current()["basis_id"])
            assert step["terminal"] and step["outcome"] == "blocked_external", step
            assert not any(row["kind"] == "execution_attempt" for row in execution.records(session))
            assert not step["evidence_accepted"]



def test_actual_zero_review_refusal_settles_replay_and_shared_counter_cap(tmp_path, monkeypatch):
    from evidence_wiki.coverage_observation import observe
    root, policy, execution, context = reference.fixture.__wrapped__(tmp_path, monkeypatch, SimpleNamespace(param={
        "role_isolation":True, "coverage_examination":True, "simulated_model_usage":True,
        "review_services":True, "service_zero_critic":True, "service_slot_tools":1}))
    context = replace(context, grants=context.grants | {"coverage_observation.observe"})
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="zero-owned-service", execution=execution) as session:
            for index in range(10):
                value = session.advance(request_id=f"producer-{index}", expected_basis_id=session.current()["basis_id"])
                if any(row["kind"] == "execution_applied" and row["data"].get("operation") == "generate" for row in execution.records(session)):
                    break
                assert not value["terminal"], value
            else:
                pytest.fail("Actual producer did not reach native applied admission")
            with native.gate(session):
                before = native.current(session)
                attempt = native.reserve(session,"zero-critic",{"operation":"evaluate","purpose":"evaluate",
                    "config_ref":"profile-b","original_id":"q","claim_id":"observation"},before)
            from tests.test_service_authentication_adversarial import final_observation
            result = final_observation(session,execution,attempt)
            assert result["observed_usage"]["model_calls"] == 0 and result["final_accounting"]
            monkeypatch.setattr(type(execution.review_signer),"registration",lambda *_args,**_kwargs:pytest.fail("Zero model output reached signing"))
            native._apply(session,attempt.attempt_id)
            records = execution.records(session)
            refusal = next(row for row in records if row["kind"] == "execution_service_refusal")
            assert refusal["data"]["reason"] == "REVIEW_SERVICE_REFUSED"
            assert any(row["kind"] == "execution_applied" and row["data"].get("operation") == "service_refused" for row in records)
            native._apply(session,attempt.attempt_id)
            assert execution.records(session) == records
            assert session.pending()["applied"] and not session.pending()["reservation_occupied"]
            with native.gate(session):
                view = native.current(session)
                instruction = observe(root,question_slug="q",source_ids=[row["source_id"] for row in view["execution"]["source_bindings"]])
                with pytest.raises(EvidenceWikiError) as blocked:
                    native.reserve(session,"counter-after-slot",{"operation":"evaluate","purpose":"counterevidence",
                        "config_ref":"profile-b","original_id":"q","claim_id":"observation","examination_instruction":instruction},view)
                assert blocked.value.error_code == "RESEARCH_BUDGET_EXHAUSTED"
            assert not any(row["kind"] == "execution_attempt" and row["data"]["attempt"]["attempt_id"] == "counter-after-slot" for row in execution.records(session))



def test_actual_alternate_vote_expiry_cannot_use_primary_qualification_at_prewrite(tmp_path, monkeypatch):
    import json
    from datetime import datetime, timedelta

    from evidence_wiki.review_service_routing import review_current
    from tests.test_service_authentication_adversarial import final_observation
    root, policy, execution, context = reference.fixture.__wrapped__(tmp_path,monkeypatch,SimpleNamespace(param={
        "role_isolation":True,"simulated_model_usage":True,"review_services":True,"service_short_backup":True}))
    # This deployment selection precedes the immutable session binding. The
    # primary remains qualified, but its current credential is unavailable.
    authority = json.loads(context.authority_file.read_bytes())
    authority["revoked_keys"] = ["evaluator-key"]
    context.authority_file.write_text(json.dumps(authority,sort_keys=True,separators=(",", ":")))
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy,run_id="target-expiry",execution=execution) as session:
            for index in range(10):
                step = session.advance(request_id=f"generation-{index}",expected_basis_id=session.current()["basis_id"])
                if any(row["kind"] == "execution_applied" and row["data"].get("operation") == "generate" for row in execution.records(session)):
                    break
                assert not step["terminal"],step
            else:
                pytest.fail("Actual owned producer did not settle")
            with native.gate(session):
                attempt = native.reserve(session,"alternate-vote",{"operation":"evaluate","purpose":"evaluate",
                    "config_ref":"profile-a-backup","original_id":"q","claim_id":"observation"},native.current(session))
            observed = final_observation(session,execution,attempt)
            native._apply(session,attempt.attempt_id)
            records = execution.records(session)
            application = next(row["data"] for row in records if row["kind"] == "execution_application" and row["data"]["attempt_id"] == attempt.attempt_id)
            vote = next(row["data"] for row in records if row["kind"] == "execution_applied" and row["data"]["attempt_id"] == attempt.attempt_id)
            assert vote["passed"] and vote["profile_ref"] == "profile-a-backup"
            assert not any(row["kind"] == "execution_applied" and row["data"].get("profile_ref") == "profile-a" for row in records)
            # Move only the trusted qualification observation clock across an
            # actually issued immutable expiry; no signed record is changed.
            signed = json.loads(execution.qualifications._records)
            target = next(row for row in signed if row["profile_ref"] == "profile-a-backup")
            future = (datetime.fromisoformat(target["expires_utc"])+timedelta(seconds=1)).isoformat()
            with monkeypatch.context() as timed:
                timed.setattr(native,"now_utc",lambda:future)
                current = native.current(session)
                rows = {row["profile_ref"]:row for row in current["qualification"]["profiles"]}
                assert rows["profile-a"]["qualified"] is True
                assert rows["profile-a-backup"]["qualified"] is not True
                assert current["qualification"]["state"] == "qualified"
                wanted = policy.to_dict()["required_evaluators"][0]
                assert not review_current(session,vote,current,wanted)
                with pytest.raises(EvidenceWikiError) as refused:
                    native._mutation(session,"expired-target-use","strict.register_review",{"envelope":application["envelope"]},
                        effect_deadline=attempt.job["deadline_utc"],effect_precondition=lambda:native._validate_application(session,attempt,observed,application,allow_receipt_proof=False))
                assert refused.value.error_code == "RESEARCH_RESULT_INELIGIBLE"
            with native.owner("_evidence_usage").current_view(root,native.owner("verify_quotes").load_config(root)) as view:
                assert len(view.state.strict_reviews) == 1



def test_actual_accepted_release_loses_only_expired_alternate_target_qualification(tmp_path,monkeypatch):
    import json
    from datetime import datetime, timedelta
    root,policy,execution,context = reference.fixture.__wrapped__(tmp_path,monkeypatch,SimpleNamespace(param={
        "role_isolation":True,"simulated_model_usage":True,"review_services":True,"service_short_backup":True}))
    authority = json.loads(context.authority_file.read_bytes())
    authority["revoked_keys"] = ["evaluator-key"]
    context.authority_file.write_text(json.dumps(authority,sort_keys=True,separators=(",", ":")))
    with context.activate(),Workspace.open(root) as workspace:
        with workspace.research.session(policy,run_id="current-release-expiry",execution=execution) as session:
            last = session.run_until_terminal(request_id="current-release",expected_basis_id=session.current()["basis_id"],max_steps=32)
            assert last["outcome"] == "accepted",last
            before = native.policy_observation(session,native.current(session))
            assert before["review_pass"] and before["selected_outcomes"]
            assert all(row["independently_eligible"] for row in before["selected_outcomes"])
            records = execution.records(session)
            assert not any(row["kind"] == "execution_applied" and row["data"].get("profile_ref") == "profile-a" for row in records)
            signed = json.loads(execution.qualifications._records)
            target = next(row for row in signed if row["profile_ref"] == "profile-a-backup")
            future = (datetime.fromisoformat(target["expires_utc"])+timedelta(seconds=1)).isoformat()
            with monkeypatch.context() as timed:
                timed.setattr(native,"now_utc",lambda:future)
                view = native.current(session)
                rows = {row["profile_ref"]:row for row in view["qualification"]["profiles"]}
                assert rows["profile-a"]["qualified"] is True and rows["profile-a-backup"]["qualified"] is not True
                assert view["qualification"]["state"] == "qualified"
                after = native.policy_observation(session,view)
                assert after["selected_outcomes"] and not after["review_pass"]
                assert all(not row["independently_eligible"] for row in after["selected_outcomes"])
                assert policy.decide(after)["outcome"] not in {"accepted","partial"}
            assert execution.records(session) == records
