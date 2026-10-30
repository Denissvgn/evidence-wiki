"""Bounded, frozen inputs and projections for independently controlled comparisons."""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
from datetime import datetime

from tools._journey_cases import canonical, digest, require

ARMS = ("low_level", "document_only", "core_library")
STRATA = ("controlled_snapshot", "live_authorized_acquisition", "repeated_update")
_STRATUM_EXECUTION = {
    "controlled_snapshot": "controlled_snapshot_only; process receipts separate",
    "live_authorized_acquisition": "unknown; acquisition native receipt required",
    "repeated_update": "revised_snapshot_only; repeated native operation unobserved",
}
STATUSES = ("reserved", "running", "succeeded", "failed", "abandoned", "abstained", "interrupted", "unknown")
TERMINAL = frozenset(STATUSES) - {"reserved", "running"}
MAX_PLAN_BYTES = 4_194_304
MAX_PACKET_BYTES = 1_048_576
MAX_ANSWER_BYTES = 262_144
MAX_REPORT_BYTES = 16_777_216
_CAPS = {"seconds", "model_calls", "tool_calls", "tokens", "cost", "max_attempts"}
_METRICS = ("elapsed_seconds", "model_calls", "tool_calls", "tokens", "cost")
_ANSWER_FIELDS = {"original_text", "behavior", "text", "scope", "limitations", "citations"}
_ATTEMPT_FIELDS = {"attempt_id", "slot_id", "attempt_index", "parent_attempt_id", "plan_sha256", "packet_sha256",
                   "reserved_at", "started_at", "finished_at", "status", "answer", "receipt"}
_RECEIPT_FIELDS = {"provenance", "kind", "input_sha256", "output_sha256", "elapsed_seconds", "exit_code",
                   "model_calls", "tool_calls", "tokens", "cost", "native_receipt_refs"}
_JUDGMENT_FIELDS = {"slot_id", "answer_sha256", "judge_ref", "rubric_sha256", "provenance", "conformance",
                    "unknown_reasons"}
_PRIVATE_FIELDS = {"rubric", "rubric_sha256", "judge_answers", "expected_label", "expected_labels", "correct_answer",
                   "correct_answers", "schedule", "assignment", "prior_outputs", "other_outputs", "judgments"}
_GRADING_FIELDS = {"provenance", "kind", "input_sha256", "output_sha256", "rubric_sha256", "profile_ref", "started_at",
                   "completed_at", "elapsed_seconds", "observed_usage"}


def _object(value, fields, reason):
    require(type(value) is dict and set(value) == set(fields), reason)
    return value


def _text(value, maximum, reason, *, empty=False):
    require(type(value) is str, reason)
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        raise ValueError(reason) from None
    require((empty or size > 0) and size <= maximum, reason)
    return value


def _identifier(value, reason):
    _text(value, 128, reason)
    require(re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}", value) is not None, reason)
    return value


def _sha(value, reason, *, unknown=False):
    if unknown and value is None:
        return value
    require(type(value) is str and re.fullmatch(r"[a-f0-9]{64}", value) is not None, reason)
    return value


def _number(value, reason, *, integer=False, unknown=False, maximum=1_000_000_000):
    if unknown and value is None:
        return value
    require(type(value) is int if integer else type(value) in {int, float}, reason)
    require(0 <= value <= maximum and math.isfinite(value), reason)
    return value


def _bounded_copy(value, maximum, reason):
    """Reject cycles, non-JSON objects and excessive trees before serializing."""
    pending = [(value, 0, frozenset())]
    nodes, string_bytes = 0, 0
    while pending:
        item, depth, ancestors = pending.pop()
        nodes += 1
        require(nodes <= 100_000 and depth <= 16, reason)
        if type(item) in {dict, list}:
            require(id(item) not in ancestors, reason)
            parents = ancestors | {id(item)}
            if type(item) is dict:
                require(all(type(key) is str for key in item), reason)
                children = [*item.keys(), *item.values()]
            else:
                children = item
            pending.extend((child, depth + 1, parents) for child in children)
        elif type(item) is str:
            try:
                string_bytes += len(item.encode("utf-8"))
            except UnicodeError:
                raise ValueError(reason) from None
            require(string_bytes <= maximum, reason)
        elif type(item) is int:
            require(item.bit_length() <= 128, reason)
        elif type(item) is float:
            require(math.isfinite(item), reason)
        else:
            require(item is None or type(item) is bool, reason)
    try:
        raw = canonical(value)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ValueError(reason) from None
    require(len(raw) <= maximum, reason)
    return json.loads(raw)


def _timestamp(value, reason, *, unknown=False):
    if unknown and value is None:
        return None
    _text(value, 64, reason)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(reason) from None
    require(parsed.tzinfo is not None and parsed.utcoffset() is not None, reason)
    return parsed


def _public_configuration(value):
    pending = [value]
    while pending:
        item = pending.pop()
        if type(item) is dict:
            require(not (set(item) & _PRIVATE_FIELDS), "condition_privileged_field")
            pending.extend(item.values())
        elif type(item) is list:
            pending.extend(item)


