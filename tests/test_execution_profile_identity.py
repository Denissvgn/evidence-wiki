"""Declared profile identity stays detached; current checks remain live."""

import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

import pytest

from evidence_wiki import agent_execution as owner
from evidence_wiki.agent_execution import ExecutionProfile, ExecutionRole
from evidence_wiki.calibration_registry import CalibrationPlan, CalibrationSelection
from evidence_wiki.disagreement_configuration import DisagreementPlan
from evidence_wiki.execution_roles import ExecutionRoleRegistry, ExecutionRoleScope
from evidence_wiki.protected_handoff import NativeInputHandoff
from evidence_wiki.qualification_calibration import calibration_suite
from evidence_wiki.reference_oracles import DOMAIN, protected_cases
from evidence_wiki.review_delivery import ReviewDelivery
from evidence_wiki.semantic_contracts import SemanticJudgingPlan
from evidence_wiki.service_repair_binding import ServiceRepairBinding
from evidence_wiki.workflow_contracts import WorkflowContractError
from tests.test_agent_execution import attempt, executor
from tests.test_agent_execution import profile as local_profile
from tests.test_artifact_delivery_contracts import selection as delivery_profile
from tests.test_disagreement_configuration import simple_profile
from tests.test_execution_scheduling import profile as scheduled_profile
from tests.test_readability_profile import profile as readability_profile
from tests.test_service_repair_profile import configured as service_profile


def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


def base_document(selected,version):
    return {'schema_version':'research-execution-profile/v'+str(version),'profile_ref':selected.profile_ref,
        'total_steps':selected.total_steps,'currency':selected.currency,'pricing_ref':selected.pricing_ref,
        'roles':[role.document() for role in selected.roles],
        'job_budgets':{'model_calls':selected.model_calls,'tool_calls':selected.tool_calls,'tokens':selected.tokens,'cost':selected.cost_limit},
        'cleanup_timeout_seconds':selected.cleanup_timeout_seconds}


@pytest.fixture(scope='module')
def declarations():
    suite = calibration_suite(protected_cases(),suite_ref='declared-reference',split_ref='held-out-reference',domains=[DOMAIN],
        rubric={'id':'source-rubric','revision':'1','criteria':{'support':'Check the exact source.'}},model_settings={'temperature':'0'})
    selections = {config:CalibrationSelection(config,suite,model_revision='declared-model-revision')
        for config in ('profile-a','critic','evidence','scope','counter','investigator')}
    scoped = simple_profile()
    calibrated = replace(scoped,calibration=CalibrationPlan([selections['profile-a'],selections['critic']]))
    disagreement = replace(scoped,disagreement=DisagreementPlan())
    calculation = replace(scoped,roles=(*scoped.roles,ExecutionRole('calculator','host_worker','calculator-controller','calculator-model','calculate/v1')),
        role_scopes=ExecutionRoleRegistry((*scoped.role_scopes.scopes,ExecutionRoleScope('calculator','calculation',['model','calculation.propose']))))
    services = service_profile()
    plan = DisagreementPlan()
    repaired = replace(services,disagreement=plan,service_repair=ServiceRepairBinding(services.review_services.sha256,'sha256:'+plan.sha256))
    readable = readability_profile.__wrapped__()[1]
    delivered = delivery_profile()[1]
    roles = [ExecutionRole('researcher','host_worker','research-controller','research-model','research/v1'),
        *(ExecutionRole(config,'evaluator',config+'-controller',config+'-model','review/v1')
            for config in ('evidence','scope','counter','investigator'))]
    capabilities = ['model']
    scopes = [ExecutionRoleScope(config,task,capabilities) for config,task in
        (('researcher','researcher'),('evidence','evaluator'),('scope','evaluator'),('counter','critic'),('investigator','adjudicator'))]
    selected_calibration = [selections[config] for config in ('evidence','scope','counter','investigator')]
    semantic = ExecutionProfile('semantic-profile',100,'EUR','declared-price',roles,1,1,50000,'1',
        role_scopes=ExecutionRoleRegistry(scopes),calibration=CalibrationPlan(selected_calibration),
        disagreement=DisagreementPlan('investigator'),semantic_judging=SemanticJudgingPlan('evidence','scope','counter'))
    profiles = {1:local_profile(),2:scheduled_profile(),3:scoped,4:calibrated,5:disagreement,6:calculation,
        7:replace(scoped,review_delivery=ReviewDelivery()),8:services,9:repaired,
        10:replace(disagreement,input_handoff=NativeInputHandoff()),11:readable,12:delivered,13:semantic}
    return profiles,{'suite':suite,'roles':roles,'scopes':scopes,'capabilities':capabilities,'selections':selected_calibration}


@pytest.mark.parametrize('version',range(1,14))
def test_supported_profile_canonical_documents_and_digests_preserve_declared_fields(declarations,version):
    selected = declarations[0][version]
    expected = base_document(selected,version)
    for attribute,key in (('schedule','scheduling'),('role_scopes','role_scopes'),('calibration','calibration'),
        ('disagreement','disagreement'),('review_delivery','review_delivery'),('review_services','review_services'),
        ('service_repair','service_repair'),('input_handoff','input_handoff'),('readability','readability'),
        ('artifact_delivery','artifact_delivery'),('semantic_judging','semantic_judging')):
        nested = getattr(selected,attribute)
        if nested is not None:expected[key]=nested.document()
    if selected.role_scopes is not None:expected['required_diversity']=list(selected.required_diversity)
    if version == 13:
        assessments = {}
        for selection in expected['calibration']['selections']:
            assessment = selection.pop('reference_assessment')
            reference = hashlib.sha256(canonical(assessment)).hexdigest()
            assessments[reference]=assessment
            selection['reference_assessment_ref']=reference
        expected['calibration']['reference_assessments']=assessments
    assert selected.document() == expected
    assert selected.sha256 == hashlib.sha256(canonical(expected)).hexdigest()
    assert selected._document_bytes == canonical(expected)
    def corrupt(value):
        if type(value) is dict:
            for child in list(value.values()):corrupt(child)
            value.clear()
        elif type(value) is list:
            for child in value:corrupt(child)
            value.clear()
    detached = selected.document()
    corrupt(detached)
    assert selected.document() == expected
    assert selected.sha256 == hashlib.sha256(canonical(expected)).hexdigest()


