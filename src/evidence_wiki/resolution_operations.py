"""Bound current resolution reads to existing publication and execution owners."""

from contextlib import nullcontext

from evidence_wiki import execution_session as native
from evidence_wiki.mutation_contracts import digest
from evidence_wiki.pack_discovery import owner
from evidence_wiki.research_session import ResearchSession

from .resolution_contracts import refuse
from .resolution_evidence import _Pass, questions
from .resolution_projection import _project
from .resolution_rules import blockers


def current(session):
    """Read every frozen original; never resolve questions or dispatch effects."""
    if type(session) is not ResearchSession:
        refuse("bound_execution_session_required")
    session._check("resolution")
    session.context.require_grant("resolution_operations.current")
    if session.execution is None:
        refuse("bound_execution_session_required")
    config = owner("verify_quotes").load_config(session.root)
    usage = owner("_evidence_usage")
    manager = usage.current_view(session.root, config) if usage.configured(config) else nullcontext(None)
    with manager as live:
        records = native.retained(session)
        view = native.current(session)
        observation = native.policy_observation(session, view)
        decision = session.policy.decide(observation)
        proofs = questions(session, view, records, usage_view=live, facts=_Pass(session, config))
        constraints = blockers(view, session.policy.to_dict(), observation, proofs)
        if native.execution_budget_overrun(session, records):
            constraints.append("current_execution_budget_overrun")
        # The run summary omits its decision intentionally. Only a retained
        # HMAC-owned terminal step can populate this lifecycle annotation.
        terminal_steps = [row["data"]["step"] for row in records if row["kind"] == "execution_step"
            and type(row["data"]["step"].get("decision")) is dict
            and row["data"]["step"]["decision"].get("terminal")]
        terminal = terminal_steps[-1]["decision"] if terminal_steps and (view["run"] or {}).get("terminal") else None
        result = _project(run_id=session.run_id, binding_id=digest(session.binding), current=view,
            policy_document=session.policy.to_dict(), policy_observation=observation, decision=decision,
            derived_proofs=proofs, subset_blockers=constraints,
            lifecycle={"terminal": terminal is not None, "decision": terminal})
        if live is not None:
            live.revalidate(session.root, config)
        closing_config = owner("verify_quotes").load_config(session.root)
        closing = native.current(session)
        closing_observation = native.policy_observation(session, closing)
        closing_decision = session.policy.decide(closing_observation)
        closing_proofs = questions(session, closing, records, usage_view=live, facts=_Pass(session, closing_config))
        closing_constraints = blockers(closing, session.policy.to_dict(), closing_observation, closing_proofs)
        if native.execution_budget_overrun(session, records):
            closing_constraints.append("current_execution_budget_overrun")
        if (closing["basis_id"] != view["basis_id"] or native.retained(session) != records
                or closing_observation["selected_outcomes"] != observation["selected_outcomes"]
                or closing["qualification"] != view["qualification"]
                or closing_proofs != proofs or closing_constraints != constraints
                or (closing_decision["outcome"], closing_decision["reason"], closing_decision["selected_outcome_refs"])
                    != (decision["outcome"], decision["reason"], decision["selected_outcome_refs"])
                or closing_config != config or owner("verify_quotes").load_config(session.root) != closing_config):
            refuse("resolution_current_inputs_changed")
        if live is not None:
            live.revalidate(session.root, config)
        session._check("resolution")
    return result
