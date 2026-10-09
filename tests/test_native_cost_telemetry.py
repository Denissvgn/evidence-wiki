"""Fresh native trust, final meter projection and scoped human observation."""

import copy
import json

import pytest

from evidence_wiki import execution_accounting as accounting
from evidence_wiki.errors import EvidenceWikiError
from tests import test_execution_accounting as meters
from tools._integration_costs import PHASES
from tools._native_cost_telemetry import from_native_accounting


def attribution(attempt_id="attempt", phase="integration", allocation="one_time"):
    return {"attempt_id": attempt_id, "phase": phase, "allocation": allocation}


def convert(plan, *, attributions=None, verifiers=None, **kwargs):
    return from_native_accounting(plan, {"host": meters.verifier()} if verifiers is None else verifiers,
        scope_id="cost-scope", attributions=[attribution()] if attributions is None else attributions,
        now_utc=kwargs.pop("now_utc", meters.NOW), **kwargs)


def closed(*, human=False, observation=None):
    return meters.close(meters.record(meters.reserved(human=human),
        meters.observation(human=meters.observer() if human else None) if observation is None else observation))


def test_fresh_authentication_preserves_scoped_zero_without_external_promotion_or_secrets():
    plan = closed(human=True)
    before = copy.deepcopy(plan)
    requests = []
    original = meters.verifier()

    def verify(request):
        requests.append(request["envelope_sha256"])
        return original.verify(request)

    value = convert(plan, verifiers={"host": accounting.CoverageVerifier("host", verify)})
    qualification = value["native_verification"]
    assert len(requests) == len(plan["observations"]) + 1
    assert qualification["human"] == {"status": "observed_scoped", "observed_human_interventions": 0,
                                      "human_seconds": None}
    assert qualification["coverage"]["coverage_complete"] is True
    assert qualification["native_scope_id"] == "research-only"
    assert qualification["whole_host_coverage_authenticated"] is False
    assert qualification["provider_billing_truth"] == "unqualified"
    assert qualification["authorized"] is qualification["evidence_accepted"] is False
    assert value["report"]["coverage"]["host_coverage_authenticated"] is False
    assert value["report"]["external_observations"] == []
    assert value["report"]["external_status"]["human"] == "unobserved"
    assert value["report"]["external_status"]["authentication"] == "unproved"
    encoded = json.dumps(value)
    assert all(row["envelope"]["authentication"] not in encoded for row in plan["observations"])
    assert plan == before


@pytest.mark.parametrize("mode", ["absent", "no_initial", "partial", "gap", "excluded", "other_observer",
                                   "unknown_initial", "imported"])
def test_human_observation_requires_continuous_measured_same_observer_and_initial_counter(mode):
    human = mode not in {"absent", "no_initial"}
    plan = meters.opened(human=human)
    if mode == "unknown_initial":
        baseline = copy.deepcopy(plan["observations"][0]["envelope"])
        baseline["human_observer"]["human_interventions"] = None
        plan = accounting.start_coverage(binding=meters.BINDING, scope_id="research-only",
            baseline=meters.signed(baseline), verifier=meters.verifier(), now_utc=meters.NOW)
    plan = accounting.reserve_attempt(plan, meters.attempt())
    observer = None if mode == "absent" else meters.observer()
    if mode == "partial":
        observer["observed_through_at"] = "2026-10-03T00:00:02Z"
    elif mode == "gap":
        observer["gaps"] = ["lost-observation"]
    elif mode == "excluded":
        observer["exclusions"] = ["manual-review"]
    elif mode == "other_observer":
        observer["observer_id"] = "different-observer"
    row = meters.observation(human=observer, provenance="imported" if mode == "imported" else "measured")
    value = convert(meters.close(meters.record(plan, row)))
    assert value["native_verification"]["human"]["observed_human_interventions"] is None
    assert value["native_verification"]["human"]["status"] == "unknown"


