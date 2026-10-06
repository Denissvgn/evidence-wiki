"""Private current input for one already reserved synchronous publication."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from decimal import Decimal

from . import execution_accounting as accounting
from . import execution_session as native
from .artifact_delivery_contracts import refuse
from .mutation_contracts import canonical, digest

_ACTIVE = ContextVar("native_artifact_publication", default=None)
_DISPATCH = ContextVar("native_artifact_effect_dispatch", default=None)


class _EffectDispatch:
    def __init__(self, session, identifier, arguments, control):
        self.session, self.identifier, self.arguments, self.control = session, identifier, canonical(arguments), control
        self.thread, self.consumed, self.active = threading.get_ident(), False, True


@contextmanager
def effect_dispatch(session, identifier, arguments, control):
    """The native caller issues exactly one synchronous fresh-effect lease."""
    if _DISPATCH.get() is not None:
        refuse("publication_dispatch_nested")
    lease = _EffectDispatch(session, identifier, arguments, control)
    token = _DISPATCH.set(lease)
    try:
        yield
    finally:
        lease.active = False
        _DISPATCH.reset(token)



class _PublicationAdmission:
    """A same-thread native effect scope, never a reusable acceptance handle."""

    def __init__(self, session, identifier, arguments, control):
        self.session, self.identifier, self.arguments, self.control = session, identifier, canonical(arguments), control
        self.records = canonical(native.retained(session))
        self.thread = threading.get_ident()
        self.active = False
        self.origin = self

    def held(self, session):
        if (self.origin is not self or _ACTIVE.get() is not self or not self.active
                or session is not self.session or self.thread != threading.get_ident()
                or canonical(native.retained(session)) != self.records):
            refuse("publication_custody_not_held")
        self.control.checkpoint("current_publication_input")

    def __copy__(self):
        refuse("publication_custody_not_transferable")

    def __deepcopy__(self, memo):
        refuse("publication_custody_not_transferable")


@contextmanager
def publication_scope(session, identifier, arguments, control):
    """Called by the fixed native effect route after its durable reservation."""
    from .operation_control import OperationControl
    from .research_session import ResearchSession
    dispatch = _DISPATCH.get()
    if (type(dispatch) is not _EffectDispatch or not dispatch.active or dispatch.consumed
            or dispatch.session is not session or dispatch.identifier != identifier
            or dispatch.control is not control or dispatch.arguments != canonical(arguments)
            or dispatch.thread != threading.get_ident()):
        refuse("publication_dispatch_not_fresh")
    dispatch.consumed = True
    if (type(session) is not ResearchSession or type(control) is not OperationControl
            or _ACTIVE.get() is not None or session.execution is None
            or session.execution.profile.artifact_delivery is None):
        refuse("publication_native_scope_required")
    session._check("advance", write=True)
    plan = session.execution.profile.artifact_delivery
    policy = session.policy.to_dict()
    if "deliver" not in policy["allowed_effects"] or policy["host_capability_refs"].get("deliver") is None:
        refuse("delivery_capability_missing")
    session.context.require_grant("artifact_delivery_operations.deliver")
    if arguments.get("plan_sha256") != plan.sha256:
        refuse("publication_plan_changed")
    records = native.retained(session)
    intents = [row for row in records if row["kind"] == "execution_native"
        and row["data"]["reservation"]["attempt_id"] == identifier]
    if (len(intents) != 1 or intents[0]["data"]["operation"] != "artifact.deliver"
            or intents[0]["data"]["arguments"] != arguments
            or any(row["kind"] == "execution_native_observation"
                and row["data"]["observation"]["attempt_id"] == identifier for row in records)):
        refuse("publication_reservation_not_fresh")
    admission = _PublicationAdmission(session, identifier, arguments, control)
    token = _ACTIVE.set(admission)
    admission.active = True
    try:
        admission.held(session)
        yield admission
        admission.held(session)
    finally:
        admission.active = False
        _ACTIVE.reset(token)


def constraints(session, view, records, proofs, admission):
    """Only this active native write may remain reserved; every peer is settled."""
    if type(admission) is not _PublicationAdmission:
        refuse("publication_native_scope_required")
    admission.held(session)
    identifier = admission.identifier
    policy = session.policy.to_dict()
    ledger = session.execution.ledger(session, records)
    projection = accounting.projection(ledger, limits=native._limits(session))
    if (ledger["closure"] is not None or projection["incomplete_attempt_ids"] != [identifier]
            or projection["missing_attempt_ids"] != [identifier] or projection["conflicting_attempt_ids"]
            or projection["missing_sequence_ranges"]
            or set(projection["missing_slots"]) - {identifier + ":" + name for name in ("dispatch", "result", "usage")}
            or view["execution"]["pending"] and not view["execution"]["pending"]["applied"]):
        refuse("publication_peer_accounting_unknown")
    from .run_policy import _derived_scope_failure, _hard_policy_failure, _owner_observation, _profiles_qualified
    observation = _owner_observation(policy, native.policy_observation(session, view))
    failure = _hard_policy_failure(policy, observation)
    if (failure not in (None, ("stop_budget", "usage_unknown")) or not _profiles_qualified(policy, observation)
            or observation["basis_id"] != observation["expected_basis_id"] or observation["basis_id"] != view["basis_id"]
            or view["qualification"]["state"] != "qualified"):
        refuse("publication_current_policy_unknown")
    budgets, upper = policy["budgets"], projection["committed"]
    if (projection["overrun"] or native.execution_budget_overrun(session, records)
            or view["execution"]["steps_used"] > view["execution"]["steps_limit"]
            or any(upper[key] > budgets[key] for key in ("model_calls", "tool_calls", "tokens"))
            or Decimal(upper["cost"]) > Decimal(budgets["cost_limit"])
            or Decimal(upper["seconds"]) > Decimal(str(budgets["seconds"]))
            or view["usage"]["seconds"] >= budgets["seconds"]
            or any(view["usage"][key] is None or view["usage"][key] > budgets[key] for key in ("claims", "sources"))
            or any(view["usage"][key] is None or view["usage"][key] > limit for key, limit in policy["continuation_limits"].items())
            or datetime.now(timezone.utc) >= native.deadline(session)):
        refuse("publication_budget_exhausted")
    publication = view["canonical_release"]["publication"] or {}
    claims = [claim for question in publication.get("questions", []) if proofs.get(question["slug"], {}).get("eligible")
        for claim in question["claims"]]
    if _derived_scope_failure(policy, observation, claim_refs=[row["id"] for row in claims],
            source_refs=[evidence["source_id"] for row in claims for evidence in row["evidence"]]):
        refuse("publication_current_scope_changed")
    if digest(session.policy.to_dict()) != digest(session.binding["policy"]):
        refuse("publication_frozen_policy_changed")
    return []
