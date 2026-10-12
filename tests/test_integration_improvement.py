"""Current original evidence governs bounded integration planning."""
from dataclasses import replace
from datetime import datetime, timezone
from decimal import localcontext
from hashlib import sha256

import pytest

from tools import _integration_improvement as gate
from tools._release_decision import OriginalCitation, canonical, digest

NOW = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
START = "2030-01-01T11:00:00Z"
END = "2030-01-01T13:00:00Z"


def h(value):
    return sha256(value.encode()).hexdigest()


def proposal(name="repair", kind="operational_repair", effort="1", metric="repair_attempts", authorized=False):
    return {"proposal_id": name, "kind": kind, "estimated_effort": effort, "effort_unit": "agent_work_units",
            "trigger_metric": metric, "trigger_threshold": 1, "capability_authorized": authorized}


def fixture(overrides=None, proposals=None):
    hosts = [{"host_id": route, "route": route, "host_sha256": h(route), "scenario_sha256": h("scenario" + route)}
             for route in gate.ROUTES]
    slots = [{"slot_id": host["host_id"] + ":" + cycle, "host_id": host["host_id"], "cycle": cycle,
              "input_sha256": h(host["host_id"] + cycle), "owner_ref": host["host_id"], "elapsed_cap_seconds": "10"}
             for host in hosts for cycle in gate.CYCLES]
    plan = gate.freeze_plan(candidate_sha256=h("candidate"), hosts=hosts, slots=slots, scope={"kind": "fixture"},
                            frozen_at=START, expires_at=END, proposals=proposals)
    natives, pins = {}, {}
    for slot in slots:
        p = {"disposition": "completed", "integration_success": True, "accepted": True, "useful": True,
             "usefulness_scope": "fixture_mechanics", "unsupported_scope": False, "elapsed_seconds": "1.1",
             "repair_attempts": 0, "review_attempts": 1, "source_drift_detected": slot["cycle"] != "baseline",
             "stale_approval_reused": False, "history_preserved": True, "critical_failure": False,
             "omission_count": None, "host_limit_count": None, "expansion_need_count": None, "supported_proposals": []}
        p.update((overrides or {}).get(slot["slot_id"], {}))
        raw = canonical({"projection": p, "prose": "An attractive but irrelevant explanation."})
        native = gate.OwnerEvidence(raw, h(slot["slot_id"]), plan["candidate_sha256"], h(slot["host_id"]),
                                    slot["input_sha256"], START, END, "deterministic_verified", p,
                                    (OriginalCitation("/projection", p),))
        natives[slot["slot_id"]] = native
        pins[slot["slot_id"]] = {"raw_sha256": sha256(raw).hexdigest(), "source_head_sha256": native.source_head_sha256}
    readers = [gate.OwnerReader(slot["slot_id"], slot["owner_ref"], lambda sid=slot["slot_id"]: natives[sid]) for slot in slots]
    return plan, natives, pins, readers


def run(data):
    plan, _, pins, readers = data
    return gate.decide(plan, digest(plan), pins, digest(pins), readers, NOW)


def test_complete_fixture_continues_and_replays_without_business_claim():
    data = fixture()
    result = run(data)
    assert result["outcome"] == "continue"
    assert result["metrics"]["planned_slots"] == result["metrics"]["terminal_slots"] == 9
    assert result["metrics"]["native_scope_burden"]["elapsed_seconds"]["total"] == "9.9"
    assert result["limitations"]["provider_total"] is None
    assert result["limitations"]["business_value"] == "business_value_unobserved"
    assert result["implementation_authorized"] is False
    plan, _, pins, readers = data
    assert gate.replay_decision(result, digest(plan), digest(pins), list(reversed(readers)), NOW) == result


