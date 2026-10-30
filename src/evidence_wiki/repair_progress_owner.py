"""Read-only progress from the existing authenticated native execution journal."""

from evidence_wiki import execution_session as native
from evidence_wiki._facades._base import _translated
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.execution_continuations import authenticated_gates, claim_sources, current_gates
from evidence_wiki.execution_disagreement import _final_matches, project_claim
from evidence_wiki.mutation_contracts import digest
from evidence_wiki.pack_discovery import owner
from evidence_wiki.research_journal_contracts import digest_records
from evidence_wiki.research_session import ResearchSession

from .repair_history_permissions import permitted
from .repair_progress import PROGRESS, fail, inventory_diff, schemas


def optional_current(operation):
    try:
        return operation()
    except (Exception, SystemExit) as error:
        if _translated(error) is None and not isinstance(error, (ValueError, EvidenceWikiError)):
            raise
        return None


def unavailable_review():
    return {"availability": "unavailable", "basis_id": None, "native_accepted": None,
            "selection": None, "reason": "current_owner_unavailable"}


def review_selection(session, *, claim_id, prepared, packets, view, records):
    """Recompute native age/authentication and actual qualification at read closure."""
    fresh = "unknown"
    sources = claim_sources(prepared, packets)
    projection = project_claim(session, claim_id=claim_id, prepared=prepared, sources=sources)
    gates = authenticated_gates(session, projection, current_gates(session, view, prepared, sources), prepared=prepared)
    configured = {item["profile_ref"] for item in session.policy.to_dict()["required_evaluators"]
        if item["role"] in {"evaluator", "critic"}}
    witnessed = {item["profile_ref"] for item in projection["reviews"]}
    if session.execution.profile.service_repair is not None:
        from .repair_slot_projection import current_first_passes
        slot_projection = current_first_passes(session, claim_id=claim_id, prepared=prepared, view=view, records=records)
        fresh = "complete" if slot_projection["complete"] else "incomplete"
    elif session.execution.profile.review_services is None:
        fresh = "unknown" if gates["authentication"] != "pass" or view["qualification"]["state"] != "qualified" else                     "complete" if configured and configured <= witnessed else "incomplete"
    strict_row = next(item for item in prepared["result"]["claims"] if item["claim"]["id"] == claim_id)
    selection = {"availability": "current", "basis_id": prepared["result"]["basis_id"],
        "native_accepted": strict_row["accepted"], "selection": strict_row["review"]}
    return selection, fresh


def current(session, *, original_id=None, after_sequence=0, max_records=32):
    """Keep native read custody until every outward commitment is revalidated."""
    if type(session) is not ResearchSession or session.execution is None:
        fail("bound_session_required")
    session._check("current")
    session.context.require_grant("repair_progress_owner.current")
    config = owner("_strict_evidence").configuration(session.root)
    usage = owner("_evidence_usage")
    with usage.current_view(session.root, config) as live:
        result = _current_owned(session, original_id=original_id, after_sequence=after_sequence,
            max_records=max_records, live=live)
        live.revalidate(session.root, config)
        session._check("current")
        return result


