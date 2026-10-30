"""Frozen assignment, evidence isolation and complete comparative denominators."""

import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from tools._comparative_protocol import (
    ARMS,
    STATUSES,
    final_judgment_packet,
    freeze_plan,
    generator_packet,
    judgment_alias_map,
    normalize_answer,
    project_report,
    validate_plan,
)
from tools._journey_cases import digest


def source(*, text="Northern observation: café. Southern conditions are unknown.", rights="allowed", source_id="record"):
    return {"source_id": source_id, "content": text,
            "provenance": {"origin": "retained synthetic record", "captured_at": None, "authorization": None},
            "rights": {"status": rights, "basis": "Authored synthetic record" if rights == "allowed" else None}}


def conditions():
    guides = {arm: {"content": "Only the " + arm + " interface guide.", "descriptor": None} for arm in ARMS}
    for guide in guides.values():
        guide["sha256"] = hashlib.sha256(guide["content"].encode()).hexdigest()
    return {"candidate": {"reference": "retained-candidate", "revision": "fixed-revision", "identity_sha256": digest("candidate")},
            "model": {"reference": None, "revision": None, "settings": None}, "capabilities": ["read_supplied_sources"],
            "rubric_sha256": digest("private finite reference rubric"), "host_observation": "scripted",
            "interface_guides": guides, "policy": {"source_access": "supplied_only", "native_review_required": True}}


def budgets():
    return {"seconds": 30, "model_calls": 20, "tool_calls": 30, "tokens": 4096, "cost": 1.0, "max_attempts": 3}


def case(*, case_id="north", stratum="controlled_snapshot", sources=None, cycles=None):
    value = {"id": case_id, "original_text": "What does this record establish, within its scope?",
             "sources": [source()] if sources is None else sources, "stratum": stratum}
    if cycles is not None:
        value["cycles"] = cycles
    return value


def plan(**kwargs):
    arguments = {"study_id": "bounded-study", "cases": [case()], "conditions": conditions(), "budgets": budgets()}
    arguments.update(kwargs)
    return freeze_plan(**arguments)


def answer(*, original_text=None, sources=None, behavior="answered"):
    supplied = [source()] if sources is None else sources
    original_text = case()["original_text"] if original_text is None else original_text
    quote = supplied[0]["content"][:26]
    return {"original_text": original_text, "behavior": behavior,
            "text": "The retained observation is limited to the northern area." if behavior == "answered" else "No answer released.",
            "scope": "Northern area only", "limitations": ["Southern conditions remain unknown."],
            "citations": [{"source_id": supplied[0]["source_id"], "start": 0, "end": len(quote), "quote": quote}]
                         if behavior == "answered" else []}


def attempt(value, slot, *, status="succeeded", index=1, parent=None, provenance="host_observed", behavior="answered"):
    packet = generator_packet(value, slot["slot_id"])
    original_case = next(row for row in value["cases"] if row["id"] == slot["case_id"])
    supplied = original_case["sources"] if slot["cycle_index"] == 0 else original_case["cycles"][slot["cycle_index"] - 1]["sources"]
    clock = datetime(2026, 10, 8, 9, tzinfo=timezone.utc) + timedelta(seconds=(index - 1) * 10)
    has_started = status not in {"reserved", "abandoned", "unknown"}
    finished = status not in {"reserved", "running", "unknown"}
    retained_answer = answer(original_text=original_case["original_text"], sources=supplied,
                             behavior="abstained" if status == "abstained" else behavior) if status in {"succeeded", "abstained"} else None
    return {"attempt_id": f"attempt-{slot['slot_id']}-{index}", "slot_id": slot["slot_id"], "attempt_index": index,
        "parent_attempt_id": parent, "plan_sha256": value["plan_sha256"], "packet_sha256": digest(packet),
        "reserved_at": clock.isoformat(), "started_at": (clock + timedelta(seconds=1)).isoformat() if has_started else None,
        "finished_at": (clock + timedelta(seconds=2)).isoformat() if finished else None, "status": status, "answer": retained_answer,
        "receipt": {"provenance": provenance, "kind": "scripted_process", "input_sha256": digest(packet),
            "output_sha256": digest({"raw_output": retained_answer}) if finished else None,
            "elapsed_seconds": 1.0 if has_started and finished else None, "exit_code": 0 if status in {"succeeded", "abstained"} else None,
            "model_calls": None, "tool_calls": None, "tokens": None, "cost": None, "native_receipt_refs": []}}


def all_attempts(value):
    return [attempt(value, slot) for slot in value["schedule"]]


def judgment(value, row, *, conformance="conforms", provenance="finite_reference"):
    return {"slot_id": row["slot_id"], "answer_sha256": digest(row["answer"]) if row["answer"] is not None else None,
            "judge_ref": "independent-finite-reference", "rubric_sha256": value["conditions"]["rubric_sha256"],
            "provenance": provenance, "conformance": conformance,
            "unknown_reasons": ["No retained answer evidence"] if conformance == "unknown" else []}


