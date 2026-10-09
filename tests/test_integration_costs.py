"""Exact cost arithmetic, complete attempt accounting and inert provenance."""

import copy
from decimal import Decimal, localcontext

import pytest

from tools._integration_costs import (
    METRICS,
    PHASES,
    freeze_cost_plan,
    project_cost_report,
    validate_cost_event,
    validate_cost_plan,
)
from tools._journey_cases import digest


def source(source_id="host", *, provenance="host_observed", kind="automated", coverage="complete"):
    return {"source_id": source_id, "provenance": provenance, "kind": kind, "origin": "retained local collector",
            "collection_started_at": "2026-10-09T09:00:00Z", "collection_ended_at": "2026-10-09T10:00:00Z",
            "coverage": coverage, "authentication_ref": None}


def plan(*, sources=None, attempts=None, repeat_use_count=None):
    if attempts is None:
        attempts = [{"attempt_id": phase, "phase": phase,
                     "allocation": "repeated" if phase in {"operation", "evidence_update"} else "one_time",
                     "source_id": "host", "required_metrics": list(METRICS)} for phase in PHASES]
    return freeze_cost_plan(scope_id="bounded-costs", binding={"candidate": "fixed-revision"},
                            sources=[source()] if sources is None else sources, attempts=attempts,
                            repeat_use_count=repeat_use_count)


def event(value, attempt_id="operation", *, status="succeeded", amount="0.1"):
    attempt = next(row for row in value["attempts"] if row["attempt_id"] == attempt_id)
    return {"event_id": "receipt-" + attempt_id, "attempt_id": attempt_id, "plan_sha256": value["plan_sha256"],
            "source_id": attempt["source_id"], "receipt_sha256": digest({"physical": attempt_id}), "status": status,
            "started_at": "2026-10-09T09:00:01Z", "finished_at": "2026-10-09T09:00:03Z",
            "metrics": {"elapsed_seconds": "2", "model_wait_seconds": "1", "model_calls": 1, "tool_calls": 2,
                        "tokens": 10, "cost": {"amount": amount, "currency": "USD", "pricing_ref": "receipt-pricing"}}}


def events(value):
    return [event(value, row["attempt_id"]) for row in value["attempts"]]


def external(observation_id, *, source_id="observer", metric="human_interventions", value=0, coverage="complete"):
    return {"observation_id": observation_id, "source_id": source_id,
            "receipt_sha256": digest({"external": observation_id}), "metric": metric, "value": value,
            "denominator": 1, "coverage": coverage}


def test_complete_exact_receipts_count_every_terminal_outcome_and_keep_wall_time_separate():
    value = plan()
    rows = events(value)
    statuses = ["succeeded", "failed", "abandoned", "abstained", "interrupted", "failed", "succeeded"]
    for row, status in zip(rows, statuses, strict=True):
        row["status"] = status
    report = project_cost_report(value, rows)
    assert report["coverage"]["complete"] is True
    assert report["coverage"]["host_coverage_authenticated"] is False
    assert report["automated"]["cost"]["total"]["amount"] == "0.7"
    assert report["automated"]["metrics"]["tokens"]["total"] == 70
    assert report["automated"]["metrics"]["elapsed_seconds"]["total"] == "14"
    assert report["intervals"]["union_seconds"] == "2"
    assert report["intervals"]["span_seconds"] == "2"
    assert report["automated"]["status_counts"]["failed"] == 2
    assert report["external_status"] == {"human": "unobserved", "adoption": "unobserved", "business": "unobserved",
                                          "telemetry": "declared", "authentication": "unproved"}


