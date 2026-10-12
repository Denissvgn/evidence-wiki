"""Pinned release decisions retain native evidence and bounded claim scopes."""

import copy
import hashlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from tools import _release_decision as release

NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
START = (NOW - timedelta(minutes=1)).isoformat()
END = (NOW + timedelta(minutes=5)).isoformat()
CANDIDATE = "a" * 64
HOST = "b" * 64
HEAD = "c" * 64
SCOPE = {"kind": "fixture_mechanics", "target": "local retained owner"}
USAGE = {"elapsed_seconds": 0.1, "model_calls": 0, "tool_calls": 1, "tokens": 0, "cost": 0.1}
CAPS = {"elapsed_seconds": 10, "model_calls": 1, "tool_calls": 10, "tokens": 10, "cost": 1}
JUDGE = {"domain": "bounded domain", "rubric_sha256": "d" * 64, "model": "qualified model",
         "settings_sha256": "e" * 64, "controller_sha256": "f" * 64, "profile": "retained exact judge"}


def fixture(*, statuses=None, business=False, profile="technical_fixture", budgets=None):
    statuses = statuses or {}
    sources, evidence, readers = [], {}, []
    categories = (*release.CATEGORIES, "business") if business else release.CATEGORIES
    for category in categories:
        raw = release.canonical({"decision": statuses.get(category, "pass"), "scope": SCOPE,
                                 "observed_usage": USAGE, "native": {"true": True, "integer": 1}})
        owner = "owner:" + category
        sources.append({"category": category, "owner_ref": owner, "raw_sha256": hashlib.sha256(raw).hexdigest(),
                        "source_head_sha256": HEAD})
        judge = dict(JUDGE, qualified=True, expires_at=END) if category == "semantic" and profile == "bounded_semantic" else None
        evidence[category] = release.OwnerEvidence(raw=raw, source_head_sha256=HEAD,
            candidate_sha256=CANDIDATE, host_sha256=HOST, scope=copy.deepcopy(SCOPE), observed_at=START,
            expires_at=END, evidence_class="agent_adjudicated" if judge else "simulated" if category == "semantic"
            else "externally_observed" if category == "business" else "deterministic_verified",
            projection={"status": statuses.get(category, "pass"), "metrics": copy.deepcopy(USAGE) if category == "cost" else None,
                        "judge": judge, "business": None},
            citations=(release.OriginalCitation("/decision", statuses.get(category, "pass")),))
        readers.append(release.OwnerReader(category, owner, lambda category=category: evidence[category]))
    plan = release.freeze_plan(candidate_sha256=CANDIDATE, host_sha256=HOST, profile=profile,
        required_sources=sources, budgets=copy.deepcopy(CAPS if budgets is None else budgets), scope=copy.deepcopy(SCOPE),
        frozen_at=START, expires_at=END, judge=JUDGE if profile == "bounded_semantic" else None)
    return plan, readers, evidence


def decision(plan, readers):
    return release.decide(plan, release.digest(plan), readers, NOW)


@pytest.mark.parametrize(("statuses", "outcome"), [({}, "qualified"),
    ({"conformance": "critical_failure", "integration": "actionable_failure", "uncertainty": "unknown"}, "reject"),
    ({"integration": "actionable_failure", "uncertainty": "unknown"}, "revise"),
    ({"uncertainty": "unknown"}, "inconclusive")])
def test_fixed_precedence_and_native_findings(statuses, outcome):
    plan, readers, _ = fixture(statuses=statuses)
    report = decision(plan, readers)
    assert report["outcome"] == outcome
    assert report["claim_scope"] == "fixture_mechanics"
    assert all(report[key] is False for key in ("authorized", "evidence_accepted", "release_published"))
    for reason in report["reasons"]:
        assert set(reason) == {"code", "category", "citations"}
        for citation in reason["citations"]:
            assert citation["raw_sha256"] in {source["raw_sha256"] for source in plan["required_sources"]}


def blocked():
    raise release.ExternalBlocked("not report prose")


