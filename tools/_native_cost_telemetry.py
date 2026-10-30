"""Bounded bridge from freshly authenticated native accounting to cost receipts.

Authentication qualifies the supplied controller's declared scope. It does not
independently establish provider billing truth or whole-host coverage.
"""

from __future__ import annotations

from datetime import datetime

from evidence_wiki import execution_accounting as accounting
from tools._integration_costs import ALLOCATIONS, METRICS, PHASES, _copy, freeze_cost_plan, project_cost_report
from tools._journey_cases import digest, require


def _attributions(values, attempt_ids):
    values = _copy(values)
    require(type(values) is list and len(values) <= accounting.MAX_ATTEMPTS, "native_attribution_bound")
    retained = {}
    for row in values:
        require(type(row) is dict and set(row) == {"attempt_id", "phase", "allocation"},
                "native_attribution_shape")
        require(type(row["attempt_id"]) is str and row["attempt_id"] in attempt_ids,
                "native_attribution_attempt")
        require(type(row["phase"]) is str and row["phase"] in PHASES
                and type(row["allocation"]) is str and row["allocation"] in ALLOCATIONS,
                "native_attribution_value")
        require(row["attempt_id"] not in retained, "native_attribution_duplicate")
        retained[row["attempt_id"]] = row
    require(set(retained) == set(attempt_ids), "native_attribution_incomplete")
    return retained


def _identifier(attempt_id):
    return "native-" + digest({"attempt_id": attempt_id})


def _source(source_id, native, coverage, *, placeholder=False):
    start = native["opened_at"]
    end = None if native["closure"] is None else native["closure"]["envelope"]["closed_at"]
    nonempty = end is not None and datetime.fromisoformat(start.replace("Z", "+00:00")) < datetime.fromisoformat(
        end.replace("Z", "+00:00"))
    return {"source_id": source_id, "provenance": "host_observed", "kind": "automated",
            "origin": "Native declared-scope accounting" if not placeholder else "Unobserved attributed phases",
            "collection_started_at": start if end is not None else None, "collection_ended_at": end,
            "coverage": "unknown" if placeholder else "complete" if coverage and nonempty else "partial",
            "authentication_ref": None if placeholder else accounting.digest(native)}


def from_native_accounting(plan, verifiers, *, scope_id, attributions, repeat_use_count=None, now_utc=None):
    """Project native final meters after fresh trusted producer verification.

    ``attributions`` is a closed list of ``attempt_id``, ``phase`` and
    ``allocation`` objects covering every native attempt, including failed work.
    Callbacks must authenticate the actual retained producer and referenced
    receipts. No caller JSON flag can qualify that trust. Native ``seconds`` is
    elapsed usage; wait duration and event intervals are not inferred from it.
    """
    native = accounting.authenticate_plan(plan, verifiers, now_utc=now_utc)
    native_report = accounting.report(native)
    coverage = native_report["coverage"]
    attributed = _attributions(attributions, coverage["expected_attempt_ids"])
    sources = [_source("native-accounting", native, coverage["coverage_complete"])]
    attempts, selected = [], []
    for attempt in native["attempts"]:
        original_id = attempt["attempt_id"]
        row = attributed[original_id]
        identifier = _identifier(original_id)
        attempts.append({"attempt_id": identifier, "phase": row["phase"], "allocation": row["allocation"],
                         "source_id": "native-accounting", "required_metrics": list(METRICS)})
        observations = sorted((row["envelope"] for row in native["observations"]
                               if row["envelope"]["attempt_id"] == original_id), key=lambda row: row["sequence"])
        finals = [row for row in observations if row["provenance"] == "measured"
                  and "usage" in row["slots"] and row["final_usage"]]
        final = finals[-1] if finals else None
        metrics = dict.fromkeys(METRICS)
        if final is not None and original_id not in coverage["incomplete_attempt_ids"]:
            for metric in ("model_calls", "tool_calls", "tokens"):
                metrics[metric] = final["usage"][metric]
            metrics["elapsed_seconds"] = final["usage"]["seconds"]
            metrics["cost"] = {"amount": final["usage"]["cost"], **final["cost_unit"]}
        # A terminal result can be observed even when its usage remains unknown.
        terminal = final if final is not None else observations[-1] if observations else None
        if terminal is not None:
            status = {"completed": "succeeded", "failed": "failed", "cancelled": "interrupted"}.get(
                terminal["state"], "unknown")
            selected.append({"event_id": "receipt-" + identifier, "attempt_id": identifier,
                             "source_id": "native-accounting", "receipt_sha256": digest(terminal),
                             "status": status, "started_at": None, "finished_at": None, "metrics": metrics})
    missing_phases = [phase for phase in PHASES if not any(row["phase"] == phase for row in attempts)]
    if missing_phases:
        sources.append(_source("native-unobserved-phases", native, False, placeholder=True))
        attempts.extend({"attempt_id": "missing-phase-" + phase, "phase": phase,
                         "allocation": "repeated" if phase in {"operation", "review", "evidence_update"} else "one_time",
                         "source_id": "native-unobserved-phases", "required_metrics": list(METRICS)}
                        for phase in missing_phases)
    measurement_plan = freeze_cost_plan(scope_id=scope_id,
        binding={"native_scope_id": native["scope_id"], "native_plan_sha256": native_report["plan_sha256"],
                 "native_binding_sha256": accounting.digest(native["binding"])},
        sources=sources, attempts=attempts, repeat_use_count=repeat_use_count)
    events = [{**row, "plan_sha256": measurement_plan["plan_sha256"]} for row in selected]
    projected = project_cost_report(measurement_plan, events)
    human = native_report["observed_human_interventions"]
    verification = {"authentication": "fresh_native_verifier", "scope": "declared_native_scope",
        "native_scope_id": native["scope_id"], "native_plan_sha256": native_report["plan_sha256"],
        "native_binding_sha256": accounting.digest(native["binding"]),
        "controller_refs": sorted({row["envelope"]["controller_ref"] for row in native["observations"]}
                                  | (set() if native["closure"] is None else
                                     {native["closure"]["envelope"]["controller_ref"]})),
        "opened_at": native["opened_at"],
        "closed_at": None if native["closure"] is None else native["closure"]["envelope"]["closed_at"],
        "coverage": {key: coverage[key] for key in ("expected_attempt_ids", "observed_attempt_ids",
            "missing_attempt_ids", "incomplete_attempt_ids", "missing_slots", "conflicting_attempt_ids",
            "missing_sequence_ranges", "coverage_closed", "coverage_complete")},
        "unobserved_phases": missing_phases,
        "human": {"status": "observed_scoped" if human is not None else "unknown",
                  "observed_human_interventions": human, "human_seconds": None},
        "whole_host_coverage_authenticated": False, "provider_billing_truth": "unqualified",
        "authorized": native_report["authorized"], "evidence_accepted": native_report["evidence_accepted"],
        "limitations": ["Trusted callbacks qualify only the native declared controller scope.",
                        "A native authenticated meter may still wrap synthetic or external provider data.",
                        "No human effort, adoption, business outcome or savings is inferred."]}
    return _copy({"plan": measurement_plan, "events": events, "report": projected,
                  "native_verification": verification})