def _strings(value, maximum, reason, *, minimum=0, width=4096, unique=False):
    require(type(value) is list and minimum <= len(value) <= maximum, reason)
    for item in value:
        _text(item, width, reason)
    if unique:
        require(len(value) == len(set(value)), reason)
    return value


def _sources(value):
    require(type(value) is list and 1 <= len(value) <= 16, "source_inventory_invalid")
    for row in value:
        _object(row, {"source_id", "content", "provenance", "rights"}, "source_shape_invalid")
        _identifier(row["source_id"], "source_id_invalid")
        _text(row["content"], 262_144, "source_content_bound", empty=True)
        provenance = _object(row["provenance"], {"origin", "captured_at", "authorization"}, "source_provenance_invalid")
        _text(provenance["origin"], 4096, "source_provenance_invalid")
        _timestamp(provenance["captured_at"], "source_provenance_invalid", unknown=True)
        if provenance["authorization"] is not None:
            _text(provenance["authorization"], 4096, "source_provenance_invalid")
        rights = _object(row["rights"], {"status", "basis"}, "source_rights_invalid")
        require(type(rights["status"]) is str and rights["status"] in {"allowed", "unknown", "denied"},
                "source_rights_invalid")
        if rights["basis"] is not None:
            _text(rights["basis"], 4096, "source_rights_invalid")
        require(rights["status"] != "allowed" or rights["basis"] is not None, "source_rights_basis_missing")
    require(len({row["source_id"] for row in value}) == len(value), "source_inventory_invalid")
    return value


def _cases(value):
    require(type(value) is list and 1 <= len(value) <= 8, "case_inventory_invalid")
    for case in value:
        require(type(case) is dict and set(case) in (
            {"id", "original_text", "sources", "stratum"},
            {"id", "original_text", "sources", "stratum", "cycles"}), "case_shape_invalid")
        _identifier(case["id"], "case_id_invalid")
        _text(case["original_text"], 16_384, "case_original_bound")
        require(case["stratum"] in STRATA, "case_stratum_invalid")
        _sources(case["sources"])
        cycles = case.setdefault("cycles", [])
        require(type(cycles) is list and len(cycles) <= 2, "case_cycle_bound")
        require(bool(cycles) == (case["stratum"] == "repeated_update"), "case_cycle_stratum_mismatch")
        for cycle in cycles:
            _object(cycle, {"sources"}, "case_cycle_shape_invalid")
            _sources(cycle["sources"])
        if case["stratum"] == "live_authorized_acquisition":
            for source in case["sources"]:
                require(source["rights"]["status"] == "allowed" and
                        source["provenance"]["authorization"] is not None and
                        source["provenance"]["captured_at"] is not None, "live_case_declaration_incomplete")
    require(len({case["id"] for case in value}) == len(value), "case_inventory_invalid")
    return value


def _budgets(value):
    _object(value, _CAPS, "budget_shape_invalid")
    _number(value["seconds"], "budget_seconds_invalid", maximum=86_400)
    require(value["seconds"] > 0, "budget_seconds_invalid")
    for key in ("model_calls", "tool_calls", "tokens"):
        _number(value[key], "budget_" + key + "_invalid", integer=True)
    _number(value["cost"], "budget_cost_invalid")
    require(type(value["max_attempts"]) is int and 1 <= value["max_attempts"] <= 3, "budget_attempts_invalid")
    return value


def _conditions(value):
    _object(value, {"candidate", "model", "capabilities", "rubric_sha256", "host_observation", "interface_guides", "policy"},
            "condition_shape_invalid")
    candidate = _object(value["candidate"], {"reference", "revision", "identity_sha256"}, "candidate_shape_invalid")
    _text(candidate["reference"], 4096, "candidate_reference_invalid")
    if candidate["revision"] is not None:
        _text(candidate["revision"], 4096, "candidate_revision_invalid")
    _sha(candidate["identity_sha256"], "candidate_identity_invalid", unknown=True)
    model = _object(value["model"], {"reference", "revision", "settings"}, "model_shape_invalid")
    for key in ("reference", "revision"):
        if model[key] is not None:
            _text(model[key], 4096, "model_" + key + "_invalid")
    require(model["settings"] is None or type(model["settings"]) is dict, "model_settings_invalid")
    _bounded_copy(model["settings"], 32_768, "model_settings_bound")
    _public_configuration(model["settings"])
    _strings(value["capabilities"], 32, "capabilities_invalid", width=4096, unique=True)
    _sha(value["rubric_sha256"], "rubric_identity_invalid")
    require(type(value["host_observation"]) is str and
            value["host_observation"] in {"scripted", "generative", "unobserved"}, "host_observation_invalid")
    policy = value["policy"]
    require(type(policy) is dict and len(policy) <= 64, "policy_shape_invalid")
    for key, item in policy.items():
        _text(key, 128, "policy_key_invalid")
        require(item is None or type(item) in {str, bool, int, float}, "policy_scalar_required")
    _bounded_copy(policy, 65_536, "policy_bound")
    _public_configuration(policy)
    guides = _object(value["interface_guides"], ARMS, "guide_inventory_invalid")
    for guide in guides.values():
        _object(guide, {"sha256", "content", "descriptor"}, "guide_shape_invalid")
        _sha(guide["sha256"], "guide_identity_invalid")
        require((guide["content"] is None) != (guide["descriptor"] is None), "guide_delivery_invalid")
        if guide["content"] is not None:
            _text(guide["content"], 65_536, "guide_content_bound")
            require(hashlib.sha256(guide["content"].encode()).hexdigest() == guide["sha256"], "guide_digest_mismatch")
        else:
            _text(guide["descriptor"], 4096, "guide_descriptor_bound")
    return value


