"""Host-owned canonical attestations and explicit public API selections."""

import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta, timezone

from evidence_wiki import RunPolicy
from evidence_wiki.run_policy import CONTINUATION_OUTCOMES, EFFECTS, TERMINAL_OUTCOMES


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)+'\n').encode()


def identity(domain, value):
    return 'sha256:'+hashlib.sha256(domain.encode()+b'\0'+canonical(value)).hexdigest()


def sha(data):
    return 'sha256:'+hashlib.sha256(data).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def expiry():
    return (datetime.now(timezone.utc)+timedelta(hours=2)).isoformat()


def private_write(path, data):
    descriptor = os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(data)


def attest(payload, principal, role, keys, *, policy='fixture-authority'):
    authentication = {'schema_version':'evidence-authentication/v1','scheme':'hmac-sha256',
        'principal':principal,'key_id':principal+'-key','role':role,'policy_id':policy,'policy_revision':'1',
        'issued_at':now(),'expires_at':keys.get('expires_at',expiry())}
    message = b'evidence-attestation/v1\0'+canonical({'payload':payload,'authentication':authentication})
    authentication['signature'] = hmac.new(keys[principal],message,hashlib.sha256).hexdigest()
    return {'payload':payload,'authentication':authentication}


GRANTS = {
    'workspace.open','workspace_status.run_status_report','source_requests.run_list_requests',
    'source_inventory.run_inventory_report','normalize_sources.run_normalization_report',
    'strict_evidence.run_operation.check','strict_evidence.run_operation.export',
    'strict_evidence.run_operation.prepare-review','strict_evidence.run_operation.review','strict_evidence.run_admit_document',
    'research_session.current','research_session.next_work','research_session.session','research_session.advance',
    'research_session.run_until_terminal','research_session.run_cycle','research_session.refresh','refresh_owner.apply',
    'research_session.host_job','research_session.host_job.generate','research_session.host_job.evaluate','research_session.host_job.acquire',
    'research_session.pending','research_session.cancel_pending','research_session.reconcile_pending',
    'research_session.intake_observation','research_session.report','telemetry.report','research_session.recovery_status',
    'research_session.brief','research_session.resolution','brief_operations.current','resolution_operations.current',
    'run_controller.run_session_start','run_controller.run_session_record','run_controller.run_session_transition',
    'run_controller.run_session_finish','question_claim.run_claim','question_resolve.run_answer',
    'coverage_manifest.run_link','coverage_manifest.run_evaluate','evidence_usage.run_transact','evidence_usage.run_status',
    'mutation_operations.revision','mutation_operations.prepare_request','mutation_operations.submit',
    'mutation_operations.inspect','mutation_operations.reconcile',
    'source_capture_operations.deliver','source_capture_operations.prepare',
}


def policy(root, objective):
    effects = list(EFFECTS)
    return RunPolicy({'schema_version':'research-run-policy/v2','policy_id':None,'objective':objective,
        'scope':{'question_ids':['q'],'source_scope':[str(root/'raw')],'source_scope_kind':'native_extent'},
        'allowed_effects':effects,'host_capability_refs':{name:'fixture-'+name for name in effects if name!='offline_check'},
        'assurance':'artifact_checked','human_review':False,
        'required_evaluators':[{'role':'evaluator','profile_ref':'evidence','controller_ref':'evidence-controller','human_required':False},
            {'role':'critic','profile_ref':'critic','controller_ref':'critic-controller','human_required':False}],
        'source_rules':{'require_current':True,'require_present':True,'require_complete_context':True,'allow_partial':False},
        'thresholds':{'critical_false_acceptances_max':0,'min_reference_cases':100,'min_families':10,
            'positive_acceptance_min':'0.90','meaning_preserving_flip_max':'0.05'},
        'budgets':{'seconds':1800,'model_calls':100,'tool_calls':200,'tokens':1000000,'cost_limit':'0',
            'questions':1,'claims':10,'sources':10},
        'continuation_limits':{'repair_cycles':2,'adjudication_cycles':1,'retry_attempts':1,'acquisition_attempts':0},
        'terminal_outcomes':list(TERMINAL_OUTCOMES),
        'continuation_outcomes':list(CONTINUATION_OUTCOMES)})


def setup_request(base, objective):
    question = {'question_id':'q','template':None,'facets':[{'facet_id':'primary','description':'Scoped retained observation',
        'required':True,'evidence_path':'official_guidance','source_policy':'primary_or_official',
        'freshness_policy':'no_staleness_check','identity_policy':'none','min_sources':1}],
        'criteria':[{'facet_id':'primary','source_classes':['primary study'],'required_scope':[],
            'time':'Capture date only','units':'Unitless reflectance','counterevidence':'Retain contradictory interpretations',
            'stopping':'Every original has reviewed support or an explicit gap','inference':'Label any inference','quantitative':None}]}
    return {'schema_version':'evidence-research-setup/v1','request':{'schema_version':'2.0',
        'kind':'research_request','request_id':'fixture-setup','payload':{'goal':objective,
            'questions':[{'id':'q','text':objective}],'derived_questions':[],
            'target':{'writable_root':str(base),'relative_path':'workspace'},'outputs':['json'],
            'scope':[{'name':'subject','value':'Synthetic local observation'}],
            'domain':{'mode':'none','pack':None,'rationale':'Fixture mechanics only; no production semantics.'},
            'sources':[],'host_tools':[],
            'authority':{'role':'caller','reference':'fixture-host','allowed_actions':['local_setup','local_research'],
                'source_scope':[str(base/'workspace/raw')],'writable_roots':[str(base)],'credential_references':[]},
            'budgets':{'questions':1,'source_requests':0,'downloads':0,'bytes':1000000,'seconds':1800},
            'assumptions':['Synthetic evidence only.'],'open_decisions':[],
            'strict_evidence':{'mode':'strict','assurance':'artifact_checked','policy_id':'fixture-evidence','policy_revision':'1'}}},
        'decisions':{'question_plans':[question]}}