@pytest.mark.parametrize("mode", ["rejected", "expired", "producer_mismatch", "missing", "inert"])
def test_prior_certificates_cannot_replace_fresh_trusted_verification(mode):
    def verify(request):
        if mode == "rejected":
            raise ValueError("producer rejected")
        value = meters.verifier().verify(request)
        if mode == "producer_mismatch":
            value["controller_ref"] = "other-producer"
        return value

    verifiers = {"host": accounting.CoverageVerifier("host", verify)}
    if mode == "missing":
        verifiers = {}
    elif mode == "inert":
        verifiers = {"host": {"authenticated": True}}
    with pytest.raises(EvidenceWikiError):
        convert(closed(), verifiers=verifiers,
                now_utc="2026-10-03T00:00:11Z" if mode == "expired" else meters.NOW)


@pytest.mark.parametrize("mutation", ["omit", "extra", "duplicate", "open_shape", "bad_phase", "bad_allocation"])
def test_every_failed_native_attempt_requires_exact_closed_attribution(mutation):
    plan = accounting.reserve_attempt(meters.reserved(), meters.attempt("repair-attempt"))
    plan = meters.record(plan, meters.observation(state="failed"))
    plan = meters.record(plan, meters.observation("repair-result", attempt_id="repair-attempt"))
    rows = [attribution(), attribution("repair-attempt", "repair")]
    if mutation == "omit":
        rows.pop(0)
    elif mutation == "extra":
        rows.append(attribution("invented"))
    elif mutation == "duplicate":
        rows.append(rows[0])
    elif mutation == "open_shape":
        rows[0]["authenticated"] = True
    elif mutation == "bad_phase":
        rows[0]["phase"] = "other"
    else:
        rows[0]["allocation"] = "free"
    with pytest.raises(ValueError, match="native_attribution"):
        convert(meters.close(plan), attributions=rows)


def test_failed_and_repair_work_are_retained_with_missing_phase_placeholders():
    plan = accounting.reserve_attempt(meters.reserved(), meters.attempt("repair-attempt"))
    plan = meters.record(plan, meters.observation(state="failed"))
    plan = meters.record(plan, meters.observation("repair-result", attempt_id="repair-attempt"))
    value = convert(meters.close(plan), attributions=[attribution(), attribution("repair-attempt", "repair")])
    assert len(value["events"]) == 2
    assert value["report"]["automated"]["status_counts"]["failed"] == 1
    assert value["report"]["automated"]["cost"]["subtotals"][0]["amount"] == "0.25"
    assert value["report"]["automated"]["cost"]["total"] is None
    assert {row["phase"] for row in value["plan"]["attempts"]} == set(PHASES)
    assert len(value["report"]["coverage"]["missing_attempt_ids"]) == 5
    assert value["native_verification"]["unobserved_phases"] == [phase for phase in PHASES
                                                                if phase not in {"integration", "repair"}]


def test_repeated_cumulative_final_receipts_are_billed_once_without_overlap_or_wait_inference():
    plan = meters.record(meters.reserved(), meters.observation("prefix", final=False, state="started",
        values=meters.usage(seconds="0.5", models=1, tools=1, tokens=5, cost="0.05")))
    plan = meters.record(plan, meters.observation(sequence=2))
    plan = meters.record(plan, meters.observation("repeated-final", sequence=3))
    value = convert(meters.close(plan))
    assert len(value["events"]) == 1
    metrics = value["events"][0]["metrics"]
    assert metrics["cost"]["amount"] == "0.125"
    assert metrics["tokens"] == 10
    assert metrics["elapsed_seconds"] == "1.25"
    assert metrics["model_wait_seconds"] is None
    assert value["events"][0]["started_at"] is value["events"][0]["finished_at"] is None
    assert value["report"]["intervals"]["union_seconds"] is None


@pytest.mark.parametrize("mode", ["missing", "unknown", "partial", "gap", "conflict", "pricing", "currency",
                                   "imported", "estimated"])