def test_missing_attempts_and_optional_unknown_metrics_never_become_zero():
    value = plan()
    rows = events(value)[1:]
    report = project_cost_report(value, rows)
    assert report["coverage"]["missing_attempt_ids"] == ["integration"]
    assert report["coverage"]["complete"] is False
    assert report["automated"]["cost"]["total"] is None
    assert report["automated"]["cost"]["subtotals"][0]["amount"] == "0.6"
    for metric in report["automated"]["metrics"].values():
        assert metric["total"] is None
        assert metric["unknown_attempt_ids"] == ["integration"]
    attempt = copy.deepcopy(value["attempts"][0])
    attempt["required_metrics"] = ["elapsed_seconds"]
    narrow = plan(attempts=[attempt])
    receipt = event(narrow, "integration")
    receipt["metrics"]["cost"] = None
    receipt["metrics"]["tokens"] = None
    narrow_report = project_cost_report(narrow, [receipt])
    assert narrow_report["coverage"]["declared_scope_complete"] is True
    assert narrow_report["coverage"]["complete"] is False
    assert narrow_report["automated"]["metrics"]["tokens"]["total"] is None
    assert narrow_report["by_phase"]["operation"]["host_observed"]["metrics"]["tokens"]["total"] is None


def test_no_receipts_has_no_known_zero_and_unknown_status_is_incomplete():
    value = plan()
    empty = project_cost_report(value, [])
    assert empty["automated"]["cost"]["subtotals"] == []
    assert empty["automated"]["metrics"]["tokens"]["known_subtotal"] is None
    rows = events(value)
    rows[0]["status"] = "unknown"
    report = project_cost_report(value, rows)
    assert report["coverage"]["declared_scope_complete"] is False
    assert report["coverage"]["unknown_status_attempt_ids"] == ["integration"]
    assert report["automated"]["cost"]["total"] is None
    assert report["automated"]["cost"]["subtotals"][0]["amount"] == "0.7"
    assert report["intervals"]["complete"] is False


def test_all_phases_with_unknown_optional_metrics_do_not_claim_complete_telemetry():
    attempts = plan()["attempts"]
    for attempt in attempts:
        attempt["required_metrics"] = ["elapsed_seconds"]
    value = plan(attempts=attempts, repeat_use_count=10)
    rows = events(value)
    rows[0]["metrics"]["model_calls"] = None
    report = project_cost_report(value, rows)
    assert report["coverage"]["unobserved_phases"] == []
    assert report["coverage"]["declared_scope_complete"] is True
    assert report["coverage"]["complete"] is False
    assert report["automated"]["metrics"]["model_calls"]["total"] is None
    assert report["allocation"]["one_time_per_use"] is None


def test_missing_phases_preserve_known_phase_subtotals_but_prevent_global_totals():
    value = plan(attempts=[plan()["attempts"][0]], repeat_use_count=2)
    report = project_cost_report(value, events(value))
    assert report["coverage"]["declared_scope_complete"] is True
    assert report["coverage"]["complete"] is False
    assert report["by_phase"]["integration"]["host_observed"]["cost"]["total"]["amount"] == "0.1"
    assert report["automated"]["cost"]["total"] is None
    assert report["automated"]["metrics"]["elapsed_seconds"]["total"] is None
    assert report["automated"]["metrics"]["elapsed_seconds"]["known_subtotal"] == "2"
    assert report["allocation"]["one_time_per_use"] is None


def test_duplicate_final_receipts_are_idempotent_and_order_is_stable():
    value = plan()
    rows = events(value)
    report = project_cost_report(value, rows)
    assert project_cost_report(value, list(reversed(rows)) + [copy.deepcopy(rows[0])]) == report
    changed = copy.deepcopy(rows[0])
    changed["metrics"]["tokens"] += 1
    with pytest.raises(ValueError, match="cost_attempt_receipt_conflict"):
        project_cost_report(value, rows + [changed])
    rows[1]["receipt_sha256"] = rows[0]["receipt_sha256"]
    with pytest.raises(ValueError, match="cost_receipt_reused"):
        project_cost_report(value, rows)


