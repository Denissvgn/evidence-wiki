"""Opt-in prospective native-case allocation, separate from study qualification."""

from datetime import datetime, timedelta, timezone

from tools import _review_control_ceiling as ceiling_policy
from tools._execution_budget import execution_policy, validate_execution_binding, validate_execution_window
from tools._review_dependency_case_contracts import CASE_IDS, case_plan, validate_case_plan
from tools._review_dependency_workload import digest

SELECTION = "native-review-case-extended-selection/v1"
ALLOCATION = "native-review-case-extended-allocation/v1"
WATCHDOG = "native-review-case-watchdog/v2"
INDEX = "native-review-case-index/v2"
COLLECTION_SELECTION = "native-review-case-extended-selection/v2"
COLLECTION_ALLOCATION = "native-review-case-extended-allocation/v2"
COLLECTION_INDEX = "native-review-case-index/v3"
LONG_SELECTION = "native-review-case-extended-selection/v3"
OWNER_SELECTION = "native-review-case-extended-selection/v4"
LONG_ALLOCATION = "native-review-case-extended-allocation/v3"
OWNER_ALLOCATION = "native-review-case-extended-allocation/v4"
LONG_INDEX = "native-review-case-index/v4"
OWNER_INDEX = "native-review-case-index/v5"
from tools._review_journal_artifacts import POLICY as JOURNAL_COLLECTION_POLICY
from tools._review_journal_artifacts import selected as collection_selected


def _require(value):
    if not value:
        raise ValueError("native_case_extended_binding_invalid")


def fixture_budget(recipe, execution):
    """Reserve a baseline and fresh cohort plus bounded owner/reference work."""
    from tools._review_dependency_pilot import measured_observation_policy
    from tools.run_review_dependency_workload import runtime_hard_limits, validate_formal_application_policy
    prospective = recipe.get("schema_version") in {"selective-complete-pilot-measurement/v4", "selective-complete-pilot-measurement/v5"}
    ceiling_policy.validate(recipe, required=prospective)
    _require(not prospective or collection_selected(recipe))
    caps = recipe["phase_caps"]
    validate_formal_application_policy(recipe["formal_application_policy"], execution, caps)
    runtime_hard_limits(measured_observation_policy(recipe), execution, phase_caps=caps, ceiling_binding=recipe)
    # Initial provisioning and the one possible requalification each receive
    # the authenticated setup cap. Individual worker deadlines remain unchanged.
    reserves = {"service_applications": 24, "reference_jobs": 400,
        "reference_host_seconds": 30, "setup_passes": 2, "mutation_passes": 2, "owner_passes": 24,
        "closing_passes": 2}
    run = (reserves["setup_passes"] * caps["setup"] + reserves["service_applications"] * caps["job"]
        + reserves["mutation_passes"] * caps["mutation"]
        + reserves["owner_passes"] * caps["owner_read"]
        + reserves["closing_passes"] * caps["global_close"]
        + caps["cleanup"] + recipe["group_overhead_cap_seconds"])
    overhead = caps["setup"] + caps["global_close"]
    cleanup = max(caps["cleanup"], 60)
    _require(type(run) is int and run + overhead + cleanup <= execution["authority_seconds"])
    return dict(reserves, fixture_run_seconds=run, fixture_overhead_seconds=overhead,
        cleanup_seconds=cleanup, outer_overhead_seconds=caps["global_close"])


