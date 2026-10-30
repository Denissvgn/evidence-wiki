"""Lossless bounded control reports with unchanged direct task facts."""

import hashlib
import json
from copy import deepcopy

import pytest

from tools._acceptance_protocol import bounded_json, canonical_json
from tools._terminal_qualification import control_task_evidence, read_control_task_evidence


def rich_tasks():
    result = {"values": [1, 2], "unicode": "Ω", "valid": False}
    for key in ("facts", "claim", "scope", "authority", "bindings", "payload"):
        result = {key: result}
    return [{"role": role, "started": True, "terminal_observed": True,
        "outcome": "failed" if role == "Integration" else "accepted",
        "receipt_sha256": "a" * 64,
        "reports": [{"kind": "observations", "result": result,
                     "records": [{"index": i, "value": str(i)} for i in range(100)]}]}
        for role in ("Build", "Attack", "Integration", "Measurement", "Verification")]


def duplicated_audit(tasks):
    controls = [{"id": str(i), "tasks": deepcopy(tasks)} for i in range(6)]
    return {"controls": controls, "predicate_results": [
        {"id": str(i), "evidence": [{"actual_controls": deepcopy(controls)}] if i == 33 else []}
        for i in range(36)]}


def test_lossless_reports_keep_direct_fields_and_fit_nested_controls():
    original = rich_tasks()
    encoded = control_task_evidence(original)
    assert read_control_task_evidence(encoded) == original
    for before, after in zip(original, encoded, strict=True):
        assert {k: v for k, v in before.items() if k != "reports"} == {
            k: v for k, v in after.items() if not k.startswith("report_results_")}
        assert after["report_results_json"].encode() == canonical_json(before["reports"])
    with pytest.raises(ValueError, match="acceptance_observation_bound"):
        bounded_json(duplicated_audit(original), 4_194_304)
    audit = duplicated_audit(encoded)
    assert bounded_json(audit, 4_194_304) == audit
    assert original == rich_tasks()


@pytest.mark.parametrize("field,value", [("started", False), ("terminal_observed", False),
                                        ("outcome", "accepted"), ("receipt_sha256", "b" * 64)])
def test_direct_fact_tampering_changes_original_projection(field, value):
    original = rich_tasks()
    changed = deepcopy(original)
    changed[2][field] = value
    assert control_task_evidence(changed) != control_task_evidence(original)


@pytest.mark.parametrize("mutation", ["digest", "data", "noncanonical", "missing_digest", "plain_reports"])
def test_untrusted_encoding_refuses_changed_or_ambiguous_reports(mutation):
    tasks = control_task_evidence(rich_tasks())
    row = tasks[0]
    if mutation == "digest":
        row["report_results_sha256"] = "0" * 64
    elif mutation == "data":
        row["report_results_json"] = "[]\n"
    elif mutation == "noncanonical":
        row["report_results_json"] = json.dumps(json.loads(row["report_results_json"]), indent=2)
        row["report_results_sha256"] = hashlib.sha256(row["report_results_json"].encode()).hexdigest()
    elif mutation == "missing_digest":
        del row["report_results_sha256"]
    else:
        row["reports"] = []
    with pytest.raises(ValueError):
        read_control_task_evidence(tasks)


@pytest.mark.parametrize("key", ["report_results_json", "report_results_sha256"])
def test_already_encoded_input_cannot_overwrite_facts(key):
    tasks = rich_tasks()
    tasks[0][key] = "arbitrary"
    with pytest.raises(ValueError, match="encoding_collision"):
        control_task_evidence(tasks)


def test_original_depth_and_size_bounds_remain_binding():
    nested = {}
    for _ in range(18):
        nested = {"next": nested}
    with pytest.raises(ValueError, match="acceptance_observation_bound"):
        control_task_evidence([{"reports": [nested]}])
    with pytest.raises(ValueError, match="acceptance_observation_bound"):
        control_task_evidence([{"reports": ["x" * 4_194_304]}])
    with pytest.raises(ValueError, match="report_shape"):
        control_task_evidence([{"reports": {}}])


def test_absent_and_empty_reports_stay_distinct():
    tasks = [{"role": "Build"}, {"role": "Attack", "reports": []}]
    assert read_control_task_evidence(control_task_evidence(tasks)) == tasks