def normalize_answer(answer, *, original_text, sources):
    """Return an exact detached answer after checking its immutable source anchors."""
    answer = _bounded_copy(answer, MAX_ANSWER_BYTES, "answer_bound")
    _object(answer, _ANSWER_FIELDS, "answer_shape_invalid")
    _text(original_text, 16_384, "case_original_bound")
    _sources(sources)
    require(answer["original_text"] == original_text, "answer_original_mismatch")
    require(type(answer["behavior"]) is str and answer["behavior"] in {"answered", "abstained", "refused"},
            "answer_behavior_invalid")
    _text(answer["text"], 131_072, "answer_text_bound")
    _text(answer["scope"], 16_384, "answer_scope_bound")
    _strings(answer["limitations"], 32, "answer_limitations_invalid", width=4096)
    citations = answer["citations"]
    require(type(citations) is list and len(citations) <= 64, "answer_citation_bound")
    source_by_id = {source["source_id"]: source for source in sources}
    seen = set()
    for citation in citations:
        _object(citation, {"source_id", "start", "end", "quote"}, "answer_citation_shape_invalid")
        _identifier(citation["source_id"], "answer_source_invalid")
        source = source_by_id.get(citation["source_id"])
        require(source is not None, "answer_source_unknown")
        require(source["rights"]["status"] == "allowed", "answer_source_unusable")
        start, end = citation["start"], citation["end"]
        require(type(start) is int and type(end) is int and 0 <= start < end <= len(source["content"]),
                "answer_anchor_bounds")
        _text(citation["quote"], 65_536, "answer_quote_bound")
        require(source["content"][start:end] == citation["quote"], "answer_anchor_mismatch")
        coordinate = (citation["source_id"], start, end)
        require(coordinate not in seen, "answer_anchor_duplicate")
        seen.add(coordinate)
    return answer


def _historical(value, cases):
    require(type(value) is list and len(value) <= 32, "historical_inventory_invalid")
    case_by_id = {case["id"]: case for case in cases}
    for row in value:
        _object(row, {"baseline_id", "case_id", "answer", "reference", "observations", "comparability_limitations"},
                "historical_shape_invalid")
        _identifier(row["baseline_id"], "historical_id_invalid")
        require(type(row["case_id"]) is str and row["case_id"] in case_by_id, "historical_case_unknown")
        _text(row["reference"], 4096, "historical_reference_invalid")
        case = case_by_id[row["case_id"]]
        row["answer"] = normalize_answer(row["answer"], original_text=case["original_text"], sources=case["sources"])
        observations = _object(row["observations"], {"seconds", "model_calls", "tool_calls", "tokens", "cost"},
                               "historical_observations_invalid")
        for key, observed in observations.items():
            _number(observed, "historical_observation_invalid", integer=key in {"model_calls", "tool_calls", "tokens"},
                    unknown=True)
        _strings(row["comparability_limitations"], 32, "historical_limitations_invalid", minimum=1)
    require(len({row["baseline_id"] for row in value}) == len(value), "historical_inventory_invalid")
    return value