@pytest.mark.parametrize("changes,outcome,code", [
    ({"critical_failure": True}, "stop", "unsafe_invariant"),
    ({"stale_approval_reused": True}, "stop", "unsafe_invariant"),
    ({"history_preserved": False}, "stop", "unsafe_invariant"),
    ({"elapsed_seconds": "10.000000000000000001"}, "stop", "elapsed_budget_exceeded"),
    ({"disposition": "failed", "integration_success": False}, "revise", "functional_failure"),
    ({"disposition": "unstarted", "elapsed_seconds": None}, "revise", "terminal_unknown"),
    ({"disposition": "started_stop_unknown", "elapsed_seconds": None}, "revise", "terminal_unknown"),
    ({"review_attempts": None}, "revise", "required_measurement_unknown"),
    ({"useful": None, "usefulness_scope": "unknown"}, "revise", "accepted_usefulness_unknown"),
    ({"useful": False}, "narrow", "accepted_scope_not_useful"),
    ({"disposition": "refused", "accepted": False, "useful": None, "unsupported_scope": True}, "narrow", "unsupported_scope"),
])
def test_frozen_classification(changes, outcome, code):
    result = run(fixture({"direct:baseline": changes}))
    assert result["outcome"] == outcome
    assert code in {reason["code"] for reason in result["reasons"]}


def test_missing_original_preserves_slot_and_unknown_denominator():
    plan, natives, pins, readers = fixture()
    sid = "direct:baseline"
    del pins[sid]
    result = run((plan, natives, pins, readers))
    assert result["outcome"] == "revise"
    assert len(result["slots"]) == 9
    assert result["metrics"]["terminal_slots"] == 8
    assert result["metrics"]["native_scope_burden"]["elapsed_seconds"]["total"] is None
    assert result["metrics"]["native_scope_burden"]["elapsed_seconds"]["known_subtotal"] == "8.8"


@pytest.mark.parametrize("field,value", [("candidate_sha256", h("other")), ("host_sha256", h("other")),
                                         ("input_sha256", h("other")), ("source_head_sha256", h("other")),
                                         ("raw", canonical({"edited": True})),
                                         ("citations", (OriginalCitation("/projection", False),))])
def test_binding_and_citation_integrity_stop(field, value):
    data = fixture()
    data[1]["direct:baseline"] = replace(data[1]["direct:baseline"], **{field: value})
    assert run(data)["outcome"] == "stop"


def test_closing_withdrawal_preserves_known_critical_failure():
    plan, natives, pins, readers = fixture({"direct:baseline": {"critical_failure": True}})
    calls = []
    def withdrawn():
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("owner withdrawn")
        return natives["direct:baseline"]
    readers[0] = gate.OwnerReader("direct:baseline", "direct", withdrawn)
    result = run((plan, natives, pins, readers))
    assert result["outcome"] == "stop"
    assert len(calls) == 2


def test_final_expiry_rechecked_after_all_owner_reads(monkeypatch):
    data = fixture()
    late = datetime(2030, 1, 1, 13, tzinfo=timezone.utc)
    clocks = iter([NOW] * 19 + [late, late])
    monkeypatch.setattr(gate, "_clock", lambda _: next(clocks))
    result = run(data)
    assert result["outcome"] == "revise"
    assert "owner_expired_at_close" in {reason["code"] for reason in result["reasons"]}


def test_changed_original_between_reads_stops():
    data = fixture()
    calls = []
    native = data[1]["direct:baseline"]
    def changed():
        calls.append(1)
        return native if len(calls) == 1 else replace(native, evidence_class="simulated")
    data[3][0] = gate.OwnerReader("direct:baseline", "direct", changed)
    assert run(data)["outcome"] == "stop"


def test_plan_thresholds_and_result_pin_require_external_identity():
    plan, _, pins, readers = fixture()
    original = digest(plan)
    plan["slots"][0]["elapsed_cap_seconds"] = "999"
    with pytest.raises(ValueError, match="integration_plan_pin"):
        gate.decide(plan, original, pins, digest(pins), readers, NOW)
    data = fixture()
    pin = digest(data[2])
    data[2]["direct:baseline"]["raw_sha256"] = h("changed")
    with pytest.raises(ValueError, match="integration_evidence_pin"):
        gate.decide(data[0], digest(data[0]), data[2], pin, data[3], NOW)


