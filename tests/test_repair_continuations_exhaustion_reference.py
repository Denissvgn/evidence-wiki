"""Two material but irreparable native revisions leave the original unresolved."""

import copy

from evidence_wiki import Workspace
from evidence_wiki import execution_session as native
from tests._repair_continuation_fixture import change_child_output, configured


def test_two_material_but_irreparable_revisions_end_with_original_unresolved(tmp_path, monkeypatch):
    root, policy, execution, context = configured(tmp_path, monkeypatch)
    change_child_output(execution, "target['text']=d['repair_scope']['before_inventory']['claims'][0]['text']+' Still always unfixable.'")
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="irreparable", execution=execution) as session:
            final = session.run_until_terminal(request_id="repair", expected_basis_id=session.current()["basis_id"], max_steps=64)
            assert final["terminal"] and final["outcome"] == "abstained", final
            records = native.retained(session)
            attempts = [r["data"] for r in records if r["kind"] == "execution_attempt" and r["data"]["purpose"] == "repair"]
            assert len(attempts) == 2
            progress = session.repair_progress()
            assert progress["repairs_used"] == 2
            assert all(r["historical_effect_recorded"] and r["material_change"] == "changed" for r in progress["rows"])
            assert not any(r["current_review"]["native_accepted"] for r in progress["rows"])
            assert final["decision"]["unresolved_count"] == 1
            assert session.current()["accounting"]["original_ids"] == ["q"]
            before = copy.deepcopy(records)
            replay = session.advance(request_id=final["request_id"], expected_basis_id=final["before_basis_id"])
            assert replay["historical"] and native.retained(session) == before
