"""Native derived extent uses the same owning hard policy predicates."""

from .run_policy import _derived_scope_failure, _hard_policy_failure, _owner_observation, _profiles_qualified


def blockers(current, policy, observation, proofs):
    """Do not manufacture an accepted original observation for a derived subset."""
    observed = _owner_observation(policy, observation)
    failure = _hard_policy_failure(policy, observed)
    if failure is not None:
        return ["current_policy_gate:" + failure[1]]
    result = []
    if observed["basis_id"] != observed["expected_basis_id"] or observed["basis_id"] != current["basis_id"]:
        result.append("current_material_stale")
    if not _profiles_qualified(policy, observed) or current["qualification"]["state"] != "qualified":
        result.append("current_mandatory_qualification_unknown")
    pending = current["execution"]["pending"]
    accounting = current["execution"].get("accounting")
    if pending and pending["reservation_occupied"] or accounting and accounting["usage_unknown"]:
        result.append("current_external_work_unknown")
    publication = current["canonical_release"]["publication"] or {}
    claims = [claim for question in publication.get("questions", []) if proofs.get(question["slug"], {}).get("eligible")
        for claim in question["claims"]]
    scope_failure = _derived_scope_failure(policy, observed,
        claim_refs=[claim["id"] for claim in claims],
        source_refs=[evidence["source_id"] for claim in claims for evidence in claim["evidence"]])
    if scope_failure:
        result.append("current_derived_gate:" + scope_failure)
    return sorted(set(result))