def grading_receipt(value, rows, judgments, *, kind="scripted_process", observed_usage=None):
    packet = final_judgment_packet(value, rows, generation_closed=True, binding_outcomes_sealed=True)
    return {"provenance": "host_observed", "kind": kind, "input_sha256": digest(packet), "output_sha256": digest(judgments),
            "rubric_sha256": value["conditions"]["rubric_sha256"], "profile_ref": "independent-observed-grader",
            "started_at": "2026-10-08T09:05:00Z", "completed_at": "2026-10-08T09:05:01Z", "elapsed_seconds": 1.0,
            "observed_usage": {"model_calls": None, "tool_calls": None, "tokens": None, "cost": None}
                              if observed_usage is None else observed_usage}


def rehash(value):
    value["plan_sha256"] = digest({key: item for key, item in value.items() if key != "plan_sha256"})


def test_assignment_is_detached_deterministic_and_complete_across_cycles_and_reproduction():
    inputs = [case(), case(case_id="updates", stratum="repeated_update", cycles=[{"sources": [source(text="Revised northern record.")]}])]
    common, caps = conditions(), budgets()
    frozen = plan(cases=inputs, conditions=common, budgets=caps, seed=22, repetitions=2, reproduction_case_ids=["updates"])
    repeated = plan(cases=copy.deepcopy(inputs), conditions=copy.deepcopy(common), budgets=copy.deepcopy(caps),
                    seed=22, repetitions=2, reproduction_case_ids=["updates"])
    assert frozen == repeated == validate_plan(frozen)
    assert len(frozen["schedule"]) == 30
    assert len({slot["slot_id"] for slot in frozen["schedule"]}) == 30
    for offset in range(0, len(frozen["schedule"]), 3):
        block = frozen["schedule"][offset:offset + 3]
        assert {slot["arm"] for slot in block} == set(ARMS)
        assert len({(slot["case_id"], slot["cycle_index"], slot["repetition"], slot["reproduction"]) for slot in block}) == 1
    inputs[0]["sources"][0]["content"] = "Changed after freeze"
    common["policy"]["native_review_required"] = False
    caps["seconds"] = 999
    assert frozen["cases"][0]["sources"][0]["content"] == source()["content"]
    assert frozen["conditions"]["policy"]["native_review_required"] is True
    assert frozen["budgets"]["seconds"] == 30
    assert plan(seed=23)["schedule"] != plan(seed=22)["schedule"]


@pytest.mark.parametrize("change", [
    lambda value: value["schedule"].pop(),
    lambda value: value["schedule"].reverse(),
    lambda value: value["schedule"][0].update(arm=value["schedule"][1]["arm"]),
    lambda value: value["schedule"][0].update(cycle_index=1),
    lambda value: value["schedule"][0].update(reproduction=True),
    lambda value: value["schedule"][0].update(slot_id="replacement-slot"),
])
def test_rehashed_assignment_tampering_is_rejected(change):
    frozen = plan()
    change(frozen)
    rehash(frozen)
    with pytest.raises(ValueError, match="plan_schedule_mismatch"):
        validate_plan(frozen)


def test_snapshot_and_interface_digest_drift_fail_closed():
    frozen = plan()
    frozen["cases"][0]["sources"][0]["content"] += " changed"
    with pytest.raises(ValueError, match="plan_digest_mismatch"):
        generator_packet(frozen, frozen["schedule"][0]["slot_id"])
    frozen = plan()
    frozen["conditions"]["interface_guides"]["low_level"]["content"] += " changed"
    rehash(frozen)
    with pytest.raises(ValueError, match="guide_digest_mismatch"):
        validate_plan(frozen)


@pytest.mark.parametrize("kwargs", [
    {"cases": [case(case_id=str(index)) for index in range(9)]},
    {"cases": [case(), case()]},
    {"repetitions": 0}, {"repetitions": 4}, {"repetitions": True},
    {"seed": -1}, {"seed": True}, {"reproduction_case_ids": ["missing"]}, {"reproduction_case_ids": ["north", "north"]},
    {"cases": [case(stratum="unsupported")]},
    {"cases": [case(stratum="repeated_update")]},
    {"cases": [case(cycles=[{"sources": [source()]}])]},
    {"cases": [case(stratum="repeated_update", cycles=[{"sources": [source()]}] * 3)]},
])
def test_case_and_schedule_bounds_are_enforced(kwargs):
    with pytest.raises(ValueError):
        plan(**kwargs)


@pytest.mark.parametrize("key,bad", [
    ("seconds", None), ("seconds", 0), ("seconds", float("inf")), ("cost", float("nan")),
    ("cost", -0.1), ("tokens", None), ("tokens", 1.5), ("model_calls", True),
    ("max_attempts", 4), ("max_attempts", None),
])
def test_caps_never_use_unknown_observations_or_nonfinite_numbers(key, bad):
    caps = budgets()
    caps[key] = bad
    with pytest.raises(ValueError):
        plan(budgets=caps)


