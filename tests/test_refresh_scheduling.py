"""Real private host queues retain budgets and native refresh ownership."""

from datetime import datetime, timedelta, timezone

import pytest

from evidence_wiki import Workspace
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.host_context import HostContext
from evidence_wiki.refresh_schedule_contracts import NOTICE, STATUS, checked
from tests.test_refresh_workflow import fixture as fixture
from tests.test_refresh_workflow import selected as refresh_context


def selected(fixture):
    root, _policy, _execution, _context = fixture
    context = refresh_context(fixture)
    return HostContext([root], context.grants | {"refresh_scheduling."+name for name in ("register", "current", "poll", "advance")},
        context.actor_ref, [root], authority_file=context.authority_file, state_directory=context.state_directory,
        mutation_control_directory=context.mutation_control_directory, controller_refs=context.controller_refs,
        role_controller_refs=context.role_controller_refs)


def configuration():
    return {"schedule_id": "host-refresh", "previous_run_id": "parent", "planner_config_ref": "planner",
        "interval_seconds": 30, "expires_at": (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),
        "max_runs": 1, "max_steps": 1}


def notice(identifier, sequence=1, source="denied"):
    return {"schema_version": NOTICE, "notice_id": identifier, "source_id": source, "sequence": sequence, "revision_id": None}


@pytest.mark.parametrize('fixture', [{'planner': True}], indirect=True)
def test_real_scheduler_register_poll_dispatch_replay_exhaustion_and_parent_lifetime(fixture):
    root, policy, execution, _context = fixture
    with selected(fixture).activate(), Workspace.open(root) as workspace:
        parent = workspace.research.run_cycle(policy, run_id='parent', execution=execution, request_id='parent-drive', max_steps=1)
        assert parent['terminal']
        config = configuration()
        schedule = workspace.research.schedule_refresh(policy, execution=execution, **config)
        first = schedule.poll(request_id='poll', notices=[notice('missing-source')], max_events=1)
        assert checked(STATUS, first) == first and first['pending'] and not first['daemon_created']
        duplicate = schedule.poll(request_id='poll', notices=[notice('missing-source')], max_events=1)
        assert duplicate['historical'] and duplicate['pending']['job_id'] == first['pending']['job_id']
        page = schedule.current(cursor=first['cursor'], max_events=32)
        assert page['events'][0]['sequence'] == first['cursor']['sequence']+1
        before = {p.relative_to(root).as_posix(): p.read_bytes() for p in (root/'runs/parent').rglob('*') if p.is_file()}
        result = schedule.advance(request_id='advance')
        assert result['terminal'] and result['status'] == 'exhausted' and result['allocated_runs'] == 1, result
        assert not result['authorized'] and not result['evidence_accepted']
        assert {p.relative_to(root).as_posix(): p.read_bytes() for p in (root/'runs/parent').rglob('*') if p.is_file()} == before
        again = schedule.advance(request_id='advance')
        assert again['historical'] and again['allocated_runs'] == 1
        reopened = workspace.research.schedule_refresh(policy, execution=execution, **config)
        assert reopened.current()['allocated_runs'] == 1
        with pytest.raises(EvidenceWikiError):
            schedule.advance(request_id='advance', notices=[notice('changed-command')])
    with selected(fixture).activate(), pytest.raises(EvidenceWikiError):
        schedule.current()


@pytest.mark.parametrize('fixture', [{'planner': True}], indirect=True)
def test_current_read_is_effect_free_and_cursors_binding_loss_and_missing_state_refuse(fixture, monkeypatch):
    import hashlib

    from evidence_wiki import refresh_scheduling
    root, policy, execution, _context = fixture
    context = selected(fixture)
    with context.activate(), Workspace.open(root) as workspace:
        workspace.research.run_cycle(policy, run_id='parent', execution=execution, request_id='parent-drive', max_steps=1)
        config = configuration()
        schedule = workspace.research.schedule_refresh(policy, execution=execution, **config)
        first = schedule.poll(request_id='poll')
        control = context.mutation_control_directory
        def retained():
            return {p.relative_to(control).as_posix(): p.read_bytes() for p in control.rglob('*') if p.is_file()}
        before = retained()
        schedule.current()
        assert retained() == before
        forged = {**first['cursor'], 'sequence': 256}
        with pytest.raises(EvidenceWikiError):
            schedule.current(cursor=forged)
        with monkeypatch.context() as blocked:
            blocked.setattr(schedule, '_snapshot', lambda: {'basis_id': None, 'source_ids': set(), 'authority': False,
                'eligible': False, 'original_ids': None})
            result = schedule.poll(request_id='permission-lost')
            assert result['status'] == 'blocked' and result['pending'] is None and result['allocated_runs'] == 0
        with monkeypatch.context() as changed:
            changed.setattr(refresh_scheduling, '_host_selection', lambda: 'changed')
            with pytest.raises(EvidenceWikiError):
                schedule.advance(request_id='wrong-authority')
        slot = control/'mutations/workspaces'/hashlib.sha256(str(root).encode()).hexdigest()/'refresh-schedules'/hashlib.sha256(config['schedule_id'].encode()).hexdigest()/'state.json'
        slot.unlink()
        with pytest.raises(EvidenceWikiError):
            schedule.poll(request_id='missing-state')
        with pytest.raises(EvidenceWikiError):
            workspace.research.schedule_refresh(policy, execution=execution, **config)
        assert not slot.exists()


