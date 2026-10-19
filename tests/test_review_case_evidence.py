"""Byte custody and partial coverage must not turn DTOs into native authority."""

from __future__ import annotations

import copy
import subprocess
import sys

import pytest

from tests._review_dependency_fixture import case_evidence_report
from tools import _review_dependency_case_evidence as evidence
from tools._review_dependency_case_contracts import CASE_IDS
from tools._review_dependency_cases import case_watchdog
from tools._review_dependency_workload import OriginalResponses, OwnedChildren, retain


def _index(tmp_path, selected=("unchanged-baseline",)):
    report, source = case_evidence_report(selected)
    pin = evidence.write_index(tmp_path / "index", report, source_before=source,
        source_after=source, refresh=False)
    return pin, evidence.read_index(pin, roots=[tmp_path]), source


def test_subset_keeps_full_inventory_and_inert_authority(tmp_path):
    pin, index, source = _index(tmp_path)
    assert pin["size_bytes"] <= evidence.INDEX_BYTES
    assert index["inventory_count"] == len(index["cases"]) == 99
    assert index["coverage"] == "partial_inventory"
    assert index["qualification"] is False and index["authority"] == "byte_evidence_only"
    assert sum(row["status"] == "not_executed" for row in index["cases"]) == 98
    assert evidence.read_case(index["cases"][0], roots=[tmp_path])["assertions"] == ["inert_fixture_assertion"]
    tracker = OwnedChildren(OriginalResponses(tmp_path / "originals"), "3" * 64)
    tracker.closed = True
    with pytest.raises(ValueError, match="live_custody_invalid"):
        evidence._completed_run(pin, tracker, source, source)
    with pytest.raises(ValueError, match="live_mint_refused"):
        evidence._LiveCaseRun(object(), pin, source)


def test_full_inventory_does_not_grant_qualification(tmp_path):
    _pin, index, _source = _index(tmp_path, CASE_IDS)
    assert index["coverage"] == "all_selected"
    assert index["qualification"] is False
    assert index["authority"] == "byte_evidence_only"