def freeze_plan(*, study_id, cases, conditions, budgets, seed=0, repetitions=1,
                reproduction_case_ids=(), historical_baselines=(), judging_budget=None):
    """Freeze all assignments and caps before any outcome is observed."""
    _identifier(study_id, "study_id_invalid")
    require(type(seed) is int and 0 <= seed <= 2**64 - 1, "seed_invalid")
    require(type(repetitions) is int and 1 <= repetitions <= 3, "repetition_bound")
    cases = _cases(_bounded_copy(cases, MAX_PLAN_BYTES, "case_document_bound"))
    conditions = _conditions(_bounded_copy(conditions, MAX_PLAN_BYTES, "condition_document_bound"))
    budgets = _budgets(_bounded_copy(budgets, 4096, "budget_document_bound"))
    require(type(reproduction_case_ids) in {tuple, list}, "reproduction_inventory_invalid")
    reproduction = list(reproduction_case_ids)
    _strings(reproduction, 8, "reproduction_inventory_invalid", width=128, unique=True)
    require(set(reproduction) <= {case["id"] for case in cases}, "reproduction_case_unknown")
    reproduction.sort()
    require(type(historical_baselines) in {tuple, list}, "historical_inventory_invalid")
    historical = _historical(_bounded_copy(list(historical_baselines), MAX_PLAN_BYTES, "historical_document_bound"), cases)
    if judging_budget is not None:
        judging_budget = _budgets(_bounded_copy(judging_budget, 4096, "judging_budget_bound"))
    schedule = []
    rng = random.Random(seed)  # noqa: S311 -- Reproducible assignments do not require secret randomness.
    for is_reproduction in (False, True):
        for case in cases:
            if is_reproduction and case["id"] not in reproduction:
                continue
            for cycle_index in range(1 + len(case["cycles"])):
                for repetition in range(1, repetitions + 1):
                    arms = list(ARMS)
                    rng.shuffle(arms)
                    for arm in arms:
                        slot = {"case_id": case["id"], "cycle_index": cycle_index, "repetition": repetition,
                                "reproduction": is_reproduction, "arm": arm}
                        slot["slot_id"] = "slot-" + digest({"study_id": study_id, **slot})[:24]
                        schedule.append(slot)
    require(len({slot["slot_id"] for slot in schedule}) == len(schedule), "schedule_slot_collision")
    result = {"schema_version": "evidence-comparative-plan/v1", "study_id": study_id, "cases": cases,
              "conditions": conditions, "budgets": budgets, "judging_budget": judging_budget, "seed": seed,
              "repetitions": repetitions, "reproduction_case_ids": reproduction, "historical_baselines": historical,
              "schedule": schedule}
    result["plan_sha256"] = digest(result)
    checked_packets = set()
    for slot in schedule:
        key = (slot["case_id"], slot["cycle_index"], slot["arm"])
        if key not in checked_packets:
            _generator_packet(result, slot)
            checked_packets.add(key)
    return _bounded_copy(result, MAX_PLAN_BYTES, "plan_document_bound")


def validate_plan(plan):
    """Reproduce the entire closed plan; a digest alone does not validate a schedule."""
    plan = _bounded_copy(plan, MAX_PLAN_BYTES, "plan_document_bound")
    _object(plan, {"schema_version", "study_id", "cases", "conditions", "budgets", "judging_budget", "seed",
                   "repetitions", "reproduction_case_ids", "historical_baselines", "schedule", "plan_sha256"},
            "plan_shape_invalid")
    require(plan["schema_version"] == "evidence-comparative-plan/v1", "plan_version_invalid")
    _sha(plan["plan_sha256"], "plan_identity_invalid")
    require(plan["plan_sha256"] == digest({key: value for key, value in plan.items() if key != "plan_sha256"}),
            "plan_digest_mismatch")
    expected = freeze_plan(**{key: plan[key] for key in ("study_id", "cases", "conditions", "budgets", "judging_budget",
                                                        "seed", "repetitions", "reproduction_case_ids", "historical_baselines")})
    require(plan == expected, "plan_schedule_mismatch")
    return plan


def _case_snapshot(plan, slot):
    case = next(case for case in plan["cases"] if case["id"] == slot["case_id"])
    sources = case["sources"] if slot["cycle_index"] == 0 else case["cycles"][slot["cycle_index"] - 1]["sources"]
    return case, sources


def _visible_sources(sources):
    return [{**source, "content": source["content"] if source["rights"]["status"] == "allowed" else None,
             "content_sha256": hashlib.sha256(source["content"].encode()).hexdigest()} for source in sources]


def _generator_packet(plan, slot):
    case, sources = _case_snapshot(plan, slot)
    return _bounded_copy({"schema_version": "evidence-comparative-generator/v1", "original_text": case["original_text"],
        "sources": _visible_sources(sources), "source_availability": {
            "usable": sum(source["rights"]["status"] == "allowed" for source in sources),
            "unusable": sum(source["rights"]["status"] != "allowed" for source in sources)},
        "conditions": {key: plan["conditions"][key] for key in ("candidate", "model", "capabilities", "host_observation", "policy")},
        "budgets": plan["budgets"], "interface_guide": plan["conditions"]["interface_guides"][slot["arm"]],
        "answer_contract": {"fields": sorted(_ANSWER_FIELDS), "behaviors": ["answered", "abstained", "refused"],
            "citation_fields": ["source_id", "start", "end", "quote"], "offset_unit": "unicode_code_point",
            "maximum_answer_bytes": MAX_ANSWER_BYTES}}, MAX_PACKET_BYTES, "generator_packet_bound")


def generator_packet(plan, slot_id):
    """Expose one exact task snapshot and its own guide, without grading information."""
    plan = validate_plan(plan)
    require(type(slot_id) is str, "slot_id_invalid")
    slot = next((slot for slot in plan["schedule"] if slot["slot_id"] == slot_id), None)
    require(slot is not None, "slot_unknown")
    return _generator_packet(plan, slot)


