"""Complete service accounting never turns unknown custody into saved work."""

import copy
from datetime import datetime, timedelta, timezone

import pytest

from tools import _review_dependency_accounting as accounting
from tools import _review_dependency_runner as runner
from tools import run_review_dependency_workload as legacy
from tools._execution_budget import execution_policy
from tools._observation_freshness import observation_policy
from tools._review_dependency_workload import digest, drive_selective_services, journal_run_id


def test_normalization_rejects_substituted_execution_documents(monkeypatch):
    # Isolate the binding check; native capability admission is tested separately.
    monkeypatch.setattr(accounting, "validate", lambda _name, value: value)
    pair = {"schema_version": accounting.PAIR, "candidate": "original"}
    plans = {"legacy_full": {"schema_version": accounting.PLAN, "plan": "original"}}
    reports = {"legacy_full": {"schema_version": accounting.REPORT, "stage_observations": [{"accepted": True}], "global_closing_utc": "close", "application": "original"}}
    proof = accounting._NativeNormalization(accounting._NORMALIZATION_MINT,
        evidence={"pair_spec_id": accounting.document_id(pair)}, claims={}, publications={},
        stages={"legacy_full": reports["legacy_full"]["stage_observations"]}, owners={"legacy_full": "close"},
        pair=pair, plans=plans, reports=reports)
    proof.assert_binding(pair, plans, reports)
    for target, key in ((pair, "candidate"), (plans["legacy_full"], "plan"), (reports["legacy_full"], "application")):
        original = target[key]
        target[key] = "substituted"
        with pytest.raises(ValueError, match="accounting_normalization_execution_changed"):
            proof.assert_binding(pair, plans, reports)
        target[key] = original


def test_native_report_binding_excludes_only_recursive_manifest():
    report = {"original_response_manifest_pin": "before", "global_closing_utc": "close", "cost": {"elapsed_seconds": 1}}
    identity = runner._report_source_identity(report)
    report["original_response_manifest_pin"] = "after"
    assert runner._report_source_identity(report) == identity
    report["cost"]["elapsed_seconds"] = 2
    assert runner._report_source_identity(report) != identity


def test_candidate_close_rejects_source_changes(monkeypatch):
    monkeypatch.setattr(legacy, "candidate_manifest", lambda _root: {"source": "before"})
    plan = {"candidate_sha256": digest({"source": "before"})}
    runner._assert_candidate(plan)
    monkeypatch.setattr(legacy, "candidate_manifest", lambda _root: {"source": "after"})
    with pytest.raises(ValueError, match="accounting_candidate_changed"):
        runner._assert_candidate(plan)


@pytest.fixture
def pin(tmp_path):
    return {"path": str(tmp_path / "original.json"), "raw_sha256": "a" * 64, "size_bytes": 1}


