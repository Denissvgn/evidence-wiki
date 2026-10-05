"""Pure bounded continuation selection over authenticated canonical review history.

The caller authenticates observations and projects existing owner history. This
reducer stores nothing, executes nothing and never approves a claim or grants an
effect. Basis IDs must bind relevant claim/source material, not bookkeeping.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from .agent_execution import ExecutionError, _integer, _ref
from .mutation_contracts import MutationError, canonical, detached
from .run_policy import RunPolicy

ACTION = "research-disagreement-action/v1"
CRITERIA = frozenset({"support", "source_suitability", "scope", "time", "units", "counterevidence"})
REVIEW_FIELDS = frozenset({"review_ref", "profile_ref", "role", "controller_ref", "basis_id", "verdicts", "objection_refs"})
HISTORY_FIELDS = frozenset({"action_ref", "claim_id", "basis_id", "action", "defect_sha256", "result_basis_id"})
GATE_FIELDS = frozenset({"authentication", "accounting", "mechanical", "context_complete", "source_present",
    "qualified", "authority_granted", "budget_available", "model_available", "repair_available",
    "adjudicator_available", "acquisition_available", "source_gap_ref", "run_repair_cycles",
    "run_adjudication_cycles", "run_acquisition_attempts"})
_HASH = re.compile(r"sha256:[a-f0-9]{64}\Z")


def _hash(value: Any) -> str:
    if type(value) is not str or _HASH.fullmatch(value) is None:
        raise ExecutionError("disagreement_digest_invalid")
    return value


def _refs(value: Any) -> list[str]:
    if type(value) is not list or len(value) > 32:
        raise ExecutionError("disagreement_references_invalid")
    values = [_ref(item) for item in value]
    if len(set(values)) != len(values):
        raise ExecutionError("disagreement_references_invalid")
    return sorted(values)


def _reviews(values, required, generator_controller_ref, service_profile=None, frozen=None):
    if type(values) is not list or len(values) > 32:
        raise ExecutionError("disagreement_reviews_invalid")
    result, refs = [], {}
    for supplied in values:
        extra = {"service_slot_ref", "qualification_ref"} if service_profile is not None else set()
        if type(supplied) is not dict or set(supplied) != REVIEW_FIELDS | extra:
            raise ExecutionError("disagreement_reviews_invalid")
        review = detached(supplied)
        for name in ("review_ref", "profile_ref", "controller_ref"):
            _ref(review[name])
        _hash(review["basis_id"])
        configured = required.get(review["profile_ref"])
        if service_profile is not None:
            from .review_service_policy import approved_profiles
            slot = _ref(review["service_slot_ref"])
            _ref(review["qualification_ref"])
            configured = next((row for row in approved_profiles(frozen, service_profile)
                if row["profile_ref"] == review["profile_ref"]), None)
            route = service_profile.review_services.route(slot)
            if slot not in required or review["profile_ref"] not in route.config_refs:
                raise ExecutionError("disagreement_review_slot_invalid")
        if (configured is None or review["role"] != configured["role"]
                or review["controller_ref"] != configured["controller_ref"]
                or review["controller_ref"] == generator_controller_ref):
            raise ExecutionError("disagreement_review_identity_invalid")
        verdicts = review["verdicts"]
        if (type(verdicts) is not dict or set(verdicts) != CRITERIA
                or any(type(value) is not str or value not in {"pass", "fail", "unknown"} for value in verdicts.values())):
            raise ExecutionError("disagreement_verdicts_invalid")
        review["objection_refs"] = _refs(review["objection_refs"])
        if review["review_ref"] in refs:
            if refs[review["review_ref"]] != review:
                raise ExecutionError("disagreement_review_identity_invalid")
            continue
        refs[review["review_ref"]] = review
        result.append(review)
    return result


def _history(values, claim_id):
    if type(values) is not list or len(values) > 128:
        raise ExecutionError("disagreement_history_invalid")
    result, identifiers = [], set()
    for supplied in values:
        if type(supplied) is not dict or set(supplied) != HISTORY_FIELDS:
            raise ExecutionError("disagreement_history_invalid")
        item = detached(supplied)
        identifier = _ref(item["action_ref"])
        if identifier in identifiers or item["claim_id"] != claim_id or item["action"] not in {"repair", "adjudicate", "acquire"}:
            raise ExecutionError("disagreement_history_invalid")
        identifiers.add(identifier)
        _hash(item["basis_id"])
        _hash(item["defect_sha256"])
        if item["result_basis_id"] is not None:
            _hash(item["result_basis_id"])
        result.append(item)
    return result


def _gates(values):
    if type(values) is not dict or set(values) != GATE_FIELDS:
        raise ExecutionError("disagreement_gates_invalid")
    gates = detached(values)
    enums = {"authentication": {"pass", "fail", "unknown"}, "accounting": {"known", "unknown", "overrun"},
             "mechanical": {"pass", "fail", "unknown"}}
    for name, permitted in enums.items():
        if type(gates[name]) is not str or gates[name] not in permitted:
            raise ExecutionError("disagreement_gates_invalid")
    counters = {"run_repair_cycles", "run_adjudication_cycles", "run_acquisition_attempts"}
    for name in GATE_FIELDS - set(enums) - counters - {"source_gap_ref"}:
        if type(gates[name]) is not bool:
            raise ExecutionError("disagreement_gates_invalid")
    for name in counters:
        if gates[name] is not None:
            _integer(gates[name], 1024)
    if gates["source_gap_ref"] is not None:
        _ref(gates["source_gap_ref"])
    return gates


def disagreement_action(*, policy: RunPolicy, claim_id: str, basis_id: str, generator_controller_ref: str,
                        reviews: list[dict[str, Any]], history: list[dict[str, Any]], gates: dict[str, Any], service_profile=None) -> dict[str, Any]:
    """Choose a bounded proposal; adjudication never resolves native eligibility.

    History includes reserved attempts, including failed attempts, from the
    existing canonical run. New reviewer results must be fresh first passes for
    changed material; an adjudicator's conclusion is never a replacement vote.
    """
    if type(policy) is not RunPolicy:
        raise ExecutionError("disagreement_policy_invalid")
    _ref(claim_id)
    _hash(basis_id)
    _ref(generator_controller_ref)
    frozen = policy.to_dict()
    limits = frozen["continuation_limits"]
    repair_limit, adjudication_limit = min(2, limits["repair_cycles"]), min(1, limits["adjudication_cycles"])
    current, retained, canonical_gates = [], [], None
    defect_sha256 = "sha256:" + hashlib.sha256(canonical({"claim_id": claim_id, "findings": []})).hexdigest()

    def action(operation, outcome, reason, detail, *, effect=None):
        return {"schema_version": ACTION, "claim_id": claim_id, "basis_id": basis_id,
            "operation": operation, "outcome": outcome, "reason": reason, "detail": detail,
            "terminal": operation == "terminal", "review_refs": sorted(row["review_ref"] for row in current),
            "defect_sha256": defect_sha256, "source_gap_ref": canonical_gates["source_gap_ref"] if effect == "acquire" else None,
            "required_effects": [] if effect is None else [effect] if effect == "acquire" else ["model", effect],
            "requires_new_basis": operation in {"repair", "acquire"},
            "requires_fresh_first_passes": operation in {"repair", "acquire", "collect_first_passes", "adjudicate"},
            "repair_limit": repair_limit, "adjudication_limit": adjudication_limit,
            "repairs_used": sum(item["action"] == "repair" for item in retained),
            "adjudications_used": sum(item["action"] == "adjudicate" for item in retained),
            "acquisitions_used": sum(item["action"] == "acquire" for item in retained),
            "authorized": False, "evidence_accepted": False}

    def terminal(outcome, reason, detail):
        return action("terminal", outcome, reason, detail)

    if frozen["human_review"] or any(item["human_required"] for item in frozen["required_evaluators"]):
        return terminal("policy_incompatible", "human_review_required", "machine_review_incompatible")
    required = {row["profile_ref"]: row for row in frozen["required_evaluators"] if row["role"] in {"evaluator", "critic"}}
    try:
        if service_profile is not None:
            from .agent_execution import ExecutionProfile
            from .service_repair_binding import ServiceRepairBinding
            if type(service_profile) is not ExecutionProfile or type(service_profile.service_repair) is not ServiceRepairBinding:
                raise ExecutionError("disagreement_service_profile_invalid")
            service_profile.service_repair.check(service_profile)
            service_profile.review_services.check(service_profile, frozen)
        canonical_gates = _gates(gates)
        current = [row for row in _reviews(reviews, required, generator_controller_ref, service_profile, frozen) if row["basis_id"] == basis_id]
        retained = _history(history, claim_id)
    except (ExecutionError, MutationError, ValueError, KeyError, TypeError, AttributeError, RecursionError):
        return terminal("rejected", "untrusted_observation", "invalid_scoped_input")
    if canonical_gates["authentication"] != "pass":
        return terminal("rejected" if canonical_gates["authentication"] == "fail" else "blocked_external",
                        "untrusted_observation", "review_authentication_unavailable")
    if canonical_gates["accounting"] == "unknown" or any(canonical_gates[name] is None for name in
            ("run_repair_cycles", "run_adjudication_cycles", "run_acquisition_attempts")):
        return terminal("stop_budget", "usage_unknown", "unsettled_reservation")
    if canonical_gates["accounting"] == "overrun" or not canonical_gates["budget_available"]:
        return terminal("stop_budget", "budget_exhausted", "run_allowance_exhausted")
    if not canonical_gates["authority_granted"]:
        return terminal("blocked_external", "authority_unavailable", "required_owner_grant_missing")
    if {row["role"] for row in required.values()} != {"evaluator", "critic"}:
        return terminal("blocked_external", "qualification_unknown", "first_pass_profiles_unavailable")
    configured_controllers = {row["controller_ref"] for row in required.values()}
    if len(configured_controllers) != len(required) or generator_controller_ref in configured_controllers:
        return terminal("rejected", "untrusted_observation", "first_pass_controller_configuration_invalid")
    if any(canonical_gates[name] > limits[counter] for name, counter in
           (("run_repair_cycles", "repair_cycles"), ("run_adjudication_cycles", "adjudication_cycles"),
            ("run_acquisition_attempts", "acquisition_attempts"))):
        return terminal("stop_budget", "budget_exhausted", "continuation_allowance_overrun")
    counts = {kind: sum(item["action"] == kind for item in retained) for kind in ("repair", "adjudicate", "acquire")}
    if counts["repair"] > repair_limit or counts["adjudicate"] > adjudication_limit:
        return terminal("stop_budget", "budget_exhausted", "claim_continuation_allowance_overrun")
    if (counts["repair"] > canonical_gates["run_repair_cycles"]
            or counts["adjudicate"] > canonical_gates["run_adjudication_cycles"]
            or counts["acquire"] > canonical_gates["run_acquisition_attempts"]):
        return terminal("rejected", "untrusted_observation", "continuation_counter_regressed")
    material_actions = [item for item in retained if item["action"] in {"repair", "acquire"}]
    if material_actions:
        last = material_actions[-1]
        if last["result_basis_id"] is None:
            return terminal("blocked_external", "executor_unavailable", "repair_completion_unobserved")
        if last["result_basis_id"] == last["basis_id"]:
            return terminal("abstained", "unsupported_claim", "repair_did_not_change_material")
        previous_bases = {item["basis_id"] for item in material_actions}
        if basis_id in previous_bases:
            return terminal("abstained", "unsupported_claim", "repair_basis_oscillation")
    fingerprints = [{"profile_ref": row["profile_ref"], "role": row["role"],
        "verdicts": {key: value for key, value in row["verdicts"].items() if value != "pass"},
        "objection_refs": row["objection_refs"]} for row in sorted(current, key=lambda item: item["profile_ref"])]
    defect_sha256 = "sha256:" + hashlib.sha256(canonical({"claim_id": claim_id, "findings": fingerprints,
        "mechanical": canonical_gates["mechanical"], "source_gap_ref": canonical_gates["source_gap_ref"]})).hexdigest()

    def permitted(effect):
        return effect in frozen["allowed_effects"] and frozen["host_capability_refs"].get(effect) is not None

    def repair(reason, detail):
        if (permitted("model") and permitted("revise") and canonical_gates["model_available"]
                and canonical_gates["repair_available"] and counts["repair"] < repair_limit
                and canonical_gates["run_repair_cycles"] < limits["repair_cycles"]):
            return action("repair", "revise", reason, detail, effect="revise")
        return None

    def acquire():
        if (canonical_gates["source_gap_ref"] is not None and canonical_gates["acquisition_available"]
                and permitted("acquire") and canonical_gates["run_acquisition_attempts"] < limits["acquisition_attempts"]
                and counts["acquire"] < limits["acquisition_attempts"]):
            return action("acquire", "acquire_more", "insufficient_evidence", "authorized_source_gap", effect="acquire")
        return None

    if canonical_gates["mechanical"] == "unknown":
        return terminal("abstained", "mechanical_failure", "required_deterministic_check_unknown")
    if canonical_gates["mechanical"] == "fail":
        return repair("mechanical_failure", "repair_native_failure") or terminal(
            "rejected", "mechanical_failure", "native_failure_not_repairable")
    if not canonical_gates["context_complete"] or not canonical_gates["source_present"]:
        return acquire() or terminal("abstained", "insufficient_evidence", "source_context_unavailable")
    if not canonical_gates["qualified"]:
        return terminal("blocked_external", "qualification_unknown", "current_reviewer_qualification_missing")
    by_profile = {}
    for row in current:
        slot = row.get("service_slot_ref", row["profile_ref"])
        prior = by_profile.get(slot)
        if prior is not None and (prior["verdicts"] != row["verdicts"] or prior["objection_refs"] != row["objection_refs"]):
            return terminal("abstained", "review_disagreement", "conflicting_first_passes_from_one_profile")
        by_profile[slot] = row
    if set(by_profile) != set(required):
        if not permitted("model") or not canonical_gates["model_available"]:
            return terminal("blocked_external", "executor_unavailable", "first_pass_execution_unavailable")
        return action("collect_first_passes", None, "current_outcomes", "fresh_required_first_passes_missing")
    if len({row["controller_ref"] for row in by_profile.values()}) != len(by_profile):
        return terminal("rejected", "untrusted_observation", "first_pass_controllers_not_distinct")
    if any(value == "unknown" for row in current for value in row["verdicts"].values()):
        return acquire() or terminal("abstained", "unsupported_claim", "required_review_unknown")
    if any(item["action"] == "adjudicate" and item["basis_id"] == basis_id for item in retained):
        return repair("review_disagreement", "adjudication_requires_material_repair") or terminal(
            "abstained", "review_disagreement", "adjudication_cannot_approve_original_basis")
    failed = [row for row in current if any(value == "fail" for value in row["verdicts"].values()) or row["objection_refs"]]
    if not failed:
        return action("return_gate", None, "current_outcomes", "first_passes_complete")
    disagree = any(len({row["verdicts"][key] for row in by_profile.values()}) > 1 for key in CRITERIA)
    if (disagree and permitted("model") and permitted("adjudicate") and canonical_gates["model_available"]
            and canonical_gates["adjudicator_available"] and counts["adjudicate"] < adjudication_limit
            and canonical_gates["run_adjudication_cycles"] < limits["adjudication_cycles"]):
        return action("adjudicate", "revise", "review_disagreement", "bounded_evidence_investigation", effect="adjudicate")
    return repair("review_disagreement" if disagree else "unsupported_claim", "scoped_review_defect_repair") or terminal(
        "abstained", "review_disagreement" if disagree else "unsupported_claim", "continuation_options_exhausted")
