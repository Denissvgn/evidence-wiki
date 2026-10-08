"""Contract-derived documentation, request parity and isolated installed journeys."""

import ast
import importlib.metadata
import json
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from evidence_wiki import IntegrationDocumentation, Onboarding, contract, research_request
from evidence_wiki.errors import UsageError
from evidence_wiki.research_command_definitions import DEFINITIONS

ROOT=Path(__file__).resolve().parents[1]


def critique(document,verdict='pass'):
    return {'schema_version':'integration-documentation-critique/v1','basis_id':document.reference()['basis_id'],'verdict':verdict}


def test_reference_and_generated_resource_follow_public_contract():
    doc=IntegrationDocumentation();reference=doc.reference();public=contract()['library_api']
    assert reference['library_api_version']==public['version']
    assert reference['matrix_version']==public['matrix_version']
    assert reference['command_boundary']==next(row for row in public['operations'] if row['operation']=='research.command')
    assert len(reference['operations'])==len(DEFINITIONS)
    with Onboarding.open() as owner:
        assert owner.resource('guide/integration/v1')['content']==doc.render()
    reference['operations'].clear()
    assert doc.reference()['operations']
    assert not re.search(r'\bUP[0-9]{3}\b',doc.render())
    assert not any(value in doc.render() for value in ('backlog','milestone'))


def test_request_builder_validates_native_argument_shapes_without_runtime_or_effects():
    request=research_request('run-cycle',run_id='original',request_id='cycle',max_steps=64)
    assert request['schema_version']=='research-command-request/v1'
    assert request['arguments']=={'run_id':'original','request_id':'cycle','max_steps':64}
    for operation,arguments in [('unknown',{}),('run-cycle',{}),('brief',{'run_id':'run','runtime':{}}),
                                ('run-cycle',{'run_id':'run','request_id':'req','max_steps':True}),
                                ('report',{'run_id':'run','max_records':128}),
                                ('recovery-status',{'run_id':'run','max_effects':128})]:
        with pytest.raises(UsageError):
            research_request(operation,**arguments)
    assert research_request('report',run_id='run',max_records=64)['arguments']['max_records']==64
    assert research_request('recovery-status',run_id='run',max_effects=64)['arguments']['max_effects']==64


def test_native_coverage_checks_override_critic_and_bound_inert_repairs(monkeypatch):
    import evidence_wiki.integration_documentation as module

    doc=IntegrationDocumentation();proposal=doc.proposal()
    assert doc.resolve(proposal,critique(doc))['status']=='ready'
    for field in ('section_ids','operation_ids'):
        broken=doc.proposal();broken[field]=[]
        result=doc.resolve(broken,critique(doc))
        assert result['issues']==['contract_coverage'] and result['markdown'] is None
    proposal['basis_id']='stale'
    assert doc.resolve(proposal,critique(doc))['status']=='refused'
    assert doc.resolve(doc.proposal(),critique(doc,'fail'))['status']=='refused'
    broken=doc.proposal();broken['section_ids']=['x'*65536]
    assert doc.resolve(broken,critique(doc))['issues']==['proposal_bound_or_encoding']
    monkeypatch.setattr(module,'LIBRARY_API_VERSION','changed')
    assert doc.resolve(doc.proposal(),critique(doc))['issues']==['declarations_changed']
    repair=doc.repair(failure_kind='integration_failure',attempt=1,basis_id=doc.reference()['basis_id'])
    assert repair['kind']=='api_investigation' and repair['effects']=='proposal_only' and repair['proposal'] is None
    assert doc.repair(failure_kind='documentation_mismatch',attempt=2,basis_id=doc.reference()['basis_id'])['next_action']=='stop_bound'
    with pytest.raises(UsageError):
        doc.repair(failure_kind='integration_failure',attempt=3,basis_id=doc.reference()['basis_id'])


def test_agent_inputs_and_rendered_example_contain_only_public_interfaces():
    doc=IntegrationDocumentation();packet=doc.agent_input(purpose='critic',proposal=doc.proposal())
    assert packet['native_issues']==[] and packet['markdown']==doc.render()
    source=doc.render().split('```python\n',1)[1].split('\n```',1)[0]
    tree=ast.parse(source)
    assert all(not name.name.startswith('_') for node in ast.walk(tree) if isinstance(node,ast.ImportFrom) for name in node.names)
    assert 'research_request' in source and 'schema_version' not in source
    for path in (ROOT/'examples/integration-docs').glob('*.py'):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node,ast.ImportFrom) and node.module and node.module.startswith('evidence_wiki'):
                assert not any(part.startswith('_') for part in node.module.split('.'))
                assert all(not name.name.startswith('_') for name in node.names)


@pytest.fixture(scope='module')
def distributions(tmp_path_factory):
    directory=tmp_path_factory.mktemp('integration-distributions');builds=directory/'builds';builds.mkdir()
    result=subprocess.run([sys.executable,'-m','build','--wheel','--sdist','--no-isolation','--outdir',str(builds)],
        cwd=ROOT,capture_output=True,text=True,timeout=120,check=False)
    assert result.returncode==0,result.stderr
    source=directory/'source';source.mkdir()
    with tarfile.open(next(builds.glob('*.tar.gz'))) as archive:
        archive.extractall(source,filter='data')  # noqa: S202 - data filter over our generated project distribution.
    rebuilt=directory/'rebuilt';rebuilt.mkdir()
    result=subprocess.run([sys.executable,'-m','build','--wheel','--no-isolation','--outdir',str(rebuilt)],
        cwd=next(source.iterdir()),capture_output=True,text=True,timeout=120,check=False)
    assert result.returncode==0,result.stderr
    return directory,{'wheel':next(builds.glob('*.whl')),'sdist':next(rebuilt.glob('*.whl'))}


