"""Installed public example mechanics, explicit live refusal and no private imports."""

import ast
import importlib.metadata
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT/'examples/unattended-host'


@pytest.fixture(scope='module')
def installed(tmp_path_factory):
    directory = tmp_path_factory.mktemp('unattended-wheel')
    wheels = directory/'wheels';wheels.mkdir()
    build = subprocess.run([sys.executable,'-m','build','--wheel','--no-isolation','--outdir',str(wheels)],
        cwd=ROOT,capture_output=True,text=True,timeout=120,check=False)
    assert build.returncode==0,build.stderr
    environment = directory/'.venv'
    created = subprocess.run([sys.executable,'-m','venv','--without-pip',str(environment)],capture_output=True,text=True,timeout=60,check=False)
    assert created.returncode==0,created.stderr
    interpreter = environment/'bin/python'
    layout = subprocess.check_output([str(interpreter),'-I','-c',"import sysconfig;print(sysconfig.get_path('purelib'))"],text=True)
    target = Path(layout.strip())
    # Reuse only the declared runtime distributions, never the editable package or checkout finder.
    for name in ('PyYAML','pypdf','ruamel.yaml','tzdata'):
        distribution=importlib.metadata.distribution(name)
        for relative in distribution.files:
            if '..' in relative.parts or relative.suffix=='.pyc':
                continue
            source=Path(distribution.locate_file(relative))
            if source.is_file():
                destination=target/relative;destination.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,destination)
    install = subprocess.run([sys.executable,'-m','pip','install','--no-deps','--no-compile','--target',str(target),
        str(next(wheels.glob('*.whl')))],capture_output=True,text=True,timeout=120,check=False)
    assert install.returncode==0,install.stderr
    extractor = '''
import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from evidence_wiki import Onboarding
import evidence_wiki
assert Path(evidence_wiki.__file__).is_relative_to(Path(sys.argv[1]))
with Onboarding.open() as host:
 bundle=json.loads(host.resource('example/unattended-host/v1')['content'])
destination=Path(sys.argv[2]);destination.mkdir()
for name,text in bundle['files'].items():
 assert name==Path(name).name
 (destination/name).write_text(text,encoding='utf-8')
'''
    app = directory/'app'
    result = subprocess.run([str(interpreter),'-I','-c',extractor,str(target),str(app)],cwd=directory,
        capture_output=True,text=True,timeout=30,check=False)
    assert result.returncode==0,result.stderr
    return target,app,directory


def run(installed,*arguments,timeout=900):
    target,app,directory=installed
    program='''
import runpy,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1]);sys.path.insert(0,sys.argv[2])
import evidence_wiki
assert Path(evidence_wiki.__file__).is_relative_to(Path(sys.argv[1]))
sys.argv=[str(Path(sys.argv[2])/'host.py'),*sys.argv[3:]]
runpy.run_path(sys.argv[0],run_name='__main__')
'''
    return subprocess.run([str(directory/'.venv/bin/python'),'-I','-c',program,str(target),str(app),*arguments],cwd=directory,
        stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=timeout,check=False)


def test_bundle_imports_only_public_package_interfaces_and_has_no_stored_secrets():
    for path in EXAMPLE.glob('*.py'):
        tree=ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node,ast.ImportFrom) and node.module and node.module.startswith('evidence_wiki'):
                assert not any(part.startswith('_') for part in node.module.split('.'))
                assert all(not name.name.startswith('_') for name in node.names)
            if isinstance(node,ast.Attribute):
                assert not node.attr.startswith('_') or node.attr in {'__file__','__name__'}
        assert 'tests.' not in path.read_text()
    assert not list(EXAMPLE.glob('*.key')) and not list(EXAMPLE.glob('*authority*.json'))


def test_wheel_only_headless_repair_export_refresh_and_separate_fixture_authority(installed):
    _,_,directory=installed
    output=directory/'repair-journey'
    result=run(installed,'--mode','fake','--output',str(output))
    assert result.returncode==0,(result.stdout,result.stderr)
    assert len(result.stdout.encode())<=1_048_576
    document=json.loads(result.stdout)
    assert document['observation_class']=='fixture_mechanics' and document['production_qualification'] is False
    assert document['reports'][0]['outcome']=='accepted',document
    assert document['brief']['verdict']=='accept'
    counts=document['accounting']['activity_summary']['counts']
    assert counts['critical_review_failures']>=1 and counts['repair_attempt_intents']>=1
    assert counts['failed_review_applications']>=1 and not counts['application_failure_records']
    assert len(document['reports'])==2 and document['reports'][1]['cycle']['terminal']
    assert document['reports'][1]['previous_run_id']=='study'
    authority=json.loads((output/'fixture-private/authority.json').read_text())
    for principal in authority['principals'].values():
        for key in principal['keys'].values():
            assert key not in result.stdout and key not in result.stderr
    assert not document['authorized'] and not document['evidence_accepted']
    assert authority['policy_id']=='fixture-authority'


def test_wheel_only_abstention_and_finite_budget_are_labeled(installed):
    _,_,directory=installed
    budget=run(installed,'--mode','fake','--output',str(directory/'budget-journey'),'--max-steps','1','--no-refresh')
    assert budget.returncode==3 and json.loads(budget.stdout)['reports'][0]['outcome']=='stop_budget'
    abstained=run(installed,'--mode','fake','--output',str(directory/'abstain-journey'),'--scenario','abstain','--no-refresh')
    document=json.loads(abstained.stdout)
    assert abstained.returncode==3 and document['reports'][0]['outcome']=='abstained',document
    assert document['production_qualification'] is False and document['brief']['verdict']=='abstain'


def test_live_missing_services_and_host_factory_errors_do_not_prompt_or_leak(installed):
    _,_,directory=installed
    missing=run(installed,'--mode','live',timeout=10)
    assert missing.returncode==2 and json.loads(missing.stdout)['terminal']
    factory=directory/'private-factory.py'
    factory.write_text("def build_runtime(root):\n    raise ValueError('PRIVATE-CREDENTIAL-MUST-NOT-LEAK')\n")
    factory.chmod(0o600)
    result=run(installed,'--mode','live','--workspace',str(directory),'--services',str(factory),timeout=10)
    assert result.returncode==2 and json.loads(result.stdout)['terminal']
    assert 'PRIVATE-CREDENTIAL-MUST-NOT-LEAK' not in result.stdout+result.stderr
    factory.write_text("PRIVATE-CREDENTIAL-MUST-NOT-LEAK =\n")
    result=run(installed,'--mode','live','--workspace',str(directory),'--services',str(factory),timeout=10)
    assert result.returncode==2 and 'PRIVATE-CREDENTIAL-MUST-NOT-LEAK' not in result.stdout+result.stderr