def test_closed_shapes_and_finite_json_are_checked_before_use():
    common = conditions()
    common["prior_answers"] = {"north": "privileged answer"}
    with pytest.raises(ValueError, match="condition_shape_invalid"):
        plan(conditions=common)
    common = conditions()
    common["policy"]["nested_authority"] = {"allow": True}
    with pytest.raises(ValueError, match="policy_scalar_required"):
        plan(conditions=common)
    common = conditions()
    common["model"]["settings"] = {"temperature": float("nan")}
    with pytest.raises(ValueError):
        plan(conditions=common)
    cyclic = case()
    cyclic["sources"] = cyclic
    with pytest.raises(ValueError, match="case_document_bound"):
        plan(cases=[cyclic])
    with pytest.raises(ValueError, match="source_content_bound"):
        plan(cases=[case(sources=[source(text="x" * 262_145)])])
    with pytest.raises(ValueError, match="generator_packet_bound"):
        plan(cases=[case(sources=[source(text="x" * 250_000, source_id="record-" + str(index)) for index in range(5)])])


@pytest.mark.parametrize("target", ["policy", "settings"])
def test_shared_configuration_cannot_smuggle_privileged_judgment_fields(target):
    common = conditions()
    if target == "policy":
        common["policy"]["expected_label"] = "secret-label"
    else:
        common["model"]["settings"] = {"metadata": {"judge_answers": "secret-label"}}
    with pytest.raises(ValueError, match="condition_privileged_field"):
        plan(conditions=common)


def test_declared_live_acquisition_is_separate_and_remains_unauthenticated():
    supplied = source()
    supplied["provenance"].update(captured_at="2026-10-08T08:00:00Z", authorization="Owner authorized capture")
    frozen = plan(cases=[case(stratum="live_authorized_acquisition", sources=[supplied])])
    report = project_report(frozen, [])
    live = next(row for row in report["strata"] if row["stratum"] == "live_authorized_acquisition")
    assert live["expected_slots"] == live["missing_slots"] == 3
    assert live["acquisition_qualification"] == "unknown"
    assert all(slot["acquisition_qualification"] == "unknown" for slot in report["slots"])
    supplied["provenance"]["authorization"] = None
    with pytest.raises(ValueError, match="live_case_declaration_incomplete"):
        plan(cases=[case(stratum="live_authorized_acquisition", sources=[supplied])])


def test_generator_packet_exposes_only_own_guide_and_the_exact_cycle_snapshot():
    snapshots = [source(text="The exact revised snapshot.")]
    frozen = plan(cases=[case(stratum="repeated_update", cycles=[{"sources": snapshots}])],
                  reproduction_case_ids=["north"], judging_budget=budgets())
    for slot in frozen["schedule"]:
        packet = generator_packet(frozen, slot["slot_id"])
        assert packet["original_text"] == frozen["cases"][0]["original_text"]
        assert packet["sources"][0]["content"] == (source()["content"] if slot["cycle_index"] == 0 else snapshots[0]["content"])
        assert packet["conditions"]["model"] == {"reference": None, "revision": None, "settings": None}
        assert packet["conditions"]["policy"] == frozen["conditions"]["policy"]
        assert packet["budgets"] == frozen["budgets"]
        assert packet["interface_guide"] == frozen["conditions"]["interface_guides"][slot["arm"]]
        serialized = json.dumps(packet)
        for other_arm in set(ARMS) - {slot["arm"]}:
            assert frozen["conditions"]["interface_guides"][other_arm]["content"] not in serialized
        assert not ({"rubric_sha256", "schedule", "seed", "slot_id", "reproduction", "historical_baselines", "judging_budget",
                     "stratum", "cycle_index", "case_id", "expected_label"} & set(packet))
        assert "rubric_sha256" not in packet["conditions"]
        packet["sources"][0]["content"] = "Modified receiver copy"
    assert generator_packet(frozen, frozen["schedule"][0]["slot_id"])["sources"][0]["content"] == source()["content"]


@pytest.mark.parametrize("rights", ["denied", "unknown"])
def test_unusable_source_bytes_are_withheld_and_cannot_anchor_answers(rights):
    supplied = source(text="Unusable private source bytes.", rights=rights)
    frozen = plan(cases=[case(sources=[supplied])])
    packet = generator_packet(frozen, frozen["schedule"][0]["slot_id"])
    assert packet["sources"][0]["content"] is None
    assert packet["sources"][0]["content_sha256"] == hashlib.sha256(supplied["content"].encode()).hexdigest()
    assert packet["source_availability"] == {"usable": 0, "unusable": 1}
    assert supplied["content"] not in json.dumps(packet)
    with pytest.raises(ValueError, match="answer_source_unusable"):
        normalize_answer(answer(sources=[supplied]), original_text=case()["original_text"], sources=[supplied])