@pytest.mark.parametrize("bad", [0.1, True, -1, "-1", "01", "1e-2", "NaN", "Infinity", "0.0000000000001"])
def test_money_rejects_inexact_or_unbounded_values(bad):
    value = plan()
    row = event(value)
    row["metrics"]["cost"]["amount"] = bad
    with pytest.raises(ValueError):
        validate_cost_event(value, row)


def test_exact_decimal_arithmetic_and_rational_amortization_have_no_rounding():
    value = plan(repeat_use_count=3)
    rows = events(value)
    amounts = ["0.000000000001", "999999999999999.999999999999", "1", "0.1", "0.2", "0", "0.3"]
    for row, amount in zip(rows, amounts, strict=True):
        row["metrics"]["cost"]["amount"] = amount
    report = project_cost_report(value, rows)
    with localcontext() as context:
        context.prec = 60
        expected = format(sum(Decimal(item) for item in amounts), "f").rstrip("0").rstrip(".")
    assert report["automated"]["cost"]["total"]["amount"] == expected
    fraction = report["allocation"]["one_time_per_use"]
    assert fraction == {"numerator": "1000000000000001.2", "denominator": 3,
                        "currency": "USD", "pricing_ref": "receipt-pricing"}
    unallocated = plan()
    assert project_cost_report(unallocated, events(unallocated))["allocation"]["one_time_per_use"] is None


def test_mixed_currency_or_pricing_never_collapses_to_a_scalar_total():
    value = plan(repeat_use_count=2)
    rows = events(value)
    rows[0]["metrics"]["cost"]["currency"] = "EUR"
    rows[1]["metrics"]["cost"]["pricing_ref"] = "distinct-tariff"
    report = project_cost_report(value, rows)
    assert report["automated"]["cost"]["total"] is None
    assert len(report["automated"]["cost"]["subtotals"]) == 3
    assert report["allocation"]["one_time_per_use"] is None


def test_imported_estimated_and_synthetic_costs_stay_outside_host_summary():
    declared_sources = [source()] + [source(provenance, provenance=provenance) for provenance in
                                     ("imported", "estimated", "synthetic")]
    attempts = plan()["attempts"]
    for row, source_id in zip(attempts[:3], ("imported", "estimated", "synthetic"), strict=True):
        row["source_id"] = source_id
    value = plan(sources=declared_sources, attempts=attempts)
    report = project_cost_report(value, events(value))
    assert report["automated"]["cost"]["total"] is None
    assert report["automated"]["cost"]["subtotals"][0]["amount"] == "0.4"
    for provenance in ("imported", "estimated", "synthetic"):
        assert report["by_provenance"][provenance]["cost"]["total"]["amount"] == "0.1"


def test_external_actual_zero_requires_declared_complete_observation_and_stays_separate():
    value = plan(sources=[source(), source("observer", kind="human", provenance="imported"),
                          source("simulated", kind="organization", provenance="synthetic")])
    observed = external("human-zero")
    proxy = external("simulated-adoption", source_id="simulated", metric="adoption_count", value=100)
    report = project_cost_report(value, events(value), external_observations=[observed, proxy, copy.deepcopy(observed)])
    assert report["external_status"] == {"human": "declared_observed", "adoption": "unobserved", "business": "unobserved",
                                          "telemetry": "declared", "authentication": "unproved"}
    assert len(report["external_observations"]) == 2
    observed["coverage"] = "partial"
    partial = project_cost_report(value, events(value), external_observations=[observed])
    assert partial["external_status"]["human"] == "partial"
    assert partial["automated"] == report["automated"]
    observed["receipt_sha256"] = events(value)[0]["receipt_sha256"]
    with pytest.raises(ValueError, match="cost_receipt_reused"):
        project_cost_report(value, events(value), external_observations=[observed])


