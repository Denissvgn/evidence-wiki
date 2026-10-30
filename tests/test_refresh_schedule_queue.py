"""Duplicate, reordered and missing hints cannot reset finite host allocations."""

from copy import deepcopy

import pytest

from evidence_wiki import refresh_schedule_queue as queue
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.refresh_schedule_contracts import NOTICE

CLOCK = "2026-10-07T12:00:00+00:00"


def state():
    return {"binding": {"plan": {"max_runs": 2, "max_notices": 4, "expires_at": "2026-10-08T12:00:00+00:00",
        "interval_seconds": 30, "source_updates": []}}, "binding_id": "sha256:"+"a"*64,
        "previous_run_id": "parent", "last_basis_id": "sha256:"+"b"*64, "last_observed_at": CLOCK,
        "next_due_at": "2026-10-07T12:00:30+00:00", "notices": {}, "source_sequences": {}, "commands": {},
        "events": [], "jobs": [], "pending": None, "change_generation": 0, "settled_generation": 0,
        "allocated_runs": 0, "fulfilled_updates": [], "usage": {"seconds": 0, "model_calls": 0,
        "tool_calls": 0, "tokens": 0, "cost_limit": "0"}, "usage_unknown": False, "terminal_reason": None}


def notice(identifier, sequence, source="source"):
    return {"schema_version": NOTICE, "notice_id": identifier, "source_id": source,
        "sequence": sequence, "revision_id": None}


def observation(*, basis=None, authority=True, eligible=True):
    return {"basis_id": basis or "sha256:"+"b"*64, "authority": authority, "eligible": eligible}


def test_duplicate_and_out_of_order_notices_do_not_revive_obsolete_work():
    value = state()
    queue.ingest(value, [notice("latest", 3), notice("old", 1)], {"source"})
    before = deepcopy(value)
    queue.ingest(value, [notice("latest", 3)], {"source"})
    assert value == before and value["change_generation"] == 1
    assert value["notices"]["old"]["status"] == "out_of_order"
    with pytest.raises(EvidenceWikiError):
        queue.ingest(value, [notice("latest", 4)], {"source"})


def test_unpermitted_hint_never_creates_a_job_or_source_authority():
    value = state()
    queue.ingest(value, [notice("denied", 1)], set())
    queue.reconcile_due(value, observation(), CLOCK)
    assert value["pending"] is None and value["change_generation"] == 0
    assert value["notices"]["denied"]["status"] == "denied"


def test_missing_notice_is_detected_by_current_native_basis_and_periodic_due():
    value = state()
    queue.reconcile_due(value, observation(basis="sha256:"+"c"*64), CLOCK)
    assert value["pending"] is not None
    other = state()
    queue.reconcile_due(other, observation(), "2026-10-07T12:00:31+00:00")
    assert other["pending"] is not None


def test_multiple_source_updates_coalesce_and_cancel_only_queued_jobs():
    value = state()
    queue.ingest(value, [notice("a", 1)], {"source", "second"})
    queue.reconcile_due(value, observation(), CLOCK)
    first = value["pending"]["job_id"]
    queue.ingest(value, [notice("b", 2), notice("c", 1, "second")], {"source", "second"})
    queue.reconcile_due(value, observation(), CLOCK)
    assert value["pending"]["job_id"] != first and value["pending"]["notice_count"] == 3
    assert any(row["kind"] == "job_cancelled" and row["job_id"] == first for row in value["events"])
    value["pending"]["state"] = "dispatching"
    before = deepcopy(value["pending"])
    queue.reconcile_due(value, observation(authority=False), CLOCK)
    assert value["pending"] == before


def test_current_authority_loss_cancels_queued_work_without_allocating():
    value = state()
    queue.reconcile_due(value, observation(eligible=False), CLOCK)
    queue.reconcile_due(value, observation(authority=False), CLOCK)
    assert value["pending"] is None and value["allocated_runs"] == 0


