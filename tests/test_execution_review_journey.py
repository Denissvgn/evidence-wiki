"""Actual separately configured reviewer dispatch and native attestations."""

import json
from types import SimpleNamespace

from evidence_wiki import ReviewContext, Workspace, isolation_report
from tests.test_execution_session import fixture


def test_public_review_controllers_receive_distinct_blinded_contexts(tmp_path, monkeypatch):
    root, policy, execution, context = fixture.__wrapped__(tmp_path, monkeypatch,
        SimpleNamespace(param={"role_isolation": True}))
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="independent-reviews", execution=execution) as session:
            basis = session.current()["basis_id"]
            result = session.run_until_terminal(request_id="drive", expected_basis_id=basis, max_steps=64)
            assert result["terminal"] and result["outcome"] == "accepted", (result, session.report())
            records = execution.records(session)
            attempts = [row["data"] for row in records if row["kind"] == "execution_attempt"]
            reviews = [row for row in attempts if row["attempt"]["job"]["operation"] == "evaluate"]
            assert [row["attempt"]["config_ref"] for row in reviews] == ["profile-a", "profile-b"]
            assert {row["attempt"]["job"]["task_role"] for row in reviews} == {"evaluator", "critic"}
            assert len({row["attempt"]["job"]["context_id"] for row in reviews}) == 2
            assert len({row["attempt"]["input_sha256"] for row in reviews}) == 2
            contexts = []
            for row in reviews:
                job, data = row["attempt"]["job"], row["attempt"]["input"]
                assert job["schema_version"] == "research-host-job/v3" and job["payload"]["claim_id"] == "claim-1"
                assert set(data) == {"packet", "instructions", "output_contract"}
                rendered = json.dumps(data)
                for excluded in ("original_accounting", "generator_controller", "registration", "expected_label", "prior_verdict", "order_seed"):
                    assert excluded not in rendered
                assert data["packet"]["manifest"]["context_complete"]
                contexts.append(ReviewContext.restore(data["packet"], row["review_context"]))
            applied = [row["data"] for row in records if row["kind"] == "execution_applied" and row["data"]["operation"] == "evaluate"]
            assert {row["profile_ref"] for row in applied} == {"profile-a", "profile-b"}
            assert all(row["passed"] for row in applied)
            observed = [row["data"]["observation"] for row in records if row["kind"] == "execution_observation"]
            isolation = isolation_report(execution.profile.roles, authenticated_observations=observed,
                contexts=contexts, generator_controller_ref="runner")
            assert isolation["control"]["authenticated_controller_refs_distinct"]
            assert isolation["context"]["distinct_context_ids"]
            assert isolation["context"]["host_session_isolation"] == "unknown"
            assert isolation["model"]["live_model_execution"] == "unknown"
            assert not isolation["authorized"] and not isolation["evidence_accepted"]
            replay = session.run_until_terminal(request_id="drive", expected_basis_id=basis, max_steps=64)
            assert replay["historical"] and replay["outcome"] == "accepted"
            assert len([row for row in execution.records(session) if row["kind"] == "execution_attempt"]) == len(attempts)
