"""Current authenticated counter findings can stop, but cannot approve release."""

from evidence_wiki import execution_session as native
from evidence_wiki.agent_execution import validate_observation
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.mutation_contracts import digest
from evidence_wiki.research_session import ResearchSession
from evidence_wiki.review_context import ReviewContext

from .counter_authority import measured_model, verify_controller
from .counter_context import translate
from .coverage_examination import examined_subject, fail
from .coverage_observation import observe


def current_objections(session, *, question_slug, source_ids=()):
    """Recompute findings from actual authenticated outputs and current owners.

    The native terminal owner must use this immediately before release/resolve.
    A not-blocked finding does not substitute for current strict review, coverage
    requirements, acquisition rights or qualified evaluator/controller proof.
    """
    if type(session) is not ResearchSession or session.execution is None:
        fail("bound_execution_session_required")
    live = native.current(session)
    if not source_ids:
        source_ids = [row["source_id"] for row in live["execution"]["source_bindings"]]
    instruction = observe(session.root, question_slug=question_slug, source_ids=source_ids)
    if live["qualification"]["state"] != "qualified":
        return {"state": "unknown", "material_objections": [], "unknown_reasons": ["current_qualification_unavailable"],
            "instruction_basis": instruction["basis_id"], "accepted": False, "semantic_override": False}
    records = session.execution.records(session)
    results = [row["data"] for row in records if row["kind"] == "execution_coverage_result"]
    blockers, unknown, examined, stale = [], [], set(), set()
    registry = session.execution.profile.role_scopes
    if registry is not None and any(scope.task_role == "coverage" for scope in registry.scopes):
        coverage_attempts = [row["data"] for row in records if row["kind"] == "execution_attempt"
            and row["data"].get("purpose") == "coverage"
            and row["data"].get("examination_instruction", {}).get("basis_id") == instruction["basis_id"]]
        coverage_results = [row for row in results if any(row.get("attempt_ref") == digest(attempt["attempt"])
            for attempt in coverage_attempts) and row.get("status") == "observed"]
        required_facets = {row["facet_id"] for row in instruction["facets"] if row["required"]}
        if not coverage_results:
            unknown.append("current_coverage_examination_missing")
        else:
            latest = coverage_results[-1]["findings"]
            linked = {row["facet_id"] for row in latest["link_proposals"]}
            missing = {row["facet_id"] for row in latest["missing_evidence"]}
            if required_facets - linked or required_facets & missing:
                unknown.append("critical_facet_support_unknown")
    pending_unknown = []
    for row in records:
        if row["kind"] != "execution_attempt" or row["data"].get("purpose") != "counterevidence":
            continue
        retained = row["data"]
        previous = retained.get("examination_instruction", {})
        if previous.get("question_slug") != question_slug:
            continue
        attempt = session.execution.attempt(retained["attempt"])
        role = session.execution.profile.role(attempt.config_ref)
        scopes = session.execution.profile.role_scopes
        if (scopes is None or scopes.scope(attempt.config_ref).task_role != "critic"
                or role.role != "evaluator" or session.context.controller_for(role.role, attempt.config_ref) != role.controller_ref):
            pending_unknown.append((retained["claim_id"], "counter_controller_unavailable"))
            continue
        scopes.scope(attempt.config_ref).require("model")
        scopes.scope(attempt.config_ref).require("objection.propose")
        current_instruction = observe(session.root, question_slug=question_slug,
            source_ids=[source["source_id"] for source in previous.get("source_contexts", [])])
        if previous.get("basis_id") != current_instruction["basis_id"]:
            stale.add(retained["claim_id"])
            continue
        observed = native._latest_observation(records, attempt.attempt_id)
        if observed is None or observed["state"] != "succeeded" or not observed["final_accounting"]:
            pending_unknown.append((retained["claim_id"], "counter_observation_incomplete"))
            continue
        # Intake authenticated the executor outside coordination gates before
        # sealing this observation into the verified parent journal. Terminal
        # gates use that pure owner proof and never call the host executor.
        observed = validate_observation(observed)
        if not measured_model(session, attempt, observed):
            pending_unknown.append((retained["claim_id"], "counter_model_provenance_unknown"))
            continue
        try:
            verify_controller(session, attempt)
        except EvidenceWikiError:
            pending_unknown.append((retained["claim_id"], "counter_current_authority_unavailable"))
            continue
        context = ReviewContext.restore(attempt.input_document["packet"], retained["review_context"])
        try:
            reproduced = translate(current_instruction, context, observed["output"])
        except EvidenceWikiError:
            pending_unknown.append((retained["claim_id"], "counter_output_invalid"))
            continue
        blockers.extend(reproduced["material_objection_candidates"])
        result = next((item for item in results if item.get("attempt_ref") == digest(attempt.document())), None)
        if (result is None or result.get("status") != "observed" or result.get("findings") != reproduced
                or not any(item["kind"] == "execution_applied" and item["data"]["attempt_id"] == attempt.attempt_id for item in records)):
            pending_unknown.append((retained["claim_id"], "counter_result_unsettled"))
            continue
        searches = [item["data"] for item in records if item["kind"] == "execution_native"
            and item["data"]["operation"] == "coverage.counter_search"]
        observed_native = {item["data"]["observation"]["attempt_id"]: item["data"] for item in records
            if item["kind"] == "execution_native_observation"}
        for finding in reproduced["findings"]:
            selected = [source["source_id"] for source in current_instruction["source_contexts"]]
            matches = [item for item in searches if item["arguments"] == {
                "question_slug": question_slug, "query": finding["query"], "source_ids": selected,
                "exclusions": finding["exclusions"], "expected_instruction_basis": current_instruction["basis_id"], "limit": 8}]
            if not any(item["reservation"]["attempt_id"] in observed_native
                    and observed_native[item["reservation"]["attempt_id"]]["error_code"] is None
                    and observed_native[item["reservation"]["attempt_id"]]["result"].get("search_execution") == "observed"
                    for item in matches):
                pending_unknown.append((retained["claim_id"], "counter_search_not_observed"))
        if examined_subject(reproduced["findings"], retained["claim_id"],
                [facet["facet_id"] for facet in current_instruction["facets"] if facet["required"]]):
            examined.add(retained["claim_id"])
        else:
            pending_unknown.append((retained["claim_id"], "counter_subject_disposition_incomplete"))
        if not reproduced["findings"] or any(item["kind"] == "no_result" for item in reproduced["findings"]):
            pending_unknown.append((retained["claim_id"], "bounded_counter_search_unknown"))
    unknown.extend(reason for subject, reason in pending_unknown if subject not in examined)
    if stale - examined:
        unknown.append("counter_material_stale")
    required = {row["id"] for row in instruction["claims"]}
    if required - examined:
        unknown.append("counter_claim_coverage_incomplete")
    return {"state": "blocked" if blockers else "unknown" if unknown else "not_blocked",
        "material_objections": blockers, "unknown_reasons": sorted(set(unknown)),
        "instruction_basis": instruction["basis_id"], "accepted": False, "semantic_override": False}


