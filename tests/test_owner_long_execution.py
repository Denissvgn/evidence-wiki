"""Explicit long authority preserves original evidence and bounded applications."""

import copy
from datetime import datetime, timedelta, timezone

import pytest

from evidence_wiki import RunPolicy
from evidence_wiki.run_policy import schemas
from evidence_wiki.workflow_contracts import WorkflowContractError
from tests._review_dependency_fixture import fixture_document
from tests._script_loader import load_module
from tests.test_extended_observer_consumers import pinned_recipe
from tests.test_native_case_extended_policy import allocated
from tests.test_prospective_control_ceiling import owner_completed as owner_completed
from tests.test_review_dependency_workload import plan as plan
from tools import _review_control_ceiling as ceiling
from tools import _review_dependency_accounting as accounting
from tools import _review_dependency_case_budget as budget
from tools import _review_dependency_case_evidence as evidence
from tools import _review_dependency_pilot as pilot
from tools import _review_dependency_workload as workload
from tools import run_review_dependency_workload as native
from tools._execution_budget import (
    execution_policy,
    provisioning_window,
    validate_execution_binding,
    validate_execution_window,
)
from tools._observation_freshness import observation_policy


def binding(profile=ceiling.OWNER_PROFILE, observer="batch-extended"):
    policy = execution_policy("forty-eight-hour")
    observed = observation_policy(observer)
    return {**ceiling.binding(profile), "execution_policy": policy,
        "execution_policy_sha256": workload.digest(policy), "observation_policy": observed,
        "observation_policy_sha256": workload.digest(observed)}


def test_explicit_long_authority_and_original_deadline():
    selected = binding()
    policy = selected["execution_policy"]
    assert policy["authority_seconds"] == policy["credential_lifetime_seconds"] == 172800
    assert policy["deadline_basis"] == "frozen_plan_expiry" and policy["renewal"] == "forbidden"
    assert validate_execution_binding(policy, selected) == policy
    start = datetime(2026, 10, 19, tzinfo=timezone.utc)
    end = start + timedelta(seconds=172800)
    assert validate_execution_window(policy, start.isoformat(), end.isoformat(), instant=end-timedelta(seconds=1))
    for instant in (start-timedelta(seconds=1), end, end+timedelta(seconds=1)):
        with pytest.raises(ValueError):
            validate_execution_window(policy, start.isoformat(), end.isoformat(), instant=instant)
    with pytest.raises(ValueError):
        validate_execution_window(policy, start.isoformat(), (end+timedelta(seconds=1)).isoformat())


@pytest.mark.parametrize("profile,observer", [(ceiling.PROFILE,"batch-extended"), (ceiling.OWNER_PROFILE,"batch")])
def test_long_authority_refuses_historical_owner_or_original_window(profile, observer):
    selected = binding(profile, observer)
    with pytest.raises(ValueError):
        validate_execution_binding(selected["execution_policy"], selected)
    with pytest.raises(ValueError):
        native.formal_phase_ceilings(selected["observation_policy"], selected["execution_policy"], ceiling_binding=selected)


def test_long_authority_keeps_phase_and_native_limits():
    selected = binding()
    caps = native.formal_phase_ceilings(selected["observation_policy"], selected["execution_policy"], ceiling_binding=selected)
    assert caps == ceiling.OWNER_POLICY["formal_phase_ceilings"]
    limits = native.runtime_hard_limits(selected["observation_policy"], selected["execution_policy"], phase_caps=caps, ceiling_binding=selected)
    assert limits["protected_authority_seconds"] == 172800
    assert limits["native_worker_seconds"] == 600
    assert limits["service_application_seconds"] == 3600
    assert limits["observer_recency_seconds"] == 7200
    with pytest.raises(ValueError):
        native.validate_service_policy(native.service_policy(ceiling.OWNER_PROFILE), study="pilot",host="direct",case_id="independent-claim-text",observation=selected["observation_policy"],execution=selected["execution_policy"])