def _current_owned(session, *, original_id, after_sequence, max_records, live):
    """Project retained repairs; source rights and strict owners decide current approval."""
    if type(session) is not ResearchSession or session.execution is None:
        fail("bound_session_required")
    if type(after_sequence) is not int or after_sequence < 0 or type(max_records) is not int or not 1 <= max_records <= 32:
        fail("page_bound")
    session._check("current")
    session.context.require_grant("repair_progress_owner.current")
    if session.execution.profile.disagreement is None:
        fail("targeted_repair_profile_required")
    records = native.retained(session)
    view = optional_current(lambda: native.current(session))
    originals = set(session.binding["original_ids"])
    if original_id is not None and (type(original_id) is not str or original_id not in originals):
        fail("original_scope_invalid")
    attempts = [item["data"] for item in records if item["kind"] == "execution_attempt"
        and item["data"].get("retry_purpose", item["data"].get("purpose")) == "repair" and (original_id is None or item["data"]["original_id"] == original_id)]
    rows = []
    applications = {item["data"]["attempt_id"]: item["data"] for item in records if item["kind"] == "execution_application"}
    applied = {item["data"]["attempt_id"]: item["data"] for item in records if item["kind"] == "execution_applied"}
    observations = {item["data"]["attempt_id"]: item["data"] for item in records if item["kind"] == "execution_observation"}
    for row in attempts[after_sequence:after_sequence + max_records]:
        attempt_id = row["attempt"]["attempt_id"]
        claim_id = row.get("target_claim_id")
        if claim_id is None:
            fail("targeted_repair_required")
        application, effect = applications.get(attempt_id), applied.get(attempt_id)
        scope = row["attempt"]["input"].get("repair_scope")
        diff, before_basis, after_basis = None, None, None
        correlated = application is not None and _final_matches(session, session.execution.attempt(row["attempt"]),
            observations.get(attempt_id, {}).get("observation"))
        prepared = optional_current(lambda claim_id=claim_id: owner("strict_evidence").prepare_review(session.root, claim_id))
        packets = optional_current(lambda: native._sources(session))
        visible = False
        if prepared is not None and packets is not None and view is not None:
            visible = optional_current(lambda row=row, application=application, packets=packets: permitted(session, row, application, packets[0], usage_view=live)) is True
        current_review, fresh = unavailable_review(), "unknown"
        if visible:
            current_review, fresh = review_selection(session, claim_id=claim_id, prepared=prepared,
                packets=packets[0], view=view, records=records)
            if application is not None and scope is not None:
                proposed = application["proposal"]["inventory"]
                before = row.get("repair_parent_inventory", scope.get("before_inventory"))
                if before is None and proposed["schema_version"] == "evidence-strict-claims/v1":
                    before = {"schema_version": proposed["schema_version"], "questions": row["attempt"]["input"]["questions"], "claims": scope["claims"]}
                if before is not None:
                    diff = inventory_diff(before, proposed, target_claim_id=claim_id)
            if correlated:
                before_basis = row.get("disagreement", {}).get("material_basis_id")
                after_basis = None if effect is None else effect.get("disagreement_result_basis")
        rows.append({"attempt_id": attempt_id, "original_id": row["original_id"], "claim_id": claim_id,
            "state": "applied" if effect else "application_retained" if application else "observed" if attempt_id in observations else "reserved",
            "before_material_basis_id": before_basis, "after_material_basis_id": after_basis,
            "material_change": "unknown" if before_basis is None or after_basis is None else "changed" if before_basis != after_basis else "unchanged",
            "diff": diff, "current_review": current_review, "fresh_independent_reviews": fresh,
            "historical_effect_recorded": effect is not None, "execution_correlation": "known" if correlated else "unknown"})
    after_view = optional_current(lambda: native.current(session))
    if digest_records(records) != digest_records(native.retained(session)) or (view is not None and
            (after_view is None or after_view["basis_id"] != view["basis_id"])):
        fail("current_basis_changed")
    # Material equality does not cover qualification expiry or review age.
    # Re-read actual owners immediately before projecting current status.
    for projected in rows:
        if projected["current_review"]["availability"] != "current":
            continue
        retained_row = next(row for row in attempts if row["attempt"]["attempt_id"] == projected["attempt_id"])
        application = applications.get(projected["attempt_id"])
        prepared = optional_current(lambda projected=projected: owner("strict_evidence").prepare_review(session.root, projected["claim_id"]))
        packets = optional_current(lambda: native._sources(session))
        visible = (after_view is not None and prepared is not None and packets is not None and
            optional_current(lambda retained_row=retained_row, application=application, packets=packets:
                permitted(session, retained_row, application, packets[0], usage_view=live)) is True)
        if not visible:
            projected.update(current_review=unavailable_review(), fresh_independent_reviews="unknown",
                diff=None, before_material_basis_id=None, after_material_basis_id=None, material_change="unknown")
        else:
            projected["current_review"], projected["fresh_independent_reviews"] = review_selection(session,
                claim_id=projected["claim_id"], prepared=prepared, packets=packets[0], view=after_view, records=records)
    final_view = optional_current(lambda: native.current(session))
    if digest_records(records) != digest_records(native.retained(session)) or (after_view is not None and
            (final_view is None or final_view["basis_id"] != after_view["basis_id"])):
        fail("current_basis_changed")
    if after_view is not None and final_view["qualification"] != after_view["qualification"]:
        fail("current_currency_changed")
    for projected in rows:
        if projected["current_review"]["availability"] == "current":
            final_prepared = optional_current(lambda projected=projected:
                owner("strict_evidence").prepare_review(session.root, projected["claim_id"]))
            if final_prepared is None:
                fail("current_currency_changed")
            native_row = next(item for item in final_prepared["result"]["claims"] if item["claim"]["id"] == projected["claim_id"])
            expected = {"availability": "current", "basis_id": final_prepared["result"]["basis_id"],
                "native_accepted": native_row["accepted"], "selection": native_row["review"]}
            if expected != projected["current_review"]:
                fail("current_currency_changed")
    result = {"schema_version": PROGRESS, "run_id": session.run_id, "current_basis_id": digest({"schema_version": "repair-visible-basis/v1", "rows": rows}) if rows and all(item["current_review"]["availability"] == "current" for item in rows) else None,
        "original_id": original_id, "after_sequence": after_sequence, "next_sequence": after_sequence + len(rows),
        "truncated": after_sequence + len(rows) < len(attempts), "repairs_used": None if view is None else view["usage"]["repair_cycles"],
        "selected_repair_cycles": len({item.get("disagreement", {}).get("cycle_ref", item["attempt"]["attempt_id"]) for item in attempts}),
        "claim_repair_limit": session.execution.profile.disagreement.document()["claim_repair_limit"],
        "repair_limit": session.policy.to_dict()["continuation_limits"]["repair_cycles"], "rows": rows,
        "authorized": False, "evidence_accepted": False}

    owner("_strict_contract").validate_shape(result, schemas()[PROGRESS])
    return result
