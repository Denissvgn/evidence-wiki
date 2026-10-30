"""An authenticated meter over a job cap blocks previously eligible subsets."""

from types import SimpleNamespace

import pytest

from evidence_wiki import execution_accounting as accounting
from evidence_wiki import execution_session as native
from evidence_wiki.run_policy import RunPolicy
from tests import test_execution_accounting as meters
from tests.test_run_policy import policy_document


@pytest.mark.parametrize("actual,job_cap,overrun", [(1, 1, False), (2, 1, True)])
def test_actual_signed_known_meter_job_overrun_is_not_hidden_by_larger_run_budget(actual, job_cap, overrun):
    declared = policy_document()
    declared["budgets"].update(model_calls=100, tokens=10000, cost_limit="100")
    policy = RunPolicy(declared)
    plan = accounting.reserve_attempt(meters.opened(), meters.attempt(reservation=meters.usage(seconds="5", models=job_cap,
        tools=3, tokens=100, cost="1")))
    plan = meters.record(plan, meters.observation(values=meters.usage(seconds="1", models=actual, tools=1, tokens=10, cost="0")))
    records = [{"kind": "execution_attempt", "data": {"attempt": {"attempt_id": "attempt", "job": {
        "budgets": {"model_calls": job_cap, "tool_calls": 3, "tokens": 100, "cost": "1"}}}}},
        {"kind": "execution_observation", "data": {"attempt_id": "attempt", "observation": {
            "sequence": 1, "observed_usage": {"model_calls": actual, "tool_calls": 1, "tokens": 10, "cost": "0"}}}}]
    session = SimpleNamespace(policy=policy, execution=SimpleNamespace(ledger=lambda *_args: plan,
        profile=SimpleNamespace(schedule=None, review_services=None)))
    summary = accounting.projection(plan, limits=native._limits(session))
    assert not summary["overrun"] and not summary["usage_unknown"]
    assert summary["usage"]["model_calls"] == actual
    assert native.execution_budget_overrun(session, records) is overrun


@pytest.mark.parametrize("bucket", ["scheduled", "service"])
def test_role_bucket_overrun_blocks_even_when_every_job_and_global_limit_fit(bucket):
    from evidence_wiki import ExecutionRoleLimit, ReviewServicePlan, ReviewServiceRoute
    from tests.test_execution_scheduling import intent, profile
    plan = accounting.reserve_attempt(meters.opened(), meters.attempt("attempt"))
    plan = meters.record(plan, meters.observation(values=meters.usage(seconds="1", models=2, tools=1, tokens=10, cost="0")))
    records = [intent("attempt")]
    records[0]["data"]["attempt"]["job"]["budgets"] = {"model_calls": 3, "tool_calls": 3, "tokens": 100, "cost": "1"}
    records.append({"kind": "execution_observation", "data": {"attempt_id": "attempt", "observation": {
        "sequence": 1, "observed_usage": {"model_calls": 2, "tool_calls": 1, "tokens": 10, "cost": "0"}}}})
    selected = profile(model_calls=1)
    if bucket == "service":
        route = ReviewServicePlan((ReviewServiceRoute("primary", "evaluator", ("primary",),
            ExecutionRoleLimit("evaluator", "reviewer", 1, 10, 1000, "1", 60)),))
        selected = SimpleNamespace(schedule=None, review_services=route)
    raw = policy_document()
    raw["budgets"].update(model_calls=100, tokens=10000, cost_limit="100")
    session = SimpleNamespace(policy=RunPolicy(raw), execution=SimpleNamespace(ledger=lambda *_: plan, profile=selected))
    assert not accounting.projection(plan, limits=native._limits(session))["overrun"]
    assert native.execution_budget_overrun(session, records)
