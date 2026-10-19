"""Historical completed objects cannot mint current comparison authority."""

import hashlib
from types import SimpleNamespace

import pytest

from tools import _review_dependency_accounting as accounting
from tools import _review_dependency_pair_closing as closing
from tools import _review_dependency_runner as runner


def _historical_mode():
    # Exercise the historical custody seam without opening a workspace, launching
    # a child or pretending to produce a valid native execution in a unit test.
    mode = object.__new__(runner._CompletedMode)
    mode.tracker = SimpleNamespace(closed=True)
    mode.native = {"strict": {"accepted": True}, "closed_at": "historical"}
    mode.report = {"global_closing_utc": "historical"}
    mode._native_bytes = accounting.canonical(mode.native)
    mode._report_bytes = accounting.canonical(mode.report)
    runner._mode_members().add(mode)
    return mode


@pytest.mark.parametrize("legacy,selective", [({}, {}), (None, None), (SimpleNamespace(), SimpleNamespace())])
def test_pair_close_refuses_caller_documents(legacy, selective):
    with pytest.raises(ValueError, match="accounting_completed_mode_owner_required"):
        closing.require_current_pair_closing(legacy, selective)


def test_pair_close_refuses_unregistered_mode():
    with pytest.raises(ValueError, match="accounting_completed_mode_owner_required"):
        closing.require_current_pair_closing(object.__new__(runner._CompletedMode), _historical_mode())


def test_pair_close_refuses_one_mode_reused_as_pair():
    mode = _historical_mode()
    with pytest.raises(ValueError, match="accounting_completed_mode_owner_required"):
        closing.require_current_pair_closing(mode, mode)


@pytest.mark.parametrize("changed", ["report", "native", "tracker"])
def test_pair_close_refuses_changed_historical_custody(changed):
    legacy, selective = _historical_mode(), _historical_mode()
    if changed == "tracker":
        legacy.tracker.closed = False
    else:
        getattr(legacy, changed)["changed"] = True
    with pytest.raises(ValueError, match="accounting_completed_mode_changed"):
        closing.require_current_pair_closing(legacy, selective)


def test_intact_historical_snapshots_remain_insufficient_and_immutable():
    legacy, selective = _historical_mode(), _historical_mode()
    before = [(accounting.canonical(mode.native), accounting.canonical(mode.report)) for mode in (legacy, selective)]
    with pytest.raises(closing.PairClosingUnavailable) as caught:
        closing.require_current_pair_closing(legacy, selective)
    assert caught.value.error_code == "accounting_pair_current_closing_unavailable"
    assert before == [(accounting.canonical(mode.native), accounting.canonical(mode.report)) for mode in (legacy, selective)]
    assert runner._failure(caught.value)["error_code"] == "accounting_pair_current_closing_unavailable"


def test_pair_plan_reservation_is_versioned_and_keeps_original_schema_closed():
    schemas = accounting.schema_documents()
    old = schemas["Plan"]["$defs"]["Plan"]
    new = schemas["PairPlan"]["$defs"]["PairPlan"]
    assert old["properties"]["schema_version"] == {"const": accounting.PLAN}
    assert "pair_closing_reservation" not in old["properties"]
    assert new["properties"]["schema_version"] == {"const": accounting.PAIR_PLAN}
    assert "pair_closing_reservation" in new["required"]
    reservation = new["properties"]["pair_closing_reservation"]["const"]
    assert reservation["mode_close_seconds"] + reservation["pair_close_seconds"] == 1500
    assert reservation["pilot_fit"] == "unmeasured"


def test_pair_normalization_evidence_requires_live_closing_pin_in_new_version():
    schemas = accounting.schema_documents()
    old = schemas["NormalizationEvidence"]["$defs"]["NormalizationEvidence"]
    new = schemas["PairNormalizationEvidence"]["$defs"]["PairNormalizationEvidence"]
    assert "current_pair_closing_pin" not in old["properties"]
    assert "current_pair_closing_pin" in new["required"]


def test_reserved_pair_refuses_missing_original_response_owner():
    legacy, selective = _historical_mode(), _historical_mode()
    for mode in (legacy, selective):
        mode.plan = {"schema_version": accounting.PAIR_PLAN}
    with pytest.raises(ValueError, match="accounting_pair_closing_originals_required"):
        closing.require_current_pair_closing(legacy, selective)


def test_reserved_pair_requires_in_guard_normalization_consumer():
    legacy, selective = _historical_mode(), _historical_mode()
    for mode in (legacy, selective):
        mode.plan = {"schema_version": accounting.PAIR_PLAN}
    with pytest.raises(ValueError, match="accounting_pair_closing_consumer_required"):
        closing.require_current_pair_closing(legacy, selective, object())


def test_new_normalization_cannot_mint_without_retained_current_owner():
    with pytest.raises(ValueError, match="accounting_pair_closing_owner_required"):
        accounting._NativeNormalization(accounting._NORMALIZATION_MINT,
            evidence={"schema_version": "review-dependency-differential-normalization-evidence/v2"},
            claims={}, publications={}, stages={}, owners={}, pair={}, plans={}, reports={})


def test_original_schema_export_bytes_remain_frozen():
    exports = accounting.schema_documents()
    original = {name: schema for name, schema in exports.items()
                if name in accounting._ORIGINAL_SCHEMA_DEFS}
    # Frozen from the original literal JSON schema, before versioned additions.
    assert len(original) == 35
    assert hashlib.sha256(accounting.canonical(original)).hexdigest() == "83cdc10896dd63181b7f7bce5fc2ff4d08ca47124acf4713df00a37a7b99639d"
    assert all(len(schema["$defs"]) == 35 for schema in original.values())
    assert len(exports["PairPlan"]["$defs"]) == len(exports["PairNormalizationEvidence"]["$defs"]) == 37


def test_extended_closing_evidence_preserves_each_frozen_decomposition():
    from types import SimpleNamespace

    modes = [SimpleNamespace(plan={"schema_version": accounting.EXTENDED_PLAN, "mode": mode,
        "pair_closing_reservation": accounting.extended_closing_reservation(seconds)})
        for mode, seconds in (("legacy_full", 3000), ("selective_reuse", 6000))]
    evidence = closing._reservation_evidence(modes)
    assert evidence["schema_version"] == "review-current-pair-closing/v2"
    assert evidence["reservations"]["legacy_full"]["pair_close_seconds"] == 2000
    assert evidence["reservations"]["selective_reuse"]["pair_close_seconds"] == 4000
    modes[0].plan["pair_closing_reservation"]["pair_close_seconds"] = 1
    assert evidence["reservations"]["legacy_full"]["pair_close_seconds"] == 2000
    assert closing._reservation_evidence([SimpleNamespace(plan={"schema_version": accounting.PAIR_PLAN})]) == {
        "schema_version": "review-current-pair-closing/v1", "reservation": accounting.PAIR_RESERVATION}