def unknown():
    raise release.EvidenceUnavailable("not report prose")


@pytest.mark.parametrize(("statuses", "callback", "outcome"), [({}, blocked, "blocked_external"),
    ({"uncertainty": "unknown"}, blocked, "inconclusive"),
    ({"integration": "actionable_failure"}, blocked, "revise"),
    ({"integration": "critical_failure"}, blocked, "reject"), ({}, unknown, "inconclusive")])
def test_external_signal_requires_trusted_reader_and_respects_precedence(statuses, callback, outcome):
    plan, readers, _ = fixture(statuses=statuses)
    readers[0] = replace(readers[0], read=callback)
    assert decision(plan, readers)["outcome"] == outcome


@pytest.mark.parametrize("category", release.CATEGORIES)
def test_each_missing_required_category_is_unknown(category):
    plan, readers, _ = fixture()
    assert decision(plan, [reader for reader in readers if reader.category != category])["outcome"] == "inconclusive"


def test_optional_business_missing_or_invalid_does_not_gate_technical():
    plan, readers, evidence = fixture(business=True)
    assert decision(plan, readers[:-1])["outcome"] == "qualified"
    evidence["business"] = replace(evidence["business"], evidence_class="simulated",
        projection={"status": "pass", "metrics": None, "judge": None,
                    "business": {"kind": "roi_actual", "value": 1000, "unit": "currency"}})
    report = decision(plan, readers)
    assert report["outcome"] == "qualified"
    assert all(item["category"] != "business" for item in report["evidence"])


def test_business_observations_are_exact_scoped_original_values():
    plan, readers, evidence = fixture(business=True)
    row = {"source": {"source_id": "customer-owner", "origin": "retained outcome owner", "evidence_class": "externally_observed",
                     "subject_kind": "customer"}, "period": {"started_at": START, "ended_at": NOW.isoformat()},
           "metric": "customer_adoption", "unit": "count", "currency": None,
           "denominator": {"value": 10, "unit": "customers", "population_ref": "observed-customer-population"},
           "coverage": "complete", "gaps": [], "exclusions": [], "observed_records": 2, "unknown_records": 0,
           "known_subtotal": "2", "total": "2", "rate": {"numerator": "2", "denominator": 10,
           "population_ref": "observed-customer-population"}, "status": "observed", "batch_sha256s": ["sha256:" + HEAD]}
    original = {"actual": row}
    raw = release.canonical(original)
    source = plan["required_sources"][-1]
    source["raw_sha256"] = hashlib.sha256(raw).hexdigest()
    evidence["business"] = replace(evidence["business"], raw=raw,
        projection={"status": "pass", "metrics": None, "judge": None, "business": {"kind": "outcome_observations", "rows": [row],
                    "business_value": "business_value_unobserved", "roi": "roi_unobserved"}},
        citations=(release.OriginalCitation("/actual", original["actual"]),))
    report = decision(plan, readers)
    business = next(row for row in report["evidence"] if row["category"] == "business")
    assert business["projection"]["business"]["rows"] == [original["actual"]]
    assert business["citations"][0]["value"] == original["actual"]


@pytest.mark.parametrize("field", ["candidate_sha256", "host_sha256", "source_head_sha256", "scope", "raw",
                                  "expires_at", "observed_at", "citations", "projection"])
def test_original_owner_substitution_and_forged_fields_fail_closed(field):
    plan, readers, evidence = fixture()
    replacements = {"candidate_sha256": "0" * 64, "host_sha256": "0" * 64, "source_head_sha256": "0" * 64,
        "scope": {"native": True}, "raw": b'{"decision":"pass"}', "expires_at": START,
        "observed_at": (NOW + timedelta(seconds=1)).isoformat(),
        "citations": (release.OriginalCitation("/native/true", 1),),
        "projection": {"status": "pass", "metrics": None, "judge": None, "business": None, "verified": True}}
    evidence["conformance"] = replace(evidence["conformance"], **{field: replacements[field]})
    assert decision(plan, readers)["outcome"] == "inconclusive"