def _attempts(plan, attempts):
    require(type(attempts) in {tuple, list}, "attempt_inventory_invalid")
    rows = _bounded_copy(list(attempts), MAX_REPORT_BYTES, "attempt_document_bound")
    require(len(rows) <= len(plan["schedule"]) * plan["budgets"]["max_attempts"], "attempt_inventory_bound")
    slots = {slot["slot_id"]: slot for slot in plan["schedule"]}
    expected_packets = {}
    by_slot, by_id = {}, {}
    for row in rows:
        _object(row, _ATTEMPT_FIELDS, "attempt_shape_invalid")
        _identifier(row["attempt_id"], "attempt_id_invalid")
        require(row["attempt_id"] not in by_id, "attempt_id_duplicate")
        require(type(row["slot_id"]) is str and row["slot_id"] in slots, "attempt_slot_unknown")
        slot_id = row["slot_id"]
        require(type(row["attempt_index"]) is int and 1 <= row["attempt_index"] <= plan["budgets"]["max_attempts"],
                "attempt_index_invalid")
        if row["parent_attempt_id"] is not None:
            _identifier(row["parent_attempt_id"], "attempt_parent_invalid")
        require(row["plan_sha256"] == plan["plan_sha256"], "attempt_plan_mismatch")
        if slot_id not in expected_packets:
            expected_packets[slot_id] = digest(_generator_packet(plan, slots[slot_id]))
        require(row["packet_sha256"] == expected_packets[slot_id], "attempt_packet_mismatch")
        require(type(row["status"]) is str and row["status"] in STATUSES, "attempt_status_invalid")
        reserved = _timestamp(row["reserved_at"], "attempt_time_invalid")
        started = _timestamp(row["started_at"], "attempt_time_invalid", unknown=True)
        finished = _timestamp(row["finished_at"], "attempt_time_invalid", unknown=True)
        require(started is None or reserved <= started, "attempt_time_order_invalid")
        require(finished is None or (started or reserved) <= finished, "attempt_time_order_invalid")
        if row["status"] == "reserved":
            require(started is None and finished is None and row["answer"] is None, "attempt_reserved_invalid")
        elif row["status"] == "running":
            require(started is not None and finished is None and row["answer"] is None, "attempt_running_invalid")
        elif row["status"] != "unknown":
            require(finished is not None, "attempt_terminal_time_missing")
        if row["answer"] is not None:
            require(row["status"] in TERMINAL, "attempt_answer_state_invalid")
            case, sources = _case_snapshot(plan, slots[slot_id])
            row["answer"] = normalize_answer(row["answer"], original_text=case["original_text"], sources=sources)
        if row["status"] in {"succeeded", "abstained"}:
            require(started is not None and row["answer"] is not None, "attempt_answer_missing")
        if row["status"] == "abstained":
            require(row["answer"]["behavior"] == "abstained", "attempt_abstention_mismatch")
        receipt = _object(row["receipt"], _RECEIPT_FIELDS, "receipt_shape_invalid")
        require(type(receipt["provenance"]) is str and receipt["provenance"] in {"host_observed", "unverified_import"},
                "receipt_provenance_invalid")
        require(type(receipt["kind"]) is str and receipt["kind"] in {"generative_agent", "scripted_process", "unobserved"},
                "receipt_kind_invalid")
        require(receipt["input_sha256"] == row["packet_sha256"], "receipt_input_mismatch")
        _sha(receipt["output_sha256"], "receipt_output_invalid", unknown=True)
        for key in _METRICS:
            _number(receipt[key], "receipt_" + key + "_invalid", unknown=True,
                    integer=key in {"model_calls", "tool_calls", "tokens"})
        require(receipt["exit_code"] is None or
                (type(receipt["exit_code"]) is int and -255 <= receipt["exit_code"] <= 255), "receipt_exit_invalid")
        refs = receipt["native_receipt_refs"]
        require(type(refs) is list and len(refs) <= 32, "receipt_native_refs_invalid")
        for reference in refs:
            _sha(reference, "receipt_native_refs_invalid")
        require(len(refs) == len(set(refs)), "receipt_native_refs_invalid")
        if row["status"] == "reserved":
            require(all(receipt[key] is None for key in (*_METRICS, "exit_code", "output_sha256")),
                    "receipt_reserved_observation_invalid")
        require(row["answer"] is None or receipt["output_sha256"] is not None, "receipt_answer_output_missing")
        by_id[row["attempt_id"]] = row
        by_slot.setdefault(slot_id, []).append(row)
    for slot_rows in by_slot.values():
        slot_rows.sort(key=lambda row: row["attempt_index"])
        require([row["attempt_index"] for row in slot_rows] == list(range(1, len(slot_rows) + 1)), "attempt_chain_gap")
        for index, row in enumerate(slot_rows):
            parent = None if index == 0 else slot_rows[index - 1]["attempt_id"]
            require(row["parent_attempt_id"] == parent, "attempt_parent_mismatch")
            if index:
                prior = slot_rows[index - 1]
                require(prior["status"] in TERMINAL and prior["finished_at"] is not None, "attempt_parent_open")
                require(_timestamp(prior["finished_at"], "attempt_time_invalid") <=
                        _timestamp(row["reserved_at"], "attempt_time_invalid"), "attempt_retry_time_invalid")
    return rows, by_slot