def test_repeated_measured_optimization_ranked_by_effort_then_effect():
    catalog = [proposal("large", "review_dependencies", "5", "review_attempts"),
               proposal("small", "integration_docs", "1", "review_attempts"), proposal("repair", effort="1")]
    overrides = {sid: {"review_attempts": 2, "repair_attempts": 1,
                       "supported_proposals": ["small", "large", "repair"]}
                 for sid in ("direct:change_1", "current_caller:change_1")}
    result = run(fixture(overrides, catalog))
    assert result["outcome"] == "revise"
    assert [item["proposal"]["proposal_id"] for item in result["ranked_improvements"]] == ["small", "repair", "large"]
    assert result["next_improvement"]["proposal"]["proposal_id"] == "small"
    assert result["implementation_authorized"] is False


def test_repetition_within_one_host_does_not_support_optimization():
    changes = {"direct:" + cycle: {"supported_proposals": ["dependencies"]} for cycle in gate.CYCLES}
    result = run(fixture(changes, [proposal("dependencies", "review_dependencies", metric="review_attempts")]))
    assert result["ranked_improvements"] == []
    assert result["outcome"] == "continue"


def test_ungranted_retrieval_stays_excluded_despite_repeated_signal():
    changes = {sid: {"omission_count": 1, "supported_proposals": ["retrieval"]}
               for sid in ("direct:baseline", "current_caller:baseline")}
    result = run(fixture(changes, [proposal("retrieval", "retrieval", metric="omission_count")]))
    assert result["ranked_improvements"] == []
    assert result["outcome"] == "continue"


def test_no_real_input_identity_change_is_rejected():
    plan, *_ = fixture()
    plan["slots"][1]["input_sha256"] = plan["slots"][0]["input_sha256"]
    with pytest.raises(ValueError, match="integration_change_cycles"):
        gate.validate_plan(plan, digest(plan))


def test_unknown_optional_demand_not_zero_and_does_not_gate_mechanics():
    result = run(fixture())
    assert result["outcome"] == "continue"
    assert all(row["evidence"]["projection"]["expansion_need_count"] is None for row in result["slots"])


def test_polished_prose_cannot_override_original_failure():
    result = run(fixture({"direct:baseline": {"disposition": "failed", "integration_success": False}}))
    assert result["outcome"] == "revise"


def test_semantic_label_without_current_native_judge_is_not_usefulness():
    result = run(fixture({"direct:baseline": {"usefulness_scope": "qualified_semantic"}}))
    assert result["outcome"] == "revise"


def test_invariant_stop_overrides_functional_revise_and_scope_narrow():
    result = run(fixture({"direct:baseline": {"critical_failure": True},
                          "current_caller:baseline": {"integration_success": False},
                          "scoped_mcp:baseline": {"useful": False}}))
    assert result["outcome"] == "stop"


def test_started_unknown_retains_measured_subtotal_without_claiming_complete_total():
    result = run(fixture({"direct:baseline": {"disposition": "started_stop_unknown"}}))
    metrics = result["metrics"]
    assert metrics["terminal_slots"] == metrics["accepted_slots"] == 8
    elapsed = metrics["native_scope_burden"]["elapsed_seconds"]
    assert elapsed["known_subtotal"] == "9.9"
    assert elapsed["total"] is None
    assert elapsed["unknown_slots"] == ["direct:baseline"]


@pytest.mark.parametrize("changes", [
    {"disposition": "refused"},
    {"disposition": "refused", "accepted": False, "useful": True},
    {"disposition": "failed", "accepted": False, "useful": False},
    {"accepted": False, "useful": True},
])
def test_contradictory_terminal_assertions_cannot_continue(changes):
    result = run(fixture({"direct:baseline": changes}))
    assert result["outcome"] == "revise"
    assert "contradictory_terminal_projection" in {reason["code"] for reason in result["reasons"]}
    assert result["metrics"]["accepted_slots"] <= 8


def test_dependency_optimization_requires_measured_change_hosts_not_baseline():
    catalog = [proposal("dependencies", "review_dependencies", metric="review_attempts")]
    baseline = {sid: {"review_attempts": 3, "supported_proposals": ["dependencies"]}
                for sid in ("direct:baseline", "current_caller:baseline")}
    assert run(fixture(baseline, catalog))["ranked_improvements"] == []
    changes = {sid: {"review_attempts": 2, "supported_proposals": ["dependencies"]}
               for sid in ("direct:change_1", "current_caller:change_2")}
    result = run(fixture(changes, catalog))
    assert result["ranked_improvements"][0]["distinct_host_scenarios"] == 2
    assert result["ranked_improvements"][0]["observed_effect"] == "4"


