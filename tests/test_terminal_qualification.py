"""Missing evidence, changed budgets and missing finals retain actual starts."""

import hashlib
import json
import sys
from pathlib import Path

import pytest

from tools._acceptance_protocol import (
    AUTOMATIC_CATEGORIES,
    REQUIRED_CATEGORIES,
    digest,
    freeze_manifest,
    validate_cases,
)
from tools._terminal_cases import automatic_cases
from tools._terminal_qualification import (
    CONTEXT_FIELDS,
    READERS,
    ROLES,
    checked_verification,
    command_terminal,
    ready_for_verification,
    reconstruct,
)
from tools.measure_integration_costs import _authenticated, _signed
from tools.qualify_unattended import observe, prepare, replay, seal, start
from tools.run_comparative_trials import publish_new

KEY = b'qualification-original-observer-key'
ROOT = Path(__file__).resolve().parents[1]


def setup(tmp_path):
    from tests._usage_fixture import require_reference_storage
    require_reference_storage()
    candidate = tmp_path/'candidate'
    (candidate/'src/evidence_wiki').mkdir(parents=True)
    (candidate/'src/evidence_wiki/__init__.py').write_text("__version__='fixture'\n")
    (candidate/'pyproject.toml').write_text('[project]\nname="fixture"\nversion="0"\n')
    (candidate/'.venv/bin').mkdir(parents=True)
    (candidate/'.venv/bin/evidence-wiki').write_text((ROOT/'.venv/bin/evidence-wiki').read_text())
    assignments = []
    for role in ROLES:
        item = {'role':role,'agent_id':'fixture-'+role,'controller_ref':'controller-'+role,
            'context_ref':'context-'+role,'task_path':'/fixture/'+role}
        transcript = tmp_path/(role+'.assignment.json')
        publish_new(transcript,{**item,'source':'trusted_host_tool_transcript','tool_calls':[{
            'tool':'collaboration.spawn_agent','agent_id':item['agent_id'],'task_path':item['task_path']}]})
        assignments.append({**item,'transcript_path':str(transcript), 'transcript_sha256':hashlib.sha256(transcript.read_bytes()).hexdigest()})
    path = tmp_path/'assignments.json'
    publish_new(path,assignments)
    config = {'repo_root':str(candidate),'assignments_path':str(path),'assignments_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
        'jobs':[{'role':role,'argv':[sys.executable,'-c',"print('{}')"],'timeout_seconds':2,
            'reports':[{'kind':kind,'path':str(tmp_path/(role+'-'+kind+'.json'))} for kind in sorted(READERS[role])]}
            for role in ROLES], 'context':{name:{'scope':'declared-fixture'} for name in CONTEXT_FIELDS},'timeout_seconds':30}
    directory = tmp_path/'evidence'
    plan = prepare(config,directory=directory)
    start(directory,observer_key=KEY)
    return plan,directory


def test_product_scenarios_are_separate_fixed_inventory_without_relabelled_attack_categories():
    cases = automatic_cases()
    assert len(cases) == 18 and {category for case in cases for category in case['categories']} == AUTOMATIC_CATEGORIES
    assert not AUTOMATIC_CATEGORIES & REQUIRED_CATEGORIES
    assert validate_cases(cases) == cases
    exact = [selector for case in cases for selector in case['selectors']]
    manifest = freeze_manifest(cases,exact,inputs={})
    assert manifest['policy']['catalog_profile'] == 'automatic'
    assert manifest['policy']['required_categories'] == sorted(AUTOMATIC_CATEGORIES)
    with pytest.raises(ValueError,match='inventory_incomplete'):
        validate_cases(cases[:-1])


def test_real_passed_process_without_original_reports_or_actual_agent_provenance_cannot_qualify(tmp_path):
    plan,directory = setup(tmp_path)
    observe(directory,'Build',observer_key=KEY)
    execution = _authenticated(directory/'execution.json',KEY)
    observations = [_authenticated(directory/'build.observation.json',KEY)]
    result = reconstruct(plan,execution,observations,observer_key=KEY)
    assert result['decision'] == 'not_qualified'
    assert result['denominators']['started_tasks'] == result['denominators']['terminal_tasks'] == 1
    assert result['denominators']['started_case_nodes'] is None and result['denominators']['native_attempts'] is None
    assert result['tasks'][0]['outcome'] == 'failed' and not result['evidence_accepted']


def test_missing_final_keeps_actual_signed_start_and_unknown_stop_denominator(tmp_path):
    plan,directory = setup(tmp_path)
    observe(directory,'Build',observer_key=KEY)
    (directory/'commands/build.receipt.json').unlink()
    result = reconstruct(plan,_authenticated(directory/'execution.json',KEY),
        [_authenticated(directory/'build.observation.json',KEY)],observer_key=KEY)
    assert result['decision'] == 'not_qualified'
    assert result['denominators']['started_tasks'] == 1
    assert result['denominators']['terminal_tasks'] == 0 and result['denominators']['started_stop_unknown_tasks'] == 1


