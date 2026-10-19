"""Current native gates for bounded claim investigation and repair."""

from datetime import datetime, timezone

from ._review_dependency_json import detached, digest
from .agent_execution import _utc
from .errors import EvidenceWikiError
from .execution_disagreement import driver_action, project_claim
from .pack_discovery import owner
from .review_context import create_review_context


def claim_sources(prepared, packets, *, complete_material=None):
    snapshot = prepared["snapshot"]
    claims = [snapshot["claim"], *snapshot["premises"]] if complete_material is None else complete_material["claims"]
    identifiers = {item["source_id"] for claim in claims
        for item in claim["evidence"]}
    if snapshot.get("computation") is not None:
        identifiers.update(row["source_id"] for row in snapshot["computation"]["sources"])
    return [item for item in packets if item["source_id"] in identifiers]


def current_gates(session, view, prepared, sources):
    """Consume owner observations; never treat an agent conclusion as a gate."""
    claim = prepared["snapshot"]["claim"]
    row = next(item for item in prepared["result"]["claims"] if item["claim"]["id"] == claim["id"])
    reason = row.get("review", {}).get("reason")
    authentication = "fail" if reason in {"strict_review_authority_changed", "strict_review_authentication_failed",
        "strict_review_not_independent"} else "unknown" if reason in {
        "strict_authenticated_review_unavailable", "strict_review_expired"} else "pass"
    observation = row.get("observation") or {}
    mechanical = ("unknown" if type(observation.get("reasons")) is not list else
        "fail" if observation["reasons"] else "pass")
    from .execution_session import _complete_context_kwargs, _context_material, _review_basis
    context_material = _complete_context_kwargs(session, prepared)
    if context_material:
        from .execution_session import _sources
        packets, _bindings = _sources(session)
        sources = claim_sources(prepared, packets, complete_material=_context_material(context_material))
    context = create_review_context(context_id="continuation-context", task_role="evaluator",
        basis_id=_review_basis(prepared), owner_snapshot=prepared["snapshot"],
        rubric=prepared["rubric"], sources=sources, order_seed=b"current-native-context-check-seed", **context_material)
    context.check_owner(owner_snapshot=prepared["snapshot"], basis_id=_review_basis(prepared))
    context.check(prepared["snapshot"], prepared["rubric"], sources, **context_material)
    profile, policy = session.execution.profile, session.policy.to_dict()
    scopes = profile.role_scopes
    repair = scopes is not None and any(scope.task_role == "repair" for scope in scopes.scopes)
    plan = profile.disagreement
    aux = view["qualification"].get("auxiliary_profiles", [])
    adjudicator = plan.adjudicator_config_ref if plan is not None else None
    adjudicator_ready = adjudicator is not None and any(item["profile_ref"] == adjudicator
        and item["qualified"] is True and item["independent"] is True for item in aux)
    accounting = view["execution"]["accounting"]
    return {"authentication": authentication, "accounting": "unknown" if accounting is None or accounting["usage_unknown"]
        else "overrun" if accounting["overrun"] else "known", "mechanical": mechanical,
        "context_complete": context.document()["manifest"]["context_complete"],
        "source_present": bool(sources), "qualified": view["qualification"]["state"] == "qualified",
        "authority_granted": view["authority"]["terminal_grants_available"],
        "budget_available": view["execution"]["steps_used"] < view["execution"]["steps_limit"],
        "model_available": "model" in policy["allowed_effects"] and policy["host_capability_refs"]["model"] is not None,
        "repair_available": repair, "adjudicator_available": adjudicator_ready,
        "acquisition_available": False, "source_gap_ref": None,
        "run_repair_cycles": view["usage"]["repair_cycles"],
        "run_adjudication_cycles": view["usage"]["adjudication_cycles"],
        "run_acquisition_attempts": view["usage"]["acquisition_attempts"]}


def claim_action(session, view, claim_id, original_id, packets):
    prepared = owner("strict_evidence").prepare_review(session.root, claim_id)
    from .execution_session import _complete_context_kwargs, _context_material
    sources = claim_sources(prepared, packets,
        complete_material=_context_material(_complete_context_kwargs(session, prepared)))
    projection = project_claim(session, claim_id=claim_id, prepared=prepared, sources=sources)
    gates = current_gates(session, view, prepared, sources)
    gates = authenticated_gates(session, projection, gates, prepared=prepared)
    action = driver_action(session, projection, gates=gates,
        original_id=original_id)
    if action["operation"] == "adjudicate":
        action["config_ref"] = session.execution.profile.disagreement.adjudicator_config_ref
    return action


def authenticated_gates(session, projection, gates, *, prepared):
    """Reverify each retained first pass against current native trust."""
    from .execution_session import _verify_review_controller
    gates = detached(gates)
    max_age = prepared["snapshot"]["policy"].get("max_review_age_seconds")
    now = datetime.now(timezone.utc)
    references = {row["review_ref"] for row in projection["reviews"]}
    records = session.execution.records(session)
    attempts = {item["data"]["attempt"]["attempt_id"]: item["data"]["attempt"] for item in records
        if item["kind"] == "execution_attempt"}
    for item in records:
        if item["kind"] == "execution_application" and item["data"].get("operation") == "evaluate":
            data = item["data"]
            if digest(data["envelope"]["payload"]["body"]["review"]) in references:
                try:
                    _verify_review_controller(session, session.execution.attempt(attempts[data["attempt_id"]]), data["envelope"])
                    review = data["envelope"]["payload"]["body"]["review"]
                    reviewed = _utc(review["reviewed_at"])
                    issued = _utc(data["envelope"]["authentication"]["issued_at"])
                    if reviewed > now or reviewed > issued:
                        gates["authentication"] = "fail"
                    elif (type(max_age) is not int or max_age < 1 or (now - reviewed).total_seconds() > max_age
                            ) and gates["authentication"] != "fail":
                        gates["authentication"] = "unknown"
                except (ValueError, EvidenceWikiError, KeyError, TypeError):
                    gates["authentication"] = "fail"
    return gates


def repair_evidence(session, claim_id, prepared, sources):
    """Expose retained cited defects as evidence, without granting an effect."""
    projection = project_claim(session, claim_id=claim_id, prepared=prepared, sources=sources)
    records = session.execution.records(session)
    attempts = {item["data"]["attempt"]["attempt_id"]: item["data"] for item in records
        if item["kind"] == "execution_attempt"}
    applied = {item["data"]["attempt_id"] for item in records if item["kind"] == "execution_applied"
        and item["data"].get("operation") == "adjudicate"}
    investigations = [detached(item["data"]["findings"]) for item in records
        if item["kind"] == "execution_application" and item["data"].get("operation") == "adjudicate"
        and item["data"].get("claim_id") == claim_id and item["data"]["attempt_id"] in applied
        and attempts[item["data"]["attempt_id"]].get("disagreement", {}).get("material_basis_id") == projection["basis_id"]]
    # Historical model prose can contain material absent from the current source
    # closure. Supply typed criteria and current owner failures, not prior quotes,
    # rationales, source references or hashes of unverified historical prose.
    return {"first_pass_criteria": [{"verdicts": detached(row["verdicts"])} for row in projection["reviews"]],
        "historical_prose": "withheld_current_closure_not_established",
        "investigation_count": len(investigations),
        "native_failures": next(item["observation"]["reasons"] for item in prepared["result"]["claims"]
            if item["claim"]["id"] == claim_id), "authorized": False, "evidence_accepted": False}