@pytest.mark.parametrize("value", [True, 1.0, None])
def test_strict_original_citation_types_do_not_collapse(value):
    plan, readers, evidence = fixture()
    evidence["conformance"] = replace(evidence["conformance"], citations=(release.OriginalCitation("/native/integer", value),))
    assert decision(plan, readers)["outcome"] == "inconclusive"


def test_closing_pass_checks_earlier_owner_after_later_owner_changes_it():
    plan, readers, evidence = fixture()
    calls = []
    original = next(reader for reader in readers if reader.category == "uncertainty")
    def later():
        calls.append("uncertainty")
        evidence["conformance"] = replace(evidence["conformance"], source_head_sha256="0" * 64)
        return original.read()
    readers[-1] = replace(original, read=later)
    assert decision(plan, readers)["outcome"] == "inconclusive"
    assert calls == ["uncertainty", "uncertainty"]


def test_closing_change_withdrawal_and_renewed_lifetime():
    plan, readers, evidence = fixture()
    count = 0
    def renewed():
        nonlocal count
        count += 1
        return replace(evidence["conformance"], observed_at=NOW.isoformat(),
                       expires_at=(NOW + timedelta(minutes=count)).isoformat())
    readers[0] = replace(readers[0], read=renewed)
    assert decision(plan, readers)["outcome"] == "qualified"
    assert count == 2
    count = 0
    def withdrawn():
        nonlocal count
        count += 1
        if count == 2:
            raise release.EvidenceUnavailable()
        return evidence["conformance"]
    readers[0] = replace(readers[0], read=withdrawn)
    assert decision(plan, readers)["outcome"] == "inconclusive"


def test_native_failure_survives_closing_unavailability():
    plan, readers, evidence = fixture(statuses={"conformance": "critical_failure"})
    count = 0
    def withdrawn():
        nonlocal count
        count += 1
        if count == 2:
            raise release.ExternalBlocked()
        return evidence["conformance"]
    readers[0] = replace(readers[0], read=withdrawn)
    assert decision(plan, readers)["outcome"] == "reject"


@pytest.mark.parametrize("change", ["qualified", "model", "settings_sha256", "rubric_sha256", "domain", "profile",
                                    "controller_sha256", "expires_at", "simulated", "missing"])
def test_semantic_judge_requires_exact_current_qualified_selector(change):
    plan, readers, evidence = fixture(profile="bounded_semantic")
    assert decision(plan, readers)["outcome"] == "qualified"
    row = evidence["semantic"]
    projection = copy.deepcopy(row.projection)
    if change == "simulated":
        row = replace(row, evidence_class="simulated")
    elif change == "missing":
        projection["judge"] = None
    else:
        projection["judge"][change] = False if change == "qualified" else START if change == "expires_at" else "0" * 64 if change.endswith("sha256") else "other"
    evidence["semantic"] = replace(row, projection=projection)
    assert decision(plan, readers)["outcome"] == "inconclusive"


def test_unknown_required_usage_and_optional_caps():
    plan, readers, evidence = fixture()
    projection = copy.deepcopy(evidence["cost"].projection)
    projection["metrics"]["cost"] = None
    evidence["cost"] = replace(evidence["cost"], projection=projection)
    assert decision(plan, readers)["outcome"] == "inconclusive"
    plan["budgets"]["cost"] = None
    assert decision(plan, readers)["outcome"] == "qualified"
    projection["metrics"]["elapsed_seconds"] = None
    evidence["cost"] = replace(evidence["cost"], projection=projection)
    assert decision(plan, readers)["outcome"] == "inconclusive"