def test_dated_preliminary_replay_excludes_only_later_verification_start(tmp_path):
    plan,directory = setup(tmp_path)
    sealed = seal(directory,observer_key=KEY,preliminary=True)
    observe(directory,'Verification',observer_key=KEY)
    original = replay(directory/'preliminary.json',observer_key=KEY,expected_report_sha256=sealed['report_sha256'])
    assert original['denominators']['started_tasks'] == 0
    current = reconstruct(plan,_authenticated(directory/'execution.json',KEY),[],observer_key=KEY)
    assert current['denominators']['started_tasks'] == 1
    assert current['denominators']['started_stop_unknown_tasks'] == 1


def verification_baseline():
    return {'preliminary':True,'decision':'not_qualified','failures':[],
        'gaps':['required_job_missing:Verification'], 'denominators':{'started_tasks':4,'terminal_tasks':4},
        'tasks':[{'role':role,'started':role != 'Verification','terminal_observed':role != 'Verification',
            'outcome':'accepted' if role != 'Verification' else 'externally_blocked',
            'reports':[{'kind':kind,'result':{'passed':True}} for kind in sorted(READERS[role])]
                if role != 'Verification' else []} for role in ROLES]}


@pytest.mark.parametrize('mutation',['positive','missing','failure','gap','start','terminal','producer'])
def test_independent_controls_need_positive_first_four_not_merely_negative_decision(mutation):
    baseline = verification_baseline()
    if mutation == 'missing':baseline['tasks'][0]['reports']=[]
    elif mutation == 'failure':baseline['failures']=['required_job_not_passed:Build']
    elif mutation == 'gap':baseline['gaps'].append('required_job_missing:Attack')
    elif mutation == 'start':baseline['tasks'][0]['started']=False
    elif mutation == 'terminal':baseline['tasks'][0]['terminal_observed']=False
    elif mutation == 'producer':baseline['tasks'][0]['reports'][0]['result']['passed']=False
    if mutation == 'positive':
        assert ready_for_verification(baseline)
    else:
        with pytest.raises(ValueError):ready_for_verification(baseline)


def test_canonical_replay_alone_cannot_omit_mandatory_independent_controls(tmp_path):
    preliminary = verification_baseline()
    report = {'schema_version':'qualification-verification/v1','plan_sha256':'plan',
        'preliminary_sha256':digest(preliminary),'decision':'not_qualified'}
    job = {'argv':[str(ROOT/'.venv/bin/python'),str(ROOT/'tools/qualify_unattended.py'),'verify'],
        'reports':[{'kind':'verification','path':str(tmp_path/'canonical.json')}]}
    with pytest.raises(ValueError,match='verification_report_shape'):
        checked_verification(report,job,{'repo_root':str(ROOT),'plan_sha256':'plan'},
            {'directory':str(tmp_path/'commands')},preliminary,KEY,{})


def test_changed_common_deadline_cannot_extend_original_frozen_allowance(tmp_path):
    plan,directory = setup(tmp_path)
    execution = _authenticated(directory/'execution.json',KEY)
    changed = {key:value for key,value in execution.items() if key != 'authentication'}
    changed['deadline_utc'] = '2099-01-01T00:00:00+00:00'
    with pytest.raises(ValueError,match='execution_original_changed'):
        reconstruct(plan,_signed(changed,KEY),[],observer_key=KEY)
    target = directory/'execution.json'
    target.write_text(json.dumps(_signed(changed,KEY)));target.chmod(0o600)
    with pytest.raises(ValueError,match='original_budget_changed'):
        reconstruct(plan,_authenticated(target,KEY),[],observer_key=KEY)


@pytest.mark.parametrize('change', ['exit','overflow','reader','meter','current','gap'])
def test_receipt_retention_does_not_promote_bad_original_process_to_accepted(tmp_path,change):
    plan,directory = setup(tmp_path)
    observe(directory,'Build',observer_key=KEY)
    from tools.measure_integration_costs import read_command_receipt
    execution = _authenticated(directory/'execution.json',KEY)
    observation = _authenticated(directory/'build.observation.json',KEY)
    checked = read_command_receipt(execution['cost_plan'],'build',plan['jobs'][0]['argv'],observer_key=KEY,
        directory=execution['directory'],work_root=execution['work_root'],expected_receipt_sha256=observation['receipt_sha256'])
    receipt = checked['receipt']
    if change == 'exit':receipt['exit_code']=1
    elif change == 'overflow':receipt['output_overflow']=True
    elif change == 'reader':receipt['output_reader_work_unresolved']=True
    elif change == 'meter':receipt['meter_work_unresolved']=True
    elif change == 'current':checked['current_command_manifest_matches']=False
    else:checked['coverage_gap']='unknown'
    intent = _authenticated(directory/'commands/build.intent.json',KEY)
    assert command_terminal(checked,intent=intent,deadline_utc=execution['deadline_utc']) != 'accepted'