@pytest.fixture
def cohort(pin):
    host, case = "direct", "independent-claim-text"
    assignments = []
    originals = {claim: "demand" if claim in {"observed-demand", "independent-note"} else "capacity" for claim in accounting.CLAIMS}
    manifest = {"questions": [{"slug": name, "original_id": name} for name in ("capacity", "demand")],
                "claims": [{"id": claim, "question_slug": original} for claim, original in originals.items()]}
    for cycle in ("baseline", "first-change"):
        for claim in accounting.CLAIMS:
            for config in ("evidence", "critic"):
                key = "/".join((host, case, cycle, claim))
                assignments.append({"host_id": host, "case_id": case, "cycle": cycle, "claim_id": claim, "config_ref": config,
                    "original_id": originals[claim], "execution_role": "evaluator", "task_role": "critic" if config == "critic" else "evaluator",
                    "controller_ref": config + "-controller", "journal_key": key, "native_run_id": journal_run_id(key), "required": True,
                    "attempt_id": None, "application_event_ref": None})
    obligations = accounting.obligations(assignments, manifest)
    policy = {"schema_version": "evidence-strict-policy/v1", "raw_pin": pin, "content_id": "sha256:" + "1" * 64}
    selective_policy = {**policy, "schema_version": "evidence-strict-policy/v2"}
    pair = {"schema_version": accounting.PAIR, "candidate_sha256": "a" * 64, "candidate_manifest_pin": pin,
        "host_id": host, "case_id": case, "changed_cycle": "first-change", "fixture_pin": pin, "mutation_pin": pin,
        "verdict_table_pin": pin, "normalization_contract_id": accounting.document_id(accounting.NORMALIZATION_CONTRACT),
        "clock_contract_pin": pin, "legacy_policy": policy, "selective_policy": selective_policy,
        "expected_legacy_refresh_ids": list(accounting.CLAIMS), "expected_selective_refresh_ids": ["observed-demand", "independent-note", "adequacy", "margin"],
        "frozen_case_count": 99, "frozen_case_manifest_pin": pin, "prospective_case_manifest_pin": pin,
        "scope": "local_bundled_deterministic_worker_fixture", "minimum_avoided_completed_review_applications": 1, "normalization_contract_pin": pin}
    execution, observation = execution_policy("six-hour"), observation_policy("batch")
    begin = datetime(2026, 10, 1, tzinfo=timezone.utc)
    slots = [{"slot_id": run, "native_run_id": run, "kind": "review", "cycle": cycle, "claim_id": claim}
             for cycle in ("baseline", "first-change") for claim in accounting.CLAIMS
             for run in [journal_run_id("/".join((host, case, cycle, claim)))]]
    control = "control-" + accounting.hashlib.sha256((host + "/" + case).encode()).hexdigest()[:24]
    slots.append({"slot_id": control, "native_run_id": control, "kind": "changed_basis_control", "cycle": "first-change", "claim_id": None})
    plan = {"schema_version": accounting.PLAN, "study": "full", "mode": "selective_reuse", "pair_spec_pin": pin,
        "pair_spec_id": accounting.document_id(pair), "candidate_sha256": "a" * 64, "candidate_manifest_pin": pin,
        "host_identity_pin": pin, "input_manifest_pin": pin, "obligation_source_pin": pin, "obligations": obligations,
        "journal_slots": slots, "protected_policy": selective_policy, "claim_schema_version": "evidence-strict-claims/v2",
        "review_producer_id": "sha256:" + "2" * 64, "producer_manifest_pin": pin, "phase_caps": {name: 1 for name in ("setup", "job", "mutation", "owner_read", "global_close", "cleanup")},
        "group_overhead_cap_seconds": 0, "elapsed_cap_seconds": 60, "measured_recipe_pin": pin, "execution_policy": execution,
        "execution_policy_sha256": digest(execution), "observation_policy": observation, "observation_policy_sha256": digest(observation),
        "source_protocol_limits_pin": pin, "frozen_at": begin.isoformat(), "expires_at": (begin + timedelta(hours=6)).isoformat(),
        "control": "reserved_native_attempt_changed_basis_no_dispatch_no_application",
        "scope": "all_six_claims_two_questions_global_policy_sources_computation", "stdin": "closed"}
    return plan, pair


def test_runtime_ships_all_closed_definitions_and_fixed_normalization():
    schemas = accounting.schema_documents()
    assert len(schemas) == 56
    assert {"ControlPhaseCeilingPolicy", "LongPhaseCaps", "LongPlan", "LongReport"} <= schemas.keys()
    assert {"CollectionJournalReceipt", "CollectionPlan", "CollectionReport"} <= schemas.keys()
    assert accounting.document_id(accounting.NORMALIZATION_CONTRACT) == "sha256:2e715b3ce7b3ede7b7aac9c891871dc620654861cacf64d8362959ba5ca4e581"
    assert all(schema["$ref"].startswith("#/$defs/") for schema in schemas.values())


