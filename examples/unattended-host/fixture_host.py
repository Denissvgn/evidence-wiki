"""Explicit isolated fixture provisioning through public APIs and host contracts."""

import json
import secrets
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from support import GRANTS, attest, canonical, expiry, identity, now, policy, private_write, setup_request, sha

from evidence_wiki import (
    BoundedSubprocessExecutor,
    DisagreementPlan,
    ExecutionAttempt,
    ExecutionProfile,
    ExecutionRole,
    ExecutionRoleRegistry,
    ExecutionRoleScope,
    HostReviewSigner,
    HostRuntime,
    NativeInputHandoff,
    Onboarding,
    QualificationAuthority,
    QualifiedEvaluators,
    ResearchExecution,
    SigningIdentity,
    Workspace,
)
from evidence_wiki.agent_qualification import reference_suite
from evidence_wiki.host_context import HostContext
from evidence_wiki.workflow_contracts import EXECUTION_OBSERVATION, JOB_V3


class FixtureMeter:
    """Synthetic model accounting for protocol mechanics; never real provider usage."""

    def finalize(self, _attempt, summary):
        return {'seconds':summary['seconds'],'model_calls':1,'tool_calls':1,'tokens':0,'cost':'0'}


def provision(base, objective, *, scenario='repair'):
    base = base.resolve()
    if base.exists():
        raise ValueError('Fixture output must be a new directory; use live mode to reopen a provisioned runtime.')
    base.mkdir(mode=0o700,parents=True)
    private = base/'fixture-private'; private.mkdir(mode=0o700)
    control = base/'fixture-control'; control.mkdir(mode=0o700)
    workers = base/'workers'; workers.mkdir(mode=0o700)
    keys = {principal:secrets.token_bytes(32) for principal in ('generator','evidence','critic','usage','scrubber','receipt','observer','qualifier')}
    until = expiry()
    keys['expires_at'] = until
    with Onboarding.open(allowed_roots=[base],allow=['apply']) as onboarding:
        prepared = onboarding.plan(setup_request(base,objective))
        setup = onboarding.apply(prepared)
    if not setup['setup_ready']:
        raise ValueError('Fixture setup refused.')
    root = base/'workspace'
    (root/'sources/jurisdictions.yml').write_text(yaml.safe_dump({'jurisdiction_profiles':[{
        'jurisdiction_id':'fixture-only','official_domains':['example.org'],'blocked_domains':[]}]}))
    raw = b'Measured solar reflectance was 0.74.\n'
    metadata = {'schema_version':'evidence-source-capture-input/v1','capture_id':'fixture-study','tool_id':'fixture-input','tool_version':'1',
        'origin_url':'https://example.org/study','title':'Synthetic local observation','retrieved_at':now(),
        'capture_method':'manual_transcription','content_format':'markdown','content_kind':'primary','completeness':'complete',
        'completeness_note':'Complete synthetic fixture text.','rights':{'status':'allowed','license':'CC0-1.0','terms_url':None,'note':'Fixture only.'},
        'scope':{},'request_id':None,'parents':[],'sanitization':{'required':False,'approved_revision_id':None,'receipt_ref':None}}
    intake_context = HostContext([root],GRANTS,'fixture-host',[root],mutation_control_directory=control)
    with intake_context.activate(), Workspace.open(root) as workspace:
        delivery = workspace.sources.capture_bytes_document(raw,metadata)
        request = workspace.sources.prepare_capture(request_id='fixture-source',path='raw/papers/study.md',delivery=delivery)
        captured = workspace.sources.capture_revision(request)
        workspace.sources.inventory()
        workspace.normalize.sources(all_sources=True)
    records = [json.loads(line) for line in (root/'sources/manifest.jsonl').read_text().splitlines()]
    source_id = records[0]['id']
    normalized = next(path for path in (root/'sources/normalized').glob('*.md') if source_id in path.read_text())
    config_path = root/'research.yml'
    config = yaml.safe_load(config_path.read_text())
    config['evidence_trust'] = {'policy_id':'fixture-authority','policy_revision':'1'}
    config['evidence_usage'] = {'state_id':'fixture-evidence'}
    policy_doc = config['strict_evidence']
    policy_doc['instructions'] = {'AGENTS.md':sha((root/'AGENTS.md').read_bytes()),
        'docs/research-requirements.json':sha((root/'docs/research-requirements.json').read_bytes())}
    config_path.write_text(yaml.safe_dump(config,sort_keys=False))
    role_map = {'generator':('generator-controller',['generator']), 'evidence':('evidence-controller',['evaluator']),
        'critic':('critic-controller',['evaluator']), 'usage':('usage-controller',['usage']), 'scrubber':('scrub-controller',['scrubber'])}
    authority = {'schema_version':'evidence-trust-policy/v1','policy_id':'fixture-authority','policy_revision':'1',
        'not_before':now(),'expires_at':until,
        'principals':{principal:{'controller':controller,'roles':roles,'keys':{principal+'-key':keys[principal].hex()}}
            for principal,(controller,roles) in role_map.items()},'revoked_keys':[],'revoked_envelopes':[]}
    authority_path = private/'authority.json'
    private_write(authority_path,canonical(authority))
    run_policy = policy(root,objective)
    roles = (ExecutionRole('generator','host_worker','generator-controller','simulated/fixture-generator','generate/v1'),
        ExecutionRole('evidence','evaluator','evidence-controller','simulated/fixture-evidence','review/v1'),
        ExecutionRole('critic','evaluator','critic-controller','simulated/fixture-critic','review/v1'),
        ExecutionRole('repairer','host_worker','generator-controller','simulated/fixture-repair','repair/v1'),
        ExecutionRole('planner','host_worker','planner-controller','simulated/fixture-planner','plan/v1'))
    registry = ExecutionRoleRegistry(tuple(ExecutionRoleScope(role.config_ref,task,('model','plan.propose') if task=='planner' else ('model',))
        for role,task in zip(roles,('researcher','evaluator','critic','repair','planner'),strict=True)))
    profile = ExecutionProfile('fixture-only',512,'EUR','simulated-no-provider-charge/v1',roles,1,1,1000,'0',1,
        role_scopes=registry,required_diversity=('control','context'),disagreement=DisagreementPlan(),input_handoff=NativeInputHandoff())
    context = HostContext([root],GRANTS,'fixture-host',[root],authority_file=authority_path,state_directory=private,
        mutation_control_directory=control, controller_refs={'host_worker':'generator-controller','generator':'generator-controller','evaluator':'evidence-controller'},
        role_controller_refs=tuple((role.config_ref,role.role,role.controller_ref) for role in roles))
    descriptor = {'schema_version':'evidence-source-revision/v1','source_id':source_id,'parents':[],
        'normalized_path':normalized.relative_to(root).as_posix(),'evidence_root':'evidence','temporal':{}}
    files = {'source-record.json':canonical(descriptor),descriptor['normalized_path']:normalized.read_bytes()}
    files.update({'evidence/'+path.relative_to(root).as_posix():path.read_bytes() for path in (root/'raw').rglob('*')
        if path.is_file() and not any(part.startswith('.') for part in path.relative_to(root).parts)})
    revision = identity('evidence-artifact-closure/v1',{name:{'content_hash':sha(data),'size_bytes':len(data)} for name,data in files.items()})
    redaction = {'id':'fixture-scrub','revision':'1'}
    grant = {'schema_version':'evidence-usage-grant/v1','source_id':source_id,'source_revision':revision,
        'permissions':dict.fromkeys(('retrieval','training','export'),True),'purposes':['research','qa-export'],'consumers':['evidence-wiki'],
        'not_before':now(),'expires_at':until,'redaction_policy':redaction,'retention':'host-managed'}
    scrub = {'schema_version':'evidence-scrub-receipt/v1','sanitized_revision':revision,'redaction_policy':redaction,
        'tool':{'name':'fixture-host-scrubber','version':'1'},'outcome':'passed','completed_at':now()}
    checkpoint = None
    with context.activate(), Workspace.open(root) as workspace:
        for index,(action,body,artifacts) in enumerate((('initialize',{},None),('deposit',{
                'source_id':source_id,'source_revision':revision,'grant':attest(grant,'usage','usage',keys),
                'scrub':attest(scrub,'scrubber','scrubber',keys)},files))):
            command = {'schema_version':'evidence-usage-command/v1','state_id':'fixture-evidence',
                'workspace_binding':identity('evidence-host-workspace/v1',{'root':str(root)}),'request_id':'fixture-usage-'+str(index),
                'expected_checkpoint':checkpoint,'action':action,'body':body}
            receipt = workspace.usage.transact(attest(command,'usage','usage',keys),artifacts=artifacts)
            checkpoint = receipt['checkpoint']
    worker = Path(__file__).with_name('fake_workers.py').resolve()
    commands = {role.config_ref:[sys.executable,str(worker),'generator' if role.config_ref=='planner' else role.config_ref,scenario] for role in roles}
    issuer = QualificationAuthority('fixture-only-issuer',keys['qualifier'],'generator-controller')
    cases = [{'case_id':'case-'+str(i),'family_id':'family-'+str(i//10),
        'input_document':{'case_id':'case-'+str(i),'supported':i//2%2==0,'wording':i%2},
        'expected_label':'accept' if i//2%2==0 else 'reject','critical':i//2%2!=0,'pair_id':'pair-'+str(i//2)} for i in range(100)]
    suite = reference_suite(cases,suite_ref='fixture-only',split_ref='fixture-held-out',domain_ref='synthetic-local',rubric=policy_doc['rubric'])
    qualifications = []
    for config_ref in ('evidence','critic'):
        calibration = BoundedSubprocessExecutor(profile,commands=commands,cwd=workers,receipt_key=keys['receipt'],meter=FixtureMeter())
        observations = []
        role = profile.role(config_ref)
        for index,case in enumerate(cases):
            instant = datetime.now(timezone.utc)
            job = {'schema_version':JOB_V3,'id':'reference-'+str(index),'operation':'evaluate','role':'evaluator',
                'controller_ref':role.controller_ref,'grant_ref':'fixture-model','input_id':case['case_id'],'basis_id':'fixture-basis',
                'policy_id':run_policy.policy_id,'policy_version':'1','prompt_version':role.prompt_version,'revision_precondition':'fixture-basis',
                'original_text':None,'scope':['q'],'required_capabilities':['model'],'payload':{'claim_id':'reference','packet_id':case['case_id']},
                'issued_utc':instant.isoformat(),'deadline_utc':(instant+timedelta(seconds=10)).isoformat(),'timeout_seconds':5,
                'max_attempts':1,'expected_result_schema':EXECUTION_OBSERVATION,'budgets':profile.document()['job_budgets'],
                'qualifications':{'assurance':'artifact_checked','status':'unknown','evaluator_ref':None,'qualification_ref':None,'context_basis':None},
                'task_role':registry.scope(config_ref).task_role,'role_scope_sha256':registry.scope(config_ref).sha256,
                'context_id':'fixture-reference-'+str(index),'packet_sha256':sha(canonical(case['input_document'])[:-1])[7:]}
            attempt = ExecutionAttempt.create(attempt_id='reference-'+str(index),nonce='nonce-'+str(index),run_id='fixture-references',
                binding_id='fixture-reference-binding',profile=profile,config_ref=config_ref,job=job,input_document=case['input_document'])
            observed = calibration.invoke(attempt)
            while observed['state']=='running':
                time.sleep(0.005);observed=calibration.poll(attempt)
            observations.append((attempt,observed))
        qualifications.append(issuer.issue(run_policy,profile,config_ref,suite,cases,observations,calibration,expires_utc=until))
    executor = BoundedSubprocessExecutor(profile,commands=commands,cwd=workers,receipt_key=keys['receipt'],meter=FixtureMeter())
    signer = HostReviewSigner('fixture-authority','1',SigningIdentity('generator','generator','generator-key','generator-controller','generator',keys['generator']),
        tuple(SigningIdentity(name,name,name+'-key',profile.role(name).controller_ref,'evaluator',keys[name]) for name in ('evidence','critic')),until)
    runtime = HostRuntime.from_services(run_policy,context,ResearchExecution(executor,qualifications=QualifiedEvaluators(qualifications,issuer),
        review_signer=signer,observer_key=keys['observer']))
    return root,runtime,{'setup_ready':True,'source_admitted':captured.get('state',captured.get('status')),'scenario':scenario}
