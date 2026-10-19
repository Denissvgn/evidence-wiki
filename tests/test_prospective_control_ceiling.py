"""Finite prospective control ceilings preserve historical admission boundaries."""

import copy
import hashlib
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests._native_application_profile_fixture import native_application_profile
from tests.test_review_dependency_workload import plan as plan
from tests.test_selective_review_workload import cohort as cohort
from tests.test_selective_review_workload import pin as pin
from tools import _review_control_ceiling as ceiling
from tools import _review_dependency_accounting as accounting
from tools import _review_dependency_case_budget as budget
from tools import _review_dependency_case_evidence as evidence
from tools import _review_dependency_pilot as pilot
from tools import _review_dependency_runner as runner
from tools import run_review_dependency_workload as workload
from tools._execution_budget import execution_policy, provisioning_window
from tools._observation_freshness import observation_policy
from tools._review_dependency_workload import digest, retain
from tools._review_journal_artifacts import POLICY as COLLECTION


@pytest.fixture(autouse=True)
def pure_only(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("native_process_forbidden")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    root = Path(__file__).resolve().parents[1]
    for module in (ceiling, accounting, budget, evidence, pilot, runner, workload):
        assert Path(module.__file__).resolve().is_relative_to(root / "tools")


def test_complete_historical_schema_export_bundles_are_frozen():
    exported = accounting.schema_documents()
    old = {name: exported[name] for name in accounting._PRE_LONG_SCHEMA_DEFS}
    assert len(old) == 49
    assert hashlib.sha256(accounting.canonical(old)).hexdigest() == "d22ddc0674c528e834dd5e1ef31abd11c144f30cbe7be5c1a2b0bfa53d6ffed1"


@pytest.mark.parametrize("value", [True, 3600.0, 3601])
def test_closed_policy_rejects_scalar_substitution_even_with_fresh_digest(value):
    selected = ceiling.binding()
    selected["phase_ceiling_policy"]["discovery_phase_caps"]["mutation"] = value
    selected["phase_ceiling_policy_sha256"] = digest(selected["phase_ceiling_policy"])
    with pytest.raises(ValueError, match="phase_ceiling_policy_invalid"):
        ceiling.validate(selected, required=True)


def test_discovery_has_exact_finite_authority_and_unchanged_worker_recency():
    service = workload.service_policy(ceiling.PROFILE)
    observer, execution = observation_policy("batch"), execution_policy("sixteen-hour")
    caps = workload.observational_phase_ceilings(service, observer, execution)
    assert caps == {"setup": 900, "job": 3600, "mutation": 3600, "owner_read": 1200, "global_close": 1500, "cleanup": 60}
    assert workload.allocation_seconds(caps, 6) == 30060 < execution["authority_seconds"] == 57600
    limits = workload.runtime_hard_limits(observer, execution, phase_caps=caps, ceiling_binding=ceiling.binding())
    assert limits["native_worker_seconds"] == 600
    assert limits["service_application_seconds"] == observer["max_age_seconds"] == 3600
    with pytest.raises(ValueError):
        workload.runtime_hard_limits(observer, execution, phase_caps=caps)
    for authority in ("eight-hour", "twenty-four-hour"):
        with pytest.raises(ValueError):
            workload.observational_phase_ceilings(service, observer, execution_policy(authority))
    for field, value in (("setup_seconds", 900.0), ("mutation_seconds", True), ("service_seconds", 3600.0)):
        with pytest.raises(ValueError):
            workload.observational_phase_ceilings({**service, field: value}, observer, execution)
    for invalid_observer, invalid_execution in (({"profile": "batch"}, execution),
            (observer, {"profile": "sixteen-hour"}),
            ({**observer, "max_age_seconds": 3600.0}, execution),
            (observer, {**execution, "schema_version": "review-execution-budget-policy/v1"}),
            (observer, {**execution, "authority_seconds": 57600.0})):
        with pytest.raises(ValueError):
            workload.observational_phase_ceilings(service, invalid_observer, invalid_execution)
    assert workload.formal_phase_ceilings(observer, execution)["mutation"] == 1200
    assert workload.formal_phase_ceilings(observer, execution, ceiling_binding=ceiling.binding())["mutation"] == 4500
    for invalid in (None, observation_policy("interactive")):
        with pytest.raises(ValueError):
            workload.formal_phase_ceilings(invalid, execution, ceiling_binding=ceiling.binding())


def long_envelope(plan):
    bundle = Path(plan["artifacts"]["fresh-fixture-input.json"]["path"]).parent
    return pilot.reference_plan(bundle, execution_profile="sixteen-hour", service_profile=ceiling.PROFILE, journal_collection=True)


def test_current_and_historical_native_shape_reject_crossed_profiles_and_bindings(plan):
    selected = long_envelope(plan)
    assert selected["schema_version"] == pilot.LONG_PLAN
    assert workload.report_schema(selected["native_plan"]) == workload.RESULT_SCHEMA_LONG
    assert pilot.validate_plan(selected, digest(selected)) == selected
    historical = copy.deepcopy(selected)
    begin = datetime(2020, 1, 1, tzinfo=timezone.utc)
    historical["native_plan"].update(frozen_at=begin.isoformat(), expires_at=(begin + timedelta(hours=16)).isoformat())
    historical["native_plan_sha256"] = digest(historical["native_plan"])
    assert pilot.validate_plan(historical, digest(historical), current=False) == historical
    for change in ("missing", "retag", "native_retag", "float_cap", "field", "policy_hash", "old_service"):
        invalid = copy.deepcopy(selected)
        if change == "missing":
            del invalid["native_plan"]["phase_ceiling_policy"]
        elif change == "retag":
            invalid["schema_version"] = pilot.COLLECTION_OBSERVATIONAL_PLAN
        elif change == "native_retag":
            invalid["native_plan"]["schema_version"] = workload.PLAN_SCHEMA_OBSERVATIONAL_COLLECTION
        elif change == "float_cap":
            invalid["native_plan"]["phase_caps"]["mutation"] = 3600.0
        elif change == "field":
            invalid["native_plan"]["extra"] = True
        elif change == "policy_hash":
            invalid["phase_ceiling_policy_sha256"] = "f" * 64
        else:
            invalid["native_plan"]["service_policy"] = workload.service_policy("control-observational")
        invalid["native_plan_sha256"] = digest(invalid["native_plan"])
        with pytest.raises(ValueError):
            pilot.validate_plan(invalid, digest(invalid), current=False)
    bundle = Path(plan["artifacts"]["fresh-fixture-input.json"]["path"]).parent
    with pytest.raises(ValueError):
        pilot.reference_plan(bundle, execution_profile="sixteen-hour", service_profile=ceiling.PROFILE)


@pytest.fixture
def completed(plan, monkeypatch):
    selected = long_envelope(plan)
    report = {"closing_current_results": {"strict": {"review_producer_id": "sha256:" + "b" * 64}}}
    worst = {"setup": 10, "mutation": 3600, "job": 100, "owner_read": 100, "cleanup": 10, "global_close": 100}
    monkeypatch.setattr(workload, "read_pinned_artifact", lambda _: (selected, b"original"))
    monkeypatch.setattr(pilot, "_originals", lambda *_: (selected, report, worst, 12))
    return selected, worst


def test_recipe_derives_headroom_from_complete_originals_and_rejects_changes(completed, monkeypatch):
    selected, worst = completed
    recipe = pilot.extended_measurement_recipe({}, {}, execution_profile="sixteen-hour")
    assert recipe["schema_version"] == pilot.LONG_RECIPE and recipe["phase_caps"]["mutation"] == 4500
    assert ceiling.validate(recipe, required=True) == ceiling.binding()
    assert pilot.completed_extended_pilot_measurements(recipe, selected["candidate_sha256"]) == (worst, 12)
    summary = pilot.observational_measurements({}, {})
    assert summary["schema_version"] == pilot.LONG_MEASUREMENT
    assert summary["formal_phase_ceilings"]["mutation"] == 4500
    for key, value in (("schema_version", pilot.COLLECTION_EXTENDED_RECIPE), ("phase_ceiling_policy_sha256", "0" * 64),
                       ("candidate_sha256", "f" * 64), ("phase_caps", {**recipe["phase_caps"], "mutation": 4500.0})):
        with pytest.raises(ValueError):
            pilot.completed_extended_pilot_measurements({**recipe, key: value}, selected["candidate_sha256"])
    worst["mutation"] = 3601
    with pytest.raises(ValueError, match="selective_pilot_allocation_invalid"):
        pilot.extended_measurement_recipe({}, {}, execution_profile="sixteen-hour")
    def incomplete(*_):
        raise ValueError("selective_pilot_report_binding_invalid")
    monkeypatch.setattr(pilot, "_originals", incomplete)
    with pytest.raises(ValueError, match="selective_pilot_report_binding_invalid"):
        pilot.extended_measurement_recipe({}, {}, execution_profile="sixteen-hour")


def long_formal(cohort):
    from tests.test_review_journal_artifacts import collection_formal_plan
    plan, pair = collection_formal_plan(cohort)
    caps = {**plan["phase_caps"], "mutation": 4500}
    plan.update(ceiling.binding(), schema_version=accounting.LONG_PLAN, phase_caps=caps,
        elapsed_cap_seconds=workload.allocation_seconds(caps, 24) + 2 * caps["global_close"])
    return plan, pair


def test_formal_schema_semantics_report_dispatch_and_allocation_bindings(cohort, monkeypatch):
    selected, pair = long_formal(cohort)
    assert accounting.validate_plan(selected, pair) == selected
    report = accounting.initialize_report(selected, selected["pair_spec_pin"], pair)
    assert report["schema_version"] == accounting.LONG_REPORT
    assert accounting.decode("Report", accounting.canonical(report)) == report
    assert accounting.validate_report(report, selected) == report
    monkeypatch.setattr(runner, "_read", lambda _: {"host_id": "direct"})
    binding = runner._allocation_binding(selected, [])
    assert ceiling.validate(binding, required=True) == ceiling.binding()
    assert binding["formal_plan"] == selected and binding["hard_limits"]["native_worker_seconds"] == 600
    for change in ("retag", "cap", "bool", "missing", "hash", "fit"):
        invalid = copy.deepcopy(selected)
        if change == "retag": invalid["schema_version"] = accounting.COLLECTION_PLAN
        elif change == "cap": invalid["phase_caps"]["mutation"] = 4501
        elif change == "bool": invalid["phase_ceiling_policy"]["formal_phase_ceilings"]["cleanup"] = True
        elif change == "missing": del invalid["phase_ceiling_policy"]
        elif change == "hash": invalid["phase_ceiling_policy_sha256"] = "0" * 64
        else: invalid["elapsed_cap_seconds"] += 1
        with pytest.raises(ValueError): accounting.validate_plan(invalid)
    with pytest.raises(ValueError):
        accounting.validate_report({**report, "schema_version": accounting.COLLECTION_REPORT}, selected)


def test_native_case_selection_allocation_provisioning_and_index_routes(tmp_path, monkeypatch):
    from tests._review_dependency_fixture import case_evidence_report
    from tests.test_native_case_extended_policy import allocated
    from tests.test_native_case_extended_policy import selected as old_fixture
    selection, recipe = old_fixture.__wrapped__(tmp_path, monkeypatch)
    recipe.update(ceiling.binding(), schema_version=pilot.LONG_RECIPE, journal_collection_policy=copy.deepcopy(COLLECTION))
    recipe["phase_caps"]["mutation"] = 4500
    pin = retain(tmp_path / "prospective-recipe.json", recipe)
    value = budget.select(pin, "twenty-four-hour", selection["fixture_input"], "selective_reuse", ["unchanged-baseline"], False, journal_collection=True)
    assert value["schema_version"] == budget.LONG_SELECTION
    binding, fixture = allocated(value)
    assert binding["schema_version"] == budget.LONG_ALLOCATION
    seconds = binding["fixture_budget"]["fixture_run_seconds"]
    assert provisioning_window(value["execution_policy"], binding, seconds)[1] == binding["authority_expires_at"]
    assert ceiling.validate(binding, required=True) == ceiling.binding()
    report, source = case_evidence_report()
    report.update(extended_selection=value, fixture_sha256=value["fixture_sha256"], watchdog=budget.watchdog(value, ["unchanged-baseline"]))
    output = evidence.write_index(tmp_path / "prospective-index", report, source_before=source, source_after=source, refresh=False)
    index = evidence.read_index(output, roots=[tmp_path])
    assert index["schema_version"] == budget.LONG_INDEX
    assert evidence.validate_index(index) == index
    for field in ("phase_ceiling_policy", "phase_ceiling_policy_sha256"):
        invalid = copy.deepcopy(value); del invalid[field]
        with pytest.raises(ValueError): budget.validate_selection(invalid)
    invalid = copy.deepcopy(index); invalid["schema_version"] = budget.COLLECTION_INDEX
    with pytest.raises(ValueError): evidence.validate_index(invalid)
    invalid = copy.deepcopy(binding); invalid["schema_version"] = budget.COLLECTION_ALLOCATION
    with pytest.raises(ValueError): provisioning_window(value["execution_policy"], invalid, seconds)
    invalid = copy.deepcopy(recipe); del invalid["journal_collection_policy"]
    with pytest.raises(ValueError): budget.fixture_budget(invalid, execution_policy("twenty-four-hour"))
    invalid = copy.deepcopy(recipe); invalid["phase_caps"]["job"] = 3600
    invalid["formal_application_policy"] = workload.formal_application_policy(3600)
    with pytest.raises(ValueError): budget.fixture_budget(invalid, execution_policy("sixteen-hour"))


def retained_complete_costs(plan, tmp_path):
    """Build inert signed cost/custody records; this conveys no native execution."""
    from tests.test_review_dependency_workload import measurement_recipe
    from tools._execution_budget import credential_expiries
    from tools._review_dependency_workload import OriginalResponses, assignment_groups
    from tools._review_journal_artifacts import RECEIPT, read_collection

    envelope = long_envelope(plan)
    native = envelope["native_plan"]
    directory = tmp_path / "complete-costs"
    _, report = measurement_recipe(native, directory)
    originals = OriginalResponses(directory / "additional", journal_collection=True)
    def save(label, value):
        originals.append((label, value))
        return originals.pins[-1]
    report.update(ceiling.binding(), schema_version=workload.RESULT_SCHEMA_LONG, mode="selective",
        **{key: native[key] for key in ("service_policy", "execution_policy", "execution_policy_sha256", "observation_policy", "observation_policy_sha256", "hard_limits", "source_protocol_limits", "journal_collection_policy")})
    for timing in report["job_elapsed"]:
        timing["cap_seconds"] = native["phase_caps"]["job"]
        timing["original_timing_pin"] = save("job-timing", {key: value for key, value in timing.items() if key != "original_timing_pin"})
    groups = assignment_groups(native["assignments"])
    for phase in report["phase_elapsed"]:
        label = phase["phase"]
        phase["cap_seconds"] = 2 * native["phase_caps"]["job"] if label in groups else native["phase_caps"]["owner_read" if label.startswith("owner-read") else label.replace("-", "_")]
    report["phase_elapsed"][next(i for i, phase in enumerate(report["phase_elapsed"]) if phase["phase"] == "mutation")]["elapsed_seconds"] = 3500
    for receipt in report["journal_receipts"]:
        _, metadata = read_collection(receipt["original_pin"])
        receipt.update(schema_version=RECEIPT, source_sha256="a" * 64, state_sha256="b" * 64, **{key: metadata[key] for key in ("record_count", "physical_nodes", "expanded_bytes", "artifact_kind", "page_pins")})
    control_pin = originals.append_records("control-authenticated", [])
    _, metadata = read_collection(control_pin)
    report["journal_receipts"].append({"schema_version": RECEIPT, "run_id": native["control_run_id"],
        "head_ref": None, "source_sha256": "a" * 64, "state_sha256": "b" * 64, "original_pin": control_pin,
        **{key: metadata[key] for key in ("records_sha256", "records_bytes", "record_count", "physical_nodes", "expanded_bytes", "artifact_kind", "page_pins")}})
    report["control"].update(attempt_id="control", dispatch_original_pin=save("control", {"attempt_id": "control", "before": [], "after": []}))
    closing_pins = []
    rounds = [*report["owner_reads"], copy.deepcopy(report["owner_reads"][-1])]
    for index, observed in enumerate(rounds):
        observed["journal_receipts"] = copy.deepcopy(report["journal_receipts"])
        observed.update(observation_policy=native["observation_policy"], observation_policy_sha256=native["observation_policy_sha256"], native_current=[])
        observed["strict"]["review_producer_id"] = "sha256:" + "b" * 64
        observed["observer_recency_not_after"] = (datetime.fromisoformat(observed["native_host_witnesses"][0]["observed_utc"]) + timedelta(seconds=3600)).isoformat()
        observed["native_host_witnesses"][0]["run_id"] = "owner"
        for label, value in (("owner-strict-current", observed["strict"]), ("owner-publication-current", observed["publication"]), ("owner-native-host-qualification", observed["native_host_witnesses"][0])):
            pin = save(label, value)
            if index == 2: closing_pins.append(pin)
    report.update(current_gate_results={"owner_read_index": 1}, closing_current_results=rounds[-1],
        closing_current_snapshots=[{"snapshot": "inert"}], closing_current_source_manifest={"source": "inert"},
        closing_current_elapsed_seconds=1, closing_current_original_pins=closing_pins)
    save("closing-current-snapshots", report["closing_current_snapshots"])
    save("closing-current-source-manifest", report["closing_current_source_manifest"])
    expiry = native["expires_at"]
    save("qualifications", [{"expires_utc": expiry}, {"expires_utc": expiry}])
    save("source-usage", [{"command": {"authentication": {"expires_at": expiry}, "payload": {"action": "read", "body": {}}}}])
    binding = {**ceiling.binding(), **{key: native[key] for key in ("candidate_sha256", "phase_caps", "hard_limits", "source_protocol_limits", "execution_policy", "execution_policy_sha256", "observation_policy", "observation_policy_sha256", "service_policy", "journal_collection_policy", "assignments", "group_overhead_cap_seconds", "control_run_id")},
        "plan_sha256": digest(native), "authority_frozen_at": native["frozen_at"], "authority_expires_at": expiry,
        "service_selection": {"study": "pilot", "host": "direct", "case_id": "independent-claim-text", "mode": "selective"}}
    lease = {**native["service_policy"]["application_lease"], "not_after_utc": expiry}
    profile_owner = native_application_profile(lease)
    profile = profile_owner.document()
    save("runtime-allocation", {"schema_version": "review-workload-runtime-allocation/v2", "allocation_binding": binding,
        "profile": profile, "review_application_binding": {"profile_sha256": profile_owner.sha256, "lease": lease},
        "execution_policy": native["execution_policy"], "execution_policy_sha256": native["execution_policy_sha256"],
        "credential_expiries": credential_expiries(expiry), "authority_start": native["frozen_at"], "authority_end": expiry,
        "issued_before_worker_outputs": native["frozen_at"], "run_policy": {"budgets": {"seconds": native["elapsed_cap_seconds"]}}})
    report["original_response_pins"].extend(originals.pins)
    retain(directory / "native-pilot-plan.json", native)
    native_pin = retain(directory / "complete-native-report.json", report)
    envelope_pin = retain(directory / "envelope.json", envelope)
    outer = {**ceiling.binding(), "schema_version": pilot.LONG_REPORT, "plan_sha256": digest(envelope),
        "candidate_sha256": envelope["candidate_sha256"], "native_report_pin": native_pin, "mode": "selective_reuse",
        "terminal_outcome": "pilot_completed_unqualified", "pair_qualified": False}
    return envelope_pin, retain(directory / "envelope-report.json", outer), report, outer


def test_actual_completed_reader_and_incomplete_original_guards(plan, tmp_path, monkeypatch):
    envelope_pin, report_pin, native, outer = retained_complete_costs(plan, tmp_path)
    recipe = pilot.extended_measurement_recipe(envelope_pin, report_pin, execution_profile="sixteen-hour")
    assert recipe["phase_caps"]["mutation"] == 4375
    assert pilot.completed_extended_pilot_measurements(recipe, plan["candidate_sha256"])[0]["mutation"] == 3500
    bundle = Path(plan["artifacts"]["fresh-fixture-input.json"]["path"]).parent
    recipe_pin = retain(tmp_path / "complete-recipe.json", recipe)
    full = workload.reference_plan(bundle, study="full", measured_recipe=recipe_pin["path"],
        measurement_reader=pilot.LONG_RECIPE, observation_profile="batch", execution_profile="sixteen-hour", journal_collection=True)
    assert full["schema_version"] == workload.PLAN_SCHEMA_LONG_FORMAL
    assert ceiling.validate(full, required=True) == ceiling.binding()
    excessive = {**recipe, "group_overhead_cap_seconds": 10000}
    excessive_pin = retain(tmp_path / "excessive-recipe.json", excessive)
    with pytest.raises(ValueError, match="workload_allocation_exceeds_authority"):
        workload.reference_plan(bundle, study="full", measured_recipe=excessive_pin["path"],
            measurement_reader=pilot.LONG_RECIPE, observation_profile="batch", execution_profile="sixteen-hour", journal_collection=True)
    for index, change in enumerate(({"terminal_outcome": "failed"}, {"applied_jobs": 4}, {"closing_current_results": None},
                                    {"cleanup": {"status": "unavailable"}}, {"failure": {"kind": "TimeoutError"}})):
        invalid = {**native, **change}
        pin = retain(tmp_path / ("incomplete-native-" + str(index) + ".json"), invalid)
        # Native-plan custody lives next to each original native report.
        if index == 0: retain(tmp_path / "native-pilot-plan.json", workload.read_pinned_artifact(envelope_pin)[0]["native_plan"])
        outer_pin = retain(tmp_path / ("incomplete-envelope-" + str(index) + ".json"), {**outer, "native_report_pin": pin})
        with pytest.raises(ValueError):
            pilot.extended_measurement_recipe(envelope_pin, outer_pin, execution_profile="sixteen-hour")
    with pytest.raises(ValueError):
        pilot.completed_extended_pilot_measurements(recipe, "f" * 64)
    monkeypatch.setattr(workload, "candidate_manifest", lambda _: {"changed": "candidate"})
    with pytest.raises(ValueError):
        pilot.completed_extended_pilot_measurements(recipe, plan["candidate_sha256"])


def test_discovery_provisioning_rejects_policy_and_scalar_crossings(plan, tmp_path):
    from tools._execution_budget import validate_runtime_allocation
    envelope_pin, _, native_report, _ = retained_complete_costs(plan, tmp_path)
    native_plan = workload.read_pinned_artifact(envelope_pin)[0]["native_plan"]
    allocation = workload.read_pinned_artifact(next(pin for pin in native_report["original_response_pins"] if pin["label"] == "runtime-allocation"))[0]
    binding = allocation["allocation_binding"]
    seconds = native_plan["elapsed_cap_seconds"]
    assert provisioning_window(native_plan["execution_policy"], binding, seconds)[1] == native_plan["expires_at"]
    assert validate_runtime_allocation(allocation, native_plan) == allocation
    for change in ("missing", "hash", "service", "cap", "limits", "collection"):
        invalid = copy.deepcopy(binding)
        if change == "missing": del invalid["phase_ceiling_policy"]
        elif change == "hash": invalid["phase_ceiling_policy_sha256"] = "f" * 64
        elif change == "service": invalid["service_policy"] = workload.service_policy("control-observational")
        elif change == "cap": invalid["phase_caps"]["mutation"] = 3600.0
        elif change == "limits": invalid["hard_limits"]["native_worker_seconds"] = 600.0
        else: invalid["journal_collection_policy"]["page_bytes"] = 1048576.0
        with pytest.raises(ValueError): provisioning_window(native_plan["execution_policy"], invalid, seconds)
    for change in ("workload_hash", "profile_schema", "profile_tamper", "lease_seconds", "lease_expiry"):
        invalid = copy.deepcopy(allocation)
        if change == "workload_hash":
            invalid["review_application_binding"]["profile_sha256"] = digest(invalid["profile"])
        elif change == "profile_schema":
            invalid["profile"]["schema_version"] = "research-execution-profile/v18"
        elif change == "profile_tamper":
            invalid["profile"]["profile_ref"] += "-changed"
        elif change == "lease_seconds":
            invalid["profile"]["review_application_lease"]["seconds"] -= 1
        else:
            invalid["profile"]["review_application_lease"]["not_after_utc"] = "2099-01-01T00:00:00+00:00"
        with pytest.raises(ValueError):
            validate_runtime_allocation(invalid, native_plan)
    invalid = copy.deepcopy(allocation)
    invalid["profile"]["review_application_lease"]["seconds"] = 3600.0
    invalid["review_application_binding"]["profile_sha256"] = digest(invalid["profile"])
    with pytest.raises(ValueError): validate_runtime_allocation(invalid, native_plan)


def test_recursive_index_keeps_exact_schema_and_selection(tmp_path, monkeypatch):
    from tests._review_dependency_fixture import case_evidence_report
    from tests.test_native_case_extended_policy import selected as old_fixture
    from tools._review_dependency_case_contracts import case_plan
    old, recipe = old_fixture.__wrapped__(tmp_path, monkeypatch)
    recipe.update(ceiling.binding(), schema_version=pilot.LONG_RECIPE, journal_collection_policy=copy.deepcopy(COLLECTION))
    pin = retain(tmp_path / "recursive-recipe.json", recipe)
    selected = budget.select(pin, "twenty-four-hour", old["fixture_input"], "selective_reuse", ["unchanged-baseline"], False, journal_collection=True)
    def index(label, shards=None):
        directory = tmp_path / label
        directory.mkdir()
        report, source = case_evidence_report()
        spawn = retain(directory / "0-child-0-spawn.json", {"plan_sha256": "3" * 64, "pid": 1, "pgid": 1})
        worker = retain(directory / "native-case-worker-manifest.json", {"worker_sha256": source["worker_sha256"], "case_plan": retain(directory / "worker-plan.json", case_plan())})
        report.update(extended_selection=selected, fixture_sha256=selected["fixture_sha256"], watchdog=budget.watchdog(selected, ["unchanged-baseline"]), originals=[spawn, worker])
        report["cleanup"]["children"] = [{"pid": 1, "leader_reaped": True, "group_absent": True}]
        if shards is not None: report["shards"] = [{"report": pin} for pin in shards]
        output = evidence.write_index(directory / "index", report, source_before=source, source_after=source, refresh=False)
        return output, evidence.read_index(output, roots=[tmp_path]), source
    child_pin, child, source = index("child")
    _, parent, _ = index("parent", [child_pin])
    evidence._verify_native_retention(parent, roots=[tmp_path], source=source)
    for change in ("schema", "selection"):
        invalid = copy.deepcopy(child)
        if change == "schema": invalid["schema_version"] = budget.COLLECTION_INDEX
        else: invalid["extended_selection"]["refresh"] = True
        bad_pin = retain(tmp_path / (change + "-child.json"), invalid)
        _, bad_parent, _ = index(change + "-parent", [bad_pin])
        with pytest.raises(ValueError): evidence._verify_native_retention(bad_parent, roots=[tmp_path], source=source)


@pytest.mark.parametrize("value", [True, 3600.0, 3601])
def test_owner_closed_policy_rejects_scalar_substitution_even_with_fresh_digest(value):
    selected = ceiling.binding(ceiling.OWNER_PROFILE)
    selected["phase_ceiling_policy"]["discovery_phase_caps"]["mutation"] = value
    selected["phase_ceiling_policy_sha256"] = digest(selected["phase_ceiling_policy"])
    with pytest.raises(ValueError, match="phase_ceiling_policy_invalid"):
        ceiling.validate(selected, required=True)

def test_owner_discovery_has_exact_finite_authority_and_unchanged_worker_recency():
    service = workload.service_policy(ceiling.OWNER_PROFILE)
    observer, execution = observation_policy("batch"), execution_policy("sixteen-hour")
    caps = workload.observational_phase_ceilings(service, observer, execution)
    assert caps == {"setup": 900, "job": 3600, "mutation": 3600, "owner_read": 3600, "global_close": 10800, "cleanup": 60}
    assert workload.allocation_seconds(caps, 6) == 44160 < execution["authority_seconds"] == 57600
    limits = workload.runtime_hard_limits(observer, execution, phase_caps=caps, ceiling_binding=ceiling.binding(ceiling.OWNER_PROFILE))
    assert limits["native_worker_seconds"] == 600
    assert limits["service_application_seconds"] == observer["max_age_seconds"] == 3600
    with pytest.raises(ValueError):
        workload.runtime_hard_limits(observer, execution, phase_caps=caps)
    for authority in ("eight-hour", "twenty-four-hour"):
        with pytest.raises(ValueError):
            workload.observational_phase_ceilings(service, observer, execution_policy(authority))
    for field, value in (("setup_seconds", 900.0), ("mutation_seconds", True), ("service_seconds", 3600.0)):
        with pytest.raises(ValueError):
            workload.observational_phase_ceilings({**service, field: value}, observer, execution)
    for invalid_observer, invalid_execution in (({"profile": "batch"}, execution),
            (observer, {"profile": "sixteen-hour"}),
            ({**observer, "max_age_seconds": 3600.0}, execution),
            (observer, {**execution, "schema_version": "review-execution-budget-policy/v1"}),
            (observer, {**execution, "authority_seconds": 57600.0})):
        with pytest.raises(ValueError):
            workload.observational_phase_ceilings(service, invalid_observer, invalid_execution)
    assert workload.formal_phase_ceilings(observer, execution)["mutation"] == 1200
    assert workload.formal_phase_ceilings(observer, execution, ceiling_binding=ceiling.binding(ceiling.OWNER_PROFILE))["mutation"] == 4500
    for invalid in (None, observation_policy("interactive")):
        with pytest.raises(ValueError):
            workload.formal_phase_ceilings(invalid, execution, ceiling_binding=ceiling.binding(ceiling.OWNER_PROFILE))

def owner_long_envelope(plan):
    bundle = Path(plan["artifacts"]["fresh-fixture-input.json"]["path"]).parent
    return pilot.reference_plan(bundle, execution_profile="sixteen-hour", service_profile=ceiling.OWNER_PROFILE, journal_collection=True)

def test_owner_current_and_historical_native_shape_reject_crossed_profiles_and_bindings(plan):
    selected = owner_long_envelope(plan)
    assert selected["schema_version"] == pilot.OWNER_PLAN
    assert workload.report_schema(selected["native_plan"]) == workload.RESULT_SCHEMA_OWNER
    assert pilot.validate_plan(selected, digest(selected)) == selected
    historical = copy.deepcopy(selected)
    begin = datetime(2020, 1, 1, tzinfo=timezone.utc)
    historical["native_plan"].update(frozen_at=begin.isoformat(), expires_at=(begin + timedelta(hours=16)).isoformat())
    historical["native_plan_sha256"] = digest(historical["native_plan"])
    assert pilot.validate_plan(historical, digest(historical), current=False) == historical
    for change in ("missing", "retag", "native_retag", "float_cap", "field", "policy_hash", "old_service"):
        invalid = copy.deepcopy(selected)
        if change == "missing":
            del invalid["native_plan"]["phase_ceiling_policy"]
        elif change == "retag":
            invalid["schema_version"] = pilot.COLLECTION_OBSERVATIONAL_PLAN
        elif change == "native_retag":
            invalid["native_plan"]["schema_version"] = workload.PLAN_SCHEMA_OBSERVATIONAL_COLLECTION
        elif change == "float_cap":
            invalid["native_plan"]["phase_caps"]["mutation"] = 3600.0
        elif change == "field":
            invalid["native_plan"]["extra"] = True
        elif change == "policy_hash":
            invalid["phase_ceiling_policy_sha256"] = "f" * 64
        else:
            invalid["native_plan"]["service_policy"] = workload.service_policy("control-observational")
        invalid["native_plan_sha256"] = digest(invalid["native_plan"])
        with pytest.raises(ValueError):
            pilot.validate_plan(invalid, digest(invalid), current=False)
    bundle = Path(plan["artifacts"]["fresh-fixture-input.json"]["path"]).parent
    with pytest.raises(ValueError):
        pilot.reference_plan(bundle, execution_profile="sixteen-hour", service_profile=ceiling.OWNER_PROFILE)

@pytest.fixture
def owner_completed(plan, monkeypatch):
    selected = owner_long_envelope(plan)
    report = {"closing_current_results": {"strict": {"review_producer_id": "sha256:" + "b" * 64}}}
    worst = {"setup": 10, "mutation": 3600, "job": 100, "owner_read": 100, "cleanup": 10, "global_close": 100}
    monkeypatch.setattr(workload, "read_pinned_artifact", lambda _: (selected, b"original"))
    monkeypatch.setattr(pilot, "_originals", lambda *_: (selected, report, worst, 12))
    return selected, worst

def test_owner_recipe_derives_headroom_from_complete_originals_and_rejects_changes(owner_completed, monkeypatch):
    selected, worst = owner_completed
    recipe = pilot.extended_measurement_recipe({}, {}, execution_profile="sixteen-hour")
    assert recipe["schema_version"] == pilot.OWNER_RECIPE and recipe["phase_caps"]["mutation"] == 4500
    assert ceiling.validate(recipe, required=True) == ceiling.binding(ceiling.OWNER_PROFILE)
    assert pilot.completed_extended_pilot_measurements(recipe, selected["candidate_sha256"]) == (worst, 12)
    summary = pilot.observational_measurements({}, {})
    assert summary["schema_version"] == pilot.OWNER_MEASUREMENT
    assert summary["formal_phase_ceilings"]["mutation"] == 4500
    for key, value in (("schema_version", pilot.COLLECTION_EXTENDED_RECIPE), ("phase_ceiling_policy_sha256", "0" * 64),
                       ("candidate_sha256", "f" * 64), ("phase_caps", {**recipe["phase_caps"], "mutation": 4500.0})):
        with pytest.raises(ValueError):
            pilot.completed_extended_pilot_measurements({**recipe, key: value}, selected["candidate_sha256"])
    worst["mutation"] = 3601
    with pytest.raises(ValueError, match="selective_pilot_allocation_invalid"):
        pilot.extended_measurement_recipe({}, {}, execution_profile="sixteen-hour")
    def incomplete(*_):
        raise ValueError("selective_pilot_report_binding_invalid")
    monkeypatch.setattr(pilot, "_originals", incomplete)
    with pytest.raises(ValueError, match="selective_pilot_report_binding_invalid"):
        pilot.extended_measurement_recipe({}, {}, execution_profile="sixteen-hour")

def owner_long_formal(cohort):
    from tests.test_review_journal_artifacts import collection_formal_plan
    plan, pair = collection_formal_plan(cohort)
    caps = {**plan["phase_caps"], "mutation": 4500}
    plan.update(ceiling.binding(ceiling.OWNER_PROFILE), schema_version=accounting.OWNER_PLAN, phase_caps=caps,
        elapsed_cap_seconds=workload.allocation_seconds(caps, 24) + 2 * caps["global_close"])
    return plan, pair

def test_owner_formal_schema_semantics_report_dispatch_and_allocation_bindings(cohort, monkeypatch):
    selected, pair = owner_long_formal(cohort)
    assert accounting.validate_plan(selected, pair) == selected
    report = accounting.initialize_report(selected, selected["pair_spec_pin"], pair)
    assert report["schema_version"] == accounting.OWNER_REPORT
    assert accounting.decode("Report", accounting.canonical(report)) == report
    assert accounting.validate_report(report, selected) == report
    monkeypatch.setattr(runner, "_read", lambda _: {"host_id": "direct"})
    binding = runner._allocation_binding(selected, [])
    assert ceiling.validate(binding, required=True) == ceiling.binding(ceiling.OWNER_PROFILE)
    assert binding["formal_plan"] == selected and binding["hard_limits"]["native_worker_seconds"] == 600
    for change in ("retag", "cap", "bool", "missing", "hash", "fit"):
        invalid = copy.deepcopy(selected)
        if change == "retag": invalid["schema_version"] = accounting.COLLECTION_PLAN
        elif change == "cap": invalid["phase_caps"]["mutation"] = 4501
        elif change == "bool": invalid["phase_ceiling_policy"]["formal_phase_ceilings"]["cleanup"] = True
        elif change == "missing": del invalid["phase_ceiling_policy"]
        elif change == "hash": invalid["phase_ceiling_policy_sha256"] = "0" * 64
        else: invalid["elapsed_cap_seconds"] += 1
        with pytest.raises(ValueError): accounting.validate_plan(invalid)
    with pytest.raises(ValueError):
        accounting.validate_report({**report, "schema_version": accounting.COLLECTION_REPORT}, selected)

def test_owner_native_case_selection_allocation_provisioning_and_index_routes(tmp_path, monkeypatch):
    from tests._review_dependency_fixture import case_evidence_report
    from tests.test_native_case_extended_policy import allocated
    from tests.test_native_case_extended_policy import selected as old_fixture
    selection, recipe = old_fixture.__wrapped__(tmp_path, monkeypatch)
    recipe.update(ceiling.binding(ceiling.OWNER_PROFILE), schema_version=pilot.OWNER_RECIPE, journal_collection_policy=copy.deepcopy(COLLECTION))
    recipe["phase_caps"]["mutation"] = 4500
    pin = retain(tmp_path / "prospective-recipe.json", recipe)
    value = budget.select(pin, "twenty-four-hour", selection["fixture_input"], "selective_reuse", ["unchanged-baseline"], False, journal_collection=True)
    assert value["schema_version"] == budget.OWNER_SELECTION
    binding, fixture = allocated(value)
    assert binding["schema_version"] == budget.OWNER_ALLOCATION
    seconds = binding["fixture_budget"]["fixture_run_seconds"]
    assert provisioning_window(value["execution_policy"], binding, seconds)[1] == binding["authority_expires_at"]
    assert ceiling.validate(binding, required=True) == ceiling.binding(ceiling.OWNER_PROFILE)
    report, source = case_evidence_report()
    report.update(extended_selection=value, fixture_sha256=value["fixture_sha256"], watchdog=budget.watchdog(value, ["unchanged-baseline"]))
    output = evidence.write_index(tmp_path / "prospective-index", report, source_before=source, source_after=source, refresh=False)
    index = evidence.read_index(output, roots=[tmp_path])
    assert index["schema_version"] == budget.OWNER_INDEX
    assert evidence.validate_index(index) == index
    for field in ("phase_ceiling_policy", "phase_ceiling_policy_sha256"):
        invalid = copy.deepcopy(value); del invalid[field]
        with pytest.raises(ValueError): budget.validate_selection(invalid)
    invalid = copy.deepcopy(index); invalid["schema_version"] = budget.COLLECTION_INDEX
    with pytest.raises(ValueError): evidence.validate_index(invalid)
    invalid = copy.deepcopy(binding); invalid["schema_version"] = budget.COLLECTION_ALLOCATION
    with pytest.raises(ValueError): provisioning_window(value["execution_policy"], invalid, seconds)
    invalid = copy.deepcopy(recipe); del invalid["journal_collection_policy"]
    with pytest.raises(ValueError): budget.fixture_budget(invalid, execution_policy("twenty-four-hour"))
    invalid = copy.deepcopy(recipe); invalid["phase_caps"]["job"] = 3600
    invalid["formal_application_policy"] = workload.formal_application_policy(3600)
    with pytest.raises(ValueError): budget.fixture_budget(invalid, execution_policy("sixteen-hour"))

def owner_retained_complete_costs(plan, tmp_path):
    """Build inert signed cost/custody records; this conveys no native execution."""
    from tests.test_review_dependency_workload import measurement_recipe
    from tools._execution_budget import credential_expiries
    from tools._review_dependency_workload import OriginalResponses, assignment_groups
    from tools._review_journal_artifacts import RECEIPT, read_collection

    envelope = owner_long_envelope(plan)
    native = envelope["native_plan"]
    directory = tmp_path / "complete-costs"
    _, report = measurement_recipe(native, directory)
    originals = OriginalResponses(directory / "additional", journal_collection=True)
    def save(label, value):
        originals.append((label, value))
        return originals.pins[-1]
    report.update(ceiling.binding(ceiling.OWNER_PROFILE), schema_version=workload.RESULT_SCHEMA_OWNER, mode="selective",
        **{key: native[key] for key in ("service_policy", "execution_policy", "execution_policy_sha256", "observation_policy", "observation_policy_sha256", "hard_limits", "source_protocol_limits", "journal_collection_policy")})
    for timing in report["job_elapsed"]:
        timing["cap_seconds"] = native["phase_caps"]["job"]
        timing["original_timing_pin"] = save("job-timing", {key: value for key, value in timing.items() if key != "original_timing_pin"})
    groups = assignment_groups(native["assignments"])
    for phase in report["phase_elapsed"]:
        label = phase["phase"]
        phase["cap_seconds"] = 2 * native["phase_caps"]["job"] if label in groups else native["phase_caps"]["owner_read" if label.startswith("owner-read") else label.replace("-", "_")]
    report["phase_elapsed"][next(i for i, phase in enumerate(report["phase_elapsed"]) if phase["phase"] == "mutation")]["elapsed_seconds"] = 3500
    for receipt in report["journal_receipts"]:
        _, metadata = read_collection(receipt["original_pin"])
        receipt.update(schema_version=RECEIPT, source_sha256="a" * 64, state_sha256="b" * 64, **{key: metadata[key] for key in ("record_count", "physical_nodes", "expanded_bytes", "artifact_kind", "page_pins")})
    control_pin = originals.append_records("control-authenticated", [])
    _, metadata = read_collection(control_pin)
    report["journal_receipts"].append({"schema_version": RECEIPT, "run_id": native["control_run_id"],
        "head_ref": None, "source_sha256": "a" * 64, "state_sha256": "b" * 64, "original_pin": control_pin,
        **{key: metadata[key] for key in ("records_sha256", "records_bytes", "record_count", "physical_nodes", "expanded_bytes", "artifact_kind", "page_pins")}})
    report["control"].update(attempt_id="control", dispatch_original_pin=save("control", {"attempt_id": "control", "before": [], "after": []}))
    closing_pins = []
    rounds = [*report["owner_reads"], copy.deepcopy(report["owner_reads"][-1])]
    for index, observed in enumerate(rounds):
        observed["journal_receipts"] = copy.deepcopy(report["journal_receipts"])
        observed.update(observation_policy=native["observation_policy"], observation_policy_sha256=native["observation_policy_sha256"], native_current=[])
        observed["strict"]["review_producer_id"] = "sha256:" + "b" * 64
        observed["observer_recency_not_after"] = (datetime.fromisoformat(observed["native_host_witnesses"][0]["observed_utc"]) + timedelta(seconds=3600)).isoformat()
        observed["native_host_witnesses"][0]["run_id"] = "owner"
        for label, value in (("owner-strict-current", observed["strict"]), ("owner-publication-current", observed["publication"]), ("owner-native-host-qualification", observed["native_host_witnesses"][0])):
            pin = save(label, value)
            if index == 2: closing_pins.append(pin)
    report.update(current_gate_results={"owner_read_index": 1}, closing_current_results=rounds[-1],
        closing_current_snapshots=[{"snapshot": "inert"}], closing_current_source_manifest={"source": "inert"},
        closing_current_elapsed_seconds=1, closing_current_original_pins=closing_pins)
    save("closing-current-snapshots", report["closing_current_snapshots"])
    save("closing-current-source-manifest", report["closing_current_source_manifest"])
    expiry = native["expires_at"]
    save("qualifications", [{"expires_utc": expiry}, {"expires_utc": expiry}])
    save("source-usage", [{"command": {"authentication": {"expires_at": expiry}, "payload": {"action": "read", "body": {}}}}])
    binding = {**ceiling.binding(ceiling.OWNER_PROFILE), **{key: native[key] for key in ("candidate_sha256", "phase_caps", "hard_limits", "source_protocol_limits", "execution_policy", "execution_policy_sha256", "observation_policy", "observation_policy_sha256", "service_policy", "journal_collection_policy", "assignments", "group_overhead_cap_seconds", "control_run_id")},
        "plan_sha256": digest(native), "authority_frozen_at": native["frozen_at"], "authority_expires_at": expiry,
        "service_selection": {"study": "pilot", "host": "direct", "case_id": "independent-claim-text", "mode": "selective"}}
    lease = {**native["service_policy"]["application_lease"], "not_after_utc": expiry}
    profile_owner = native_application_profile(lease)
    profile = profile_owner.document()
    save("runtime-allocation", {"schema_version": "review-workload-runtime-allocation/v2", "allocation_binding": binding,
        "profile": profile, "review_application_binding": {"profile_sha256": profile_owner.sha256, "lease": lease},
        "execution_policy": native["execution_policy"], "execution_policy_sha256": native["execution_policy_sha256"],
        "credential_expiries": credential_expiries(expiry), "authority_start": native["frozen_at"], "authority_end": expiry,
        "issued_before_worker_outputs": native["frozen_at"], "run_policy": {"budgets": {"seconds": native["elapsed_cap_seconds"]}}})
    report["original_response_pins"].extend(originals.pins)
    retain(directory / "native-pilot-plan.json", native)
    native_pin = retain(directory / "complete-native-report.json", report)
    envelope_pin = retain(directory / "envelope.json", envelope)
    outer = {**ceiling.binding(ceiling.OWNER_PROFILE), "schema_version": pilot.OWNER_REPORT, "plan_sha256": digest(envelope),
        "candidate_sha256": envelope["candidate_sha256"], "native_report_pin": native_pin, "mode": "selective_reuse",
        "terminal_outcome": "pilot_completed_unqualified", "pair_qualified": False}
    return envelope_pin, retain(directory / "envelope-report.json", outer), report, outer

def test_owner_actual_completed_reader_and_incomplete_original_guards(plan, tmp_path, monkeypatch):
    envelope_pin, report_pin, native, outer = owner_retained_complete_costs(plan, tmp_path)
    recipe = pilot.extended_measurement_recipe(envelope_pin, report_pin, execution_profile="sixteen-hour")
    assert recipe["phase_caps"]["mutation"] == 4375
    assert pilot.completed_extended_pilot_measurements(recipe, plan["candidate_sha256"])[0]["mutation"] == 3500
    bundle = Path(plan["artifacts"]["fresh-fixture-input.json"]["path"]).parent
    recipe_pin = retain(tmp_path / "complete-recipe.json", recipe)
    full = workload.reference_plan(bundle, study="full", measured_recipe=recipe_pin["path"],
        measurement_reader=pilot.OWNER_RECIPE, observation_profile="batch", execution_profile="sixteen-hour", journal_collection=True)
    assert full["schema_version"] == workload.PLAN_SCHEMA_OWNER_FORMAL
    assert ceiling.validate(full, required=True) == ceiling.binding(ceiling.OWNER_PROFILE)
    excessive = {**recipe, "group_overhead_cap_seconds": 10000}
    excessive_pin = retain(tmp_path / "excessive-recipe.json", excessive)
    with pytest.raises(ValueError, match="workload_allocation_exceeds_authority"):
        workload.reference_plan(bundle, study="full", measured_recipe=excessive_pin["path"],
            measurement_reader=pilot.OWNER_RECIPE, observation_profile="batch", execution_profile="sixteen-hour", journal_collection=True)
    for index, change in enumerate(({"terminal_outcome": "failed"}, {"applied_jobs": 4}, {"closing_current_results": None},
                                    {"cleanup": {"status": "unavailable"}}, {"failure": {"kind": "TimeoutError"}})):
        invalid = {**native, **change}
        pin = retain(tmp_path / ("incomplete-native-" + str(index) + ".json"), invalid)
        # Native-plan custody lives next to each original native report.
        if index == 0: retain(tmp_path / "native-pilot-plan.json", workload.read_pinned_artifact(envelope_pin)[0]["native_plan"])
        outer_pin = retain(tmp_path / ("incomplete-envelope-" + str(index) + ".json"), {**outer, "native_report_pin": pin})
        with pytest.raises(ValueError):
            pilot.extended_measurement_recipe(envelope_pin, outer_pin, execution_profile="sixteen-hour")
    with pytest.raises(ValueError):
        pilot.completed_extended_pilot_measurements(recipe, "f" * 64)
    monkeypatch.setattr(workload, "candidate_manifest", lambda _: {"changed": "candidate"})
    with pytest.raises(ValueError):
        pilot.completed_extended_pilot_measurements(recipe, plan["candidate_sha256"])

def test_owner_discovery_provisioning_rejects_policy_and_scalar_crossings(plan, tmp_path):
    from tools._execution_budget import validate_runtime_allocation
    envelope_pin, _, native_report, _ = owner_retained_complete_costs(plan, tmp_path)
    native_plan = workload.read_pinned_artifact(envelope_pin)[0]["native_plan"]
    allocation = workload.read_pinned_artifact(next(pin for pin in native_report["original_response_pins"] if pin["label"] == "runtime-allocation"))[0]
    binding = allocation["allocation_binding"]
    seconds = native_plan["elapsed_cap_seconds"]
    assert provisioning_window(native_plan["execution_policy"], binding, seconds)[1] == native_plan["expires_at"]
    assert validate_runtime_allocation(allocation, native_plan) == allocation
    for change in ("missing", "hash", "service", "cap", "limits", "collection"):
        invalid = copy.deepcopy(binding)
        if change == "missing": del invalid["phase_ceiling_policy"]
        elif change == "hash": invalid["phase_ceiling_policy_sha256"] = "f" * 64
        elif change == "service": invalid["service_policy"] = workload.service_policy("control-observational")
        elif change == "cap": invalid["phase_caps"]["mutation"] = 3600.0
        elif change == "limits": invalid["hard_limits"]["native_worker_seconds"] = 600.0
        else: invalid["journal_collection_policy"]["page_bytes"] = 1048576.0
        with pytest.raises(ValueError): provisioning_window(native_plan["execution_policy"], invalid, seconds)
    for change in ("workload_hash", "profile_schema", "profile_tamper", "lease_seconds", "lease_expiry"):
        invalid = copy.deepcopy(allocation)
        if change == "workload_hash":
            invalid["review_application_binding"]["profile_sha256"] = digest(invalid["profile"])
        elif change == "profile_schema":
            invalid["profile"]["schema_version"] = "research-execution-profile/v18"
        elif change == "profile_tamper":
            invalid["profile"]["profile_ref"] += "-changed"
        elif change == "lease_seconds":
            invalid["profile"]["review_application_lease"]["seconds"] -= 1
        else:
            invalid["profile"]["review_application_lease"]["not_after_utc"] = "2099-01-01T00:00:00+00:00"
        with pytest.raises(ValueError):
            validate_runtime_allocation(invalid, native_plan)
    invalid = copy.deepcopy(allocation)
    invalid["profile"]["review_application_lease"]["seconds"] = 3600.0
    invalid["review_application_binding"]["profile_sha256"] = digest(invalid["profile"])
    with pytest.raises(ValueError): validate_runtime_allocation(invalid, native_plan)

def test_owner_recursive_index_keeps_exact_schema_and_selection(tmp_path, monkeypatch):
    from tests._review_dependency_fixture import case_evidence_report
    from tests.test_native_case_extended_policy import selected as old_fixture
    from tools._review_dependency_case_contracts import case_plan
    old, recipe = old_fixture.__wrapped__(tmp_path, monkeypatch)
    recipe.update(ceiling.binding(ceiling.OWNER_PROFILE), schema_version=pilot.OWNER_RECIPE, journal_collection_policy=copy.deepcopy(COLLECTION))
    pin = retain(tmp_path / "recursive-recipe.json", recipe)
    selected = budget.select(pin, "twenty-four-hour", old["fixture_input"], "selective_reuse", ["unchanged-baseline"], False, journal_collection=True)
    def index(label, shards=None):
        directory = tmp_path / label
        directory.mkdir()
        report, source = case_evidence_report()
        spawn = retain(directory / "0-child-0-spawn.json", {"plan_sha256": "3" * 64, "pid": 1, "pgid": 1})
        worker = retain(directory / "native-case-worker-manifest.json", {"worker_sha256": source["worker_sha256"], "case_plan": retain(directory / "worker-plan.json", case_plan())})
        report.update(extended_selection=selected, fixture_sha256=selected["fixture_sha256"], watchdog=budget.watchdog(selected, ["unchanged-baseline"]), originals=[spawn, worker])
        report["cleanup"]["children"] = [{"pid": 1, "leader_reaped": True, "group_absent": True}]
        if shards is not None: report["shards"] = [{"report": pin} for pin in shards]
        output = evidence.write_index(directory / "index", report, source_before=source, source_after=source, refresh=False)
        return output, evidence.read_index(output, roots=[tmp_path]), source
    child_pin, child, source = index("child")
    _, parent, _ = index("parent", [child_pin])
    evidence._verify_native_retention(parent, roots=[tmp_path], source=source)
    for change in ("schema", "selection"):
        invalid = copy.deepcopy(child)
        if change == "schema": invalid["schema_version"] = budget.COLLECTION_INDEX
        else: invalid["extended_selection"]["refresh"] = True
        bad_pin = retain(tmp_path / (change + "-child.json"), invalid)
        _, bad_parent, _ = index(change + "-parent", [bad_pin])
        with pytest.raises(ValueError): evidence._verify_native_retention(bad_parent, roots=[tmp_path], source=source)

def test_all_historical_schema_bundles_stay_frozen():
    exported = accounting.schema_documents()
    old = {name: exported[name] for name in accounting._PRE_OWNER_SCHEMA_DEFS}
    assert len(old) == 53
    assert hashlib.sha256(accounting.canonical(old)).hexdigest() == "2648accfb9d6e4215ff4e50af46cc388242daf5f286bddfcd04202a38077de1c"
    assert len(exported) == 56



@pytest.mark.parametrize("old,new", list(ceiling.VERSION_PAIRS.items()))
def test_ceiling_versions_reject_crossed_declarations(old, new):
    for schema, profile, opposite in ((old, ceiling.PROFILE, ceiling.OWNER_PROFILE),
                                      (new, ceiling.OWNER_PROFILE, ceiling.PROFILE)):
        selected = {"schema_version": schema, **ceiling.binding(profile)}
        assert ceiling.validate(selected, required=True) == ceiling.binding(profile)
        with pytest.raises(ValueError, match="phase_ceiling_policy_invalid"):
            ceiling.validate({"schema_version": schema, **ceiling.binding(opposite)}, required=True)
    for profile, opposite in ((ceiling.PROFILE, ceiling.OWNER_PROFILE),
                              (ceiling.OWNER_PROFILE, ceiling.PROFILE)):
        with pytest.raises(ValueError, match="phase_ceiling_policy_invalid"):
            ceiling.validate({**ceiling.binding(profile), "service_policy": workload.service_policy(opposite)}, required=True)


@pytest.mark.parametrize("name,maximum,ceiling_value", [("owner_read", 2880, 3600), ("global_close", 8640, 10800)])
def test_owner_observed_headroom_boundaries(owner_completed, name, maximum, ceiling_value):
    _, worst = owner_completed
    worst[name] = maximum
    recipe = pilot.extended_measurement_recipe({}, {}, execution_profile="sixteen-hour")
    assert recipe["phase_caps"][name] == ceiling_value
    worst[name] = maximum + 1
    with pytest.raises(ValueError, match="selective_pilot_allocation_invalid"):
        pilot.extended_measurement_recipe({}, {}, execution_profile="sixteen-hour")


def test_owner_formal_constructor_routes_exact_completed_recipe(tmp_path, monkeypatch):
    # This routing check stops before native admission; provenance is checked separately.
    monkeypatch.setattr(pilot, "measured_observation_policy", lambda _recipe: observation_policy("batch"))
    recipe = {**ceiling.binding(ceiling.OWNER_PROFILE), "schema_version": pilot.OWNER_RECIPE,
              "journal_collection_policy": copy.deepcopy(COLLECTION)}
    recipe_pin = retain(tmp_path / "owner-recipe.json", recipe)
    pair = {"host_id": "direct", "case_id": "independent-claim-text"}
    monkeypatch.setattr(runner, "_read", lambda _: pair)
    monkeypatch.setattr(accounting, "validate_pair", lambda value: value)
    seen = []
    class SelectedRoute(Exception):
        pass
    def selected_route(bundle, **kwargs):
        seen.append((bundle, kwargs))
        raise SelectedRoute
    monkeypatch.setattr(workload, "reference_plan", selected_route)
    with pytest.raises(SelectedRoute):
        runner.reference_plan(tmp_path, {}, {}, mode="selective_reuse", directory=tmp_path / "plan",
            runtime_directory=tmp_path / "runtime", measured_recipe=recipe_pin["path"],
            execution_profile="sixteen-hour", pair_closing=True, journal_collection=True)
    assert len(seen) == 1
    assert seen[0][1]["measurement_reader"] == pilot.OWNER_RECIPE
    assert seen[0][1]["study"] == "full" and seen[0][1]["journal_collection"] is True


@pytest.mark.parametrize("profile", [ceiling.PROFILE, ceiling.OWNER_PROFILE])
@pytest.mark.parametrize("field,value", [("service_policy", None), ("service_policy", []),
    ("service_policy", True), ("schema_version", []), ("schema_version", {})])
def test_ceiling_malformed_discriminants_refuse_at_runtime_boundaries(profile, field, value):
    binding = {**ceiling.binding(profile), field: value}
    observer, execution = observation_policy("batch"), execution_policy("sixteen-hour")
    caps = ceiling.binding(profile)["phase_ceiling_policy"]["discovery_phase_caps"]
    for check in (lambda: ceiling.validate(binding, required=True),
                  lambda: workload.runtime_hard_limits(observer, execution, phase_caps=caps, ceiling_binding=binding),
                  lambda: workload.formal_phase_ceilings(observer, execution, ceiling_binding=binding)):
        with pytest.raises(ValueError, match="workload_phase_ceiling_policy_invalid"):
            check()
