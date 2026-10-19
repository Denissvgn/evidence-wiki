"""Exact retained control identity remains distinct from collection metadata."""

from copy import deepcopy

import pytest

from tools import _review_dependency_pilot as pilot


@pytest.fixture
def custody():
    pin = {"path": "/retained/control.json", "raw_sha256": "a" * 64, "size_bytes": 922}
    return pin, [{**pin, "label": "selective-control-no-dispatch"}]


@pytest.mark.parametrize("labeled", [False, True])
def test_control_accepts_producer_return_and_labeled_historical_pin(custody, labeled):
    pin, originals = custody
    if labeled:
        pin = dict(originals[0])
    before = deepcopy((pin, originals))
    assert pilot._control_original_pin(pin, originals) is pin
    assert (pin, originals) == before


@pytest.mark.parametrize("mutation", [
    "missing_pin", "missing_originals", "empty_originals", "path", "hash", "size",
    "missing_size", "bool_size", "extra_pin_field", "wrong_pin_label", "extra_row_field",
    "missing_row_label", "wrong_row_label", "duplicate", "duplicate_identity_wrong_label",
    "duplicate_label_other_identity", "row_bool_size",
])
def test_control_custody_rejects_ambiguous_or_modified_identity(custody, mutation):
    pin, originals = custody
    if mutation == "missing_pin": pin = None
    elif mutation == "missing_originals": originals = None
    elif mutation == "empty_originals": originals = []
    elif mutation == "path": originals[0]["path"] += "-other"
    elif mutation == "hash": originals[0]["raw_sha256"] = "b" * 64
    elif mutation == "size": originals[0]["size_bytes"] += 1
    elif mutation == "missing_size": del pin["size_bytes"]
    elif mutation == "bool_size": pin["size_bytes"] = True
    elif mutation == "extra_pin_field": pin["other"] = "ignored"
    elif mutation == "wrong_pin_label": pin["label"] = "other"
    elif mutation == "extra_row_field": originals[0]["other"] = "ignored"
    elif mutation == "missing_row_label": del originals[0]["label"]
    elif mutation == "wrong_row_label": originals[0]["label"] = "other"
    elif mutation == "duplicate": originals.append(dict(originals[0]))
    elif mutation == "duplicate_identity_wrong_label": originals.append({**originals[0], "label": "other"})
    elif mutation == "duplicate_label_other_identity": originals.append({**originals[0], "path": "/other"})
    elif mutation == "row_bool_size":
        pin["size_bytes"] = 1
        originals[0]["size_bytes"] = True
    with pytest.raises(ValueError, match="^selective_pilot_control_custody_invalid$"):
        pilot._control_original_pin(pin, originals)


def _reader_prefix(monkeypatch, custody):
    pin, originals = custody
    plan = {"schema_version": pilot.PLAN, "candidate_sha256": "candidate", "native_plan": {}}
    report = {"schema_version": pilot.REPORT, "plan_sha256": pilot.digest(plan),
              "candidate_sha256": "candidate", "native_report_pin": {"path": "/native-report"},
              "mode": "selective_reuse", "terminal_outcome": "pilot_completed_unqualified", "pair_qualified": False}
    native = {"control": {"dispatch_original_pin": pin, "attempt_id": "attempt"},
              "original_response_pins": originals}
    reads = []
    def read(value):
        reads.append(value)
        if value == {"path": "/plan"}: return plan, b"plan"
        if value == {"path": "/report"}: return report, b"report"
        if value == {"path": "/native-report"}: return native, b"native"
        if value is pin: return {"attempt_id": "attempt", "before": [], "after": []}, b"x"
        raise AssertionError("unexpected read")
    monkeypatch.setattr(pilot.legacy, "read_pinned_artifact", read)
    monkeypatch.setattr(pilot, "validate_plan", lambda *args, **kwargs: None)
    monkeypatch.setattr(pilot.ceiling_policy, "validate", lambda *args, **kwargs: {})
    monkeypatch.setattr(pilot, "_report_schema", lambda plan: pilot.REPORT)
    return pin, originals, reads


def test_missing_control_custody_precedes_physical_control_read(monkeypatch, custody):
    pin, originals, reads = _reader_prefix(monkeypatch, custody)
    originals.clear()
    with pytest.raises(ValueError, match="^selective_pilot_control_custody_invalid$"):
        pilot._originals({"path": "/plan"}, {"path": "/report"}, "candidate")
    assert len(reads) == 3
    assert all(value is not pin for value in reads)


def test_physical_control_length_cannot_be_forged_in_both_pins(monkeypatch, custody):
    pin, originals, reads = _reader_prefix(monkeypatch, custody)
    with pytest.raises(ValueError, match="^selective_pilot_control_custody_invalid$"):
        pilot._originals({"path": "/plan"}, {"path": "/report"}, "candidate")
    assert reads[-1] is pin