def _judging_ready(plan, by_slot, generation_closed, binding_outcomes_sealed):
    require(type(generation_closed) is bool and type(binding_outcomes_sealed) is bool, "judgment_seal_invalid")
    require(generation_closed and binding_outcomes_sealed, "judgment_seals_required")
    require(len(by_slot) == len(plan["schedule"]), "judgment_generation_incomplete")
    require(all(row["status"] in TERMINAL and row["finished_at"] is not None
                for rows in by_slot.values() for row in rows), "judgment_generation_open")


def _judgment_groups(plan):
    # Fixed arm-independent order prevents assignment order from becoming a judge cue.
    for case_index, case in enumerate(plan["cases"], 1):
        for cycle_index in range(1 + len(case["cycles"])):
            selected = [slot for slot in plan["schedule"] if
                        slot["case_id"] == case["id"] and slot["cycle_index"] == cycle_index]
            selected.sort(key=lambda slot: digest({"plan_sha256": plan["plan_sha256"], "neutral": slot["slot_id"]}))
            sources = case["sources"] if cycle_index == 0 else case["cycles"][cycle_index - 1]["sources"]
            yield f"case-{case_index:03d}-{cycle_index + 1:02d}", case, sources, selected


def judgment_alias_map(plan, attempts, *, generation_closed=False, binding_outcomes_sealed=False):
    """Return a separate host-only alias binding, never part of the judge packet."""
    plan = validate_plan(plan)
    _, by_slot = _attempts(plan, attempts)
    _judging_ready(plan, by_slot, generation_closed, binding_outcomes_sealed)
    result = []
    for case_alias, _, _, selected in _judgment_groups(plan):
        for slot_index, slot in enumerate(selected, 1):
            slot_rows = by_slot[slot["slot_id"]]
            for row in slot_rows:
                result.append({"case_alias": case_alias, "answer_alias": f"answer-{slot_index:03d}-{row['attempt_index']:02d}",
                               "slot_id": slot["slot_id"], "attempt_id": row["attempt_id"],
                               "final_attempt": row["attempt_index"] == slot_rows[-1]["attempt_index"]})
    return _bounded_copy(result, MAX_REPORT_BYTES, "judgment_alias_bound")


def final_judgment_packet(plan, attempts, *, generation_closed=False, binding_outcomes_sealed=False):
    """Require sealed generation and expose neutral aliases with exact answer evidence."""
    plan = validate_plan(plan)
    _, by_slot = _attempts(plan, attempts)
    _judging_ready(plan, by_slot, generation_closed, binding_outcomes_sealed)
    cases = []
    for case_alias, case, sources, selected in _judgment_groups(plan):
        answers = []
        for slot_index, slot in enumerate(selected, 1):
            for row in by_slot[slot["slot_id"]]:
                answers.append({"answer_alias": f"answer-{slot_index:03d}-{row['attempt_index']:02d}",
                    "execution_status": row["status"], "answer": row["answer"],
                    "answer_sha256": digest(row["answer"]) if row["answer"] is not None else None,
                    "raw_output_sha256": row["receipt"]["output_sha256"]})
        cases.append({"case_alias": case_alias,
                      "original_text": case["original_text"], "sources": _visible_sources(sources), "answers": answers})
    return _bounded_copy({"schema_version": "evidence-comparative-judgment-input/v1", "cases": cases},
                         MAX_REPORT_BYTES, "judgment_packet_bound")


def _judgments(plan, by_slot, judgments):
    require(type(judgments) in {tuple, list}, "judgment_inventory_invalid")
    rows = _bounded_copy(list(judgments), MAX_REPORT_BYTES, "judgment_document_bound")
    require(len(rows) <= len(plan["schedule"]), "judgment_inventory_bound")
    seen = set()
    for row in rows:
        _object(row, _JUDGMENT_FIELDS, "judgment_shape_invalid")
        require(type(row["slot_id"]) is str and row["slot_id"] in by_slot, "judgment_slot_unknown")
        require(row["slot_id"] not in seen, "judgment_slot_duplicate")
        seen.add(row["slot_id"])
        answer = by_slot[row["slot_id"]][-1]["answer"]
        expected = digest(answer) if answer is not None else None
        require(row["answer_sha256"] == expected, "judgment_answer_mismatch")
        _text(row["judge_ref"], 4096, "judge_reference_invalid")
        require(row["rubric_sha256"] == plan["conditions"]["rubric_sha256"], "judgment_rubric_mismatch")
        require(type(row["provenance"]) is str and
                row["provenance"] in {"finite_reference", "agent_adjudicated", "unknown"}, "judgment_provenance_invalid")
        require(type(row["conformance"]) is str and row["conformance"] in {"conforms", "does_not_conform", "unknown"},
                "judgment_conformance_invalid")
        _strings(row["unknown_reasons"], 32, "judgment_unknown_reasons_invalid")
        require(row["conformance"] != "unknown" or bool(row["unknown_reasons"]), "judgment_unknown_reason_missing")
        require(row["provenance"] != "unknown" or row["conformance"] == "unknown", "judgment_unknown_claim_invalid")
        require(answer is not None or row["conformance"] == "unknown", "judgment_missing_answer_claim")
    return rows


