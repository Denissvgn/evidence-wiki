"""Closed prospective execution lifetimes, separate from observer recency."""

from datetime import datetime, timedelta, timezone

from tools import _review_control_ceiling as ceiling_policy


def execution_policy(profile):
    choices = {"two-hour": 7200, "four-hour": 14400, "six-hour": 21600,
               "eight-hour": 28800, "sixteen-hour": 57600, "twenty-four-hour": 86400, "forty-eight-hour": 172800}
    if type(profile) is not str or profile not in choices:
        raise ValueError("execution_policy_invalid")
    seconds = choices[profile]
    return {"schema_version": "review-execution-budget-policy/v2" if seconds > 21600 else "review-execution-budget-policy/v1", "profile": profile,
        "authority_seconds": seconds, "credential_lifetime_seconds": seconds,
        "deadline_basis": "frozen_plan_expiry", "renewal": "forbidden"}


def validate_execution_policy(policy):
    if (type(policy) is not dict or type(policy.get("authority_seconds")) is not int
            or type(policy.get("credential_lifetime_seconds")) is not int
            or policy != execution_policy(policy.get("profile"))):
        raise ValueError("execution_policy_invalid")
    return policy


def validate_execution_binding(policy, binding):
    from tools._observation_freshness import is_batch_policy, validate_observation_policy
    from tools._review_dependency_workload import digest

    policy = validate_execution_policy(policy)
    if (type(binding) is not dict or binding.get("execution_policy") != policy
            or binding.get("execution_policy_sha256") != digest(policy)):
        raise ValueError("execution_policy_binding_invalid")
    observer = validate_observation_policy(binding.get("observation_policy"))
    validate_execution_scope(policy, binding, observer)
    if (not is_batch_policy(observer)
            or binding.get("observation_policy_sha256") != digest(observer)):
        raise ValueError("execution_observation_profile_invalid")
    return policy


def validate_execution_scope(policy, binding, observation):
    """Long authority requires the current owner and extended original window."""
    validate_execution_policy(policy)
    if policy["profile"] == "forty-eight-hour":
        ceiling_policy.validate(binding, required=True)
        if (not ceiling_policy.owner(binding) or type(observation) is not dict
                or observation.get("profile") != "batch-extended"):
            raise ValueError("execution_runtime_binding_invalid")


def validate_execution_window(policy, frozen_at, expires_at, *, instant=None):
    policy = validate_execution_policy(policy)
    try:
        frozen, expires = (datetime.fromisoformat(value) for value in (frozen_at, expires_at))
        if (frozen.tzinfo is None or expires.tzinfo is None
                or expires - frozen != timedelta(seconds=policy["authority_seconds"])
                or instant is not None and not frozen <= instant < expires):
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("execution_authority_clock_invalid") from None
    return frozen, expires


def credential_expiries(expires_at):
    return dict.fromkeys(("trust", "usage", "signing", "service", "reference_qualification"), expires_at)


def _application_profile_sha256(profile):
    from evidence_wiki.agent_execution import _digest

    if (type(profile) is not dict or profile.get("schema_version") not in
            {"research-execution-profile/v16", "research-execution-profile/v17"}):
        raise ValueError("execution_runtime_binding_invalid")
    return _digest(profile)