@pytest.fixture(scope='module',params=['wheel','sdist'])
def installed(request,distributions):
    directory,wheels=distributions;root=directory/request.param;root.mkdir()
    environment=root/'.venv'
    result=subprocess.run([sys.executable,'-m','venv','--without-pip',str(environment)],capture_output=True,text=True,timeout=60,check=False)
    assert result.returncode==0,result.stderr
    interpreter=environment/'bin/python'
    site=Path(subprocess.check_output([str(interpreter),'-I','-c','import sysconfig;print(sysconfig.get_path("purelib"))'],text=True).strip())
    for name in ('PyYAML','pypdf','ruamel.yaml','tzdata'):
        distribution=importlib.metadata.distribution(name)
        for relative in distribution.files:
            if '..' not in relative.parts and relative.suffix!='.pyc':
                path=Path(distribution.locate_file(relative))
                if path.is_file():
                    target=site/relative;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(path,target)
    result=subprocess.run([sys.executable,'-m','pip','install','--no-deps','--no-compile','--target',str(site),str(wheels[request.param])],
        capture_output=True,text=True,timeout=120,check=False)
    assert result.returncode==0,result.stderr
    program='''
import json,sys
from pathlib import Path
from evidence_wiki import Onboarding
import evidence_wiki
assert Path(evidence_wiki.__file__).is_relative_to(Path(sys.argv[1]))
with Onboarding.open() as owner:
 for name in ('integration-docs','unattended-host'):
  files=json.loads(owner.resource('example/'+name+'/v1')['content'])['files']
  target=Path(sys.argv[2])/name;target.mkdir()
  for relative,content in files.items():
   assert relative==Path(relative).name
   (target/relative).write_text(content,encoding='utf-8')
 guide=owner.resource('guide/integration/v1')['content']
(Path(sys.argv[2])/'integration.md').write_text(guide,encoding='utf-8')
'''
    result=subprocess.run([str(interpreter),'-I','-c',program,str(site),str(root)],cwd=root,
        stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=30,check=False)
    assert result.returncode==0,result.stderr
    return request.param,root,interpreter,site


def run_host(installed,scenario):
    kind,root,interpreter,_=installed
    program='''
import runpy,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1]);sys.argv=[str(Path(sys.argv[1])/'documentation_host.py'),*sys.argv[2:]]
runpy.run_path(sys.argv[0],run_name='__main__')
'''
    result=subprocess.run([str(interpreter),'-I','-c',program,str(root/'integration-docs'),'--mode','scripted',
        '--scenario',scenario,'--output',str(root/scenario)],cwd=root,stdin=subprocess.DEVNULL,
        capture_output=True,text=True,timeout=90,check=False)
    (root/(scenario+'-record.json')).write_text(result.stdout,encoding='utf-8')
    return result


def test_installed_documentation_agent_critic_repair_and_terminal_bound(installed):
    for scenario,calls,code in [('ready',2,0),('repair',4,0),('always-fail',4,2)]:
        result=run_host(installed,scenario)
        assert result.returncode==code,(result.stdout,result.stderr)
        record=json.loads(result.stdout)
        assert record['observation_class']=='scripted_documentation_mechanics'
        assert len(record['telemetry']['calls'])==calls and record['operator_request'] is False
        assert record['telemetry']['external_interventions']=='unknown'
        assert record['production_qualification'] is False
        if code==0:
            assert (installed[1]/scenario/'integration.md').read_text()==(installed[1]/'integration.md').read_text()
        else:
            assert record['repairs'][-1]['next_action']=='stop_bound'


def test_installed_generated_code_follows_full_journey_without_source_or_schema_authoring(installed):
    kind,root,interpreter,site=installed
    program='''
import json,sys
from pathlib import Path
from evidence_wiki import IntegrationDocumentation
import evidence_wiki
assert Path(evidence_wiki.__file__).is_relative_to(Path(sys.argv[1]))
sys.path.insert(0,sys.argv[2])
from fixture_host import provision
root,runtime,_=provision(Path(sys.argv[3]),'Assess the observed solar reflectance with uncertainty.')
guide=Path(sys.argv[4]).read_text()
assert guide==IntegrationDocumentation().render()
code=guide.split('```python\\n',1)[1].split('\\n```',1)[0]
namespace={};exec(compile(code,'installed-public-guide','exec'),namespace)
result=namespace['run_journey'](root,runtime)
assert result['cycle']['result']['outcome']=='accepted',result
assert result['brief']['result']['verdict']=='accept',result
print(json.dumps({'observation_class':'scripted_installed_journey','distribution':sys.argv[5],
 'result':result,'operator_requests':0,'external_interventions':'unknown','production_qualification':False},sort_keys=True))
'''
    result=subprocess.run([str(interpreter),'-I','-c',program,str(site),str(root/'unattended-host'),str(root/'journey'),
        str(root/'integration.md'),kind],cwd=root,stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=1800,check=False)
    (root/'journey-record.json').write_text(result.stdout,encoding='utf-8')
    assert result.returncode==0,(result.stdout,result.stderr)
