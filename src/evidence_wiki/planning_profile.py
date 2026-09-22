"""Compile explicit research decisions through the standalone initializer owners."""

from __future__ import annotations

import copy
import io
import json
from contextlib import redirect_stdout

from ._script_host import shared_assets_root
from .agent_resources import resource_document
from .pack_discovery import owner
from .planning_contracts import blocker, digest, owned_call, refuse


def compile_profile(request, target, pack, blockers):
    payload, decisions = request["request"]["payload"], request["decisions"]
    init = owner("init_research_workspace")
    owned_call("/decisions/raw_roots", init.validate_source_roots, decisions["raw_roots"], "raw.source_roots")
    profile = json.loads(resource_document("example/init-profile/v1")["content"])["workspace_init"]
    profile.update(target_path=str(target), project={"name": decisions["project_name"], "description": payload["goal"],
        "owner_goal": payload["goal"], "language": decisions["language"]},
        raw={"immutable": True, "source_roots": decisions["raw_roots"]},
        outputs={"supported_formats": payload["outputs"]}, assumptions=payload["assumptions"] or ["No additional caller assumptions."],
        skipped_decisions=payload["open_decisions"] or ["Execution and evidence acceptance require their owning checks."],
        questions=[], frozen_requirements={"schema_version": "evidence-research-requirements/v1",
            "request": copy.deepcopy(request["request"]), "decisions": copy.deepcopy(decisions)})
    domain = payload["domain"]
    profile["domain_guidance"] = {"mode": domain["mode"], "rationale": domain["rationale"]}
    if domain["mode"] == "project_local":
        if decisions["project_local"] is None:
            blockers.append(blocker("project_local_guidance_missing", field="/decisions/project_local"))
            profile["domain_guidance"]["mode"] = "deferred"
        else:
            profile["domain_guidance"] = copy.deepcopy(decisions["project_local"])
            if profile["domain_guidance"].get("mode") != "project_local":
                refuse("/decisions/project_local/mode")
            profile["domain_guidance"]["rationale"] = domain["rationale"]
    elif decisions["project_local"] is not None:
        refuse("/decisions/project_local_requires_matching_domain")
    if domain["mode"] == "deferred":
        blockers.append(blocker("domain_decision_deferred", field="/request/payload/domain"))
    profile["domain_pack"] = {"enabled": True, "path": str(pack.root)} if pack else {"enabled": False}
    budgets = payload["budgets"]
    profile["run"] = {"max_questions_per_run": budgets["questions"],
        "max_source_requests_per_run": max(1, budgets["source_requests"]),
        "max_academic_provider_requests_per_run": max(1, budgets["source_requests"])}
    integrations = {"git": {"snapshot_user_edits": "explicit"},
        "codebase_analysis": {"enabled": False, "provider": "none", "read_only": True, "command": None},
        "retrieval": {"provider": "lexical", "command": None}}
    registry = owner("_provider_registry")
    for phase, builtin in (("discovery", registry.DISCOVERY_PROVIDER_IDS), ("acquisition", registry.ACQUISITION_PROVIDER_IDS)):
        requested = decisions[phase]
        permitted = phase in payload["authority"]["allowed_actions"]
        providers = []
        for provider in requested:
            if provider not in builtin or provider == "search":
                blockers.append(blocker("provider_adapter_qualification_required", field="/decisions/" + phase))
            elif not permitted or budgets["source_requests"] == 0 or budgets["bytes"] == 0 or phase == "acquisition" and budgets["downloads"] == 0:
                blockers.append(blocker("provider_authority_or_budget_missing", field="/decisions/" + phase))
            else:
                providers.append(provider)
        integrations[phase] = {"enabled": bool(providers), "providers": providers}
    integrations["acquisition"].update(target_root="raw/papers", max_downloads_per_run=max(1, budgets["downloads"]), require_license_check=True,
        web={"allowed_domains": decisions["allowed_domains"], "max_download_bytes": max(1, budgets["bytes"]), "target_root": "raw/web"},
        github={"max_archive_bytes": max(1, budgets["bytes"])}, registered={"max_download_bytes": max(1, budgets["bytes"])})
    if "web" in integrations["acquisition"]["providers"] and not decisions["allowed_domains"]:
        integrations["acquisition"]["providers"].remove("web")
        integrations["acquisition"]["enabled"] = bool(integrations["acquisition"]["providers"])
        blockers.append(blocker("web_allowed_domains_missing", field="/decisions/allowed_domains"))
    if decisions["codebase"] is not None:
        blockers.append(blocker("codebase_adapter_qualification_required", questions=decisions["codebase"]["question_ids"], field="/decisions/codebase"))
    destinations = {"arxiv": "raw/papers", "openalex": "raw/papers", "github": "raw/code", "web": "raw/web"}
    for provider in integrations["acquisition"]["providers"]:
        destination = destinations[provider]
        if not any(destination == root or destination.startswith(root.rstrip("/") + "/") for root in decisions["raw_roots"]):
            blockers.append(blocker("acquisition_root_not_in_inventory_scope", field="/decisions/raw_roots"))
    profile["integrations"] = integrations
    policy = None
    if decisions["mode"] == "strict":
        policy = copy.deepcopy(decisions["policy"])
        selected = payload.get("strict_evidence")
        if policy is None:
            policy = json.loads(resource_document("example/strict-policy/v1")["content"])
            if selected is not None:
                policy.update(policy_id=selected["policy_id"], revision=selected["policy_revision"], assurance=selected["assurance"])
        policy = owned_call("/decisions/policy", owner("_strict_contract").policy_document, policy)
        if selected is not None and (policy["policy_id"], policy["revision"], policy["assurance"]) != (
            selected["policy_id"], selected["policy_revision"], selected["assurance"]):
            refuse("/decisions/policy_selection_mismatch")
        profile["strict_evidence"] = policy
        if decisions["trust"] is not None:
            profile["evidence_trust"] = decisions["trust"]
    elif decisions["policy"] is not None or decisions["trust"] is not None:
        refuse("/decisions/legacy_policy_conflict")
    if decisions["computation"] is not None:
        profile["computation"] = decisions["computation"]
    owned_call("/request/payload/domain", init.validate_domain_decision, profile)
    owned_call("/profile/workspace_init", init.validate_profile, profile)
    options = init.InitOptions(starter_root=shared_assets_root() / "workspace-template", target=target,
        project_name=decisions["project_name"], project_description=payload["goal"], owner_goal=payload["goal"],
        language=decisions["language"], domain_pack=str(pack.root) if pack else None,
        profile_path=None, profile=profile, dry_run=True, force=False)
    selection = owned_call("/request/payload/domain/pack", init.resolve_domain_pack, options.domain_pack, options.starter_root)
    config = owned_call("/profile/effective_configuration", init.build_config, options, selection)
    normalization = owned_call("/profile/normalization", owner("_normalization_config").normalization_config, config)
    if normalization["adapters"]:
        blockers.append(blocker("normalization_adapter_qualification_required", field="/request/payload/domain/pack"))
    # A selected overlay may contribute optional declarations. Validate the merged
    # result, then freeze it in the profile so an explicit replay has the same policy.
    merged_policy = config.get("strict_evidence")
    if decisions["mode"] == "legacy" and merged_policy is not None:
        refuse("legacy_selected_pack_requires_strict_policy")
    if policy is not None and merged_policy != policy:
        profile["strict_evidence"] = owned_call("/profile/strict_evidence", owner("_strict_contract").policy_document, merged_policy)
        policy = profile["strict_evidence"]
    with redirect_stdout(io.StringIO()):
        owned_call("/profile/initializer_dry_run", init.initialize_workspace, options)
    return {"workspace_init": profile}, config, policy


