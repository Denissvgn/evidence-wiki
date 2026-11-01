"""Selective native review stays out of the grouped suite and keeps its assertions on its own route."""

import copy
import json
import subprocess
import sys
from pathlib import Path
from unittest import SkipTest

import pytest

from tests._review_dependency_fixture import execute
from tools import qualify_selective_review as qualify
from tools._review_dependency_case_contracts import case_manifest
from tools._review_dependency_cases import INITIAL_CASES

ROOT = Path(__file__).resolve().parents[1]
RECIPE = "recipe.json"


def initial_report():
    rows = case_manifest()
    for row in rows:
        if row["case_id"] in INITIAL_CASES:
            row.update(status="passed", assertions=[
                "read_only_closing_custody", "exact_before_refresh_core_acceptance",
                "signed_historical_workspace_provenance_preserved"], evidence=[{"state": "eligible"}])
    return {"status": "completed", "mode": "selective_reuse", "case_count": 99, "cases": rows}


def case_row(report, case_id):
    return next(row for row in report["cases"] if row["case_id"] == case_id)


def changed_claim_report():
    report = initial_report()
    case_row(report, "independent-claim-text").update(
        accepted_before_refresh=sorted({"observed-capacity", "effective-capacity", "observed-demand", "adequacy", "margin"}),
        material_invalidated=["independent-note"], refreshed=True,
        assertions=["actual_authenticated_refresh_cohort", "final_native_acceptance_and_publication"])
    return report


def newest_report(case_id="newest-fail-blocks-older-pass"):
    report = {"selected_case_ids": [case_id], "cases": case_manifest()}
    case_row(report, case_id).update(status="passed", assertions=[
        "same_native_claim_and_workspace_bases", "exact_newest_negative_acceptance_set",
        "native_no_older_pass_resurrection"])
    return report


@pytest.fixture
def launches(monkeypatch):
    """Record each child launch instead of running hours of native work; the real checks still judge its report."""
    calls = []

    def fake(directory, **kwargs):
        label = Path(directory).name
        calls.append({"label": label, **kwargs})
        return (initial_report() if label == "initial-cases" else changed_claim_report() if label == "changed-claim"
                else newest_report(kwargs["cases"][0]))

    monkeypatch.setattr(qualify, "execute", fake)
    monkeypatch.setattr(qualify, "require_storage", lambda: None)
    return calls


def arguments(output, *extra):
    return ["--output", str(output), "--extended-recipe", RECIPE, "--execution-profile", "eight-hour", *extra]


def test_plain_route_refuses_selective_mode_before_touching_the_directory(tmp_path, monkeypatch):
    def launched(*_args, **_kwargs):
        pytest.fail("The plain route launched a selective child.")

    monkeypatch.setattr(subprocess, "Popen", launched)
    target = tmp_path / "native"
    with pytest.raises(ValueError, match="selective_reuse_requires_extended_route"):
        execute(target, mode="selective_reuse")
    assert not target.exists()


def test_grouped_module_collects_only_legacy_nodes():
    collected = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "tests/test_native_review_dependencies.py"],
                               cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=120, check=True)
    nodes = {line.split("::", 1)[1] for line in collected.stdout.splitlines() if "::" in line}
    assert nodes == {f"test_actual_complete_review_pipeline_and_reversible_input_custody[legacy_full-{case}]" for case in INITIAL_CASES}


def test_every_moved_workload_runs_selective_mode_on_the_extended_route(tmp_path, launches):
    recipe = str((Path.cwd() / RECIPE).resolve())
    assert qualify.main(arguments(tmp_path / "out", "--temporal-margin-seconds", "1800")) == 0
    assert [call["label"] for call in launches] == ["initial-cases", "changed-claim", "newest-fail", "newest-unknown"]
    assert [(call["cases"], call["refresh"]) for call in launches] == [
        (None, False), (("independent-claim-text",), True),
        (("newest-fail-blocks-older-pass",), False), (("newest-unknown-blocks-older-pass",), False)]
    for call in launches:
        assert call["mode"] == "selective_reuse"
        assert call["route"] == ["--extended-recipe", recipe, "--execution-profile", "eight-hour", "--journal-collection",
                                 "--temporal-margin-seconds", "1800"]
    summary = json.loads((tmp_path / "out" / "qualification.json").read_text(encoding="utf-8"))
    assert summary["status"] == "passed"
    assert [row["status"] for row in summary["workloads"]] == ["passed"] * 4


def test_outer_wait_outlasts_every_fixture_the_child_may_reserve(tmp_path, launches):
    assert qualify.main(arguments(tmp_path / "out")) == 0
    authority = qualify.execution_policy("eight-hour")["authority_seconds"]
    assert launches[0]["wait_seconds"] > len(INITIAL_CASES) * authority
    assert launches[1]["wait_seconds"] > authority


