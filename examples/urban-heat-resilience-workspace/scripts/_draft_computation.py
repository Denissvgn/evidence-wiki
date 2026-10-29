#!/usr/bin/env python3
"""Native deterministic values bind unaccepted quantitative claim references."""

from pathlib import Path

from _workspace_module_loader import load_workspace_module

_SIBLINGS = {}
SCRIPT_DIR = Path(__file__).resolve().parent


def sibling(name):
    return load_workspace_module(SCRIPT_DIR, name, cache=_SIBLINGS)


def check(root, config, proposal, *, view=None):
    draft = sibling("_draft_admission")
    declarations = [reference for claim in proposal["inventory"]["claims"] for reference in claim.get("calculations", [])]
    if config.get("computation") is None:
        draft.require(not declarations, "computation_not_configured")
        return None
    draft.require(proposal["inventory"]["schema_version"] == "evidence-strict-claims/v2", "computation_claim_schema_required")
    result = sibling("_computation_service").required(root, config, view=view)
    draft.require(result is not None and all(row["passed"] and row["complete"] for row in result["invariants"]), "computation_invariants_failed")
    for claim in proposal["inventory"]["claims"]:
        for calculation in claim.get("calculations", []):
            draft.require(calculation["result_id"] == result["result_id"], "computation_result_changed")
            resolved = sibling("_structured_view").resolve_pointer(result, calculation["pointer"])
            value = resolved.value if resolved.ok else None
            draft.require(type(value) is dict and value.get("type") == "decimal"
                and value.get(calculation["form"]) == calculation["expected"] and value.get("unit") == calculation["unit"]
                and value.get("rounded") == calculation["rounded"], "computation_value_or_unit_changed")
            sources = {result["lineage"][identifier]["source_id"] for identifier in value["lineage"]
                if "source_id" in result["lineage"][identifier]}
            draft.require(sources <= {row["source_id"] for row in claim["evidence"]}, "computation_source_missing")
    return result
