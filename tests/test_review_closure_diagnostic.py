"""Diagnostic closing retains real owner results without granting review credit."""

import copy
import json
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from tools import run_review_closure_diagnostic as diagnostic
from tools._review_dependency_workload import digest, digest_bytes
from tools.decide_release import _HostDeadline


@pytest.fixture
def harness(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    fixture = bundle / "fresh-fixture-input.json"
    fixture.write_text(json.dumps({"fixture": {"sentinel": "fresh-input"}}))
    candidate = {"sha256": "candidate-original"}

    def native_plan(_bundle):
        instant = datetime.now(timezone.utc)
        return {"candidate_sha256": candidate["sha256"], "artifacts": {
            fixture.name: {"path": str(fixture), "raw_sha256": digest_bytes(fixture.read_bytes())}},
            "host": {"host_id": "direct"}, "hard_limits": diagnostic.workload.HARD_LIMITS,
            "scope": "full-fixture", "frozen_at": instant.isoformat(),
            "expires_at": (instant + timedelta(seconds=7200)).isoformat()}

    monkeypatch.setattr(diagnostic.workload, "reference_plan", native_plan)
    events, state = [], {"read_count": 0, "created": [], "retained": []}
    runtime = SimpleNamespace(execution=SimpleNamespace(executor=SimpleNamespace(_attempts={})))

    class Children:
        def __init__(self, originals, plan_sha256):
            self.originals, self.plan_sha256 = originals, plan_sha256

        def open(self):
            events.append("child-open")

        def confirm_completed(self):
            events.append("child-confirm")

        def close(self):
            events.append("child-close")
            self.originals.append(("actual-child-cleanup", {"status": "confirmed"}))
            return {"status": "confirmed", "children": [], "plan_sha256": self.plan_sha256}

    def provision(base, fixture_input, originals, **kwargs):
        events.append("provision")
        assert isinstance(kwargs.pop("children"), Children)
        assert fixture_input == {"sentinel": "fresh-input"}
        assert kwargs["run_seconds"] == state.get("allocation_seconds", 600)
        assert kwargs["allocation_binding"]["review_jobs"] == 0
        state["allocation_binding"] = kwargs["allocation_binding"]
        root = base / "workspace"
        root.mkdir(parents=True)
        clock = datetime.now(timezone.utc)
        allocation = dict(kwargs,
            authority_start=clock.isoformat(),
            authority_end=(clock + timedelta(seconds=state.get("authority_seconds", 7200))).isoformat())
        if kwargs.get("execution_policy") is not None:
            from tools._execution_budget import credential_expiries
            end = kwargs["allocation_binding"]["authority_expires_at"]
            allocation.update(schema_version="review-workload-runtime-allocation/v2", authority_end=end,
                execution_policy_sha256=digest(kwargs["execution_policy"]), credential_expiries=credential_expiries(end),
                run_policy={"budgets": {"seconds": kwargs["run_seconds"]}}, issued_before_worker_outputs=clock.isoformat())
            if state.get("allocation_change"):
                field, value = state["allocation_change"]
                allocation[field] = value
        originals.append(("runtime-allocation", allocation))
        if kwargs.get("execution_policy") is not None:
            originals.append(("qualifications", [{"expires_utc": end}] * 2))
            originals.append(("source-usage", [{"command": {"authentication": {"expires_at": end},
                "payload": {"action": "initialize", "body": {}}}}]))
        return root, runtime, {}, {}, {}

    def receipt(run_id):
        return {"run_id": run_id, "records_sha256": "records", "head_ref": "head",
            "source_sha256": "source", "state_sha256": "state"}

    def create(root, selected, run_ids, originals):
        events.append("create-journals")
        assert selected is runtime
        state["created"] = list(run_ids)
        for run_id in run_ids:
            path = root / "runs" / run_id
            path.mkdir(parents=True)
            (path / "events.jsonl").write_text('{"native":"execution_begin"}\n')
        return [receipt(run_id) for run_id in run_ids]

    def read(root, selected, run_ids, originals, *, observation_policy=None):
        state["read_count"] += 1
        events.append("owner-read-" + str(state["read_count"]))
        age = state.get("witness_ages", {}).get(state["read_count"], state.get("witness_age", 0))
        observed = datetime.now(timezone.utc) - timedelta(seconds=age)
        witness = {"observed_utc": observed.isoformat(), "credentials": {"state": "ready"}}
        for run_id in run_ids:
            originals.append((run_id + "-native-host-qualification", witness))
        if state.get("read_failure") == state["read_count"]:
            raise _HostDeadline()
        receipts = [receipt(run_id) for run_id in run_ids]
        if ((state.get("changed_identity") and state["read_count"] == 2)
                or state.get("changed_closing_identity") and state["read_count"] == 3):
            receipts[0]["head_ref"] = "changed"
        result = {"strict": {"claims": [], "evaluated_at": observed.isoformat()},
            "publication": {"verdict": "no_ship", "evaluated_at": observed.isoformat()},
            "journal_receipts": receipts, "native_host_witnesses": [witness] * len(run_ids),
            "observer_recency_not_after": (observed + timedelta(
                seconds=30 if observation_policy is None else observation_policy["max_age_seconds"])).isoformat()}
        if observation_policy is not None:
            result.update(observation_policy=observation_policy, observation_policy_sha256=digest(observation_policy),
                witness_semantics=observation_policy["witness_semantics"],
                native_current=[{"observed_utc": observed.isoformat(), "policy_ref": "policy",
                    "qualification": {"state": "qualified"}, "rights": "bound"} for _ in run_ids])
            if state.get("changed_closing_policy") and state["read_count"] == 3:
                result["observation_policy_sha256"] = "changed"
            if state.get("changed_closing_current") and state["read_count"] == 3:
                result["native_current"][0][state["changed_closing_current"]] = "withdrawn"
        originals.append(("owner-round-" + str(state["read_count"]), result))
        if ((state.get("candidate_change") and state["read_count"] == 2)
                or state.get("closing_candidate_change") and state["read_count"] == 3):
            candidate["sha256"] = "changed"
        return result, [{"run_id": run_id} for run_id in run_ids], {
            run_id: [{"kind": "execution_begin"}] for run_id in run_ids}

    def authenticate(root, selected, run_id, originals):
        events.append("authenticate-" + run_id)
        result = receipt(run_id)
        originals.append((run_id + "-authenticated", [{"kind": "execution_begin"}]))
        if state.get("history_failure"):
            raise _HostDeadline()
        return [{"kind": "execution_begin"}], result, {}, None

    def verify(snapshots):
        events.append("verify-closing")
        state["verified_snapshots"] = snapshots

    monkeypatch.setattr(diagnostic, "OwnedChildren", Children)
    monkeypatch.setattr(diagnostic, "provision", provision)
    monkeypatch.setattr(diagnostic, "create_journals", create)
    monkeypatch.setattr(diagnostic.workload, "_owner_read", read)
    monkeypatch.setattr(diagnostic, "authenticated_journal", authenticate)
    monkeypatch.setattr(diagnostic, "verify_journal_closing", verify)
    monkeypatch.setattr(diagnostic, "file_manifest", lambda _root: {})
    monkeypatch.setattr(diagnostic, "workload_deadline", lambda seconds: nullcontext())
    plan = diagnostic.reference_plan(bundle)
    return SimpleNamespace(plan=plan, directory=tmp_path / "fresh-run", events=events,
        state=state, runtime=runtime, bundle=bundle, candidate=candidate)


def run(harness):
    return diagnostic.run_diagnostic(harness.plan, digest(harness.plan), harness.directory)


def test_frozen_allocation_retains_native_limits_and_zero_review_jobs(harness):
    plan = harness.plan
    assert diagnostic.allocation_seconds(plan["phase_caps"]) == 600
    assert plan["hard_limits"] == diagnostic.workload.HARD_LIMITS
    assert plan["hard_limits"]["each_owner_pass_read_seconds"] == 120
    assert plan["hard_limits"]["protected_authority_seconds"] == 7200
    assert plan["hard_limits"]["witness_seconds"] == 30
    assert plan["hard_limits"]["native_record_collection_bytes"] == 1048576
    assert plan["journal_count"] == 4 and len(set(plan["run_ids"])) == 4
    assert plan["review_jobs"] == 0 and plan["reference_checks"] == 200
    assert diagnostic.preflight(plan, digest(plan))["qualification"] is False


@pytest.mark.parametrize("count", [True, 3, 14, 4.0])
def test_count_requires_bounded_frozen_integer(harness, count):
    with pytest.raises(ValueError, match="closure_journal_count_invalid"):
        diagnostic.reference_plan(harness.bundle, journal_count=count)


@pytest.mark.parametrize("field,value", [("setup", 181), ("owner_read", 121), ("cleanup", 61),
    ("global_close", 121), ("setup", 0), ("owner_read", True)])
def test_phase_caps_cannot_extend_diagnostic_authority(harness, field, value):
    caps = dict(diagnostic.PHASE_CAPS, **{field: value})
    with pytest.raises(ValueError, match="closure_allocation_invalid"):
        diagnostic.reference_plan(harness.bundle, phase_caps=caps)


def test_complete_diagnostic_has_two_reads_then_cleanup_authentication_retention_and_closing(harness):
    report = run(harness)
    assert report["schema_version"] == "review-closure-diagnostic-observation/v1"
    assert "observation_policy" not in report and "source_protocol_limits" not in report
    assert report["terminal_outcome"] == "diagnostic_completed_unqualified"
    assert report["failure"] is None
    assert report["review_attempts"] == report["applied_jobs"] == report["acceptance_credits"] == 0
    assert report["pair_qualified"] is report["pilot_completed"] is report["automatic_canonical_completion"] is False
    assert report["reference_checks"] == 200
    assert len(report["owner_reads"]) == 2
    assert len(report["journal_receipts"]) == len(report["native_journal_pins"]) == 4
    assert len(harness.state["verified_snapshots"]) == 8
    assert report["retention"]["status"] == "complete"
    assert report["closing_status"] == "within_original_bound"
    assert report["first_witness"]["closed_within_original_bound"] is True
    assert 0 <= report["first_witness"]["closing_latency_seconds"] < 30
    assert len(report["candidate_checks"]) == 2
    assert harness.events.index("owner-read-2") < harness.events.index("child-close")
    assert harness.events.index("child-close") < harness.events.index("authenticate-closure-journal-0")
    assert [row["phase"] for row in report["phase_elapsed"]] == ["setup", "owner-read-1", "owner-read-2", "cleanup", "global-close"]
    assert all(row["status"] == "completed" and row["elapsed_seconds"] >= 0 for row in report["phase_elapsed"])
    assert json.loads((harness.directory / "report.json").read_bytes()) == report


def test_original_first_witness_expiry_is_not_reset_by_second_read(harness):
    harness.state["witness_age"] = 31
    report = run(harness)
    assert report["terminal_outcome"] == "diagnostic_failed_unqualified"
    assert report["failure"]["reason"] == "workload_original_witness_expired"
    assert report["closing_status"] == "original_witness_expired"
    assert report["first_witness"]["closing_latency_seconds"] >= 31
    assert report["first_witness"]["closed_within_original_bound"] is False
    assert report["retention"]["status"] == "complete"


@pytest.mark.parametrize("round_number", [1, 2])
def test_partial_owner_failure_retains_first_witness_and_raw_journals(harness, round_number):
    harness.state["read_failure"] = round_number
    report = run(harness)
    assert report["terminal_outcome"] == "diagnostic_failed_unqualified"
    assert report["failure"]["kind"] == "_HostDeadline"
    assert len(report["owner_reads"]) == round_number - 1
    assert report["first_witness"] is not None
    assert report["first_witness"]["closed_within_original_bound"] is None
    assert report["global_closing_utc"] is None
    assert report["retention"]["status"] == "complete"
    assert report["cleanup"]["status"] == report["child_cleanup"]["status"] == "confirmed"
    assert len(report["native_journal_pins"]) == 4


def test_second_pass_identity_change_refuses_closing(harness):
    harness.state["changed_identity"] = True
    report = run(harness)
    assert report["failure"]["reason"] == "workload_owner_two_pass_changed"
    assert "verify-closing" not in harness.events
    assert len(report["owner_reads"]) == 2
    assert report["retention"]["status"] == "complete"


def test_changed_candidate_is_refused_after_retaining_originals(harness):
    harness.state["candidate_change"] = True
    report = run(harness)
    assert report["failure"]["reason"] == "workload_candidate_or_recipe_changed"
    assert len(report["candidate_checks"]) == 1
    assert report["retention"]["status"] == "complete"
    assert report["original_response_pins"]


def test_history_failure_still_collects_all_raw_journals(harness):
    harness.state["history_failure"] = True
    report = run(harness)
    assert report["terminal_outcome"] == "diagnostic_failed_unqualified"
    assert report["retention"]["status"] == "complete"
    assert len(report["native_journal_pins"]) == 4
    assert len([pin for pin in report["original_response_pins"] if pin["label"].endswith("-history-refusal")]) == 4


def test_failed_setup_keeps_unknown_counts_and_partial_native_originals(harness, monkeypatch):
    def failed(base, fixture, originals, **kwargs):
        partial = base / "workspace" / "runs" / "partial"
        partial.mkdir(parents=True)
        (partial / "events.jsonl").write_text('{"native":"partial"}\n')
        originals.append(("partial-setup", {"status": "started"}))
        raise _HostDeadline()
    monkeypatch.setattr(diagnostic, "provision", failed)
    report = run(harness)
    assert report["review_attempts"] is report["reference_checks"] is None
    assert report["owner_reads"] == [] and report["first_witness"] is None
    assert report["retention"]["status"] == "complete"
    assert len(report["native_journal_pins"]) == 1


def test_changed_frozen_plan_or_existing_directory_refused_before_children(harness):
    altered = copy.deepcopy(harness.plan)
    altered["review_jobs"] = 1
    with pytest.raises(ValueError, match="workload_candidate_or_recipe_changed"):
        diagnostic.run_diagnostic(altered, digest(altered), harness.directory)
    harness.directory.mkdir()
    with pytest.raises(ValueError, match="workload_new_private_external_directory_required"):
        run(harness)
    assert harness.events == []


def test_additional_journals_remain_explicitly_frozen_and_bounded(harness):
    plan = diagnostic.reference_plan(harness.bundle, journal_count=13)
    assert len(plan["run_ids"]) == len(set(plan["run_ids"])) == 13
    assert plan["elapsed_cap_seconds"] == 600


@pytest.mark.parametrize("kind", ["execution_begin", "execution_attempt"])
def test_journal_creation_uses_native_begin_and_refuses_review_records(tmp_path, monkeypatch, kind):
    from evidence_wiki import execution_session as native

    events = []
    runtime = SimpleNamespace(context=SimpleNamespace(activate=lambda: nullcontext()), policy="frozen-policy",
        execution=SimpleNamespace(executor=SimpleNamespace(_attempts={})))

    class Research:
        def session(self, policy, *, run_id, execution):
            assert policy == runtime.policy and execution is runtime.execution
            events.append(("session", run_id))
            return nullcontext(SimpleNamespace(run_id=run_id))

    monkeypatch.setattr(diagnostic.Workspace, "open", lambda root: nullcontext(SimpleNamespace(research=Research())))
    monkeypatch.setattr(native, "gate", lambda session: nullcontext())
    monkeypatch.setattr(native, "_begin", lambda session: events.append(("native-begin", session.run_id)))

    def authenticate(root, selected, run_id, originals):
        events.append(("authenticated", run_id))
        return [{"kind": kind}], {"run_id": run_id}, {}, None

    monkeypatch.setattr(diagnostic, "authenticated_journal", authenticate)
    if kind == "execution_begin":
        receipts = diagnostic.create_journals(tmp_path, runtime, ["one", "two"], [])
        assert receipts == [{"run_id": "one"}, {"run_id": "two"}]
        assert events == [("session", "one"), ("native-begin", "one"), ("session", "two"),
            ("native-begin", "two"), ("authenticated", "one"), ("authenticated", "two")]
    else:
        with pytest.raises(ValueError, match="closure_unexpected_execution_records"):
            diagnostic.create_journals(tmp_path, runtime, ["one", "two"], [])


def test_diagnostic_failure_details_do_not_export_unknown_private_strings():
    secret = "secret-observer-key-and-path"
    error = ValueError(secret)
    error.details = {"reason": secret}
    error.error_code = secret
    assert secret not in json.dumps(diagnostic.failure_document(error))
    local = diagnostic.failure_document(ValueError("closure_review_attempt_observed"))
    assert local["reason"] == "closure_review_attempt_observed"


def test_unconfirmed_children_prevent_success_but_preserve_history_and_raw_journals(harness, monkeypatch):
    def unconfirmed(children):
        children.originals.append(("actual-child-cleanup", {"status": "unavailable"}))
        raise _HostDeadline()
    monkeypatch.setattr(diagnostic.OwnedChildren, "close", unconfirmed)
    report = run(harness)
    assert report["terminal_outcome"] == "diagnostic_failed_unqualified"
    assert report["cleanup"]["status"] == report["child_cleanup"]["status"] == "unavailable"
    assert report["retention"]["status"] == "complete"
    assert len(report["journal_receipts"]) == len(report["native_journal_pins"]) == 4
    assert report["global_closing_utc"] is None


def batch_plan(harness):
    harness.plan = diagnostic.reference_plan(harness.bundle, observation_profile="batch")
    harness.state["allocation_seconds"] = 2040
    return harness.plan


def test_batch_plan_binds_closed_policy_without_changing_native_calibration(harness):
    plan = batch_plan(harness)
    assert plan["schema_version"] == "review-closure-diagnostic-plan/v2"
    assert plan["phase_caps"] == diagnostic.BATCH_PHASE_CAPS
    assert plan["elapsed_cap_seconds"] == 2040 < plan["hard_limits"]["protected_authority_seconds"]
    assert "witness_seconds" not in plan["hard_limits"]
    assert plan["hard_limits"]["each_owner_pass_read_seconds"] == 600
    assert plan["hard_limits"]["global_close_seconds"] == 600
    assert plan["hard_limits"]["observer_recency_seconds"] == 3600
    assert plan["hard_limits"]["native_calibration_probe_lifetime_seconds"] == 30
    assert plan["source_protocol_limits"] == diagnostic.workload.HARD_LIMITS
    assert plan["observation_policy"]["max_age_seconds"] == 3600
    assert plan["observation_policy_sha256"] == digest(plan["observation_policy"])
    assert diagnostic.preflight(plan, digest(plan))["qualification"] is False


@pytest.mark.parametrize("alteration", ["ttl", "digest", "unknown"])
def test_policy_binding_refuses_tampering_before_children(harness, alteration):
    plan = batch_plan(harness)
    if alteration == "ttl":
        plan["observation_policy"]["max_age_seconds"] += 1
    elif alteration == "digest":
        plan["observation_policy_sha256"] = "changed"
    else:
        plan["observation_policy"]["approval"] = True
    with pytest.raises(ValueError, match="observation_policy_invalid|observer_policy_binding_invalid"):
        run(harness)
    assert harness.events == []


@pytest.mark.parametrize("field,key,value", [
    ("hard_limits", "each_owner_pass_read_seconds", 120),
    ("hard_limits", "observer_recency_seconds", 30),
    ("hard_limits", "native_calibration_probe_lifetime_seconds", 3600),
    ("source_protocol_limits", "witness_seconds", 3600),
])
def test_runtime_limits_and_source_provenance_remain_exactly_bound(harness, field, key, value):
    batch_plan(harness)
    harness.plan = copy.deepcopy(harness.plan)
    harness.plan[field][key] = value
    with pytest.raises(ValueError, match="workload_candidate_or_recipe_changed"):
        run(harness)
    assert harness.events == []


def test_batch_closing_refreshes_native_owner_after_cleanup_and_keeps_original_window(harness):
    batch_plan(harness)
    harness.state["witness_ages"] = {1: 90, 2: 60, 3: 0}
    report = run(harness)
    assert report["terminal_outcome"] == "diagnostic_completed_unqualified"
    assert report["schema_version"] == "review-closure-diagnostic-observation/v2"
    for field in ("observation_policy", "observation_policy_sha256", "hard_limits", "source_protocol_limits"):
        assert report[field] == harness.plan[field] == harness.state["allocation_binding"][field]
    assert len(report["owner_reads"]) == 2
    assert report["closing_owner_read"] is not None
    assert harness.state["read_count"] == 3
    assert harness.events.index("child-close") < harness.events.index("owner-read-3")
    assert harness.events.index("owner-read-3") < harness.events.index("verify-closing")
    assert len(harness.state["verified_snapshots"]) == 12
    assert report["closing_owner_read_identity"] == report["owner_read_identities"][0]
    first = report["first_witness"]
    assert 90 <= first["closing_latency_seconds"] < 3600
    assert first["max_age_seconds"] == report["observation_freshness"]["max_age_seconds"] == 3600
    assert report["observation_freshness"]["actual_age_seconds"] == first["closing_latency_seconds"]
    assert datetime.fromisoformat(first["observer_recency_not_after"]) - datetime.fromisoformat(first["observed_utc"]) == timedelta(seconds=3600)
    assert report["review_jobs"] == report["review_attempts"] == report["applied_jobs"] == report["acceptance_credits"] == 0
    assert report["pair_qualified"] is report["pilot_completed"] is False
    assert any(pin["label"] == "closing-owner-read" for pin in report["original_response_pins"])


@pytest.mark.parametrize("age", [3600, 3601])
def test_fresh_closing_owner_never_renews_expired_original_batch_window(harness, age):
    batch_plan(harness)
    harness.state["witness_ages"] = {1: age, 2: 20, 3: 0}
    report = run(harness)
    assert report["terminal_outcome"] == "diagnostic_failed_unqualified"
    assert report["failure"]["reason"] == "workload_original_witness_expired"
    assert report["closing_owner_read"] is not None
    assert report["first_witness"]["closed_within_original_bound"] is False
    assert report["retention"]["status"] == "complete"


@pytest.mark.parametrize("round_number", [1, 2, 3])
def test_batch_future_observation_is_refused(harness, round_number):
    batch_plan(harness)
    harness.state["witness_ages"] = {round_number: -60}
    report = run(harness)
    assert report["terminal_outcome"] == "diagnostic_failed_unqualified"
    assert report["failure"]["reason"] == "observer_clock_invalid"
    assert report["retention"]["status"] == "complete"


@pytest.mark.parametrize("change,reason", [
    ("changed_closing_identity", "closure_owner_closing_changed"),
    ("changed_closing_policy", "observer_policy_binding_invalid"),
    ("closing_candidate_change", "workload_candidate_or_recipe_changed"),
])
def test_closing_change_cannot_turn_batch_freshness_into_approval(harness, change, reason):
    batch_plan(harness)
    harness.state[change] = True
    report = run(harness)
    assert report["terminal_outcome"] == "diagnostic_failed_unqualified"
    assert report["failure"]["reason"] == reason
    assert report["closing_owner_read"] is not None
    assert report["retention"]["status"] == "complete"
    assert report["applied_jobs"] == report["acceptance_credits"] == 0


def test_explicit_interactive_policy_keeps_short_bound_and_two_owner_reads(harness):
    harness.plan = diagnostic.reference_plan(harness.bundle, observation_profile="interactive")
    assert harness.plan["hard_limits"]["each_owner_pass_read_seconds"] == 120
    assert harness.plan["hard_limits"]["global_close_seconds"] == 120
    assert harness.plan["hard_limits"]["observer_recency_seconds"] == 30
    assert harness.plan["hard_limits"]["native_calibration_probe_lifetime_seconds"] == 30
    report = run(harness)
    assert report["terminal_outcome"] == "diagnostic_completed_unqualified"
    assert harness.state["read_count"] == 2
    assert report["closing_owner_read"] is None
    assert report["observation_freshness"]["max_age_seconds"] == 30


@pytest.mark.parametrize("field", ["policy_ref", "qualification", "rights"])
def test_changed_current_owner_authority_refuses_batch_closing(harness, field):
    batch_plan(harness)
    harness.state["changed_closing_current"] = field
    report = run(harness)
    assert report["failure"]["reason"] == "closure_owner_closing_changed"
    assert report["retention"]["status"] == "complete"
    assert report["pair_qualified"] is False


def test_closing_owner_runs_after_raw_retention(harness, monkeypatch):
    batch_plan(harness)
    collect = diagnostic.workload.collect_journals

    def retain_then_withdraw(root, directory):
        result = collect(root, directory)
        harness.events.append("raw-retention")
        harness.state["changed_closing_current"] = "rights"
        return result

    monkeypatch.setattr(diagnostic.workload, "collect_journals", retain_then_withdraw)
    report = run(harness)
    assert harness.events.index("raw-retention") < harness.events.index("owner-read-3")
    assert report["failure"]["reason"] == "closure_owner_closing_changed"


def test_actual_authority_interval_limits_final_batch_clock(harness):
    batch_plan(harness)
    harness.state["authority_seconds"] = -1
    report = run(harness)
    assert report["failure"]["reason"] == "closure_authority_clock_invalid"
    assert report["closing_owner_read"] is not None
    assert report["global_closing_utc"] is not None
    pin = report["runtime_authority"]["original_pin"]
    assert any(all(original[key] == value for key, value in pin.items()) for original in report["original_response_pins"])


def test_plan_expiry_is_checked_after_final_owner_and_snapshot_validation(harness, monkeypatch):
    batch_plan(harness)
    future = datetime.fromisoformat(harness.plan["expires_at"]) + timedelta(seconds=1)

    class ClosingClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return future if harness.state.get("final_validation") else datetime.now(tz)

    verify = diagnostic.verify_journal_closing

    def completed_validation(snapshots):
        verify(snapshots)
        harness.state["final_validation"] = True

    monkeypatch.setattr(diagnostic, "datetime", ClosingClock)
    monkeypatch.setattr(diagnostic, "verify_journal_closing", completed_validation)
    report = run(harness)
    assert report["failure"]["reason"] == "closure_plan_expired_or_clock_invalid"
    assert report["closing_owner_read"] is not None


def test_partial_closing_failure_preserves_original_window_and_raw_history(harness):
    batch_plan(harness)
    harness.state["witness_ages"] = {1: 90, 2: 60}
    harness.state["read_failure"] = 3
    report = run(harness)
    assert report["failure"]["kind"] == "_HostDeadline"
    assert report["first_witness"]["closing_latency_seconds"] is None
    assert report["closing_owner_read"] is None
    assert report["retention"]["status"] == "complete"
    assert len(report["native_journal_pins"]) == 4
    assert len([pin for pin in report["original_response_pins"]
        if pin["label"].endswith("-native-host-qualification")]) == 12