def test_guide_descriptor_is_pinned_without_fetching_or_exposing_other_interfaces():
    common = conditions()
    common["interface_guides"]["document_only"] = {"sha256": digest("external interface bytes"), "content": None,
                                                   "descriptor": "installed/docs/interface.md"}
    frozen = plan(conditions=common)
    slot = next(slot for slot in frozen["schedule"] if slot["arm"] == "document_only")
    assert generator_packet(frozen, slot["slot_id"])["interface_guide"] == common["interface_guides"]["document_only"]


@pytest.mark.parametrize("behavior", ["answered", "abstained", "refused"])
def test_normalized_answer_preserves_original_unicode_and_explicit_behavior(behavior):
    supplied = [source()]
    raw = answer(behavior=behavior)
    normalized = normalize_answer(raw, original_text=case()["original_text"], sources=supplied)
    assert normalized == raw
    assert normalized is not raw and normalized["citations"] is not raw["citations"]
    if behavior == "answered":
        citation = normalized["citations"][0]
        assert "é" in citation["quote"]
        assert supplied[0]["content"][citation["start"]:citation["end"]] == citation["quote"]


@pytest.mark.parametrize("change,reason", [
    (lambda row: row.update(original_text="Altered task"), "answer_original_mismatch"),
    (lambda row: row.update(behavior="approved"), "answer_behavior_invalid"),
    (lambda row: row.update(model="claimed-model"), "answer_shape_invalid"),
    (lambda row: row.update(model_calls=12), "answer_shape_invalid"),
    (lambda row: row.update(native_approval=True), "answer_shape_invalid"),
    (lambda row: row["citations"][0].update(source_id="invented"), "answer_source_unknown"),
    (lambda row: row["citations"][0].update(start=True), "answer_anchor_bounds"),
    (lambda row: row["citations"][0].update(start=-1), "answer_anchor_bounds"),
    (lambda row: row["citations"][0].update(end=99999), "answer_anchor_bounds"),
    (lambda row: row["citations"][0].update(quote="Invented quote"), "answer_anchor_mismatch"),
    (lambda row: row["citations"].append(copy.deepcopy(row["citations"][0])), "answer_anchor_duplicate"),
    (lambda row: row["citations"][0].update(approved=True), "answer_citation_shape_invalid"),
])
def test_answer_schema_and_exact_source_anchors_reject_inventions(change, reason):
    raw = answer()
    change(raw)
    with pytest.raises(ValueError, match=reason):
        normalize_answer(raw, original_text=case()["original_text"], sources=[source()])


def test_repeated_updates_do_not_accept_stale_anchors_from_another_cycle():
    frozen = plan(cases=[case(stratum="repeated_update", cycles=[{"sources": [source(text="Different northern observation.")]}])])
    slot = next(slot for slot in frozen["schedule"] if slot["cycle_index"] == 1)
    row = attempt(frozen, slot)
    row["answer"] = answer()
    with pytest.raises(ValueError, match="answer_anchor_mismatch"):
        project_report(frozen, [row])


def test_report_retains_every_status_and_missing_slot_without_denominator_shrinkage():
    frozen = plan(repetitions=3)
    rows = [attempt(frozen, slot, status=status) for slot, status in zip(frozen["schedule"], STATUSES, strict=False)]
    report = project_report(frozen, rows)
    assert len(report["slots"]) == report["counts"]["expected_slots"] == 9
    assert report["counts"]["attempts"] == 8
    assert report["counts"]["missing_slots"] == 1
    assert report["counts"]["reserved_attempts"] == 1
    assert report["counts"]["confirmed_started_attempts"] == 5
    assert report["counts"]["unknown_conformance_slots"] == 9
    assert report["counts"]["status_attempts"] == dict.fromkeys(STATUSES, 1)
    assert report["counts"]["slot_behaviors"] == {"answered": 1, "abstained": 1, "refused": 0, "unknown": 7}
    assert {slot["execution_status"] for slot in report["slots"]} == set(STATUSES) | {"missing"}
    assert sum(len(slot["attempts"]) for slot in report["slots"]) == 8
    assert report["measurements"]["cost"] == {
        "host_observed_sum": None, "host_observed_count": 0, "unknown_count": 9, "denominator": 9}