@pytest.mark.parametrize("kind,metric", [("retrieval", "omission_count"), ("host_capability", "host_limit_count"),
                                        ("capability_expansion", "expansion_need_count")])
def test_simulated_demand_selects_qualification_experiment_without_expansion_grant(kind, metric):
    catalog = [proposal("experiment", kind, metric=metric, authorized=True)]
    changes = {sid: {metric: 1, "supported_proposals": ["experiment"]}
               for sid in ("direct:change_1", "current_caller:change_1")}
    result = run(fixture(changes, catalog))
    selected = result["next_improvement"]
    assert selected["recommendation_scope"] == "next_qualification_experiment"
    assert selected["substantive_expansion_qualified"] is False
    assert selected["implementation_authorized"] is False
    assert result["implementation_authorized"] is False


def test_ambient_decimal_precision_cannot_change_report_metrics_or_ranking():
    catalog = [proposal("a", metric="review_attempts"), proposal("b", metric="repair_attempts")]
    changes = {sid: {"elapsed_seconds": "1.123456789123456789", "review_attempts": 123456789,
                     "repair_attempts": 123456780, "supported_proposals": ["a", "b"]}
               for sid in ("direct:change_1", "current_caller:change_1")}
    data = fixture(changes, catalog)
    expected = run(data)
    with localcontext() as context:
        context.prec = 2
        observed = run(data)
        assert gate.replay_decision(expected, digest(data[0]), digest(data[2]), data[3], NOW) == expected
    assert observed == expected
    assert observed["ranked_improvements"][0]["proposal"]["proposal_id"] == "a"


def test_expiry_during_ranking_or_summary_removes_eligible_improvement(monkeypatch):
    changes = {"direct:change_1": {"repair_attempts": 1, "supported_proposals": ["repair"]}}
    data = fixture(changes, [proposal()])
    late = datetime(2030, 1, 1, 13, tzinfo=timezone.utc)
    clock = [NOW]
    original_summary = gate._summary
    def delayed_summary(rows):
        result = original_summary(rows)
        clock[0] = late
        return result
    monkeypatch.setattr(gate, "_clock", lambda _: clock[0])
    monkeypatch.setattr(gate, "_summary", delayed_summary)
    result = run(data)
    assert result["outcome"] == "revise"
    assert result["ranked_improvements"] == []
    assert result["next_improvement"] is None
    assert result["metrics"]["terminal_slots"] == 0
    assert result["metrics"]["native_scope_burden"]["elapsed_seconds"]["total"] is None
    assert "owner_expired_at_close" in {reason["code"] for reason in result["reasons"]}


@pytest.mark.parametrize("code", sorted(gate.INTEGRITY_ERRORS))
def test_native_integrity_errors_stop_at_policy_boundary(code):
    data = fixture()
    def changed():
        raise ValueError(code)
    data[3][0] = gate.OwnerReader("direct:baseline", "direct", changed)
    assert run(data)["outcome"] == "stop"


@pytest.mark.parametrize("error", [RuntimeError("unavailable"),
    ValueError("integration_original_command_coverage_unknown"),
    ValueError("integration_owner_current"), ValueError("integration_original_current_probe_missing")])
def test_unknown_or_expired_owner_remains_revision(error):
    data = fixture()
    def unavailable():
        raise error
    data[3][0] = gate.OwnerReader("direct:baseline", "direct", unavailable)
    assert run(data)["outcome"] == "revise"


@pytest.mark.parametrize("error,outcome", [
    (ValueError("integration_closing_candidate_or_host_changed"), "stop"),
    (OSError("unavailable"), "revise")])
def test_trusted_closure_failure_is_classified_and_replay_checks_it(error, outcome):
    plan, _, pins, readers = fixture()
    def failed():
        raise error
    report = gate.decide(plan, digest(plan), pins, digest(pins), readers, NOW, closing_check=failed)
    assert report["outcome"] == outcome
    assert gate.replay_decision(report, digest(plan), digest(pins), readers, NOW, closing_check=failed) == report
    with pytest.raises(ValueError, match="integration_replay_changed"):
        gate.replay_decision(report, digest(plan), digest(pins), readers, NOW, closing_check=lambda: None)


