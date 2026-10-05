"""Scientific repair uses native arithmetic and explicit simulated host meters."""

from types import SimpleNamespace

from evidence_wiki import Workspace
from evidence_wiki import execution_session as native
from evidence_wiki.pack_discovery import owner
from tests import test_calculation_execution_fixture as reference


def test_quantitative_repair_preserves_native_value_peer_and_requires_new_reviews(tmp_path, monkeypatch):
    root, policy, execution, context = reference.fixture.__wrapped__(tmp_path, monkeypatch,
        SimpleNamespace(param={"calculation": True, "calculated_generation": True,
            "role_isolation": True, "simulated_model_usage": True, "disagreement": True, "repair": True}))
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="scientific-repair", execution=execution) as session:
            final = session.run_until_terminal(request_id="repair", expected_basis_id=session.current()["basis_id"], max_steps=64)
            assert final["outcome"] == "accepted", final
            records = native.retained(session)
            attempt = next(row["data"] for row in records if row["kind"] == "execution_attempt" and row["data"]["purpose"] == "repair")
            application = next(row["data"] for row in records if row["kind"] == "execution_application"
                and row["data"]["attempt_id"] == attempt["attempt"]["attempt_id"])
            before = attempt["repair_parent_inventory"]
            delivery = [row["data"] for row in records if row["kind"] == "execution_input_handoff"
                and row["data"]["attempt_id"] == attempt["attempt"]["attempt_id"]]
            assert len(delivery) == 1 and delivery[0]["input_sha256"] == attempt["attempt"]["input_sha256"]
            after = application["proposal"]["inventory"]
            assert before["schema_version"] == after["schema_version"] == "evidence-strict-claims/v2"
            target = attempt["target_claim_id"]
            assert target == "derived"
            assert [c for c in before["claims"] if c["id"] != target] == [c for c in after["claims"] if c["id"] != target]
            current = owner("strict_evidence").prepare_review(root, target)
            assert current["snapshot"]["computation"]["graphs"]["worksheet"]["outputs"]["total"]["value"] == "6"
            assert current["snapshot"]["claim"]["calculations"][0]["expected"] == "6"
            progress = session.repair_progress()
            row = progress["rows"][0]
            assert row["material_change"] == "changed" and row["diff"]["changes"]
            assert row["fresh_independent_reviews"] == "complete" and row["current_review"]["native_accepted"]
            assert row["historical_effect_recorded"]
            assert len([r for r in records if r["kind"] == "execution_applied" and r["data"].get("operation") == "evaluate"
                and r["data"].get("claim_id") == target]) == 4
            replay = session.advance(request_id=final["request_id"], expected_basis_id=final["before_basis_id"])
            assert replay["historical"] and native.retained(session) == records