def test_report_keeps_failed_parent_and_retry_and_uses_host_metrics_only():
    frozen = plan()
    slot = frozen["schedule"][0]
    failed = attempt(frozen, slot, status="failed")
    retry = attempt(frozen, slot, index=2, parent=failed["attempt_id"], provenance="unverified_import")
    retry["receipt"].update(elapsed_seconds=900, model_calls=100, tool_calls=100, tokens=100_000, cost=999)
    report = project_report(frozen, [retry, failed])
    retained = next(row for row in report["slots"] if row["slot_id"] == slot["slot_id"])
    assert [row["status"] for row in retained["attempts"]] == ["failed", "succeeded"]
    assert report["counts"]["attempts"] == 2
    assert report["counts"]["confirmed_started_attempts"] == 1
    assert report["measurements"]["elapsed_seconds"] == {
        "host_observed_sum": 1.0, "host_observed_count": 1, "unknown_count": 3, "denominator": 4}
    assert report["measurements"]["cost"]["host_observed_sum"] is None
    assert report["measurements"]["tokens"]["host_observed_sum"] is None


@pytest.mark.parametrize("change,reason", [
    (lambda row: row.update(plan_sha256="0" * 64), "attempt_plan_mismatch"),
    (lambda row: row.update(packet_sha256="0" * 64), "attempt_packet_mismatch"),
    (lambda row: row.update(slot_id="nonexistent"), "attempt_slot_unknown"),
    (lambda row: row.update(attempt_index=2), "attempt_chain_gap"),
    (lambda row: row.update(parent_attempt_id="nonexistent"), "attempt_parent_mismatch"),
    (lambda row: row.update(attempt_index=4), "attempt_index_invalid"),
    (lambda row: row.update(started_at="2026-10-08T08:00:00Z"), "attempt_time_order_invalid"),
    (lambda row: row.update(finished_at=None), "attempt_terminal_time_missing"),
    (lambda row: row["receipt"].update(input_sha256="0" * 64), "receipt_input_mismatch"),
    (lambda row: row["receipt"].update(cost=float("nan")), "attempt_document_bound"),
    (lambda row: row["receipt"].update(model_calls=True), "receipt_model_calls_invalid"),
    (lambda row: row["receipt"].update(native_approved=True), "receipt_shape_invalid"),
])
def test_attempt_binding_history_and_observation_tampering_are_rejected(change, reason):
    frozen = plan()
    row = attempt(frozen, frozen["schedule"][0])
    change(row)
    with pytest.raises(ValueError, match=reason):
        project_report(frozen, [row])


def test_duplicate_attempts_and_retries_while_parent_is_running_are_rejected():
    frozen = plan()
    row = attempt(frozen, frozen["schedule"][0])
    with pytest.raises(ValueError, match="attempt_id_duplicate"):
        project_report(frozen, [row, row])
    running = attempt(frozen, frozen["schedule"][0], status="running")
    retry = attempt(frozen, frozen["schedule"][0], index=2, parent=running["attempt_id"])
    with pytest.raises(ValueError, match="attempt_parent_open"):
        project_report(frozen, [running, retry])


def test_historical_baselines_remain_observational_and_excluded_from_assignments_and_measurements():
    historical = {"baseline_id": "human-record", "case_id": "north", "answer": answer(),
        "reference": "Retained human-authored comparison record", "observations": {
            "seconds": 999, "model_calls": 99, "tool_calls": 99, "tokens": 99999, "cost": 99},
        "comparability_limitations": ["Nonrandom historical record; conditions differ."]}
    frozen = plan(historical_baselines=[historical])
    report = project_report(frozen, [])
    assert len(frozen["schedule"]) == 3
    assert all(slot["arm"] in ARMS for slot in frozen["schedule"])
    assert report["counts"]["expected_slots"] == report["counts"]["missing_slots"] == 3
    assert all(metric["host_observed_sum"] is None for metric in report["measurements"].values())
    assert all(metric["unknown_count"] == metric["denominator"] == 3 for metric in report["measurements"].values())
    assert report["historical_baselines"][0]["classification"] == "observational_only"
    assert report["historical_baselines"][0]["observation_provenance"] == "unverified_import"
    packet = generator_packet(frozen, frozen["schedule"][0]["slot_id"])
    assert "human-record" not in json.dumps(packet)
    historical["answer"]["model_calls"] = 99
    with pytest.raises(ValueError, match="answer_shape_invalid"):
        plan(historical_baselines=[historical])


@pytest.mark.parametrize("flags", [{}, {"generation_closed": True}, {"binding_outcomes_sealed": True}])
def test_judgment_requires_both_host_seals_even_when_every_answer_is_present(flags):
    frozen = plan()
    rows = all_attempts(frozen)
    with pytest.raises(ValueError, match="judgment_seals_required"):
        final_judgment_packet(frozen, rows, **flags)
    with pytest.raises(ValueError, match="judgment_seals_required"):
        project_report(frozen, rows, [judgment(frozen, rows[0])], **flags)


def test_seal_claims_do_not_hide_missing_slots_or_unclosed_generation():
    frozen = plan()
    rows = all_attempts(frozen)
    with pytest.raises(ValueError, match="judgment_generation_incomplete"):
        final_judgment_packet(frozen, rows[:-1], generation_closed=True, binding_outcomes_sealed=True)
    rows[-1] = attempt(frozen, frozen["schedule"][-1], status="running")
    with pytest.raises(ValueError, match="judgment_generation_open"):
        final_judgment_packet(frozen, rows, generation_closed=True, binding_outcomes_sealed=True)
    rows[-1] = attempt(frozen, frozen["schedule"][-1], status="unknown")
    with pytest.raises(ValueError, match="judgment_generation_open"):
        final_judgment_packet(frozen, rows, generation_closed=True, binding_outcomes_sealed=True)