def test_trusted_closure_expiry_removes_ranking_and_preserves_canonical_replay(monkeypatch):
    data = fixture({"direct:change_1": {"repair_attempts": 1, "supported_proposals": ["repair"]}}, [proposal()])
    plan, _, pins, readers = data
    clock = [NOW]
    calls = []
    monkeypatch.setattr(gate, "_clock", lambda _: clock[0])
    stable = lambda: calls.append("closed")
    report = gate.decide(plan, digest(plan), pins, digest(pins), readers, closing_check=stable)
    assert gate.replay_decision(report, digest(plan), digest(pins), readers, closing_check=stable) == report
    assert calls == ["closed", "closed"]
    def expires():
        clock[0] = datetime(2030, 1, 1, 13, tzinfo=timezone.utc)
    expired = gate.decide(plan, digest(plan), pins, digest(pins), readers, closing_check=expires)
    assert expired["outcome"] == "revise"
    assert expired["next_improvement"] is None
    assert expired["metrics"]["terminal_slots"] == 0
    assert all(row["problem"] == "owner_expired_at_close" for row in expired["slots"])



def test_staggered_expiry_during_recomputation_reseals_metrics_and_rank(monkeypatch):
    changes = {sid: {"repair_attempts": 1, "supported_proposals": ["repair"]}
               for sid in ("direct:change_1", "current_caller:change_1")}
    plan, natives, pins, readers = fixture(changes, [proposal()])
    natives["direct:change_1"] = replace(natives["direct:change_1"], expires_at="2030-01-01T12:00:01Z")
    natives["current_caller:change_1"] = replace(natives["current_caller:change_1"], expires_at="2030-01-01T12:00:02Z")
    clock = [NOW]
    monkeypatch.setattr(gate, "_clock", lambda _: clock[0])
    original_summary = gate._summary
    summaries = []
    def summary(rows):
        summaries.append(1)
        result = original_summary(rows)
        if len(summaries) == 2:
            clock[0] = datetime(2030, 1, 1, 12, 0, 2, tzinfo=timezone.utc)
        return result
    monkeypatch.setattr(gate, "_summary", summary)
    def closing_check():
        clock[0] = datetime(2030, 1, 1, 12, 0, 1, tzinfo=timezone.utc)
    report = gate.decide(plan, digest(plan), pins, digest(pins), readers, closing_check=closing_check)
    assert len(summaries) == 3
    expired = {row["slot"]["slot_id"] for row in report["slots"] if row["problem"] == "owner_expired_at_close"}
    assert expired == {"direct:change_1", "current_caller:change_1"}
    assert report["metrics"]["terminal_slots"] == 7
    assert report["ranked_improvements"] == [] and report["next_improvement"] is None
    assert report["metrics"]["native_scope_burden"]["repair_attempts"]["known_subtotal"] == "0"


def test_plan_expiry_during_final_recomputation_is_checked(monkeypatch):
    plan, natives, pins, readers = fixture()
    natives["direct:baseline"] = replace(natives["direct:baseline"], expires_at="2030-01-01T12:00:01Z")
    clock = [NOW]
    monkeypatch.setattr(gate, "_clock", lambda _: clock[0])
    original_summary = gate._summary
    calls = []
    def summary(rows):
        calls.append(1)
        result = original_summary(rows)
        if len(calls) == 2:
            clock[0] = datetime(2030, 1, 1, 13, tzinfo=timezone.utc)
        return result
    monkeypatch.setattr(gate, "_summary", summary)
    def closing_check():
        clock[0] = datetime(2030, 1, 1, 12, 0, 1, tzinfo=timezone.utc)
    report = gate.decide(plan, digest(plan), pins, digest(pins), readers, closing_check=closing_check)
    assert report["metrics"]["terminal_slots"] == 0
    assert "plan_not_current_at_close" in {item["code"] for item in report["reasons"]}
