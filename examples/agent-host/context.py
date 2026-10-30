"""Two native bounded agent dispatches over public instruction projections."""

import hashlib
import json
import time
from datetime import datetime, timedelta, timezone

from evidence_wiki import AgentHostContext, BoundedSubprocessExecutor, ExecutionAttempt
from evidence_wiki.agent_execution import ExecutionError
from evidence_wiki.workflow_contracts import EXECUTION_OBSERVATION, JOB_V3


def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


def select_context(executor, *, task='research', route='current-caller', role='host',
                   objective, capabilities, profile='autonomous', max_context_bytes=32768):
    """Fresh projected inputs, independent controllers, one attempt and no retries."""
    started = time.monotonic()
    if type(executor) is not BoundedSubprocessExecutor or executor.profile.role_scopes is None:
        raise ValueError('Select a bounded native executor with Planner and critic scopes.')
    registry = executor.profile.role_scopes
    planner = registry.select('planner'); critic = registry.select('critic')
    selection = AgentHostContext(task=task,route=route,role=role,objective=objective,capabilities=capabilities,
        planner_ref=executor.profile.role(planner.config_ref).controller_ref,
        critic_ref=executor.profile.role(critic.config_ref).controller_ref,profile=profile,
        max_context_bytes=max_context_bytes)
    calls = []
    deadline = datetime.now(timezone.utc)+timedelta(seconds=60)

    def invoke(scope, packet):
        selected = executor.profile.role(scope.config_ref)
        instant = datetime.now(timezone.utc)
        seconds = min(30,int((deadline-instant).total_seconds()))
        if seconds < 1:
            raise ValueError('Context dispatch budget exhausted.')
        identifier = 'context-'+scope.task_role
        operation = 'generate' if scope.task_role=='planner' else 'evaluate'
        job = {'schema_version':JOB_V3,'id':identifier,'operation':operation,'role':selected.role,
            'controller_ref':selected.controller_ref,'grant_ref':'context-model','input_id':identifier,
            'basis_id':packet['basis_id'],'policy_id':'context-selection','policy_version':'1',
            'prompt_version':selected.prompt_version,'revision_precondition':packet['basis_id'],
            'original_text':objective,'scope':['public-instructions'],'required_capabilities':['model'],
            'payload':{'question':objective} if operation=='generate' else {'claim_id':'context','packet_id':packet['basis_id']},
            'issued_utc':instant.isoformat(),'deadline_utc':deadline.isoformat(),'timeout_seconds':seconds,
            'max_attempts':1,'expected_result_schema':EXECUTION_OBSERVATION,'budgets':executor.profile.document()['job_budgets'],
            'qualifications':{'assurance':'artifact_checked','status':'unknown','evaluator_ref':None,
                'qualification_ref':None,'context_basis':None},'task_role':scope.task_role,
            'role_scope_sha256':scope.sha256,'context_id':identifier,
            'packet_sha256':hashlib.sha256(canonical(packet)).hexdigest()}
        # Use a fresh executor per selection; stable IDs never redispatch a retained attempt.
        attempt = ExecutionAttempt.create(attempt_id=identifier,nonce=identifier,run_id='context-selection',
            binding_id=packet['basis_id'],profile=executor.profile,config_ref=selected.config_ref,job=job,input_document=packet)
        record={'role':scope.task_role,'input_bytes':len(canonical(packet)),'state':'attempted',
            'usage':dict.fromkeys(('seconds','model_calls','tool_calls','tokens','cost')),'final_accounting':False}
        calls.append(record)
        try:
            observed = executor.invoke(attempt)
            while observed['state']=='running':
                if datetime.now(timezone.utc)>=deadline:
                    observed=executor.cancel(attempt);break
                time.sleep(0.01)
                observed = executor.poll(attempt)
            observed = executor.authenticate(observed,attempt)
        except ExecutionError:
            record['state']='refused'
            return None
        record.update(state=observed['state'],usage=observed['observed_usage'],final_accounting=observed['final_accounting'])
        if observed['state']!='succeeded':
            return None
        return observed['output']

    packet = selection.planner_input()
    proposal = invoke(planner,packet) if packet is not None else None
    packet = selection.critic_input(proposal)
    critique = invoke(critic,packet) if packet is not None else None
    result = selection.resolve(proposal,critique)
    return {'selection':result,'telemetry':{'scope':'context-host','calls':calls,'retries':0,
        'elapsed_seconds':time.monotonic()-started,'interventions':[],
        'operator_requests':0,'external_interventions':'unknown','semantic_qualification':'unknown'}}
