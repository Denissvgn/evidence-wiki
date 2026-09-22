"""Deterministic, read-only setup compilation with revalidated local preconditions."""

from __future__ import annotations

from ._pack_io import canonical
from .frameworks import compatibility
from .planning_contracts import MAX_BYTES, PLAN, blocker, decode, digest, normalize, refuse
from .planning_inputs import installation_basis, selected_pack, target_basis
from .planning_profile import compile_profile, computation_plan, strict_plan
from .planning_questions import coverage_plan, intake_plan, intent
from .planning_sources import source_plan
from .source_commands import _no_secret_values


def plan_identity(plan):
    """Formatting and default provenance are not semantic setup preconditions."""
    value = {key: item for key, item in plan.items() if key not in {"plan_id", "decision_basis"}}
    return digest(value)


def compile_plan(raw):
    request, basis = normalize(decode(raw))
    payload, decisions = request["request"]["payload"], request["decisions"]
    blockers = []
    rows = intent(request, blockers)
    from .planning_authoring import authoring_action

    authoring = authoring_action(decisions["pack_authoring"], blockers)
    accepted = None
    if decisions["accepted_pack"] is not None:
        from .pack_acceptance import selection

        if decisions["pack_authoring"] is not None:
            refuse("/decisions/accepted_pack_conflicts_with_authoring")
        _, accepted = selection(decisions["accepted_pack"], payload["domain"])
        blockers.append(blocker("local_pack_domain_review_not_verified", questions=[row["id"] for row in rows], stage="release"))
    target, target_before = target_basis(payload["target"])
    payload["target"] = target_before["target"]
    allowed_roots = payload["authority"]["writable_roots"]
    from pathlib import Path

    roots = [Path(value).expanduser().resolve() for value in allowed_roots]
    if "local_setup" not in payload["authority"]["allowed_actions"] or not any(target != root and root in target.parents for root in roots):
        blockers.append(blocker("setup_authority_not_declared", field="/request/payload/authority"))
    installed_before = installation_basis()
    pack, metadata = selected_pack(payload["domain"])
    if metadata:
        required = metadata["selection"].get("required_scope_inputs") or []
        supplied = {row["name"] for row in payload["scope"]}
        for scope in required:
            if scope["id"] not in supplied:
                blockers.append(blocker("pack_required_scope_missing", questions=[row["id"] for row in rows], field="/request/payload/scope/" + scope["id"]))
    profile, config, policy = compile_profile(request, target, pack, blockers)
    questions = intake_plan(rows, config, blockers)
    strict = strict_plan(policy, decisions, profile, pack, blockers, [row["id"] for row in rows])
    computation = computation_plan(config)
    coverage = coverage_plan(request, questions, config, policy, metadata, computation, blockers)
    sources = source_plan(request, config, blockers)
    if sum(row.get("bytes", 0) for row in sources["local_inputs"]) > 33_554_432:
        refuse("planning_local_input_bytes_bound", "ONBOARDING_LIMIT")
    framework = None
    if decisions["framework"] is not None:
        choice = decisions["framework"]
        try:
            framework = compatibility(framework=choice["id"], version=choice["version"], mode=choice["mode"])
        except Exception:
            framework = {"selection": choice, "status": "unqualified"}
            blockers.append(blocker("framework_version_or_mode_unqualified", field="/decisions/framework"))
        framework.update(local_access="not_probed", bridge_acknowledgment_is_output=False, execution_authorized=False)
    if metadata and metadata.get("human_gated"):
        blockers.append(blocker("pack_human_review_required", questions=[row["id"] for row in rows], stage="release", field="/request/payload/domain/pack"))
    if metadata:
        strict["pack_review_requirements"] = metadata["selection"].get("review_requirements")
        strict["pack_manual_policies"] = metadata["human_review_policies"]
    if policy and policy["human_review"]:
        blockers.append(blocker("human_review_required", questions=[row["id"] for row in rows], stage="release"))
    if target_basis(payload["target"])[1] != target_before or installation_basis() != installed_before:
        refuse("planning_target_or_installation_changed", "ONBOARDING_PLAN_STALE")
    after_pack, _ = selected_pack(payload["domain"])
    if pack and after_pack.tree_sha256 != pack.tree_sha256:
        refuse("planning_pack_changed", "ONBOARDING_PLAN_STALE")
    source_check = source_plan(request, config, [])
    if source_check != sources:
        refuse("planning_source_inputs_changed", "ONBOARDING_PLAN_STALE")
    if accepted is not None and selection(decisions["accepted_pack"], payload["domain"])[1] != accepted:
        refuse("planning_accepted_pack_changed", "ONBOARDING_PLAN_STALE")
    steps = [
        {"id": "initialize", "operation": "initialize", "owner": "init_research_workspace", "depends_on": [],
         "mutations": ["target starter tree", "research.yml", "docs/research-requirements.json", "project guidance", "selected domain pack"]},
        {"id": "intake", "operation": "question_intake", "owner": "intake_questions", "depends_on": ["initialize"],
         "mutations": ["wiki/questions", "index.md", "log.md"]},
        {"id": "coverage", "operation": "coverage_setup", "owner": "coverage_manifest", "depends_on": ["intake"],
         "mutations": ["sources/coverage", "question coverage metadata"]},
        {"id": "sources", "operation": "source_request_intake_and_delivery", "owner": "source_requests", "depends_on": ["coverage"],
         "mutations": ["sources/source-requests.jsonl", "selected raw roots"], "requires_current_source_authority": True},
        {"id": "inventory", "operation": "inventory", "owner": "source_inventory", "depends_on": ["sources"], "mutations": ["sources/manifest.jsonl"]},
        {"id": "normalize", "operation": "normalize", "owner": "normalize_sources", "depends_on": ["inventory"], "mutations": ["sources/normalized"]},
        {"id": "validate", "operation": "doctor_smoke_lint", "owner": "workspace_checks", "depends_on": ["coverage", "normalize"],
         "mutations": [], "state": "not_run"},
    ]
    result = {"schema_version": PLAN, "plan_id": "0" * 64, "request": request, "request_sha256": digest(request),
        "decision_basis": basis, "bindings": {**installed_before, "target": target_before,
            "pack": {"selection": payload["domain"]["pack"], "tree_sha256": pack.tree_sha256} if pack else None,
            "inputs": sources["local_inputs"], "accepted_pack": accepted}, "profile": profile,
        "initialization": {"dry_run": "passed", "effective_config": config, "config_sha256": digest(config), "writes": False},
        "questions": questions, "coverage": coverage, "sources": sources, "strict": strict, "computation": computation,
        "framework": framework, "steps": steps, "blockers": blockers, "authoring_action": authoring,
        "setup_ready": not any(row["stage"] == "setup" for row in blockers), "research_ready": False, "actions_executed": False,
        "assumptions": payload["assumptions"], "open_decisions": payload["open_decisions"],
        "limitations": ["Read-only plan; no initializer writes, acquisition, checker execution, model calls, or accepted evidence.",
            "Caller decisions and references do not authenticate authority, reviewer independence, source truth, or access.",
            "Readiness is for setup only; all research/release blockers remain. No apply executor is exposed.",
            "Inputs are rechecked local observations, not a protected atomic host snapshot.",
            "Host must enforce aggregate bytes, wall-clock and tokens and revalidate current authority before execution."]}
    _no_secret_values(result)
    result["plan_id"] = plan_identity(result)
    if len(canonical(result)) + 1 > MAX_BYTES:
        refuse("planning_output_bound", "ONBOARDING_LIMIT")
    decode(canonical(result), PLAN)
    return result


def check_plan(raw):
    saved = decode(raw, PLAN)
    if plan_identity(saved) != saved["plan_id"]:
        refuse("saved_plan_identity_changed", "ONBOARDING_PLAN_STALE")
    try:
        current = compile_plan(canonical(saved["request"]))
    except (Exception, SystemExit):
        refuse("saved_plan_preconditions_unavailable", "ONBOARDING_PLAN_STALE")
    if current["plan_id"] != saved["plan_id"]:
        refuse("saved_plan_preconditions_changed", "ONBOARDING_PLAN_STALE")
    return {"schema_version": "evidence-setup-plan-check/v1", "plan_id": saved["plan_id"], "status": "current",
            "setup_ready": current["setup_ready"], "research_ready": False, "authority_verified": False, "actions_executed": False}
