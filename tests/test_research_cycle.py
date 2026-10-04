"""Actual canonical terminal bound and immutable host drive replay."""

import json
import shutil
from types import SimpleNamespace

import pytest

from evidence_wiki import Workspace
from evidence_wiki.host_context import HostContext
from evidence_wiki.pack_discovery import owner
from evidence_wiki.workflow_contracts import WorkflowContractError
from tests.test_execution_session import fixture as fixture


def selected(fixture):
    root,policy,execution,context=fixture
    return HostContext([root],context.grants|{"research_session.run_cycle"},context.actor_ref,[root],
        authority_file=context.authority_file,state_directory=context.state_directory,
        mutation_control_directory=context.mutation_control_directory,controller_refs=context.controller_refs,
        role_controller_refs=context.role_controller_refs)


def test_one_host_call_bound_is_canonical_terminal_and_default_basis_replay(fixture):
    root,policy,execution,_context=fixture
    context=selected(fixture)
    with context.activate(),Workspace.open(root) as workspace:
        first=workspace.research.run_cycle(policy,run_id="cycle-run",execution=execution,request_id="drive",max_steps=1)
        assert first["terminal"] and first["outcome"]=="stop_budget",first
        assert first["canonical_run_state"] in owner("run_controller").TERMINAL_STATES
        assert first["original_outcomes"][0]["attempted"]
        assert first["original_outcomes"][0]["disposition"]=="blocked_current"
        assert not first["evidence_accepted"]
        replay=workspace.research.run_cycle(policy,run_id="cycle-run",execution=execution,request_id="drive",max_steps=1)
        assert replay["historical"] and replay["initial_basis_id"]==first["initial_basis_id"]
        assert replay["outcome"]==first["outcome"]
        with pytest.raises(WorkflowContractError) as caught:
            workspace.research.run_cycle(policy,run_id="cycle-run",execution=execution,request_id="drive",max_steps=2)
        assert caught.value.error_code=="RESEARCH_REQUEST_CONFLICT"


def test_bound_step_written_before_finish_resumes_finish_and_close(fixture,monkeypatch):
    from evidence_wiki import execution_session as native
    root,policy,execution,_context=fixture
    context=selected(fixture)
    original=native._finish
    crashed=[]
    def failure(session,decision):
        if not crashed:
            crashed.append(True)
            raise RuntimeError("Interrupted final transition")
        return original(session,decision)
    with context.activate(),Workspace.open(root) as workspace:
        monkeypatch.setattr(native,"_finish",failure)
        with pytest.raises(RuntimeError,match="Interrupted"):
            workspace.research.run_cycle(policy,run_id="cycle-run",execution=execution,request_id="drive",max_steps=1)
        result=workspace.research.run_cycle(policy,run_id="cycle-run",execution=execution,request_id="drive",max_steps=1)
        assert result["terminal"] and result["canonical_run_state"] in owner("run_controller").TERMINAL_STATES
        assert result["historical"]


def test_original_dispositions_preserve_native_status_and_actual_attempts():
    from evidence_wiki.research_cycle import _originals

    def original(identifier, status="open", accepted=False, missing=None):
        return {"original_id":identifier,"original_text":"Exact "+identifier, "accepted":accepted,
            "questions":[{"status":status}],"missing_question_ids":missing or []}
    rows=[original("accepted",accepted=True),original("rejected",status="rejected"),
        original("deferred",status="deferred"),original("untouched"),original("prepared"),
        original("missing",accepted=True,missing=["derived"])]
    records=[{"kind":"execution_prepared","data":{"original_id":"prepared"}},
        {"kind":"execution_attempt","data":{"original_id":"missing"}}]
    result=_originals({"accounting":{"original_outcomes":rows}},records,terminal=True)
    assert [row["disposition"] for row in result]==["accepted_current","rejected_native","abstained_native",
        "not_attempted","blocked_current","blocked_current"]
    assert [row["attempted"] for row in result]==[False,False,False,False,True,True]
    assert not result[-1]["native_accepted"] and result[-1]["missing_question_ids"]==["derived"]
    pending=_originals({"accounting":{"original_outcomes":[rows[4]]}},records,terminal=False)
    assert pending[0]["disposition"]=="pending"


