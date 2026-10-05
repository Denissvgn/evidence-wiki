"""Native scoped repair protocol uses explicit simulation meters, not live models."""

from types import SimpleNamespace

import pytest

from evidence_wiki import Workspace
from tests import test_execution_session as reference


@pytest.fixture
def configured(tmp_path, monkeypatch):
    return reference.fixture.__wrapped__(tmp_path, monkeypatch, SimpleNamespace(param={
        "role_isolation": True, "simulated_model_usage": True, "review_services": True,
        "disagreement": True, "repair": True, "service_repair": True}))


def test_scoped_repair_has_native_diff_fresh_reviews_and_exact_replay(configured):
    root, policy, execution, context = configured
    assert execution.profile.document()["schema_version"] == "research-execution-profile/v10"
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="scoped-repair", execution=execution) as session:
            final = session.run_until_terminal(request_id="repair", expected_basis_id=session.current()["basis_id"], max_steps=64)
            assert final["outcome"] == "accepted", final
            progress = session.repair_progress()
            assert progress["repairs_used"] == 1
            assert len(progress["rows"]) == 1
            row = progress["rows"][0]
            assert row["historical_effect_recorded"] and row["execution_correlation"] == "known"
            assert row["material_change"] == "changed"
            assert row["diff"]["changes"] and row["fresh_independent_reviews"] == "complete"
            assert row["current_review"]["native_accepted"]
            records = execution.records(session)
            replay = session.advance(request_id=final["request_id"], expected_basis_id=final["before_basis_id"])
            assert replay["historical"] and replay["outcome"] == "accepted"
            assert execution.records(session) == records