def select(recipe_pin, profile, fixture, mode, cases, refresh, *, temporal_margin_seconds=None, journal_collection=False):
    """Authenticate complete current measurements before freezing a selection."""
    from tools._review_dependency_case_evidence import source_identity
    from tools._review_dependency_pilot import measured_observation_policy
    from tools.run_review_dependency_workload import read_pinned_artifact
    recipe, _ = read_pinned_artifact(recipe_pin)
    observation = measured_observation_policy(recipe)
    execution = execution_policy(profile)
    source = source_identity()
    prospective = recipe.get("schema_version") in {"selective-complete-pilot-measurement/v4", "selective-complete-pilot-measurement/v5"}
    value = {**ceiling_policy.validate(recipe, required=prospective), "schema_version": SELECTION, "measured_recipe_pin": recipe_pin,
        "candidate_sha256": source["candidate_sha256"], "source": source,
        "review_producer_id": recipe["review_producer_id"], "fixture_sha256": digest(fixture), "fixture_input": fixture,
        "mode": mode, "selected_case_ids": list(cases), "refresh": refresh,
        "native_case_plan": case_plan(), "execution_policy": execution,
        "execution_policy_sha256": digest(execution), "observation_policy": observation,
        "observation_policy_sha256": digest(observation),
        "phase_caps": recipe["phase_caps"], "formal_application_policy": recipe["formal_application_policy"],
        "temporal_margin_seconds": temporal_margin_seconds}
    _require(type(journal_collection) is bool and collection_selected(recipe) == journal_collection)
    if journal_collection:
        value.update(schema_version=COLLECTION_SELECTION, journal_collection_policy=dict(JOURNAL_COLLECTION_POLICY))
    if prospective:
        _require(journal_collection)
        value["schema_version"] = ceiling_policy.version(LONG_SELECTION, recipe)
    validate_selection(value, current=True)
    return value


def validate_selection(value, *, current=False):
    """Byte validation stays separate from original current-owner authentication."""
    from tools.run_review_dependency_workload import read_pinned_artifact
    keys = {"schema_version", "measured_recipe_pin", "candidate_sha256", "source", "review_producer_id",
        "fixture_sha256", "fixture_input", "mode", "selected_case_ids", "refresh", "native_case_plan", "execution_policy",
        "execution_policy_sha256", "observation_policy", "observation_policy_sha256", "phase_caps",
        "formal_application_policy", "temporal_margin_seconds"}
    prospective = type(value) is dict and value.get("schema_version") in {LONG_SELECTION, OWNER_SELECTION}
    keys.update(ceiling_policy.validate(value, required=prospective))
    collection = type(value) is dict and value.get("schema_version") in {COLLECTION_SELECTION, LONG_SELECTION, OWNER_SELECTION}
    if collection:
        keys.add("journal_collection_policy")
    _require(type(value) is dict and set(value) == keys and value["schema_version"] in {SELECTION, COLLECTION_SELECTION, LONG_SELECTION, OWNER_SELECTION}
        and collection_selected(value) == collection)
    execution = validate_execution_binding(value["execution_policy"], value)
    _require(execution["profile"] in {"eight-hour", "sixteen-hour", "twenty-four-hour", "forty-eight-hour"})
    cases = value["selected_case_ids"]
    _require(type(cases) is list and cases and len(cases) == len(set(cases))
        and set(cases) <= set(CASE_IDS) and value["mode"] in {"selective_reuse", "legacy_full"}
        and type(value["refresh"]) is bool)
    validate_case_plan(value["native_case_plan"])
    for name in ("fixture_sha256", "candidate_sha256"):
        _require(type(value[name]) is str and len(value[name]) == 64
            and set(value[name]) <= set("0123456789abcdef"))
    _require(digest(value["fixture_input"]) == value["fixture_sha256"])
    from tools._review_dependency_pilot import measured_observation_policy
    recipe, _ = read_pinned_artifact(value["measured_recipe_pin"])
    observation = measured_observation_policy(recipe)
    _require(ceiling_policy.exact(value["observation_policy"], observation)
        and value["observation_policy_sha256"] == digest(observation))
    _require(recipe.get("schema_version") == (ceiling_policy.version("selective-complete-pilot-measurement/v4", value) if prospective else "selective-complete-pilot-measurement/v3" if collection else "selective-complete-pilot-measurement/v2")
        and collection_selected(recipe) == collection
        and recipe.get("candidate_sha256") == value["candidate_sha256"]
        and recipe.get("formal_execution_policy") == execution
        and recipe.get("review_producer_id") == value["review_producer_id"]
        and recipe.get("phase_caps") == value["phase_caps"]
        and recipe.get("formal_application_policy") == value["formal_application_policy"])
    _require(ceiling_policy.exact(ceiling_policy.validate(recipe, required=prospective), ceiling_policy.validate(value)))
    _require(not prospective or ceiling_policy.exact(recipe.get("phase_caps"), value["phase_caps"])
        and ceiling_policy.exact(recipe.get("formal_application_policy"), value["formal_application_policy"]))
    budget = fixture_budget(recipe, execution)
    margin = value["temporal_margin_seconds"]
    _require(margin is None or type(margin) is int
        and budget["fixture_run_seconds"] + budget["fixture_overhead_seconds"] <= margin
        < execution["authority_seconds"] - budget["cleanup_seconds"])
    if current:
        from evidence_wiki import _review_producer
        from tools._review_dependency_case_evidence import source_identity
        from tools._review_dependency_pilot import completed_extended_pilot_measurements
        _require(value["source"] == source_identity()
            and value["source"]["candidate_sha256"] == value["candidate_sha256"])
        completed_extended_pilot_measurements(recipe, value["candidate_sha256"])
        generation = _review_producer.capture()
        _require(generation.producer_id == value["review_producer_id"])
        _review_producer.assert_current(generation)
    return budget


