"""Superseding work uses fresh native review without resetting terminal journals."""

import json

import pytest

from evidence_wiki import Workspace
from evidence_wiki.host_context import HostContext
from evidence_wiki.pack_discovery import owner
from tests.test_execution_session import fixture as make_fixture


@pytest.fixture
def fixture(tmp_path, monkeypatch, request):
    return make_fixture.__wrapped__(tmp_path, monkeypatch, request=request)


def selected(fixture):
    root, _policy, _execution, context = fixture
    return HostContext([root], context.grants | {"research_session.run_cycle", "research_session.refresh", "refresh_owner.apply", "research_session.brief", "research_session.resolution",
        "brief_operations.current", "resolution_operations.current"},
        context.actor_ref, [root], authority_file=context.authority_file, state_directory=context.state_directory,
        mutation_control_directory=context.mutation_control_directory, controller_refs=context.controller_refs,
        role_controller_refs=context.role_controller_refs)


@pytest.mark.parametrize("fixture", [{"planner": True, "role_isolation": True, "simulated_model_usage": True}], indirect=True)
def test_superseding_same_material_forces_fresh_reviews_and_preserves_parent(fixture):
    root, policy, execution, _ = fixture
    with selected(fixture).activate(), Workspace.open(root) as workspace:
        parent = workspace.research.run_cycle(policy, run_id="original-run", execution=execution,
            request_id="original-drive", max_steps=64)
        assert parent["outcome"] == "accepted", parent
        original = {path.relative_to(root).as_posix(): path.read_bytes() for path in (root / "runs/original-run").rglob('*') if path.is_file()}
        refreshed = workspace.research.refresh(policy, run_id="updated-run", previous_run_id="original-run", execution=execution,
            request_id="update-drive", planner_config_ref="planner", max_steps=64)
        assert refreshed["terminal"] and refreshed["outcome"] == "accepted", (refreshed["reason"], refreshed["cycle"]["step"], refreshed["refresh_id"])
        assert refreshed["refresh_id"] and refreshed["superseding_brief"]
        assert refreshed["superseding_brief"]["verdict"] == "accept"
        assert refreshed["cycle"]["current_basis_id"] != parent["current_basis_id"]
        assert refreshed["reused_source_ids"] and not refreshed["approval_reuse"]
        assert {path.relative_to(root).as_posix(): path.read_bytes() for path in (root / "runs/original-run").rglob('*') if path.is_file()} == original
        with workspace.research.session(policy, run_id="updated-run", execution=execution) as session:
            records = execution.records(session)
            assert any(row['kind'] == 'execution_planner_selection' and row['data']['owner_action'].get('refresh_request_id') for row in records)
            assert any(row['kind'] == 'execution_applied' and row['data'].get('operation') == 'generate' for row in records)
            assert any(row['kind'] == 'execution_applied' and row['data'].get('operation') == 'evaluate' for row in records)
        marker = json.loads((root / 'docs/research-refresh.json').read_bytes())
        assert marker['previous_run_id'] == 'original-run' and marker['run_id'] == 'updated-run'
        replay = workspace.research.refresh(policy, run_id="updated-run", previous_run_id="original-run", execution=execution,
            request_id="update-drive", planner_config_ref="planner", max_steps=64)
        assert replay['historical'] and replay['refresh_id'] == refreshed['refresh_id']


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


@pytest.mark.parametrize('value', [None, [None], [{}], [{'source_request_id':'x'}], [0]*17])
def test_invalid_update_shapes_refuse_without_host_work(value):
    from evidence_wiki.errors import EvidenceWikiError
    from evidence_wiki.refresh_session import _updates
    with pytest.raises(EvidenceWikiError):
        _updates(value)


@pytest.mark.parametrize(('cycle_outcome', 'verdict', 'expected'), [
    ('accepted', 'accept', ('accepted', 'native_reason')),
    ('accepted', 'partial', ('partial', 'superseding_brief_partial')),
    ('accepted', 'abstain', ('abstained', 'superseding_brief_abstain')),
    ('accepted', 'reject', ('abstained', 'superseding_brief_reject')),
    ('accepted', None, ('blocked_external', 'superseding_brief_unavailable')),
    ('partial', 'accept', ('partial', 'native_reason')),
    ('stop_budget', 'accept', ('stop_budget', 'native_reason')),
    ('blocked_external', 'accept', ('blocked_external', 'native_reason')),
])
def test_current_brief_cannot_revive_recorded_acceptance(cycle_outcome, verdict, expected):
    from evidence_wiki.refresh_contracts import current_outcome
    assert current_outcome({'outcome': cycle_outcome, 'reason': 'native_reason'},
        None if verdict is None else {'verdict': verdict}) == expected


def test_active_original_run_blocks_refresh_even_for_same_actor():
    from types import SimpleNamespace

    from evidence_wiki.errors import EvidenceWikiError
    from evidence_wiki.refresh_owner import _check_active_runs
    capture = SimpleNamespace(files={'runs/other/run-state.json': json.dumps({
        'state': {'current': 'answering'}, 'research_session': {'original_ids': ['q'], 'actor_ref': 'runner'}}).encode()})
    with pytest.raises(EvidenceWikiError) as caught:
        _check_active_runs(capture, {'run_id': 'new', 'original_ids': ['q']})
    assert caught.value.error_code == 'RESEARCH_ACTIVE_ORIGINAL_RUN'


@pytest.mark.parametrize('failed', [False, True])
def test_frozen_source_update_uses_native_pipeline_and_retains_refusal(monkeypatch, failed):
    from contextlib import nullcontext
    from types import SimpleNamespace

    from evidence_wiki import refresh_session
    from evidence_wiki.errors import EvidenceWikiError, UsageError
    called, retained = [], []
    def acquire(**arguments):
        called.append(arguments)
        if failed:
            raise UsageError('SOURCE_UNAVAILABLE', 'No retained update.', recoverable=False)
        return {'status': 'fulfilled'}
    session = SimpleNamespace(execution=SimpleNamespace(records=lambda _session: [
        {'kind': 'execution_refresh', 'data': {'request_id': 'refresh'}}]), acquire_evidence=acquire)
    monkeypatch.setattr(refresh_session.native, 'gate', lambda _session: nullcontext())
    monkeypatch.setattr(refresh_session.native, 'append', lambda *args: retained.append(args[-1]))
    update = {'source_request_id': 'source', 'expected_request_basis': 'sha256:' + 'a'*64,
        'metadata': {'retained': 'opaque'}, 'path': 'raw/web/source.txt'}
    if failed:
        with pytest.raises(EvidenceWikiError):
            refresh_session.collect(session, update)
        assert retained == [{'source_request_id': 'source', 'status': 'refused', 'error_code': 'SOURCE_UNAVAILABLE'}]
    else:
        refresh_session.collect(session, update)
        assert retained[0]['status'] == 'fulfilled'
    assert {key: value for key, value in called[0].items() if key != 'request_id'} == update
