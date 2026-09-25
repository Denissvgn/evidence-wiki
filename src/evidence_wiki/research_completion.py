"""Original-question accounting over the existing final publication owner."""

from __future__ import annotations

from .pack_discovery import owner
from .research_contracts import COMPLETION, checked
from .research_observation import ResearchObservation, optional


def completion(target, *, allow_partial=False, run_id=None):
    view = ResearchObservation(target, run_id=run_id)
    mapping = view.mapping.get("result")
    gaps = [] if mapping else ["original_question_accounting_unavailable"]
    if mapping:
        gaps.extend(mapping["gaps"])
    if view.managed:
        gaps.append("managed_result_requires_existing_host_submission")
    stale_run = bool(
        view.run
        and view.run.get("caller_context") is not None
        and view.run["caller_context"] != view.controls.get("result")
    )
    if stale_run:
        gaps.append("run_instruction_policy_or_implementation_changed")
    release = (
        {"state": "unavailable", "reason": "caller_provider_requires_explicit_qualification"}
        if not view.integrations_qualified
        else (
            optional(lambda: owner("_strict_evidence").publication(view.root))
            if view.policy
            else optional(
                lambda: owner("_selected_publication").run_selected_publication(
                    view.root, [q["slug"] for q in view.questions]
                )
            )
        )
    )
    publication = release.get("result") if not stale_run and not view.managed else None
    if publication is None:
        gaps.append("release_unavailable:" + release.get("reason", "caller_boundary_requires_inspection"))
    rows = publication.get("questions", []) if view.policy and publication else []
    if not view.policy and publication:
        rows = publication.get("export", {}).get("questions", [])
    by_slug = {row["slug"]: row for row in rows}
    current = {row["slug"]: row for row in view.questions}
    outcomes = []
    for original in (mapping or {}).get("originals", []):
        selected, missing = [], []
        for qid in original["question_ids"]:
            slug = mapping["expected"].get(qid)
            if slug is None or slug not in current:
                missing.append(qid)
                continue
            row = by_slug.get(slug)
            accepted = bool(
                row
                and (
                    row.get("accepted") is True
                    if view.policy
                    else row.get("status") == "answered" and publication.get("verdict") == "ship"
                )
            )
            selected.append(
                {
                    "question_id": qid,
                    "slug": slug,
                    "status": current[slug]["status"],
                    "accepted": accepted,
                    "gaps": row.get("gaps", []) if row else ["not_released"],
                    "blocked_reason": current[slug].get("blocked_reason"),
                }
            )
        outcomes.append(
            {
                "original_id": original["id"],
                "original_text": original["text"],
                "questions": selected,
                "missing_question_ids": missing,
                "accepted": bool(selected) and not missing and all(row["accepted"] for row in selected),
            }
        )
    complete = bool(outcomes) and not gaps and all(row["accepted"] for row in outcomes)
    partial = allow_partial and not view.managed and any(row.get("accepted") for row in rows)
    if not view.policy and publication:
        gaps.append("legacy_structural_release_has_no_strict_confirmation")
        complete = False
    view.finish()
    return checked(
        {
            "schema_version": COMPLETION,
            "target": str(view.root),
            "observed_at": view.now.isoformat(),
            "basis": view.basis,
            "status": "complete" if complete else "partial" if partial else "incomplete",
            "research_complete": complete,
            "accounting_complete": bool(outcomes) and not (mapping or {}).get("gaps", ["unavailable"]),
            "partial_allowed": allow_partial,
            "original_outcomes": outcomes,
            "publication": publication,
            "gaps": sorted(set(gaps)),
            "source_context": {
                "requests": [
                    {
                        "request_id": r["request_id"],
                        "kind": r["kind"],
                        "status": r["status"],
                        "scope": r.get("scope"),
                        "question_slugs": r.get("question_slugs", []),
                    }
                    for r in view.requests
                ],
                "inventory": view.status.get("sources", {}),
                "discovery": view.status.get("candidates", {}),
                "artifact_budgets": view.status.get("readiness", {}).get("budget_state"),
                "declared_question_criteria": (view.requirements or {}).get("decisions", {}).get("question_plans", []),
                "search_completeness": "not_inferred_from_inventory_or_successful_tools",
            },
            "computation": view.computation,
            "authority": {
                "route": "strict_publication_owner" if view.policy else "legacy_selected_publication_owner",
                "requested_assurance": view.policy.get("assurance") if view.policy else None,
                "host_protection": "not_established_by_this_command",
                "semantic_truth_guarantee": False,
            },
            "limitations": [
                "Accepted text and per-value calculation lineage come only from the current release owner; raw answer prose is not substituted.",
                "Every frozen original and derived question is accounted for; blocked, deferred, missing and unselected work remains explicit.",
                "Completion is re-evaluated, not a reusable success receipt. Expiry, revocation and changed evidence require fresh owner checks.",
            ],
        }
    )
