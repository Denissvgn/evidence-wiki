"""Changed retained sources drive new superseding cycles without rewriting earlier runs.

Five native research drives run in sequence here, so this module forms a suite group of its own.
"""

import json

import pytest

from evidence_wiki import Workspace
from evidence_wiki.pack_discovery import owner
from tests.test_refresh_workflow import fixture as fixture
from tests.test_refresh_workflow import selected


def rotate_retained_source(root, *, suffix, request_id):
    """Host supplies a newly permitted immutable local revision, retaining originals."""
    from tests._execution_fixture import authenticate, closure
    from tests._execution_fixture import canonical as source_bytes
    usage = owner('_evidence_usage')
    config = owner('_strict_evidence').configuration(root)
    with usage.current_view(root, config) as live:
        prior = next(row for row in reversed(list(live.state.revisions.values())) if row['descriptor']['normalized_path'])
        descriptor = dict(prior['descriptor'])
        files = dict(prior['files'])
        grant, scrub = dict(prior['grant']['payload']), dict(prior['scrub']['payload'])
        checkpoint = live.state.checkpoint
    normalized = descriptor['normalized_path']
    import yaml
    front, body, error = owner('_normalized_contract').split_record(files[normalized].decode())
    assert error is None
    front['title'] += ' revision ' + suffix
    files[normalized] = ('---\n' + yaml.safe_dump(front, sort_keys=False) + '---' + files[normalized].decode().split('---', 2)[2]).encode()
    files['source-record.json'] = source_bytes(descriptor)
    revision = closure(files)
    grant['source_revision'] = revision
    scrub['sanitized_revision'] = revision
    body = {'source_id': descriptor['source_id'], 'source_revision': revision,
        'grant': authenticate(grant, 'owner', 'usage'), 'scrub': authenticate(scrub, 'runner', 'scrubber')}
    command = authenticate({'schema_version': 'evidence-usage-command/v1', 'state_id': usage.selection(config),
        'workspace_binding': usage.workspace_binding(root), 'request_id': request_id,
        'expected_checkpoint': checkpoint, 'action': 'deposit', 'body': body}, 'owner', 'usage')
    usage.transact(root, config, command, files)
    (root / normalized).write_bytes(files[normalized])
    return revision


@pytest.mark.parametrize('fixture', [{'planner': True, 'role_isolation': True, 'simulated_model_usage': True}], indirect=True)
def test_changed_source_cycles_preserve_history_and_measure_new_work(fixture):
    root, policy, execution, _ = fixture
    from evidence_wiki.agent_execution import BoundedSubprocessExecutor
    from evidence_wiki.research_execution import ResearchExecution
    from tests.test_execution_session import HostProtocolSimulationMeter
    commands = dict(execution.executor._commands)
    argv = list(commands['researcher'])
    source = argv[-1]
    source = source.replace('print(json.dumps(g))',
        "[e.update(record_sha256=d['sources'][0]['record_sha256']) for c in g['proposal']['inventory']['claims'] for e in c['evidence']]; print(json.dumps(g))")
    if source == argv[-1]:
        # The ordinary fixture uses a literal proposal expression.
        source = "import json,sys; d=json.load(sys.stdin)['input']; g=" + repr(json.loads(__import__('subprocess').check_output(argv, input=b'{}'))) + "; [e.update(record_sha256=d['sources'][0]['record_sha256']) for c in g['proposal']['inventory']['claims'] for e in c['evidence']]; print(json.dumps(g))"
    argv[-1] = source
    commands['researcher'] = tuple(argv)
    executor = BoundedSubprocessExecutor(execution.profile, commands=commands, cwd=execution.executor._cwd,
        receipt_key=b'e' * 32, meter=HostProtocolSimulationMeter())
    execution = ResearchExecution(executor, qualifications=execution.qualifications,
        review_signer=execution.review_signer, observer_key=b'o' * 32)
    with selected(fixture).activate(), Workspace.open(root) as workspace:
        parent_id = 'baseline-run'
        baseline = workspace.research.run_cycle(policy, run_id=parent_id, execution=execution,
            request_id='baseline-drive', max_steps=64)
        assert baseline['outcome'] == 'accepted'
        prior_basis = baseline['current_basis_id']
        for index in range(2):
            before = {p.relative_to(root).as_posix(): p.read_bytes() for p in (root/'runs'/parent_id).rglob('*') if p.is_file()}
            rotate_retained_source(root, suffix=str(index), request_id='revision-' + str(index))
            run_id = 'refresh-' + str(index)
            result = workspace.research.refresh(policy, run_id=run_id, previous_run_id=parent_id, execution=execution,
                request_id='refresh-drive-' + str(index), planner_config_ref='planner', max_steps=64)
            assert result['outcome'] == 'accepted', result['reason']
            assert result['changed_source_ids'] and not result['reused_source_ids']
            assert result['cycle']['current_basis_id'] != prior_basis
            assert result['superseding_brief']['verdict'] == 'accept'
            assert result['usage']['tool_calls'] > 0 and result['previous_usage']['tool_calls'] > 0
            assert {p.relative_to(root).as_posix(): p.read_bytes() for p in (root/'runs'/parent_id).rglob('*') if p.is_file()} == before
            prior_basis = result['cycle']['current_basis_id']
            parent_id = run_id
        failed_commands = {**commands, 'researcher': (commands['researcher'][0], '-c', 'raise SystemExit(1)')}
        failed_executor = BoundedSubprocessExecutor(execution.profile, commands=failed_commands,
            cwd=execution.executor._cwd, receipt_key=b'e' * 32, meter=HostProtocolSimulationMeter())
        failed_execution = ResearchExecution(failed_executor, qualifications=execution.qualifications,
            review_signer=execution.review_signer, observer_key=b'o' * 32)
        failure = workspace.research.refresh(policy, run_id='failed-model', previous_run_id=parent_id,
            execution=failed_execution, request_id='failed-model-drive', planner_config_ref='planner')
        assert failure['terminal'] and failure['outcome'] not in {'accepted', 'partial'}
        assert failure['usage']['model_calls'] > 0
        assert failure['superseding_brief']['verdict'] == 'abstain'
        from tests.test_native_capture_revision import metadata
        update = {'source_request_id': 'missing-request', 'expected_request_basis': 'sha256:' + 'a'*64,
            'metadata': metadata('plain_text', request_id='missing-request', scope={'facet_id': 'primary'},
                origin_url='https://example.org/source'), 'path': 'raw/web/new.txt'}
        source_failure = workspace.research.refresh(policy, run_id='failed-source', previous_run_id='failed-model',
            execution=execution, request_id='failed-source-drive', planner_config_ref='planner', source_updates=[update])
        assert source_failure['outcome'] not in {'accepted', 'partial'}
        assert source_failure['unresolved_source_update_request_ids'] == ['missing-request']
        assert source_failure['source_updates'][0]['status'] == 'refused'
