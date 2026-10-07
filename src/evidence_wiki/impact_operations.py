"""Current strict eligibility and conservative, read-only dependency explanations."""

from __future__ import annotations

from pathlib import Path

from evidence_wiki import change_operations
from evidence_wiki.errors import UsageError
from evidence_wiki.host_context import HostContext, current_context
from evidence_wiki.mutation_contracts import canonical, detached
from evidence_wiki.pack_discovery import owner

MAX_BYTES = 1_048_576
GLOBAL_DEPENDENCIES = ("workspace_material", "claims_inventory", "questions_and_requirements",
    "strict_policy_and_instructions", "source_usage_and_review_authority", "implementation", "computation", "time")


def refuse(reason):
    raise UsageError("RESEARCH_IMPACT_REFUSED", "Current strict impact observation refused.", details={"reason": reason})


def _recheck_comparisons(live, changes):
    """Carry disclosed retained revisions into the closing native use custody."""
    if changes is None:
        return
    for row in changes["sources"]:
        if any(dimension["before"]["availability"] == "observed" for dimension in row["dimensions"].values()):
            decision = live.check(row["before_revision"], uses=["retrieval"],
                purpose="research", consumer="evidence-wiki")
            if decision["eligible"] is not True or decision["complete"] is not True:
                refuse("comparison_usage_changed")


def _project(result, live, changes):
    """Known edges explain effects but cannot narrow the native approval basis."""
    claims = {row["claim"]["id"]: row["claim"] for row in result["claims"]}
    comparison_affected = set(changes["affected_source_ids"]) if changes is not None else set()
    changed = {row["before_source_id"] for row in changes["sources"]
        if row["before_source_id"] == row["after_source_id"] and row["changed_dimensions"]} if changes is not None else set()
    uncertain = set()
    if changes is not None:
        for row in changes["sources"]:
            if any(dimension["state"] in {"denied", "unavailable"} for dimension in row["dimensions"].values()):
                uncertain.update((row["before_source_id"], row["after_source_id"]))
    rows = []
    for selected in result["claims"]:
        claim = selected["claim"]
        premises, pending = set(), list(claim["premises"])
        while pending:
            identifier = pending.pop()
            if identifier in premises:
                continue
            premises.add(identifier)
            if identifier in claims:
                pending.extend(claims[identifier]["premises"])
        direct = {item["source_id"] for item in claim["evidence"]}
        sources = direct | {item["source_id"] for identifier in premises if identifier in claims
            for item in claims[identifier]["evidence"]}
        retained = [record for record in live.state.strict_reviews.values()
            if record["envelope"]["payload"]["body"]["review"]["claim_id"] == claim["id"]]
        latest = retained[-1] if retained else None
        differences = []
        retained_source_changes = set()
        basis_changed = None
        if latest is not None:
            payload = latest["envelope"]["payload"]["body"]["review"]
            snapshot = payload["snapshot"]
            basis_changed = payload["basis_id"] != result["basis_id"]
            retained_source_changes = {identifier for identifier in sources
                if snapshot["source_revisions"].get(identifier) != result["source_revisions"].get(identifier)}
            if retained_source_changes:
                differences.append("source")
            for label, before, after in (("claim", snapshot["claim"], claim),
                    ("premises", snapshot["premises"], [claims[name] for name in claim["premises"]]),
                    ("question", snapshot["question"], next(q for q in result["questions"] if q["slug"] == claim["question_slug"])),
                    ("policy", snapshot["policy"], result.get("effective_policy")),
                    ("implementation", payload["observation"]["producer_id"], result["producer_id"]),
                    ("permission", latest["authority"]["content_hash"], live.trust["content_hash"])):
                if before != after:
                    differences.append(label)
        invalidation_reasons = []
        if basis_changed is True:
            invalidation_reasons.append("whole_workspace_review_basis_changed")
        if "permission" in differences:
            invalidation_reasons.append("global_review_authority_changed")
        rows.append({"claim_id": claim["id"], "question_slug": claim["question_slug"],
            "known_dependencies": {"direct_source_ids": sorted(direct), "source_ids": sorted(sources),
                "premise_claim_ids": sorted(premises), "calculation_refs": detached(claim.get("calculations", []))},
            "known_changed_source_ids": sorted((sources & changed) | retained_source_changes),
            "comparison_affected_source_ids": sorted(sources & comparison_affected),
            "unavailable_selected_source_ids": sorted(sources & uncertain),
            "dependency_changes": differences, "retained_review_basis_changed": basis_changed,
            "conservative_invalidation": bool(invalidation_reasons),
            "conservative_invalidation_reasons": invalidation_reasons,
            "accepted": selected["accepted"], "review_passed": selected["review"]["passed"],
            "review_reason": selected["review"]["reason"], "reasons": selected["reasons"],
            "required_review_role": selected["required_review_role"],
            "requires_review": not selected["review"]["passed"]})
    return {"schema_version": "research-current-impact/v1", "basis_id": result["basis_id"],
        "claims": rows, "requires_review_claim_ids": [row["claim_id"] for row in rows if row["requires_review"]],
        "conservatively_invalidated_claim_ids": [row["claim_id"] for row in rows if row["conservative_invalidation"]],
        "changes": changes, "global_dependencies": list(GLOBAL_DEPENDENCIES),
        "approval_basis": "whole_workspace_native_strict", "selective_approval_reuse": False,
        "legacy_assessments": "not_strict_approval; use assessment refresh only under its own contract",
        "coverage": {"claims_total": len(rows), "claims_returned": len(rows), "truncated": False,
            "dependencies": "declared_current_claim_edges; not_complete_selective_review_bases",
            "changes": "selected_local_comparisons" if changes is not None else "not_requested",
            "unattributed_basis_changes": any(row["retained_review_basis_changed"] is True for row in rows),
            "attribution": "partial; complete historical workspace delta unavailable",
            "global_absence_inferred": False},
        "effects_executed": 0, "remote_refresh": "not_observed", "authorized": False, "evidence_accepted": False}


