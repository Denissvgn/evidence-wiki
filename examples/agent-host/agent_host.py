"""Installed-only scripted journey or explicit provisioned live host execution."""

import argparse
import json
import sys
import time
from pathlib import Path

from context import select_context

from evidence_wiki import (
    BoundedSubprocessExecutor,
    ExecutionProfile,
    ExecutionRole,
    ExecutionRoleRegistry,
    ExecutionRoleScope,
    HostRuntime,
    Onboarding,
    Workspace,
)
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.onboarding_mcp import OnboardingMcpServer


def scripted_executor(directory, scenario):
    directory.mkdir(mode=0o700)
    roles = (ExecutionRole('context-planner','host_worker','planner-controller','simulated/context-planner','select/v1'),
        ExecutionRole('context-critic','evaluator','critic-controller','simulated/context-critic','critic/v1'))
    scopes = ExecutionRoleRegistry((ExecutionRoleScope('context-planner','planner',('model',)),
        ExecutionRoleScope('context-critic','critic',('model',))))
    profile = ExecutionProfile('scripted-context',60,'EUR','unknown/v1',roles,1,1,1000,'0',1,role_scopes=scopes)
    worker = str(Path(__file__).with_name('worker.py').resolve())
    return BoundedSubprocessExecutor(profile,commands={row.config_ref:[sys.executable,worker,scenario] for row in roles},cwd=directory)


def bundle(destination):
    with Onboarding.open() as owner:
        files=json.loads(owner.resource('example/unattended-host/v1')['content'])['files']
    destination.mkdir(mode=0o700)
    for name,content in files.items():
        if name != Path(name).name:
            raise ValueError('Invalid installed example filename.')
        (destination/name).write_text(content,encoding='utf-8')
    sys.path.insert(0,str(destination))


