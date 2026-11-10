"""Superseding work uses fresh native review without resetting terminal journals."""

import json

import pytest

from evidence_wiki import Workspace
from evidence_wiki.host_context import HostContext
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
