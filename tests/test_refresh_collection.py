"""Refresh collection joins actual transport, capture and source owners."""

from evidence_wiki import Workspace, refresh_session
from evidence_wiki.mutation_contracts import digest
from evidence_wiki.pack_discovery import owner
from tests.test_acquisition_dispatch import opener
from tests.test_acquisition_workflow_adversarial import configured as configured
from tests.test_native_capture_revision import metadata


def test_refresh_collects_permitted_update_without_refetch_on_workflow_replay(configured, monkeypatch):
    root, policy, execution, context = configured
    transport = owner('_acquisition_transport')
    called = []
    monkeypatch.setattr(transport, 'execute_planned_request', opener(transport.execute_planned_request,
        b'Measured reflectance was 0.74 ratio with retained original context.\n', called,
        root, context.mutation_control_directory))
    with context.activate(), Workspace.open(root) as workspace:
        source = workspace.source_requests.add(kind='web', query_or_identifier='https://example.org/source',
            rationale='Need scoped retained original', question_slugs=['q'], scope={'facet_id': 'primary'})['request']
        update = {'source_request_id': source['request_id'], 'expected_request_basis': digest(source),
            'metadata': metadata('plain_text', request_id=source['request_id'], scope={'facet_id': 'primary'},
                origin_url='https://example.org/source'), 'path': 'raw/web/refreshed.txt'}
        with workspace.research.session(policy, run_id='collect-update', execution=execution) as session:
            with refresh_session.native.gate(session):
                refresh_session.native._begin(session)
                refresh_session.native.append(session, 'execution_refresh', 'refresh', {'request_id': 'refresh'})
            refresh_session.collect(session, update)
            refresh_session.collect(session, update)
            collections = [row['data'] for row in execution.records(session) if row['kind'] == 'execution_refresh_collection']
            assert len(collections) == 1 and collections[0]['status'] == 'fulfilled'
            assert called == ['https://example.org/source']
            assert (root / update['path']).read_bytes().startswith(b'Measured reflectance')
            current = workspace.source_requests.current(request_ids=[source['request_id']])['requests'][0]
            assert current['recorded_status'] == 'fulfilled' and current['current_ready']
