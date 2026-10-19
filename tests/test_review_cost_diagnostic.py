"""Bounded diagnostic selection retains observed work without granting credit."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_review_dependency_workload import plan as source_plan
from tools import run_review_cost_diagnostic as diagnostic
from tools._review_dependency_workload import digest

workload_plan = source_plan


@pytest.fixture
def plan(workload_plan):
    bundle = Path(workload_plan["artifacts"]["fresh-fixture-input.json"]["path"]).parent
    return diagnostic.reference_plan(bundle)


def test_frozen_selection_is_exact_and_has_no_qualification(plan):
    assert plan["schema_version"] != diagnostic.workload.PLAN_SCHEMA
    assert plan["scope"] == "all_six_claims_two_questions_global_policy_sources_computation"
    assert plan["phase_caps"] == {"setup": 180, "review_group": 1200, "cleanup": 60, "retention_source_check": 120}
    assert plan["elapsed_cap_seconds"] == 1560 and plan["job_cap_seconds"] == 600
    assert plan["hard_limits"]["protected_authority_seconds"] == 7200
    assert plan["mutations_applied"] is False and plan["qualification"] is False
    assert {(row["cycle"], row["claim_id"], row["config_ref"]) for row in plan["assignments"]} == {
        ("baseline", "observed-capacity", "evidence"), ("baseline", "observed-capacity", "critic")}
    assert plan["reference_checks"] == 200 and plan["owner_read_rounds"] == 0
    assert diagnostic.preflight(plan, digest(plan))["status"] == "prepared_diagnostic_unqualified"


@pytest.mark.parametrize("field,value", [("phase_caps", {"setup": 181}), ("claim_id", "independent-note"),
    ("profile_enabled", 1), ("mutations_applied", True)])
def test_rehashed_selection_changes_are_refused(plan, field, value):
    changed = copy.deepcopy(plan)
    changed[field] = value
    with pytest.raises(ValueError):
        diagnostic.validate_plan(changed, digest(changed))


def test_candidate_and_artifact_changes_are_refused(plan, monkeypatch):
    monkeypatch.setattr(diagnostic.workload, "candidate_manifest", lambda root: {"changed": True})
    with pytest.raises(ValueError, match="workload_candidate_or_recipe_changed"):
        diagnostic.preflight(plan, digest(plan))


@pytest.fixture
def harness(plan, monkeypatch):
    calls, records = [], []
    executor = SimpleNamespace(_attempts={})
    executor.cancel = lambda attempt: calls.append("cancel") or {"state": "succeeded", "final_accounting": True}
    executor.authenticate = lambda observed, attempt: calls.append("authenticate") or observed
    runtime = SimpleNamespace(execution=SimpleNamespace(executor=executor))

    class Children:
        def __init__(self, originals, binding):
            self.originals = originals

        def open(self):
            calls.append("children-open")

        def confirm_completed(self):
            calls.append("children-confirm")

        def close(self):
            calls.append("children-close")
            return {"status": "confirmed", "children": []}

    def provision(directory, fixture, originals, **kwargs):
        calls.append("provision")
        assert isinstance(kwargs["children"], Children)
        assert len(fixture["inventory"]["claims"]) == 6 and len(fixture["inventory"]["questions"]) == 2
        assert kwargs["run_seconds"] == 1560
        root = directory / "workspace"
        root.mkdir(parents=True)
        return root, runtime, {}, {}, []

    def drive(root, runtime, route, run_id, originals, **kwargs):
        calls.append("drive")
        assert isinstance(kwargs["children"], Children)
        assert route == "direct" and kwargs["case_id"] == "independent-claim-text"
        assert kwargs["claim_ids"] == {"observed-capacity"} and kwargs["job_cap_seconds"] == 600
        for index, row in enumerate(kwargs["job_elapsed"]):
            applied = {"kind": "execution_applied", "event_ref": "event-" + str(index),
                "data": {"operation": "evaluate", "claim_id": row["claim_id"], "profile_ref": row["config_ref"],
                    "attempt_id": "attempt-" + str(index), "passed": True}}
            records.append(applied)
            row.update(status="applied", elapsed_seconds=0.5 + index, native_passed=True,
                attempt_id=applied["data"]["attempt_id"], application_event_ref=applied["event_ref"],
                native_record_sha256=applied["event_ref"])
            row["original_timing_pin"] = originals.append(("synthetic-timing-" + str(index),
                {key: value for key, value in row.items() if key != "original_timing_pin"}))
            attempt = SimpleNamespace(attempt_id=row["attempt_id"])
            executor._attempts[row["attempt_id"]] = SimpleNamespace(attempt=attempt)

    def journal(root, runtime, run_id, originals):
        calls.append("journal")
        pin = originals.append(("synthetic-authenticated", records))
        return records, {"head_ref": "head", "source_sha256": "source", "original_pin": pin}, None, None

    def collect(root, directory):
        calls.append("retain")
        return [], {"status": "complete"}

    monkeypatch.setattr(diagnostic, "OwnedChildren", Children)
    monkeypatch.setattr(diagnostic, "provision", provision)
    monkeypatch.setattr(diagnostic, "file_manifest", lambda root: {})
    monkeypatch.setattr(diagnostic.workload, "drive", drive)
    monkeypatch.setattr(diagnostic, "authenticated_journal", journal)
    monkeypatch.setattr(diagnostic.workload, "collect_journals", collect)
    monkeypatch.setattr(diagnostic, "digest_record", lambda record: record["event_ref"])
    return SimpleNamespace(calls=calls, records=records, executor=executor, drive=drive)


def test_success_binds_observed_jobs_and_cleans_before_retention(plan, harness, tmp_path, monkeypatch):
    from tools import _native_cost_profile

    monkeypatch.setattr(_native_cost_profile, "NativeCostProfile", lambda: pytest.fail("disabled profiler was started"))
    report = diagnostic.run_diagnostic(plan, digest(plan), tmp_path / "diagnostic")
    assert report["terminal_outcome"] == "diagnostic_completed_unqualified" and report["applied_jobs"] == 2
    assert report["qualification"] is report["pair_qualified"] is report["pilot_completed"] is False
    assert report["acceptance_credits"] == report["owner_read_rounds"] == 0
    assert report["mutations_applied"] is False and report["instrumentation"]["status"] == "disabled"
    assert len(report["candidate_checks"]) == 2
    assert harness.calls.index("children-close") < harness.calls.index("retain")
    assert harness.calls.count("cancel") == harness.calls.count("authenticate") == 2
    assert [row["elapsed_seconds"] for row in report["job_elapsed"]] == [0.5, 1.5]
    assert all(row["attempt_id"] and row["native_record_sha256"] for row in report["assignments"])


def test_setup_failure_preserves_unknown_jobs_and_failure_originals(plan, harness, tmp_path, monkeypatch):
    def refused(*args, **kwargs):
        raise RuntimeError("private-secret")

    monkeypatch.setattr(diagnostic, "provision", refused)
    report = diagnostic.run_diagnostic(plan, digest(plan), tmp_path / "diagnostic")
    assert report["terminal_outcome"] == "diagnostic_failed_unqualified"
    assert report["applied_jobs"] is None and report["observed_work"] == "unobserved"
    assert all(row["elapsed_seconds"] is None and row["status"] == "unstarted" for row in report["job_elapsed"])
    assert report["cleanup"]["status"] == report["child_cleanup"]["status"] == "confirmed"
    assert "children-close" in harness.calls and "drive" not in harness.calls
    assert any(pin["label"].startswith("failure-") for pin in report["original_response_pins"])
    assert "private-secret" not in json.dumps(report)


def test_partial_drive_failure_keeps_one_application_and_unstarted_role(plan, harness, tmp_path, monkeypatch):
    def interrupted(*args, **kwargs):
        collection = kwargs["job_elapsed"]
        kwargs["job_elapsed"] = collection[:1]
        harness.drive(*args, **kwargs)
        raise RuntimeError("private-secret")

    monkeypatch.setattr(diagnostic.workload, "drive", interrupted)
    report = diagnostic.run_diagnostic(plan, digest(plan), tmp_path / "diagnostic")
    assert report["terminal_outcome"] == "diagnostic_failed_unqualified"
    assert report["applied_jobs"] == 1 and report["observed_work"] == "partial"
    assert report["job_elapsed"][1]["elapsed_seconds"] is None
    assert report["cleanup"]["attempt_ids"] == ["attempt-0"]
    assert report["retention"]["status"] == "complete" and report["acceptance_credits"] == 0


def test_post_cleanup_source_change_refuses_completion(plan, harness, tmp_path, monkeypatch):
    collect = diagnostic.workload.collect_journals

    def changed(root, directory):
        result = collect(root, directory)
        monkeypatch.setattr(diagnostic.workload, "candidate_manifest", lambda root: {"changed": True})
        return result

    monkeypatch.setattr(diagnostic.workload, "collect_journals", changed)
    report = diagnostic.run_diagnostic(plan, digest(plan), tmp_path / "diagnostic")
    assert report["terminal_outcome"] == "diagnostic_failed_unqualified" and report["applied_jobs"] == 2
    assert report["failure"]["reason"] == "workload_candidate_or_recipe_changed"
    assert report["retention"]["status"] == "complete" and len(report["candidate_checks"]) == 1


def test_profile_enter_failure_is_not_replaced_by_report_failure(plan, harness, tmp_path, monkeypatch):
    from tools import _native_cost_profile

    bundle = Path(plan["artifacts"]["fresh-fixture-input.json"]["path"]).parent
    plan = diagnostic.reference_plan(bundle, profile_enabled=True)

    class Refused:
        def __enter__(self):
            raise ValueError("workload_existing_host_timer")

        def __exit__(self, *args):
            pytest.fail("failed entry exited")

        def report(self):
            pytest.fail("failed entry reported")

    monkeypatch.setattr(_native_cost_profile, "NativeCostProfile", Refused)
    report = diagnostic.run_diagnostic(plan, digest(plan), tmp_path / "diagnostic")
    assert report["failure"]["reason"] == "workload_existing_host_timer"
    assert report["instrumentation"]["status"] == "unavailable" and "drive" not in harness.calls
    assert report["cleanup"]["status"] == "confirmed"


def test_profile_scope_is_only_drive_and_exits_before_authenticated_read(plan, harness, tmp_path, monkeypatch):
    from tools import _native_cost_profile

    bundle = Path(plan["artifacts"]["fresh-fixture-input.json"]["path"]).parent
    plan = diagnostic.reference_plan(bundle, profile_enabled=True)

    class Profile:
        def __enter__(self):
            harness.calls.append("profile-enter")
            return self

        def __exit__(self, *args):
            harness.calls.append("profile-exit")

        def report(self):
            return {"diagnostic_only": True, "qualification_granted": False, "overhead": {"total_perturbation": "unknown"}}

    monkeypatch.setattr(_native_cost_profile, "NativeCostProfile", Profile)
    report = diagnostic.run_diagnostic(plan, digest(plan), tmp_path / "diagnostic")
    calls = harness.calls
    assert calls.index("provision") < calls.index("profile-enter") < calls.index("drive") < calls.index("profile-exit") < calls.index("journal")
    assert report["instrumentation"]["profile"]["overhead"]["total_perturbation"] == "unknown"
    assert report["qualification"] is False


def test_binding_cannot_invent_a_missing_job_timing(plan, monkeypatch):
    report = {"assignments": copy.deepcopy(plan["assignments"]), "job_elapsed": [], "applied_jobs": None}
    record = {"kind": "execution_applied", "event_ref": "event", "data": {"operation": "evaluate",
        "claim_id": "observed-capacity", "profile_ref": "evidence", "attempt_id": "attempt", "passed": True}}
    monkeypatch.setattr(diagnostic, "digest_record", lambda record: "digest")
    with pytest.raises(ValueError, match="workload_job_timing_binding_invalid"):
        diagnostic.bind_observed_jobs(report, [record], {"head_ref": "head", "source_sha256": "source"})
    assert report["job_elapsed"] == []