def _grading_observation(plan, rows, judgments, grading_receipt, generation_closed, binding_outcomes_sealed):
    """Bind an already host-authenticated grading observation to sealed exact evidence."""
    if grading_receipt is None:
        return None
    receipt = _bounded_copy(grading_receipt, 65_536, "grading_receipt_bound")
    _object(receipt, _GRADING_FIELDS, "grading_receipt_shape_invalid")
    require(receipt["provenance"] == "host_observed", "grading_receipt_provenance_invalid")
    require(type(receipt["kind"]) is str and receipt["kind"] in {"generative_agent", "scripted_process"},
            "grading_receipt_kind_invalid")
    caps = plan["judging_budget"]
    require(caps is not None, "grading_budget_missing")
    packet = final_judgment_packet(plan, rows, generation_closed=generation_closed,
                                   binding_outcomes_sealed=binding_outcomes_sealed)
    require(receipt["input_sha256"] == digest(packet), "grading_receipt_input_mismatch")
    require(receipt["output_sha256"] == digest(judgments), "grading_receipt_output_mismatch")
    require(receipt["rubric_sha256"] == plan["conditions"]["rubric_sha256"], "grading_receipt_rubric_mismatch")
    _text(receipt["profile_ref"], 4096, "grading_receipt_profile_invalid")
    started = _timestamp(receipt["started_at"], "grading_receipt_time_invalid")
    completed = _timestamp(receipt["completed_at"], "grading_receipt_time_invalid")
    require(started.utcoffset().total_seconds() == completed.utcoffset().total_seconds() == 0,
            "grading_receipt_time_not_utc")
    require(started <= completed, "grading_receipt_time_order_invalid")
    require(all(_timestamp(row["finished_at"], "attempt_time_invalid") <= started for row in rows),
            "grading_receipt_precedes_generation")
    _number(receipt["elapsed_seconds"], "grading_receipt_elapsed_invalid")
    require(receipt["elapsed_seconds"] <= caps["seconds"], "grading_receipt_seconds_exceeded")
    usage = _object(receipt["observed_usage"], {"model_calls", "tool_calls", "tokens", "cost"},
                    "grading_receipt_usage_invalid")
    for key, observed in usage.items():
        _number(observed, "grading_receipt_" + key + "_invalid", unknown=True, integer=key != "cost")
        require(observed is None or observed <= caps[key], "grading_receipt_" + key + "_exceeded")
    expected = "agent_adjudicated" if receipt["kind"] == "generative_agent" else "finite_reference"
    require(all(row["provenance"] in {expected, "unknown"} for row in judgments), "grading_receipt_judgment_mode_mismatch")
    return receipt