def watchdog(selection, cases):
    """Aggregate independent fixture lifetimes; never extend an entered fixture."""
    budget = validate_selection(selection)
    _require(list(cases) and set(cases) <= set(selection["selected_case_ids"]))
    negative = {"source-effective-interval-not-started", "source-record-bound"}
    fixtures = len(cases) + sum(case in negative for case in cases)
    seconds = fixtures * (budget["fixture_run_seconds"] + budget["fixture_overhead_seconds"]
        + budget["cleanup_seconds"]) + (len(cases) + 1) * budget["outer_overhead_seconds"]
    authority_cap = (172800 if ceiling_policy.owner(selection)
        and selection["execution_policy"]["profile"] == "forty-eight-hour" else 86400)
    _require(0 < seconds <= 112 * authority_cap + 100 * 10800)
    return dict(budget, schema_version=WATCHDOG, fixture_reservations=fixtures,
        watchdog_seconds=seconds, shard_count=len(cases) if len(cases) > 1 else 0,
        authority_renewal="forbidden", run_budget_extension="forbidden")


def allocation(selection, fixture, mode, cases, *, fixture_case_id, fixture_prepared_at):
    """Mint authority once, solely for a genuinely new fixture."""
    from tools.run_review_dependency_workload import HARD_LIMITS, runtime_hard_limits
    budget = validate_selection(selection, current=True)
    _require(set(cases) <= set(selection["selected_case_ids"]) and mode in {"selective_reuse", "legacy_full"})
    frozen = datetime.now(timezone.utc)
    execution = selection["execution_policy"]
    expires = frozen + timedelta(seconds=execution["authority_seconds"])
    return {**ceiling_policy.validate(selection), "schema_version": ceiling_policy.version(LONG_ALLOCATION, selection) if selection["schema_version"] in {LONG_SELECTION, OWNER_SELECTION} else COLLECTION_ALLOCATION if collection_selected(selection) else ALLOCATION, "native_case_selection": selection,
        **({"journal_collection_policy": dict(JOURNAL_COLLECTION_POLICY)} if collection_selected(selection) else {}),
        "fixture_sha256": digest(fixture), "fixture_case_id": fixture_case_id,
        "fixture_prepared_at": fixture_prepared_at, "native_case_plan": case_plan(), "native_case_mode": mode,
        "native_case_ids": list(cases), "candidate_sha256": selection["candidate_sha256"],
        "execution_policy": execution, "execution_policy_sha256": digest(execution),
        "observation_policy": selection["observation_policy"],
        "observation_policy_sha256": selection["observation_policy_sha256"],
        "phase_caps": selection["phase_caps"], "formal_application_policy": selection["formal_application_policy"],
        "hard_limits": runtime_hard_limits(selection["observation_policy"], execution, phase_caps=selection["phase_caps"], ceiling_binding=selection),
        "source_protocol_limits": HARD_LIMITS, "authority_frozen_at": frozen.isoformat(),
        "authority_expires_at": expires.isoformat(), "fixture_budget": budget}