def test_long_recipe_requires_current_extended_originals(owner_completed):
    original, worst = owner_completed
    native_plan = original["native_plan"]
    native_plan.update(observation_policy=observation_policy("batch-extended"))
    native_plan["observation_policy_sha256"] = workload.digest(native_plan["observation_policy"])
    recipe = pilot.extended_measurement_recipe({}, {}, execution_profile="forty-eight-hour")
    assert recipe["schema_version"] == pilot.OWNER_RECIPE
    assert recipe["measurement_execution_policy"]["authority_seconds"] == 57600
    assert recipe["formal_execution_policy"]["authority_seconds"] == 172800
    assert pilot.completed_extended_pilot_measurements(recipe, original["candidate_sha256"]) == (worst,12)
    native_plan.update(observation_policy=observation_policy("batch"))
    with pytest.raises(ValueError):
        pilot.extended_measurement_recipe({}, {}, execution_profile="forty-eight-hour")


def test_long_native_selection_allocation_and_original_provisioning(tmp_path, monkeypatch):
    recipe, source = pinned_recipe(tmp_path,"batch-extended")
    recipe.update(ceiling.binding(ceiling.OWNER_PROFILE), schema_version=pilot.OWNER_RECIPE,
        journal_collection_policy=copy.deepcopy(native.JOURNAL_COLLECTION_POLICY),
        formal_execution_policy=execution_policy("forty-eight-hour"))
    recipe["phase_caps"].update(owner_read=3600, global_close=10800, mutation=4500)
    # Keep current-owner provenance admission isolated; exercise actual allocation and clocks.
    monkeypatch.setattr(pilot,"completed_extended_pilot_measurements",lambda *_:None)
    monkeypatch.setattr(evidence,"source_identity",lambda:source)
    from types import SimpleNamespace

    from evidence_wiki import _review_producer
    monkeypatch.setattr(_review_producer,"capture",lambda:SimpleNamespace(producer_id=recipe["review_producer_id"]))
    monkeypatch.setattr(_review_producer,"assert_current",lambda *_:None)
    pin=workload.retain(tmp_path/"recipe.json",recipe)
    selected=budget.select(pin,"forty-eight-hour",fixture_document(),"selective_reuse",["unchanged-baseline"],False,journal_collection=True)
    assert selected["schema_version"] == budget.OWNER_SELECTION
    allocation,fixture=allocated(selected)
    seconds=allocation["fixture_budget"]["fixture_run_seconds"]
    assert seconds > 86400
    assert provisioning_window(selected["execution_policy"],allocation,seconds)[1] == allocation["authority_expires_at"]
    support_path=workload.EXAMPLE/"support.py"
    support=load_module("long_native_support",support_path)
    for mode in ("legacy_full", "selective_reuse"):
        # Both modes share genuine authenticated allocation and exact policy budgets.
        mode_selected=copy.deepcopy(selected);mode_selected["mode"]=mode
        mode_allocation=budget.allocation(mode_selected,fixture,mode,["unchanged-baseline"],
            fixture_case_id="unchanged-baseline",fixture_prepared_at=allocation["fixture_prepared_at"])
        document=workload._run_policy_document(support.policy,tmp_path,"Read retained sources",seconds,
            execution_policy=selected["execution_policy"],allocation_binding=mode_allocation)
        actual=RunPolicy(document)
        assert actual.to_dict()["schema_version"] == "research-run-policy/v4"
        assert actual.to_dict()["budgets"]["seconds"] == seconds
        from evidence_wiki import ExecutionRoleLimit
        from evidence_wiki.review_service_policy import ReviewServicePlan, ReviewServiceRoute
        def service_factory(root, objective):
            base=support.policy(root,objective).to_dict();base["policy_id"]=None
            routes=ReviewServicePlan(tuple(ReviewServiceRoute(row["profile_ref"],row["role"],(row["profile_ref"],),
                ExecutionRoleLimit("evaluator",row["controller_ref"],1,1,1000,"0",120)) for row in base["required_evaluators"]))
            base.update(schema_version="research-run-policy/v3",review_services=routes.document())
            return RunPolicy(base)
        service_document=workload._run_policy_document(service_factory,tmp_path,"Read retained sources",seconds,
            execution_policy=selected["execution_policy"],allocation_binding=mode_allocation)
        service_policy=RunPolicy(service_document)
        assert service_policy.to_dict()["schema_version"] == "research-run-policy/v4"
        assert service_policy.to_dict()["review_services"] == service_factory(tmp_path,"Read retained sources").to_dict()["review_services"]
    invalid=copy.deepcopy(allocation);invalid["authority_expires_at"]=(datetime.fromisoformat(invalid["authority_expires_at"])+timedelta(seconds=1)).isoformat()
    with pytest.raises(ValueError):
        provisioning_window(selected["execution_policy"],invalid,seconds)