def journey(root, runtime, route, run_id, calls):
    """Use the same public command envelope through both selected routes."""
    server = None
    if route=='mcp':
        server = OnboardingMcpServer(allowed_roots=[root],runtime=runtime,research_allow=['run-cycle','refresh'])
        initialized = server.handle_message({'jsonrpc':'2.0','id':0,'method':'initialize','params':{
            'protocolVersion':'2024-11-05','capabilities':{},'clientInfo':{'name':'installed-host','version':'1'}}})
        if 'error' in initialized:
            raise ValueError('Scoped protocol initialization refused.')
        server.handle_message({'jsonrpc':'2.0','method':'notifications/initialized','params':{}})
        discovery=server.handle_message({'jsonrpc':'2.0','id':1,'method':'tools/list','params':{}})
        required={'research_run_cycle','research_brief','research_report','research_recovery_status'}
        if 'error' in discovery or not required<={row['name'] for row in discovery['result']['tools']}:
            server.close()
            raise ValueError('Required scoped tools unavailable.')
        calls.append({'operation':'tools/list','state':'returned','exit_code':0})

    def call(operation, arguments):
        calls.append({'operation':operation,'state':'attempted'})
        if server is None:
            with runtime.context.activate():
                with Workspace.open(root) as workspace:
                    result=workspace.research.command({'schema_version':'research-command-request/v1',
                        'operation':operation,'arguments':arguments},runtime=runtime)
        else:
            # The server activates the startup runtime around the research owner;
            # protocol discovery retains its separately selected onboarding scope.
            response=server.handle_message({'jsonrpc':'2.0','id':len(calls)+1,'method':'tools/call','params':{
                'name':'research_'+operation.replace('-','_'),'arguments':{'target':str(root),**arguments}}})
            if 'error' in response:
                calls[-1]['state']='refused'
                raise ValueError('Scoped protocol call refused.')
            result=json.loads(response['result']['content'][0]['text'])
        if type(result) is not dict or result.get('schema_version')!='research-command-result/v1':
            calls[-1]['state']='refused'
            raise ValueError('Unexpected native command envelope.')
        calls[-1]['state']=result['status'];calls[-1]['exit_code']=result['exit_code']
        return result

    try:
        scopes=runtime.execution.profile.role_scopes
        planners=[] if scopes is None else [row.config_ref for row in scopes.scopes if row.task_role=='planner']
        arguments={'run_id':run_id,'request_id':run_id+'-cycle','max_steps':64}
        if len(planners)==1:
            arguments['planner_config_ref']=planners[0]
        cycle=call('run-cycle',arguments)
        if cycle['status']=='refused':
            return {'cycle':cycle}
        brief=call('brief',{'run_id':run_id})
        accounting=call('report',{'run_id':run_id,'max_records':64})
        recovery=call('recovery-status',{'run_id':run_id,'max_effects':64})
        return {'cycle':cycle,'brief':brief,'report':accounting,'recovery':recovery}
    finally:
        if server is not None:
            server.close()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode',choices=['scripted','live'],required=True)
    parser.add_argument('--route',choices=['current-caller','mcp'],default='current-caller')
    parser.add_argument('--output',required=True)
    parser.add_argument('--workspace');parser.add_argument('--runtime')
    parser.add_argument('--context-services',help='Owner-private trusted Python factory returning a bounded context executor.')
    parser.add_argument('--scenario',choices=['ready','omit','overload','missing-capability','human-required'],default='ready')
    parser.add_argument('--run-id',default='installed-journey')
    args=parser.parse_args(argv)
    started=time.monotonic();calls=[]
    result={'schema_version':'agent-host-run/v1','observation_class':'scripted_fixture_mechanics' if args.mode=='scripted' else 'live_host_execution',
        'production_qualification':False,'terminal':True,'operator_request':False}
    try:
        base=Path(args.output).expanduser().absolute()
        if base.exists() or base.is_symlink():
            raise ValueError('Select a new output directory.')
        if args.mode=='scripted' and (args.workspace or args.runtime or args.context_services):
            raise ValueError('Scripted mode refuses live services.')
        if args.mode=='live' and (not args.workspace or not args.runtime or not args.context_services or args.scenario!='ready'):
            raise ValueError('Live mode requires explicitly provisioned runtime and context services.')
        base.mkdir(mode=0o700,parents=True)
        bundle(base/'public-example')
        objective='Assess the observed solar reflectance with explicit uncertainty.'
        selected_profile='human_required' if args.scenario=='human-required' else 'autonomous'
        if args.mode=='scripted':
            executor=scripted_executor(base/'context-workers',args.scenario)
        else:
            # Reuse the installed example's owner-private factory loader. This
            # separate module implements the same selection checks for executors.
            from services import load_executor
            root=Path(args.workspace).resolve(strict=True)
            runtime=HostRuntime.from_file(args.runtime)
            live_policy=runtime.policy.to_dict()
            objective=live_policy['objective']
            if live_policy['human_review'] or any(row['human_required'] for row in live_policy['required_evaluators']):
                selected_profile='human_required'
            if any(row.model_ref.startswith(('simulated/','offline/','fixture/')) for row in runtime.execution.profile.roles):
                raise ValueError('Scripted profiles cannot establish live execution.')
            executor=load_executor(args.context_services,root)
        capabilities=['autonomous_runtime','library' if args.route=='current-caller' else 'scoped_mcp']
        if args.scenario=='missing-capability':
            capabilities=[]
        context=select_context(executor,objective=objective,
            route=args.route,capabilities=capabilities,profile=selected_profile,
            max_context_bytes=1024 if args.scenario=='overload' else 32768)
        result['context']=context
        if context['selection']['status']!='ready':
            result['status']='refused';code=2
        else:
            if args.mode=='scripted':
                from fixture_host import provision
                root,runtime,_=provision(base/'fixture',context['selection']['context']['objective'])
            result['journey']=journey(root,runtime,args.route,args.run_id,calls)
            cycle=result['journey']['cycle'];code=cycle['exit_code'];result['status']=cycle['status']
    except (EvidenceWikiError,ValueError,OSError):
        result['status']='refused';result['reason']='host_selection_or_execution_refused';code=2
    result['telemetry']={'scope':'installed-host','calls':calls,'retries':0,'elapsed_seconds':time.monotonic()-started,
        'interventions':[],'operator_requests':0,'external_interventions':'unknown'}
    text=json.dumps(result,sort_keys=True,separators=(',',':'))
    if len((text+'\n').encode())>1_048_576:
        text=json.dumps({'schema_version':'agent-host-run/v1','terminal':True,'status':'refused',
            'reason':'output_bound','operator_request':False});code=2
    print(text)
    return code


if __name__=='__main__':
    raise SystemExit(main())
