"""Explicit reusable-guidance actions without inferring them from source failures."""

from __future__ import annotations

from ._pack_io import canonical
from .pack_authoring_contracts import DERIVE, SPEC, checked, digest
from .pack_decisions import decision_document
from .planning_contracts import blocker, refuse


def authoring_action(value, blockers):
    if value is None:
        return None
    decision = decision_document(canonical(value["decision"]))
    choice = decision["choice"]
    if choice not in {"create", "revise"}:
        refuse("/decisions/pack_authoring/choice")
    if decision["local_guidance"] is not None or decision["partitions"]:
        refuse("/decisions/pack_authoring/incompatible_guidance_choices")
    if not any(gap["kind"] == "guidance" for gap in decision["gaps"]):
        blockers.append(blocker("guidance_gap_required_for_pack_authoring", field="/decisions/pack_authoring"))
        return {"state": "unsupported", "reason": "missing evidence does not justify new guidance", "decision": decision}
    selected = value["specification"] if choice == "create" else value["derivation"]
    if choice == "create" and (decision["selections"] or value["derivation"] is not None):
        refuse("/decisions/pack_authoring/create_selection")
    if choice == "create" and any(row["pack_basis"] for row in decision["mapping"]):
        refuse("/decisions/pack_authoring/unselected_basis")
    if choice == "revise":
        if len(decision["selections"]) != 1 or value["specification"] is not None:
            refuse("/decisions/pack_authoring/revision_selection")
        if selected is not None and (selected["base"]["selector"], selected["base"]["tree_sha256"]) != (
                decision["selections"][0]["selector"], decision["selections"][0]["tree_sha256"]):
            refuse("/decisions/pack_authoring/base_mismatch")
    if selected is not None:
        checked(selected, SPEC if choice == "create" else DERIVE)
        required = {row["requirement_id"] for row in decision["gaps"] if row["kind"] == "guidance"}
        if not required <= {row["id"] for row in selected["requirements"]}:
            refuse("/decisions/pack_authoring/requirement_mapping_incomplete")
    blockers.append(blocker("pack_authoring_pending", field="/decisions/pack_authoring"))
    return {"state": "needs_specification" if selected is None else "ready_for_explicit_authoring", "choice": choice,
        "decision": decision, "decision_sha256": digest(decision), "specification": selected,
        "specification_validation": "shape_only" if selected else "missing", "operation": "pack scaffold" if choice == "create" else "pack derive",
        "acceptance_requirements": ["current canonical validation", "frozen requirements and case expectations", "mechanical case observations",
            "separate domain rationale and unverified semantic limitations", "explicit local catalog registration"],
        "actions_executed": False, "source_failures_grant_authoring": False}