def test_current_owner_schema_accepts_long_plan_and_old_exports_stay_closed():
    exports=accounting.schema_documents()
    assert exports["OwnerPlan"]["$defs"]["OwnerPlan"]["properties"]["elapsed_cap_seconds"]["maximum"] == 172800
    for name in accounting._PRE_OWNER_SCHEMA_DEFS:
        defs=exports[name]["$defs"]
        if "ExtendedExecutionPolicy" in defs:
            assert "forty-eight-hour" not in defs["ExtendedExecutionPolicy"]["properties"]["profile"]["enum"]
    assert "forty-eight-hour" in exports["OwnerPlan"]["$defs"]["ExtendedExecutionPolicy"]["properties"]["profile"]["enum"]


def test_long_run_policy_is_separate_and_closed(plan, monkeypatch):
    # Run policy construction has no native owner effects.
    from pathlib import Path
    support_path=Path(__file__).resolve().parents[1]/"examples/unattended-host/support.py"
    support=load_module("long_policy_support",support_path)
    monkeypatch.setattr("tools._execution_budget.provisioning_window",lambda *_:("start","end"))
    selected=binding()
    doc=workload._run_policy_document(support.policy,Path("/private/tmp/long-policy"),"Read sources",158159,execution_policy=selected["execution_policy"],allocation_binding=selected)
    policy=RunPolicy(doc)
    assert policy.to_dict()["schema_version"] == "research-run-policy/v4"
    assert policy.to_dict()["budgets"]["seconds"] == 158159
    assert policy.to_dict()["execution_lifetime"] == selected["execution_policy"]
    for version in ("research-run-policy/v1","research-run-policy/v2","research-run-policy/v3"):
        assert schemas()[version]["properties"]["budgets"]["properties"]["seconds"]["maximum"] == 86400
    for mutate in (lambda d:d.pop("execution_lifetime"), lambda d:d["budgets"].update(seconds=172801),lambda d:d["execution_lifetime"].update(renewal="allowed")):
        invalid=copy.deepcopy(doc);mutate(invalid)
        with pytest.raises(WorkflowContractError):RunPolicy(invalid)


@pytest.mark.parametrize("services", [False, True])
def test_long_policy_roundtrip_decisions_and_declared_service_binding(services):
    from evidence_wiki import (
        ExecutionProfile,
        ExecutionRole,
        ExecutionRoleLimit,
        ExecutionRoleRegistry,
        ExecutionRoleScope,
    )
    from evidence_wiki.review_service_policy import ReviewServicePlan, ReviewServiceRoute, approved_profiles
    from tests.test_run_policy_scope import native_policy_document, owner_observation
    document = native_policy_document()
    document.update(schema_version="research-run-policy/v4", execution_lifetime=execution_policy("forty-eight-hour"))
    document["budgets"].update(seconds=99038, model_calls=10, tool_calls=10, tokens=2000)
    service = ReviewServicePlan((ReviewServiceRoute("local-evaluator", "evaluator", ("local-evaluator",),
        ExecutionRoleLimit("evaluator", "isolated-controller", 2, 2, 1000, "0", 120)),))
    roles=(ExecutionRole("local-evaluator","evaluator","isolated-controller","local","prompt/v1"),)
    registry=ExecutionRoleRegistry((ExecutionRoleScope("local-evaluator","evaluator",("model",)),))
    profile=ExecutionProfile("bounded",4,"EUR","no-provider-charge",roles,2,2,1000,"0",role_scopes=registry,
        review_services=service if services else None)
    if services:document["review_services"]=service.document()
    policy=RunPolicy(document)
    assert RunPolicy.from_json(policy.to_json()).to_dict() == policy.to_dict()
    assert approved_profiles(policy.to_dict(),profile) == policy.to_dict()["required_evaluators"]
    observed=owner_observation(policy)
    decision=policy.evaluate(observed)
    assert decision["outcome"] == "partial" and not decision["authorized"]
    if services:
        changed=copy.deepcopy(document);changed["required_evaluators"][0]["controller_ref"]="foreign"
        with pytest.raises(WorkflowContractError):RunPolicy(changed)
    for version in ("research-run-policy/v1","research-run-policy/v2","research-run-policy/v3"):
        changed=copy.deepcopy(document);changed.pop("execution_lifetime");changed["schema_version"]=version
        if version=="research-run-policy/v1":changed["scope"].pop("source_scope_kind")
        with pytest.raises(WorkflowContractError):RunPolicy(changed)