def test_final_judge_receives_neutral_aliases_exact_evidence_and_every_retained_attempt():
    common = conditions()
    common["model"].update(reference="private-generator-model", revision="private-revision", settings={"temperature": 0})
    frozen = plan(conditions=common)
    rows = all_attempts(frozen)
    failed = attempt(frozen, frozen["schedule"][0], status="failed")
    retry = attempt(frozen, frozen["schedule"][0], index=2, parent=failed["attempt_id"])
    rows[0:1] = [failed, retry]
    packet = final_judgment_packet(frozen, rows, generation_closed=True, binding_outcomes_sealed=True)
    mapping = judgment_alias_map(frozen, rows, generation_closed=True, binding_outcomes_sealed=True)
    serialized = json.dumps(packet)
    for secret in [*ARMS, "private-generator-model", "private-revision", "retained-candidate", "fixed-revision",
                   frozen["plan_sha256"], frozen["conditions"]["rubric_sha256"], *(row["attempt_id"] for row in rows),
                   *(slot["slot_id"] for slot in frozen["schedule"])]:
        assert secret not in serialized
    assert len(packet["cases"]) == 1
    visible = packet["cases"][0]
    assert visible["original_text"] == frozen["cases"][0]["original_text"]
    assert visible["sources"][0]["content"] == source()["content"]
    assert len(visible["answers"]) == len(mapping) == 4
    assert len({(row["case_alias"], row["answer_alias"]) for row in mapping}) == 4
    assert sum(row["final_attempt"] for row in mapping) == 3
    original_by_id = {row["attempt_id"]: row for row in rows}
    for alias in mapping:
        retained = next(row for row in visible["answers"] if row["answer_alias"] == alias["answer_alias"])
        original = original_by_id[alias["attempt_id"]]
        assert retained["answer"] == original["answer"]
        assert retained["raw_output_sha256"] == original["receipt"]["output_sha256"]
        assert retained["answer_sha256"] == (digest(original["answer"]) if original["answer"] is not None else None)


def test_conformance_grading_preserves_full_denominators_and_never_supplies_native_approval():
    frozen = plan()
    rows = all_attempts(frozen)
    document_slot = next(slot for slot in frozen["schedule"] if slot["arm"] == "document_only")
    document_row = next(row for row in rows if row["slot_id"] == document_slot["slot_id"])
    document_row["answer"] = answer(behavior="refused")
    partial = [judgment(frozen, document_row)]
    report = project_report(frozen, rows, partial, generation_closed=True, binding_outcomes_sealed=True)
    assert report["counts"]["expected_slots"] == 3
    assert report["counts"]["unknown_conformance_slots"] == 3
    visible = next(slot for slot in report["slots"] if slot["arm"] == "document_only")
    assert visible["execution_status"] == "succeeded" and visible["answer_behavior"] == "refused"
    assert visible["declared_answer_conformance"] == "conforms"
    assert visible["answer_conformance"] == "unknown"
    assert visible["judgment_execution_qualification"] == "unverified_import; host grading invocation unobserved"
    assert visible["native_approval"] == "separate_receipt_required"
    assert visible["attempts"][0]["receipt"]["native_receipt_refs"] == []


@pytest.mark.parametrize("change,reason", [
    (lambda row: row.update(answer_sha256="0" * 64), "judgment_answer_mismatch"),
    (lambda row: row.update(rubric_sha256="0" * 64), "judgment_rubric_mismatch"),
    (lambda row: row.update(native_approval=True), "judgment_shape_invalid"),
    (lambda row: row.update(conformance="universally_correct"), "judgment_conformance_invalid"),
    (lambda row: row.update(provenance="unknown"), "judgment_unknown_claim_invalid"),
    (lambda row: row.update(conformance="unknown"), "judgment_unknown_reason_missing"),
])
def test_judgment_binding_and_claim_scope_are_closed(change, reason):
    frozen = plan()
    rows = all_attempts(frozen)
    result = judgment(frozen, rows[0])
    change(result)
    with pytest.raises(ValueError, match=reason):
        project_report(frozen, rows, [result], generation_closed=True, binding_outcomes_sealed=True)