def test_decimal_budget_sum_exact_boundary_and_overrun():
    plan, readers, evidence = fixture()
    plan["budgets"]["cost"] = 0.3
    source = copy.deepcopy(next(row for row in plan["required_sources"] if row["category"] == "cost"))
    source["owner_ref"] = "owner:other-cost"
    plan["required_sources"].append(source)
    projection = copy.deepcopy(evidence["cost"].projection)
    projection["metrics"]["cost"] = 0.2
    extra = replace(evidence["cost"], projection=projection)
    readers.append(release.OwnerReader("cost", source["owner_ref"], lambda: extra))
    assert decision(plan, readers)["outcome"] == "qualified"
    projection["metrics"]["cost"] = 0.200000000000001
    assert decision(plan, readers)["outcome"] == "revise"


def test_frozen_plan_external_pin_policy_and_closed_fields():
    plan, readers, _ = fixture()
    pinned = release.digest(plan)
    for path, value in [("budgets", dict(CAPS, cost=10)), ("profile", "bounded_semantic"),
                        ("scope", {"kind": "broader"}), ("candidate_sha256", "0" * 64)]:
        changed = copy.deepcopy(plan)
        changed[path] = value
        with pytest.raises(ValueError, match="release_plan_pin"):
            release.decide(changed, pinned, readers, NOW)
    changed = copy.deepcopy(plan)
    changed["policy"]["critical_failures_allowed"] = 1
    changed["policy_sha256"] = release.digest(changed["policy"])
    with pytest.raises(ValueError, match="release_policy_pin"):
        release.validate_plan(changed, release.digest(changed))
    changed = copy.deepcopy(plan)
    changed["reviewer_override"] = True
    with pytest.raises(ValueError, match="release_plan_shape"):
        release.validate_plan(changed, release.digest(changed))


def test_plan_and_report_detach_and_replay_recomputes_without_claim_authority():
    plan, readers, evidence = fixture()
    report = decision(plan, readers)
    assert release.replay_decision(report, release.digest(plan), readers, NOW) == report
    report["evidence"][0]["projection"]["status"] = "critical_failure"
    assert evidence["conformance"].projection["status"] == "pass"
    report["report_sha256"] = release.digest({key: value for key, value in report.items() if key != "report_sha256"})
    with pytest.raises(ValueError, match="release_replay_changed"):
        release.replay_decision(report, release.digest(plan), readers, NOW)
    report = decision(plan, readers)
    report["reasons"][0]["citations"].append({"owner_ref": "unselected", "raw_sha256": HEAD, "pointer": "/anything", "value": True})
    report["report_sha256"] = release.digest({key: value for key, value in report.items() if key != "report_sha256"})
    with pytest.raises(ValueError, match="release_replay_changed"):
        release.replay_decision(report, release.digest(plan), readers, NOW)


def test_replay_requires_live_owner_and_refuses_unavailable_or_expired_evidence():
    plan, readers, _ = fixture()
    report = decision(plan, readers)
    readers[0] = replace(readers[0], read=unknown)
    with pytest.raises(ValueError, match="release_replay_changed"):
        release.replay_decision(report, release.digest(plan), readers, NOW)
    plan, readers, _ = fixture()
    report = decision(plan, readers)
    with pytest.raises(ValueError, match="release_replay_changed"):
        release.replay_decision(report, release.digest(plan), readers, NOW + timedelta(minutes=6))


def test_final_clock_expiry_is_checked_after_closing_reads(monkeypatch):
    plan, readers, _ = fixture()
    ticks = iter([NOW] * 13 + [NOW + timedelta(minutes=6)] * 3)
    monkeypatch.setattr(release, "_clock", lambda now: next(ticks))
    assert release.decide(plan, release.digest(plan), readers)["outcome"] == "inconclusive"


