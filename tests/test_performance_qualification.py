"""Native synthetic measurements and independent authenticated accounting checks."""

import sys
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from evidence_wiki import Workspace
from evidence_wiki.agent_execution import BoundedSubprocessExecutor, ExecutionRole
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.execution_roles import ExecutionRoleRegistry, ExecutionRoleScope
from evidence_wiki.host_context import HostContext
from evidence_wiki.performance_contracts import REPORT, THRESHOLDS, catalog, checked, gate
from evidence_wiki.performance_probe import run as probe
from evidence_wiki.research_execution import ResearchExecution
from evidence_wiki.run_policy import RunPolicy
from tests.test_execution_session import HostProtocolSimulationMeter
from tests.test_execution_session import fixture as execution_fixture
from tests.test_research_session import policy_document


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    root, policy, original, context = execution_fixture.__wrapped__(tmp_path, monkeypatch,
        SimpleNamespace(param={'role_isolation': True, 'simulated_model_usage': True}))
    roles = (*original.profile.roles, ExecutionRole('benchmark', 'host_worker', 'benchmark-controller', 'simulated/benchmark', 'performance/v1'))
    scopes = tuple(ExecutionRoleScope(scope.config_ref, scope.task_role,
        ('model', 'verdict.propose') if scope.config_ref == 'profile-a' else scope.capabilities)
        for scope in original.profile.role_scopes.scopes)
    scopes += (ExecutionRoleScope('benchmark', 'integration', ('model', 'integration.propose', 'offline_check')),)
    profile = replace(original.profile, roles=roles, role_scopes=ExecutionRoleRegistry(scopes))
    selector = "import json,sys; d=json.load(sys.stdin)['input']; print(json.dumps({'schema_version':'research-performance-selection/v1','catalog_id':d['catalog_id'],'workload_ids':[x['workload_id'] for x in reversed(d['catalog'])]}))"
    critic = "import json,sys,hashlib,math; d=json.load(sys.stdin)['input']; rows=d['measurements']; h='sha256:'+hashlib.sha256(json.dumps(rows,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest(); problems=[]; problems.extend(['measurement_count'] if len(rows)!=len(d['expected_workloads']) else []); problems.extend(['samples'] if any(len(r['samples'])!=4 or not r['complete'] or r['truncated'] for r in rows) else []); print(json.dumps({'schema_version':'research-performance-audit/v1','measurement_id':h,'complete':h==d['measurement_id'],'issues':problems}))"
    commands = {**original.executor._commands, 'benchmark': (sys.executable, '-c', selector), 'profile-a': (sys.executable, '-c', critic)}
    executor = BoundedSubprocessExecutor(profile, commands=commands, cwd=original.executor._cwd,
        receipt_key=b'e'*32, meter=HostProtocolSimulationMeter())
    execution = ResearchExecution(executor, qualifications=original.qualifications, review_signer=original.review_signer, observer_key=b'o'*32)
    selected = HostContext([root], context.grants | {'performance_qualification.run'}, context.actor_ref, [root],
        authority_file=context.authority_file, state_directory=context.state_directory, mutation_control_directory=context.mutation_control_directory,
        controller_refs=context.controller_refs, role_controller_refs=tuple((role.config_ref,role.role,role.controller_ref) for role in roles))
    return root, policy, execution, selected


def test_real_qualification_native_bounds_agent_selection_critic_replay_and_read_only_science(fixture):
    root, policy, execution, context = fixture
    before = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    with context.activate(), Workspace.open(root) as workspace:
        report = workspace.research.qualify_performance(policy, execution=execution, request_id='performance',
            benchmark_config_ref='benchmark', critic_config_ref='profile-a', seed=13)
        assert report['status'] == 'passed', (report['status'], report['reason'], report['violations'], report['agent_accounting'])
        assert report['complete'] and report['performance_qualified'] and len(report['measurements']) == 12
        assert report['measurements'][0]['workload_id'] == report['workloads'][-1]['workload_id']
        assert sum(row['observed_usage']['model_calls'] for row in report['agent_accounting']) == 2
        assert checked(REPORT, report) == report and not report['authorized'] and not report['evidence_accepted']
        assert all(row['partition'] and not row['partition']['complete'] for row in report['measurements'] if row['samples'][0]['outcome']=='refused')
        replay = workspace.research.qualify_performance(policy, execution=execution, request_id='performance',
            benchmark_config_ref='benchmark', critic_config_ref='profile-a', seed=13)
        assert replay['historical'] and replay['measurements'] == report['measurements']
        with pytest.raises(EvidenceWikiError):
            workspace.research.qualify_performance(policy, execution=execution, request_id='performance',
                benchmark_config_ref='benchmark', critic_config_ref='profile-a', seed=14)
    assert {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()} == before


@pytest.fixture
def measured():
    rows = catalog(RunPolicy(policy_document()), seed=9)
    results = [probe(row) for row in rows]
    from evidence_wiki.mutation_contracts import digest
    audit = {'schema_version': 'research-performance-audit/v1', 'measurement_id': digest(results), 'complete': True, 'issues': []}
    return rows, results, audit, {'machine':'arm64','platform':'local','python':'3','cpu_count':1}, [{'complete':True, 'observed_usage':{'seconds':1,'model_calls':1,'tool_calls':1,'tokens':0,'cost':'0'}}]*2