def project_report(plan, attempts, judgments=(), *, generation_closed=False, binding_outcomes_sealed=False,
                   grading_receipt=None):
    """Retain every denominator; aggregate only explicitly host-observed measurements."""
    plan = validate_plan(plan)
    rows, by_slot = _attempts(plan, attempts)
    require(type(generation_closed) is bool and type(binding_outcomes_sealed) is bool, "judgment_seal_invalid")
    if judgments:
        _judging_ready(plan, by_slot, generation_closed, binding_outcomes_sealed)
    judgments = _judgments(plan, by_slot, judgments)
    grading_receipt = _grading_observation(plan, rows, judgments, grading_receipt,
                                         generation_closed, binding_outcomes_sealed)
    judgment_qualification = "host_observed; provider identity and profile qualification unknown" if grading_receipt else (
        "unverified_import; host grading invocation unobserved" if judgments else "unobserved")
    judge_by_slot = {row["slot_id"]: row for row in judgments}
    case_by_id = {case["id"]: case for case in plan["cases"]}
    slots = []
    for slot in plan["schedule"]:
        retained = by_slot.get(slot["slot_id"], [])
        last = retained[-1] if retained else None
        answer = last["answer"] if last else None
        stratum = case_by_id[slot["case_id"]]["stratum"]
        slots.append({**slot, "stratum": stratum, "attempts": retained,
            "execution_status": last["status"] if last else "missing",
            "answer_behavior": answer["behavior"] if answer is not None else "unknown",
            "judgment": judge_by_slot.get(slot["slot_id"]),
            "declared_answer_conformance": judge_by_slot[slot["slot_id"]]["conformance"] if slot["slot_id"] in judge_by_slot else "unknown",
            "answer_conformance": judge_by_slot[slot["slot_id"]]["conformance"] if
                grading_receipt and slot["slot_id"] in judge_by_slot else "unknown",
            "judgment_execution_qualification": judgment_qualification if slot["slot_id"] in judge_by_slot else "unobserved",
            "native_approval": "separate_receipt_required", "acquisition_qualification": "unknown",
            "execution_qualification": _STRATUM_EXECUTION[stratum]})
    measurements = {}
    missing_slots = sum(not slot["attempts"] for slot in slots)
    for key in _METRICS:
        observed = [row["receipt"][key] for row in rows if row["receipt"]["provenance"] == "host_observed"
                    and row["receipt"][key] is not None]
        measurements[key] = {"host_observed_sum": sum(observed) if observed else None,
                             "host_observed_count": len(observed), "unknown_count": len(rows) - len(observed) + missing_slots,
                             "denominator": len(rows) + missing_slots}
    counts = {"expected_slots": len(slots), "attempts": len(rows),
              "confirmed_started_attempts": sum(row["started_at"] is not None and
                  row["receipt"]["provenance"] == "host_observed" for row in rows),
              "reserved_attempts": sum(row["status"] == "reserved" for row in rows),
              "missing_slots": missing_slots,
              "unknown_conformance_slots": sum(slot["answer_conformance"] == "unknown" for slot in slots),
              "status_attempts": {status: sum(row["status"] == status for row in rows) for status in STATUSES},
              "slot_behaviors": {behavior: sum(slot["answer_behavior"] == behavior for slot in slots)
                                 for behavior in ("answered", "abstained", "refused", "unknown")}}
    strata = [{"stratum": stratum, "expected_slots": sum(slot["stratum"] == stratum for slot in slots),
               "missing_slots": sum(slot["stratum"] == stratum and not slot["attempts"] for slot in slots),
               "unknown_conformance_slots": sum(slot["stratum"] == stratum and
                   slot["answer_conformance"] == "unknown" for slot in slots), "acquisition_qualification": "unknown",
               "execution_qualification": _STRATUM_EXECUTION[stratum]}
              for stratum in STRATA]
    receipt_kinds = ("scripted_process", "generative_agent", "unobserved")
    execution_observation = {"declared_host_observation": plan["conditions"]["host_observation"],
        "host_observed_receipt_kind_counts": {
            kind: sum(row["receipt"]["provenance"] == "host_observed" and row["receipt"]["kind"] == kind for row in rows)
            for kind in receipt_kinds},
        "unverified_import_receipt_kind_counts": {
            kind: sum(row["receipt"]["provenance"] == "unverified_import" and row["receipt"]["kind"] == kind for row in rows)
            for kind in receipt_kinds}}
    control_qualification = {"full_controlled_sdk_caps": "unknown", "observer_binding": "unobserved",
        "process_bounds": {"seconds": {"cap": plan["budgets"]["seconds"], "enforcement": "host_runner_only"},
                           "attempts": {"cap": plan["budgets"]["max_attempts"], "enforcement": "protocol_bound"}},
        "declared_sdk_caps": {key: {"cap": plan["budgets"][key], "control": "unknown"}
                              for key in ("model_calls", "tool_calls", "tokens", "cost")}}
    grading_qualification = {"accuracy_qualified": False, "provider_identity": "unknown", "profile_qualification": "unknown",
        "usage_observation": {key: "observed_within_frozen_cap" if
            grading_receipt and grading_receipt["observed_usage"][key] is not None else "unknown"
            for key in ("model_calls", "tool_calls", "tokens", "cost")}}
    return _bounded_copy({"schema_version": "evidence-comparative-report/v1", "plan_sha256": plan["plan_sha256"],
        "conditions": plan["conditions"], "budgets": plan["budgets"], "judging_budget": plan["judging_budget"],
        "generation_closed": generation_closed, "binding_outcomes_sealed": binding_outcomes_sealed,
        "slots": slots, "counts": counts, "measurements": measurements, "strata": strata,
        "execution_observation": execution_observation, "control_qualification": control_qualification,
        "grading_receipt": grading_receipt, "grading_qualification": grading_qualification,
        "judgment_execution_qualification": judgment_qualification,
        "historical_baselines": [{**row, "classification": "observational_only",
                                  "observation_provenance": "unverified_import"} for row in plan["historical_baselines"]],
        "limitations": ["Finite answer conformance does not establish general factual accuracy.",
                        "Observed resource measurements do not establish causal savings.",
                        "Declared acquisition strata do not authenticate live acquisition.",
                        "Revised snapshots do not establish retained arm state, refresh lineage, or repeated native operations.",
                        "Host process bounds are separate from model-call, tool-call, token, and cost control.",
                        "Full SDK cap control remains unknown without an actual observer binding.",
                        "Receipt kind counts do not establish model execution or provider identity.",
                        "Reasoning quality, operating-system isolation, and credential isolation remain unassessed.",
                        "Imported judgments remain declarations until a bound host grading invocation is measured.",
                        "Native approval is independent of answer conformance.",
                        "Historical baselines are observational and excluded from scheduled arms."]},
        MAX_REPORT_BYTES, "report_document_bound")