def validate_runtime_allocation(allocation, plan, *, closing=None):
    from tools._review_dependency_workload import digest

    policy = validate_execution_binding(plan.get("execution_policy"), plan)
    frozen, expires = validate_execution_window(policy, plan["frozen_at"], plan["expires_at"])
    if type(allocation) is not dict or allocation.get("schema_version") != "review-workload-runtime-allocation/v2":
        raise ValueError("execution_runtime_binding_invalid")
    binding = allocation.get("allocation_binding")
    ceiling_policy.validate(binding, required=bool(ceiling_policy.validate(plan)))
    validate_execution_binding(policy, binding)
    expected = {**ceiling_policy.validate(plan), **{key: plan[key] for key in ("candidate_sha256", "phase_caps", "hard_limits", "source_protocol_limits",
        "execution_policy", "execution_policy_sha256", "observation_policy", "observation_policy_sha256")}}
    if "service_policy" in plan:
        from tools.run_review_dependency_workload import plan_phase_ceilings
        if plan["phase_caps"] != plan_phase_ceilings(plan):
            raise ValueError("execution_runtime_binding_invalid")
        profile = allocation.get("profile", {})
        lease = {**plan["service_policy"]["application_lease"], "not_after_utc": plan["expires_at"]}
        if (profile.get("schema_version") != plan["service_policy"]["execution_profile_schema"]
                or profile.get("review_application_lease") != lease
                or allocation.get("review_application_binding") != {"profile_sha256": _application_profile_sha256(profile), "lease": lease}):
            raise ValueError("execution_runtime_binding_invalid")
        expected["service_policy"] = plan["service_policy"]
        expected["service_selection"] = {"study": plan["study"], "host": plan["host"]["host_id"], "case_id": plan["case_id"], "mode": "selective"}
    from tools._review_journal_artifacts import selected as collection_selected
    if collection_selected(plan):
        expected["journal_collection_policy"] = plan["journal_collection_policy"]
    expected.update(plan_sha256=digest(plan), authority_frozen_at=plan["frozen_at"], authority_expires_at=plan["expires_at"])
    if "assignments" in plan:
        expected.update((key, plan[key]) for key in ("assignments", "group_overhead_cap_seconds", "control_run_id"))
    else:
        expected.update(study="closure_feasibility_diagnostic", run_ids=plan["run_ids"], review_jobs=0)
    if (not ceiling_policy.exact(binding, expected) if ceiling_policy.validate(plan) else binding != expected):
        raise ValueError("execution_runtime_binding_invalid")
    if (binding != expected or allocation.get("execution_policy") != policy
            or allocation.get("execution_policy_sha256") != digest(policy)
            or allocation.get("credential_expiries") != credential_expiries(plan["expires_at"])
            or allocation.get("authority_end") != plan["expires_at"]
            or allocation.get("run_policy", {}).get("budgets", {}).get("seconds") != plan["elapsed_cap_seconds"]):
        raise ValueError("execution_runtime_binding_invalid")
    if ceiling_policy.validate(plan):
        if (not ceiling_policy.exact(allocation.get("execution_policy"), policy)
                or type(allocation.get("run_policy", {}).get("budgets", {}).get("seconds")) is not int
                or "service_policy" in plan and (not ceiling_policy.exact(profile.get("review_application_lease"), lease)
                    or not ceiling_policy.exact(allocation.get("review_application_binding"), {"profile_sha256": _application_profile_sha256(profile), "lease": lease}))):
            raise ValueError("execution_runtime_binding_invalid")
    try:
        begin = datetime.fromisoformat(allocation["authority_start"])
        issued = datetime.fromisoformat(allocation["issued_before_worker_outputs"])
        if (begin.tzinfo is None or issued.tzinfo is None or not frozen <= begin <= issued < expires
                or not 0 < (expires - begin).total_seconds() <= policy["credential_lifetime_seconds"]
                or closing is not None and not begin <= closing < expires):
            raise ValueError
    except (ValueError, TypeError, KeyError):
        raise ValueError("execution_authority_clock_invalid") from None
    return allocation