def test_live_managed_session_refuses_caller_cycle_before_run_effects(fixture):
    root,policy,execution,_context=fixture
    directory=root/"runs/orchestrations/managed"
    directory.mkdir(parents=True)
    (directory/"session.json").write_text(json.dumps({"status":"active","orchestration_id":"managed","pending_action_id":None}))
    context=selected(fixture)
    with context.activate(),Workspace.open(root) as workspace:
        with pytest.raises(WorkflowContractError) as caught:
            workspace.research.run_cycle(policy,run_id="caller-cycle",execution=execution,request_id="drive",max_steps=1)
        assert caught.value.error_code=="RESEARCH_MANAGED_RUN_OWNER_REQUIRED"
        assert not owner("run_controller").run_state_path(root,"caller-cycle").exists()


def test_public_cycle_finishes_reference_journey_and_replays_native_release(fixture):
    root,policy,execution,_context=fixture
    context=selected(fixture)
    with context.activate(),Workspace.open(root) as workspace:
        first=workspace.research.run_cycle(policy,run_id="complete-cycle",execution=execution,request_id="drive",max_steps=64)
        assert first["terminal"] and first["outcome"]=="accepted",first
        assert first["canonical_run_state"]=="complete"
        assert first["original_outcomes"][0]["disposition"]=="accepted_current"
        assert first["original_outcomes"][0]["attempted"] and first["accounting_complete"]
        assert first["current_release"]["research_complete"]
        assert not first["evidence_accepted"]
        replay=workspace.research.run_cycle(policy,run_id="complete-cycle",execution=execution,request_id="drive",max_steps=64)
        assert replay["historical"] and replay["outcome"]=="accepted"
        assert replay["initial_basis_id"]==first["initial_basis_id"]


def test_cycle_missing_qualification_stops_without_model_attempt(fixture,monkeypatch):
    root,policy,execution,_context=fixture
    context=selected(fixture)
    def unavailable(*_args,**_kwargs):
        raise ValueError("Qualification unavailable")
    monkeypatch.setattr(type(execution.qualifications),"observations",unavailable)
    with context.activate(),Workspace.open(root) as workspace:
        result=workspace.research.run_cycle(policy,run_id="unqualified-cycle",execution=execution,request_id="drive")
        assert result["terminal"] and result["reason"]=="qualification_unknown"
        assert result["original_outcomes"][0]["disposition"]=="not_attempted"
        with workspace.research.session(policy,run_id="unqualified-cycle",execution=execution) as session:
            assert not any(row["kind"]=="execution_attempt" for row in execution.records(session))


def test_cycle_empty_authorized_evidence_abstains_without_dispatch(fixture):
    root,policy,execution,_context=fixture
    context=selected(fixture)
    shutil.rmtree(root/"raw")
    (root/"raw").mkdir()
    for path in (root/"sources/normalized").iterdir():
        if path.is_file():
            path.unlink()
    with context.activate(),Workspace.open(root) as workspace:
        result=workspace.research.run_cycle(policy,run_id="empty-cycle",execution=execution,request_id="drive")
        assert result["terminal"] and result["outcome"]=="abstained"
        assert result["reason"]=="insufficient_evidence"
        assert result["original_outcomes"][0]["disposition"]=="not_attempted"


def test_cycle_partial_accounts_for_unattempted_original(tmp_path,monkeypatch):
    case=fixture.__wrapped__(tmp_path,monkeypatch,SimpleNamespace(param={"partial":True}))
    root,policy,execution,_context=case
    context=selected(case)
    with context.activate(),Workspace.open(root) as workspace:
        result=workspace.research.run_cycle(policy,run_id="partial-cycle",execution=execution,request_id="drive")
        assert result["terminal"] and result["outcome"]=="partial",result
        assert [row["original_id"] for row in result["original_outcomes"]]==["q","unavailable"]
        assert result["original_outcomes"][0]["native_accepted"]
        assert result["original_outcomes"][1]["disposition"]=="not_attempted"
        assert not result["current_release"]["research_complete"]