def test_unsettled_meters_never_become_known_costs(mode):
    plan = meters.reserved()
    if mode != "missing":
        row = meters.observation(sequence=2 if mode == "gap" else 1,
            final=mode != "partial", state="started" if mode == "partial" else "completed",
            values=meters.usage(cost=None) if mode == "unknown" else None,
            unit={"currency": "USD", "pricing_ref": "different-rate"} if mode == "pricing" else
                 {"currency": "EUR", "pricing_ref": "meter-rate-v1"} if mode == "currency" else None,
            provenance=mode if mode in {"imported", "estimated"} else "measured")
        plan = meters.record(plan, row)
        if mode == "conflict":
            plan = meters.record(plan, meters.observation("correction", sequence=2, values=meters.usage(cost="0.3")))
    value = convert(meters.close(plan))
    assert value["native_verification"]["coverage"]["coverage_complete"] is False
    assert value["report"]["automated"]["cost"]["total"] is None
    assert value["report"]["automated"]["cost"]["subtotals"] == []
    assert all(row["metrics"]["cost"] is None for row in value["events"])


def test_missing_meter_retains_compatible_subtotal_from_other_attempt():
    plan = accounting.reserve_attempt(meters.reserved(), meters.attempt("missing-meter"))
    value = convert(meters.close(meters.record(plan, meters.observation())),
                    attributions=[attribution(), attribution("missing-meter", "repair")])
    assert value["report"]["automated"]["cost"]["subtotals"][0]["amount"] == "0.125"
    assert value["report"]["automated"]["cost"]["total"] is None


def test_forged_native_provenance_and_digest_cannot_bypass_authentication():
    plan = meters.record(meters.reserved(), meters.observation(provenance="imported"))
    retained = plan["observations"][-1]
    retained["envelope"]["provenance"] = "measured"
    retained["verification"]["envelope_sha256"] = accounting.digest(retained["envelope"])
    with pytest.raises(EvidenceWikiError):
        convert(plan)


def test_unclosed_scope_cannot_claim_human_zero_or_closed_coverage():
    value = convert(meters.record(meters.reserved(human=True), meters.observation(human=meters.observer())))
    assert value["native_verification"]["closed_at"] is None
    assert value["native_verification"]["human"]["status"] == "unknown"
    assert value["report"]["automated"]["cost"]["total"] is None


def test_empty_authenticated_native_scope_has_all_unobserved_cost_phases():
    value = convert(meters.close(meters.opened()), attributions=[])
    assert value["native_verification"]["coverage"]["coverage_complete"] is True
    assert value["native_verification"]["unobserved_phases"] == list(PHASES)
    assert value["events"] == []
    assert len(value["plan"]["attempts"]) == len(PHASES)
    assert value["report"]["automated"]["cost"]["subtotals"] == []
    repeated = {"operation", "review", "evidence_update"}
    assert {row["phase"] for row in value["plan"]["attempts"] if row["allocation"] == "repeated"} == repeated
    assert not any(row["allocation"] == "one_time" and row["phase"] in repeated
                   for row in value["plan"]["attempts"])
    assert value["report"]["by_allocation"]["repeated"]["cost"]["total"] is None
    assert value["report"]["by_allocation"]["one_time"]["cost"]["total"] is None


def test_all_attributed_phases_keep_exact_costs_and_unknown_model_wait_coverage():
    plan = meters.opened()
    rows = []
    for phase in PHASES:
        plan = accounting.reserve_attempt(plan, meters.attempt(phase))
        plan = meters.record(plan, meters.observation("result-" + phase, attempt_id=phase,
            state="failed" if phase == "repair" else "completed"))
        rows.append(attribution(phase, phase, "repeated" if phase == "operation" else "one_time"))
    value = convert(meters.close(plan), attributions=rows, repeat_use_count=3)
    assert value["native_verification"]["unobserved_phases"] == []
    assert value["report"]["automated"]["cost"]["total"]["amount"] == "0.875"
    assert value["report"]["automated"]["metrics"]["model_wait_seconds"]["total"] is None
    assert value["report"]["coverage"]["complete"] is False
    assert value["report"]["allocation"]["one_time_per_use"] is None
    assert "spent" not in value["native_verification"]["coverage"]
    assert "cost_subtotals" not in value["native_verification"]["coverage"]