def validate_allocation(policy, binding, run_seconds, *, fixture=None, mode=None, worker_plan=None):
    from tools.run_review_dependency_workload import HARD_LIMITS, runtime_hard_limits
    keys = {"schema_version", "native_case_selection", "fixture_sha256", "native_case_plan", "native_case_mode",
        "native_case_ids", "fixture_case_id", "fixture_prepared_at", "candidate_sha256", "execution_policy", "execution_policy_sha256", "observation_policy",
        "observation_policy_sha256", "phase_caps", "formal_application_policy", "hard_limits", "source_protocol_limits",
        "authority_frozen_at", "authority_expires_at", "fixture_budget"}
    prospective = type(binding) is dict and binding.get("schema_version") in {LONG_ALLOCATION, OWNER_ALLOCATION}
    keys.update(ceiling_policy.validate(binding, required=prospective))
    collection = type(binding) is dict and binding.get("schema_version") in {COLLECTION_ALLOCATION, LONG_ALLOCATION, OWNER_ALLOCATION}
    if collection:
        keys.add("journal_collection_policy")
    _require(type(binding) is dict and set(binding) == keys and binding["schema_version"] in {ALLOCATION, COLLECTION_ALLOCATION, LONG_ALLOCATION, OWNER_ALLOCATION}
        and collection_selected(binding) == collection)
    selection = binding["native_case_selection"]
    _require((selection.get("schema_version") in {LONG_SELECTION, OWNER_SELECTION}) == prospective
        and ceiling_policy.exact(ceiling_policy.validate(selection, required=prospective), ceiling_policy.validate(binding)))
    _require(collection_selected(selection) == collection)
    budget = validate_selection(selection, current=True)
    validate_execution_binding(policy, binding)
    _require(all(binding[key] == selection[key] for key in ("candidate_sha256", "execution_policy", "phase_caps",
        "formal_application_policy", "observation_policy", "observation_policy_sha256", "execution_policy_sha256")))
    validate_case_plan(binding["native_case_plan"])
    if prospective:
        _require(all(ceiling_policy.exact(binding[key], selection[key]) for key in ("candidate_sha256", "execution_policy", "phase_caps",
            "formal_application_policy", "observation_policy", "observation_policy_sha256", "execution_policy_sha256"))
            and ceiling_policy.exact(binding["fixture_budget"], budget)
            and ceiling_policy.exact(binding["source_protocol_limits"], HARD_LIMITS)
            and ceiling_policy.exact(binding["hard_limits"], runtime_hard_limits(binding["observation_policy"], policy,
                phase_caps=binding["phase_caps"], ceiling_binding=binding)))
    _require(binding["fixture_budget"] == budget and type(run_seconds) is int and run_seconds == budget["fixture_run_seconds"]
        and binding["hard_limits"] == runtime_hard_limits(binding["observation_policy"], policy, phase_caps=binding["phase_caps"], ceiling_binding=binding)
        and binding["source_protocol_limits"] == HARD_LIMITS
        and binding["native_case_ids"] and set(binding["native_case_ids"]) <= set(selection["selected_case_ids"])
        and binding["native_case_mode"] in {"selective_reuse", "legacy_full"})
    from tools._review_dependency_cases import _fixture_inputs, _initial_mode
    case_id = binding["fixture_case_id"]
    selected = binding["native_case_ids"]
    _require(len(selected) == 1 and (case_id is None or case_id == selected[0]))
    negative = {"source-effective-interval-not-started", "source-record-bound"}
    _require(case_id is not None or selected[0] in negative)
    _require(binding["native_case_mode"] == (selection["mode"] if case_id in negative
        else _initial_mode(selected[0], selection["mode"])))
    prepared = datetime.fromisoformat(binding["fixture_prepared_at"])
    _require(prepared.tzinfo is not None)
    derived = _fixture_inputs(selection["fixture_input"], case_id,
        temporal_margin_seconds=selection["temporal_margin_seconds"], frozen_at=prepared)
    _require(binding["fixture_sha256"] == digest(derived))
    if case_id in {"unused-source-expired", "source-effective-interval-ended"}:
        boundary = prepared + timedelta(seconds=selection["temporal_margin_seconds"] or 1800)
        _require(datetime.now(timezone.utc) + timedelta(seconds=budget["fixture_run_seconds"]) < boundary)
    if fixture is not None:
        _require(binding["fixture_sha256"] == digest(fixture) and binding["native_case_mode"] == mode
            and binding["native_case_plan"] == worker_plan)
    validate_execution_window(policy, binding["authority_frozen_at"], binding["authority_expires_at"], instant=datetime.now(timezone.utc))
    return budget
