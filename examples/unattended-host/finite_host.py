"""Explicit finite calibrated fixture provision and same-service restoration."""

import copy
import hashlib
import json
import os
import stat
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fixture_host import FixtureMeter
from support import GRANTS, canonical, private_write

from evidence_wiki import (
    BoundedSubprocessExecutor,
    CalibratedEvaluators,
    CalibrationAuthority,
    CalibrationPlan,
    CalibrationSelection,
    DisagreementPlan,
    ExecutionAttempt,
    ExecutionProfile,
    ExecutionRole,
    ExecutionRoleRegistry,
    ExecutionRoleScope,
    HostReviewSigner,
    HostRuntime,
    ResearchExecution,
    RunPolicy,
    SemanticJudgingPlan,
    SigningIdentity,
    Workspace,
)
from evidence_wiki.finite_domain import DESCRIPTOR, DOMAIN
from evidence_wiki.host_context import HostContext
from evidence_wiki.qualification_calibration import calibration_suite, digest
from evidence_wiki.reference_oracles import protected_cases
from evidence_wiki.workflow_contracts import EXECUTION_OBSERVATION, JOB_V3

REVIEWERS = (('evidence', 'evaluator'), ('scope', 'evaluator'), ('critic', 'critic'), ('adjudicator', 'adjudicator'))


def provision(base, *, observation_sink=None):
    """Create the finite fixture once; restore retains its exact keys and IDs."""
    from fixture_host import provision as create
    return create(base, 'Assess the exact declared finite observation for fixture-cohort at fixture-observation.',
        finite=True, observation_sink=observation_sink)


def configuration(profile, config):
    role = profile.role(config)
    selected = profile.calibration.selection(config).document()
    return {'execution_profile_sha256': profile.sha256, 'config_ref': config,
        'controller_ref': role.controller_ref, 'model_ref': role.model_ref, 'prompt_version': role.prompt_version,
        'model_revision': selected['model_revision'], 'model_settings_sha256': digest(selected['model_settings']),
        'rubric_sha256': selected['rubric_sha256'], 'suite_sha256': selected['suite_sha256'], 'split_ref': selected['split_ref'],
        'reference_assessment_ref': selected['reference_assessment']['assessment_ref'],
        'domain_descriptor_sha256': digest(DESCRIPTOR), 'domains': [DOMAIN], 'active': True, 'execution_kind': 'model'}


def selections(document):
    root = Path(document['root'])
    worker = Path(document['worker'])
    required = [{'role': role, 'profile_ref': name, 'controller_ref': name+'-controller', 'human_required': False}
        for name, role in REVIEWERS if role != 'adjudicator']
    policy = RunPolicy(document['policy'] | {'required_evaluators': required, 'policy_id': None})
    suite = calibration_suite(protected_cases(), suite_ref='synthetic-finite-protected', split_ref='fixture-held-out-finite',
        domains=[DOMAIN], rubric=document['rubric'], model_settings={'execution': 'synthetic-protocol-observation', 'provider': 'none', 'temperature': '0'})
    revision = 'synthetic-program/'+hashlib.sha256(worker.read_bytes()).hexdigest()
    calibration = CalibrationPlan(tuple(CalibrationSelection(name, suite, model_revision=revision) for name, _role in REVIEWERS))
    roles = (ExecutionRole('generator','host_worker','generator-controller','synthetic/finite-generator-process','generate/v1'),
        ExecutionRole('planner','host_worker','planner-controller','synthetic/finite-planner-process','plan/v1'),
        *(ExecutionRole(name,'evaluator',name+'-controller','synthetic/'+name+'-process','semantic-review/v1') for name, _role in REVIEWERS))
    registry = ExecutionRoleRegistry((ExecutionRoleScope('generator','researcher',('model',)),
        ExecutionRoleScope('planner','planner',('model','plan.propose')),
        *(ExecutionRoleScope(name,role,('model',)) for name, role in REVIEWERS)))
    profile = ExecutionProfile('synthetic-finite-host',512,'EUR','synthetic-protocol-observation/v1',roles,1,1,1000,'0',1,
        role_scopes=registry, required_diversity=('control','context'), disagreement=DisagreementPlan('adjudicator'),
        calibration=calibration, semantic_judging=SemanticJudgingPlan('evidence','scope','critic'))
    keys = {name: bytes.fromhex(value) for name, value in document['keys'].items()}
    commands = {role.config_ref: [document['interpreter'], str(worker), role.config_ref] for role in roles}
    executor = BoundedSubprocessExecutor(profile,commands=commands,cwd=Path(document['workers']),receipt_key=keys['receipt'],meter=FixtureMeter())
    executor.calibration_configuration = lambda name: configuration(profile,name)
    executor.qualification_configuration = lambda request: {'request': copy.deepcopy(request), 'configuration': configuration(profile,request['config_ref'])}
    authority = CalibrationAuthority('fixture-finite-issuer',keys['qualifier'],'generator-controller')
    signer = HostReviewSigner('fixture-authority','1',SigningIdentity('generator','generator','generator-key','generator-controller','generator',keys['generator']),
        tuple(SigningIdentity(name,name,name+'-key',name+'-controller','evaluator',keys[name]) for name, _role in REVIEWERS),document['expires_utc'])
    context = HostContext([root],GRANTS | {'research_session.semantic_report','host_qualification.current','service_authentication.status'},'fixture-host',[root],
        authority_file=Path(document['private'])/'authority.json',state_directory=Path(document['private']),mutation_control_directory=Path(document['control']),
        controller_refs={'host_worker':'generator-controller','generator':'generator-controller','evaluator':'evidence-controller'},
        role_controller_refs=tuple((role.config_ref,role.role,role.controller_ref) for role in roles))
    return policy, profile, suite, executor, authority, signer, context


