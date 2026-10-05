"""Actual native repair continuations use explicitly simulated model meters."""

import copy
from types import SimpleNamespace

import pytest

from evidence_wiki import Workspace
from evidence_wiki import execution_session as native
from evidence_wiki.pack_discovery import owner
from tests import test_calculation_execution_fixture as scientific
from tests import test_execution_session as reference


def configured(tmp_path, monkeypatch, *, calculated=False):
    factory = scientific if calculated else reference
    options = {"role_isolation": True, "simulated_model_usage": True, "disagreement": True, "repair": True}
    options.update({"calculation": True, "calculated_generation": True} if calculated else {"review_services": True, "service_repair": True})
    return factory.fixture.__wrapped__(tmp_path, monkeypatch, SimpleNamespace(param=options))


def change_child_output(execution, changes):
    # Model outputs remain untrusted after qualification. This controlled child
    # produces a genuine owned observation; no policy, receipt or key is edited.
    command = execution.executor._commands["repairer"]
    wrapper = ("import json,subprocess,sys; incoming=sys.stdin.read(); d=json.loads(incoming)['input']; "
        "result=subprocess.run(" + repr(list(command)) + ",input=incoming,text=True,capture_output=True,check=True); "
        "output=json.loads(result.stdout); target=next(c for c in output['proposal']['inventory']['claims'] if c['id']==d['repair_scope']['target_claim_id']); "
        + changes + "; print(json.dumps(output))")
    execution.executor._commands["repairer"] = (*command[:-1], wrapper)


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
