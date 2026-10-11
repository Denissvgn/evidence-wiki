"""Installed closed-stdin adapters preserve public decisions, errors and authority."""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from evidence_wiki import HostRuntime, Onboarding, Workspace
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.host_context import HostContext
from evidence_wiki.research_command_contracts import REQUEST, RESULT, checked
from tests.test_execution_session import fixture as fixture


def selected(fixture):
    root, _policy, _execution, context = fixture
    return HostContext([root], context.grants | {'research_session.run_cycle','research_session.refresh','refresh_owner.apply',
        'research_session.brief','research_session.resolution','brief_operations.current','resolution_operations.current'},
        context.actor_ref,[root],authority_file=context.authority_file,state_directory=context.state_directory,
        mutation_control_directory=context.mutation_control_directory,controller_refs=context.controller_refs,
        role_controller_refs=context.role_controller_refs)


def document(fixture):
    root, policy, execution, _context = fixture
    context=selected(fixture)
    authority=execution.qualifications.authority
    def signing(value):
        return {key:getattr(value,key) for key in ('config_ref','principal','key_id','controller_ref','role')} | {'key':value.key.hex()}
    return {'schema_version':'research-host-runtime/v1','policy':policy.to_dict(),'profile':execution.profile.document(),
        'context':{'allowed_roots':[str(root)],'writable_roots':[str(root)],'actor_ref':context.actor_ref,'grants':sorted(context.grants),
            'authority_file':str(context.authority_file),'state_directory':str(context.state_directory),
            'mutation_control_directory':str(context.mutation_control_directory),'controller_refs':list(context.controller_refs),
            'role_controller_refs':list(context.role_controller_refs)},'commands':execution.executor._commands,'cwd':str(execution.executor._cwd),
        'meter':'native_process','keys':{'observer':(b'o'*32).hex(),'receipt':(b'e'*32).hex()},
        'qualification_authority':{'issuer_ref':authority.issuer_ref,'key':authority._key.hex(),'generator_controller_ref':authority.generator_controller_ref},
        'qualification_records':json.loads(execution.qualifications._records),
        'review_signer':{'authority_policy_id':execution.review_signer.authority_policy_id,'authority_revision':execution.review_signer.authority_revision,
            'generator':signing(execution.review_signer.generator),'reviewers':[signing(x) for x in execution.review_signer.reviewers],
            'expires_utc':execution.review_signer.expires_utc,'not_before_utc':execution.review_signer.not_before_utc}}


def write_runtime(tmp_path, value):
    path=tmp_path/'private-runtime.json';path.write_text(json.dumps(value));path.chmod(0o600);return path


def write_factory(tmp_path, value):
    path = tmp_path / 'private-services.py'
    path.write_text('from evidence_wiki import HostRuntime\n'
        'def build_runtime(root):\n    return HostRuntime.from_document(' + repr(value) + ')\n')
    path.chmod(0o600)
    return path


def test_explicit_factory_uses_same_public_runtime_and_real_closed_console(fixture, tmp_path):
    root, policy, execution, _ = fixture
    path = write_factory(tmp_path, document(fixture))
    runtime = HostRuntime.from_factory(path, root)
    assert runtime.policy.to_dict() == policy.to_dict() and runtime.execution.identity() == execution.identity()
    response = command('current', '--target', str(root), '--services', str(path), '--run-id', 'factory-read')
    assert response.returncode == 0 and response.stderr == '', (response.stdout, response.stderr)
    with Workspace.open(root) as workspace:
        expected = workspace.research.command({'schema_version': REQUEST, 'operation': 'current',
            'arguments': {'run_id': 'factory-read'}}, runtime=runtime)
    assert json.loads(response.stdout) == expected
    conflict = command('current', '--target', str(root), '--services', str(path), '--runtime', str(path))
    assert conflict.returncode != 0 and json.loads(conflict.stdout)['status'] == 'refused'


@pytest.mark.parametrize('kind', ['public', 'link', 'pipe', 'stdin', 'oversize', 'wrong', 'exception'])
def test_factory_private_selection_fails_without_disclosing_code(tmp_path, kind):
    path = tmp_path / 'services.py'
    path.write_text("def build_runtime(root):\n    raise ValueError('PRIVATE-FACTORY-SECRET')\n")
    path.chmod(0o600)
    if kind == 'public':
        path.chmod(0o644)
    elif kind == 'link':
        linked = tmp_path / 'linked.py'; linked.symlink_to(path); path = linked
    elif kind == 'pipe':
        path.unlink(); os.mkfifo(path)
    elif kind == 'stdin':
        path = '-'
    elif kind == 'oversize':
        path.write_text('#' + 'x' * 65_536)
    elif kind == 'wrong':
        path.write_text('def build_runtime(root):\n    return {}\n')
    with pytest.raises(EvidenceWikiError) as caught:
        HostRuntime.from_factory(path, tmp_path)
    assert caught.value.error_code == 'RESEARCH_RUNTIME_REFUSED'
    assert 'PRIVATE-FACTORY-SECRET' not in str(caught.value)