def test_terminal_unknown_outputs_remain_unknown_with_sealed_generation():
    frozen = plan()
    rows = [attempt(frozen, slot, status="unknown") for slot in frozen["schedule"]]
    for row in rows:
        row["finished_at"] = "2026-10-08T09:00:02Z"
    packet = final_judgment_packet(frozen, rows, generation_closed=True, binding_outcomes_sealed=True)
    assert all(row["answer"] is None for row in packet["cases"][0]["answers"])
    results = [judgment(frozen, row, conformance="unknown", provenance="unknown") for row in rows]
    report = project_report(frozen, rows, results, generation_closed=True, binding_outcomes_sealed=True)
    assert report["counts"]["expected_slots"] == report["counts"]["unknown_conformance_slots"] == 3
    assert report["counts"]["confirmed_started_attempts"] == 0
    assert report["counts"]["status_attempts"]["unknown"] == 3


def test_actual_receipt_kinds_remain_separate_from_frozen_host_declarations_and_imports():
    common = conditions()
    common["host_observation"] = "generative"
    frozen = plan(conditions=common)
    rows = all_attempts(frozen)
    rows[0]["receipt"]["kind"] = "scripted_process"
    rows[1]["receipt"]["kind"] = "generative_agent"
    rows[1]["receipt"]["provenance"] = "unverified_import"
    rows[2]["receipt"]["kind"] = "unobserved"
    report = project_report(frozen, rows)
    observation = report["execution_observation"]
    assert observation["declared_host_observation"] == "generative"
    assert observation["host_observed_receipt_kind_counts"] == {"scripted_process": 1, "generative_agent": 0, "unobserved": 1}
    assert observation["unverified_import_receipt_kind_counts"] == {"scripted_process": 0, "generative_agent": 1, "unobserved": 0}


def test_measured_counts_do_not_upgrade_sdk_cap_control_or_snapshot_lineage():
    frozen = plan(cases=[case(stratum="repeated_update", cycles=[{"sources": [source(text="Revised retained bytes.")]}])])
    rows = all_attempts(frozen)
    for row in rows:
        row["receipt"].update(model_calls=0, tool_calls=0, tokens=0, cost=0)
    report = project_report(frozen, rows)
    control = report["control_qualification"]
    assert control["full_controlled_sdk_caps"] == "unknown"
    assert control["observer_binding"] == "unobserved"
    assert control["process_bounds"] == {
        "seconds": {"cap": 30, "enforcement": "host_runner_only"},
        "attempts": {"cap": 3, "enforcement": "protocol_bound"}}
    assert all(value["control"] == "unknown" for value in control["declared_sdk_caps"].values())
    expected = "revised_snapshot_only; repeated native operation unobserved"
    assert all(slot["execution_qualification"] == expected for slot in report["slots"])
    assert next(row for row in report["strata"] if row["stratum"] == "repeated_update")["execution_qualification"] == expected
    assert any("operating-system isolation" in limitation for limitation in report["limitations"])
    assert report["grading_qualification"]["accuracy_qualified"] is False


@pytest.mark.parametrize("kind,provenance", [("scripted_process", "finite_reference"), ("generative_agent", "agent_adjudicated")])
def test_bound_measured_grading_activates_descriptive_conformance_without_accuracy_or_identity_claims(kind, provenance):
    frozen = plan(judging_budget=budgets())
    rows = all_attempts(frozen)
    results = [judgment(frozen, row, provenance=provenance) for row in rows]
    observed = grading_receipt(frozen, rows, results, kind=kind)
    report = project_report(frozen, rows, results, generation_closed=True, binding_outcomes_sealed=True,
                            grading_receipt=observed)
    assert report["counts"]["expected_slots"] == 3
    assert report["counts"]["unknown_conformance_slots"] == 0
    assert all(slot["answer_conformance"] == "conforms" for slot in report["slots"])
    assert all(slot["declared_answer_conformance"] == "conforms" for slot in report["slots"])
    assert report["grading_receipt"] == observed
    assert report["grading_receipt"] is not observed
    assert report["grading_qualification"] == {"accuracy_qualified": False, "provider_identity": "unknown",
        "profile_qualification": "unknown", "usage_observation": {
            "model_calls": "unknown", "tool_calls": "unknown", "tokens": "unknown", "cost": "unknown"}}
    assert report["control_qualification"]["full_controlled_sdk_caps"] == "unknown"


def test_observed_partial_grading_and_usage_retain_missing_judgment_denominators():
    frozen = plan(judging_budget=budgets())
    rows = all_attempts(frozen)
    results = [judgment(frozen, rows[0])]
    observed = grading_receipt(frozen, rows, results,
                               observed_usage={"model_calls": 0, "tool_calls": 0, "tokens": None, "cost": None})
    report = project_report(frozen, rows, results, generation_closed=True, binding_outcomes_sealed=True,
                            grading_receipt=observed)
    assert report["counts"]["expected_slots"] == 3
    assert report["counts"]["unknown_conformance_slots"] == 2
    assert report["grading_qualification"]["usage_observation"] == {
        "model_calls": "observed_within_frozen_cap", "tool_calls": "observed_within_frozen_cap", "tokens": "unknown", "cost": "unknown"}
    assert report["control_qualification"]["full_controlled_sdk_caps"] == "unknown"


