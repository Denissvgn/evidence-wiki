"""Pure prospective policy boundaries; native owner execution is not simulated."""

import copy
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from evidence_wiki import _review_producer
from evidence_wiki.review_application_lease import ReviewApplicationLease
from tests._review_dependency_fixture import case_evidence_report, fixture_document
from tools import _review_dependency_case_budget as budget
from tools import _review_dependency_case_evidence as evidence
from tools import _review_dependency_pilot as pilot
from tools import _review_dependency_workload as workload
from tools import run_review_dependency_workload as formal
from tools._execution_budget import execution_policy, provisioning_window
from tools._observation_freshness import observation_policy
from tools._review_dependency_case_contracts import (
    REVIEW_TEMPORAL_FIXTURE,
    case_manifest,
    case_plan,
    review_fixture_max_age,
)
from tools._review_dependency_case_services import _construct_profile, _constructor_inputs
from tools._review_dependency_cases import _fixture_inputs, case_watchdog


@pytest.fixture(autouse=True)
def pure_only(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("subprocess_forbidden_in_pure_policy_test")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    root = Path(__file__).resolve().parents[1]
    for module in (budget, workload, evidence, pilot, formal):
        assert Path(module.__file__).resolve().is_relative_to(root / "tools")
    assert Path(_review_producer.__file__).resolve().is_relative_to(root / "src")


@pytest.fixture
def selected(tmp_path, monkeypatch):
    # Only original completed-reader / installed-owner boundaries are isolated.
    # The actual policy, pin, allocation, clocks and provisioning validators run.
    source = case_evidence_report()[1]
    observation = observation_policy("batch")
    native = {"schema_version": formal.PLAN_SCHEMA_EXECUTION, "study": "pilot",
        "candidate_sha256": source["candidate_sha256"], "assignments": [],
        "observation_policy": observation, "observation_policy_sha256": workload.digest(observation)}
    envelope = {"schema_version": pilot.PLAN, "mode": "selective_reuse", "native_plan": native,
        "native_plan_sha256": workload.digest(native), "candidate_sha256": source["candidate_sha256"],
        "obligations": []}
    recipe = {"schema_version": pilot.EXTENDED_RECIPE, "candidate_sha256": source["candidate_sha256"],
        "pilot_plan_pin": workload.retain(tmp_path / "pilot-policy.json", envelope),
        "formal_execution_policy": execution_policy("twenty-four-hour"),
        "phase_caps": {"setup": 100, "job": 700, "mutation": 100, "owner_read": 100,
            "global_close": 100, "cleanup": 30}, "group_overhead_cap_seconds": 100,
        "review_producer_id": "sha256:" + "a" * 64,
        "formal_application_policy": formal.formal_application_policy(700)}
    pin = workload.retain(tmp_path / "measured.json", recipe)
    monkeypatch.setattr(evidence, "source_identity", lambda: source)
    seen = []
    monkeypatch.setattr(pilot, "completed_extended_pilot_measurements", lambda value, candidate:
        seen.append((value, candidate)))
    generation = SimpleNamespace(producer_id=recipe["review_producer_id"])
    monkeypatch.setattr(_review_producer, "capture", lambda: generation)
    monkeypatch.setattr(_review_producer, "assert_current", lambda value: seen.append(value))
    value = budget.select(pin, "twenty-four-hour", fixture_document(), "selective_reuse",
        ["unchanged-baseline"], False)
    assert seen[0] == (recipe, source["candidate_sha256"]) and seen[1] is generation
    return value, recipe


def allocated(selection, case="unchanged-baseline"):
    prepared = datetime.now(timezone.utc)
    fixture = _fixture_inputs(selection["fixture_input"], case, frozen_at=prepared,
        temporal_margin_seconds=selection["temporal_margin_seconds"],
        execution_profile=selection["execution_policy"]["profile"])
    binding = budget.allocation(selection, fixture, "selective_reuse", [case],
        fixture_case_id=case, fixture_prepared_at=prepared.isoformat())
    return binding, fixture


def test_actual_provisioning_window_accepts_native_branch_without_formal_plan(selected):
    selection, _recipe = selected
    binding, fixture = allocated(selection)
    seconds = binding["fixture_budget"]["fixture_run_seconds"]
    budget.validate_allocation(selection["execution_policy"], binding, seconds,
        fixture=fixture, mode="selective_reuse", worker_plan=case_plan())
    begin, end = provisioning_window(selection["execution_policy"], binding, seconds)
    assert datetime.fromisoformat(begin) < datetime.fromisoformat(end)
    assert end == binding["authority_expires_at"]
    assert "formal_plan" not in binding and "formal_service_selection" not in binding
    assert binding["hard_limits"]["native_worker_seconds"] == 600
    assert binding["hard_limits"]["service_application_seconds"] == 700


@pytest.mark.parametrize("change", ["caps", "profile", "producer", "candidate", "worker", "fixture", "mode", "cases", "authority", "run"])
def test_actual_allocation_refuses_changed_bindings(selected, change):
    selection, _recipe = selected
    binding, fixture = allocated(selection)
    value = copy.deepcopy(binding)
    seconds = value["fixture_budget"]["fixture_run_seconds"]
    if change == "caps":
        value["phase_caps"]["job"] += 1
    elif change == "profile":
        value["execution_policy"] = execution_policy("eight-hour")
    elif change == "producer":
        value["native_case_selection"]["review_producer_id"] = "sha256:" + "b" * 64
    elif change == "candidate":
        value["candidate_sha256"] = "f" * 64
    elif change == "worker":
        value["native_case_plan"]["default_verdict"] = "fail"
    elif change == "fixture":
        fixture["source_material"]["shared"]["raw"] += "changed"
    elif change == "mode":
        value["native_case_mode"] = "legacy_full"
    elif change == "cases":
        value["native_case_ids"] = ["source-addition"]
    elif change == "authority":
        value["authority_expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    else:
        seconds += 1
    with pytest.raises((ValueError, AssertionError)):
        budget.validate_allocation(selection["execution_policy"], value, seconds,
            fixture=fixture, mode="selective_reuse", worker_plan=case_plan())


def test_current_authentication_failure_is_not_interpreted_as_complete(selected, monkeypatch):
    selection, _recipe = selected
    def unavailable(*_args):
        raise ValueError("selective_pilot_report_binding_invalid")
    monkeypatch.setattr(pilot, "completed_extended_pilot_measurements", unavailable)
    with pytest.raises(ValueError, match="selective_pilot_report_binding_invalid"):
        budget.validate_selection(selection, current=True)


def test_temporal_margin_is_prospective_and_does_not_widen_review_age(selected):
    selection, _recipe = selected
    selection = copy.deepcopy(selection)
    selection["selected_case_ids"] = ["source-effective-interval-ended"]
    reserve = budget.validate_selection(selection)
    selection["temporal_margin_seconds"] = reserve["fixture_run_seconds"] + reserve["fixture_overhead_seconds"] + 100
    binding, fixture = allocated(selection, "source-effective-interval-ended")
    budget.validate_allocation(selection["execution_policy"], binding, reserve["fixture_run_seconds"],
        fixture=fixture, mode="selective_reuse", worker_plan=case_plan())
    assert _fixture_inputs(selection["fixture_input"], "review-max-age-expired")["global_inputs"]["policy"]["max_review_age_seconds"] == 600
    selection["temporal_margin_seconds"] = 86400
    with pytest.raises(ValueError):
        budget.validate_selection(selection)


@pytest.mark.parametrize("profile", [None, "two-hour", "four-hour", "six-hour", "eight-hour",
                                     "sixteen-hour", "twenty-four-hour", "forty-eight-hour"])
def test_review_age_fixture_is_selected_by_execution_profile(profile):
    original = fixture_document()
    before = copy.deepcopy(original)
    derived = _fixture_inputs(original, "review-max-age-expired", execution_profile=profile)
    assert derived["global_inputs"]["policy"]["max_review_age_seconds"] == (86400 if profile == "forty-eight-hour" else 600)
    assert original == before
    assert review_fixture_max_age(profile) == derived["global_inputs"]["policy"]["max_review_age_seconds"]


@pytest.mark.parametrize("profile", [True, False, 48, "unknown", ""])
def test_unknown_temporal_execution_profile_refused(profile):
    with pytest.raises(AssertionError, match="native_temporal_fixture_profile_invalid"):
        _fixture_inputs(fixture_document(), "review-max-age-expired", execution_profile=profile)


def test_temporal_fixture_contract_is_part_of_exact_native_manifest_identity():
    rows = case_manifest()
    expiry = next(row for row in rows if row["case_id"] == "review-max-age-expired")
    assert expiry["temporal_fixture"] == REVIEW_TEMPORAL_FIXTURE
    assert all("temporal_fixture" not in row for row in rows if row["case_id"] != "review-max-age-expired")
    identity = evidence.source_identity()
    assert identity["case_manifest_sha256"] == workload.digest(rows)
    expiry["temporal_fixture"]["default_max_review_age_seconds"] = 1
    assert workload.digest(rows) != identity["case_manifest_sha256"]
    assert case_manifest() != rows


def test_allocation_refuses_day_fixture_under_shorter_selected_profile(selected):
    selection, _recipe = selected
    selection = copy.deepcopy(selection)
    selection["selected_case_ids"] = ["review-max-age-expired"]
    binding, fixture = allocated(selection, "review-max-age-expired")
    fixture["global_inputs"]["policy"]["max_review_age_seconds"] = 86400
    binding["fixture_sha256"] = workload.digest(fixture)
    with pytest.raises(ValueError, match="native_case_extended_binding_invalid"):
        budget.validate_allocation(selection["execution_policy"], binding, binding["fixture_budget"]["fixture_run_seconds"],
            fixture=fixture, mode="selective_reuse", worker_plan=case_plan())


def test_budget_reserves_fit_selected_authority_or_refuse(selected):
    selection, recipe = selected
    for profile in ("eight-hour", "sixteen-hour", "twenty-four-hour"):
        assert budget.fixture_budget(recipe, execution_policy(profile))["service_applications"] == 24
    large = copy.deepcopy(recipe)
    large["phase_caps"] = formal.formal_phase_ceilings(selection["observation_policy"], selection["execution_policy"])
    large["formal_application_policy"] = formal.formal_application_policy(3600)
    with pytest.raises(ValueError):
        budget.fixture_budget(large, execution_policy("twenty-four-hour"))


def test_runtime_policy_construction_uses_actual_validated_native_binding(selected):
    selection, _recipe = selected
    binding, _fixture = allocated(selection)
    seconds = binding["fixture_budget"]["fixture_run_seconds"]
    calls = []
    def factory(root, objective, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(to_dict=lambda: {"policy_id": "original", "budgets": {"seconds": 5220}})
    document = workload._run_policy_document(factory, Path("/private/tmp"), "fixture", seconds,
        execution_policy=selection["execution_policy"], allocation_binding=binding)
    assert document == {"policy_id": None, "budgets": {"seconds": seconds}}
    assert calls == [{}]
    legacy = workload._run_policy_document(factory, Path("/private/tmp"), "fixture", 5220)
    assert legacy["budgets"]["seconds"] == 5220 and calls[-1] == {"run_seconds": 5220}


def test_provision_rejects_fixture_mismatch_before_any_owner_side_effect(selected, tmp_path):
    selection, _recipe = selected
    binding, fixture = allocated(selection)
    fixture["source_material"]["shared"]["raw"] += "changed"
    with pytest.raises(ValueError, match="native_case_extended_binding_invalid"):
        workload.provision(tmp_path / "no-owner", fixture, [], native_case_plan=case_plan(),
            review_mode="selective_reuse", execution_policy=selection["execution_policy"],
            allocation_binding=binding, run_seconds=binding["fixture_budget"]["fixture_run_seconds"])
    assert not (tmp_path / "no-owner").exists()


def test_new_index_shard_binding_and_legacy_bytes_stay_separate(selected, tmp_path):
    from tests._usage_fixture import require_reference_storage
    require_reference_storage()
    selection, _recipe = selected
    report, source = case_evidence_report()
    legacy_pin = evidence.write_index(tmp_path / "legacy", report, source_before=source, source_after=source, refresh=False)
    legacy = evidence.read_index(legacy_pin, roots=[tmp_path])
    assert legacy["schema_version"] == evidence.SCHEMA and "extended_selection" not in legacy
    assert legacy["watchdog"] == case_watchdog(["unchanged-baseline"])
    report.update(extended_selection=selection, fixture_sha256=selection["fixture_sha256"],
        watchdog=budget.watchdog(selection, ["unchanged-baseline"]))
    pin = evidence.write_index(tmp_path / "extended", report, source_before=source, source_after=source, refresh=False)
    index = evidence.read_index(pin, roots=[tmp_path])
    assert index["schema_version"] == budget.INDEX and index["qualification"] is False
    assert index["authority"] == "byte_evidence_only"
    index["watchdog"]["fixture_run_seconds"] += 1
    with pytest.raises(ValueError, match="case_evidence_watchdog_binding_invalid"):
        evidence.validate_index(index)


def test_extended_profile_reconstruction_preserves_real_lease():
    from evidence_wiki import (
        DisagreementPlan,
        ExecutionProfile,
        ExecutionRole,
        ExecutionRoleRegistry,
        ExecutionRoleScope,
        NativeInputHandoff,
        ReviewDelivery,
    )
    roles = tuple(ExecutionRole(name, role, name + "-controller", "simulated/" + name, "review/v1")
        for name, role in (("generator", "host_worker"), ("evidence", "evaluator"), ("critic", "evaluator")))
    scopes = ExecutionRoleRegistry(tuple(ExecutionRoleScope(role.config_ref, task, ("model",))
        for role, task in zip(roles, ("researcher", "evaluator", "critic"), strict=True)))
    profile = ExecutionProfile("fixture-profile", 512, "EUR", "simulated-pricing/v1", roles,
        1, 1, 1000, "0", 1, role_scopes=scopes, disagreement=DisagreementPlan(), input_handoff=NativeInputHandoff(),
        review_delivery=ReviewDelivery(context_version="dependencies"),
        review_application_lease=ReviewApplicationLease(700, (datetime.now(timezone.utc) + timedelta(hours=8)).isoformat(),
            schema_version="research-review-application-lease/v2"))
    restored = _construct_profile(json.loads(json.dumps(_constructor_inputs(profile))))
    assert restored.document() == profile.document()
    assert type(restored.review_application_lease) is ReviewApplicationLease
    assert restored.document()["schema_version"] == "research-execution-profile/v17"


def test_actual_provision_constructs_selected_lease_before_owner_fixture_boundary(selected, tmp_path, monkeypatch):
    import evidence_wiki.review_application_lease as leases
    selection, _recipe = selected
    binding, fixture = allocated(selection)
    seen = []
    original = leases.ReviewApplicationLease
    def lease(*args, **kwargs):
        value = original(*args, **kwargs)
        seen.append(value)
        return value
    monkeypatch.setattr(leases, "ReviewApplicationLease", lease)
    strict_boundary = SimpleNamespace(claims_document=lambda value: value, policy_document=lambda value: value)
    original_owner = workload.owner
    monkeypatch.setattr(workload, "owner", lambda name: strict_boundary if name == "_strict_contract" else original_owner(name))
    class OwnerBoundary(Exception):
        pass
    def stop():
        raise OwnerBoundary()
    monkeypatch.setattr(workload, "_example", stop)
    with pytest.raises(OwnerBoundary):
        workload.provision(tmp_path / "fixture", fixture, [], native_case_plan=case_plan(),
            review_mode="selective_reuse", execution_policy=selection["execution_policy"],
            allocation_binding=binding, run_seconds=binding["fixture_budget"]["fixture_run_seconds"])
    assert len(seen) == 1 and seen[0].seconds == 700
    assert seen[0].not_after_utc == binding["authority_expires_at"]
    assert not (tmp_path / "fixture").exists()


def test_parent_selection_is_bound_to_single_case_children(selected, tmp_path, monkeypatch):
    from tools import _review_dependency_cases as cases
    selection, _recipe = selected
    selection = copy.deepcopy(selection)
    selection["selected_case_ids"] = ["unchanged-baseline", "independent-claim-text"]
    seen = []
    def shards(*args):
        seen.append(args)
        return args[5]
    monkeypatch.setattr(cases, "_run_shards", shards)
    report = cases.run_cases(tmp_path / "parent", selection["fixture_input"],
        cases=selection["selected_case_ids"], extended_selection=selection)
    assert seen[0][-1] == selection
    assert report["watchdog"]["shard_count"] == 2
    assert report["watchdog"]["fixture_reservations"] == 2
    assert report["watchdog"]["watchdog_seconds"] > report["watchdog"]["fixture_run_seconds"] * 2
    bad = copy.deepcopy(selection)
    bad["refresh"] = True
    with pytest.raises(AssertionError, match="native_case_extended_selection_invalid"):
        cases.run_cases(tmp_path / "invalid", selection["fixture_input"],
            cases=selection["selected_case_ids"], extended_selection=bad)


def test_legacy_watchdog_and_temporal_fixture_remain_exact():
    assert case_watchdog(["unchanged-baseline"]) == {"fixture_reservations": 1, "fixture_run_seconds": 5220,
        "fixture_overhead_seconds": 600, "cleanup_seconds": 60, "outer_overhead_seconds": 600,
        "watchdog_seconds": 6480, "authority_renewal": "forbidden", "run_budget_extension": "forbidden"}
    frozen = datetime(2026, 10, 17, tzinfo=timezone.utc)
    fixture = _fixture_inputs(fixture_document(), "unused-source-expired", frozen_at=frozen)
    assert fixture["source_material"]["unused"]["temporal"]["expires_at"]["value"] == (frozen + timedelta(minutes=30)).isoformat()


def test_actual_preflight_selector_keeps_native_and_formal_journals_separate(selected):
    selection, _recipe = selected
    binding, _fixture = allocated(selection)
    assert workload._fixture_preflight_run(binding) == "fixture-preflight"
    assert workload._fixture_preflight_run(None) == "fixture-preflight"
    assert workload._fixture_preflight_run({"assignments": [{"native_run_id": "original-formal-run"}]}) == "original-formal-run"


@pytest.mark.parametrize("expire_at", [None, "index"])
def test_parent_uses_one_run_timer_through_shard_reads_and_reserved_terminal_phases(selected, tmp_path, monkeypatch, expire_at):
    from tests._usage_fixture import require_posix_signals
    require_posix_signals()
    from tools import _review_dependency_cases as cases
    from tools.decide_release import _HostDeadline
    selection, _recipe = selected
    selection = copy.deepcopy(selection)
    selection["selected_case_ids"] = ["unchanged-baseline", "independent-claim-text"]
    report, source = case_evidence_report(selection["selected_case_ids"])
    report.update(extended_selection=selection, fixture_sha256=selection["fixture_sha256"],
        watchdog=budget.watchdog(selection, selection["selected_case_ids"]))
    directory = tmp_path / "parent-timers"
    directory.mkdir()
    originals = SimpleNamespace(pins=[])
    samples = []
    def sample(stage):
        active = workload._DEADLINES.get()
        assert len(active) == 1
        samples.append((stage, active[0]))
        return active[0]
    class Tracker:
        def __init__(self, *_args):
            self.originals = originals
        def open(self):
            sample("open")
        def confirm_completed(self):
            sample("confirm")
        def close(self):
            sample("cleanup")
            return {"status": "confirmed", "children": []}
    class Child:
        returncode = 0
        def __init__(self, command, **_kwargs):
            sample("launch")
            self.command = command
            output = Path(command[command.index("--output") + 1]) / "case-evidence"
            output.mkdir(parents=True)
            (output / "index.json").write_text("{}")
        def wait(self, timeout):
            end = sample("wait")
            assert 0 < timeout <= end - cases.time.monotonic() + 0.01
    monkeypatch.setattr(cases, "OwnedChildren", Tracker)
    monkeypatch.setattr(cases.subprocess, "Popen", Child)
    monkeypatch.setattr(evidence, "_begin_native_run", lambda *_args: object())
    monkeypatch.setattr(evidence, "_record_native_shard", lambda *_args: sample("record-shard"))
    def read_index(pin, **_kwargs):
        sample("index")
        if expire_at == "index":
            raise _HostDeadline()
        ordinal = int(Path(pin["path"]).parents[1].name.split("-")[1])
        return {"extended_selection": selection, "source": source, "refresh": False, "mode": "selective_reuse",
            "selected_case_ids": [selection["selected_case_ids"][ordinal]], "fixture_sha256": selection["fixture_sha256"],
            "cleanup_pin": {}, "status": "completed", "cases": [{"case_id": selection["selected_case_ids"][ordinal]}]}
    monkeypatch.setattr(evidence, "read_index", read_index)
    monkeypatch.setattr(evidence, "read", lambda *_args, **_kwargs: (sample("read-cleanup"), {"status": "confirmed"})[1])
    monkeypatch.setattr(evidence, "read_case", lambda *_args, **_kwargs: (sample("read-case"), {"status": "passed"})[1])
    def finish(*args, **kwargs):
        end = sample("finalization")
        assert end <= kwargs["completion_deadline"]
        assert args[1]["status"] == ("failed" if expire_at else "completed")
    monkeypatch.setattr(cases, "_finish_case_evidence", finish)
    invoke = lambda: cases._run_shards(directory, selection["fixture_input"], "selective_reuse",
        selection["selected_case_ids"], False, report, originals, source, {}, selection)
    if expire_at:
        with pytest.raises(_HostDeadline):
            invoke()
    else:
        invoke()
    run = {end for stage, end in samples if stage not in {"cleanup", "finalization"}}
    assert len(run) == 1
    terminal = {stage: end for stage, end in samples if stage in {"cleanup", "finalization"}}
    assert terminal["cleanup"] != next(iter(run)) and terminal["finalization"] != next(iter(run))
    assert not workload._DEADLINES.get()


def test_single_fixture_late_timer_exit_marks_failed_before_finalization(selected, tmp_path, monkeypatch):
    from tests._usage_fixture import require_posix_signals
    require_posix_signals()
    from contextlib import contextmanager

    from tools import _review_dependency_cases as cases
    from tools.decide_release import _HostDeadline
    selection, _recipe = selected
    seen = []
    class Tracker:
        def __init__(self, originals, *_args):
            self.originals = originals
        def open(self):
            pass
        def close(self):
            assert workload._DEADLINES.get()
            return {"status": "confirmed", "children": []}
    class Harness:
        def __init__(self, root, _runtime, _originals, _children, mode, *_args):
            self.root, self.mode = root, mode
        def setup_residual(self):
            pass
        setup_usage_role = setup_residual
        setup_pending_source = setup_residual
        establish = setup_residual
        def run_case(self, *_args, **_kwargs):
            return {"status": "passed"}
    monkeypatch.setattr(cases, "OwnedChildren", Tracker)
    monkeypatch.setattr(cases, "NativeCases", Harness)
    monkeypatch.setattr(cases, "provision", lambda base, *_args, **_kwargs: (base / "workspace", object(), {}, {}, {}))
    monkeypatch.setattr(evidence, "_begin_native_run", lambda *_args: object())
    monkeypatch.setattr(evidence, "_record_native_outcome", lambda *_args: None)
    original_phase = cases._case_phase
    phases = []
    @contextmanager
    def late(end, allowance=None):
        phases.append((end, allowance))
        with original_phase(end, allowance):
            yield
            if len(phases) == 1:
                raise _HostDeadline()
    monkeypatch.setattr(cases, "_case_phase", late)
    monkeypatch.setattr(cases, "_finish_case_evidence", lambda *args, **_kwargs: seen.append(copy.deepcopy(args[1])))
    with pytest.raises(_HostDeadline):
        cases.run_cases(tmp_path / "late-exit", selection["fixture_input"],
            cases=selection["selected_case_ids"], extended_selection=selection)
    assert len(phases) == 3 and phases[1][0] < phases[2][0]
    assert phases[1][0] <= phases[0][0] + selection["phase_caps"]["cleanup"] + 60
    assert seen[0]["status"] == "failed" and seen[0]["failure"]["kind"] == "_HostDeadline"
    assert not workload._DEADLINES.get()


def test_expired_run_cannot_restart_and_terminal_timeout_drops_live_completion(selected, tmp_path, monkeypatch):
    from tests._usage_fixture import require_posix_signals
    require_posix_signals()
    from tools import _review_dependency_cases as cases
    from tools.decide_release import _HostDeadline
    selection, _recipe = selected
    report = {"status": "completed", "watchdog": budget.watchdog(selection, selection["selected_case_ids"])}
    tracker = SimpleNamespace(originals=SimpleNamespace(pins=[]), _native_case_completion=object())
    with pytest.raises(_HostDeadline):
        with cases._case_phase(cases.time.monotonic() - 1):
            pytest.fail("expired phase was restarted")
    tracker.close = lambda: {"status": "confirmed", "children": []}
    def expired_finish(*_args, **_kwargs):
        raise _HostDeadline()
    monkeypatch.setattr(cases, "_finish_case_evidence", expired_finish)
    with pytest.raises(_HostDeadline):
        cases._finish_extended_cases(tmp_path, report, tracker, {}, False, {}, object(),
            cases._case_deadlines(report["watchdog"]))
    assert report["status"] == "failed" and not hasattr(tracker, "_native_case_completion")


@pytest.mark.parametrize("mutation", [None, "foreign-selection", "multi-case-child", "legacy"])
def test_original_retention_traverses_99_single_case_extended_shards(selected, monkeypatch, mutation):
    from tools._review_dependency_case_contracts import CASE_IDS
    selection, _recipe = selected
    source = selection["source"]
    indices, documents, manifests = {}, {}, {}
    def make(name, chosen, shards):
        prefix = "/private/tmp/pure-retention/" + name
        cleanup_pin, original_pin, shard_pin = prefix + "/cleanup", prefix + "/originals", prefix + "/shards"
        spawn = {"path": prefix + "/0-child-1-spawn.json"}
        worker = {"path": prefix + "/1-native-case-worker-manifest.json"}
        plan = {"path": prefix + "/plan"}
        documents[cleanup_pin] = {"status": "confirmed", "children": [{"pid": 123, "leader_reaped": True, "group_absent": True}], "plan_sha256": "p"}
        documents[spawn["path"]] = {"plan_sha256": "p", "pid": 123, "pgid": 123}
        documents[worker["path"]] = {"worker_sha256": source["worker_sha256"], "case_plan": plan}
        documents[plan["path"]] = case_plan()
        manifests[original_pin], manifests[shard_pin] = [spawn, worker], shards
        return {"schema_version": budget.INDEX, "source": source, "fixture_sha256": selection["fixture_sha256"],
            "mode": "selective_reuse", "refresh": False, "status": "completed", "extended_selection": selection,
            "selected_case_ids": list(chosen), "cases": [], "cleanup_pin": {"path": cleanup_pin},
            "originals_manifest_pin": {"path": original_pin}, "shards_manifest_pin": {"path": shard_pin}}
    shard_pins = []
    for ordinal, case in enumerate(CASE_IDS):
        pin = {"path": "/private/tmp/pure-retention/index-" + str(ordinal)}
        indices[pin["path"]] = make("child-" + str(ordinal), [case], [])
        shard_pins.append(pin)
    index = make("parent", CASE_IDS, shard_pins)
    if mutation == "foreign-selection":
        indices[shard_pins[-1]["path"]]["extended_selection"] = dict(selection, refresh=True)
    elif mutation == "multi-case-child":
        indices[shard_pins[-1]["path"]]["selected_case_ids"] *= 2
    elif mutation == "legacy":
        index["schema_version"] = evidence.SCHEMA
    monkeypatch.setattr(evidence, "read", lambda pin, **_kwargs: documents[pin["path"]])
    monkeypatch.setattr(evidence, "read_manifest", lambda pin, **_kwargs: manifests[pin["path"]])
    monkeypatch.setattr(evidence, "read_index", lambda pin, **_kwargs: indices[pin["path"]])
    if mutation:
        with pytest.raises(ValueError, match="case_evidence_shard_(binding_invalid|bound)"):
            evidence._verify_native_retention(index, roots=[], source=source)
    else:
        evidence._verify_native_retention(index, roots=[], source=source)


def test_cleanup_reserves_existing_60_seconds_without_changing_measured_caps(selected):
    selection, recipe = selected
    reserved = budget.fixture_budget(recipe, selection["execution_policy"])
    assert recipe["phase_caps"]["cleanup"] == 30
    assert reserved["cleanup_seconds"] == 60
    near_limit = copy.deepcopy(recipe)
    near_limit["group_overhead_cap_seconds"] += (selection["execution_policy"]["authority_seconds"]
        - reserved["fixture_run_seconds"] - reserved["fixture_overhead_seconds"] - 59)
    with pytest.raises(ValueError, match="native_case_extended_binding_invalid"):
        budget.fixture_budget(near_limit, selection["execution_policy"])


@pytest.mark.parametrize("unblocked_receiver", [False, True])
def test_pending_timeout_finishes_finite_owned_cleanup_then_retains_unqualified_overrun(selected, tmp_path, monkeypatch, unblocked_receiver):
    from tests._usage_fixture import require_posix_signals
    require_posix_signals()
    import signal
    import threading
    import time

    from tools import _review_dependency_cases as cases
    from tools.decide_release import _HostDeadline
    selection, _recipe = selected
    report = {"status": "completed", "watchdog": budget.watchdog(selection, selection["selected_case_ids"])}
    completed, observed = [], []
    original_handler = signal.getsignal(signal.SIGALRM)
    original_timer = signal.getitimer(signal.ITIMER_REAL)
    original_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    def close():
        assert signal.SIGALRM in signal.pthread_sigmask(signal.SIG_BLOCK, set())
        time.sleep(0.02)
        completed.append(True)
        return {"status": "confirmed", "children": []}
    tracker = SimpleNamespace(originals=SimpleNamespace(pins=[]), close=close, _native_case_completion=object())
    def finish(*args, **_kwargs):
        assert completed == [True]
        observed.append(copy.deepcopy(args[1]))
    monkeypatch.setattr(cases, "_finish_case_evidence", finish)
    ready, stop = threading.Event(), threading.Event()
    def receive():
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGALRM})
        ready.set()
        stop.wait()
    receiver = threading.Thread(target=receive) if unblocked_receiver else None
    try:
        if receiver is not None:
            receiver.start()
            assert ready.wait(1)
        now = time.monotonic()
        with pytest.raises(_HostDeadline):
            cases._finish_extended_cases(tmp_path, report, tracker, {}, False, {}, object(),
                (now - 1, now + 0.01, now + 1))
    finally:
        stop.set()
        if receiver is not None:
            receiver.join(1)
            assert not receiver.is_alive()
    assert completed == [True] and observed[0]["status"] == "failed"
    assert report["cleanup"]["status"] == "confirmed"
    assert report["cleanup_timing"]["deadline_exceeded"] is True
    retained = json.loads((tmp_path / "report.json").read_bytes())
    assert retained["status"] == "failed" and retained["cleanup_timing"]["deadline_exceeded"] is True
    assert not hasattr(tracker, "_native_case_completion")
    assert not workload._DEADLINES.get()
    assert signal.getsignal(signal.SIGALRM) is original_handler
    assert signal.getitimer(signal.ITIMER_REAL) == original_timer
    assert signal.pthread_sigmask(signal.SIG_BLOCK, set()) == original_mask


def test_already_expired_terminal_phase_still_closes_owned_children_once(selected, tmp_path, monkeypatch):
    from tests._usage_fixture import require_posix_signals
    require_posix_signals()
    import signal
    import time

    from tools import _review_dependency_cases as cases
    from tools.decide_release import _HostDeadline
    selection, _recipe = selected
    report = {"status": "completed", "watchdog": budget.watchdog(selection, selection["selected_case_ids"])}
    closed = []
    def close():
        assert signal.SIGALRM in signal.pthread_sigmask(signal.SIG_BLOCK, set())
        closed.append(True)
        return {"status": "confirmed", "children": []}
    tracker = SimpleNamespace(originals=SimpleNamespace(pins=[]), close=close, _native_case_completion=object())
    monkeypatch.setattr(cases, "_finish_case_evidence", lambda *_args, **_kwargs: pytest.fail("expired finalization was reopened"))
    now = time.monotonic()
    with pytest.raises(_HostDeadline):
        cases._finish_extended_cases(tmp_path, report, tracker, {}, False, {}, object(),
            (now - 3, now - 2, now - 1))
    assert closed == [True] and report["status"] == "failed"
    assert report["cleanup"]["status"] == "confirmed" and report["cleanup_timing"]["deadline_exceeded"] is True
    retained = json.loads((tmp_path / "report.json").read_bytes())
    assert retained["status"] == "failed" and retained["cleanup"]["status"] == "confirmed"
    assert not hasattr(tracker, "_native_case_completion") and not workload._DEADLINES.get()


def test_finalization_timeout_unpublishes_completed_summary_and_preserves_originals(selected, tmp_path, monkeypatch):
    from tests._usage_fixture import require_posix_signals
    require_posix_signals()
    from tools import _review_dependency_cases as cases
    from tools.decide_release import _HostDeadline
    selection, _recipe = selected
    report = {"status": "completed", "watchdog": budget.watchdog(selection, selection["selected_case_ids"])}
    original = tmp_path / "original-ledger.json"
    original.write_bytes(b'{"original":"unaltered"}\n')
    original_pin = workload.retain(tmp_path / "original-response.json", {"retained": True})
    tracker = SimpleNamespace(originals=SimpleNamespace(pins=[original_pin]),
        close=lambda: {"status": "confirmed", "children": []})
    def partial_finalizer(directory, actual_report, children, *_args, **_kwargs):
        index = directory / "case-evidence/index.json"
        index.parent.mkdir()
        index.write_text('{"status":"completed","qualification":false}')
        actual_report["case_results_index_pin"] = {"path": str(index)}
        (directory / "report.json").write_text(json.dumps(actual_report))
        children._native_case_completion = object()
        raise _HostDeadline()
    monkeypatch.setattr(cases, "_finish_case_evidence", partial_finalizer)
    with pytest.raises(_HostDeadline):
        cases._finish_extended_cases(tmp_path, report, tracker, {}, False, {}, object(),
            cases._case_deadlines(report["watchdog"]))
    retained = json.loads((tmp_path / "report.json").read_bytes())
    assert retained["status"] == "failed" and retained["cleanup"]["status"] == "confirmed"
    assert "case_results_index_pin" not in retained
    assert retained["originals"] == [original_pin]
    assert not (tmp_path / "case-evidence/index.json").exists()
    assert original.read_bytes() == b'{"original":"unaltered"}\n'
    assert evidence.read(original_pin, roots=[tmp_path]) == {"retained": True}
    assert not hasattr(tracker, "_native_case_completion") and not workload._DEADLINES.get()


def test_oversized_terminal_report_uses_bounded_failed_summary_after_unpublishing(selected, tmp_path):
    from tests._usage_fixture import require_posix_signals
    require_posix_signals()
    from tools import _review_dependency_cases as cases
    selection, _recipe = selected
    index = tmp_path / "case-evidence/index.json"
    index.parent.mkdir()
    index.write_text('{"status":"completed"}')
    (tmp_path / "report.json").write_text('{"status":"completed"}')
    original = tmp_path / "original-ledger.json"
    original.write_bytes(b"original-retained-bytes\n")
    report = {"status": "failed", "failure": {"kind": "_HostDeadline", "reason": None, "error_code": None},
        "watchdog": budget.watchdog(selection, selection["selected_case_ids"]), "case_results_index_pin": {"path": str(index)},
        "cleanup": {"status": "confirmed", "shards": ["x" * 16_777_217]},
        "cleanup_timing": {"reserved_seconds": 60, "elapsed_seconds": 61.5, "deadline_exceeded": True}}
    tracker = SimpleNamespace(originals=SimpleNamespace(pins=[]), _native_case_completion=object())
    cases._retain_case_failure(tmp_path, report, tracker)
    raw = (tmp_path / "report.json").read_bytes()
    retained = json.loads(raw)
    assert len(raw) <= 65_536 and retained["status"] == "failed" and retained["qualification"] is False
    assert retained["retention_status"] == "incomplete_inventory"
    assert retained["original_responses"]["inventory_complete"] is False
    assert retained["cleanup_timing"]["deadline_exceeded"] is True
    assert "case_results_index_pin" not in retained and not index.exists()
    assert original.read_bytes() == b"original-retained-bytes\n" and not hasattr(tracker, "_native_case_completion")
