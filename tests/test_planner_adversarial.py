"""Actual authenticated planner hostility, interruption and finite retry."""

import sys
from types import SimpleNamespace

import pytest

from evidence_wiki import Workspace
from evidence_wiki import execution_session as native
from tests.test_execution_session import fixture


def configured(tmp_path,monkeypatch):
    return fixture.__wrapped__(tmp_path,monkeypatch,SimpleNamespace(param={"planner":True}))


def begin(workspace,policy,execution):
    session=workspace.research.session(policy,run_id="planning-run",execution=execution)
    with native.gate(session):
        before=session.current()["basis_id"]
        native._begin(session)
        native.append(session,"execution_cycle","drive",{"before_basis_id":before,"max_steps":32,"planner_config_ref":"planner"})
    return session


def until(session,predicate,max_steps=24):
    seen=[]
    for index in range(max_steps):
        step=session.advance(request_id="step-"+str(index),expected_basis_id=session.current()["basis_id"])
        seen.append(step)
        if predicate(step,session.execution.records(session)):
            return step,seen
        if step["terminal"]:
            failures=[row["data"] for row in session.execution.records(session) if row["kind"] in {"execution_application_failed","execution_reservation_failed"}]
            pytest.fail(str({"terminal":step,"failures":failures}))
    pytest.fail("Bounded expected condition not reached")


def test_authenticated_arbitrary_tool_proposal_cannot_reach_native_admission(tmp_path,monkeypatch):
    root,policy,execution,context=configured(tmp_path,monkeypatch)
    execution.executor._commands["planner"]=(sys.executable,"-c","import json,sys; json.load(sys.stdin); print(json.dumps({'argv':['arbitrary-tool'],'authorized':True}))")
    with context.activate(),Workspace.open(root) as workspace:
        session=begin(workspace,policy,execution)
        result,seen=until(session,lambda step,records:step["terminal"])
        assert result["outcome"]!="accepted"
        records=execution.records(session)
        plans=[row["data"] for row in records if row["kind"]=="execution_attempt" and row["data"].get("purpose")=="plan"]
        assert len(plans)==1
        observed=[row["data"]["observation"] for row in records if row["kind"]=="execution_observation"]
        assert any(row["state"]=="succeeded" for row in observed)
        assert not any(row["kind"]=="execution_native" and row["data"]["operation"]=="strict.admit" for row in records)


def test_selection_persisted_before_applied_resumes_idempotently(tmp_path,monkeypatch):
    root,policy,execution,context=configured(tmp_path,monkeypatch)
    append=native.append
    interrupted=[]
    def record(session,kind,request_id,data):
        if kind=="execution_applied" and data.get("operation")=="plan" and not interrupted:
            interrupted.append(True)
            raise RuntimeError("Interrupted after selected candidate persistence")
        return append(session,kind,request_id,data)
    with context.activate(),Workspace.open(root) as workspace:
        session=begin(workspace,policy,execution)
        last=None
        for index in range(24):
            records=execution.records(session)
            work=session.current()["execution"]["pending"]
            if work and work["state"]=="succeeded" and not work["applied"]:
                attempt_id=work["attempt_id"]
                with monkeypatch.context() as fault:
                    fault.setattr(native,"append",record)
                    with pytest.raises(RuntimeError,match="Interrupted"):
                        native._apply(session,attempt_id)
                native._apply(session,attempt_id)
                break
            last=session.advance(request_id="step-"+str(index),expected_basis_id=session.current()["basis_id"])
            assert not last["terminal"],last
        else:pytest.fail("Planner completion not observed")
        records=execution.records(session)
        assert len([row for row in records if row["kind"]=="execution_planner_selection"])==1
        assert len([row for row in records if row["kind"]=="execution_applied" and row["data"].get("operation")=="plan"])==1


def test_actual_failed_planner_retries_same_owner_tuple_once_then_selects(tmp_path,monkeypatch):
    root,policy,execution,context=configured(tmp_path,monkeypatch)
    original=execution.executor._commands["planner"]
    count=tmp_path/"planner-count"
    program="import pathlib,sys; p=pathlib.Path("+repr(str(count))+"); n=int(p.read_text()) if p.exists() else 0; p.write_text(str(n+1)); sys.exit(1) if n==0 else None; exec("+repr(original[-1])+")"
    execution.executor._commands["planner"]=(sys.executable,"-c",program)
    with context.activate(),Workspace.open(root) as workspace:
        session=begin(workspace,policy,execution)
        result,seen=until(session,lambda step,records:any(row["kind"]=="execution_planner_selection" for row in records))
        records=execution.records(session)
        attempts=[row["data"] for row in records if row["kind"]=="execution_attempt" and row["data"].get("retry_purpose",row["data"].get("purpose"))=="plan"]
        assert len(attempts)==2 and count.read_text()=="2"
        assert attempts[0]["owner_action"]==attempts[1]["owner_action"]
        assert attempts[0]["selection_key"]==attempts[1]["selection_key"]
        assert attempts[1]["purpose"]=="retry" and attempts[1]["attempt"]["job"]["task_role"]=="planner"
        assert session.current()["usage"]["retry_attempts"]==1
