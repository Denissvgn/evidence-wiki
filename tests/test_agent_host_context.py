"""Task context boundaries and isolated installed host recipe mechanics."""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from evidence_wiki import AgentHostContext, Onboarding
from evidence_wiki.errors import UsageError
from tests._script_loader import load_module_uncached

pytest_plugins = ["tests.test_unattended_example"]


def context(**changes):
    arguments=dict(task='research',route='current-caller',role='host',objective='Assess a measured reflectance.',
        capabilities=['library','autonomous_runtime'],planner_ref='planner-controller',critic_ref='critic-controller')
    return AgentHostContext(**{**arguments,**changes})


def replies(selected, identifiers=None):
    packet=selected.planner_input()
    proposal={'schema_version':'agent-host-selection/v1','basis_id':packet['basis_id'],
        'resource_ids':packet['required_resource_ids'] if identifiers is None else identifiers}
    critique={'schema_version':'agent-host-critique/v1','basis_id':packet['basis_id'],'verdict':'pass'}
    return proposal,critique


def test_minimal_context_role_projection_and_detached_content():
    selected=context();packet=selected.planner_input()
    assert len(packet['resources'])==2 and all('content' not in row for row in packet['resources'])
    assert [row['id'] for row in packet['resources']]==['guide/agent-host/v1','guide/integration/v1']
    proposal,critique=replies(selected)
    assert selected.critic_input(proposal)['selected_context']['resources'][0]['content']
    result=selected.resolve(proposal,critique)
    assert result['status']=='ready' and result['context_bytes']<8192
    assert not result['authorized'] and not result['evidence_accepted']
    result['context']['resources'][0]['content']='changed'
    assert selected.resolve(proposal,critique)['context']['resources'][0]['content']!='changed'
    generator=context(role='generator')
    packet=generator.planner_input()
    assert [row['id'] for row in packet['resources']]==['guide/agent-generator/v1']
    assert generator.resolve(*replies(generator))['context_bytes']<2048


@pytest.mark.parametrize(('identifiers','reason'),[
    ([], 'missing_context'),(['example/unattended-host/v1'],'resource_outside_task'),
    (['guide/agent-host/v1']*2,'selection_invalid'),
])
def test_agent_agreement_cannot_override_native_checks(identifiers,reason):
    selected=context();proposal,critique=replies(selected,identifiers)
    assert selected.resolve(proposal,critique)['reason']==reason


def test_overload_stale_basis_malformed_critic_and_critic_refusal():
    selected=context(max_context_bytes=1024)
    proposal,critique=replies(selected)
    assert selected.critic_input(proposal)['native_reason']=='context_overload'
    assert selected.critic_input(proposal)['selected_context'] is None
    assert selected.resolve(proposal,critique)['reason']=='context_overload'
    selected=context();proposal,critique=replies(selected)
    for value in (None,{},[],{'bad':'value'}):
        assert selected.resolve(proposal,value)['reason']=='critique_invalid'
    critique['verdict']=[]
    assert selected.resolve(proposal,critique)['reason']=='critique_invalid'
    critique['verdict']='fail'
    assert selected.resolve(proposal,critique)['reason']=='critic_refused'
    proposal['basis_id']='stale'
    assert selected.resolve(proposal,critique)['reason']=='selection_invalid'


@pytest.mark.parametrize('changes',[{'profile':'human_required'},{'profile':'incompatible'},
    {'capabilities':[]},{'critic_ref':'planner-controller'}])
def test_incompatible_host_terminates_before_dispatch(changes):
    selected=context(**changes)
    assert selected.planner_input() is None and selected.critic_input(None) is None
    result=selected.resolve()
    assert result['terminal'] and result['status']=='refused' and result['operator_request'] is False


@pytest.mark.parametrize('changes',[{'max_context_bytes':True},{'objective':'é'*4096},
    {'task':'unknown'},{'route':[]},{'capabilities':['library']*33},{'objective':'\ud800'}])
def test_request_bounds(changes):
    with pytest.raises(UsageError):
        context(**changes)


def test_recipe_is_discoverable_through_public_resources():
    with Onboarding.open() as owner:
        recipe=owner.recipes()['agent_hosts']
        assert recipe['routes']==['current-caller','mcp']
        for key in ('instructions','generator_instructions','example'):
            assert owner.resource(recipe[key])['content']


def test_resource_generation_change_refuses_retained_selection(monkeypatch):
    import evidence_wiki.agent_host_context as module

    selected=context();proposal,critique=replies(selected)
    original=module.resource_document
    monkeypatch.setattr(module,'resource_document',lambda key:{**original(key),'sha256':'changed'})
    assert selected.resolve(proposal,critique)['reason']=='resource_changed'


@pytest.fixture(scope='module')
def agent_host(installed):
    target,_,directory=installed
    interpreter=directory/'.venv/bin/python';app=directory/'agent-app'
    program='''
import json,sys
from pathlib import Path
from evidence_wiki import Onboarding
with Onboarding.open() as owner:
 files=json.loads(owner.resource('example/agent-host/v1')['content'])['files']
destination=Path(sys.argv[1]);destination.mkdir()
for name,content in files.items():
 assert name==Path(name).name
 (destination/name).write_text(content,encoding='utf-8')
'''
    result=subprocess.run([str(interpreter),'-I','-c',program,str(app)],cwd=directory,
        capture_output=True,text=True,timeout=30,check=False)
    assert result.returncode==0,result.stderr
    return interpreter,app,directory,target