def provision_services(root, private, control, workers, keys, policy, rubric, until, *, observation_sink=None):
    document = {'root': str(root), 'private': str(private), 'control': str(control), 'workers': str(workers),
        'worker': str(Path(__file__).with_name('finite_workers.py').resolve()), 'interpreter': sys.executable,
        'policy': policy.to_dict(), 'rubric': rubric, 'expires_utc': until,
        'keys': {name: value.hex() for name, value in keys.items() if type(value) is bytes}, 'qualification_records': []}
    selected, profile, suite, executor, authority, _signer, _context = selections(document)
    job_budgets = profile.document()['job_budgets']
    private_write(private/'reference-observations.jsonl',b'')
    def retain(attempt, observed):
        descriptor = os.open(private/'reference-observations.jsonl',os.O_WRONLY|os.O_APPEND|os.O_NOFOLLOW)
        with os.fdopen(descriptor,'ab') as stream:
            stream.write(canonical({'attempt':attempt.document(),'observation':observed}))
            stream.flush();os.fsync(stream.fileno())
        if observation_sink is not None:
            observation_sink(attempt.document(),observed)
    for config, task_role in REVIEWERS:
        observations = []
        role = profile.role(config)
        scope = profile.role_scopes.scope(config)
        for index, case in enumerate(suite['cases']):
            instant = datetime.now(timezone.utc)
            job = {'schema_version':JOB_V3,'id':'reference-'+str(index),'operation':'evaluate','role':'evaluator',
                'controller_ref':role.controller_ref,'grant_ref':'fixture-model','input_id':case['case_id'],'basis_id':'fixture-basis',
                'policy_id':selected.policy_id,'policy_version':'1','prompt_version':role.prompt_version,'revision_precondition':'fixture-basis',
                'original_text':None,'scope':['q'],'required_capabilities':['model'],'payload':{'claim_id':'reference','packet_id':case['case_id']},
                'issued_utc':instant.isoformat(),'deadline_utc':(instant+timedelta(seconds=10)).isoformat(),'timeout_seconds':5,
                'max_attempts':1,'expected_result_schema':EXECUTION_OBSERVATION,'budgets':job_budgets,
                'qualifications':{'assurance':'artifact_checked','status':'unknown','evaluator_ref':None,'qualification_ref':None,'context_basis':None},
                'task_role':task_role,'role_scope_sha256':scope.sha256,'context_id':'fixture-reference-'+str(index),
                'packet_sha256':hashlib.sha256(canonical(case['input_document'])[:-1]).hexdigest()}
            attempt = ExecutionAttempt.create(attempt_id=config+'-reference-'+str(index),nonce='nonce-'+str(index),run_id='fixture-references',
                binding_id='fixture-finite-reference',profile=profile,config_ref=config,job=job,input_document=case['input_document'])
            # Select the current logical case identity through host-owned argv.
            reference_executor = BoundedSubprocessExecutor(profile, commands={config:[document['interpreter'],document['worker'],config,case['case_id']]},
                cwd=workers,receipt_key=keys['receipt'],meter=FixtureMeter())
            reference_executor.calibration_configuration = executor.calibration_configuration
            retain(attempt,{'state':'reserved','attempt_id':attempt.attempt_id,'start_observed':False})
            try:
                observed = reference_executor.invoke(attempt)
            except BaseException:
                retain(attempt,{'state':'dispatch_unknown','attempt_id':attempt.attempt_id,'start_observed':None})
                raise
            initial = reference_executor.authenticate(observed,attempt)
            retain(attempt,initial)
            while observed['state']=='running':
                time.sleep(0.005);observed=reference_executor.poll(attempt)
            authenticated = reference_executor.authenticate(observed,attempt)
            if authenticated != initial:
                retain(attempt,authenticated)
            observations.append((attempt,authenticated))
        record = authority.issue(selected,profile,config,suite,observations,executor,
            observed_utc=datetime.now(timezone.utc).isoformat(),expires_utc=until,verification_seconds=30,auxiliary=task_role=='adjudicator')
        if record['state'] != 'qualified':
            raise ValueError('Finite reference qualification is unavailable: '+','.join(record['reasons']))
        document['qualification_records'].append(record)
    private_write(private/'finite-services.json',canonical(document))
    factory = ('import sys\nfrom pathlib import Path\nsys.path.insert(0,'+repr(str(Path(__file__).parent))+')\n'
        'from finite_host import restore\ndef build_runtime(root):\n    return restore(root,Path('+repr(str(private/'finite-services.json'))+'))\n')
    private_write(private/'services.py',factory.encode())
    return restore(root,private/'finite-services.json')