@pytest.mark.parametrize('fault', ['missing', 'unknown', 'truncated', 'memory', 'serialized', 'critic', 'sample', 'partition'])
def test_deterministic_gate_never_qualifies_incomplete_or_over_threshold_evidence(measured, fault):
    rows, results, audit, hardware, accounting = deepcopy(measured)
    if fault == 'missing': results.pop()
    elif fault == 'unknown': results[0]['samples'][0]['validation_seconds'] = None
    elif fault == 'truncated': results[0]['truncated'] = True
    elif fault == 'memory': results[0]['samples'][0]['traced_peak_bytes'] = THRESHOLDS['traced_peak_bytes']+1
    elif fault == 'serialized': results[0]['samples'][0]['serialized_bytes'] = None
    elif fault == 'critic': audit['complete'] = False
    elif fault == 'sample': results[0]['samples'].pop()
    else: results[-1]['partition'] = None
    outcome = gate(rows, results, audit, hardware, accounting)
    assert outcome['status'] != 'passed' and not outcome['performance_qualified']


@pytest.mark.parametrize('seed', [0, 1, 23, 65535])
def test_randomized_bounded_native_workloads_and_updates_reproduce_exact_refusals(seed):
    for row in catalog(RunPolicy(policy_document()), seed=seed):
        result = probe(row)
        assert result['complete'] and not result['truncated']
        assert all(sample['outcome'] == row['expected'] for sample in result['samples']), result
        assert [sample['revision'] for sample in result['samples']] == [0,0,1,2]


@pytest.mark.parametrize('value', [None, float('nan'), float('inf'), -1, True])
def test_legacy_scale_gate_cannot_accept_unknown_or_nonfinite_metrics(value):
    from tests.test_scale_smoke import SCALE
    timings = dict.fromkeys(['inventory','normalization','lint','workspace_status','workspace_status_cached','index_build','indexed_query','total'], 0.01)
    timings['total'] = value
    quality = dict.fromkeys(['counts_complete','normalization_complete','lint_clear','status_complete','query_complete'], True)
    result = SCALE.evaluate_release_budget(SCALE.BENCHMARK_PROFILES['standard'], timings, peak_memory_bytes=1, output_bytes=1, quality=quality)
    assert result['verdict'] == 'no_ship' and not result['complete']


def test_legacy_scale_gate_requires_complete_counts_and_quality_not_only_fast_timings():
    from tests.test_scale_smoke import SCALE
    timings = dict.fromkeys(['inventory','normalization','lint','workspace_status','workspace_status_cached','index_build','indexed_query','total'], 0.01)
    assert SCALE.evaluate_release_budget(SCALE.BENCHMARK_PROFILES['standard'], timings, peak_memory_bytes=1, output_bytes=1)['verdict']=='no_ship'


@pytest.mark.parametrize('fault', ['unknown_model', 'missing_selection', 'critic_disagrees', 'owner_missing', 'unknown_usage'])
def test_authenticated_workflow_faults_never_turn_missing_data_into_qualification(fixture, monkeypatch, fault):
    from evidence_wiki import performance_qualification as module
    root, policy, execution, context = fixture
    actual = module._agent
    def altered(store, execution, state, policy, config, packet, deadline):
        if fault == 'unknown_model':
            raise RuntimeError('Unknown external dispatch.')
        result = actual(store, execution, state, policy, config, packet, deadline)
        if config == 'benchmark' and fault == 'missing_selection':
            result['workload_ids'].pop()
        if config == 'profile-a' and fault == 'critic_disagrees':
            result['issues'] = ['Incomplete accounting view']
        if config == 'profile-a' and fault == 'unknown_usage':
            state['attempts'][config]['observation']['observed_usage']['cost'] = None
        return result
    monkeypatch.setattr(module, '_agent', altered)
    if fault == 'owner_missing':
        monkeypatch.setattr(module.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=b''))
    with context.activate(), Workspace.open(root) as workspace:
        result = workspace.research.qualify_performance(policy, execution=execution, request_id='fault-'+fault,
            benchmark_config_ref='benchmark', critic_config_ref='profile-a')
        assert result['status'] != 'passed' and not result['performance_qualified']
        assert not result['authorized'] and not result['evidence_accepted']


def test_missing_retention_cannot_reset_or_redispatch_old_performance_work(fixture):
    import hashlib
    root, policy, execution, context = fixture
    with context.activate(), Workspace.open(root) as workspace:
        result = workspace.research.qualify_performance(policy, execution=execution, request_id='retained',
            benchmark_config_ref='benchmark', critic_config_ref='profile-a')
        assert result['status']=='passed'
        state = context.mutation_control_directory/'mutations/workspaces'/hashlib.sha256(str(root).encode()).hexdigest()/'performance-runs'/hashlib.sha256(b'retained').hexdigest()/'state.json'
        state.unlink()
        with pytest.raises(EvidenceWikiError):
            workspace.research.qualify_performance(policy, execution=execution, request_id='retained',
                benchmark_config_ref='benchmark', critic_config_ref='profile-a')
        assert not state.exists()
