"""An actual approved alternate reviewer votes freshly before and after native repair."""

from evidence_wiki import Workspace
from evidence_wiki import execution_session as native
from tests._repair_continuation_fixture import configured


def test_actual_approved_alternate_is_fresh_native_vote_before_and_after_repair(tmp_path, monkeypatch):
    root, policy, execution, context = configured(tmp_path, monkeypatch)
    execution.executor._commands.pop("profile-a")  # Actual controlled no-start service loss.
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="alternate-repair", execution=execution) as session:
            final = session.run_until_terminal(request_id="repair", expected_basis_id=session.current()["basis_id"], max_steps=64)
            assert final["outcome"] == "accepted", final
            records = native.retained(session)
            votes = [r["data"] for r in records if r["kind"] == "execution_applied" and r["data"].get("operation") == "evaluate"]
            backup = [r for r in votes if r["service_slot_ref"] == "profile-a"]
            assert len(backup) == 2 and all(r["profile_ref"] == "profile-a-backup" for r in backup)
            assert all(r["service_identity"]["controller_ref"] == "backup-controller" for r in backup)
            assert len({r["strict_basis_id"] for r in backup}) == 2
            assert len({r["service_identity"]["qualification_ref"] for r in backup}) == 1
            progress = session.repair_progress()
            assert progress["rows"][0]["fresh_independent_reviews"] == "complete"
            assert progress["rows"][0]["current_review"]["native_accepted"]
            replay = session.advance(request_id=final["request_id"], expected_basis_id=final["before_basis_id"])
            assert replay["historical"] and native.retained(session) == records
