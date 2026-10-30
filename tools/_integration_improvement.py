"""Frozen improvement decisions over current, host-authenticated originals.

Callbacks are trusted host code. Serialized results cannot appoint their own
readers, establish provider identity, or grant implementation authority.
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from tools._release_decision import (
    EVIDENCE_CLASSES,
    OriginalCitation,
    _clock,
    _metric,
    _object,
    _original,
    _pointer,
    _require,
    _sha,
    _sum,
    _text,
    _time,
    bounded_json,
    canonical,
    digest,
)

INTEGRITY_ERRORS = frozenset({
    "cost_receipt_authentication_failed", "cost_receipt_binding_changed",
    "cost_trusted_receipt_hash_mismatch", "cost_start_binding_changed",
    "cost_output_changed", "cost_output_overflow", "trial_file_changed_or_overflow",
    "integration_plan_pin", "integration_original_pin", "integration_owner_binding", "integration_owner_changed",
    "integration_citation_value", "integration_projection_original",
    "integration_original_command_changed", "integration_original_stream_changed",
    "integration_original_command_output_changed", "integration_original_head_changed",
    "integration_current_binding_changed", "integration_original_history_changed",
    "integration_original_response_changed", "integration_original_projection_changed",
    "integration_original_parent_changed", "integration_original_changed_during_read",
    "integration_current_candidate_changed", "integration_closing_candidate_or_host_changed",
    "integration_execution_policy_binding", "integration_current_execution_authority_changed",
    "cost_receipt_lifecycle_version", "cost_producer_completion_invalid",
    "cost_authenticated_producer_receipt_required",
})

OUTCOMES = ("stop", "revise", "narrow", "continue")
ROUTES = ("direct", "current_caller", "scoped_mcp")
CYCLES = ("baseline", "change_1", "change_2")
KINDS = ("operational_repair", "integration_docs", "review_dependencies", "retrieval",
         "host_capability", "capability_expansion")
COUNTERS = ("repair_attempts", "review_attempts", "omission_count", "host_limit_count", "expansion_need_count")
BOOLS = ("integration_success", "accepted", "useful", "unsupported_scope", "source_drift_detected",
         "stale_approval_reused", "history_preserved", "critical_failure")
FIELDS = {"disposition", "usefulness_scope", "elapsed_seconds", "supported_proposals", *BOOLS, *COUNTERS}
_POLICY = {"version": "integration-improvement-policy/v1", "precedence": list(OUTCOMES),
           "routes": list(ROUTES), "cycles": list(CYCLES), "terminal_coverage": "1",
           "integration_success": "1", "accepted_usefulness": "1", "stale_approvals_allowed": 0,
           "history_mutations_allowed": 0, "optimization_distinct_host_scenarios": 2,
           "closing_read_required": True}


def policy():
    return bounded_json(_POLICY)


def freeze_plan(*, candidate_sha256, hosts, slots, scope, frozen_at, expires_at, proposals=None):
    """Freeze the nine obligations and selection thresholds before execution."""
    value = {"schema_version": "integration-improvement-plan/v1", "candidate_sha256": candidate_sha256,
             "hosts": hosts, "slots": slots, "scope": scope, "frozen_at": frozen_at, "expires_at": expires_at,
             "proposals": [] if proposals is None else proposals, "policy": policy(), "policy_sha256": digest(policy())}
    return validate_plan(value, digest(value))


def validate_execution_scope(plan):
    """Validate optional native clocks without promoting serialized authority."""
    scope = plan["scope"]
    if "execution_policy" in scope or "execution_policy_sha256" in scope:
        from evidence_wiki._native_owner_execution import fixture_window, validate_execution_policy
        _require("execution_policy" in scope and "execution_policy_sha256" in scope,
                 "integration_execution_policy_binding")
        selected = validate_execution_policy(scope["execution_policy"])
        _require(scope["execution_policy_sha256"] == digest(selected)
                 and scope.get("native_run_seconds") == selected["native_run_seconds"]
                 and scope.get("native_cleanup_recipe") == "owned-popen-groups/v2"
                 and all(type(slot["elapsed_cap_seconds"]) is int
                         and slot["elapsed_cap_seconds"] == selected["slot_seconds"] for slot in plan["slots"]),
                 "integration_execution_policy_binding")
        fixture_window(selected, plan["frozen_at"], plan["expires_at"], current_utc=plan["frozen_at"])
    else:
        _require(scope.get("native_cleanup_recipe") != "owned-popen-groups/v2",
                 "integration_execution_policy_binding")


def validate_plan(plan, expected_plan_sha256):
    plan = bounded_json(plan)
    _sha(expected_plan_sha256)
    _require(digest(plan) == expected_plan_sha256, "integration_plan_pin")
    _object(plan, {"schema_version", "candidate_sha256", "hosts", "slots", "scope", "frozen_at",
                   "expires_at", "proposals", "policy", "policy_sha256"}, "integration_plan_shape")
    _require(plan["schema_version"] == "integration-improvement-plan/v1", "integration_plan_version")
    _sha(plan["candidate_sha256"])
    _require(canonical(plan["policy"]) == canonical(policy()) and plan["policy_sha256"] == digest(policy()),
             "integration_policy_pin")
    _require(type(plan["scope"]) is dict and 1 <= len(plan["scope"]) <= 32, "integration_scope")
    _require(_time(plan["frozen_at"]) < _time(plan["expires_at"]), "integration_window")
    _require(type(plan["hosts"]) is list and len(plan["hosts"]) == 3, "integration_hosts")
    hosts, routes, scenarios = {}, set(), set()
    for host in plan["hosts"]:
        _object(host, {"host_id", "route", "host_sha256", "scenario_sha256"}, "integration_host_shape")
        _text(host["host_id"], "integration_host_id", 128)
        _require(host["host_id"] not in hosts and host["route"] in ROUTES, "integration_host_identity")
        for key in ("host_sha256", "scenario_sha256"):
            _sha(host[key])
        hosts[host["host_id"]] = host
        routes.add(host["route"])
        scenarios.add(host["scenario_sha256"])
    _require(routes == set(ROUTES) and len(scenarios) == 3, "integration_distinct_scenarios")
    _require(type(plan["slots"]) is list and len(plan["slots"]) == 9, "integration_slots")
    seen, pairs, inputs = set(), set(), {host_id: set() for host_id in hosts}
    for slot in plan["slots"]:
        _object(slot, {"slot_id", "host_id", "cycle", "input_sha256", "owner_ref", "elapsed_cap_seconds"},
                "integration_slot_shape")
        for key in ("slot_id", "owner_ref"):
            _text(slot[key], "integration_slot_identity", 256)
        _require(slot["slot_id"] not in seen and slot["host_id"] in hosts and slot["cycle"] in CYCLES,
                 "integration_slot_identity")
        _sha(slot["input_sha256"])
        _require(_metric(slot["elapsed_cap_seconds"], "elapsed_seconds") > 0, "integration_elapsed_cap")
        seen.add(slot["slot_id"])
        pairs.add((slot["host_id"], slot["cycle"]))
        inputs[slot["host_id"]].add(slot["input_sha256"])
    _require(len(pairs) == 9 and all(len(items) == 3 for items in inputs.values()), "integration_change_cycles")
    validate_execution_scope(plan)
    proposals = plan["proposals"]
    _require(type(proposals) is list and len(proposals) <= 32, "integration_proposals")
    ids = set()
    for proposal in proposals:
        _object(proposal, {"proposal_id", "kind", "estimated_effort", "effort_unit", "trigger_metric",
                           "trigger_threshold", "capability_authorized"}, "integration_proposal_shape")
        _text(proposal["proposal_id"], "integration_proposal_id", 128)
        _require(proposal["proposal_id"] not in ids and proposal["kind"] in KINDS, "integration_proposal_identity")
        ids.add(proposal["proposal_id"])
        _require(proposal["effort_unit"] == "agent_work_units" and
                 _metric(proposal["estimated_effort"], "cost") > 0, "integration_proposal_effort")
        _require(proposal["trigger_metric"] in COUNTERS and
                 _metric(proposal["trigger_threshold"], "tokens") > 0, "integration_proposal_trigger")
        _require(type(proposal["capability_authorized"]) is bool, "integration_capability_flag")
        expected = {"review_dependencies": "review_attempts", "retrieval": "omission_count",
                    "host_capability": "host_limit_count", "capability_expansion": "expansion_need_count"}
        if proposal["kind"] in expected:
            _require(proposal["trigger_metric"] == expected[proposal["kind"]], "integration_proposal_trigger")
    return plan


@dataclass(frozen=True)
class OwnerEvidence:
    raw: bytes
    source_head_sha256: str
    candidate_sha256: str
    host_sha256: str
    input_sha256: str
    observed_at: str
    expires_at: str
    evidence_class: str
    projection: dict
    citations: tuple[OriginalCitation, ...]


@dataclass(frozen=True)
class OwnerReader:
    slot_id: str
    owner_ref: str
    read: Callable[[], OwnerEvidence]


def _projection(value, plan):
    value = bounded_json(value)
    _object(value, FIELDS, "integration_projection_shape")
    _require(value["disposition"] in {"unstarted", "started_stop_unknown", "completed", "failed", "refused"},
             "integration_disposition")
    _require(value["usefulness_scope"] in {"fixture_mechanics", "qualified_semantic", "unknown"},
             "integration_usefulness_scope")
    for key in BOOLS:
        _require(value[key] is None or type(value[key]) is bool, "integration_boolean")
    for key in COUNTERS:
        if value[key] is not None:
            _metric(value[key], "tokens")
    if value["elapsed_seconds"] is not None:
        _metric(value["elapsed_seconds"], "elapsed_seconds")
    _require(type(value["supported_proposals"]) is list and len(value["supported_proposals"]) <= 32 and
             all(type(item) is str for item in value["supported_proposals"]), "integration_supported_proposals")
    ids = {item["proposal_id"] for item in plan["proposals"]}
    _require(len(set(value["supported_proposals"])) == len(value["supported_proposals"]) and
             set(value["supported_proposals"]) <= ids, "integration_supported_proposals")
    return value


def _admit(native, pin, slot, host, plan, now):
    _require(type(native) is OwnerEvidence, "integration_owner_type")
    original = _original(native.raw)
    raw_sha = hashlib.sha256(native.raw).hexdigest()
    _require(raw_sha == pin["raw_sha256"] and native.source_head_sha256 == pin["source_head_sha256"],
             "integration_original_pin")
    _require(native.candidate_sha256 == plan["candidate_sha256"] and native.host_sha256 == host["host_sha256"]
             and native.input_sha256 == slot["input_sha256"], "integration_owner_binding")
    _require(_time(plan["frozen_at"]) <= _time(native.observed_at) <= now < _time(native.expires_at),
             "integration_owner_current")
    _require(native.evidence_class in EVIDENCE_CLASSES, "integration_evidence_class")
    projection = _projection(native.projection, plan)
    _require(projection["usefulness_scope"] != "qualified_semantic", "integration_semantic_owner_not_configured")
    _require(type(native.citations) is tuple and 1 <= len(native.citations) <= 32, "integration_citations")
    citations, pointers = [], set()
    for citation in native.citations:
        _require(type(citation) is OriginalCitation and citation.pointer not in pointers, "integration_citations")
        value = bounded_json(citation.value, 65536)
        _require(canonical(_pointer(original, citation.pointer)) == canonical(value), "integration_citation_value")
        citations.append({"slot_id": slot["slot_id"], "raw_sha256": raw_sha,
                          "pointer": citation.pointer, "value": value})
        pointers.add(citation.pointer)
    _require(any(canonical(item["value"]) == canonical(projection) for item in citations),
             "integration_projection_original")
    return {"slot_id": slot["slot_id"], "owner_ref": slot["owner_ref"], "evidence_class": native.evidence_class,
            "raw_sha256": raw_sha, "source_head_sha256": native.source_head_sha256,
            "projection": projection, "citations": citations}


def _pins(pins, expected, plan):
    pins = bounded_json(pins)
    _sha(expected)
    _require(digest(pins) == expected and type(pins) is dict, "integration_evidence_pin")
    _require(set(pins) <= {slot["slot_id"] for slot in plan["slots"]}, "integration_unplanned_pin")
    for pin in pins.values():
        _object(pin, {"raw_sha256", "source_head_sha256"}, "integration_evidence_pin_shape")
        _sha(pin["raw_sha256"])
        _sha(pin["source_head_sha256"])
    return pins


def decide(plan, expected_plan_sha256, evidence_pins, expected_evidence_sha256, readers, now=None, *, closing_check=None,
           prepare_pass=None):
    """Preserve every frozen obligation while replaying exact current originals."""
    plan = validate_plan(plan, expected_plan_sha256)
    _require(closing_check is None or callable(closing_check), "integration_closing_check")
    _require(prepare_pass is None or callable(prepare_pass), "integration_prepare_pass")
    pins = _pins(evidence_pins, expected_evidence_sha256, plan)
    _require(type(readers) in {list, tuple} and len(readers) <= 9, "integration_readers")
    registry, slots = {}, {slot["slot_id"]: slot for slot in plan["slots"]}
    for reader in readers:
        _require(type(reader) is OwnerReader and callable(reader.read) and reader.slot_id in slots
                 and reader.owner_ref == slots[reader.slot_id]["owner_ref"] and reader.slot_id not in registry,
                 "integration_reader_identity")
        registry[reader.slot_id] = reader
    hosts = {host["host_id"]: host for host in plan["hosts"]}
    first, admitted, problems, expiries, observed_failures = {}, {}, {}, {}, []
    opening = _clock(now)
    if not (_time(plan["frozen_at"]) <= opening < _time(plan["expires_at"])):
        problems["plan"] = "plan_not_current"
    for closing_pass in (False, True):
        unavailable = frozenset()
        if prepare_pass is not None:
            prepared = prepare_pass()
            _require(prepared is None or (type(prepared) is frozenset and prepared <= slots.keys()),
                     "integration_prepare_pass")
            unavailable = frozenset() if prepared is None else prepared
        for slot_id in sorted(slots):
            slot = slots[slot_id]
            if slot_id in unavailable:
                problems[slot_id] = "owner_unavailable"
            if slot_id not in pins or slot_id not in registry:
                problems[slot_id] = "owner_missing"
                continue
            try:
                native = registry[slot_id].read()
                item = _admit(native, pins[slot_id], slot, hosts[slot["host_id"]], plan, _clock(now))
                p = item["projection"]
                if p["critical_failure"] is True or p["stale_approval_reused"] is True or p["history_preserved"] is False:
                    observed_failures.append((slot_id, "unsafe_invariant", item["citations"]))
                if p["elapsed_seconds"] is not None and _metric(p["elapsed_seconds"], "elapsed_seconds") > _metric(slot["elapsed_cap_seconds"], "elapsed_seconds"):
                    observed_failures.append((slot_id, "elapsed_budget_exceeded", item["citations"]))
                if closing_pass:
                    _require(slot_id in first and canonical(first[slot_id]) == canonical(item), "integration_owner_changed")
                    admitted[slot_id] = item
                    expiries[slot_id] = _time(native.expires_at)
                else:
                    first[slot_id] = item
            except Exception as error:
                problems[slot_id] = "owner_invalid_or_changed" if isinstance(error, ValueError) else "owner_unavailable"
                if isinstance(error, ValueError) and str(error) in INTEGRITY_ERRORS:
                    observed_failures.append((slot_id, "evidence_integrity_failure", []))
    findings, rows = [], []
    for slot_id, code, citations in observed_failures:
        finding = {"outcome": "stop", "slot_id": slot_id, "code": code, "citations": citations}
        if finding not in findings:
            findings.append(finding)
    for slot_id in sorted(slots):
        item = admitted.get(slot_id)
        problem = problems.get(slot_id)
        p = None if item is None else item["projection"]
        row = {"slot": slots[slot_id], "disposition": "missing" if p is None else p["disposition"],
               "evidence": item, "problem": problem}
        rows.append(row)
        citations = [] if item is None else item["citations"]
        def finding(outcome, code, slot_id=slot_id, citations=citations):
            findings.append({"outcome": outcome, "slot_id": slot_id, "code": code, "citations": citations})
        if problem or item is None:
            finding("revise", problem or "owner_missing")
            continue
        if p["disposition"] in {"unstarted", "started_stop_unknown"}:
            finding("revise", "terminal_unknown")
        elif p["disposition"] == "failed" or p["integration_success"] is False:
            finding("revise", "functional_failure")
        if ((p["accepted"] is True and (p["disposition"] != "completed" or p["integration_success"] is not True))
                or (p["useful"] is True and p["accepted"] is not True)
                or (p["disposition"] == "failed" and p["integration_success"] is True)):
            finding("revise", "contradictory_terminal_projection")
        if slots[slot_id]["cycle"] != "baseline" and p["source_drift_detected"] is False:
            finding("revise", "planned_change_not_detected")
        # Correct unsupported-scope refusals are terminal, not accepted useful results.
        if p["unsupported_scope"] is True:
            finding("narrow", "unsupported_scope")
        required = ["repair_attempts", "review_attempts", "elapsed_seconds", "integration_success", "accepted", "unsupported_scope",
                    "source_drift_detected", "stale_approval_reused", "history_preserved", "critical_failure"]
        if any(p[key] is None for key in required):
            finding("revise", "required_measurement_unknown")
        if p["accepted"] is True:
            if p["useful"] is None or p["usefulness_scope"] == "unknown":
                finding("revise", "accepted_usefulness_unknown")
            elif p["useful"] is False:
                finding("narrow", "accepted_scope_not_useful")
        elif p["unsupported_scope"] is not True:
            finding("narrow", "no_supported_acceptance")
    closing = _clock(now)
    if "plan" in problems or not (_time(plan["frozen_at"]) <= closing < _time(plan["expires_at"])):
        findings.append({"outcome": "revise", "slot_id": "all", "code": "plan_not_current_at_close", "citations": []})
    for row in rows:
        sid = row["slot"]["slot_id"]
        if sid in expiries and closing >= expiries[sid]:
            row["problem"] = "owner_expired_at_close"
            findings.append({"outcome": "revise", "slot_id": sid, "code": "owner_expired_at_close",
                             "citations": row["evidence"]["citations"]})
    ranked, excluded = _rank(plan, rows)
    metrics = _summary(rows)
    # Ranking and measurement are part of the decision, so validity must cover
    # them as well as the two original-owner read passes.
    if closing_check is not None:
        try:
            closing_check()
        except Exception as error:
            integrity = isinstance(error, ValueError) and str(error) in INTEGRITY_ERRORS
            findings.append({"outcome": "stop" if integrity else "revise", "slot_id": "all",
                             "code": "evidence_integrity_failure" if integrity else "closing_owner_unavailable",
                             "citations": []})
    # Each recomputation can cross another witness deadline. Invalidation is
    # monotone, so at most one iteration per admitted slot needs recomputation.
    for _ in range(len(rows) + 1):
        final_closing = _clock(now)
        if not (_time(plan["frozen_at"]) <= final_closing < _time(plan["expires_at"])):
            reason = {"outcome": "revise", "slot_id": "all", "code": "plan_not_current_at_close", "citations": []}
            if reason not in findings:
                findings.append(reason)
        refreshed = False
        for row in rows:
            sid = row["slot"]["slot_id"]
            if sid in expiries and final_closing >= expiries[sid] and row["problem"] != "owner_expired_at_close":
                row["problem"] = "owner_expired_at_close"
                findings.append({"outcome": "revise", "slot_id": sid, "code": "owner_expired_at_close",
                                 "citations": row["evidence"]["citations"]})
                refreshed = True
        if not refreshed:
            break
        ranked, excluded = _rank(plan, rows)
        metrics = _summary(rows)
    outcome = next((value for value in OUTCOMES if any(item["outcome"] == value for item in findings)), "continue")
    if ranked and outcome == "continue":
        outcome = "revise"
        findings.append({"outcome": "revise", "slot_id": "all", "code": "measured_improvement_supported",
                         "citations": ranked[0]["citations"]})
    report = {"schema_version": "integration-improvement-decision/v1", "plan": plan,
              "plan_sha256": expected_plan_sha256, "evidence_pins": pins,
              "evidence_sha256": expected_evidence_sha256, "outcome": outcome,
              "reasons": sorted(findings, key=lambda item: (OUTCOMES.index(item["outcome"]), item["slot_id"], item["code"])),
              "slots": rows, "ranked_improvements": ranked, "excluded_improvements": excluded,
              "metrics": metrics,
              "next_improvement": ranked[0] if ranked and outcome != "stop" else None,
              "limitations": {"integration_claim": "simulated_agent_integration", "usefulness_claim": "fixture_mechanics",
                              "semantic_usefulness": "semantic_usefulness_unobserved", "provider_total": None,
                              "whole_host_cost": None, "business_value": "business_value_unobserved",
                              "roi": "roi_unobserved", "human_hours_saved": None},
              "implementation_authorized": False, "release_published": False}
    report["report_sha256"] = digest(report)
    return bounded_json(report)


def _summary(rows):
    terminal = successful = accepted = useful = 0
    unknown_slots = []
    burden = {key: {"known_subtotal": Decimal(0), "unknown_slots": []}
              for key in ("elapsed_seconds", "repair_attempts", "review_attempts")}
    for row in rows:
        sid = row["slot"]["slot_id"]
        p = None if row["problem"] or row["evidence"] is None else row["evidence"]["projection"]
        if p is None:
            unknown_slots.append(sid)
        else:
            is_terminal = p["disposition"] in {"completed", "failed", "refused"}
            terminal += is_terminal
            successful += is_terminal and p["integration_success"] is True
            accepted += (p["disposition"] == "completed" and p["integration_success"] is True
                         and p["accepted"] is True)
            useful += (p["disposition"] == "completed" and p["integration_success"] is True and p["accepted"] is True
                       and p["useful"] is True and p["usefulness_scope"] == "fixture_mechanics")
            if not is_terminal:
                unknown_slots.append(sid)
        for key, metric in burden.items():
            value = None if p is None else p[key]
            if value is None or p["disposition"] in {"unstarted", "started_stop_unknown"}:
                metric["unknown_slots"].append(sid)
            if value is not None:
                metric["known_subtotal"] = _sum((metric["known_subtotal"], Decimal(str(value))))
    for metric in burden.values():
        metric["known_subtotal"] = str(metric["known_subtotal"])
        metric["total"] = metric["known_subtotal"] if not metric["unknown_slots"] else None
    return {"planned_slots": len(rows), "terminal_slots": terminal, "successful_slots": successful,
            "accepted_slots": accepted, "useful_accepted_slots": useful, "unknown_slots": unknown_slots,
            "native_scope_burden": burden}


def _rank(plan, rows):
    ranked, excluded = [], []
    hosts = {host["host_id"]: host for host in plan["hosts"]}
    for proposal in plan["proposals"]:
        supports, effect, citations = set(), Decimal(0), []
        for row in rows:
            if row["problem"] or row["evidence"] is None:
                continue
            if proposal["kind"] == "review_dependencies" and row["slot"]["cycle"] == "baseline":
                continue
            p = row["evidence"]["projection"]
            value = p[proposal["trigger_metric"]]
            if proposal["proposal_id"] not in p["supported_proposals"] or value is None or value < proposal["trigger_threshold"]:
                continue
            host = hosts[row["slot"]["host_id"]]
            supports.add((host["host_id"], host["scenario_sha256"]))
            effect = _sum((effect, Decimal(value)))
            citations.extend(row["evidence"]["citations"])
        repeated = proposal["kind"] == "operational_repair" or len(supports) >= 2
        granted = proposal["kind"] not in {"retrieval", "host_capability", "capability_expansion"} or proposal["capability_authorized"]
        if not supports or not repeated or not granted:
            excluded.append({"proposal_id": proposal["proposal_id"], "reason": "unsupported_or_ungranted_trigger"})
            continue
        experiment = proposal["kind"] in {"retrieval", "host_capability", "capability_expansion"}
        ranked.append({"proposal": proposal, "distinct_host_scenarios": len(supports), "observed_effect": str(effect),
                       "citations": citations,
                       "recommendation_scope": "next_qualification_experiment" if experiment else "bounded_measured_improvement",
                       "substantive_expansion_qualified": False, "implementation_authorized": False})
    ranked.sort(key=lambda item: (_metric(item["proposal"]["estimated_effort"], "cost"),
                                  Decimal(item["observed_effect"]).copy_negate(), item["proposal"]["proposal_id"]))
    return ranked, excluded


def replay_decision(report, expected_plan_sha256, expected_evidence_sha256, readers, now=None, *, closing_check=None,
                    prepare_pass=None):
    """Check the external pins and recompute over current original owner reads."""
    report = bounded_json(report)
    _require(report.get("report_sha256") == digest({key: value for key, value in report.items() if key != "report_sha256"})
             and report.get("plan_sha256") == expected_plan_sha256 and report.get("evidence_sha256") == expected_evidence_sha256,
             "integration_report_pin")
    replayed = decide(report["plan"], expected_plan_sha256, report["evidence_pins"], expected_evidence_sha256, readers, now,
                      closing_check=closing_check, prepare_pass=prepare_pass)
    _require(canonical(replayed) == canonical(report), "integration_replay_changed")
    return replayed