@owner("_strict_evidence").typed
def current(root, *, pairs=None, before_requirements_revision_id=None):
    """Recompute native acceptance; refuse unknown or changing owning inputs."""
    if pairs is not None:
        pairs = change_operations.selections(pairs)
    elif before_requirements_revision_id is not None:
        refuse("requirements_comparison_requires_pairs")
    context = current_context()
    if type(context) is not HostContext:
        refuse("host_context_required")
    context.require_grant("impact_operations.current")
    root = context.require_path(Path(root)).resolve(strict=True)
    strict, revision, usage = owner("_strict_evidence"), owner("_evidence_revision"), owner("_evidence_usage")
    capture = revision.capture_workspace(root)
    for path in capture.files:
        context.require_path(root / path)
    config = strict.configuration(root)
    policy = strict.resolve_policy(root, config)
    if policy is None or not usage.configured(config):
        refuse("protected_strict_owner_required")
    # Selected comparisons close their own native use/requirements scope before
    # the strict view is acquired; retained uses are reacquired in that view
    # and the enclosing capture closes the workspace join.
    changes = change_operations.current(root, pairs=pairs,
        before_requirements_revision_id=before_requirements_revision_id) if pairs is not None else None
    with usage.current_view(root, config) as live:
        _recheck_comparisons(live, changes)
        result = strict.evaluate(root, config, policy, view=live)
        source_revisions = strict.sibling("_publication_usage").qualify_capture(capture.files, config, live,
            purpose="qa-export", consumer="evidence-wiki")["selected"]
        report = _project({**result, "effective_policy": policy, "source_revisions": source_revisions}, live, changes)
        if len(canonical(report)) > MAX_BYTES:
            refuse("output_bound")
        report = detached(report)
        live.revalidate(root, config)
        if (strict.configuration(root) != config or strict.resolve_policy(root, config) != policy
                or revision.capture_workspace(root).revision_id != capture.revision_id
                or strict.selection_inputs(root, config, policy, view=live)[2] != result["basis_id"]):
            refuse("current_inputs_changed")
        for path in capture.files:
            context.require_path(root / path)
        context.require_grant("impact_operations.current")
        live.revalidate(root, config)
    return report