def test_factory_timeout_retains_uncertain_effects_and_does_not_redispatch(tmp_path):
    import time
    from datetime import datetime, timedelta, timezone

    from evidence_wiki import OperationControl
    path = tmp_path / 'services.py'
    completed = tmp_path / 'late-effect'
    path.write_text('import time\nfrom pathlib import Path\n'
        'def build_runtime(root):\n    time.sleep(0.25)\n    Path(' + repr(str(completed)) + ').write_text("once")\n    return {}\n')
    path.chmod(0o600)
    control = OperationControl(deadline_utc=(datetime.now(timezone.utc) + timedelta(seconds=0.1)).isoformat(), timeout_seconds=0.1)
    try:
        with pytest.raises(EvidenceWikiError) as caught:
            HostRuntime.from_factory(path, tmp_path, operation_control=control)
        assert caught.value.details['reason'] == 'factory_execution_uncertain'
        time.sleep(0.3)
        assert completed.read_text() == 'once'
    finally:
        control.close()


@pytest.mark.parametrize('startup', ["print('UNEXPECTED-STARTUP-OUTPUT')", "input()"])
def test_actual_console_rejects_noisy_or_input_reading_factory_transcript(tmp_path, startup):
    path = tmp_path / 'services.py'
    path.write_text(startup + '\ndef build_runtime(root):\n    return {}\n')
    path.chmod(0o600)
    response = command('current', '--target', str(tmp_path), '--services', str(path), '--run-id', 'bad-startup', timeout=10)
    assert response.returncode != 0 and response.stderr == ''
    if startup.startswith('print'):
        with pytest.raises(json.JSONDecodeError):
            json.loads(response.stdout)
        assert 'UNEXPECTED-STARTUP-OUTPUT' in response.stdout
    else:
        result = json.loads(response.stdout)
        assert result['status'] == 'refused' and result['error']['error_code'] == 'RESEARCH_RUNTIME_REFUSED'


def test_original_process_cap_retains_factory_entry_and_observed_stop(tmp_path):
    from datetime import datetime, timedelta, timezone

    from tools._integration_costs import freeze_cost_plan
    from tools.measure_integration_costs import observe_command, read_command_receipt
    entered, late = tmp_path/'entered', tmp_path/'late'
    path = tmp_path/'services.py'
    path.write_text('from pathlib import Path\nimport time\nPath('+repr(str(entered))+').write_text("entered")\n'
        'time.sleep(10)\nPath('+repr(str(late))+').write_text("late")\ndef build_runtime(root):\n    return {}\n')
    path.chmod(0o600)
    retained, workers = tmp_path/'retained', tmp_path/'workers'
    retained.mkdir(mode=0o700);workers.mkdir(mode=0o700)
    now = datetime.now(timezone.utc)
    plan = freeze_cost_plan(scope_id='factory-process-cap',binding={'factory':'original-cap'},sources=[{
        'source_id':'host','provenance':'host_observed','kind':'automated','origin':'Explicit console factory startup',
        'collection_started_at':(now-timedelta(seconds=1)).isoformat(),'collection_ended_at':(now+timedelta(seconds=30)).isoformat(),
        'coverage':'complete','authentication_ref':None}],attempts=[{'attempt_id':'startup','phase':'setup','allocation':'one_time',
            'source_id':'host','required_metrics':['elapsed_seconds']}])
    argv = [str(Path(sys.executable).with_name('evidence-wiki')),'agent','current','--target',str(tmp_path),
        '--services',str(path),'--run-id','bounded-startup']
    key = b'factory-process-observer-key-0000'
    value = observe_command(plan,'startup',argv,observer_key=key,directory=retained,work_root=workers,timeout_seconds=2)
    checked = read_command_receipt(plan,'startup',argv,observer_key=key,directory=retained,work_root=workers,
        expected_receipt_sha256=value['receipt_sha256'])
    assert entered.exists() and not late.exists()
    assert checked['receipt']['started_at'] and checked['receipt']['finished_at']
    assert checked['receipt']['exit_code'] is not None and 'timeout' in checked['receipt']['failure']
    assert checked['receipt']['status'] == 'failed' and not checked['receipt']['output_reader_work_unresolved']


