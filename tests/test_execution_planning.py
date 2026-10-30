"""Actual configured planner dispatch and inert authenticated application."""


from types import SimpleNamespace

import pytest

from evidence_wiki import Workspace
from evidence_wiki import execution_session as native
from tests.test_execution_session import fixture as fixture


@pytest.mark.parametrize('operation', ['prepare', 'generate'])
def test_calibrated_refresh_bootstrap_keeps_native_owner_action(monkeypatch, operation):
    from evidence_wiki import execution_planning, refresh_session
    session = SimpleNamespace(binding={'original_ids': ['q']},
        execution=SimpleNamespace(profile=SimpleNamespace(calibration=object())))
    records = [{'kind': 'execution_cycle', 'data': {'planner_config_ref': 'planner'}},
        {'kind': 'execution_refresh', 'data': {'request_id': 'refresh',
            'parent_usage_unknown': False, 'arguments': {'source_updates': []}}}]
    view = {'qualification': {'state': 'unknown'}}
    monkeypatch.setattr(execution_planning, 'configured', lambda *_: None)
    if operation == 'generate':
        records.extend([{'kind': 'execution_refresh_prepared', 'data': {'marker_sha256': 'marker'}},
            {'kind': 'execution_prepared', 'data': {'original_id': 'q'}}])
        monkeypatch.setattr(refresh_session, 'sha', lambda _: 'marker')
        monkeypatch.setattr(refresh_session, 'owner', lambda _: SimpleNamespace(
            capture_workspace=lambda _: SimpleNamespace(files={})))
        session.root = 'root'
    action = refresh_session.next_action(session, view, records)
    assert action['operation'] == operation
    assert execution_planning.proposed_action(session, view, records, action) is action
    monkeypatch.setattr(native, '_terminal_action', lambda *_: {'operation': 'terminal'})
    forged = dict(action, original_id='another-original')
    assert execution_planning.proposed_action(session, view, records, forged)['operation'] == 'terminal'
    assert execution_planning.proposed_action(session, view,
        [row for row in records if row['kind'] != 'execution_refresh'], action)['operation'] == 'terminal'
    instruction = next(row['data'] for row in records if row['kind'] == 'execution_refresh')
    instruction['parent_usage_unknown'] = True
    assert execution_planning.proposed_action(session, view, records, action)['operation'] == 'terminal'
    instruction['parent_usage_unknown'] = False
    if operation == 'prepare':
        assert execution_planning.proposed_action(session, view, records,
            dict(action, refresh_request_id='forged-refresh'))['operation'] == 'terminal'
    else:
        monkeypatch.setattr(refresh_session, 'sha', lambda _: 'changed-marker')
        assert execution_planning.proposed_action(session, view, records, action)['operation'] == 'terminal'
        monkeypatch.setattr(refresh_session, 'sha', lambda _: 'marker')
        records.append({'kind': 'execution_applied', 'data': {'original_id': 'q', 'operation': 'generate'}})
        assert execution_planning.proposed_action(session, view, records, action)['operation'] == 'terminal'
        records.pop()
    for refused in ['evaluate', 'repair', 'adjudicate']:
        assert execution_planning.proposed_action(session, view, records,
            {'operation': refused, 'original_id': 'q'})['operation'] == 'terminal'
    session.execution.profile.calibration = None
    assert execution_planning.proposed_action(session, view, records, action)['operation'] == 'terminal'


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
