"""Public finite termination without promoting old or out-of-domain judgments."""

from dataclasses import replace
from types import SimpleNamespace

from evidence_wiki import ResearchExecution, Workspace
from evidence_wiki.calibration_registry import CalibratedEvaluators, CalibrationPlan, CalibrationSelection
from evidence_wiki.execution_roles import ExecutionRoleRegistry, ExecutionRoleScope
from evidence_wiki.finite_domain import DOMAIN
from evidence_wiki.pack_discovery import owner
from evidence_wiki.qualification_calibration import CalibrationAuthority, calibration_suite
from evidence_wiki.reference_oracles import protected_cases
from tests.test_execution_session import fixture


def test_public_unqualified_domain_can_propose_but_cannot_review_or_release(tmp_path, monkeypatch):
    root, policy, old_execution, context = fixture.__wrapped__(tmp_path, monkeypatch, SimpleNamespace(param={}))
    rubric = owner("verify_quotes").load_config(root)["strict_evidence"]["rubric"]
    suite = calibration_suite(protected_cases(), suite_ref="finite-protected", split_ref="held-out",
        domains=[DOMAIN], rubric=rubric, model_settings={"temperature": "0"})
    selected = CalibrationSelection("profile-a", suite, model_revision="explicit-model-revision")
    profile = replace(old_execution.profile, role_scopes=ExecutionRoleRegistry((
        ExecutionRoleScope("researcher", "researcher", ("model",)),
        ExecutionRoleScope("profile-a", "evaluator", ("model",)))), calibration=CalibrationPlan((selected,)))
    old_execution.executor.profile = profile
    execution = ResearchExecution(old_execution.executor,
        qualifications=CalibratedEvaluators([], CalibrationAuthority("calibration-issuer", b"c" * 32, "runner")),
        review_signer=old_execution.review_signer, observer_key=b"n" * 32)
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="unqualified-domain", execution=execution) as session:
            basis = session.current()["basis_id"]
            result = session.run_until_terminal(request_id="drive", expected_basis_id=basis, max_steps=64)
            assert result["terminal"] and result["outcome"] == "blocked_external"
            assert result["reason"] == "qualification_unknown"
            records = execution.records(session)
            attempts = [row["data"]["attempt"] for row in records if row["kind"] == "execution_attempt"]
            assert attempts and all(attempt["job"]["operation"] == "generate" for attempt in attempts)
            assert session.current()["accounting"]["accepted_count"] == 0
            assert any(row["kind"] == "execution_application" for row in records)
            count = len(records)
            replay = session.run_until_terminal(request_id="drive", expected_basis_id=basis, max_steps=64)
            assert replay["historical"] and replay["outcome"] == "blocked_external"
            assert len(execution.records(session)) == count