def test_initial_report_preserves_every_unknown_and_every_denominator(cohort, pin):
    plan, pair = cohort
    report = accounting.initialize_report(plan, pin, pair)
    accounting.validate_report(report, plan)
    assert len(report["resolutions"]) == 24
    assert len(report["journal_observations"]) == 13
    assert [row["round"] for row in report["owner_passes"]] == list(accounting.ROUNDS)
    assert report["counters"]["status_counts"]["unstarted"] == 24
    assert report["counters"]["fresh_completed_baseline_applications"] is None
    assert report["counters"]["validated_reused_changed_obligations"] is None
    assert all(row["additional_review_dispatches"] is None for row in report["resolutions"])
    assert all(row["model_calls"] is None for row in [report["cost"]])
    assert sum(row["planned_action"] == "retained_candidate" for row in report["resolutions"]) == 4


@pytest.mark.parametrize("mutation", ["drop_obligation", "duplicate_slot", "drop_owner", "claim_counter_success", "pretend_completed"])
def test_missing_or_self_asserted_completion_never_qualifies(cohort, pin, mutation):
    plan, pair = cohort
    report = accounting.initialize_report(plan, pin, pair)
    if mutation == "drop_obligation":
        report["resolutions"].pop()
    elif mutation == "duplicate_slot":
        report["journal_observations"][-1] = copy.deepcopy(report["journal_observations"][0])
    elif mutation == "drop_owner":
        report["owner_passes"].pop()
    elif mutation == "claim_counter_success":
        report["counters"]["fresh_completed_changed_applications"] = 0
    else:
        report["terminal_outcome"] = "completed"
    with pytest.raises(ValueError):
        accounting.validate_report(report, plan)


@pytest.mark.parametrize("malformed", [b'{"path":"/a","path":"/b","raw_sha256":"' + b'a' * 64 + b'","size_bytes":1}',
                                      b'{"path":"/a","raw_sha256":"' + b'a' * 64 + b'","size_bytes":true}',
                                      b'{"path":"/a","raw_sha256":"' + b'a' * 64 + b'","size_bytes":NaN}'])
def test_closed_codec_rejects_duplicates_boolean_counts_and_nonfinite(malformed):
    with pytest.raises(ValueError):
        accounting.decode("Pin", malformed)


def test_obligation_identifiers_bind_full_service_coordinate(cohort):
    plan, _pair = cohort
    evidence, critic = plan["obligations"][:2]
    assert evidence["obligation_id"] != critic["obligation_id"]
    assert evidence["native_run_id"] == critic["native_run_id"]
    changed = next(row for row in plan["obligations"] if row["coordinate"]["cycle"] == "first-change" and row["coordinate"]["claim_id"] == evidence["coordinate"]["claim_id"] and row["coordinate"]["config_ref"] == "evidence")
    assert changed["baseline_obligation_id"] == evidence["obligation_id"]
    broken = copy.deepcopy(plan)
    broken["obligations"][0]["controller_ref"] = "critic-controller"
    with pytest.raises(ValueError):
        accounting.validate_plan(broken)


def test_empty_fresh_service_selection_never_opens_a_native_session():
    result = drive_selective_services(None, object(), "direct", "run", object(), case_id="independent-claim-text", obligations=[])
    assert result["steps"] == []


def test_raw_dispatch_snapshot_dict_cannot_prove_zero(cohort):
    plan, _pair = cohort
    with pytest.raises(ValueError, match="receipt_not_live"):
        accounting.zero_dispatch(plan, plan["obligations"][-1], {}, {}, {})


def test_completed_mode_and_popen_handoff_cannot_be_minted_from_pins():
    with pytest.raises(ValueError):
        runner._admit_handoff({"closed": True, "cleanup": "confirmed"})
    with pytest.raises(ValueError):
        runner._CompletedMode(None, plan={}, report={"terminal_outcome": "completed"}, runtime=None, root=None, tracker=None,
                              fixture={}, sources={}, native={}, originals=None)


def test_legacy_cli_and_allocation_routes_keep_their_closed_versions():
    assert legacy.PLAN_SCHEMA_EXECUTION == "review-dependency-workload-plan/v4"
    assert legacy.RESULT_SCHEMA_EXECUTION == "review-dependency-workload-observation/v4"
    assert accounting.PLAN == "review-dependency-workload-plan/v5"


