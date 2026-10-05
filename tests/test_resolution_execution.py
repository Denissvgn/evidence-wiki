"""Installed native resolution with reference processes and simulated model meters."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from evidence_wiki import Workspace
from evidence_wiki import execution_session as native
from evidence_wiki.resolution_contracts import checked
from tests.test_execution_session import fixture


def selected(case):
    root, policy, execution, context = case
    context = replace(context, grants=context.grants | {"research_session.resolution", "resolution_operations.current"})
    return root, policy, execution, context


@pytest.mark.parametrize("derived,allow_partial,expected", [(False, True, "accept"), (True, True, "partial"), (True, False, "abstain")])
def test_native_current_resolution_preserves_partial_original_accounting_without_effects(tmp_path, monkeypatch, derived, allow_partial, expected):
    case = fixture.__wrapped__(tmp_path, monkeypatch, SimpleNamespace(param={
        "role_isolation": True, "simulated_model_usage": True,
        "derived_partial": derived, "allow_partial": allow_partial}))
    root, policy, execution, context = selected(case)
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="resolution", execution=execution) as session:
            assert native.machine_compatible(session), "Declared unresolved work still requires a known native coverage contract."
            initial = session.current()
            terminal = session.run_until_terminal(request_id="drive", expected_basis_id=initial["basis_id"], max_steps=64)
            assert terminal["terminal"]
            if not derived:
                assert terminal["outcome"] == "accepted", terminal
            before = native.retained(session)
            for name in ("authenticate", "invoke", "poll", "cancel"):
                monkeypatch.setattr(execution.executor, name, lambda *_a, **_k: pytest.fail("Resolution called the external executor."))
            result = session.resolution()
            assert checked(result) == result
            assert result["verdict"] == expected, (result, terminal)
            assert result["lifecycle"]["terminal"] and not result["authorized"] and not result["external_action_authorized"]
            assert [row["original_id"] for row in result["original_outcomes"]] == ["q"]
            outcome = result["original_outcomes"][0]
            if derived:
                assert [row["question_id"] for row in outcome["questions"]] == ["q", "refinement"]
                assert outcome["questions"][1]["outcome"] == "abstain"
                assert session.current()["accounting"]["accepted_count"] == 0
            if expected in {"accept", "partial"}:
                claim = outcome["eligible_questions"][0]["claims"][0]
                assert claim["text"] == "Observed reflectance was 0.74." and "verification" not in claim
                assert result["current_basis_id"] is not None
                assert outcome["questions"][0]["current_checks"]["required_reviews"] == "current_owned_pass"
            else:
                assert not outcome["eligible_questions"] and result["current_basis_id"] is None
            assert session.resolution() == result
            assert native.retained(session) == before