def command(*args, timeout=120):
    console=Path(sys.executable).with_name('evidence-wiki.exe' if os.name=='nt' else 'evidence-wiki')
    return subprocess.run([str(console), 'agent', *args], stdin=subprocess.DEVNULL, capture_output=True,text=True,timeout=timeout,check=False)


def test_installed_runtime_roundtrip_budget_terminal_and_python_cli_replay_parity(fixture,tmp_path):
    root, policy, execution, _=fixture
    value=document(fixture);path=write_runtime(tmp_path,value)
    runtime=HostRuntime.from_file(path)
    assert runtime.policy.to_dict()==policy.to_dict() and runtime.execution.identity()==execution.identity()
    result=command('run-cycle','--target',str(root),'--runtime',str(path),'--run-id','bounded','--request-id','bounded-drive','--max-steps','1','--noninteractive')
    assert result.stderr=='' and result.returncode==3,(result.stdout,result.stderr)
    payload=json.loads(result.stdout);assert checked(RESULT,payload)==payload
    assert payload['status']=='returned' and payload['terminal'] and payload['result']['outcome']=='stop_budget'
    with Workspace.open(root) as workspace:
        replay=workspace.research.command({'schema_version':REQUEST,'operation':'run-cycle','arguments':{
            'run_id':'bounded','request_id':'bounded-drive','max_steps':1}},runtime=runtime)
    assert replay['status']=='returned', replay
    assert replay['exit_code']==result.returncode and replay['result']['historical']
    assert replay['result']['step']['decision']==payload['result']['step']['decision']
    conflict=command('run-cycle','--target',str(root),'--runtime',str(path),'--run-id','bounded','--request-id','bounded-drive','--max-steps','2')
    assert json.loads(conflict.stdout)['error']['error_code']=='RESEARCH_REQUEST_CONFLICT'
    assert (b'o'*32).hex() not in result.stdout and value['review_signer']['generator']['key'] not in result.stdout


def test_missing_runtime_closed_stdin_returns_same_terminal_error_as_python(fixture):
    root,*_=fixture
    response=command('run-cycle','--target',str(root),'--run-id','no-runtime','--request-id','drive',timeout=10)
    assert response.stderr==''
    payload=json.loads(response.stdout)
    with Workspace.open(root) as workspace:
        direct=workspace.research.command({'schema_version':REQUEST,'operation':'run-cycle','arguments':{'run_id':'no-runtime','request_id':'drive'}})
    assert payload==direct and payload['terminal'] and payload['status']=='refused'
    assert payload['error']['error_code']=='RESEARCH_EXECUTION_CONFIG_INVALID'
    assert not (root/'runs/no-runtime').exists()


@pytest.mark.parametrize('kind',['public','link','pipe','stdin','corrupt'])
def test_runtime_refusals_are_bounded_no_prompt_no_secret_disclosure(fixture,tmp_path,kind):
    root,*_=fixture
    value=document(fixture);path=write_runtime(tmp_path,value)
    if kind=='public':path.chmod(0o644)
    elif kind=='link':
        link=tmp_path/'runtime-link.json';link.symlink_to(path);path=link
    elif kind=='pipe':
        path=tmp_path/'runtime.fifo';os.mkfifo(path)
    elif kind=='stdin':path='-'
    else:Path(path).write_text('{}')
    response=command('run-cycle','--target',str(root),'--runtime',str(path),'--run-id','refused','--request-id','drive',timeout=10)
    payload=json.loads(response.stdout)
    assert response.stderr=='' and payload['status']=='refused' and payload['terminal']
    assert payload['error']['error_code']=='RESEARCH_RUNTIME_REFUSED'
    assert value['keys']['observer'] not in response.stdout


def test_command_arguments_and_stdin_file_selector_refuse_without_starting_run(fixture):
    root,*_=fixture
    response=command('run-cycle','--target',str(root),'--request-file','-',timeout=10)
    assert json.loads(response.stdout)['error']['error_code']=='RESEARCH_COMMAND_ARGUMENTS_INVALID'
    assert response.stderr==''


@pytest.mark.parametrize('options,expected', [({},'accepted'), ({'partial':True},'partial'),
    ({'role_isolation':True},'abstained')])