def stop(session, *, question_slug=None, original_ids=None):
    """An opt-in examination profile cannot release with missing current findings."""
    registry = session.execution.profile.role_scopes
    attempted = any(row["kind"] == "execution_attempt" and row["data"].get("purpose") == "counterevidence"
        for row in native.retained(session))
    if not attempted and (registry is None or not any(scope.task_role == "coverage" for scope in registry.scopes)):
        return {"state": "not_required", "accepted": False}
    if question_slug is None:
        view = native.current(session)
        selected = original_ids if original_ids is not None else session.policy.to_dict()["scope"]["question_ids"]
        slugs = [question["slug"] for original in view["accounting"]["original_outcomes"]
            if original["original_id"] in selected for question in original["questions"]]
    else:
        slugs = [question_slug]
    for slug in slugs:
        result = current_objections(session, question_slug=slug)
        if result["state"] != "not_blocked":
            fail("material_counterevidence_unresolved" if result["state"] == "blocked" else "counterevidence_current_coverage_unknown")
    return {"state": "not_blocked", "accepted": False}


def next_examination(session, view, records, *, original_ids):
    """Schedule opted-in agents, then abstain on unresolved current findings."""
    registry = session.execution.profile.role_scopes
    if registry is None or not any(scope.task_role == "coverage" for scope in registry.scopes):
        return None
    from .execution_session import _terminal_action
    coverage = registry.select("coverage")
    critic = registry.select("critic")
    for original in view["accounting"]["original_outcomes"]:
        if original["original_id"] not in original_ids:
            continue
        for question in original["questions"]:
            instruction = observe(session.root, question_slug=question["slug"],
                source_ids=[row["source_id"] for row in view["execution"]["source_bindings"]])
            attempts = [row["data"] for row in records if row["kind"] == "execution_attempt"
                and row["data"].get("examination_instruction", {}).get("basis_id") == instruction["basis_id"]]
            for kind, config_ref, claim_id in [("coverage", coverage.config_ref, None),
                    *[("counterevidence", critic.config_ref, row["id"]) for row in instruction["claims"]]]:
                matches = [row for row in attempts if row["purpose"] == kind and row.get("claim_id") == claim_id]
                if not matches:
                    return {"operation": "examine", "kind": kind, "question_slug": question["slug"],
                        "claim_id": claim_id, "config_ref": config_ref, "original_id": original["original_id"],
                        "reason": "scoped_examination_required"}
                results = [row["data"] for row in records if row["kind"] == "execution_coverage_result"
                    and row["data"].get("attempt_ref") == digest(matches[-1]["attempt"])]
                if not results or results[-1]["status"] != "observed":
                    return _terminal_action(session, view, "abstained", "unsupported_claim")
            findings = current_objections(session, question_slug=question["slug"])
            if findings["state"] != "not_blocked":
                return _terminal_action(session, view, "abstained", "review_disagreement" if findings["state"] == "blocked" else "insufficient_evidence")
    return None