def test_failed_workload_is_reported_and_later_workloads_still_run(tmp_path, launches, monkeypatch):
    fake = qualify.execute

    def failing(directory, **kwargs):
        if Path(directory).name == "changed-claim":
            raise AssertionError("native child failed")
        return fake(directory, **kwargs)

    monkeypatch.setattr(qualify, "execute", failing)
    assert qualify.main(arguments(tmp_path / "out")) == 1
    summary = json.loads((tmp_path / "out" / "qualification.json").read_text(encoding="utf-8"))
    assert summary["status"] == "failed"
    assert [(row["workload"], row["status"]) for row in summary["workloads"]] == [
        ("initial-cases", "passed"), ("changed-claim", "failed"), ("newest-fail", "passed"), ("newest-unknown", "passed")]
    assert summary["workloads"][1]["reason"] == "native child failed"
    assert [call["label"] for call in launches] == ["initial-cases", "newest-fail", "newest-unknown"]


def test_unsupported_storage_refuses_without_creating_output(tmp_path, monkeypatch):
    def unavailable():
        raise SkipTest("no native storage")

    monkeypatch.setattr(qualify, "require_storage", unavailable)
    assert qualify.main(arguments(tmp_path / "out")) == 2
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("missing", ["--output", "--extended-recipe", "--execution-profile"])
def test_route_arguments_are_required(tmp_path, missing):
    given = arguments(tmp_path / "out")
    index = given.index(missing)
    del given[index:index + 2]
    with pytest.raises(SystemExit):
        qualify.main(given)


def test_existing_output_is_refused(tmp_path, launches):
    with pytest.raises(SystemExit):
        qualify.main(arguments(tmp_path))
    assert launches == []


def edit_row(case_id, **fields):
    def change(report):
        case_row(report, case_id).update(fields)
    return change


@pytest.mark.parametrize("change, reason", [
    (lambda report: report.update(status="failed"), "run_not_completed"),
    (lambda report: report.update(mode="legacy_full"), "mode_changed"),
    (lambda report: report.update(case_count=98), "case_inventory_changed"),
    (lambda report: report["cases"].pop(), "case_inventory_changed"),
    (edit_row("unchanged-baseline", status="failed"), "initial_case_not_passed"),
    (edit_row("unchanged-baseline", assertions=[]), "closing_custody_missing"),
    (edit_row("independent-claim-text", assertions=["read_only_closing_custody"]), "core_acceptance_missing"),
    (edit_row("independent-claim-text", evidence=[]), "reuse_evidence_missing"),
    (edit_row("independent-claim-text", evidence=[{"state": "invalidated"}]), "reuse_evidence_ineligible"),
    (edit_row("independent-claim-text", assertions=["read_only_closing_custody", "exact_before_refresh_core_acceptance"]),
     "historical_provenance_missing"),
])
def test_initial_case_check_refuses_each_changed_outcome(change, reason):
    report = initial_report()
    qualify.check_initial_cases(copy.deepcopy(report))
    change(report)
    with pytest.raises(ValueError, match=reason):
        qualify.check_initial_cases(report)


def test_initial_case_check_refuses_an_unselected_case_that_ran():
    report = initial_report()
    next(row for row in report["cases"] if row["case_id"] not in INITIAL_CASES).update(status="passed")
    with pytest.raises(ValueError, match="unselected_case_executed"):
        qualify.check_initial_cases(report)


@pytest.mark.parametrize("fields, reason", [
    ({"accepted_before_refresh": []}, "accepted_set_changed"),
    ({"material_invalidated": []}, "invalidated_set_changed"),
    ({"refreshed": False}, "refresh_missing"),
    ({"assertions": ["final_native_acceptance_and_publication"]}, "refresh_cohort_missing"),
    ({"assertions": ["actual_authenticated_refresh_cohort"]}, "final_publication_missing"),
])
def test_changed_claim_check_refuses_each_changed_outcome(fields, reason):
    report = changed_claim_report()
    qualify.check_changed_claim(copy.deepcopy(report))
    case_row(report, "independent-claim-text").update(fields)
    with pytest.raises(ValueError, match=reason):
        qualify.check_changed_claim(report)


@pytest.mark.parametrize("fields, reason", [
    ({"status": "failed"}, "negative_case_not_passed"),
    ({"assertions": ["exact_newest_negative_acceptance_set", "native_no_older_pass_resurrection"]}, "basis_changed"),
    ({"assertions": ["same_native_claim_and_workspace_bases", "native_no_older_pass_resurrection"]}, "acceptance_set_changed"),
    ({"assertions": ["same_native_claim_and_workspace_bases", "exact_newest_negative_acceptance_set"]}, "older_pass_resurrected"),
])
def test_newest_negative_check_refuses_each_changed_outcome(fields, reason):
    report = newest_report()
    qualify.check_newest_negative(copy.deepcopy(report))
    case_row(report, "newest-fail-blocks-older-pass").update(fields)
    with pytest.raises(ValueError, match=reason):
        qualify.check_newest_negative(report)
