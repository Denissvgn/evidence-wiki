"""Validated immutable digests leave live lease and authentication checks intact."""

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from evidence_wiki import agent_execution as owner
from evidence_wiki.agent_execution import ExecutionAttempt, ExecutionError, ExecutionProfile, ExecutionRole, _json
from evidence_wiki.execution_roles import ExecutionRoleRegistry, ExecutionRoleScope
from evidence_wiki.workflow_contracts import (
    EXECUTION_OBSERVATION,
    JOB,
    JOB_V2,
    JOB_V3,
    JOB_V4,
    WorkflowContractError,
    document_sha256,
)
from tests.test_agent_execution import executor
from tests.test_agent_execution import profile as local_profile
from tests.test_workflow_contracts import job


def declared(version=JOB):
    document = job()
    inputs = {'packet':{'text':'Original α text.\nKeep literal spacing.  ','rows':[{'value':1.25}]}}
    profile = local_profile()
    if version != JOB:
        document.update(schema_version=version,expected_result_schema=EXECUTION_OBSERVATION)
    if version in {JOB_V3,JOB_V4}:
        task = 'calculation' if version == JOB_V4 else 'researcher'
        scope = ExecutionRoleScope('researcher',task,['model'])
        profile = ExecutionProfile('scoped-host',5,'EUR','declared-price',
            [ExecutionRole('researcher','host_worker','registered-controller','declared-model','prompt/v1')],
            0,5,0,'0',role_scopes=ExecutionRoleRegistry([scope]))
        document.update(task_role=task,role_scope_sha256=scope.sha256,context_id='current-context',
            packet_sha256=hashlib.sha256(_json(inputs)).hexdigest(),required_capabilities=['model'])
    attempt = ExecutionAttempt.create(attempt_id='owned-attempt',nonce='owned-nonce',run_id='owned-run',binding_id='owned-binding',
        profile=profile,config_ref='researcher',job=document,input_document=inputs)
    return profile,document,inputs,attempt


@pytest.mark.parametrize('version',[JOB,JOB_V2,JOB_V3,JOB_V4])
def test_attempt_digests_equal_original_canonical_owner_for_every_job_version(version):
    profile,document,inputs,attempt = declared(version)
    assert attempt.job_sha256 == document_sha256(version,document)
    assert attempt.input_sha256 == hashlib.sha256(_json(inputs)).hexdigest()
    direct = ExecutionAttempt(attempt.attempt_id,attempt.nonce,attempt.run_id,attempt.binding_id,
        profile.sha256,attempt.config_ref,_json(document),_json(inputs))
    assert direct == attempt and direct.job_sha256 == attempt.job_sha256 and direct.input_sha256 == attempt.input_sha256
    identity = (attempt.job_sha256,attempt.input_sha256)
    document['payload'].clear();inputs['packet']['rows'][0]['value']=999
    returned_job,returned_input = attempt.job,attempt.input_document
    returned_job['qualifications'].clear();returned_input['packet'].clear()
    assert (attempt.job_sha256,attempt.input_sha256) == identity
    assert attempt.job['payload'] and attempt.input_document['packet']['rows'][0]['value'] == 1.25


@pytest.mark.parametrize('change',['same','job','input'])
def test_attempt_replacement_revalidates_and_recomputes_changed_declarations(change):
    _profile,_job,_input,original = declared()
    if change == 'same':new=replace(original)
    elif change == 'job':new=replace(original,_job_bytes=_json(original.job|{'prompt_version':'updated-prompt'}))
    else:new=replace(original,_input_bytes=_json({'packet':{'text':'Different original bytes.'}}))
    assert new.job_sha256 == document_sha256(new.job['schema_version'],new.job)
    assert new.input_sha256 == hashlib.sha256(_json(new.input_document)).hexdigest()
    if change == 'same':
        assert new == original and (new.job_sha256,new.input_sha256) == (original.job_sha256,original.input_sha256)
    elif change == 'job':
        assert new.job_sha256 != original.job_sha256 and new.input_sha256 == original.input_sha256
    else:
        assert new.input_sha256 != original.input_sha256 and new.job_sha256 == original.job_sha256


