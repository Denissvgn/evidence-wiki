"""Native readability with a material repair, and hostile readability prose.

Each test runs a complete native research drive, so this module forms a suite group of its own.
"""

from types import SimpleNamespace

import pytest

from evidence_wiki import Workspace
from evidence_wiki import execution_session as native
from tests.test_execution_session import fixture
from tests.test_readability_execution import qualified_readability


@pytest.mark.parametrize("derived,material", [(False, True)])
def test_native_qualified_readability_keeps_scope_and_obtains_current_brief(tmp_path, monkeypatch, derived, material):
    qualified_readability(tmp_path, monkeypatch, derived, material)


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