@pytest.mark.parametrize("change,reason", [
    (lambda row: row.update(provenance="unverified_import"), "grading_receipt_provenance_invalid"),
    (lambda row: row.update(kind="unobserved"), "grading_receipt_kind_invalid"),
    (lambda row: row.update(input_sha256="0" * 64), "grading_receipt_input_mismatch"),
    (lambda row: row.update(output_sha256="0" * 64), "grading_receipt_output_mismatch"),
    (lambda row: row.update(rubric_sha256="0" * 64), "grading_receipt_rubric_mismatch"),
    (lambda row: row.update(started_at="2026-10-08T09:05:02Z"), "grading_receipt_time_order_invalid"),
    (lambda row: row.update(started_at="2026-10-08T09:05:00+01:00"), "grading_receipt_time_not_utc"),
    (lambda row: row.update(started_at="2026-10-08T08:05:00Z"), "grading_receipt_precedes_generation"),
    (lambda row: row.update(elapsed_seconds=31), "grading_receipt_seconds_exceeded"),
    (lambda row: row.update(elapsed_seconds=None), "grading_receipt_elapsed_invalid"),
    (lambda row: row.update(profile_ref=""), "grading_receipt_profile_invalid"),
    (lambda row: row.update(native_approval=True), "grading_receipt_shape_invalid"),
    (lambda row: row["observed_usage"].update(model_calls=True), "grading_receipt_model_calls_invalid"),
    (lambda row: row["observed_usage"].update(model_calls=21), "grading_receipt_model_calls_exceeded"),
    (lambda row: row["observed_usage"].update(tool_calls=31), "grading_receipt_tool_calls_exceeded"),
    (lambda row: row["observed_usage"].update(tokens=4097), "grading_receipt_tokens_exceeded"),
    (lambda row: row["observed_usage"].update(cost=1.001), "grading_receipt_cost_exceeded"),
    (lambda row: row["observed_usage"].update(cost=float("nan")), "grading_receipt_bound"),
])
def test_grading_receipts_reject_wrong_evidence_modes_times_caps_and_fields(change, reason):
    frozen = plan(judging_budget=budgets())
    rows = all_attempts(frozen)
    results = [judgment(frozen, row) for row in rows]
    observed = grading_receipt(frozen, rows, results)
    change(observed)
    with pytest.raises(ValueError, match=reason):
        project_report(frozen, rows, results, generation_closed=True, binding_outcomes_sealed=True,
                       grading_receipt=observed)


def test_grading_receipt_requires_frozen_caps_and_cannot_upgrade_scripted_judgments_to_agent_adjudication():
    frozen = plan()
    rows = all_attempts(frozen)
    results = [judgment(frozen, row) for row in rows]
    observed = grading_receipt(frozen, rows, results)
    with pytest.raises(ValueError, match="grading_budget_missing"):
        project_report(frozen, rows, results, generation_closed=True, binding_outcomes_sealed=True,
                       grading_receipt=observed)
    frozen = plan(judging_budget=budgets())
    rows = all_attempts(frozen)
    results = [judgment(frozen, row, provenance="agent_adjudicated") for row in rows]
    observed = grading_receipt(frozen, rows, results)
    with pytest.raises(ValueError, match="grading_receipt_judgment_mode_mismatch"):
        project_report(frozen, rows, results, generation_closed=True, binding_outcomes_sealed=True,
                       grading_receipt=observed)


def test_measured_judge_input_binding_includes_exact_answer_evidence():
    frozen = plan(judging_budget=budgets())
    rows = all_attempts(frozen)
    results = [judgment(frozen, row) for row in rows]
    observed = grading_receipt(frozen, rows, results)
    rows[0]["answer"]["text"] = "Altered retained answer after judging."
    results[0]["answer_sha256"] = digest(rows[0]["answer"])
    observed["output_sha256"] = digest(results)
    with pytest.raises(ValueError, match="grading_receipt_input_mismatch"):
        project_report(frozen, rows, results, generation_closed=True, binding_outcomes_sealed=True,
                       grading_receipt=observed)


def test_measured_judge_receipt_cannot_discard_prior_failed_attempts_from_input():
    frozen = plan(judging_budget=budgets())
    rows = all_attempts(frozen)
    results = [judgment(frozen, row) for row in rows]
    observed = grading_receipt(frozen, rows, results)
    failed = attempt(frozen, frozen["schedule"][0], status="failed")
    retry = attempt(frozen, frozen["schedule"][0], index=2, parent=failed["attempt_id"])
    rows[0:1] = [failed, retry]
    assert results[0]["answer_sha256"] == digest(retry["answer"])
    with pytest.raises(ValueError, match="grading_receipt_input_mismatch"):
        project_report(frozen, rows, results, generation_closed=True, binding_outcomes_sealed=True,
                       grading_receipt=observed)
