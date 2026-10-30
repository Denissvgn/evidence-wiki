"""Independent inventory, authenticated meters and uncertain retained costs."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
from decimal import ROUND_UP, Inexact, localcontext

import pytest

from evidence_wiki import execution_accounting as accounting
from evidence_wiki.errors import EvidenceWikiError

BINDING = {"run_id": "run", "workspace_binding": {"path": "/local/workspace", "device": "1", "inode": "2"},
    "policy_id": "sha256:" + "1" * 64, "execution_profile_id": "sha256:" + "2" * 64}
UNIT = {"currency": "USD", "pricing_ref": "meter-rate-v1"}
NOW = "2026-10-03T00:00:05Z"
HOST_KEY = b"fixture host accounting key"


def usage(*, seconds="0", models=0, tools=0, tokens=0, cost="0"):
    return {"seconds": seconds, "model_calls": models, "tool_calls": tools, "tokens": tokens, "cost": cost}


def signature(value):
    value = copy.deepcopy(value)
    value["authentication"] = None
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(HOST_KEY, raw, hashlib.sha256).hexdigest()


def signed(value):
    value = copy.deepcopy(value)
    value["authentication"] = signature(value)
    return value


def verifier(controller="host"):
    def verify(request):
        if not hmac.compare_digest(signature(request["envelope"]), request["envelope"]["authentication"] or ""):
            raise ValueError("Unverified producer")
        return {"schema_version": accounting.VERIFICATION, "envelope_sha256": request["envelope_sha256"],
            "binding": request["binding"], "controller_ref": controller,
            "verified_at": "2026-10-03T00:00:00Z", "expires_at": "2026-10-03T00:00:10Z", "authenticated": True}
    return accounting.CoverageVerifier(controller, verify)


def observer(*, end="2026-10-03T00:00:03Z", gaps=None, count=0):
    return {"observer_id": "actual-host-observer", "scope_id": "research-only", "started_at": "2026-10-03T00:00:00Z",
        "observed_through_at": end, "human_interventions": count, "gaps": gaps or [], "exclusions": []}


def observation(identifier="result", *, attempt_id="attempt", sequence=1, values=None,
                slots=None, final=True, state="completed", human=None, provenance="measured", unit=None):
    return signed({"schema_version": accounting.OBSERVATION, "observation_id": identifier, "binding": BINDING,
        "scope_id": "research-only", "controller_ref": "host", "attempt_id": attempt_id,
        "observed_at": "2026-10-03T00:00:02Z", "sequence": sequence,
        "slots": ["dispatch", "result", "usage"] if slots is None else slots, "state": state,
        "usage": usage(seconds="1.25", models=1, tools=2, tokens=10, cost="0.125") if values is None else values,
        "cost_unit": UNIT if unit is None else unit, "provenance": provenance,
        "final_usage": final, "correction": False, "artifact_refs": [], "human_observer": human, "authentication": None})


def opened(*, human=False):
    baseline = observation("baseline", attempt_id=None, sequence=0, values=usage(), slots=["baseline"],
        final=False, state="started", human=observer(end="2026-10-03T00:00:00Z") if human else None)
    baseline["observed_at"] = "2026-10-03T00:00:00Z"
    baseline = signed(baseline)
    return accounting.start_coverage(binding=BINDING, scope_id="research-only", baseline=baseline,
        verifier=verifier(), now_utc=NOW)


def attempt(identifier="attempt", *, reservation=None, unit=None):
    return {"attempt_id": identifier, "job_id": "job-" + identifier, "controller_ref": "host",
        "issued_at": "2026-10-03T00:00:01Z", "reservation": usage(seconds="5", models=2, tools=3, tokens=100, cost="1")
            if reservation is None else reservation,
        "cost_unit": UNIT if unit is None else unit, "required_slots": ["dispatch", "result", "usage"]}


def reserved(*, human=False):
    return accounting.reserve_attempt(opened(human=human), attempt())


def record(plan, value):
    return accounting.observe_attempt(plan, value, verifier(), now_utc=NOW)


def close(plan):
    closure = signed({"schema_version": accounting.CLOSURE, "binding": BINDING, "scope_id": "research-only",
        "controller_ref": "host", "plan_sha256": accounting.digest(plan), "closed_at": "2026-10-03T00:00:03Z",
        "terminal_decision_ref": "decision-terminal", "last_observation_ids": [row["envelope"]["observation_id"]
            for row in plan["observations"]], "authentication": None})
    return accounting.close_coverage(plan, closure, verifier(), now_utc=NOW)


def test_prefix_is_measured_and_usable_without_whole_scope_closure():
    value = accounting.projection(opened(), limits=usage(seconds="30", models=5, tools=10, tokens=1000, cost="5"))
    assert value["usage"] == usage()
    assert not value["usage_unknown"] and not value["coverage_complete"]
    assert value["remaining"]["cost"] == "5"
    assert all(value is None for value in accounting.report(opened())["totals"].values())


def test_loss_of_entire_attempt_is_detected_from_predeclared_inventory():
    value = accounting.report(close(reserved()))
    assert value["coverage"]["expected_attempt_ids"] == ["attempt"]
    assert value["coverage"]["observed_attempt_ids"] == []
    assert value["coverage"]["missing_attempt_ids"] == ["attempt"]
    assert value["coverage"]["outstanding"]["cost"] == "1"
    assert value["totals"]["cost"] is None


def test_observed_final_usage_releases_only_unused_reservation():
    plan = record(reserved(), observation())
    value = accounting.projection(plan, limits=usage(seconds="10", models=2, tools=3, tokens=100, cost="1"))
    assert value["spent"] == usage(seconds="1.25", models=1, tools=2, tokens=10, cost="0.125")
    assert value["outstanding"] == usage()
    assert value["remaining"]["cost"] == "0.875"
    assert not value["usage_unknown"] and not value["coverage_complete"]
    assert accounting.report(close(plan))["totals"]["cost"] == "0.125"


@pytest.mark.parametrize("state", ["failed", "cancelled", "completed"])
def test_all_final_observed_attempt_outcomes_cost_once(state):
    plan = record(reserved(), observation(state=state))
    assert record(plan, observation(state=state)) == plan
    assert accounting.report(close(plan))["totals"]["model_calls"] == 1


def test_changed_duplicate_conflicts_without_mutating_original_input():
    plan = record(reserved(), observation())
    before = copy.deepcopy(plan)
    with pytest.raises(EvidenceWikiError, match="Execution accounting"):
        record(plan, observation(values=usage(cost="3")))
    assert plan == before


@pytest.mark.parametrize("field", ["binding", "controller_ref", "attempt_id", "usage", "slots", "authentication"])
def test_full_envelope_authentication_rejects_changed_binding_and_measurements(field):
    value = observation()
    if field == "binding":
        value[field] = {"run_id": "unrelated"}
    elif field == "usage":
        value[field]["cost"] = "0"
    elif field == "slots":
        value[field] = ["usage"]
    else:
        value[field] = "unrelated"
    with pytest.raises(EvidenceWikiError):
        record(reserved(), value)


def test_reloaded_plan_requires_producer_authentication_and_expired_cert_is_not_authority():
    plan = close(record(reserved(), observation()))
    assert accounting.authenticate_plan(json.loads(json.dumps(plan)), {"host": verifier()}, now_utc=NOW) == plan
    with pytest.raises(EvidenceWikiError):
        accounting.authenticate_plan(plan, {"host": verifier()}, now_utc="2026-10-03T00:00:11Z")
    forged = copy.deepcopy(plan)
    forged["observations"][0]["envelope"]["authentication"] = "worker-declared"
    forged["observations"][0]["verification"]["envelope_sha256"] = accounting.digest(forged["observations"][0]["envelope"])
    with pytest.raises(EvidenceWikiError):
        accounting.authenticate_plan(forged, {"host": verifier()}, now_utc=NOW)


def test_predeclared_slots_cannot_be_replaced_by_observed_events():
    plan = accounting.declare_slot(reserved(), slot_id="decision", controller_ref="host",
        declared_at="2026-10-03T00:00:01Z")
    plan = record(plan, observation())
    assert accounting.report(close(plan))["coverage"]["missing_slots"] == ["decision"]
    with pytest.raises(EvidenceWikiError):
        record(reserved(), observation("unexpected", attempt_id=None, slots=["decision"], values=usage(), sequence=1))


def test_non_attempt_events_cannot_hide_resource_consumption():
    plan = accounting.declare_slot(opened(), slot_id="decision", controller_ref="host",
        declared_at="2026-10-03T00:00:01Z")
    with pytest.raises(EvidenceWikiError) as error:
        record(plan, observation("decision", attempt_id=None, slots=["decision"], values=usage(cost="1")))
    assert error.value.error_code == "ACCOUNTING_UNSCOPED_USAGE"


def test_unknown_final_meter_keeps_full_reservation_even_after_confirmed_cancellation():
    plan = record(reserved(), observation(state="cancelled", values=usage(cost=None, tokens=None)))
    value = accounting.projection(plan)
    assert value["spent"]["model_calls"] == 0
    assert value["outstanding"]["cost"] == "1" and value["usage_unknown"]
    assert value["usage"]["cost"] is None


def test_estimated_and_imported_values_cannot_replace_measured_totals():
    for provenance in ("estimated", "imported"):
        plan = record(reserved(), observation(provenance=provenance))
        value = accounting.report(close(plan))
        assert value["totals"]["cost"] is None and value["coverage"]["outstanding"]["cost"] == "1"


def test_mixed_currency_retains_separate_subtotals_and_unknown_scalar():
    plan = record(reserved(), observation(unit={"currency": "EUR", "pricing_ref": "euro-meter"}))
    value = accounting.report(close(plan))
    assert value["coverage"]["cost_subtotals"] == [{"cost_unit": {"currency": "EUR", "pricing_ref": "euro-meter"},
        "known_subtotal": "0.125"}]
    assert value["totals"]["cost"] is None and value["coverage"]["outstanding"]["cost"] == "1"


def test_late_real_cost_retains_overrun_and_does_not_reopen_scope():
    plan = close(reserved())
    value = accounting.projection(record(plan, observation(values=usage(cost="2.5"))),
        limits=usage(seconds="10", models=3, tools=3, tokens=100, cost="1"))
    assert value["spent"]["cost"] == "2.5" and value["remaining"]["cost"] == "0"
    assert value["overrun"] == ["cost"] and value["coverage_closed"]
    assert not value["coverage_complete"]


def test_corrected_final_receipt_preserves_both_and_blocks_spending_projection():
    plan = record(reserved(), observation())
    correction = observation("correction", sequence=2, values=usage(cost="0.2"))
    correction["correction"] = True
    plan = record(plan, signed(correction))
    value = accounting.projection(plan)
    assert value["conflicting_attempt_ids"] == ["attempt"]
    assert value["spent"]["cost"] == "0.2" and value["outstanding"]["cost"] == "1"
    assert len(plan["observations"]) == 3 and value["usage_unknown"]


def test_cumulative_meter_regression_does_not_refund_or_claim_complete():
    plan = record(reserved(), observation("prefix", final=False, state="started", values=usage(cost="0.5")))
    plan = record(plan, observation(sequence=2, values=usage(cost="0.1")))
    value = accounting.projection(plan)
    assert value["spent"]["cost"] == "0.5" and value["usage_unknown"]


def test_exact_decimal_accounting_ignores_hostile_decimal_context():
    reservation = usage(seconds="1", cost="999999999999999.999999999")
    plan = accounting.reserve_attempt(opened(), attempt("first", reservation=reservation))
    plan = accounting.reserve_attempt(plan, attempt("second", reservation=reservation))
    for identifier in ("first", "second"):
        plan = record(plan, observation(identifier, attempt_id=identifier, values=reservation))
    with localcontext() as context:
        context.prec = 1
        context.rounding = ROUND_UP
        context.traps[Inexact] = True
        assert accounting.report(close(plan))["totals"]["cost"] == "1999999999999999.999999998"


def test_human_zero_requires_started_observer_ending_at_closure_without_gaps():
    plan = record(reserved(human=True), observation(human=observer()))
    assert accounting.report(close(plan))["observed_human_interventions"] == 0
    without_start = record(reserved(), observation(human=observer()))
    assert accounting.report(close(without_start))["observed_human_interventions"] is None
    gapped = record(reserved(human=True), observation(human=observer(gaps=["driver-restart"])))
    assert accounting.report(close(gapped))["observed_human_interventions"] is None


def test_human_counter_regression_or_unknown_initial_reading_preserves_unknown():
    for initial in (None, 1):
        plan = opened(human=True)
        baseline = plan["observations"][0]["envelope"]
        baseline["human_observer"]["human_interventions"] = initial
        baseline = signed(baseline)
        plan = accounting.start_coverage(binding=BINDING, scope_id="research-only", baseline=baseline,
            verifier=verifier(), now_utc=NOW)
        plan = accounting.reserve_attempt(plan, attempt())
        plan = record(plan, observation(human=observer(count=0)))
        assert accounting.report(close(plan))["observed_human_interventions"] is None


def test_empty_scope_requires_explicit_signed_zero_baseline():
    baseline = observation("baseline", attempt_id=None, sequence=0, values=usage(cost=None), slots=["baseline"],
        state="started", final=False)
    with pytest.raises(EvidenceWikiError):
        accounting.start_coverage(binding=BINDING, scope_id="research-only", baseline=baseline,
            verifier=verifier(), now_utc=NOW)
    assert accounting.report(close(opened()))["totals"] == usage()


def test_closed_inventory_and_original_reservations_are_immutable():
    plan = reserved()
    assert accounting.reserve_attempt(plan, attempt()) == plan
    with pytest.raises(EvidenceWikiError):
        accounting.reserve_attempt(plan, attempt(reservation=usage(cost="9")))
    closed = close(plan)
    with pytest.raises(EvidenceWikiError):
        accounting.reserve_attempt(closed, attempt("extra"))
    with pytest.raises(EvidenceWikiError):
        accounting.declare_slot(closed, slot_id="new", controller_ref="host", declared_at=NOW)
    changed = copy.deepcopy(closed)
    changed["attempts"][0]["reservation"]["cost"] = "0"
    with pytest.raises(EvidenceWikiError):
        accounting.validate_document(accounting.PLAN, changed)


@pytest.mark.parametrize("bad", [1.25, "-1", "NaN", "1e2", "0.1234567890123"])
def test_inexact_or_unbounded_amounts_refuse(bad):
    with pytest.raises(EvidenceWikiError):
        record(reserved(), observation(values=usage(cost=bad)))


def test_missing_logical_meter_sequence_retains_cost_and_reservation_after_close():
    first = observation("first", sequence=1, final=False, state="started", values=usage(cost="0.1"))
    third = observation("third", sequence=3, values=usage(cost="0.3"))
    plan = record(record(reserved(), first), third)
    value = accounting.report(close(plan))
    assert value["coverage"]["missing_sequence_ranges"] == [{"attempt_id": "attempt", "first_sequence": 2, "last_sequence": 2}]
    assert value["coverage"]["usage_unknown"] and not value["coverage"]["coverage_complete"]
    assert value["coverage"]["spent"]["cost"] == "0.3" and value["coverage"]["outstanding"]["cost"] == "1"
    assert value["totals"]["cost"] is None


def test_late_missing_sequence_settles_prefix_in_logical_order_without_reordering_journal():
    first = observation("first", sequence=1, final=False, state="started", values=usage(cost="0.1"))
    third = observation("third", sequence=3, values=usage(cost="0.3"))
    second = observation("second", sequence=2, final=False, state="started", values=usage(cost="0.2"))
    plan = record(record(reserved(), first), third)
    closed = close(plan)
    repaired_open = record(plan, second)
    repaired_closed = record(closed, second)
    assert [row["envelope"]["sequence"] for row in repaired_open["observations"]] == [0, 1, 3, 2]
    assert accounting.projection(repaired_open)["missing_sequence_ranges"] == []
    assert not accounting.projection(repaired_open)["usage_unknown"]
    assert accounting.projection(repaired_open)["spent"]["cost"] == "0.3"
    assert accounting.report(close(repaired_open))["totals"]["cost"] == "0.3"
    assert not accounting.report(repaired_closed)["coverage"]["coverage_complete"]
    assert accounting.report(repaired_closed)["totals"]["cost"] is None
    assert record(repaired_open, second) == repaired_open
    with pytest.raises(EvidenceWikiError) as error:
        record(repaired_open, observation("changed-second", sequence=2, values=usage(cost="0.9")))
    assert error.value.error_code == "ACCOUNTING_SEQUENCE_CONFLICT"


def test_late_sequence_with_earlier_observation_time_is_not_a_clock_regression():
    first = observation("first", sequence=1, final=False, state="started", values=usage(cost="0.1"))
    first["observed_at"] = "2026-10-03T00:00:01Z"
    third = observation("third", sequence=3, values=usage(cost="0.3"))
    third["observed_at"] = "2026-10-03T00:00:03Z"
    second = observation("second", sequence=2, final=False, state="started", values=usage(cost="0.2"))
    plan = record(record(record(reserved(), signed(first)), signed(third)), second)
    assert accounting.projection(plan)["conflicting_attempt_ids"] == []
    assert accounting.report(close(plan))["totals"]["cost"] == "0.3"


def test_late_sequence_retains_actual_clock_and_cumulative_meter_regressions():
    first = observation("first", sequence=1, final=False, state="started", values=usage(cost="0.2"))
    third = observation("third", sequence=3, values=usage(cost="0.3"))
    second = observation("second", sequence=2, final=False, state="started", values=usage(cost="0.1"))
    plan = record(record(record(reserved(), first), third), second)
    value = accounting.projection(plan)
    assert value["missing_sequence_ranges"] == [] and value["conflicting_attempt_ids"] == ["attempt"]
    assert value["usage_unknown"] and value["spent"]["cost"] == "0.3"


def test_lost_initial_sequence_and_huge_sequence_numbers_use_bounded_ranges():
    plan = record(reserved(), observation("later", sequence=1_000_000))
    value = accounting.projection(plan)
    assert value["missing_sequence_ranges"] == [{"attempt_id": "attempt", "first_sequence": 1, "last_sequence": 999_999}]
    assert value["usage_unknown"]


def test_exact_twelve_place_costs_preserve_precision_and_rounding_context_independence():
    actual = usage(cost="0.000000000001")
    plan = accounting.reserve_attempt(opened(), attempt("first"))
    plan = accounting.reserve_attempt(plan, attempt("second"))
    plan = record(plan, observation("first", attempt_id="first", values=actual))
    plan = record(plan, observation("second", attempt_id="second", values=actual))
    with localcontext() as context:
        context.prec = 1
        context.rounding = ROUND_UP
        context.traps[Inexact] = True
        value = accounting.report(close(plan))
        assert value["totals"]["cost"] == "0.000000000002"
        remaining = accounting.projection(plan, limits=usage(seconds="10", models=2, tools=4, tokens=100,
            cost="0.000000000003"))["remaining"]["cost"]
        assert remaining == "0.000000000001"
    with pytest.raises(EvidenceWikiError):
        record(reserved(), observation(values=usage(seconds="0.0000000001")))
