"""Bounded Documentation-agent assembly, independent critic and inert repair loop."""

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from evidence_wiki import (
    BoundedSubprocessExecutor,
    ExecutionAttempt,
    ExecutionProfile,
    ExecutionRole,
    ExecutionRoleRegistry,
    ExecutionRoleScope,
    IntegrationDocumentation,
    Onboarding,
)
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.workflow_contracts import EXECUTION_OBSERVATION, JOB_V3


def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


def scripted_executor(root):
    roles=(ExecutionRole('documentation','host_worker','documentation-controller','simulated/documentation','derive/v1'),
        ExecutionRole('documentation-critic','evaluator','critic-controller','simulated/documentation-critic','critic/v1'))
    scopes=ExecutionRoleRegistry((ExecutionRoleScope('documentation','integration',('model','public_contract.read')),
        ExecutionRoleScope('documentation-critic','critic',('model',))))
    profile=ExecutionProfile('scripted-documentation',60,'EUR','unknown/v1',roles,1,1,16000,'0',1,role_scopes=scopes)
    worker=str(Path(__file__).with_name('documentation_worker.py').resolve())
    return BoundedSubprocessExecutor(profile,commands={row.config_ref:[sys.executable,worker] for row in roles},cwd=root)


def pipeline(executor, *, scenario='ready'):
    if type(executor) is not BoundedSubprocessExecutor or executor.profile.role_scopes is None:
        raise ValueError('Select a native bounded executor with Documentation integration and independent critic scopes.')
    registry=executor.profile.role_scopes
    writer=registry.select('integration');critic=registry.select('critic')
    if executor.profile.role(writer.config_ref).controller_ref==executor.profile.role(critic.config_ref).controller_ref:
        raise ValueError('Documentation and critic controllers must be distinct.')
    document=IntegrationDocumentation();started=time.monotonic();calls=[];repairs=[]
    deadline=datetime.now(timezone.utc)+timedelta(seconds=60)

    def call(scope,packet,index):
        instant=datetime.now(timezone.utc);seconds=min(15,int((deadline-instant).total_seconds()))
        if seconds<1:
            return None
        role=executor.profile.role(scope.config_ref);identifier='documentation-'+str(index)+'-'+scope.task_role
        operation='generate' if scope.task_role=='integration' else 'evaluate'
        job={'schema_version':JOB_V3,'id':identifier,'operation':operation,'role':role.role,
            'controller_ref':role.controller_ref,'grant_ref':'public-doc-model','input_id':identifier,
            'basis_id':packet['basis_id'],'policy_id':'public-documentation','policy_version':'1',
            'prompt_version':role.prompt_version,'revision_precondition':packet['basis_id'],
            'original_text':'Derive integration material from the exact public contract.','scope':['public-contract'],
            'required_capabilities':['model','public_contract.read'] if scope.task_role=='integration' else ['model'],
            'payload':{'question':'Derive integration material from the exact public contract.'} if operation=='generate' else {'claim_id':'documentation','packet_id':packet['basis_id']},
            'issued_utc':instant.isoformat(),'deadline_utc':deadline.isoformat(),'timeout_seconds':seconds,'max_attempts':1,
            'expected_result_schema':EXECUTION_OBSERVATION,'budgets':executor.profile.document()['job_budgets'],
            'qualifications':{'assurance':'artifact_checked','status':'unknown','evaluator_ref':None,'qualification_ref':None,'context_basis':None},
            'task_role':scope.task_role,'role_scope_sha256':scope.sha256,'context_id':identifier,
            'packet_sha256':hashlib.sha256(canonical(packet)).hexdigest()}
        trace={'role':'documentation' if scope.task_role=='integration' else 'critic','controller_ref':role.controller_ref,
            'input_bytes':len(canonical(packet)),'state':'attempted','usage':None,'final_accounting':False}
        calls.append(trace)
        try:
            attempt=ExecutionAttempt.create(attempt_id=identifier,nonce=identifier,run_id='documentation',binding_id=packet['basis_id'],
                profile=executor.profile,config_ref=scope.config_ref,job=job,input_document=packet)
            observed=executor.invoke(attempt)
            while observed['state']=='running':
                if datetime.now(timezone.utc)>=deadline:
                    observed=executor.cancel(attempt);break
                time.sleep(0.01);observed=executor.poll(attempt)
            observed=executor.authenticate(observed,attempt)
            trace.update(state=observed['state'],usage=observed['observed_usage'],final_accounting=observed['final_accounting'])
            return observed['output'] if observed['state']=='succeeded' else None
        except EvidenceWikiError:
            trace['state']='refused';return None

    for index in (1,2):
        packet=document.agent_input();packet['scenario']=scenario;packet['attempt']=index
        if repairs:
            packet['repair']=repairs[-1]
        proposal=call(writer,packet,index)
        packet=document.agent_input(purpose='critic',proposal=proposal)
        critique=call(critic,packet,index)
        result=document.resolve(proposal,critique)
        if result['status']=='ready':
            break
        failure='integration_failure' if any(row['state']!='succeeded' for row in calls[-2:]) else 'documentation_mismatch'
        repairs.append(document.repair(failure_kind=failure,attempt=index,basis_id=result['basis_id']))
    return {'schema_version':'documentation-host-result/v1','result':result,'repairs':repairs,
        'telemetry':{'scope':'documentation-host','calls':calls,'retries':max(0,len(calls)//2-1),
            'elapsed_seconds':time.monotonic()-started,'interventions':[],'operator_requests':0,'external_interventions':'unknown'},
        'production_qualification':False}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode',choices=['scripted','live'],required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--services',help='Explicit owner-private build_context_executor factory.')
    parser.add_argument('--scenario',choices=['ready','repair','always-fail'],default='ready')
    args=parser.parse_args(argv)
    started=time.monotonic()
    record={'schema_version':'documentation-host-result/v1','observation_class':'scripted_documentation_mechanics' if args.mode=='scripted' else 'live_host_execution',
        'terminal':True,'operator_request':False,'production_qualification':False,
        'telemetry':{'scope':'documentation-host','calls':[],'retries':0,'elapsed_seconds':None,
            'interventions':[],'operator_requests':0,'external_interventions':'unknown'}}
    try:
        root=Path(args.output).expanduser().absolute()
        if root.exists() or root.is_symlink() or args.mode=='scripted' and args.services:
            raise ValueError('Select a new output and explicitly separate scripted/live services.')
        if args.mode=='live' and (not args.services or args.scenario!='ready'):
            raise ValueError('Live mode requires explicit services and a production scenario.')
        root.mkdir(mode=0o700,parents=True)
        if args.mode=='scripted':
            executor=scripted_executor(root)
        else:
            # Retrieve only the public installed factory loader, never a checkout helper.
            with Onboarding.open() as owner:
                files=json.loads(owner.resource('example/agent-host/v1')['content'])['files']
            services=root/'services.py';services.write_text(files['services.py'],encoding='utf-8')
            import importlib.util
            spec=importlib.util.spec_from_file_location('selected_documentation_services',services)
            module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
            executor=module.load_executor(args.services,root)
        value=pipeline(executor,scenario=args.scenario)
        record.update(value);record['status']=value['result']['status']
        if value['result']['status']=='ready':
            (root/'integration.md').write_text(value['result']['markdown'],encoding='utf-8')
        code=0 if record['status']=='ready' else 2
    except (EvidenceWikiError,ValueError,OSError):
        record.update(status='refused',reason='host_selection_or_documentation_refused');code=2
    if record['telemetry']['elapsed_seconds'] is None:
        record['telemetry']['elapsed_seconds']=time.monotonic()-started
    text=json.dumps(record,sort_keys=True,separators=(',',':'))
    if len((text+'\n').encode())>1_048_576:
        text=json.dumps({'schema_version':'documentation-host-result/v1','terminal':True,'status':'refused','reason':'output_bound','operator_request':False});code=2
    print(text)
    return code


if __name__=='__main__':
    raise SystemExit(main())
