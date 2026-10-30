"""Actual protected reference controllers use explicitly simulated model meters."""

import json
from html import unescape
from types import SimpleNamespace

import pytest

from evidence_wiki import Workspace
from evidence_wiki import execution_session as native
from tests.test_execution_session import fixture


@pytest.mark.parametrize("derived,material", [(False, False), (True, False), (False, True)])
def test_native_qualified_readability_keeps_scope_and_obtains_current_brief(tmp_path, monkeypatch, derived, material):
    root, policy, execution, context = fixture.__wrapped__(tmp_path, monkeypatch, SimpleNamespace(param={
        "role_isolation": True, "simulated_model_usage": True, "disagreement": True,
        "readability": True, "readability_material": material, "derived_partial": derived, "allow_partial": True}))
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="readable", execution=execution) as session:
            before = session.current()
            terminal = session.run_until_terminal(request_id="drive", expected_basis_id=before["basis_id"], max_steps=64)
            assert terminal["terminal"], terminal
            if not derived:
                assert terminal["outcome"] == "accepted", terminal
            records = native.retained(session)
            applications = [row["data"] for row in records if row["kind"] == "execution_application"
                and row["data"].get("operation") == "readability"]
            assert applications and not applications[-1]["result"]["issues"], (applications, terminal)
            captured = json.loads((execution.executor._cwd / "readability-input.json").read_text())
            encoded = json.dumps(captured)
            assert "readability-controller" not in encoded and "runner-key" not in encoded
            assert "reviewer-secret" not in encoded and "current_owned_pass" not in encoded
            assert any(row["kind"] == "execution_input_handoff" and row["data"]["boundary"] == "native_readability_stdin_complete" for row in records)
            for operation in ("invoke", "invoke_readability_guarded", "qualification_configuration", "poll", "cancel", "authenticate"):
                monkeypatch.setattr(execution.executor, operation, lambda *_a, **_k: pytest.fail("Brief read invoked an external callback."))
            read = session.brief()
            artifact = session.brief(format="markdown")
            assert session.brief() == read and session.brief(format="markdown") == artifact
            assert read["readability"]["state"] == "observed", read
            assert read["readability"]["scope"] == "finite_scalar_preservation"
            assert "## Original question" in artifact["markdown"] and "#### Findings" in artifact["markdown"]
            assert native.retained(session) == records
            if derived:
                assert read["verdict"] == "partial" and session.current()["accounting"]["accepted_count"] == 0
                assert read["original_outcomes"][0]["questions"][1]["outcome"] == "abstain"
            if material:
                repairs = [row["data"] for row in records if row["kind"] == "execution_attempt" and row["data"].get("purpose") == "repair"]
                assert len(repairs) == 1 and repairs[0]["target_claim_id"] == "observation"
                assert "This is a single retained measurement." in read["original_outcomes"][0]["eligible_questions"][0]["claims"][0]["limitations"]
                assert "This is a single retained measurement." in unescape(artifact["markdown"])
                applied_reviews = [row["data"] for row in records if row["kind"] == "execution_applied" and row["data"].get("operation") == "evaluate"]
                assert len({row["strict_basis_id"] for row in applied_reviews}) >= 2


def test_hostile_readability_prose_cannot_be_applied_or_sign_a_review(tmp_path, monkeypatch):
    root, policy, execution, context = fixture.__wrapped__(tmp_path, monkeypatch, SimpleNamespace(param={
        "role_isolation": True, "simulated_model_usage": True, "disagreement": True,
        "readability": True, "readability_hostile": True}))
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="hostile", execution=execution) as session:
            before = session.current()
            result = session.run_until_terminal(request_id="drive", expected_basis_id=before["basis_id"], max_steps=64)
            assert result["terminal"] and result["outcome"] != "accepted", result
            records = native.retained(session)
            assert not any(row["kind"] == "execution_application" and row["data"].get("operation") == "readability" for row in records)
            assert not any(row["kind"] == "execution_application" and row["data"].get("profile_ref") == "readability" for row in records)
