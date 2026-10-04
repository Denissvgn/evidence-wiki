"""Actual configured planner dispatch and inert authenticated application."""


from types import SimpleNamespace

from evidence_wiki import Workspace
from evidence_wiki import execution_session as native
from tests.test_execution_session import fixture as fixture


def test_actual_planner_dispatch_authentication_and_application_before_draft(tmp_path, monkeypatch):
    root,policy,execution,context=fixture.__wrapped__(tmp_path, monkeypatch, SimpleNamespace(param={"planner":True}))
    with context.activate(),Workspace.open(root) as workspace:
        session=workspace.research.session(policy,run_id="planning-run",execution=execution)
        with native.gate(session):
            before=session.current()["basis_id"]
            native._begin(session)
            native.append(session,"execution_cycle","drive",{"before_basis_id":before,"max_steps":12,"planner_config_ref":"planner"})
        seen=[]
        for index in range(12):
            step=session.advance(request_id="step-"+str(index),expected_basis_id=session.current()["basis_id"])
            seen.append(step["action"])
            records=execution.records(session)
            selections=[row for row in records if row["kind"]=="execution_planner_selection"]
            if selections:
                break
            assert not step["terminal"],step
        assert selections,seen
        attempts=[row["data"] for row in records if row["kind"]=="execution_attempt" and row["data"].get("purpose")=="plan"]
        assert len(attempts)==1 and attempts[0]["attempt"]["job"]["task_role"]=="planner"
        assert "plan.propose" in execution.profile.role_scopes.scope("planner").capabilities
        assert selections[0]["data"]["selection"]["selected"]["operation"]=="generate"
        assert not selections[0]["data"]["selection"]["authorized"]
        assert selections[0]["data"]["planner_semantic_qualification"]=="unknown"
        assert session.current()["usage"]["tool_calls"]>=1


def test_public_cycle_uses_scoped_planner_and_native_release(tmp_path,monkeypatch):
    root,policy,execution,context=fixture.__wrapped__(tmp_path,monkeypatch,SimpleNamespace(param={"planner":True}))
    from evidence_wiki.host_context import HostContext
    selected=HostContext([root],context.grants|{"research_session.run_cycle"},context.actor_ref,[root],
        authority_file=context.authority_file,state_directory=context.state_directory,
        mutation_control_directory=context.mutation_control_directory,controller_refs=context.controller_refs,
        role_controller_refs=context.role_controller_refs)
    with selected.activate(),Workspace.open(root) as workspace:
        result=workspace.research.run_cycle(policy,run_id="planned-cycle",execution=execution,request_id="drive",planner_config_ref="planner")
        assert result["terminal"] and result["outcome"]=="accepted",result
        assert result["original_outcomes"][0]["native_accepted"]
        with workspace.research.session(policy,run_id="planned-cycle",execution=execution) as session:
            records=execution.records(session)
            plans=[record["data"] for record in records if record["kind"]=="execution_planner_selection"]
            assert plans and all(row["planner_semantic_qualification"]=="unknown" for row in plans)
            assert all(row["selection"]["authorized"] is False for row in plans)
        replay=workspace.research.run_cycle(policy,run_id="planned-cycle",execution=execution,request_id="drive",planner_config_ref="planner")
        assert replay["historical"] and replay["outcome"]==result["outcome"]
