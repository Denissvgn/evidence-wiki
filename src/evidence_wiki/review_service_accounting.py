"""A frozen service slot shares one budget across every actual approved target."""

from evidence_wiki import execution_accounting as accounting
from evidence_wiki.agent_execution import ExecutionError
from evidence_wiki.mutation_contracts import detached

from .review_service_policy import ReviewServicePlan


def projection(plan, accounting_plan, records, slot_ref):
    """Project an already authenticated canonical plan; never refund failed work."""
    if type(plan) is not ReviewServicePlan or type(records) is not list:
        raise ExecutionError("service_accounting_invalid")
    route = plan.route(slot_ref)
    selected, known, native = set(), set(), set()
    for record in records:
        if record["kind"] == "execution_attempt":
            attempt = record["data"]["attempt"]
            known.add(attempt["attempt_id"])
            if attempt["config_ref"] in route.config_refs:
                selected.add(attempt["attempt_id"])
        elif record["kind"] == "execution_native":
            native.add(record["data"]["reservation"]["attempt_id"])
    if {row["attempt_id"] for row in accounting_plan["attempts"]} != known | native:
        raise ExecutionError("service_accounting_history_incomplete")
    current = detached(accounting_plan)
    current["closure"] = None
    current["attempts"] = [row for row in current["attempts"] if row["attempt_id"] in selected]
    current["observations"] = [row for row in current["observations"]
        if row["envelope"]["attempt_id"] is None or row["envelope"]["attempt_id"] in selected]
    limit = route.limit
    return accounting.projection(current, limits={"seconds": str(limit.seconds), "model_calls": limit.model_calls,
        "tool_calls": limit.tool_calls, "tokens": limit.tokens, "cost": limit.cost_limit})
