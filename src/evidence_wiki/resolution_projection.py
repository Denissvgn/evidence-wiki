"""Private reduction of current canonical publication and deterministic policy facts."""

from copy import deepcopy

from .mutation_contracts import digest
from .resolution_contracts import RESOLUTION, checked, refuse


def _eligible_content(question):
    """Copy allowed canonical content without private global proof commitments."""
    value = deepcopy(question)
    for claim in value["claims"]:
        claim.pop("verification", None)
    return value


def _project(*, run_id, binding_id, current, policy_document, policy_observation, decision,
             derived_proofs=None, subset_blockers=None, lifecycle=None):
    """Inert dictionaries establish no authentication; only the bound owner calls this."""
    wanted = policy_document["scope"]["question_ids"]
    if not 1 <= len(wanted) <= 100 or len(set(wanted)) != len(wanted):
        refuse("resolution_frozen_originals_invalid")
    accounting = current["accounting"]
    available = accounting["original_outcomes"]
    by_original = {row["original_id"]: row for row in available}
    if len(by_original) != len(available) or set(by_original) - set(wanted):
        refuse("resolution_original_accounting_conflict")
    release = current["canonical_release"]
    publication = release["publication"]
    blockers = list(release["gaps"])
    if accounting["accounting_complete"] is not True or set(by_original) != set(wanted):
        blockers.append("original_accounting_incomplete")
    if not current["policy_compatible"]:
        blockers.append("frozen_policy_incompatible")
    if publication is None or publication.get("schema_version") not in {"evidence-strict-publication/v1", "evidence-strict-publication/v2"}:
        blockers.append("current_strict_publication_unavailable")
        publication = None
    elif publication.get("selection_complete") is not True:
        blockers.append("publication_selection_incomplete")
    if current["qualification"]["state"] != "qualified":
        blockers.append("current_mandatory_qualification_unknown")
    if derived_proofs is None and decision["outcome"] not in {"accepted", "partial"}:
        blockers.append("current_policy_gate:" + decision["reason"])
    blockers.extend(subset_blockers or [])
    selected = {row["question_id"]: row for row in policy_observation["selected_outcomes"]}
    if len(selected) != len(policy_observation["selected_outcomes"]) or set(selected) - set(wanted):
        refuse("resolution_selected_original_conflict")
    released = {row["slug"]: row for row in (publication or {}).get("questions", [])}
    if len(released) != len((publication or {}).get("questions", [])):
        refuse("resolution_publication_question_conflict")
    selected_flags = ("independently_eligible", "source_current", "source_present", "mechanical_pass", "review_pass", "qualified", "context_complete")
    allow_partial = policy_document["source_rules"]["allow_partial"]
    rows = []
    for identifier in wanted:
        original = by_original.get(identifier)
        proof = selected.get(identifier)
        eligible = bool(not blockers and original and original["accepted"] and proof
            and proof["basis_id"] == current["basis_id"] and all(proof[name] is True for name in selected_flags))
        questions, reasons = [], []
        for question in (original or {}).get("questions", []):
            row = released.get(question["slug"])
            native_proof = (derived_proofs or {}).get(question["slug"])
            selected_question = (eligible if derived_proofs is None else bool(not blockers and native_proof
                and native_proof["eligible"] and (eligible or allow_partial)))
            accepted = bool(selected_question and row and row["original_id"] == identifier
                and row["accepted"] and question["accepted"] and row["claims"])
            refs = sorted({claim["id"] for claim in row["claims"]}) if accepted else []
            selected_refs = native_proof["claim_refs"] if native_proof is not None else proof["claim_refs"] if proof else []
            if accepted and not set(refs) <= set(selected_refs):
                refuse("resolution_claims_outside_authenticated_selection")
            qreasons = [] if accepted else sorted(set(question["gaps"] + ["current_required_review_or_publication_not_eligible"]))
            reasons.extend(qreasons)
            rejected = bool(not blockers and native_proof and native_proof.get("rejected") is True)
            questions.append({"question_id": question["question_id"], "slug": question["slug"],
                "outcome": "accept" if accepted else "reject" if rejected else "abstain", "eligible_claim_refs": refs, "reasons": qreasons,
                "current_checks": {"source_and_mechanics": "current_native_pass", "required_reviews": "current_owned_pass",
                    "actual_target_qualification": "current", "producer_and_context": "current_owned_proof"} if accepted else None})
        missing = list((original or {}).get("missing_question_ids", []))
        whole = bool(questions) and not missing and all(q["outcome"] == "accept" for q in questions)
        partial = allow_partial and any(q["outcome"] == "accept" for q in questions)
        outcome = "accept" if whole else "partial" if partial else "abstain"
        if outcome == "abstain" and questions and not missing and all(q["outcome"] == "reject" for q in questions):
            outcome = "reject"
        # A coarse run rejection does not identify a rejected original. Native
        # unresolved/failure rows remain abstentions without a local decision.
        if original is None:
            reasons.append("original_mapping_unavailable")
        rows.append({"original_id": identifier, "original_text": None if original is None else original["original_text"],
            "outcome": outcome, "questions": questions,
            "eligible_questions": [_eligible_content(released[q["slug"]]) for q in questions if q["outcome"] == "accept"],
            "missing_question_ids": missing, "reasons": sorted(set(reasons))})
    if not allow_partial and any(row["outcome"] != "accept" for row in rows):
        blockers.append("frozen_partial_not_allowed")
        for row in rows:
            if row["outcome"] in {"accept", "partial"}:
                row["outcome"] = "abstain"
                row["eligible_questions"] = []
                row["reasons"] = sorted(set(row["reasons"]) | {"frozen_partial_not_allowed"})
                for q in row["questions"]:
                    if q["outcome"] == "accept":
                        q.update(outcome="abstain", eligible_claim_refs=[], current_checks=None, reasons=["frozen_partial_not_allowed"])
    eligible_ids = {row["original_id"] for row in rows if row["outcome"] in {"accept", "partial"}}
    # The complete native publication and its proof commitments stay private.
    run = lifecycle or {}
    return checked({"schema_version": RESOLUTION, "run_id": run_id, "binding_id": binding_id,
        "current_basis_id": digest({"schema_version": "resolution-visible-basis/v1", "rows": rows}) if eligible_ids and not blockers else None,
        "publication_basis_id": None,
        "policy_id": current["policy_id"], "verdict": "accept" if len(eligible_ids) == len(rows) and all(row["outcome"] == "accept" for row in rows)
            else "partial" if eligible_ids else "reject" if all(row["outcome"] == "reject" for row in rows) else "abstain",
        "original_outcomes": rows, "global_blockers": sorted(set(blockers)),
        "accounting_complete": accounting["accounting_complete"], "current_publication": None,
        "lifecycle": {"terminal": run.get("terminal", False), "outcome": run.get("decision", {}).get("outcome") if run.get("decision") else None,
            "reason": run.get("decision", {}).get("reason") if run.get("decision") else None},
        "resolution_owner": "current_strict_publication_and_authenticated_run_policy", "external_action_authorized": False, "authorized": False,
        "limitations": ["Current publication and mandatory authenticated run-policy eligibility are both required.",
            "A terminal run does not establish current evidence eligibility or external-action authority.",
            "Within-original partial credit is not inferred from native publication without mandatory per-derived review proof."]})