def test_unrelated_real_process_cannot_mint_authored_native_completion(tmp_path):
    report, source = case_evidence_report()
    tracker = OwnedChildren(OriginalResponses(tmp_path / "originals"), "3" * 64)
    process = subprocess.Popen([sys.executable, "-I", "-c", "pass"],  # noqa: S603 - fixed interpreter and inert program
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    try:
        assert process.wait(timeout=10) == 0
    finally:
        if process.returncode is None:
            process.kill()
            process.wait(timeout=5)
    tracker.owned.append(process)
    tracker.closed = True
    report["cleanup"]["children"] = [{"pid": process.pid, "leader_reaped": True, "group_absent": True}]
    from tools._review_dependency_case_contracts import case_plan
    plan = retain(tmp_path / "plan.json", case_plan())
    worker = retain(tmp_path / "native-case-worker-manifest.json",
        {"worker_sha256": source["worker_sha256"], "case_plan": plan})
    report["originals"] = [worker]
    pin = evidence.write_index(tmp_path / "index", report, source_before=source,
        source_after=source, refresh=False)
    with pytest.raises(ValueError, match="live_execution_binding_invalid"):
        evidence._completed_run(pin, tracker, source, source)


@pytest.mark.parametrize("mutation", ["candidate", "selection", "scope", "outcome", "qualification", "unknown"])
def test_index_rejects_mismatched_binding_and_coverage(tmp_path, mutation):
    _pin, index, _source = _index(tmp_path)
    value = copy.deepcopy(index)
    if mutation == "candidate":
        value["source"]["candidate_sha256"] = "4" * 64
    elif mutation == "selection":
        value["selected_case_ids"] *= 2
    elif mutation == "scope":
        value["cases"][0]["frozen_assertion_scope"] = "invented"
    elif mutation == "outcome":
        value["cases"][0]["status"] = "not_executed"
    elif mutation == "qualification":
        value["qualification"] = True
    else:
        value["extra"] = None
    with pytest.raises(ValueError):
        evidence.validate_index(value, candidate_sha256="0" * 64)


def test_read_detects_original_byte_changes(tmp_path):
    pin, _index_value, _source = _index(tmp_path)
    changed = dict(pin, raw_sha256="f" * 64)
    with pytest.raises(ValueError):
        evidence.read_index(changed, roots=[tmp_path])
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    with pytest.raises(ValueError):
        evidence.read_index(pin, roots=[unrelated])


def test_strict_json_and_tree_limits(tmp_path):
    for number, raw in enumerate((b'{"a":1,"a":2}', b'{"a":NaN}', b'[' * 17 + b'0' + b']' * 17)):
        pin = retain(tmp_path / (str(number) + ".json"), raw, raw=True)
        with pytest.raises(ValueError):
            evidence.read(pin, roots=[tmp_path])


def test_original_manifest_pages_once_without_inline_dtos(tmp_path):
    report, source = case_evidence_report()
    original = retain(tmp_path / "native.json", {"unchanged": "native bytes"})
    report["originals"] = [dict(original, label="retained")] * 129
    pin = evidence.write_index(tmp_path / "index", report, source_before=source, source_after=source, refresh=True)
    index = evidence.read_index(pin, roots=[tmp_path])
    manifest = evidence.read(index["originals_manifest_pin"], roots=[tmp_path])
    assert len(manifest["pages"]) == 2
    assert evidence.read_manifest(index["originals_manifest_pin"], roots=[tmp_path]) == [original] * 129
    assert "originals" not in index and all("evidence" not in row for row in index["cases"])


def test_many_shard_cleanup_stays_separately_bounded_byte_evidence(tmp_path):
    report, source = case_evidence_report(CASE_IDS)
    report["shards"] = []
    cleanups = []
    for number, offset in enumerate(range(0, len(CASE_IDS), 8)):
        shard_report, _source = case_evidence_report(CASE_IDS[offset:offset + 8])
        cleanup = {"status": "confirmed", "plan_sha256": "3" * 64,
            "children": [{"pid": child + 1, "leader_reaped": True, "group_absent": True,
                "completion_pin": {"path": "/inert/completion", "raw_sha256": "4" * 64, "size_bytes": 0}}
                for child in range(512)]}
        shard_report["cleanup"] = cleanup
        shard_pin = evidence.write_index(tmp_path / ("shard-" + str(number)), shard_report,
            source_before=source, source_after=source, refresh=False)
        report["shards"].append({"report": shard_pin})
        cleanups.append(cleanup)
    report["cleanup"]["children"] = [
        {"pid": number + 1, "leader_reaped": True, "group_absent": True} for number in range(13)]
    report["cleanup"]["shards"] = cleanups
    with pytest.raises(ValueError, match="tree_bound"):
        evidence._tree(report["cleanup"])
    pin = evidence.write_index(tmp_path / "parent", report, source_before=source,
        source_after=source, refresh=False)
    index = evidence.read_index(pin, roots=[tmp_path])
    parent_cleanup = evidence.read(index["cleanup_pin"], roots=[tmp_path], maximum=evidence.DETAIL_BYTES)
    assert len(parent_cleanup["children"]) == 13 and "shards" not in parent_cleanup
    assert len(report["cleanup"]["shards"]) == 13
    for shard_pin in evidence.read_manifest(index["shards_manifest_pin"], roots=[tmp_path]):
        shard_index = evidence.read_index(shard_pin, roots=[tmp_path])
        cleanup = evidence.read(shard_index["cleanup_pin"], roots=[tmp_path], maximum=evidence.DETAIL_BYTES)
        assert len(cleanup["children"]) == 512
        assert shard_index["qualification"] is False


@pytest.mark.parametrize("field", ["status", "leader_reaped", "group_absent"])
def test_retained_native_cleanup_refuses_unconfirmed_children(tmp_path, field):
    report, source = case_evidence_report()
    report["cleanup"]["children"] = [{"pid": 1, "leader_reaped": True, "group_absent": True}]
    if field == "status":
        report["cleanup"]["status"] = "unconfirmed"
    else:
        report["cleanup"]["children"][0][field] = False
    pin = evidence.write_index(tmp_path / "index", report, source_before=source,
        source_after=source, refresh=False)
    index = evidence.read_index(pin, roots=[tmp_path])
    with pytest.raises(ValueError, match="retained_cleanup_invalid"):
        evidence._verify_native_retention(index, roots=[tmp_path], source=source)


def test_watchdog_reserves_each_isolated_fixture_without_extending_run_budget():
    cases = ("qualification-profile", "current-qualification-failure", "producer-identity",
        "mode-caller-flag-cannot-switch", "legacy-envelope-not-convertible",
        "selective-envelope-current-runtime-refusal", "protected-policy-enable-selective", "protected-policy-rollback")
    budget = case_watchdog(cases)
    assert budget["fixture_reservations"] == 9
    assert budget["fixture_run_seconds"] == 5220
    assert budget["watchdog_seconds"] == 9 * (5220 + 600 + 60) + 600
    assert budget["authority_renewal"] == budget["run_budget_extension"] == "forbidden"


def test_failed_native_outcome_remains_visible_without_live_credit(tmp_path):
    report, source = case_evidence_report()
    report.update(status="failed", failure={"kind": "ValueError", "reason": "actual_owner_refused", "error_code": None})
    report["cases"][0].update(status="failed", assertions=[], native_reason="actual_owner_refused")
    pin = evidence.write_index(tmp_path / "index", report, source_before=source, source_after=source, refresh=False)
    index = evidence.read_index(pin, roots=[tmp_path])
    assert index["status"] == index["cases"][0]["status"] == "failed"
    assert evidence.read_case(index["cases"][0], roots=[tmp_path])["native_reason"] == "actual_owner_refused"
    assert evidence.read(index["failure_pin"], roots=[tmp_path])["reason"] == "actual_owner_refused"
    assert index["qualification"] is False


def test_source_and_original_binding_change_refused(tmp_path):
    report, source = case_evidence_report()
    changed = dict(source, worker_sha256="f" * 64)
    with pytest.raises(ValueError, match="source_changed"):
        evidence.write_index(tmp_path / "changed", report, source_before=source, source_after=changed, refresh=False)
    original = retain(tmp_path / "recipe.json", {"exact": "recipe"})
    invalid = dict(original, raw_sha256="f" * 64)
    with pytest.raises(ValueError):
        evidence.freeze_bindings({"recipes_pin": invalid})


def test_earlier_native_owner_scope_does_not_expand_frozen_scope(tmp_path):
    report, source = case_evidence_report(("dependency-basis-byte-bound",))
    row = next(item for item in report["cases"] if item["case_id"] == "dependency-basis-byte-bound")
    row.update(status="passed_limited_scope", native_assertion_scope="physical_workspace_earlier_native_owner_refusal",
        unavailable_branches=["isolated_later_bound_owner"], native_bound_qualification=False)
    report["status"] = "completed_with_limits"
    pin = evidence.write_index(tmp_path / "index", report, source_before=source, source_after=source, refresh=False)
    index = evidence.read_index(pin, roots=[tmp_path])
    indexed = next(item for item in index["cases"] if item["case_id"] == row["case_id"])
    assert indexed["frozen_assertion_scope"] == "native_bound_refusal"
    assert indexed["native_assertion_scope"] == "physical_workspace_earlier_native_owner_refusal"
    assert evidence.read_case(indexed, roots=[tmp_path])["unavailable_branches"] == ["isolated_later_bound_owner"]