def provisioning_window(policy, binding, run_seconds):
    from tools import run_review_dependency_workload as workload

    policy = validate_execution_binding(policy, binding)
    ceiling_policy.validate(binding)
    if binding.get("schema_version") in {"native-review-case-extended-allocation/v1", "native-review-case-extended-allocation/v2", "native-review-case-extended-allocation/v3", "native-review-case-extended-allocation/v4"}:
        from tools._review_dependency_case_budget import validate_allocation
        validate_allocation(policy, binding, run_seconds)
        instant = datetime.now(timezone.utc)
        return instant.isoformat(), binding["authority_expires_at"]
    if (policy["schema_version"] == "review-execution-budget-policy/v2"
            and not ({"service_policy", "formal_application_policy"} & binding.keys())):
        raise ValueError("execution_runtime_binding_invalid")
    if (binding.get("hard_limits") != workload.runtime_hard_limits(binding["observation_policy"], policy, phase_caps=binding["phase_caps"], ceiling_binding=binding)
            or binding.get("source_protocol_limits") != workload.HARD_LIMITS):
        raise ValueError("execution_runtime_binding_invalid")
    if ceiling_policy.validate(binding):
        limits = workload.runtime_hard_limits(binding["observation_policy"], policy, phase_caps=binding["phase_caps"], ceiling_binding=binding)
        if not ceiling_policy.exact(binding.get("hard_limits"), limits) or not ceiling_policy.exact(binding.get("source_protocol_limits"), workload.HARD_LIMITS):
            raise ValueError("execution_runtime_binding_invalid")
    if type(run_seconds) is not int or not 0 < run_seconds <= policy["authority_seconds"]:
        raise ValueError("execution_allocation_invalid")
    if "service_policy" in binding:
        if binding.get("service_selection") != {"study": "pilot", "host": "direct", "case_id": "independent-claim-text", "mode": "selective"}:
            raise ValueError("execution_runtime_binding_invalid")
        ceiling_policy.validate(binding, required=binding["service_policy"].get("profile") in {ceiling_policy.PROFILE, ceiling_policy.OWNER_PROFILE})
        if binding["service_policy"].get("profile") in {ceiling_policy.PROFILE, ceiling_policy.OWNER_PROFILE}:
            from tools._review_journal_artifacts import selected
            if not selected(binding):
                raise ValueError("execution_runtime_binding_invalid")
        workload.validate_service_policy(binding["service_policy"], **{key: binding["service_selection"][key] for key in ("study", "host", "case_id")},
            observation=binding.get("observation_policy"), execution=policy)
        caps = workload.observational_phase_ceilings(binding["service_policy"], binding["observation_policy"], policy)
        if (binding.get("phase_caps") != caps or binding.get("group_overhead_cap_seconds") != 0
                or run_seconds != workload.allocation_seconds(caps, 6)):
            raise ValueError("execution_runtime_binding_invalid")
    if "formal_application_policy" in binding:
        from tools._review_dependency_accounting import validate_plan
        from tools._review_dependency_runner import _allocation_binding, _budget_producer
        plan = validate_plan(binding["formal_plan"])
        if not ceiling_policy.exact(ceiling_policy.validate(binding), ceiling_policy.validate(plan)):
            raise ValueError("execution_runtime_binding_invalid")
        assignments = []
        for obligation in plan["obligations"]:
            assignments.append({**obligation["coordinate"],
                **{key: obligation[key] for key in ("original_id", "controller_ref", "execution_role", "task_role", "required", "journal_key", "native_run_id")},
                "attempt_id": None, "application_event_ref": None})
        expected_binding = _allocation_binding(plan, assignments)
        if (not ceiling_policy.exact(binding, expected_binding) if ceiling_policy.validate(plan) else binding != expected_binding) or run_seconds != plan["elapsed_cap_seconds"]:
            raise ValueError("execution_runtime_binding_invalid")
        workload.validate_formal_application_policy(binding["formal_application_policy"], policy, binding["phase_caps"])
        selection = binding.get("formal_service_selection", {})
        if (set(selection) != {"study", "host", "mode"} or selection["study"] != "full"
                or selection["host"] not in {"direct", "scoped_mcp"}
                or selection["mode"] not in {"legacy_full", "selective_reuse"} or "service_policy" in binding):
            raise ValueError("execution_runtime_binding_invalid")
        from tools._review_dependency_pilot import completed_extended_pilot_measurements
        recipe, _ = workload.read_pinned_artifact(binding["measured_recipe_pin"])
        completed_extended_pilot_measurements(recipe, binding["candidate_sha256"])
        if (not ceiling_policy.exact(ceiling_policy.validate(recipe), ceiling_policy.validate(binding))
                or recipe["formal_execution_policy"] != policy or recipe["phase_caps"] != binding["phase_caps"]
                or not _budget_producer(recipe, binding["candidate_sha256"], plan["review_producer_id"])
                or recipe["formal_application_policy"] != binding["formal_application_policy"]
                or recipe["group_overhead_cap_seconds"] != binding["group_overhead_cap_seconds"]):
            raise ValueError("execution_runtime_binding_invalid")
    instant = datetime.now(timezone.utc)
    _, expires = validate_execution_window(policy, binding.get("authority_frozen_at"),
        binding.get("authority_expires_at"), instant=instant)
    return instant.isoformat(), binding["authority_expires_at"]


