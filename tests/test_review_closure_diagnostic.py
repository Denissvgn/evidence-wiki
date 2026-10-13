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

        def close(self):
            events.append("child-close")
            self.originals.append(("actual-child-cleanup", {"status": "confirmed"}))
            return {"status": "confirmed", "children": [], "plan_sha256": self.plan_sha256}

    def provision(base, fixture_input, originals, **kwargs):
        events.append("provision")
        assert fixture_input == {"sentinel": "fresh-input"}
        assert kwargs["run_seconds"] == 600
        assert kwargs["allocation_binding"]["review_jobs"] == 0
        root = base / "workspace"
        root.mkdir(parents=True)
        originals.append(("fresh-runtime-allocation", kwargs))
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

    def read(root, selected, run_ids, originals):
        state["read_count"] += 1
        events.append("owner-read-" + str(state["read_count"]))
        observed = datetime.now(timezone.utc) - timedelta(seconds=state.get("witness_age", 0))
        witness = {"observed_utc": observed.isoformat(), "credentials": {"state": "ready"}}
        for run_id in run_ids:
            originals.append((run_id + "-native-host-qualification", witness))
        if state.get("read_failure") == state["read_count"]:
            raise _HostDeadline()
        receipts = [receipt(run_id) for run_id in run_ids]
        if state.get("changed_identity") and state["read_count"] == 2:
            receipts[0]["head_ref"] = "changed"
        result = {"strict": {"claims": [], "evaluated_at": observed.isoformat()},
            "publication": {"verdict": "no_ship", "evaluated_at": observed.isoformat()},
            "journal_receipts": receipts, "native_host_witnesses": [witness] * len(run_ids),
            "observer_recency_not_after": (observed + timedelta(seconds=30)).isoformat()}
        originals.append(("owner-round-" + str(state["read_count"]), result))
        if state.get("candidate_change") and state["read_count"] == 2:
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
