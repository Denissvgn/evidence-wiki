"""Explicit complete-cohort routes with native service and child observations."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from evidence_wiki import Workspace
from evidence_wiki.pack_discovery import owner
from evidence_wiki.research_journal_contracts import digest_record
from tools import _review_control_ceiling as ceiling_policy
from tools import _review_dependency_accounting as accounting
from tools._review_dependency_workload import (
    OriginalResponses,
    OwnedChildren,
    authenticated_journal,
    changed_basis_control,
    digest,
    drive_selective_services,
    file_manifest,
    mutation,
    native_dependency_rows,
    provision,
    retain,
    workload_deadline,
)


def pin(value):
    return {key: value[key] for key in ("path", "raw_sha256", "size_bytes")}


def _read(pin_value, *, maximum=1_048_576):
    path = Path(pin_value["path"])
    raw = accounting.read_pin(pin_value, roots=[path.parent], maximum=maximum)
    return owner("_qualified_packet").strict_json(raw)


def _failure(error, originals=None):
    # Exception text may contain source bodies, paths or credentials. The typed
    # native failure family is enough to keep an interrupted obligation visible.
    from tools._review_dependency_workload import deadline_cause
    error = deadline_cause(error) or error
    if originals is not None and originals.journal_collection:
        from tools._review_dependency_workload import failure_document
        document = failure_document(error)
        raw = originals.append(("bounded-native-failure", document))
        return {"kind": document["kind"] or "Exception", "error_code": document["error_code"], "reason": document["reason"], "raw_pin": raw}
    return {"kind": type(error).__name__[:128] or "Exception", "error_code": getattr(error, "error_code", None), "reason": None, "raw_pin": None}


def _mark_failure(report, error, originals):
    failure = _failure(error, originals)
    if report["failure"] is None:
        report["failure"] = failure
    else:
        originals.append(("secondary-failure", failure))
    report["terminal_outcome"] = "failed"


def reference_plan(bundle, pair_spec_pin, producer_manifest_pin, *, mode, directory, runtime_directory, measured_recipe, observation_profile="batch", execution_profile="six-hour", pair_closing=False, closing_reservation=None, journal_collection=False):
    """Freeze every service and journal before the first measured worker output."""
    from tools import run_review_dependency_workload as legacy
    pair = accounting.validate_pair(_read(pair_spec_pin))
    accounting.require(mode in {"legacy_full", "selective_reuse"}, "accounting_mode_invalid")
    recipe = legacy.read_pinned_artifact({"path": str(Path(measured_recipe).absolute()),
        "raw_sha256": legacy.digest_bytes(Path(measured_recipe).read_bytes())})[0]
    extended = recipe.get("schema_version") in {"selective-complete-pilot-measurement/v2", "selective-complete-pilot-measurement/v3", "selective-complete-pilot-measurement/v4", "selective-complete-pilot-measurement/v5"}
    accounting.require(type(journal_collection) is bool and accounting.collection_selected(recipe) == journal_collection, "accounting_journal_collection_policy_changed")
    accounting.require(not journal_collection or extended and pair_closing, "accounting_extended_pair_required")
    accounting.require(not extended or pair_closing, "accounting_extended_pair_required")
    accounting.require(extended or closing_reservation is None, "accounting_extended_pair_required")
    from tools._review_dependency_pilot import OWNER_RECIPE, measured_observation_policy
    observation = measured_observation_policy(recipe)
    accounting.require(observation["profile"] != "batch-extended" or recipe["schema_version"] == OWNER_RECIPE,
        "accounting_observation_profile_unsupported")
    selected = legacy.reference_plan(bundle, study="full", host=pair["host_id"], case_id=pair["case_id"], measured_recipe=measured_recipe,
                                     observation_profile=observation_profile, execution_profile=execution_profile, journal_collection=journal_collection,
                                     **({"measurement_reader": recipe["schema_version"]} if pair_closing else {}))
    accounting.require(ceiling_policy.exact(selected["observation_policy"], observation)
        and selected["observation_policy_sha256"] == digest(observation), "accounting_measured_recipe_changed")
    accounting.require(pair["candidate_sha256"] == selected["candidate_sha256"], "accounting_candidate_changed")
    directory = Path(directory).absolute()
    accounting.require(directory.resolve() == directory and not directory.exists() and not directory.is_relative_to(legacy.ROOT), "accounting_private_output_required")
    directory.mkdir(mode=0o700)
    originals = OriginalResponses(directory)
    assets = {}
    for name, original in selected["artifacts"].items():
        _value, raw = legacy.read_pinned_artifact(original)
        assets[name] = retain(directory / "inputs" / name, raw, raw=True)
    fixture_document = legacy.read_pinned_artifact(assets["fresh-fixture-input.json"])[0]
    runtime_directory = Path(runtime_directory).absolute()
    accounting.require(runtime_directory.resolve() == runtime_directory and not runtime_directory.exists() and not runtime_directory.is_relative_to(legacy.ROOT), "accounting_planned_target_invalid")
    from tools._review_dependency_workload import compile_fixture_policy
    compiled, protected = compile_fixture_policy(runtime_directory / "fixture", fixture_document["fixture"], mode)
    selected_policy_pin = pair["legacy_policy" if mode == "legacy_full" else "selective_policy"]
    accounting.require(_read(selected_policy_pin["raw_pin"]) == protected and selected_policy_pin["content_id"] == owner("_strict_contract").artifact_id(protected), "accounting_prospective_policy_changed")
    compiled_pin = originals.append(("compiled-profile", compiled))
    obligations = accounting.obligations(selected["assignments"], fixture_document["fixture"]["inventory"])
    slots, seen = [], set()
    for obligation in obligations:
        if obligation["native_run_id"] in seen:
            continue
        seen.add(obligation["native_run_id"])
        slots.append({"slot_id": obligation["native_run_id"], "native_run_id": obligation["native_run_id"], "kind": "review",
                      "cycle": obligation["coordinate"]["cycle"], "claim_id": obligation["coordinate"]["claim_id"]})
    slots.append({"slot_id": selected["control_run_id"], "native_run_id": selected["control_run_id"], "kind": "changed_basis_control", "cycle": "first-change", "claim_id": None})
    producer = _read(producer_manifest_pin)
    owner("_review_dependency_contract").validate("evidence-review-producer-manifest/v1", producer)
    identity = {"schema_version": "evidence-review-producer/v1", "manifest_id": accounting.identity("evidence-review-producer-manifest/v1", producer)}
    plan = {"schema_version": accounting.PLAN, "study": "full", "mode": mode, "pair_spec_pin": pair_spec_pin, "pair_spec_id": accounting.document_id(pair),
        "candidate_sha256": selected["candidate_sha256"], "candidate_manifest_pin": pair["candidate_manifest_pin"],
        "host_identity_pin": originals.append(("host-identity", selected["host"])),
        "input_manifest_pin": originals.append(("input-manifest", {"schema_version": "review-dependency-input-manifest/v1",
            "artifacts": {name: {**row, "size_bytes": Path(row["path"]).stat().st_size} for name, row in assets.items()},
            "planned_directory": str(runtime_directory), "compiled_profile_pin": compiled_pin})),
        "obligation_source_pin": originals.append(("obligation-source", selected["assignments"])), "obligations": obligations, "journal_slots": slots,
        "protected_policy": pair["legacy_policy" if mode == "legacy_full" else "selective_policy"],
        "claim_schema_version": fixture_document["fixture"]["inventory"]["schema_version"], "review_producer_id": accounting.identity("evidence-review-producer/v1", identity),
        "producer_manifest_pin": producer_manifest_pin, "phase_caps": selected["phase_caps"], "group_overhead_cap_seconds": selected["group_overhead_cap_seconds"],
        "elapsed_cap_seconds": selected["elapsed_cap_seconds"], "measured_recipe_pin": {**selected["measured_recipe"], "size_bytes": Path(selected["measured_recipe"]["path"]).stat().st_size},
        "execution_policy": selected["execution_policy"], "execution_policy_sha256": selected["execution_policy_sha256"],
        "observation_policy": selected["observation_policy"], "observation_policy_sha256": selected["observation_policy_sha256"],
        "source_protocol_limits_pin": originals.append(("runtime-limits", {"hard_limits": selected["hard_limits"], "source_protocol_limits": selected["source_protocol_limits"]})),
        "frozen_at": selected["frozen_at"], "expires_at": selected["expires_at"], "control": selected["control"], "scope": selected["scope"], "stdin": "closed"}
    if pair_closing:
        plan.update(schema_version=accounting.PAIR_PLAN,
                    pair_closing_reservation=dict(accounting.PAIR_RESERVATION))
    if extended:
        plan.update(schema_version=accounting.EXTENDED_PLAN, formal_application_policy=selected["formal_application_policy"],
            pair_closing_reservation=accounting.extended_closing_reservation(selected["phase_caps"]["global_close"], closing_reservation))
        accounting.require(recipe["review_producer_id"] == plan["review_producer_id"], "accounting_measured_producer_changed")
    if journal_collection:
        plan.update(schema_version=accounting.COLLECTION_PLAN, journal_collection_policy=dict(accounting.JOURNAL_COLLECTION_POLICY))
    if recipe.get("schema_version") in {"selective-complete-pilot-measurement/v4", "selective-complete-pilot-measurement/v5"}:
        plan.update(ceiling_policy.validate(recipe, required=True), schema_version=ceiling_policy.version(accounting.LONG_PLAN, recipe))
    accounting.validate_plan(plan, pair)
    return plan



def _input_frame(plan):
    frame = _read(plan["input_manifest_pin"])
    accounting.require(type(frame) is dict and set(frame) == {"schema_version", "artifacts", "planned_directory", "compiled_profile_pin"}
        and frame["schema_version"] == "review-dependency-input-manifest/v1", "accounting_input_manifest_invalid")
    accounting.validate("Pin", frame["compiled_profile_pin"])
    directory = Path(frame["planned_directory"])
    accounting.require(directory.is_absolute() and directory.resolve() == directory, "accounting_planned_target_invalid")
    return frame


def _assert_candidate(plan):
    from tools import run_review_dependency_workload as legacy
    accounting.require(plan["candidate_sha256"] == digest(legacy.candidate_manifest(legacy.ROOT)), "accounting_candidate_changed")


def _report_source_identity(report):
    # The manifest contains the native close carrying this digest.
    return digest({key: value for key, value in report.items() if key != "original_response_manifest_pin"})


def preflight(plan, expected_plan):
    from tools import run_review_dependency_workload as legacy
    accounting.validate_plan(plan)
    accounting.require(expected_plan == digest(plan) and plan["candidate_sha256"] == digest(legacy.candidate_manifest(legacy.ROOT)), "accounting_candidate_changed")
    pair = accounting.validate_pair(_read(plan["pair_spec_pin"]))
    accounting.validate_plan(plan, pair)
    moment = datetime.now(timezone.utc)
    accounting.require(datetime.fromisoformat(plan["frozen_at"]) <= moment < datetime.fromisoformat(plan["expires_at"]), "accounting_plan_expired")
    from tools._review_dependency_pilot import OWNER_RECIPE, measured_observation_policy
    recipe = _read(plan["measured_recipe_pin"])
    observation = measured_observation_policy(recipe)
    accounting.require(observation["profile"] != "batch-extended" or recipe["schema_version"] == OWNER_RECIPE,
        "accounting_observation_profile_unsupported")
    accounting.require(ceiling_policy.exact(plan["observation_policy"], observation)
        and plan["observation_policy_sha256"] == digest(observation), "accounting_measured_recipe_changed")
    frame = _input_frame(plan)
    assets = frame["artifacts"]
    documents = {name: legacy.read_pinned_artifact(row)[0] for name, row in assets.items()}
    assignments, _case = legacy.validate_inputs(documents, "full", pair["host_id"], pair["case_id"])
    accounting.require(accounting.obligations(assignments, documents["fresh-fixture-input.json"]["fixture"]["inventory"]) == plan["obligations"], "accounting_assignment_changed")
    cases = documents["acceptance-cases.json"]["cases"]
    accounting.require(len(cases) == 99 and pair["frozen_case_count"] == 99, "accounting_case_denominator_changed")
    normalization = _read(pair["normalization_contract_pin"])
    accounting.require(normalization == accounting.NORMALIZATION_CONTRACT, "accounting_normalization_contract_changed")
    if plan["schema_version"] in {accounting.EXTENDED_PLAN, accounting.COLLECTION_PLAN, accounting.LONG_PLAN, accounting.OWNER_PLAN}:
        from tools._review_dependency_pilot import completed_extended_pilot_measurements
        recipe = _read(plan["measured_recipe_pin"])
        completed_extended_pilot_measurements(recipe, plan["candidate_sha256"])
        accounting.require(ceiling_policy.exact(ceiling_policy.validate(recipe, required=plan["schema_version"] in {accounting.LONG_PLAN, accounting.OWNER_PLAN}), ceiling_policy.validate(plan)), "accounting_measured_recipe_changed")
        accounting.require(accounting.collection_selected(recipe) == accounting.collection_selected(plan), "accounting_journal_collection_policy_changed")
        accounting.require(recipe["phase_caps"] == plan["phase_caps"] and recipe["formal_execution_policy"] == plan["execution_policy"]
            and recipe["review_producer_id"] == plan["review_producer_id"]
            and recipe["formal_application_policy"] == plan["formal_application_policy"]
            and recipe["group_overhead_cap_seconds"] == plan["group_overhead_cap_seconds"], "accounting_measured_recipe_changed")
    elif plan["schema_version"] == accounting.PAIR_PLAN:
        from tools._review_dependency_pilot import completed_pilot_measurements
        completed_pilot_measurements(_read(plan["measured_recipe_pin"]), plan["candidate_sha256"])
    else:
        legacy.completed_pilot_measurements(_read(plan["measured_recipe_pin"]), plan["candidate_sha256"])
    from tools._review_dependency_workload import compile_fixture_policy
    compiled, protected = compile_fixture_policy(Path(frame["planned_directory"]) / "fixture", documents["fresh-fixture-input.json"]["fixture"], plan["mode"])
    original_compiled = _read(frame["compiled_profile_pin"])
    accounting.require(compiled["profile"] == original_compiled["profile"] and {key: value for key, value in compiled["bindings"].items() if key != "target"} == {key: value for key, value in original_compiled["bindings"].items() if key != "target"}
        and _read(plan["protected_policy"]["raw_pin"]) == protected and owner("_strict_contract").artifact_id(protected) == plan["protected_policy"]["content_id"], "accounting_compiled_profile_changed")
    for name in ("fixture_pin", "mutation_pin", "verdict_table_pin", "clock_contract_pin", "frozen_case_manifest_pin", "prospective_case_manifest_pin", "candidate_manifest_pin"):
        _read(pair[name])
    return {"status": "prepared_runtime_unqualified", "plan_id": accounting.document_id(plan), "plan_sha256": expected_plan,
            "service_obligations": 24, "review_journals": 12, "control_journals": 1, "owner_passes": 3, "qualification": False}


def _allocation_binding(plan, assignments):
    from tools import run_review_dependency_workload as legacy
    result = {**ceiling_policy.validate(plan), "candidate_sha256": plan["candidate_sha256"], "phase_caps": plan["phase_caps"],
        "hard_limits": legacy.runtime_hard_limits(plan["observation_policy"], plan["execution_policy"], phase_caps=plan["phase_caps"], ceiling_binding=plan), "source_protocol_limits": legacy.HARD_LIMITS,
        "execution_policy": plan["execution_policy"], "execution_policy_sha256": plan["execution_policy_sha256"],
        "observation_policy": plan["observation_policy"], "observation_policy_sha256": plan["observation_policy_sha256"],
        "plan_sha256": digest(plan), "authority_frozen_at": plan["frozen_at"], "authority_expires_at": plan["expires_at"],
        "assignments": assignments, "group_overhead_cap_seconds": plan["group_overhead_cap_seconds"],
        "control_run_id": next(row["native_run_id"] for row in plan["journal_slots"] if row["kind"] == "changed_basis_control")}
    if "formal_application_policy" in plan:
        result.update(formal_application_policy=plan["formal_application_policy"], measured_recipe_pin=plan["measured_recipe_pin"],
            formal_plan=plan,
            formal_service_selection={"study": "full", "host": _read(plan["host_identity_pin"])["host_id"], "mode": plan["mode"]})
    if accounting.collection_selected(plan):
        result["journal_collection_policy"] = dict(accounting.JOURNAL_COLLECTION_POLICY)
    return result


def _native_facts(root, runtime, obligation, applied_ref, *, journal_collection=False):
    from evidence_wiki import execution_session as native
    helper = getattr(native, "retained_review_evidence", None)
    accounting.require(callable(helper), "accounting_native_owner_unavailable")
    with runtime.context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(runtime.policy, run_id=obligation["native_run_id"], execution=runtime.execution) as session:
            return helper(session, {"run_id": obligation["native_run_id"], "record_sha256": applied_ref}, obligation["coordinate"], **({"journal_collection": True} if journal_collection else {}))


def _strict(root, runtime, run_id):
    from evidence_wiki._review_dependency_inputs import bind_session
    with runtime.context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(runtime.policy, run_id=run_id, execution=runtime.execution) as session, bind_session(session):
            return workspace.strict.check(), workspace.strict.export()


def _stage(plan, report, root, runtime, run_id, label, originals):
    strict, publication = _strict(root, runtime, run_id)
    rows = native_dependency_rows(root, runtime, run_id) if plan["mode"] == "selective_reuse" else [
        {"claim_id": row["claim"]["id"], "basis_id": strict["basis_id"], "semantic_material_id": None, "accepted": row["accepted"]} for row in strict["claims"]]
    row = next(item for item in report["stage_observations"] if item["stage"] == label)
    row.update(status="returned", strict_pin=originals.append((label + "-strict", strict)), publication_pin=originals.append((label + "-publication", publication)),
        native_basis_rows_pin=originals.append((label + "-basis", rows)), native_claim_ids=[item["claim"]["id"] for item in strict["claims"]],
        accepted_claim_ids=[item["claim"]["id"] for item in strict["claims"] if item["accepted"]],
        accepted_original_ids=[item["original_id"] for item in publication["original_outcomes"] if item["accepted"]])
    return strict, publication, rows


def _resolve(root, runtime, run_id, originals, label):
    from evidence_wiki import execution_session as native
    with runtime.context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(runtime.policy, run_id=run_id, execution=runtime.execution) as session:
            for original in runtime.policy.to_dict()["scope"]["question_ids"]:
                native._resolve(session, original)
            originals.append_records(label + "-resolution", runtime.execution.records(session))


def run(plan, expected_plan, directory, *, plan_pin, _completed_mode=False, _predecessor=None):
    """Run complete native cohorts; retained services never enter the fresh driver."""
    from tools import run_review_dependency_workload as legacy
    from tools._execution_budget import validate_workload_allocation_v5
    preflight(plan, expected_plan)
    pair = _read(plan["pair_spec_pin"])
    directory = Path(directory).absolute()
    accounting.require(str(directory) == _input_frame(plan)["planned_directory"], "accounting_runtime_target_changed")
    accounting.require(directory.resolve() == directory and not directory.exists() and not directory.is_relative_to(legacy.ROOT), "accounting_private_output_required")
    directory.mkdir(mode=0o700)
    originals = OriginalResponses(directory / "observations", journal_collection=accounting.collection_selected(plan))
    report = accounting.initialize_report(plan, plan_pin, pair)
    report["terminal_outcome"] = "started_stop_unknown"
    root = runtime = sources = None
    tracker = OwnedChildren(originals, expected_plan)
    tracker.open(predecessor=_predecessor)
    started = time.monotonic()
    timings, applications, receipts, snapshots = [], {}, {}, {}
    baseline_rows = None
    by_id = {row["obligation_id"]: row for row in plan["obligations"]}
    baseline = {(row["coordinate"]["claim_id"], row["coordinate"]["config_ref"]): row for row in plan["obligations"] if row["coordinate"]["cycle"] == "baseline"}
    resolutions = {row["obligation_id"]: row for row in report["resolutions"]}
    def snapshot(label):
        observation = tracker.review_dispatch_snapshot(runtime.execution.executor, label, {"plan": plan, "runtime": runtime, "root": root})
        snapshots[label] = observation
        report["dispatch_snapshot_pins"].append(observation.pin)
        return observation
    def observe_owner(label):
        tick = time.monotonic()
        with workload_deadline(plan.get("pair_closing_reservation", {}).get("mode_close_seconds", plan["phase_caps"]["global_close"]) if label == "current_closing" else plan["phase_caps"]["owner_read"]):
            observed, _snapshots, _records = legacy._owner_read(root, runtime,
                [row["native_run_id"] for row in plan["journal_slots"]], originals, plan["observation_policy"],
                **({"selective_run_id": plan["obligations"][-1]["native_run_id"]}
                   if plan["mode"] == "selective_reuse" else {}))
            owner_row = next(row for row in report["owner_passes"] if row["round"] == label)
            owner_row.update(status="returned", strict_pin=originals.append((label + "-strict", observed["strict"])),
                publication_pin=originals.append((label + "-publication", observed["publication"])), native_current_pin=originals.append((label + "-current", observed["native_current"])),
                host_qualification_pin=originals.append((label + "-qualification", observed["native_host_witnesses"])),
                journal_receipt_pins=[originals.append((label + "-journal", row)) for row in observed["journal_receipts"]],
                source_manifest_pin=originals.append((label + "-source-manifest", file_manifest(root))),
                authority_custody_pin=originals.append((label + "-authority", {"authority_sha256": allocation["authority_raw_sha256"]})),
                semantic_identity="sha256:" + legacy.owner_read_identity(observed), observed_utc=datetime.now(timezone.utc).isoformat())
            for obligation in plan["obligations"]:
                resolution = resolutions[obligation["obligation_id"]]
                if resolution["planned_action"] != "retained_candidate":
                    continue
                historical = by_id[obligation["baseline_obligation_id"]]
                validation_tick = time.monotonic()
                facts = _native_facts(root, runtime, historical, applications[historical["obligation_id"]]["applied_record_ref"], journal_collection=originals.journal_collection)
                receipt = accounting.eligibility_receipt(plan, obligation, applications[historical["obligation_id"]], facts, label, originals)
                resolution["reuse_validation_elapsed_seconds"] = (resolution["reuse_validation_elapsed_seconds"] or 0) + time.monotonic() - validation_tick
                receipts[(obligation["obligation_id"], label)] = receipt
                receipt_pin = originals.append((label + "-retained-service", receipt))
                resolution["eligibility_receipt_pins"].append(receipt_pin)
                owner_row["eligibility_receipt_pins"].append(receipt_pin)
                accounting.require(receipt["state"] == "eligible", "accounting_retained_service_ineligible")
            owner_row["elapsed_seconds"] = time.monotonic() - tick
            for journal, receipt_pin in zip(report["journal_observations"], owner_row["journal_receipt_pins"], strict=True):
                journal.update(state="authenticated", closing_receipt_pin=receipt_pin)
                journal["round_receipt_pins"].append(receipt_pin)
    try:
        remaining = (datetime.fromisoformat(plan["expires_at"]) - datetime.now(timezone.utc)).total_seconds()
        with workload_deadline(min(plan["elapsed_cap_seconds"] - plan["phase_caps"]["cleanup"] - plan.get("pair_closing_reservation", {}).get("pair_close_seconds", 0), remaining)):
            input_frame = _input_frame(plan)
            assets = input_frame["artifacts"]
            documents = {name: legacy.read_pinned_artifact(row)[0] for name, row in assets.items()}
            assignments, case = legacy.validate_inputs(documents, "full", pair["host_id"], pair["case_id"])
            fixture = documents["fresh-fixture-input.json"]["fixture"]
            with workload_deadline(plan["phase_caps"]["setup"]):
                root, runtime, sources, keys, _claims = provision(directory / "fixture", fixture, originals, run_seconds=plan["elapsed_cap_seconds"],
                    allocation_binding=_allocation_binding(plan, assignments), execution_policy=plan["execution_policy"], children=tracker,
                    mcp=pair["host_id"] == "scoped_mcp", review_mode=plan["mode"], compiled_profile=_read(input_frame["compiled_profile_pin"]),
                    protected_policy=_read(plan["protected_policy"]["raw_pin"]))
                allocation = owner("_qualified_packet").strict_json((directory / "runtime-allocation.json").read_bytes())
                validate_workload_allocation_v5(allocation, plan, runtime, {"root": root, "private_roots": [Path(plan["source_protocol_limits_pin"]["path"]).parent], "responses": originals.pins})
                tracker.bind_review_dispatch(plan, runtime)
            # Existing native begin creates each real bounded header/binding;
            # no packet, reservation, external call or application is created.
            from evidence_wiki import execution_session as native
            with runtime.context.activate(), Workspace.open(root) as workspace:
                for slot in plan["journal_slots"]:
                    with workspace.research.session(runtime.policy, run_id=slot["native_run_id"], execution=runtime.execution) as session:
                        with native.gate(session):
                            session._run()
                        originals.append_records(slot["slot_id"] + "-header", runtime.execution.records(session))
            snapshot("opening")
            for observation in report["journal_observations"]:
                records, receipt, _capture, _view = authenticated_journal(root, runtime, observation["run_id"], originals)
                observation.update(state="authenticated" if records else "authenticated_empty",
                    opening_receipt_pin=originals.append((observation["slot_id"] + "-opening", receipt)))
            for cycle in ("baseline", "first-change"):
                cycle_obligations = [row for row in plan["obligations"] if row["coordinate"]["cycle"] == cycle]
                if cycle == "first-change":
                    snapshot("changed_opening")
                    control_id = next(row["native_run_id"] for row in plan["journal_slots"] if row["kind"] == "changed_basis_control")
                    with workload_deadline(plan["phase_caps"]["mutation"]):
                        control = changed_basis_control(root, runtime, control_id, originals, lambda: mutation(root, fixture, case, sources, keys, runtime, originals))
                    originals.append(("native-changed-basis-control", control))
                    attempts = [pin(row) for row in originals.pins if row["label"] == "changed-basis-attempt"]
                    accounting.require(len(attempts) == 1, "accounting_control_attempt_missing")
                    report["control"].update(status="reserved_refused_as_expected" if control.get("native_error_code") == "RESEARCH_BASIS_CHANGED" and control.get("applied") is False else "unknown",
                        attempt_pin=attempts[0], native_refusal_pin=originals.append(("native-control-refusal", control)))
                    _strict_result, _publication, after = _stage(plan, report, root, runtime, cycle_obligations[0]["native_run_id"], "before_refresh", originals)
                    accounting.require(type(baseline_rows) is list, "accounting_native_baseline_missing")
                    before = {row["claim_id"]: row for row in baseline_rows}
                    observed_change = {row["claim_id"] for row in after if (row["basis_id"], row["semantic_material_id"]) != (before[row["claim_id"]]["basis_id"], before[row["claim_id"]]["semantic_material_id"])}
                    expected_change = set(pair["expected_legacy_refresh_ids"] if plan["mode"] == "legacy_full" else pair["expected_selective_refresh_ids"])
                    accounting.require(observed_change == expected_change, "accounting_native_refresh_expectation_mismatch")
                    for obligation in cycle_obligations:
                        resolution = resolutions[obligation["obligation_id"]]
                        if resolution["planned_action"] != "retained_candidate":
                            continue
                        resolution["status"] = "started_stop_unknown"
                        historical = baseline[(obligation["coordinate"]["claim_id"], obligation["coordinate"]["config_ref"])]
                        tick = time.monotonic()
                        facts = _native_facts(root, runtime, historical, applications[historical["obligation_id"]]["applied_record_ref"], journal_collection=originals.journal_collection)
                        receipt = accounting.eligibility_receipt(plan, obligation, applications[historical["obligation_id"]], facts, "before_refresh", originals)
                        receipts[(obligation["obligation_id"], "before_refresh")] = receipt
                        resolution["eligibility_receipt_pins"].append(originals.append(("retained-before-refresh", receipt)))
                        resolution["reuse_validation_elapsed_seconds"] = time.monotonic() - tick
                        accounting.require(receipt["state"] == "eligible", "accounting_retained_service_ineligible")
                for run_id in dict.fromkeys(row["native_run_id"] for row in cycle_obligations):
                    fresh = [row for row in cycle_obligations if row["native_run_id"] == run_id and resolutions[row["obligation_id"]]["planned_action"] == "fresh_required"]
                    for obligation in fresh:
                        resolutions[obligation["obligation_id"]]["status"] = "started_stop_unknown"
                    with workload_deadline(2 * plan["phase_caps"]["job"] + plan["group_overhead_cap_seconds"]):
                        drive_selective_services(root, runtime, pair["host_id"], run_id, originals, case_id=pair["case_id"], obligations=fresh,
                            job_cap_seconds=plan["phase_caps"]["job"], job_elapsed=timings, children=tracker)
                    tracker.confirm_completed()
                    records, receipt, _capture, _view = authenticated_journal(root, runtime, run_id, originals)
                    for obligation in fresh:
                        coordinate = obligation["coordinate"]
                        selected = [row for row in records if row["kind"] == "execution_applied" and row["data"].get("operation") == "evaluate"
                            and row["data"].get("claim_id") == coordinate["claim_id"] and row["data"].get("profile_ref") == coordinate["config_ref"]]
                        accounting.require(len(selected) == 1, "accounting_native_application_missing")
                        timing = next(row for row in timings if row["native_run_id"] == run_id and row["config_ref"] == coordinate["config_ref"] and row["claim_id"] == coordinate["claim_id"])
                        facts = _native_facts(root, runtime, obligation, digest_record({key: value for key, value in selected[0].items() if key != "event_ref"}), journal_collection=originals.journal_collection)
                        application = accounting.application_evidence(facts, pin(timing["original_timing_pin"]), originals)
                        applications[obligation["obligation_id"]] = application
                        resolution = resolutions[obligation["obligation_id"]]
                        passed = application["passed"] is True and timing["native_passed"] is True and timing["status"] == "applied"
                        resolution.update(status="fresh_applied" if passed else "failed", final_action="fresh_application", fresh_application=application,
                            attempt_history_pin=originals.append_records("fresh-attempt-history", records), review_elapsed_seconds=timing["elapsed_seconds"],
                            additional_review_dispatches=None, additional_completed_review_applications=1 if passed else 0,
                            failure=None if passed else {"kind": "ValueError", "error_code": None, "reason": "native_review_not_passed", "raw_pin": None})
                last_run = cycle_obligations[-1]["native_run_id"]
                _resolve(root, runtime, last_run, originals, cycle)
                strict, publication, rows = _stage(plan, report, root, runtime, last_run, "baseline" if cycle == "baseline" else "after_refresh", originals)
                accounting.require(all(row["accepted"] for row in strict["claims"]) and publication["verdict"] == "ship", "accounting_native_cycle_incomplete")
                if cycle == "baseline":
                    baseline_rows = rows
                    baseline_dispatch = snapshot("baseline_complete")
                    if baseline_dispatch.document["state"] == "complete":
                        for obligation in cycle_obligations:
                            resolutions[obligation["obligation_id"]]["additional_review_dispatches"] = sum(
                                row["kind"] == "local_review_worker" and row["obligation_id"] == obligation["obligation_id"] for row in baseline_dispatch.document["births"])
                else:
                    snapshot("changed_complete")
            for label in accounting.ROUNDS[:2]:
                observe_owner(label)
    except BaseException as error:
        _mark_failure(report, error, originals)
    finally:
        try:
            with workload_deadline(plan["phase_caps"]["cleanup"]):
                cleanup = tracker.close()
                if runtime is not None:
                    for actual in runtime.execution.executor._attempts.values():
                        observed = runtime.execution.executor.cancel(actual.attempt)
                        authenticated = runtime.execution.executor.authenticate(observed, actual.attempt)
                        originals.append((actual.attempt.attempt_id + "-cleanup", authenticated))
                        accounting.require(authenticated["state"] != "running" and authenticated["final_accounting"] is True, "accounting_cleanup_unconfirmed")
                report["cleanup_pin"] = originals.append(("cleanup", cleanup))
            if root is not None and runtime is not None and tracker._review_dispatch_state is not None:
                raw_pins, retention = legacy.collect_journals(root, directory)
                report["raw_journal_manifest_pin"] = originals.append(("raw-journal-manifest", {"pins": raw_pins, "retention": retention}))
                accounting.require(retention["status"] == "complete", "accounting_raw_retention_incomplete")
                if report["failure"] is None:
                    observe_owner("current_closing")
                    accounting.require(len({row["semantic_identity"] for row in report["owner_passes"]}) == 1, "accounting_closing_owner_changed")
                    snapshot("current_closing")
                    cleanup_dispatch = snapshot("cleanup_closed")
                    if cleanup_dispatch.document["state"] == "complete":
                        for obligation in plan["obligations"]:
                            resolution = resolutions[obligation["obligation_id"]]
                            if resolution["status"] == "fresh_applied":
                                resolution["additional_review_dispatches"] = sum(row["kind"] == "local_review_worker" and row["obligation_id"] == obligation["obligation_id"] for row in cleanup_dispatch.document["births"])
                    for obligation in plan["obligations"]:
                        resolution = resolutions[obligation["obligation_id"]]
                        if resolution["planned_action"] != "retained_candidate" or len(resolution["eligibility_receipt_pins"]) != 4:
                            continue
                        zero = accounting.zero_dispatch(plan, obligation, snapshots["changed_opening"], snapshots["current_closing"], snapshots["cleanup_closed"])
                        resolution["zero_dispatch_receipt_pin"] = originals.append(("retained-zero-dispatch", zero))
                        if zero["state"] == "verified_zero":
                            resolution.update(status="reused_validated", final_action="retained_review", additional_review_dispatches=0,
                                              additional_completed_review_applications=0, review_elapsed_seconds=None)
                        else:
                            resolution["status"] = "unknown"
                    control_run = next(row["native_run_id"] for row in plan["journal_slots"] if row["kind"] == "changed_basis_control")
                    report["control"]["dispatch_count"] = sum(row["run_id"] == control_run for row in snapshots["cleanup_closed"].attempts)
                    report["control"]["applied_count"] = sum(row["kind"] == "execution_applied" for row in snapshots["cleanup_closed"].journals[control_run])
        except BaseException as error:
            _mark_failure(report, error, originals)
        for obligation in plan["obligations"]:
            resolution = resolutions[obligation["obligation_id"]]
            entered = [row for row in timings if row["native_run_id"] == obligation["native_run_id"]
                and row["claim_id"] == obligation["coordinate"]["claim_id"] and row["config_ref"] == obligation["coordinate"]["config_ref"]]
            if resolution["status"] == "started_stop_unknown" and len(entered) == 1:
                timing = entered[0]
                resolution["status"] = "timed_out" if timing["status"] == "timed_out" else "failed" if timing["status"] == "failed" else "unknown"
                resolution["review_elapsed_seconds"] = timing["elapsed_seconds"]
                resolution["failure"] = report["failure"]
        report["cost"].update(elapsed_seconds=time.monotonic() - started,
            known_review_elapsed_subtotal_seconds=sum(row["review_elapsed_seconds"] for row in report["resolutions"] if row["review_elapsed_seconds"] is not None),
            reuse_validation_elapsed_seconds=sum(row["reuse_validation_elapsed_seconds"] for row in report["resolutions"] if row["reuse_validation_elapsed_seconds"] is not None),
            owner_validation_elapsed_seconds=sum(row["elapsed_seconds"] for row in report["owner_passes"] if row["elapsed_seconds"] is not None),
            coverage="partial" if timings else "unknown",
            unknown_obligation_ids=[row["obligation_id"] for row in report["resolutions"] if row["review_elapsed_seconds"] is None])
        report["counters"] = accounting.counters(report)
        report["original_response_manifest_pin"] = originals.append(("original-response-manifest", originals.pins))
        report["global_closing_utc"] = datetime.now(timezone.utc).isoformat()
        if report["failure"] is None:
            try:
                _assert_candidate(plan)
                report["terminal_outcome"] = "completed"
            except ValueError as error:
                _mark_failure(report, error, originals)
        try:
            accounting.validate_report(report, plan)
        except ValueError as error:
            _mark_failure(report, error, originals)
            for resolution in report["resolutions"]:
                if resolution["status"] in {"fresh_applied", "reused_validated"}:
                    resolution["status"] = "unknown"
                    resolution["failure"] = report["failure"]
            report["counters"] = accounting.counters(report)
            if report["cost"]["elapsed_seconds"] > (plan["execution_policy"]["authority_seconds"] if plan["schema_version"] in {accounting.EXTENDED_PLAN, accounting.COLLECTION_PLAN, accounting.LONG_PLAN, accounting.OWNER_PLAN} else 21_600):
                report["failure"]["raw_pin"] = originals.append(("elapsed-overrun", {"elapsed_seconds": report["cost"]["elapsed_seconds"]}))
                report["cost"]["elapsed_seconds"] = None
            accounting.validate_report(report, plan)
    capability = None
    if _completed_mode and report["terminal_outcome"] == "completed":
        try:
            remaining = (datetime.fromisoformat(plan["expires_at"]) - datetime.now(timezone.utc)).total_seconds()
            with workload_deadline(min(plan.get("pair_closing_reservation", {}).get("mode_close_seconds", plan["phase_caps"]["global_close"]), remaining, started + plan["elapsed_cap_seconds"] - plan.get("pair_closing_reservation", {}).get("pair_close_seconds", 0) - time.monotonic())):
                capability = _capture_completed_mode(plan, report, runtime, root, tracker, fixture, sources, originals, snapshots["cleanup_closed"], started)
        except BaseException as error:
            _mark_failure(report, error, originals)
            report["counters"] = accounting.counters(report)
    retain(directory / "report.json", report)
    return capability if capability is not None else report


_MODE_MINT = object()


def _mode_members():
    import weakref
    if not hasattr(_mode_members, "members"):
        _mode_members.members = weakref.WeakSet()
    return _mode_members.members


class _CompletedMode:
    """Process-private close evidence cannot be restored from report dictionaries."""

    def __init__(self, mint, *, plan, report, runtime, root, tracker, fixture, sources, native, originals, started=None):
        accounting.require(mint is _MODE_MINT and report["terminal_outcome"] == "completed" and tracker.closed,
                           "accounting_completed_mode_owner_required")
        accounting.validate_report(report, plan)
        self.plan, self.report = accounting.validate_plan(plan), accounting.validate_report(report, plan)
        self.runtime, self.root, self.tracker = runtime, root, tracker
        self.fixture, self.sources, self.native = fixture, sources, native
        self.originals = originals
        accounting.require(type(started) in {int, float}, "accounting_completed_mode_clock_required")
        self.started = started
        self.deadline = started + plan["elapsed_cap_seconds"]
        self._timing = (self.started, self.deadline)
        self._pair_closing_claimed = False
        self._plan_bytes = accounting.canonical(plan)
        self._native_bytes = accounting.canonical(native)
        self._report_bytes = accounting.canonical(report)
        _mode_members().add(self)

    def assert_original(self):
        accounting.require(self in _mode_members() and self.tracker.closed and accounting.canonical(self.native) == self._native_bytes
                           and accounting.canonical(self.report) == self._report_bytes, "accounting_completed_mode_changed")
        return self


def _admit_handoff(capability):
    """Keep the old guard installed until a new guarded wrapper is ready."""
    import subprocess

    from tools._integration_owner_pass import _defer_signals
    accounting.require(type(capability) is _CompletedMode and capability in _mode_members(), "accounting_dispatch_handoff_owner_required")
    capability.assert_original()
    tracker, runtime = capability.tracker, capability.runtime
    with tracker.lock, runtime.execution.executor._lock, _defer_signals():
        accounting.require(tracker.closed and subprocess.Popen is tracker.launch and tracker.real_popen is not None
            and all(actual.snapshot is not None and actual.snapshot.get("final_accounting") is True
                    and actual.snapshot.get("state") != "running" and actual.finished.is_set() for actual in runtime.execution.executor._attempts.values()),
            "accounting_dispatch_handoff_incomplete")
        accounting.require(all(id(process) in tracker._completed for process in tracker.owned)
            and all(row["kind"] != "unclassified" for row in capability.native["dispatch"]["births"])
            and capability.native["cleanup"]["status"] == "confirmed", "accounting_dispatch_handoff_unconfirmed")
        return tracker.real_popen


def _capture_completed_mode(plan, report, runtime, root, tracker, fixture, sources, originals, snapshot, started, *, fresh=False):
    from evidence_wiki._review_dependency_inputs import bind_session
    from evidence_wiki.host_context import review_producer_binding
    tick = time.monotonic()
    run_id = plan["obligations"][0]["native_run_id"]
    with runtime.context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(runtime.policy, run_id=run_id, execution=runtime.execution) as session, bind_session(session), review_producer_binding() as producer:
            actual_producer = producer.document()
            accounting.require(producer.producer_id == plan["review_producer_id"] and actual_producer["manifest"] == _read(plan["producer_manifest_pin"]),
                "accounting_planned_producer_changed")
            strict, publication = workspace.strict.check(), workspace.strict.export()
            config = owner("_strict_evidence").configuration(root)
            capture = owner("_evidence_revision").capture_workspace(root)
            with owner("_evidence_usage").current_view(root, config) as view:
                qualified = owner("_publication_usage").qualify_capture(capture.files, config, view, purpose="qa-export", consumer="evidence-wiki")
                computation = owner("_computation_service").required(root, config, view=view)
                _coverage, summaries = owner("_review_dependencies")._coverage(root, config, capture, fixture["inventory"], view)
                manifest = owner("_strict_contract").claims_document(owner("_record_artifacts").json_document(capture.files[config["strict_evidence"]["claims_path"]]))
                questions = [owner("_question_dependencies").project(root, config, config["strict_evidence"], capture, manifest, question["slug"], coverage_summary=summaries[question["slug"]]) for question in manifest["questions"]]
                source_material = {}
                records = [owner("_record_artifacts").json_document(line) for line in capture.files[config["sources"]["manifest_path"]].splitlines() if line.strip()]
                for coordinate, selected in sources.items():
                    source_id = selected["source_id"]
                    revision = qualified["selected"][source_id]
                    original = view.state.revisions[revision]
                    normalized = capture.files[selected["normalized_path"]]
                    metadata, body, error = owner("_normalized_contract").split_record(normalized.decode("utf-8"))
                    accounting.require(error is None, "accounting_normalized_source_invalid")
                    record = next(row for row in records if row["id"] == source_id)
                    inspected = owner("_host_capture").inspect_record(root, record)
                    source_material[coordinate] = {"source_id": source_id, "revision": revision, "record_path": selected["normalized_path"],
                        "record_sha256": "sha256:" + digest_bytes_local(normalized), "record_bytes": normalized.hex(), "metadata": metadata,
                        "body": body, "manifest": record, "capture": inspected,
                        "files": {path: raw.hex() for path, raw in original["files"].items()}, "descriptor": original["descriptor"],
                        "usage": view.check(revision, uses=["retrieval", "export"], purpose="qa-export", consumer="evidence-wiki"),
                        "usage_expires_at": owner("_evidence_usage").qualify_revision(original, revision, view.trust, view.now)["expires_at"]}
                accounting.require(all(row["usage"]["eligible"] is True and row["usage"]["complete"] is True
                                       for row in source_material.values()), "accounting_source_close_ineligible")
                authority = {"content_hash": view.trust["content_hash"], "policy": view.trust["policy"]}
                temporal_limits = [datetime.fromisoformat(authority["policy"]["expires_at"])]
                policy = config["strict_evidence"]
                for source in source_material.values():
                    temporal_limits.append(datetime.fromisoformat(source["usage_expires_at"]))
                for row in strict["claims"]:
                    for evidence in row["claim"]["evidence"]:
                        temporal_limits.append(datetime.fromisoformat(evidence["observed_at"].replace("Z", "+00:00"))
                                               + timedelta(seconds=policy["max_source_age_seconds"]))
                    review_id = row["review"]["review_id"]
                    matching = [record["envelope"]["payload"]["body"]["review"] for record in view.state.strict_reviews.values()
                                if owner("_strict_contract").artifact_id(record["envelope"]["payload"]["body"]["review"]) == review_id]
                    accounting.require(len(matching) == 1, "accounting_pair_review_owner_missing")
                    temporal_limits.append(datetime.fromisoformat(matching[0]["reviewed_at"].replace("Z", "+00:00"))
                                           + timedelta(seconds=policy["max_review_age_seconds"]))
                view.revalidate(root, config)
            from evidence_wiki import execution_session as native
            from evidence_wiki import host_qualification
            with native._current_facts(session, operation="current"):
                current = native._current_view(session)
                qualification = host_qualification(session)
                accounting.require(native.current_qualifications(session)["state"] == "qualified", "accounting_completed_mode_qualification_failed")
            accounting.require(strict["claims"] and all(row["accepted"] for row in strict["claims"]) and publication["verdict"] == "ship", "accounting_completed_mode_current_failed")
            if plan["mode"] == "selective_reuse":
                accounting.require(strict["review_producer_id"] == producer.producer_id and publication["review_producer_id"] == producer.producer_id, "accounting_native_producer_changed")
    native = {"temporal_not_after": min(temporal_limits).isoformat(), "authority": authority, "producer": actual_producer, "strict": strict, "publication": publication, "native_current": current, "host_qualification": qualification, "config": config, "computation": computation, "questions": questions,
        "sources": source_material, "dispatch": snapshot.document, "cleanup": _read(report["cleanup_pin"]),
        "requirements": owner("_question_dependencies").typed_node(owner("_requirements_bridge").effective(root, capture.files[owner("_requirements_bridge").PATH])),
        "report_source_excluded_fields": ["original_response_manifest_pin"], "closed_at": report["global_closing_utc"]}
    # Native dates retain their types in the source owner. The private capability
    # stores their tagged equivalent rather than coercing semantic values.
    native["sources"] = {key: {**value, "metadata": owner("_question_dependencies").typed_node(value["metadata"]),
        "manifest": owner("_question_dependencies").typed_node(value["manifest"]), "capture": owner("_question_dependencies").typed_node(value["capture"])} for key, value in source_material.items()}
    if fresh:
        native["closed_at"] = datetime.now(timezone.utc).isoformat()
        _assert_candidate(plan)
        originals.append(("pair-current-native-close", native))
        return native
    report["cost"]["elapsed_seconds"] = time.monotonic() - started
    report["cost"]["owner_validation_elapsed_seconds"] = sum(row["elapsed_seconds"] for row in report["owner_passes"]) + time.monotonic() - tick
    report["global_closing_utc"] = datetime.now(timezone.utc).isoformat()
    native["closed_at"] = report["global_closing_utc"]
    _assert_candidate(plan)
    native["report_source_sha256"] = _report_source_identity(report)
    originals.append(("completed-mode-native-close", native))
    report["original_response_manifest_pin"] = originals.append(("completed-mode-original-manifest", originals.pins))
    accounting.require(report["cost"]["elapsed_seconds"] <= plan["elapsed_cap_seconds"], "accounting_outer_allocation_expired")
    accounting.validate_report(report, plan)
    return _CompletedMode(_MODE_MINT, plan=plan, report=report, runtime=runtime, root=root, tracker=tracker,
                          fixture=fixture, sources=sources, native=native, originals=originals, started=started)


def digest_bytes_local(raw):
    import hashlib
    return hashlib.sha256(raw).hexdigest()


def run_pair(legacy_plan, selective_plan, *, legacy_plan_pin, selective_plan_pin, directory, prospective_case_results_pin=None):
    """Retain two independent actual mode closes in one trusted parent process."""
    from tools._review_dependency_normalization import compare_completed
    pair = accounting.validate_pair(_read(legacy_plan["pair_spec_pin"]))
    accounting.validate_plan(legacy_plan, pair)
    accounting.validate_plan(selective_plan, pair)
    accounting.require(legacy_plan["mode"] == "legacy_full" and selective_plan["mode"] == "selective_reuse"
        and legacy_plan["candidate_sha256"] == selective_plan["candidate_sha256"]
        and legacy_plan["review_producer_id"] == selective_plan["review_producer_id"], "accounting_pair_plan_binding_invalid")
    directory = Path(directory).absolute()
    from tools import run_review_dependency_workload as legacy_tools
    accounting.require(directory.resolve() == directory and not directory.exists() and not directory.is_relative_to(legacy_tools.ROOT), "accounting_private_output_required")
    directory.mkdir(mode=0o700)
    accounting.require(accounting.collection_selected(legacy_plan) == accounting.collection_selected(selective_plan), "accounting_journal_collection_policy_changed")
    originals = OriginalResponses(directory / "comparison", journal_collection=accounting.collection_selected(legacy_plan))
    capabilities = {}
    reports = {}
    normalization = None
    error = None
    try:
        with workload_deadline(legacy_plan["elapsed_cap_seconds"] + selective_plan["elapsed_cap_seconds"]):
            first = run(legacy_plan, digest(legacy_plan), directory / "legacy", plan_pin=legacy_plan_pin, _completed_mode=True)
            if type(first) is _CompletedMode:
                capabilities["legacy_full"] = first
                reports["legacy_full"] = first.report
            else:
                reports["legacy_full"] = first
            if "legacy_full" in capabilities:
                second = run(selective_plan, digest(selective_plan), directory / "selective", plan_pin=selective_plan_pin,
                             _completed_mode=True, _predecessor=capabilities["legacy_full"])
                if type(second) is _CompletedMode:
                    capabilities["selective_reuse"] = second
                    reports["selective_reuse"] = second.report
                else:
                    reports["selective_reuse"] = second
            else:
                reports["selective_reuse"] = accounting.initialize_report(selective_plan, selective_plan_pin, pair)
                retain(directory / "selective-unstarted.json", reports["selective_reuse"])
            if len(capabilities) == 2:
                normalization = compare_completed(capabilities["legacy_full"], capabilities["selective_reuse"], pair, originals)
            _assert_candidate(legacy_plan)
            _assert_candidate(selective_plan)
    except BaseException as caught:
        error = _failure(caught, originals)
    plans = {"legacy_full": legacy_plan, "selective_reuse": selective_plan}
    plan_pins = {"legacy_full": legacy_plan_pin, "selective_reuse": selective_plan_pin}
    for mode in ("legacy_full", "selective_reuse"):
        if mode not in reports:
            reports[mode] = accounting.initialize_report(plans[mode], plan_pins[mode], pair)
            reports[mode].update(terminal_outcome="unavailable", failure=error)
    report_pins = {mode: originals.append((mode + "-report", report)) for mode, report in reports.items()}
    result = accounting.differential(pair, plans, reports, pair_pin=legacy_plan["pair_spec_pin"], plan_pins=plan_pins, report_pins=report_pins,
        originals=originals, normalization=normalization, prospective_case_results_pin=prospective_case_results_pin)
    if error is not None:
        result.update(outcome="failed", failure=error)
    retain(directory / "differential.json", result)
    return result


def freeze_pair_spec(declaration, directory):
    """Pin the complete fixed comparison recipe before either native mode runs."""
    from tools import run_review_dependency_workload as legacy
    from tools._review_dependency_normalization import CLOCK_CONTRACT
    directory = Path(directory).absolute()
    accounting.require(directory.resolve() == directory and not directory.exists() and not directory.is_relative_to(legacy.ROOT), "accounting_private_output_required")
    directory.mkdir(mode=0o700)
    originals = OriginalResponses(directory)
    value = dict(declaration)
    value.update(schema_version=accounting.PAIR, normalization_contract_id=accounting.document_id(accounting.NORMALIZATION_CONTRACT),
        normalization_contract_pin=originals.append(("normalization-contract", accounting.NORMALIZATION_CONTRACT)),
        clock_contract_pin=originals.append(("clock-contract", CLOCK_CONTRACT)))
    accounting.validate_pair(value)
    accounting.require(value["candidate_sha256"] == digest(legacy.candidate_manifest(legacy.ROOT)), "accounting_candidate_changed")
    for name in ("legacy_policy", "selective_policy"):
        policy_pin = value[name]
        document = _read(policy_pin["raw_pin"])
        owner("_strict_contract").policy_document(document)
        accounting.require(document["schema_version"] == policy_pin["schema_version"]
            and owner("_strict_contract").artifact_id(document) == policy_pin["content_id"], "accounting_policy_pin_changed")
    fixture = _read(value["fixture_pin"])
    owner("_strict_contract").claims_document(fixture["fixture"]["inventory"])
    cases = _read(value["frozen_case_manifest_pin"])
    accounting.require(type(cases) is dict and type(cases.get("cases")) is list and len(cases["cases"]) == 99
        and len({row["id"] for row in cases["cases"]}) == 99, "accounting_case_denominator_changed")
    result_pin = originals.append(("pair-spec", value))
    return value, result_pin


def compile_pair_policies(fixture_pin, directory, *, runtime_directory):
    """Compile both exact prospective policies before any apply or worker call."""
    from tools import run_review_dependency_workload as legacy
    from tools._review_dependency_workload import compile_fixture_policy
    fixture = _read(fixture_pin)["fixture"]
    directory = Path(directory).absolute()
    runtime_directory = Path(runtime_directory).absolute()
    accounting.require(directory.resolve() == directory and not directory.exists() and not directory.is_relative_to(legacy.ROOT)
        and runtime_directory.resolve() == runtime_directory and not runtime_directory.exists() and not runtime_directory.is_relative_to(legacy.ROOT), "accounting_private_output_required")
    directory.mkdir(mode=0o700)
    originals = OriginalResponses(directory)
    result = {}
    for mode, name in (("legacy_full", "legacy"), ("selective_reuse", "selective")):
        compiled, policy = compile_fixture_policy(runtime_directory / name / "fixture", fixture, mode)
        originals.append((name + "-compiled-profile", compiled))
        raw_pin = originals.append((name + "-prospective-policy", policy))
        result[name + "_policy"] = {"schema_version": policy["schema_version"], "raw_pin": raw_pin, "content_id": owner("_strict_contract").artifact_id(policy)}
    return result