def validate_workload_allocation_v5(actual_allocation, actual_v5_plan, authenticated_runtime, original_pins):
    """Match unchanged allocation/v2 against genuine current native selections."""
    import hashlib
    from pathlib import Path

    from evidence_wiki import HostRuntime
    from evidence_wiki.pack_discovery import owner
    from tools import run_review_dependency_workload as workload
    from tools._review_dependency_accounting import canonical, read_pin, require, validate_plan
    from tools._review_dependency_workload import EXAMPLE, digest, file_manifest

    plan = validate_plan(actual_v5_plan)
    require(type(authenticated_runtime) is HostRuntime, "execution_native_runtime_required")
    allocation = actual_allocation
    expected_keys = {"schema_version", "allocation_binding", "run_policy", "profile", "authority_raw_sha256", "authority_start", "authority_end",
        "realized_sources", "realized_claims_sha256", "realized_input_manifest_sha256", "worker_command_manifest", "issued_before_worker_outputs",
        "execution_policy", "execution_policy_sha256", "credential_expiries"}
    if "formal_application_policy" in plan:
        expected_keys.add("review_application_binding")
    require(type(allocation) is dict and set(allocation) == expected_keys and allocation["schema_version"] == "review-workload-runtime-allocation/v2",
            "execution_runtime_binding_invalid")
    policy = validate_execution_binding(plan["execution_policy"], plan)
    frozen, expires = validate_execution_window(policy, plan["frozen_at"], plan["expires_at"])
    limits = workload.runtime_hard_limits(plan["observation_policy"], policy, phase_caps=plan["phase_caps"], ceiling_binding=plan)
    require(read_pin(plan["source_protocol_limits_pin"], roots=original_pins["private_roots"], maximum=1_048_576)
            == canonical({"hard_limits": limits, "source_protocol_limits": workload.HARD_LIMITS}), "execution_runtime_limits_changed")
    assignments = []
    for obligation in plan["obligations"]:
        coordinate = obligation["coordinate"]
        assignments.append({**coordinate, **{key: obligation[key] for key in ("original_id", "controller_ref", "execution_role", "task_role", "required", "journal_key", "native_run_id")},
                            "attempt_id": None, "application_event_ref": None})
    expected = {**ceiling_policy.validate(plan), **{key: plan[key] for key in ("candidate_sha256", "phase_caps", "execution_policy", "execution_policy_sha256", "observation_policy", "observation_policy_sha256")}}
    expected.update(hard_limits=limits, source_protocol_limits=workload.HARD_LIMITS, plan_sha256=digest(plan), authority_frozen_at=plan["frozen_at"],
                    authority_expires_at=plan["expires_at"], assignments=assignments, group_overhead_cap_seconds=plan["group_overhead_cap_seconds"],
                    control_run_id=next(row["native_run_id"] for row in plan["journal_slots"] if row["kind"] == "changed_basis_control"))
    if "formal_application_policy" in plan:
        expected.update(formal_application_policy=plan["formal_application_policy"], measured_recipe_pin=plan["measured_recipe_pin"],
            formal_plan=plan,
            formal_service_selection={"study": "full", "host": workload.read_pinned_artifact(plan["host_identity_pin"])[0]["host_id"], "mode": plan["mode"]})
        selected = workload.validate_formal_application_policy(plan["formal_application_policy"], policy, plan["phase_caps"])
        lease = {**selected["application_lease"], "not_after_utc": plan["expires_at"]}
        require(allocation["profile"]["schema_version"] == selected["execution_profile_schema"]
                and allocation["profile"].get("review_application_lease") == lease
                and allocation.get("review_application_binding") == {"profile_sha256": _application_profile_sha256(allocation["profile"]), "lease": lease},
                "execution_runtime_binding_invalid")
    from tools._review_journal_artifacts import selected as collection_selected
    if collection_selected(plan):
        expected["journal_collection_policy"] = plan["journal_collection_policy"]
    if ceiling_policy.validate(plan):
        require(ceiling_policy.exact(allocation["allocation_binding"], expected), "execution_runtime_binding_invalid")
    require(allocation["allocation_binding"] == expected and allocation["execution_policy"] == policy
            and allocation["execution_policy_sha256"] == digest(policy)
            and allocation["credential_expiries"] == credential_expiries(plan["expires_at"]), "execution_runtime_binding_invalid")
    runtime, root = authenticated_runtime, Path(original_pins["root"])
    require(allocation["run_policy"] == runtime.policy.to_dict() and allocation["profile"] == runtime.execution.profile.document()
            and allocation["run_policy"]["budgets"]["seconds"] == plan["elapsed_cap_seconds"], "execution_native_configuration_changed")
    begin = datetime.fromisoformat(allocation["authority_start"])
    issued = datetime.fromisoformat(allocation["issued_before_worker_outputs"])
    require(begin.tzinfo is not None and issued.tzinfo is not None and frozen <= begin <= issued < expires
            and allocation["authority_end"] == plan["expires_at"], "execution_authority_clock_invalid")
    with runtime.context.activate():
        strict = owner("_strict_evidence")
        config = strict.configuration(root)
        selected = strict.resolve_policy(root, config)
        require(selected["schema_version"] == plan["protected_policy"]["schema_version"]
                and (selected.get("review_basis_mode") == "claim_dependencies_v1") == (plan["mode"] == "selective_reuse"), "execution_policy_mode_changed")
        capture = owner("_evidence_revision").capture_workspace(root)
        claims = owner("_strict_contract").claims_document(owner("_record_artifacts").json_document(capture.files[selected["claims_path"]]))
        require(digest(claims) == allocation["realized_claims_sha256"] and digest(file_manifest(root)) == allocation["realized_input_manifest_sha256"],
                "execution_native_input_changed")
        with owner("_evidence_usage").current_view(root, config) as live:
            qualified = owner("_publication_usage").qualify_capture(capture.files, config, live, purpose="qa-export", consumer="evidence-wiki")
            require(live.trust["content_hash"] == "sha256:" + allocation["authority_raw_sha256"]
                    and live.trust["policy"]["not_before"] == allocation["authority_start"]
                    and live.trust["policy"]["expires_at"] == allocation["authority_end"], "execution_native_authority_changed")
            for row in allocation["realized_sources"].values():
                require(qualified["selected"].get(row["source_id"]) == row["revision"]
                        and row["normalized_path"] == owner("_usage_gate").normalized_relative(config, row["source_id"]), "execution_native_source_changed")
    commands = runtime.execution.executor._commands
    require(set(commands) == {"generator", "evidence", "critic"} and set(allocation["worker_command_manifest"]) == {"evidence", "critic"}, "execution_native_commands_changed")
    for config_ref in ("evidence", "critic"):
        argv = commands[config_ref]
        require(allocation["worker_command_manifest"][config_ref] == {"argv": list(argv),
            "source_sha256": hashlib.sha256((EXAMPLE / "fake_workers.py").read_bytes()).hexdigest()}, "execution_native_commands_changed")
    workload.validate_credential_originals(original_pins["responses"], plan["expires_at"])
    return allocation