def test_dispatch_reservation_is_not_reset_by_notices_or_unknown_result():
    value = state()
    queue.reconcile_due(value, observation(eligible=False), CLOCK)
    request, replay = queue.command(value, "tick", {"dispatch": True})
    assert not replay
    queue.reserve(value, value["pending"], request)
    job = deepcopy(value["pending"])
    queue.settle(value, job, None, CLOCK)
    queue.ingest(value, [notice("after-unknown", 2)], {"source"})
    queue.reconcile_due(value, observation(), CLOCK)
    assert value["allocated_runs"] == 1 and value["usage_unknown"]
    assert value["pending"] is None and value["terminal_reason"] == "dispatch_accounting_unknown"


def test_measured_runs_reach_terminal_exhaustion_with_retained_cost():
    value = state()
    for index in range(2):
        queue.reconcile_due(value, observation(eligible=False), CLOCK)
        request, _ = queue.command(value, "tick-"+str(index), {"dispatch": True})
        queue.reserve(value, value["pending"], request)
        job = deepcopy(value["pending"])
        report = {"run_id": job["run_id"], "cycle": {"current_basis_id": observation()["basis_id"]},
            "usage": {"seconds": 2, "model_calls": 1, "tool_calls": 4, "tokens": 10, "cost_limit": "0.02"},
            "source_updates": [], "outcome": "abstained", "reason": "insufficient_evidence"}
        queue.settle(value, job, report, CLOCK)
    assert value["allocated_runs"] == 2 and value["terminal_reason"] == "run_allocation_exhausted"
    assert value["usage"] == {"seconds": 4, "model_calls": 2, "tool_calls": 8, "tokens": 20, "cost_limit": "0.04"}
    queue.reconcile_due(value, observation(eligible=False), CLOCK)
    assert value["pending"] is None


def test_request_replay_and_clock_regression_are_finite_refusals():
    value = state()
    first, replay = queue.command(value, "tick", {"dispatch": True})
    assert not replay and queue.command(value, "tick", {"dispatch": True}) == (first, True)
    with pytest.raises(EvidenceWikiError):
        queue.command(value, "tick", {"dispatch": False})
    with pytest.raises(EvidenceWikiError):
        queue.reconcile_due(value, observation(), "2026-10-07T11:59:59+00:00")


@pytest.mark.parametrize('kind', ['notices', 'commands', 'events'])
def test_control_capacity_exhaustion_never_creates_unbounded_work(kind):
    value = state()
    if kind == 'notices':
        queue.ingest(value, [notice('n'+str(i), i+1) for i in range(5)], {'source'})
        assert len(value['notices']) == 4
    elif kind == 'commands':
        for index in range(129):
            queue.command(value, 'tick-'+str(index), {})
        assert len(value['commands']) == 128
    else:
        for _ in range(257):
            queue.event(value, 'ignored', 'bounded_capacity')
        assert len(value['events']) == 256
    queue.reconcile_due(value, observation(eligible=False), CLOCK)
    assert value['terminal_reason'].endswith('capacity_exhausted') and value['pending'] is None


def test_expiry_never_cancels_or_relabels_already_reserved_work_as_absent():
    value = state()
    queue.reconcile_due(value, observation(eligible=False), CLOCK)
    request, _ = queue.command(value, 'tick', {})
    queue.reserve(value, value['pending'], request)
    job = deepcopy(value['pending'])
    queue.reconcile_due(value, observation(), '2026-10-09T12:00:00+00:00')
    assert value['pending'] == job and value['terminal_reason'] == 'schedule_expired'


@pytest.mark.parametrize('field,value', [('max_runs', 0), ('max_runs', 9), ('max_steps', 65), ('max_notices', 65),
    ('interval_seconds', 0), ('previous_run_id', '/outside'), ('planner_config_ref', '../outside'),
    ('expires_at', '2026-10-07T12:00:00'), ('expires_at', '2026-10-07T12:00:00+02:00')])
def test_schedule_plan_rejects_unbounded_or_unowned_inputs(field, value):
    from evidence_wiki.refresh_schedule_contracts import PLAN, checked
    plan = {**state()['binding']['plan'], 'schema_version': PLAN, 'previous_run_id': 'parent',
        'planner_config_ref': 'planner', 'max_steps': 64}
    plan[field] = value
    with pytest.raises(EvidenceWikiError):
        checked(PLAN, plan)