from tests.test_review_workload_run_policy import cohort as cohort
from tests.test_review_workload_run_policy import pin as pin
from tests.test_review_workload_run_policy import real_policy as real_policy
from tools import _review_dependency_runner as runner


@pytest.mark.parametrize("mode", ["legacy_full", "selective_reuse"])
def test_formal_long_plan_current_pair_closing_and_real_runtime_policy(cohort, real_policy, tmp_path, monkeypatch, mode):
    from tests.test_prospective_control_ceiling import owner_long_formal
    plan,pair=owner_long_formal(cohort)
    policy=execution_policy("forty-eight-hour")
    observer=observation_policy("batch-extended")
    caps=copy.deepcopy(ceiling.OWNER_POLICY["formal_phase_ceilings"])
    caps["job"]=2400
    frozen=datetime.now(timezone.utc)-timedelta(seconds=1)
    plan.update(mode=mode,execution_policy=policy,execution_policy_sha256=workload.digest(policy),
        observation_policy=observer,observation_policy_sha256=workload.digest(observer),phase_caps=caps,
        formal_application_policy=native.formal_application_policy(caps["job"]),
        pair_closing_reservation=accounting.extended_closing_reservation(caps["global_close"]),
        frozen_at=frozen.isoformat(),expires_at=(frozen+timedelta(hours=48)).isoformat(),
        elapsed_cap_seconds=native.allocation_seconds(caps,24)+2*caps["global_close"])
    plan["protected_policy"]=pair["legacy_policy" if mode=="legacy_full" else "selective_policy"]
    plan["host_identity_pin"]=workload.retain(tmp_path/"host-long.json",{"host_id":"direct"})
    recipe={**ceiling.binding(ceiling.OWNER_PROFILE),"schema_version":pilot.OWNER_RECIPE,
        "journal_collection_policy":copy.deepcopy(native.JOURNAL_COLLECTION_POLICY),
        "formal_execution_policy":policy,"phase_caps":caps,"candidate_sha256":plan["candidate_sha256"],
        "review_producer_id":plan["review_producer_id"],
        "formal_application_policy":plan["formal_application_policy"],"group_overhead_cap_seconds":0}
    plan["measured_recipe_pin"]=workload.retain(tmp_path/"formal-long-recipe.json",recipe)
    monkeypatch.setattr(pilot,"completed_extended_pilot_measurements",lambda value,candidate: ({},0))
    accounting.validate_plan(plan,pair)
    assignments=[{**row["coordinate"],**{key:row[key] for key in ("original_id","controller_ref","execution_role","task_role","required","journal_key","native_run_id")},
        "attempt_id":None,"application_event_ref":None} for row in plan["obligations"]]
    allocated=runner._allocation_binding(plan,assignments)
    seconds=plan["elapsed_cap_seconds"]
    assert 86400 < seconds <= 172800
    document=workload._run_policy_document(real_policy,tmp_path,"Read retained sources",seconds,execution_policy=policy,allocation_binding=allocated)
    actual=RunPolicy(document)
    assert actual.to_dict()["schema_version"] == "research-run-policy/v4"
    assert actual.to_dict()["budgets"]["seconds"] == seconds
    report=accounting.initialize_report(plan,plan["pair_spec_pin"],pair)
    report["cost"]["elapsed_seconds"]=99038
    accounting.validate("Report",report)
    historical=copy.deepcopy(report)
    historical.update(ceiling.binding(ceiling.PROFILE),schema_version=accounting.LONG_REPORT)
    with pytest.raises(ValueError):accounting.validate("Report",historical)


def test_public_contract_advertises_explicit_service_policy_versions():
    from evidence_wiki import contract
    assert contract()["review_services"]["policy"] == "research-run-policy/v3 or v4 explicit approved alternatives"