def refresh_current(runtime, root, run_id):
    """Explicit trusted host work before reads; never renew a run's budget.

    Only an existing authenticated bound run is eligible. The native owner
    checks its original deadline, grants, source domain and current configuration
    and retains a new nonce-correlated witness with the unchanged short expiry.
    Read operations themselves remain free of callbacks and journal writes.
    """
    from evidence_wiki import calibration_probes

    with runtime.context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(runtime.policy, run_id=run_id, execution=runtime.execution) as session:
            if not runtime.execution.records(session):
                return False
            calibration_probes.refresh(session, 'host-current-' + os.urandom(16).hex())
    return True


def restore(root, path, *, current_run_id=None):
    supplied = Path(path).absolute()
    if '..' in supplied.parts or supplied.is_symlink():
        raise ValueError('Finite host configuration path is linked or unsafe.')
    descriptors = []
    try:
        descriptor = os.open(supplied.anchor,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        descriptors.append(descriptor)
        for part in supplied.parts[1:-1]:
            descriptor = os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=descriptor)
            descriptors.append(descriptor)
        descriptor = os.open(supplied.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=descriptor)
        descriptors.append(descriptor)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_mode & 0o077 or before.st_nlink != 1 or before.st_uid != os.geteuid() or before.st_size>4_194_304:
            raise ValueError('Finite host configuration is not private.')
        chunks, size = [], 0
        while chunk := os.read(descriptor,min(65_536,4_194_305-size)):
            chunks.append(chunk);size += len(chunk)
            if size>4_194_304:
                raise ValueError('Finite host configuration exceeds its bound.')
        raw = b''.join(chunks)
        after = os.fstat(descriptor)
        current = os.stat(supplied.name,dir_fd=descriptors[-2],follow_symlinks=False)
        fields = ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns','st_uid','st_mode','st_nlink')
        if any(tuple(getattr(value,key) for key in fields)!=tuple(getattr(before,key) for key in fields) for value in (after,current)):
            raise ValueError('Finite host configuration changed or exceeds its bound.')
        for parent,child,part in zip(descriptors[:-2],descriptors[1:-1],supplied.parts[1:-1],strict=True):
            named,held = os.stat(part,dir_fd=parent,follow_symlinks=False),os.fstat(child)
            if (named.st_dev,named.st_ino)!=(held.st_dev,held.st_ino):
                raise ValueError('Finite host configuration parent changed.')
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    document = json.loads(raw)
    if Path(document['root']).resolve()!=Path(root).resolve():
        raise ValueError('Finite factory root differs from the selected workspace.')
    policy, _profile, _suite, executor, authority, signer, context = selections(document)
    execution = ResearchExecution(executor,qualifications=CalibratedEvaluators(document['qualification_records'],authority),
        review_signer=signer,observer_key=bytes.fromhex(document['keys']['observer']))
    runtime = HostRuntime.from_services(policy,context,execution)
    if current_run_id is not None:
        refresh_current(runtime, root, current_run_id)
    return runtime