def test_declared_identity_retains_no_caller_collection_or_returned_document_alias(declarations):
    selected = declarations[0][13]
    inputs = declarations[1]
    before,identity = selected.document(),selected.sha256
    inputs['roles'].clear();inputs['scopes'].clear();inputs['capabilities'].append('ungranted')
    inputs['selections'].clear()
    inputs['suite']['model_settings']['temperature']='different'
    inputs['suite']['cases'][0]['input_document']['claim']['value']='999'
    returned = selected.document()
    returned['roles'][0]['controller_ref']='forged'
    next(iter(returned['calibration']['reference_assessments'].values()))['results'].clear()
    assert selected.document() == before and selected.sha256 == identity
    with pytest.raises(FrozenInstanceError):selected.roles[0].controller_ref='forged'
    assert all(type(selection._document_bytes) is bytes for selection in selected.calibration.selections)
    assert type(selected.roles) is type(selected.calibration.selections) is type(selected.role_scopes.scopes) is tuple


def test_repeated_declared_identity_never_regenerates_nested_assessments(declarations,monkeypatch):
    selected = declarations[0][13]
    before,identity = selected.document(),selected.sha256
    def fail(*_args,**_kwargs):
        pytest.fail('A validated declared profile was rebuilt.')
    monkeypatch.setattr(CalibrationPlan,'document',fail)
    monkeypatch.setattr(ExecutionProfile,'_declared_document',fail)
    for _ in range(5):
        assert selected.sha256 == identity and selected.document() == before
    detached = selected.document();detached['calibration'].clear()
    assert selected.document() == before


@pytest.mark.parametrize('change',['same','budget','model','controller','calibration'])
def test_replace_revalidates_and_creates_the_exact_new_declared_identity(declarations,change):
    selected = declarations[0][13]
    before,identity = selected.document(),selected.sha256
    if change == 'same':new=replace(selected)
    elif change == 'budget':new=replace(selected,tokens=selected.tokens+1)
    elif change in {'model','controller'}:
        field = 'model_ref' if change == 'model' else 'controller_ref'
        roles = tuple(replace(role,**{field:'new-identity'}) if role.config_ref == 'researcher' else role for role in selected.roles)
        new=replace(selected,roles=roles)
    else:new=replace(selected,calibration=replace(selected.calibration,probe_seconds=4.0))
    if change == 'same':
        assert new.document() == before and new.sha256 == identity
    else:
        assert new.document() != before and new.sha256 != identity
    assert new.sha256 == hashlib.sha256(canonical(new.document())).hexdigest()
    assert selected.document() == before and selected.sha256 == identity


def test_changed_declared_authority_role_creates_a_new_identity():
    selected = local_profile()
    replacement = replace(selected,roles=(replace(selected.roles[0],role='evaluator'),))
    assert replacement.document()['roles'][0]['role'] == 'evaluator'
    assert replacement.sha256 != selected.sha256
    assert selected.document()['roles'][0]['role'] == 'host_worker'


def test_cached_declaration_does_not_cache_current_semantic_source_probe(declarations,monkeypatch):
    from evidence_wiki import semantic_contracts
    selected = declarations[0][13]
    identity = selected.sha256
    original = semantic_contracts._protocol_hashes()
    monkeypatch.setattr(semantic_contracts,'_protocol_hashes',lambda:((original[0][0],'0'*64),*original[1:]))
    assert selected.sha256 == identity
    with pytest.raises(owner.ExecutionError,match='protocol_changed'):selected.semantic_judging.check_profile(selected)
    with pytest.raises(owner.ExecutionError,match='protocol_changed'):replace(selected,total_steps=selected.total_steps+1)


def test_cached_profile_does_not_cache_receipt_authentication_or_current_job_expiry(tmp_path,monkeypatch):
    selected = local_profile()
    host = executor(tmp_path,'unused',selected=selected,receipt_key=b'declared-profile-fixture-key-0000')
    value = attempt(selected)
    receipt = host._envelope(value,sequence=1,started_utc=value.job['issued_utc'],details={
        'state':'succeeded','reason':'completed','completed_utc':value.job['issued_utc'],'output':{},
        'observed_usage':{'seconds':0,'model_calls':0,'tool_calls':0,'tokens':0,'cost':'0'},
        'final_accounting':True,'cancellation_confirmed':False})
    identity = selected.sha256
    assert host.authenticate(receipt,value) == receipt
    forged = dict(receipt,authentication='0'*64)
    with pytest.raises(owner.ExecutionError,match='authentication_refused'):host.authenticate(forged,value)
    assert host.authenticate(receipt,value) == receipt and selected.sha256 == identity
    future = (datetime.now(timezone.utc)+timedelta(seconds=30)).isoformat()
    monkeypatch.setattr(owner,'_now',lambda:future)
    monkeypatch.setattr(owner.subprocess,'Popen',lambda *_args,**_kwargs:pytest.fail('Expired work was dispatched.'))
    with pytest.raises(WorkflowContractError) as caught:host.invoke(value)
    assert caught.value.error_code == 'WORKFLOW_DEADLINE_EXPIRED'
    assert selected.sha256 == identity