def test_all_native_cases_long_watchdog_preserves_independent_fixture_authority(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from evidence_wiki import _review_producer
    from tools._review_dependency_case_contracts import CASE_IDS
    recipe,source=pinned_recipe(tmp_path,"batch-extended")
    recipe.update(ceiling.binding(ceiling.OWNER_PROFILE),schema_version=pilot.OWNER_RECIPE,
        journal_collection_policy=copy.deepcopy(native.JOURNAL_COLLECTION_POLICY),
        formal_execution_policy=execution_policy("forty-eight-hour"),group_overhead_cap_seconds=24129)
    recipe["phase_caps"].update(owner_read=3600,global_close=10800,mutation=4500)
    monkeypatch.setattr(pilot,"completed_extended_pilot_measurements",lambda *_:None)
    monkeypatch.setattr(evidence,"source_identity",lambda:source)
    monkeypatch.setattr(_review_producer,"capture",lambda:SimpleNamespace(producer_id=recipe["review_producer_id"]))
    monkeypatch.setattr(_review_producer,"assert_current",lambda *_:None)
    pin=workload.retain(tmp_path/"all-case-recipe.json",recipe)
    selected=budget.select(pin,"forty-eight-hour",fixture_document(),"selective_reuse",CASE_IDS,False,journal_collection=True)
    actual=budget.watchdog(selected,CASE_IDS)
    assert len(CASE_IDS)==99
    assert actual["fixture_reservations"]==101 and actual["shard_count"]==99
    assert actual["fixture_run_seconds"]==158159
    fixture_seconds=sum(actual[key] for key in ("fixture_run_seconds","fixture_overhead_seconds","cleanup_seconds"))
    assert fixture_seconds<=172800
    assert actual["watchdog_seconds"]==101*fixture_seconds+100*actual["outer_overhead_seconds"]
    assert 112*86400+100*10800 < actual["watchdog_seconds"] <= 112*172800+100*10800
    assert actual["authority_renewal"]==actual["run_budget_extension"]=="forbidden"
    allocation,_=allocated(selected)
    assert (datetime.fromisoformat(allocation["authority_expires_at"])-datetime.fromisoformat(allocation["authority_frozen_at"])).total_seconds()==172800


@pytest.mark.parametrize("profile,owner", [("eight-hour",True),("sixteen-hour",True),("twenty-four-hour",True),("forty-eight-hour",False)])
def test_watchdog_preserves_conservative_bound_outside_owner_long_selection(monkeypatch,profile,owner):
    # Isolate the arithmetic boundary; normal admission refuses these oversized allocations earlier.
    from tools._review_dependency_case_contracts import CASE_IDS
    selected={"selected_case_ids":CASE_IDS,"execution_policy":execution_policy(profile),
        **ceiling.binding(ceiling.OWNER_PROFILE if owner else ceiling.PROFILE)}
    monkeypatch.setattr(budget,"validate_selection",lambda _:dict(fixture_run_seconds=158159,
        fixture_overhead_seconds=10900,cleanup_seconds=60,outer_overhead_seconds=10800))
    with pytest.raises(ValueError):budget.watchdog(selected,CASE_IDS)


@pytest.mark.parametrize("mode", ["selective_reuse", "legacy_full"])
def test_day_expiry_uses_current_owner_selection_allocation_and_original_window(tmp_path, monkeypatch, mode):
    from types import SimpleNamespace

    from evidence_wiki import _review_producer
    from tools._review_dependency_case_contracts import REVIEW_TEMPORAL_FIXTURE, case_manifest, case_plan
    from tools._review_dependency_cases import _fixture_inputs

    recipe, _ = pinned_recipe(tmp_path, "batch-extended")
    source = evidence.source_identity()
    original, _ = native.read_pinned_artifact(recipe["pilot_plan_pin"])
    original["native_plan"]["candidate_sha256"] = source["candidate_sha256"]
    original["candidate_sha256"] = source["candidate_sha256"]
    original["native_plan_sha256"] = workload.digest(original["native_plan"])
    recipe.update(ceiling.binding(ceiling.OWNER_PROFILE), schema_version=pilot.OWNER_RECIPE,
        candidate_sha256=source["candidate_sha256"],
        pilot_plan_pin=workload.retain(tmp_path / "day-pilot-policy.json", original),
        journal_collection_policy=copy.deepcopy(native.JOURNAL_COLLECTION_POLICY),
        formal_execution_policy=execution_policy("forty-eight-hour"))
    recipe["phase_caps"].update(owner_read=3600, global_close=10800, mutation=4500)
    # Isolate original completed-reader and installed producer admission only.
    # Source identity, policy, selection, derived fixture and window validation stay real.
    monkeypatch.setattr(pilot, "completed_extended_pilot_measurements", lambda *_: None)
    generation = SimpleNamespace(producer_id=recipe["review_producer_id"])
    monkeypatch.setattr(_review_producer, "capture", lambda: generation)
    monkeypatch.setattr(_review_producer, "assert_current", lambda *_: None)
    recipe_pin = workload.retain(tmp_path / "day-recipe.json", recipe)
    selected = budget.select(recipe_pin, "forty-eight-hour", fixture_document(), mode,
        ["review-max-age-expired"], False, journal_collection=True)
    assert selected["schema_version"] == budget.OWNER_SELECTION
    assert selected["source"] == source
    assert source["case_manifest_sha256"] == workload.digest(case_manifest())
    expiry = next(row for row in case_manifest() if row["case_id"] == "review-max-age-expired")
    assert expiry["temporal_fixture"] == REVIEW_TEMPORAL_FIXTURE
    prepared = datetime.now(timezone.utc)
    fixture = _fixture_inputs(selected["fixture_input"], "review-max-age-expired", frozen_at=prepared,
        temporal_margin_seconds=selected["temporal_margin_seconds"],
        execution_profile=selected["execution_policy"]["profile"])
    allocation = budget.allocation(selected, fixture, mode, ["review-max-age-expired"],
        fixture_case_id="review-max-age-expired", fixture_prepared_at=prepared.isoformat())
    assert fixture["global_inputs"]["policy"]["max_review_age_seconds"] == 86400
    assert _fixture_inputs(selected["fixture_input"], "review-max-age-expired")["global_inputs"]["policy"]["max_review_age_seconds"] == 600
    assert allocation["fixture_sha256"] == workload.digest(fixture)
    assert allocation["native_case_selection"] == selected
    seconds = allocation["fixture_budget"]["fixture_run_seconds"]
    policy = selected["execution_policy"]
    budget.validate_allocation(policy, allocation, seconds, fixture=fixture, mode=mode, worker_plan=case_plan())
    begin, end = provisioning_window(policy, allocation, seconds)
    frozen = datetime.fromisoformat(allocation["authority_frozen_at"])
    expires = datetime.fromisoformat(end)
    assert frozen <= datetime.fromisoformat(begin) < expires
    assert end == allocation["authority_expires_at"]
    assert (expires - frozen).total_seconds() == 172800
    assert frozen + timedelta(seconds=86401) < expires
    assert policy["renewal"] == "forbidden"

    changed_fixture = copy.deepcopy(fixture)
    changed_fixture["global_inputs"]["policy"]["max_review_age_seconds"] = 600
    changed_allocation = copy.deepcopy(allocation)
    changed_allocation["fixture_sha256"] = workload.digest(changed_fixture)
    with pytest.raises(ValueError, match="native_case_extended_binding_invalid"):
        budget.validate_allocation(policy, changed_allocation, seconds,
            fixture=changed_fixture, mode=mode, worker_plan=case_plan())

    downgraded = copy.deepcopy(selected)
    downgraded["execution_policy"] = execution_policy("twenty-four-hour")
    downgraded["execution_policy_sha256"] = workload.digest(downgraded["execution_policy"])
    with pytest.raises(ValueError):
        budget.validate_selection(downgraded, current=True)
    changed_allocation = copy.deepcopy(allocation)
    changed_allocation["native_case_selection"] = downgraded
    for key in ("execution_policy", "execution_policy_sha256"):
        changed_allocation[key] = downgraded[key]
    changed_allocation["authority_expires_at"] = (frozen + timedelta(seconds=86400)).isoformat()
    with pytest.raises(ValueError):
        provisioning_window(downgraded["execution_policy"], changed_allocation, seconds)

    changed_allocation = copy.deepcopy(allocation)
    changed_allocation["native_case_selection"]["source"]["case_manifest_sha256"] = "0" * 64
    with pytest.raises(ValueError):
        budget.validate_allocation(policy, changed_allocation, seconds,
            fixture=fixture, mode=mode, worker_plan=case_plan())