@pytest.mark.parametrize("target", ["owner", "judge"])
def test_expiry_during_classification_cannot_qualify(monkeypatch, target):
    plan, readers, evidence = fixture(profile="bounded_semantic")
    expiry = (NOW + timedelta(seconds=1)).isoformat()
    if target == "owner":
        evidence["conformance"] = replace(evidence["conformance"], expires_at=expiry)
    else:
        projection = copy.deepcopy(evidence["semantic"].projection)
        projection["judge"]["expires_at"] = expiry
        evidence["semantic"] = replace(evidence["semantic"], projection=projection)
    ticks = iter([NOW] * 14 + [NOW + timedelta(seconds=2)])
    monkeypatch.setattr(release, "_clock", lambda now: next(ticks))
    report = release.decide(plan, release.digest(plan), readers)
    assert report["outcome"] == "inconclusive"
    assert any(reason["code"].endswith("at_close") for reason in report["reasons"])


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), {1: "bad"}, 1 << 129])
def test_bounded_json_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        release.bounded_json(value)


def test_bounds_cycles_json_duplicate_keys_and_clock_offsets():
    cycle = []
    cycle.append(cycle)
    with pytest.raises(ValueError):
        release.bounded_json(cycle)
    plan, readers, evidence = fixture()
    raw = b'{"decision":"pass","decision":"critical_failure"}'
    plan["required_sources"][0]["raw_sha256"] = hashlib.sha256(raw).hexdigest()
    evidence["conformance"] = replace(evidence["conformance"], raw=raw)
    assert decision(plan, readers)["outcome"] == "inconclusive"
    plan, readers, _ = fixture()
    plan["frozen_at"] = "2026-10-12T11:59:00"
    with pytest.raises(ValueError, match="release_utc_time"):
        release.validate_plan(plan, release.digest(plan))
    with pytest.raises(ValueError, match="release_utc_clock"):
        release.decide(fixture()[0], release.digest(fixture()[0]), readers, datetime(2026, 10, 12))


def test_no_json_reader_selection_unrequested_or_duplicate_reader():
    plan, readers, _ = fixture()
    with pytest.raises(ValueError, match="release_reader_type"):
        decision(plan, [{"read": "shell", "verified": True}])
    with pytest.raises(ValueError, match="release_duplicate_reader"):
        decision(plan, readers + readers[:1])
    with pytest.raises(ValueError, match="release_unrequested_reader"):
        decision(plan, readers + [replace(readers[0], owner_ref="owner:unselected")])


@pytest.mark.parametrize("status", ["critical_failure", "actionable_failure"])
def test_simulated_or_unqualified_semantics_cannot_drive_real_quality_outcomes(status):
    plan, readers, evidence = fixture(profile="bounded_semantic", statuses={"semantic": status})
    evidence["semantic"] = replace(evidence["semantic"], evidence_class="simulated")
    assert decision(plan, readers)["outcome"] == "inconclusive"
    plan, readers, evidence = fixture(profile="bounded_semantic", statuses={"semantic": status})
    projection = copy.deepcopy(evidence["semantic"].projection)
    projection["judge"]["qualified"] = False
    evidence["semantic"] = replace(evidence["semantic"], projection=projection)
    assert decision(plan, readers)["outcome"] == "inconclusive"


def test_native_decimal_strings_preserve_exact_budget_math():
    plan, readers, evidence = fixture()
    plan["budgets"]["elapsed_seconds"] = "0.100000000000000001"
    plan["budgets"]["cost"] = "0.100000000000000001"
    projection = copy.deepcopy(evidence["cost"].projection)
    projection["metrics"]["elapsed_seconds"] = "0.100000000000000001"
    projection["metrics"]["cost"] = "0.100000000000000001"
    evidence["cost"] = replace(evidence["cost"], projection=projection)
    assert decision(plan, readers)["outcome"] == "qualified"
    projection["metrics"]["cost"] = "0.100000000000000002"
    assert decision(plan, readers)["outcome"] == "revise"


@pytest.mark.parametrize("value", ["NaN", "Infinity", "1e2", "-1", "01", True])
def test_invalid_decimal_and_count_literals_fail_closed(value):
    plan, readers, evidence = fixture()
    projection = copy.deepcopy(evidence["cost"].projection)
    projection["metrics"]["cost"] = value
    evidence["cost"] = replace(evidence["cost"], projection=projection)
    assert decision(plan, readers)["outcome"] == "inconclusive"
