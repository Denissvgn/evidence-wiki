"""Native repair refuses an authenticated wrong source or computational unit."""

import copy

import pytest

from evidence_wiki import Workspace
from evidence_wiki import execution_session as native
from evidence_wiki.pack_discovery import owner
from tests._repair_continuation_fixture import change_child_output, configured


@pytest.mark.parametrize("defect", ["source", "units"])
def test_native_refuses_authenticated_wrong_source_or_computational_unit_repair(tmp_path, monkeypatch, defect):
    root, policy, execution, context = configured(tmp_path, monkeypatch, calculated=defect == "units")
    change_child_output(execution, "target['evidence'][0]['source_id']='unavailable-source'" if defect == "source" else "target['calculations'][0]['unit']='kelvin'")
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="invalid-" + defect, execution=execution) as session:
            initial = session.current()
            final = session.run_until_terminal(request_id="repair", expected_basis_id=initial["basis_id"], max_steps=64)
            assert final["terminal"] and final["outcome"] in {"abstained", "rejected"}, final
            records = native.retained(session)
            attempts = [r["data"] for r in records if r["kind"] == "execution_attempt" and r["data"]["purpose"] == "repair"]
            assert len(attempts) == 1
            identifier = attempts[0]["attempt"]["attempt_id"]
            assert any(r["kind"] == "execution_observation" and r["data"]["attempt_id"] == identifier
                and r["data"]["observation"]["state"] == "succeeded" and r["data"]["observation"]["final_accounting"] for r in records)
            assert not any(r["kind"] == "execution_applied" and r["data"]["attempt_id"] == identifier for r in records)
            target = attempts[0]["target_claim_id"]
            prepared = owner("strict_evidence").prepare_review(root, target)
            assert not next(r["accepted"] for r in prepared["result"]["claims"] if r["claim"]["id"] == target)
            if defect == "units":
                assert prepared["snapshot"]["computation"]["graphs"]["worksheet"]["outputs"]["total"]["value"] == "6"
                assert prepared["snapshot"]["claim"]["calculations"][0]["unit"] == "units"
            assert final["decision"]["unresolved_count"] == 1
            assert session.current()["accounting"]["original_ids"] == ["q"]
            before = copy.deepcopy(records)
            replay = session.advance(request_id=final["request_id"], expected_basis_id=final["before_basis_id"])
            assert replay["historical"] and native.retained(session) == before
