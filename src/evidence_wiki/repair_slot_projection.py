"""Current first passes preserve both logical requirement and actual signer identity."""

from datetime import datetime, timezone

from evidence_wiki import execution_session as native
from evidence_wiki.agent_execution import _utc
from evidence_wiki.mutation_contracts import digest
from evidence_wiki.review_service_routing import review_current
from evidence_wiki.service_authentication import owned

from .repair_progress import fail


def current_first_passes(session, *, claim_id, prepared, view, records):
    """Consume actual HMAC-owned applications and currently verified native signatures.

    This is parent read-only validation. No logical slot label substitutes for the
    dispatched target, controller, qualification or exact native review envelope.
    """
    plan = session.execution.profile.review_services
    if plan is None:
        fail("service_slot_plan_required")
    plan.check(session.execution.profile, session.policy.to_dict())
    basis = prepared["result"]["basis_id"]
    required = {row["profile_ref"]: row for row in session.policy.to_dict()["required_evaluators"]}
    applications = {row["data"]["attempt_id"]: row["data"] for row in records if row["kind"] == "execution_application"}
    max_age = prepared["snapshot"]["policy"]["max_review_age_seconds"]
    now = datetime.now(timezone.utc)
    selected = {}
    for record in records:
        if record["kind"] != "execution_applied":
            continue
        effect = record["data"]
        slot = effect.get("service_slot_ref")
        if (effect.get("operation") != "evaluate" or effect.get("claim_id") != claim_id
                or effect.get("strict_basis_id") != basis or slot not in required):
            continue
        row, attempt, observation = owned(session, effect["attempt_id"])
        application = applications.get(attempt.attempt_id)
        if (application is None or row.get("service_slot_ref") != slot or application.get("service_slot_ref") != slot
                or application.get("profile_ref") != attempt.config_ref or effect.get("profile_ref") != attempt.config_ref
                or row.get("claim_id") != claim_id or application.get("strict_basis_id") != basis
                or effect.get("service_identity") != application.get("service_identity")
                or observation["state"] != "succeeded" or not observation["final_accounting"]):
            fail("service_slot_application_mismatch")
        envelope = application["envelope"]
        review = envelope["payload"]["body"]["review"]
        if review["claim_id"] != claim_id or review["basis_id"] != basis:
            fail("service_slot_native_review_mismatch")
        native._verify_review_controller(session, attempt, envelope)
        reviewed, issued = _utc(review["reviewed_at"]), _utc(envelope["authentication"]["issued_at"])
        if reviewed > now or reviewed > issued:
            fail("service_slot_review_time_invalid")
        if (now - reviewed).total_seconds() > max_age or not review_current(session, effect, view, required[slot]):
            selected.pop(slot, None)
            continue
        selected[slot] = {"service_slot_ref": slot, "actual_config_ref": attempt.config_ref,
            "controller_ref": session.execution.profile.role(attempt.config_ref).controller_ref,
            "qualification_ref": effect["service_identity"]["qualification_ref"],
            "native_review_ref": digest(review), "verdicts": review["verdicts"]}
    return {"required_slots": sorted(required), "first_passes": [selected[key] for key in sorted(selected)],
        "complete": set(selected) == set(required), "accepted": False}
