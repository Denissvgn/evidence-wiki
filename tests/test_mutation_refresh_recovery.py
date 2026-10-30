"""Lost refresh returns prove desired state without inventing completion."""

import pytest

from evidence_wiki import Workspace
from evidence_wiki import execution_session as native
from evidence_wiki import mutation_operations as operations
from tests.test_refresh_workflow import fixture as fixture
from tests.test_refresh_workflow import selected


def test_unknown_parent_accounting_stops_before_material_preparation(monkeypatch):
    from evidence_wiki import refresh_session
    monkeypatch.setattr(native, '_terminal_action', lambda session, view, outcome, reason: (outcome, reason))
    result = refresh_session.next_action(None, {}, [
        {'kind': 'execution_refresh', 'data': {'parent_usage_unknown': True}}])
    assert result == ('stop_budget', 'usage_unknown')


def test_distinct_update_requests_cannot_share_an_output_path():
    from evidence_wiki.errors import EvidenceWikiError
    from evidence_wiki.refresh_session import _updates
    from tests.test_native_capture_revision import metadata
    rows = [{'source_request_id': identifier, 'expected_request_basis': 'sha256:' + 'a'*64,
        'metadata': metadata('plain_text', request_id=identifier, scope={'facet_id': 'primary'},
            origin_url='https://example.org/source'), 'path': 'raw/web/shared.txt'}
        for identifier in ('one', 'two')]
    with pytest.raises(EvidenceWikiError) as caught:
        _updates(rows)
    assert caught.value.error_code == 'RESEARCH_REFRESH_ARGUMENT_INVALID'


def test_report_withholds_retained_source_receipts_and_currently_denied_ids(monkeypatch):
    from types import SimpleNamespace

    from evidence_wiki import refresh_session
    rows = [{'kind': 'execution_refresh_collection', 'data': {'source_request_id': 'requested',
        'status': 'fulfilled', 'outcome': {'protected': 'receipt and source material'}}}]
    session = SimpleNamespace(run_id='new', execution=SimpleNamespace(records=lambda _session: rows))
    previous = {'parent_binding_id': 'binding', 'parent_terminal_ref': 'terminal', 'parent_outcome': 'accepted',
        'reused_source_ids': ['permitted', 'denied'], 'changed_source_ids': ['unavailable'], 'parent_usage': {}}
    cycle = {'terminal': True, 'outcome': 'stop_budget', 'reason': 'usage_unknown', 'historical': True, 'usage': {}}
    monkeypatch.setattr(refresh_session, 'checked', lambda value: value)
    result = refresh_session._report(session, 'refresh', 'parent', previous, cycle, [], None, None, {'permitted'})
    assert result['reused_source_ids'] == ['permitted'] and result['changed_source_ids'] == []
    assert result['source_updates'] == [{'source_request_id': 'requested', 'status': 'fulfilled'}]


def test_closing_brief_custody_failure_discards_constructed_acceptance(monkeypatch):
    from contextlib import contextmanager, nullcontext
    from types import SimpleNamespace

    from evidence_wiki import brief_operations, execution_planning, refresh_session
    from evidence_wiki.errors import UsageError
    instruction = {'request_id': 'refresh', 'arguments': {'previous_run_id': 'parent', 'max_steps': 64,
        'planner_config_ref': 'planner', 'source_updates': []}}
    session = SimpleNamespace(run_id='new', _check=lambda *args, **kwargs: None, execution=object(),
        context=SimpleNamespace(require_grant=lambda _grant: None), run_cycle=lambda **kwargs: {})
    monkeypatch.setattr(execution_planning, 'configured', lambda *args: None)
    monkeypatch.setattr(native, 'gate', lambda _session: nullcontext())
    monkeypatch.setattr(native, 'retained', lambda _session: [{'kind': 'execution_refresh', 'data': instruction}])
    @contextmanager
    def held(_session):
        yield {'verdict': 'accept'}, SimpleNamespace(source_packets=lambda: [{'source_id': 'permitted'}])
        raise UsageError('CURRENT_CUSTODY_CHANGED', 'Closing inputs changed.', recoverable=False)
    monkeypatch.setattr(brief_operations, '_held_brief', held)
    calls = []
    def report(*arguments):
        brief, error, permitted = arguments[-3:]
        calls.append((brief, error, permitted))
        return {'accepted': brief is not None}
    monkeypatch.setattr(refresh_session, '_report', report)
    result = refresh_session.refresh(session, request_id='refresh', previous_run_id='parent', planner_config_ref='planner')
    assert result == {'accepted': False}
    assert calls == [({'verdict': 'accept'}, None, {'permitted'}), (None, 'CURRENT_CUSTODY_CHANGED', set())]


@pytest.mark.parametrize('field', ['run_id', 'previous_run_id', 'request_id'])
@pytest.mark.parametrize('value', ['/outside', '../outside', 'bad/id', ''])
def test_native_refresh_rejects_path_like_identities_before_owner_reads(field, value):
    from evidence_wiki.mutation_contracts import MutationError, arguments
    supplied = {'run_id': 'new', 'previous_run_id': 'old', 'request_id': 'refresh',
        'expected_basis_id': 'sha256:' + 'a'*64, 'expected_marker_sha256': None, 'original_ids': ['q']}
    supplied[field] = value
    with pytest.raises(MutationError):
        arguments('questions.refresh', supplied)


def test_refresh_lost_return_retains_preimages_and_proves_only_desired_state(fixture, monkeypatch):
    root, policy, execution, _context = fixture
    with selected(fixture).activate(), Workspace.open(root) as workspace:
        parent = workspace.research.run_cycle(policy, run_id='parent', execution=execution,
            request_id='parent-drive', max_steps=1)
        assert parent['terminal'] and parent['outcome'] == 'stop_budget'
        with workspace.research.session(policy, run_id='new', execution=execution) as session:
            with native.gate(session):
                native._begin(session)
                current = native.current(session)
            request = workspace.mutations.prepare(request_id='refresh-effect', operation='questions.refresh', arguments={
                'run_id': 'new', 'previous_run_id': 'parent', 'request_id': 'refresh',
                'expected_basis_id': current['basis_id'], 'expected_marker_sha256': None,
                'original_ids': session.binding['original_ids']})
            original = operations._invoke
            def lost_return(*arguments):
                original(*arguments)
                raise RuntimeError('Synthetic interruption after canonical publication.')
            with monkeypatch.context() as interrupted:
                interrupted.setattr(operations, '_invoke', lost_return)
                receipt = workspace.mutations.submit(request)
            assert receipt['status'] == 'commit_unknown'
            marker = (root / 'docs/research-refresh.json').read_bytes()
            proof = workspace.mutations.reconcile('refresh-effect')
            assert proof['desired_state_proved'] is True and proof['completion_proved'] is False
            assert proof['proof_kind'] == 'refresh_postconditions'
            assert workspace.mutations.inspect('refresh-effect')['record']['receipt'] == receipt
            assert workspace.mutations.submit(request) == receipt
            assert (root / 'docs/research-refresh.json').read_bytes() == marker
