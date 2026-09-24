"""Optional local progress with measured artifacts separated from estimates and grading."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

from ._filesystem import os
from ._pack_io import canonical, read_file, relative_path
from .pack_catalog import _outside_assets, _writer_flags
from .pack_discovery import owner
from .planning_contracts import refuse
from .research_contracts import PROGRESS, checked, decode_estimates
from .research_observation import ResearchObservation
from .source_delivery import _publish


def progress(target, *, run_id=None, estimates=None):
    if estimates is not None:
        estimates = decode_estimates(
            canonical(
                {
                    key: estimates[key]
                    for key in ("tokens", "seconds", "tool_calls", "setup_interventions", "configuration_repairs")
                    if key in estimates
                }
            )
        )
    started = perf_counter()
    view = ResearchObservation(target, run_id=run_id)
    events = []
    if view.run:
        events = owner("run_controller").load_events(owner("run_controller").events_path(view.root, view.run["run_id"]))
    strict = view.strict_summary()
    source_events = [
        event
        for event in events
        if event.get("event_type") == "acquisition_completed"
        and (event.get("data") or {}).get("observation") == "source_verified"
    ]
    first = None
    if source_events and view.run:
        parse = owner("_evidence_authority").timestamp
        interval = (parse(source_events[0]["occurred_at"]) - parse(view.run["started_at"])).total_seconds()
        first = interval if interval >= 0 else None
    view.finish()
    result = {
        "schema_version": PROGRESS,
        "target": str(view.root),
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "basis": view.basis,
        "measured": {
            "observation_seconds": perf_counter() - started,
            "question_outcomes": dict(Counter(q["status"] for q in view.questions)),
            "request_outcomes": dict(Counter(r["status"] for r in view.requests)),
            "budget_state": view.status.get("readiness", {}).get("budget_state"),
            "run_id": view.run["run_id"] if view.run else None,
            "recorded_event_count": len(events),
            "recorded_tool_observations": {
                "count": sum(
                    event.get("event_type") in {"acquisition_completed", "fetch_failed", "verification_failed"}
                    for event in events
                ),
                "scope": "explicit caller acquisition/ingestion observations only; not all model or tool calls",
            },
            "first_verified_source_seconds": first,
            "first_source_basis": "local_owner_event; editable, not authenticated execution",
            "recorded_failures": sum(
                event.get("event_type") in {"fetch_failed", "verification_failed", "delegation_failed"}
                for event in events
            ),
            "claim_acceptance": {
                "accepted": strict.get("accepted_claims") if strict.get("acceptance_observed") else None,
                "denominator": strict.get("claims_count"),
                "grading": "current_strict_owner",
                "semantic_correctness": "not_independently_graded",
            },
            "setup_interventions": None,
            "configuration_repairs": None,
            "tool_calls": None,
            "unavailable_metrics": "No inferred zero for uninstrumented tool calls, setup repairs or interventions.",
        },
        "caller_estimates": estimates or {"basis": "not_supplied"},
        "semantic_evaluation": {
            key: {"value": None, "denominator": None, "grading_provenance": "not_observed"}
            for key in (
                "unsupported_claim_escapes",
                "appropriate_abstention",
                "unnecessary_refusal",
                "semantic_correctness",
            )
        },
        "framework": view.framework,
        "computation": view.computation,
        "limitations": [
            "Telemetry is optional and local; it grants no authority and never determines acceptance.",
            "Retained event clocks and counters are artifact observations, not protected measurement or semantic evaluation.",
        ],
    }
    return checked(result)


def save_report(path, value, *, target):
    """Explicit reports go to new caller-selected files outside research inputs."""
    selected = Path(path).expanduser().absolute()
    parent = selected.parent.resolve(strict=True)
    selected = parent / relative_path(selected.name)
    root = Path(target).resolve()
    if selected == root or root in selected.parents:
        refuse("research_report_requires_external_local_directory")
    _outside_assets(selected, additional_roots=(Path(__file__).parent,))
    descriptor = os.open(parent, _writer_flags())
    try:
        _outside_assets(descriptor, additional_roots=(Path(__file__).parent,))
        raw = canonical(checked(value)) + b"\n"
        _publish(descriptor, selected.name, raw)
        if read_file(descriptor, selected.name) != raw:
            refuse("research_report_changed", "ONBOARDING_OWNERSHIP_CONFLICT")
    finally:
        os.close(descriptor)
    return str(selected)
