"""Separate native review processes and a targeted repair share one owned run."""

from types import SimpleNamespace

from evidence_wiki import Workspace
from evidence_wiki import execution_session as driver
from tests.test_execution_session import fixture


def test_targeted_repair_requires_new_material_and_fresh_first_passes(tmp_path, monkeypatch):
    root, policy, execution, context = fixture.__wrapped__(tmp_path, monkeypatch,
        SimpleNamespace(param={"role_isolation": True, "repair": True, "disagreement": True}))
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="targeted-repair", execution=execution) as session:
            result = session.run_until_terminal(request_id="drive", expected_basis_id=session.current()["basis_id"], max_steps=64)
            assert result["terminal"] and result["outcome"] == "accepted", (result, session.report())
            records = driver.retained(session)
            repair_rows = [row["data"] for row in records if row["kind"] == "execution_attempt"
                and row["data"].get("purpose") == "repair"]
            assert len(repair_rows) == 1
            repair = repair_rows[0]
            claim_id = repair["target_claim_id"]
            evidence = repair["attempt"]["input"]["repair_scope"]["defect_evidence"]
            assert len(evidence["first_pass_findings"]) == 2
            applied = next(row["data"] for row in records if row["kind"] == "execution_applied"
                and row["data"]["attempt_id"] == repair["attempt"]["attempt_id"])
            assert applied["disagreement_result_basis"] != repair["disagreement"]["material_basis_id"]
            reviews = [row["data"] for row in records if row["kind"] == "execution_applied"
                and row["data"].get("operation") == "evaluate"]
            assert {row["claim_id"] for row in reviews} == {claim_id}
            assert sum(row["passed"] is False for row in reviews) == 2
            assert {row["profile_ref"] for row in reviews if row["passed"]} == {"profile-a", "profile-b"}
            assert session.current()["usage"]["repair_cycles"] == 1