def strict_plan(policy, decisions, profile, pack, blockers, question_ids):
    if policy is None:
        return {"mode": "legacy", "requested_assurance": None, "effective_assurance": None,
                "evidence_accepted": False, "reason": "explicit legacy selection; no strict acceptance claim"}
    from ._pack_io import read_file

    for relative, expected in policy["instructions"].items():
        if relative == "docs/research-requirements.json":
            raw = owner("init_research_workspace").frozen_requirements_bytes(profile["workspace_init"])
        elif pack and relative.startswith("domain-packs/" + pack.root.name + "/"):
            raw = pack.files.get(relative.removeprefix("domain-packs/" + pack.root.name + "/"))
        else:
            path = shared_assets_root() / "workspace-template" / relative
            raw = read_file(shared_assets_root() / "workspace-template", relative) if path.is_file() else None
        import hashlib

        if raw is None or expected != "sha256:" + hashlib.sha256(raw).hexdigest():
            blockers.append(blocker("instruction_identity_unavailable", questions=question_ids, field="/decisions/policy/instructions"))
    blockers.append(blocker("trusted_independent_review_required", questions=question_ids, stage="release", field="/decisions/reviewer_reference"))
    blockers.append(blocker("verification_authority_not_verified", questions=question_ids, stage="release", field="/decisions/trust"))
    if policy["assurance"] == "host_enforced":
        blockers.append(blocker("protected_host_not_verified", questions=question_ids, stage="release", field="/decisions/host_reference"))
    return {"mode": "strict", "requested_assurance": policy["assurance"], "effective_assurance": None,
        "policy": policy, "policy_sha256": digest(policy), "rubric_sha256": digest(policy["rubric"]),
        "require_coverage": True, "require_grounding": True, "human_review": policy["human_review"],
        "authority_reference": decisions["trust"], "reviewer_reference": decisions["reviewer_reference"],
        "host_reference": decisions["host_reference"], "authority_verified": False, "evidence_accepted": False,
        "checkers": ["strict check", "strict prepare-review", "strict review", "strict export"],
        "policy_enforcement": "strict owner forces coverage and grounding; host enforcement requires protected external policy"}


def computation_plan(config):
    definition = owned_call("/decisions/computation", owner("_computation_runtime").load_definition, config)
    if definition is None:
        return None
    contract = owner("_computation_contract")
    _, zone = owner("_computation_schedule").zone_identity(definition["clock"]["timezone"])
    return {"definition": definition, "definition_id": contract.definition_id(definition),
        "configuration_id": contract.configuration_id(config),
        "engine_id": owner("_selected_publication").producer_identity(), "timezone": zone,
        "arithmetic": definition["arithmetic"], "clock": definition["clock"], "executed": False,
        "result_id": None, "inputs": "usable normalized structured records required at execution",
        "checkers": ["computation check", "computation verify"], "dispatch_authorized": False}