@pytest.mark.parametrize('change',['job_type','input_type','noncanonical','extra','version','input_shape','deep_input'])
def test_direct_attempt_constructor_rejects_invalid_bytes_before_digest_retention(change):
    _profile,_job,_input,original = declared()
    job_bytes,input_bytes = original._job_bytes,original._input_bytes
    if change == 'job_type':job_bytes=bytearray(job_bytes)
    elif change == 'input_type':input_bytes=bytearray(input_bytes)
    elif change == 'noncanonical':job_bytes=json.dumps(original.job,indent=2).encode()
    elif change == 'extra':job_bytes=_json(original.job|{'unexpected':True})
    elif change == 'version':job_bytes=_json(original.job|{'schema_version':'unsupported'})
    elif change == 'input_shape':input_bytes=b'[]'
    else:input_bytes=('{"nested":'*18+'null'+'}'*18).encode()
    expected_error = WorkflowContractError if change == 'extra' else ExecutionError
    with pytest.raises(expected_error):
        ExecutionAttempt(original.attempt_id,original.nonce,original.run_id,original.binding_id,
            original.profile_sha256,original.config_ref,job_bytes,input_bytes)


def test_warm_attempt_digest_access_does_not_decode_or_revalidate_immutable_bytes(monkeypatch):
    _profile,_job,_input,attempt = declared()
    expected = (attempt.job_sha256,attempt.input_sha256)
    def fail(*_args,**_kwargs):
        pytest.fail('A validated immutable digest was rebuilt.')
    monkeypatch.setattr(owner,'validate_document',fail)
    monkeypatch.setattr(owner.json,'loads',fail)
    monkeypatch.setattr(owner.hashlib,'sha256',fail)
    for _ in range(5):assert (attempt.job_sha256,attempt.input_sha256) == expected


def test_warm_digests_do_not_suppress_receipt_tampering_or_current_lease_refusal(tmp_path,monkeypatch):
    profile,document,inputs,_attempt = declared()
    instant = datetime.now(timezone.utc)
    document.update(issued_utc=(instant-timedelta(seconds=1)).isoformat(),deadline_utc=(instant+timedelta(seconds=10)).isoformat(),timeout_seconds=5)
    attempt = ExecutionAttempt.create(attempt_id='live-attempt',nonce='live-nonce',run_id='live-run',binding_id='live-binding',
        profile=profile,config_ref='researcher',job=document,input_document=inputs)
    host = executor(tmp_path,'unused',selected=profile,receipt_key=b'local-attempt-fixture-receipt-0000')
    receipt = host._envelope(attempt,sequence=1,started_utc=document['issued_utc'],details={
        'state':'succeeded','reason':'completed','completed_utc':document['issued_utc'],'output':{},
        'observed_usage':{'seconds':0,'model_calls':0,'tool_calls':0,'tokens':0,'cost':'0'},
        'final_accounting':True,'cancellation_confirmed':False})
    identity = (attempt.job_sha256,attempt.input_sha256)
    assert host.authenticate(receipt,attempt) == receipt
    with pytest.raises(ExecutionError,match='authentication_refused'):
        host.authenticate(receipt|{'authentication':'0'*64},attempt)
    changed = replace(attempt,_job_bytes=_json(attempt.job|{'prompt_version':'changed-prompt'}))
    with pytest.raises(ExecutionError,match='role_mismatch'):host.authenticate(receipt,changed)
    monkeypatch.setattr(owner,'_now',lambda:(instant+timedelta(seconds=30)).isoformat())
    monkeypatch.setattr(owner.subprocess,'Popen',lambda *_args,**_kwargs:pytest.fail('Expired work dispatched.'))
    with pytest.raises(WorkflowContractError) as caught:host.invoke(attempt)
    assert caught.value.error_code == 'WORKFLOW_DEADLINE_EXPIRED'
    assert (attempt.job_sha256,attempt.input_sha256) == identity


def test_warm_declared_hashes_do_not_admit_changed_scoped_input(tmp_path):
    from tests._usage_fixture import require_reference_executor
    require_reference_executor()
    profile,_job,_inputs,attempt = declared(JOB_V3)
    host = owner.BoundedSubprocessExecutor(profile,commands={},cwd=tmp_path,receipt_key=b'scoped-attempt-fixture-key-000000')
    receipt = host._envelope(attempt,sequence=1,started_utc=attempt.job['issued_utc'],details={
        'state':'succeeded','reason':'completed','completed_utc':attempt.job['issued_utc'],'output':{},
        'observed_usage':{'seconds':0,'model_calls':0,'tool_calls':0,'tokens':0,'cost':'0'},
        'final_accounting':True,'cancellation_confirmed':False})
    assert host.authenticate(receipt,attempt) == receipt
    changed = replace(attempt,_input_bytes=_json({'packet':{'text':'Changed evidence input.'}}))
    assert changed.job_sha256 == attempt.job_sha256 and changed.input_sha256 != attempt.input_sha256
    with pytest.raises(ExecutionError,match='context_mismatch'):host.authenticate(receipt,changed)