def test_installed_native_journeys_match_public_replay(tmp_path,monkeypatch,options,expected):
    case=fixture.__wrapped__(tmp_path,monkeypatch,SimpleNamespace(param=options))
    root,*_=case
    value=document(case)
    if options.get('partial'):
        from evidence_wiki.pack_discovery import owner
        owner('coverage_manifest').run_init_inline(root,slug='unavailable',template={'required_facets':[{
            'facet_id':'primary','description':'Retain the unavailable independent observation.',
            'evidence_path':'official_guidance','source_policy':'primary_or_official',
            'freshness_policy':'no_staleness_check','identity_policy':'none','min_sources':1}]})
    if options.get('role_isolation'):
        # Preserve qualified service identities while retaining a genuine disagreeing review.
        value['commands']['profile-b']=[*value['commands']['profile-b'][:2],
            value['commands']['profile-b'][2].replace("'pass')", "'fail')")]
    path=write_runtime(tmp_path,value)
    response=command('run-cycle','--target',str(root),'--runtime',str(path),'--run-id','journey',
        '--request-id','drive','--max-steps','64',timeout=600)
    assert response.stderr=='',response.stderr
    payload=json.loads(response.stdout)
    assert len(response.stdout.encode())<=1_048_576 and checked(RESULT,payload)==payload
    assert payload['status']=='returned' and payload['result']['outcome']==expected,payload
    with Workspace.open(root) as workspace:
        replay=workspace.research.command({'schema_version':REQUEST,'operation':'run-cycle','arguments':{
            'run_id':'journey','request_id':'drive','max_steps':64}},runtime=HostRuntime.from_file(path))
    assert replay['status']=='returned',replay
    assert replay['result']['outcome']==expected and replay['exit_code']==response.returncode
    assert replay['result']['original_outcomes']==payload['result']['original_outcomes']


def test_missing_private_signing_key_refuses_and_malformed_requests_are_terminal(fixture,tmp_path):
    root,*_=fixture
    value=document(fixture);del value['keys']['observer']
    path=write_runtime(tmp_path,value)
    response=command('run-cycle','--target',str(root),'--runtime',str(path),timeout=10)
    assert response.stderr=='' and json.loads(response.stdout)['error']['error_code']=='RESEARCH_RUNTIME_REFUSED'
    with Workspace.open(root) as workspace:
        malformed=workspace.research.command({'schema_version':REQUEST,'operation':'run-cycle'})
    assert malformed['terminal'] and malformed['status']=='refused'


@pytest.mark.parametrize('change',['parent','permissions'])
def test_runtime_parent_replacement_and_invalid_services_refuse(fixture,tmp_path,monkeypatch,change):
    directory=tmp_path/'private';directory.mkdir()
    path=write_runtime(directory,document(fixture))
    read=os.read
    swapped=[]
    def replace_parent(descriptor,count):
        data=read(descriptor,count)
        if data and not swapped:
            if change=='parent':
                directory.rename(tmp_path/'previous-private');directory.mkdir()
            else:
                path.chmod(0o644)
            swapped.append(True)
        return data
    monkeypatch.setattr('evidence_wiki.runtime_configuration.os.read',replace_parent)
    with pytest.raises(EvidenceWikiError) as error:
        HostRuntime.from_file(path)
    assert error.value.error_code=='RESEARCH_RUNTIME_REFUSED'
    with pytest.raises(EvidenceWikiError):
        HostRuntime(None,None,None)


def test_progress_report_write_requires_explicit_library_grant(fixture,tmp_path):
    root,*_=fixture
    reports=tmp_path/'local-reports';reports.mkdir();output=reports/'progress.json'
    with Onboarding.open(allowed_roots=[root,reports]) as library:
        assert library.research_progress(root)['schema_version']=='evidence-research-progress/v1'
        with pytest.raises(EvidenceWikiError) as error:
            library.research_progress(root,output=output)
        assert error.value.error_code=='ONBOARDING_AUTHORITY_REQUIRED' and not output.exists()
    with Onboarding.open(allowed_roots=[root,reports],allow=['research_progress.write']) as library:
        result=library.research_progress(root,output=output)
    assert json.loads(output.read_text())==result


def test_transport_bound_includes_json_stdout_newline():
    from evidence_wiki.mutation_contracts import canonical
    payload={'schema_version':RESULT,'operation':'current','terminal':True,'status':'returned',
        'result':{'pieces':['x'*65_536 for _ in range(15)]+['']},'error':None,'exit_code':0,
        'authorized':False,'evidence_accepted':False}
    remaining=1_048_575-len(canonical(payload))
    payload['result']['pieces'][-1]='x'*remaining
    assert len(canonical(checked(RESULT,payload)))+1==1_048_576
    payload['result']['pieces'][-1]+='x'
    with pytest.raises(EvidenceWikiError):
        checked(RESULT,payload)