def test_source_windows_partial_coverage_and_event_interval_validation():
    value = plan(sources=[source(coverage="partial")])
    report = project_cost_report(value, events(value))
    assert report["coverage"]["incomplete_source_ids"] == ["host"]
    assert report["coverage"]["declared_scope_complete"] is False
    assert report["automated"]["cost"]["total"] is None
    assert report["intervals"]["complete"] is False
    row = event(value)
    row["started_at"] = "2026-10-09T08:59:59Z"
    with pytest.raises(ValueError, match="cost_event_outside_source"):
        validate_cost_event(value, row)
    row = event(value)
    row["started_at"] = None
    with pytest.raises(ValueError, match="cost_interval_incomplete"):
        validate_cost_event(value, row)
    row = event(value)
    row["metrics"]["model_wait_seconds"] = "2.000000000001"
    with pytest.raises(ValueError, match="cost_wait_exceeds_elapsed"):
        validate_cost_event(value, row)


@pytest.mark.parametrize("window", [(None, None), ("2026-10-09T09:00:00Z", "2026-10-09T09:00:00Z")])
def test_complete_source_declarations_require_known_nonempty_collection_window(window):
    declared = source()
    declared["collection_started_at"], declared["collection_ended_at"] = window
    with pytest.raises(ValueError, match="cost_complete_source_window"):
        plan(sources=[declared])


def test_outcome_status_requires_a_declared_denominator_and_complete_source_window():
    value = plan(sources=[source(), source("organization", kind="organization", provenance="imported")])
    row = external("adoption", source_id="organization", metric="adoption_count", value=0)
    row["denominator"] = None
    report = project_cost_report(value, events(value), external_observations=[row])
    assert report["external_status"]["adoption"] == "partial"
    row["denominator"] = 50
    report = project_cost_report(value, events(value), external_observations=[row])
    assert report["external_status"]["adoption"] == "declared_observed"
    assert report["external_status"]["authentication"] == "unproved"


def test_interval_union_handles_overlap_gaps_and_unknown_intervals():
    value = plan()
    rows = events(value)
    rows[0]["started_at"], rows[0]["finished_at"] = "2026-10-09T09:00:00Z", "2026-10-09T09:00:02Z"
    rows[1]["started_at"], rows[1]["finished_at"] = "2026-10-09T09:00:05Z", "2026-10-09T09:00:06.000001Z"
    rows[2]["started_at"] = rows[2]["finished_at"] = None
    report = project_cost_report(value, rows)
    assert report["intervals"]["union_seconds"] == "4.000001"
    assert report["intervals"]["span_seconds"] == "6.000001"
    assert report["intervals"]["complete"] is False
    assert report["intervals"]["unknown_attempt_ids"] == ["repair"]


def test_closed_shapes_binding_digest_and_input_bounds_are_not_bypassed():
    value = plan()
    changed = copy.deepcopy(value)
    changed["binding"]["candidate"] = "changed"
    with pytest.raises(ValueError, match="cost_plan_digest_conflict"):
        validate_cost_plan(changed)
    row = event(value)
    row["authenticated"] = True
    with pytest.raises(ValueError, match="cost_shape_invalid"):
        validate_cost_event(value, row)
    cyclic = {}
    cyclic["self"] = cyclic
    with pytest.raises(ValueError, match="cost_input_cycle"):
        freeze_cost_plan(scope_id="scope", binding=cyclic, sources=[source()], attempts=value["attempts"])
    with pytest.raises(ValueError, match="cost_input_bound"):
        project_cost_report(value, [event(value)] * 2049)
    for count in (0, -1, True, 1.0):
        with pytest.raises(ValueError):
            plan(repeat_use_count=count)


def test_validation_and_projection_return_detached_data():
    value = plan()
    rows = events(value)
    before = copy.deepcopy((value, rows))
    projected = project_cost_report(value, rows)
    projected["automated"]["cost"]["total"]["amount"] = "999"
    validated = validate_cost_plan(value)
    validated["binding"]["candidate"] = "different"
    assert (value, rows) == before
