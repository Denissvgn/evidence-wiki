#!/usr/bin/env python3
"""Shared public operations and acceptance adapters for computed evidence."""

from __future__ import annotations

import re
from pathlib import Path

from _computation_contract import schemas
from _computation_expression import require
from _computation_runtime import evaluate, sibling
from _evidence_revision import capture_workspace
from _script_errors import ScriptRefusal


def reason(error):
    if getattr(error, "error_code", None) == "EVIDENCE_REVISION_UNSUPPORTED":
        return "computation_platform_unsupported"
    value = str(error) if type(error).__name__ in {"ComputationInvalid", "EvidenceInvalid"} else "computation_input_invalid_or_unavailable"
    if value == "usage_materialization_unsupported":
        return "computation_platform_unsupported"
    if type(error).__name__ == "LockUnavailableError":
        value = "computation_lock_busy"
    return value if re.fullmatch(r"[a-z][a-z0-9_]{0,95}", value) else "computation_input_invalid_or_unavailable"


def refusal(value):
    busy = value in {"computation_lock_busy", "host_state_lock_unavailable"}
    return ScriptRefusal("COMPUTATION_BUSY" if busy else "COMPUTATION_REFUSED", "Computation requirements were not satisfied.",
        remediation="Inspect the reason, correct the declaration or evidence, recompute, and retry with its current result identity.",
        details={"reason": value}, recoverable=busy, exit_code=6 if busy else 2)


def run(project_root, operation="check", *, as_of=None, expected_result_id=None, request_id=None, cadence_id=None, dry_run=False):
    try:
        require(type(dry_run) is bool, "computation_option_invalid")
        if operation in {"schemas", "check", "aggregate", "evaluate", "verify", "schedule"}:
            require(expected_result_id is None and request_id is None and cadence_id is None and not dry_run,
                    "computation_mutation_options_require_mutation")
        if operation == "schemas":
            require(as_of is None, "computation_clock_option_requires_evaluation")
        if operation in {"write", "apply-warnings"}:
            require(cadence_id is None, "computation_cadence_option_requires_dispatch")
        if operation == "schemas":
            return schemas()
        if operation in {"check", "aggregate", "evaluate", "verify", "schedule"}:
            return evaluate(Path(project_root).resolve(), as_of=as_of)
        if operation in {"write", "apply-warnings", "dispatch"}:
            return sibling("_computation_effects").apply(project_root, operation, as_of=as_of,
                expected_result_id=expected_result_id, request_id=request_id, cadence_id=cadence_id, dry_run=dry_run)
        raise refusal("computation_operation_unknown")
    except (Exception, SystemExit) as error:
        if getattr(error, "error_code", "").startswith("COMPUTATION_"):
            raise
        raise refusal(reason(error)) from None


def required(project_root, config, *, view=None):
    if "computation" in config:
        try:
            sibling("_computation_contract").validate_yaml(capture_workspace(Path(project_root).resolve()).files["research.yml"].decode("utf-8"))
        except (Exception, SystemExit) as error:
            raise refusal(reason(error)) from None
    if config.get("computation") is None:
        return None
    try:
        if sibling("_computation_effects").pending(project_root):
            raise refusal("computation_recovery_required")
        result = evaluate(project_root, config=config, view=view)
        if result["status"] != "passed":
            raise refusal("computation_invariants_failed")
        return result
    except (Exception, SystemExit) as error:
        if getattr(error, "error_code", "").startswith("COMPUTATION_"):
            raise
        raise refusal(reason(error)) from None


def status(project_root, config):
    if config.get("computation") is None:
        return None
    try:
        result = evaluate(project_root, config=config)
        effects = sibling("_computation_effects")
        state, _raw = effects.state_from(capture_workspace(Path(project_root).resolve()))
        waiting = [key for key, item in state["requests"].items() if item["status"] == "pending"]
        dispatch = []
        for row in result["clock"]["schedules"]:
            observed = state["occurrences"].get(row["occurrence_id"])
            dispatch.append({"cadence_id": row["id"], "occurrence_id": row["occurrence_id"],
                "state": "not_recorded" if observed is None else "recorded" if observed["result_id"] == result["result_id"] else "recorded_for_prior_result",
                "recorded_result_id": observed["result_id"] if observed else None})
        return {"schema_version": "evidence-computation-status/v1", "status": "blocked" if waiting or result["status"] != "passed" else "passed",
                "result_id": result["result_id"], "definition_id": result["definition_id"], "clock": result["clock"],
                "engine_id": result["engine_id"], "measurement": {"sources": len(result["sources"]), "records": len(result["records"]),
                    "operations": result["operations"], "arithmetic": result["definition"]["arithmetic"]},
                "invariants": result["invariants"], "findings": result["findings"], "dispatch": dispatch,
                "reasons": ["computation_recovery_required"] if waiting else ["computation_invariants_failed"] if result["status"] != "passed" else []}
    except (Exception, SystemExit) as error:
        return {"schema_version": "evidence-computation-status/v1", "status": "blocked", "result_id": None,
                "definition_id": None, "clock": None, "engine_id": None, "measurement": None, "invariants": [], "findings": [], "dispatch": [], "reasons": [reason(error)]}