def run(agent_host,*arguments,timeout=900):
    interpreter,app,directory,target=agent_host
    program='''
import runpy,sys
from pathlib import Path
import evidence_wiki
assert Path(evidence_wiki.__file__).is_relative_to(Path(sys.argv[1]))
sys.path.insert(0,sys.argv[2]);sys.argv=[str(Path(sys.argv[2])/'agent_host.py'),*sys.argv[3:]]
runpy.run_path(sys.argv[0],run_name='__main__')
'''
    return subprocess.run([str(interpreter),'-I','-c',program,str(target),str(app),*arguments],cwd=directory,
        stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=timeout,check=False)


@pytest.mark.parametrize('scenario',['omit','overload','missing-capability','human-required'])
def test_installed_scripted_negative_contexts_have_complete_scoped_telemetry(agent_host,scenario):
    result=run(agent_host,'--mode','scripted','--scenario',scenario,'--output',str(agent_host[2]/scenario),timeout=60)
    assert result.returncode==2,(result.stdout,result.stderr)
    record=json.loads(result.stdout)
    assert record['observation_class']=='scripted_fixture_mechanics'
    assert record['context']['selection']['status']=='refused'
    telemetry=record['context']['telemetry']
    assert len(telemetry['calls'])==(2 if scenario in {'omit','overload'} else 0)
    assert telemetry['operator_requests']==0 and telemetry['interventions']==[]
    assert telemetry['external_interventions']=='unknown'
    assert not (agent_host[2]/scenario/'fixture').exists()


def test_installed_critic_rejects_missing_capability_packet(agent_host):
    selected=context();proposal,_=replies(selected)
    packet=selected.critic_input(proposal);packet['missing_capabilities']=['scoped_mcp']
    interpreter,app,directory,_=agent_host
    result=subprocess.run([str(interpreter),str(app/'worker.py')],cwd=directory,
        input=json.dumps({'job':{'task_role':'critic'},'input':packet}),capture_output=True,text=True,timeout=30,check=False)
    assert result.returncode==0 and json.loads(result.stdout)['verdict']=='fail'


def test_installed_live_startup_is_terminal_and_secret_safe(agent_host):
    _,_,directory,_=agent_host
    factory=directory/'private-services.py'
    factory.write_text('syntax-invalid confidential-sentinel',encoding='utf-8');factory.chmod(0o600)
    result=run(agent_host,'--mode','live','--workspace',str(directory),'--runtime',str(directory/'absent-runtime.json'),
        '--context-services',str(factory),'--output',str(directory/'live-refusal'),timeout=60)
    assert result.returncode==2
    assert 'confidential-sentinel' not in result.stdout+result.stderr
    record=json.loads(result.stdout)
    assert record['observation_class']=='live_host_execution' and record['operator_request'] is False
    assert record['telemetry']['calls']==[]


@pytest.mark.parametrize('human_required',[False,True])
def test_live_context_uses_the_selected_policy_objective_and_review_profile(tmp_path,monkeypatch,human_required):
    example=Path(__file__).resolve().parents[1]/'examples/agent-host'
    monkeypatch.syspath_prepend(str(example))
    # Each case patches module globals and needs an independent host module.
    module=load_module_uncached('selected_agent_host',example/'agent_host.py')
    selected_policy={'objective':'Assess a different original objective.','human_review':human_required,
        'required_evaluators':[{'human_required':False}]}
    runtime=SimpleNamespace(policy=SimpleNamespace(to_dict=lambda:selected_policy),
        execution=SimpleNamespace(profile=SimpleNamespace(roles=[SimpleNamespace(model_ref='provider/selected')])) )
    monkeypatch.setattr(module.HostRuntime,'from_file',lambda _path:runtime)
    monkeypatch.setattr(module,'bundle',lambda _path:None)
    monkeypatch.setitem(sys.modules,'services',SimpleNamespace(load_executor=lambda _path,_root:object()))
    captured={}

    def selected_context(_executor,**arguments):
        captured.update(arguments)
        return {'selection':{'status':'refused'}}

    monkeypatch.setattr(module,'select_context',selected_context)
    assert module.main(['--mode','live','--workspace',str(tmp_path),'--runtime','selected.json',
        '--context-services','selected.py','--output',str(tmp_path/'record')])==2
    assert captured['objective']==selected_policy['objective']
    assert captured['profile']==('human_required' if human_required else 'autonomous')


@pytest.mark.parametrize('route',['current-caller','mcp'])
def test_installed_closed_stdin_full_scripted_journey(agent_host,route):
    result=run(agent_host,'--mode','scripted','--route',route,'--output',str(agent_host[2]/route))
    assert result.returncode==0,(result.stdout,result.stderr)
    record=json.loads(result.stdout)
    assert record['production_qualification'] is False
    assert record['context']['selection']['context_bytes']<8192
    assert len(record['context']['telemetry']['calls'])==2
    assert all(row['state']=='succeeded' for row in record['context']['telemetry']['calls'])
    assert record['journey']['cycle']['result']['outcome']=='accepted'
    assert record['journey']['brief']['result']['verdict']=='accept'
    assert all(row['state']=='returned' for row in record['telemetry']['calls'])
    assert record['telemetry']['retries']==0 and record['telemetry']['operator_requests']==0