def test_offline_report_values_cannot_mint_native_comparison_credit(cohort, pin, tmp_path):
    from tools._review_dependency_workload import OriginalResponses
    selective_plan, pair = cohort
    legacy_plan = copy.deepcopy(selective_plan)
    legacy_plan.update(mode="legacy_full", protected_policy=pair["legacy_policy"])
    plans = {"legacy_full": legacy_plan, "selective_reuse": selective_plan}
    reports = {mode: accounting.initialize_report(plan, pin, pair) for mode, plan in plans.items()}
    for report in reports.values():
        for stage in report["stage_observations"]:
            stage.update(status="returned", native_claim_ids=list(accounting.CLAIMS), accepted_claim_ids=list(accounting.CLAIMS),
                         accepted_original_ids=["capacity", "demand"])
    result = accounting.differential(pair, plans, reports, pair_pin=pin, plan_pins=dict.fromkeys(plans, pin),
        report_pins=dict.fromkeys(plans, pin), originals=OriginalResponses(tmp_path / "comparison"))
    assert result["outcome"] == "unknown"
    assert len(result["claim_joins"]) == 6 and len(result["changed_obligation_joins"]) == 12
    assert result["legacy_changed_completed_applications"] is None
    assert result["avoided_completed_applications"] is None
    assert result["all_current_owners_validated"] is False
    assert all(row["legacy_after_accepted"] is None and row["selective_after_accepted"] is None for row in result["claim_joins"])
    assert all(row["saved_completed_application"] is None for row in result["changed_obligation_joins"])


@pytest.mark.parametrize("profile,hours", [("eight-hour", 8), ("sixteen-hour", 16), ("twenty-four-hour", 24)])
def test_extended_accounting_requires_typed_policy_and_exact_allocation(cohort, profile, hours):
    plan, pair = cohort
    caps = dict(plan["phase_caps"], job=1000, global_close=1500)
    execution = execution_policy(profile)
    plan.update(schema_version=accounting.EXTENDED_PLAN, execution_policy=execution,
        execution_policy_sha256=digest(execution), phase_caps=caps,
        formal_application_policy=legacy.formal_application_policy(1000),
        pair_closing_reservation=accounting.extended_closing_reservation(caps["global_close"]),
        expires_at=(datetime.fromisoformat(plan["frozen_at"]) + timedelta(hours=hours)).isoformat(),
        elapsed_cap_seconds=legacy.allocation_seconds(caps, 24) + 2 * caps["global_close"])
    accounting.validate_plan(plan, pair)
    report = accounting.initialize_report(plan, plan["pair_spec_pin"], pair)
    assert report["schema_version"] == accounting.EXTENDED_REPORT
    report["cost"]["elapsed_seconds"] = 22000
    accounting.validate_report(report, plan)
    changed = copy.deepcopy(plan)
    changed["elapsed_cap_seconds"] += 1
    with pytest.raises(ValueError, match="accounting_allocation_invalid"):
        accounting.validate_plan(changed)
    changed = copy.deepcopy(plan)
    changed["formal_application_policy"]["application_lease"]["seconds"] -= 1
    with pytest.raises(ValueError, match="workload_application_policy_invalid"):
        accounting.validate_plan(changed)
    changed = copy.deepcopy(plan)
    changed["schema_version"] = accounting.PAIR_PLAN
    with pytest.raises(ValueError):
        accounting.validate_plan(changed)


def test_extended_schema_keeps_old_caps_and_costs_closed():
    schemas = accounting.schema_documents()
    assert schemas["Plan"]["$defs"]["PhaseCaps"]["properties"]["job"]["maximum"] == 600
    assert schemas["PairPlan"]["$defs"]["PhaseCaps"]["properties"]["job"]["maximum"] == 600
    assert schemas["ExtendedPlan"]["$defs"]["ExtendedPhaseCaps"]["properties"]["job"]["maximum"] == 3600
    assert schemas["ExtendedCost"]["$defs"]["ExtendedCost"]["properties"]["elapsed_seconds"]["anyOf"][0]["maximum"] == 86400
    assert schemas["Cost"]["$defs"]["Cost"]["properties"]["elapsed_seconds"]["anyOf"][0]["maximum"] == 21600