@pytest.mark.parametrize('fixture', [{'planner': True}], indirect=True)
def test_reserved_dispatch_reconciles_same_native_identity_and_never_reallocates(fixture, monkeypatch):
    root, policy, execution, _context = fixture
    with selected(fixture).activate(), Workspace.open(root) as workspace:
        workspace.research.run_cycle(policy, run_id='parent', execution=execution, request_id='parent-drive', max_steps=1)
        schedule = workspace.research.schedule_refresh(policy, execution=execution, **configuration())
        original = schedule._dispatch
        attempts = []
        def crash_after_native_terminal(job, *, recovering):
            result = original(job, recovering=recovering)
            assert result['terminal']
            attempts.append(job['run_id'])
            raise RuntimeError('Synthetic interruption after native terminal result.')
        with monkeypatch.context() as crashed:
            crashed.setattr(schedule, '_dispatch', crash_after_native_terminal)
            with pytest.raises(RuntimeError):
                schedule.advance(request_id='original-dispatch')
        assert schedule.current()['status'] == 'running'
        with schedule._new_store().locked(write=True) as store:
            retained = schedule._load(store)
            for index in range(127):
                retained['commands']['used-'+str(index)] = {'sha256': 'sha256:'+'a'*64, 'job_id': None, 'complete': True}
            schedule._save(retained, store)
        restored = schedule.advance(request_id='reconcile')
        assert restored['terminal'] and restored['allocated_runs'] == 1 and not restored['usage_unknown']
        with monkeypatch.context() as repeated:
            repeated.setattr(schedule, '_dispatch', lambda *args, **kwargs: pytest.fail('Repeated native work'))
            replay = schedule.advance(request_id='reconcile')
            assert replay['terminal'] and replay['allocated_runs'] == 1
        assert attempts and len([p for p in (root/'runs').iterdir() if p.name.startswith('scheduled-')]) == 1


@pytest.mark.parametrize('fixture', [{'planner': True}], indirect=True)
def test_crash_before_native_run_retains_unknown_allocation_without_redispatch(fixture, monkeypatch):
    root, policy, execution, _context = fixture
    with selected(fixture).activate(), Workspace.open(root) as workspace:
        workspace.research.run_cycle(policy, run_id='parent', execution=execution, request_id='parent-drive', max_steps=1)
        schedule = workspace.research.schedule_refresh(policy, execution=execution, **configuration())
        with monkeypatch.context() as crashed:
            def crash(*args, **kwargs):
                raise RuntimeError('Synthetic loss before any native run was observed.')
            crashed.setattr(schedule, '_dispatch', crash)
            with pytest.raises(RuntimeError):
                schedule.advance(request_id='lost-dispatch')
        result = schedule.advance(request_id='recover-unknown')
        assert result['terminal'] and result['usage_unknown'] and result['allocated_runs'] == 1
        assert not result['recorded_usage']['complete']
        assert not any(p.name.startswith('scheduled-') for p in (root/'runs').iterdir())


@pytest.mark.parametrize('fixture', [{'planner': True}], indirect=True)
def test_real_permitted_notices_coalesce_out_of_order_hints_and_dispatch_lease_is_exclusive(fixture):
    root, policy, execution, _context = fixture
    with selected(fixture).activate(), Workspace.open(root) as workspace:
        workspace.research.run_cycle(policy, run_id='parent', execution=execution, request_id='parent-drive', max_steps=1)
        schedule = workspace.research.schedule_refresh(policy, execution=execution, **configuration())
        source = next(iter(schedule._snapshot()['source_ids']))
        first = schedule.poll(request_id='first', notices=[notice('first-hint', 2, source)])
        second = schedule.poll(request_id='second', notices=[notice('second-hint', 3, source)])
        assert second['pending']['job_id'] != first['pending']['job_id']
        assert any(row['kind'] == 'job_cancelled' for row in second['events'])
        late = schedule.poll(request_id='late', notices=[notice('old-hint', 1, source), notice('second-hint', 3, source)])
        assert late['pending']['job_id'] == second['pending']['job_id']
        with schedule._new_store().dispatch_lease():
            with pytest.raises(EvidenceWikiError) as caught:
                schedule.advance(request_id='competing-dispatch')
            assert caught.value.error_code == 'RESEARCH_SCHEDULE_BUSY'
        assert schedule.current()['allocated_runs'] == 0
