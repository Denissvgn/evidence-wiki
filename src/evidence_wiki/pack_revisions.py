"""Revision plans and resumable advice over existing pack and evidence owners."""

from __future__ import annotations

from pathlib import Path

from ._pack_io import canonical, capture_pack, read_file, refuse, yaml_document
from ._script_host import shared_assets_root
from .domain_pack_lifecycle import _validate_candidate
from .pack_discovery import owner
from .pack_revision_contracts import PLAN, REQUEST, decode


def _candidate(request):
    qualification = None
    if request["path"] is not None:
        if request["catalog"] is not None or request["revision"] is not None:
            refuse("revision_candidate_selection_ambiguous")
        path = Path(request["path"]).expanduser().absolute()
    else:
        from .pack_acceptance import selection
        from .pack_catalog import _read

        if request["catalog"] is None or request["revision"] is None:
            refuse("revision_candidate_selection_required")
        catalog = Path(request["catalog"]).expanduser().absolute()
        value = _read(catalog)
        record = value["revisions"].get(request["revision"], {})
        if not record.get("assessment"):
            refuse("revision_assessment_missing")
        pack, qualification = selection({"catalog": str(catalog), "revision": request["revision"], "assessment_sha256": record["assessment"]})
        path = Path(pack["locator"])
    return path, qualification


def plan(request):
    request = decode(canonical(request), REQUEST)
    if not request["rationale"].strip():
        refuse("revision_rationale_required")
    target = Path(request["target"]).expanduser().absolute()
    if target.is_symlink():
        refuse("revision_target_link")
    root = target.resolve(strict=True)
    candidate, qualification = _candidate(request)
    lifecycle = owner("_domain_pack_lifecycle")
    validated = _validate_candidate(candidate, assets=shared_assets_root(), lifecycle=lifecycle)
    result = lifecycle.run_refresh(root, candidate, dry_run=True, keep_local=request["keep_local"], accept_pack=request["accept_pack"],
        validated_candidate_fingerprint=validated, rationale=request["rationale"], qualification=qualification)
    value = {"schema_version": PLAN, "request": request, "candidate_sha256": validated,
             "qualification": qualification, "owner_plan": result}
    value["plan_id"] = owner("_pack_revision_impact").digest(value)
    return decode(canonical(value), PLAN)


def verify_replay(value):
    """An operation label or matching history ID cannot substitute for current inputs."""
    if value["plan_id"] != owner("_pack_revision_impact").digest({k: v for k, v in value.items() if k != "plan_id"}):
        refuse("revision_replay_plan_changed", "ONBOARDING_PLAN_STALE")
    if "generation" in value:
        from .runtime_identity import generation

        if value["generation"] != generation():
            refuse("revision_replay_generation_changed", "ONBOARDING_PLAN_STALE")
    request, proposed = value["request"], value["owner_plan"]
    target = Path(request["target"]).expanduser().absolute()
    candidate, qualification = _candidate(request)
    lifecycle = owner("_domain_pack_lifecycle")
    state = lifecycle.load_state(target)
    if (proposed.get("target") != str(target.resolve()) or qualification != value["qualification"]
            or capture_pack(candidate).tree_sha256 != value["candidate_sha256"]
            or state["pack"]["tree_sha256"] != value["candidate_sha256"]
            or lifecycle.inspect_workspace(target)["state"] != "current"):
        refuse("revision_replay_inputs_changed", "ONBOARDING_PLAN_STALE")
    revision_id = proposed.get("revision_id")
    if revision_id is None:
        if "mappings" in request or proposed.get("status") != "no_changes" or proposed.get("changes") != []:
            refuse("revision_replay_no_change_proof_missing", "ONBOARDING_PLAN_STALE")
        return None
    records = state.get("research_revisions", [])
    if (not records or records[-1]["revision_id"] != revision_id or records[-1]["to"] != state["pack"]
            or records[-1]["rationale"] != request["rationale"] or records[-1]["qualification"] != qualification
            or records[-1]["impact"] != proposed.get("impact")):
        refuse("revision_replay_history_changed", "ONBOARDING_PLAN_STALE")
    migration = records[-1]["impact"].get("identity_migration")
    if (migration is not None) != ("mappings" in request):
        refuse("revision_replay_operation_changed", "ONBOARDING_PLAN_STALE")
    if migration is not None and (not isinstance(migration, dict) or migration.get("mappings") != request["mappings"]
            or migration.get("resolutions") != {"keep_local": sorted(set(request["keep_local"])), "accept_pack": sorted(set(request["accept_pack"]))}):
        refuse("migration_replay_intent_changed", "ONBOARDING_PLAN_STALE")
    return records[-1]


def apply(raw):
    from .pack_catalog import _outside_assets

    value = decode(raw, PLAN)
    if value["plan_id"] != owner("_pack_revision_impact").digest({k: v for k, v in value.items() if k != "plan_id"}):
        refuse("revision_plan_identity_changed", "ONBOARDING_PLAN_STALE")
    request = value["request"]
    candidate, qualification = _candidate(request)
    if qualification != value["qualification"]:
        refuse("revision_qualification_changed", "ONBOARDING_PLAN_STALE")
    target = Path(request["target"]).expanduser().absolute()
    _outside_assets(target, additional_roots=(Path(__file__).parent,))
    lifecycle = owner("_domain_pack_lifecycle")
    if not (target / "domain-packs/.evidence-wiki-transaction.yml").exists():
        state = lifecycle.load_state(target)
        recorded = state.get("research_revisions", [])
        if recorded and recorded[-1]["revision_id"] == value["owner_plan"].get("revision_id"):
            verify_replay(value)
            return {"status": "already_applied", "revision_id": recorded[-1]["revision_id"], "research": status(target)}
    if not value["owner_plan"].get("revision_plan_id"):
        refuse("revision_plan_binding_missing")

    def validate(path):
        current, binding = _candidate(request)
        if current != candidate or binding != qualification:
            refuse("revision_candidate_changed", "ONBOARDING_PLAN_STALE")
        fingerprint = _validate_candidate(path, assets=shared_assets_root(), lifecycle=lifecycle)
        if fingerprint != value["candidate_sha256"]:
            refuse("revision_candidate_changed", "ONBOARDING_PLAN_STALE")
        return fingerprint

    result = lifecycle.run_refresh(target, candidate, keep_local=request["keep_local"], accept_pack=request["accept_pack"],
        candidate_validator=validate, expected_plan_id=value["owner_plan"].get("revision_plan_id"),
        rationale=request["rationale"], qualification=qualification)
    return {"status": result["status"], "revision_id": result.get("revision_id"), "refresh": result, "research": status(target)}


def status(target, *, catalog=None, evaluate=False):
    from .research_observation import optional

    root = Path(target).expanduser().resolve(strict=True)
    capture = owner("_evidence_revision").capture_workspace(root)
    lifecycle = owner("_domain_pack_lifecycle")
    state = lifecycle.load_state(root)
    config = lifecycle.load_mapping(root / "research.yml", "configuration")
    coverage = owner("coverage_manifest")
    qualified = owner("_caller_context").builtin_integrations(config)
    checks_allowed = qualified and (not config.get("computation") or evaluate)
    requirements = {}
    for record in state.get("research_revisions", []):
        for row in record["impact"]["questions"]:
            requirements[row["slug"]] = (record, row)
    rows = []
    for slug, (record, impact) in sorted(requirements.items()):
        q = owner("question_status")
        path = q.questions_directory(root, config) / (slug + ".md")
        fm = q.load_frontmatter(path) if path.exists() else {}
        current = optional(lambda slug=slug, fm=fm: coverage.coverage_summary_for_question(root, config, slug, fm)) if checks_allowed else {"state": "not_evaluated", "reason": "explicit_evaluation_or_provider_qualification_required"}
        if not checks_allowed:
            selected = coverage.selected_manifest_path(root, config, slug, fm.get("coverage_manifest")).relative_to(root).as_posix()
            observed = yaml_document(capture.files[selected]) if selected in capture.files else None
            pending_id = owner("_pack_revision_guard").pending_question(root, slug, observed)
            if pending_id:
                current = {"state": "observed", "result": {"coverage_status": "invalid", "coverage_verdict": "blocked",
                    "error_code": "COVERAGE_REVISION_REQUIRED", "revision_id": pending_id}}
        rows.append({"slug": slug, "revision_id": record["revision_id"], "impact": impact,
            "coverage": current, "question_present": path.is_file(), "question_status": fm.get("status")})
    strict = (optional(lambda: owner("strict_evidence").run_operation(root, "check")) if checks_allowed
              else {"state": "not_evaluated", "reason": "explicit_evaluation_or_provider_qualification_required"}) if config.get("strict_evidence") else None
    # Status never publishes claim text or attests to an unobserved semantic judgment.
    if strict and strict["state"] == "observed":
        result = strict["result"]
        strict["result"] = {"basis_id": result["basis_id"], "claims": [
            {"id": row["claim"]["id"], "accepted": row["accepted"], "reasons": row["reasons"]} for row in result["claims"]]}
    candidates = []
    if catalog:
        from .pack_catalog import entries

        candidates = [{"selector": r["selector"], "state": r["state"], "assessment": r.get("assessment"),
            "scope": r["scope"], "version": (r["metadata"] or {}).get("version")}
            for r in entries(Path(catalog)) if r["name"] == state["pack"]["name"] and r["registered_identity"]["tree_sha256"] != state["pack"]["tree_sha256"]]
    assessments = optional(lambda: owner("evidence_assessments").run_operation(root, operation="plan-refresh", request={
        "schema_version": "evidence-assessment-refresh-request/v1", "changed_sources": [], "evaluation_time": None,
        "limit": 64, "cursor": None})["plan"]) if checks_allowed and config.get("evidence_trust") else {"state": "not_evaluated" if config.get("evidence_trust") else "not_configured"}
    calculation = (owner("_computation_service").status(root, config) if qualified and evaluate else
                   {"status": "not_evaluated", "next": "computation check"}) if config.get("computation") else None
    installed = lifecycle.inspect_workspace(root)
    if owner("_pack_revision_guard").inputs(owner("_evidence_revision").capture_workspace(root)) != owner("_pack_revision_guard").inputs(capture):
        refuse("revision_status_changed", "ONBOARDING_PLAN_STALE")
    pending = [row["slug"] for row in rows if not row["question_present"] or row["question_status"] != "answered"
               or row["coverage"].get("result", {}).get("coverage_status") in {None, "invalid", "missing", "blocked", "pending"}]
    actions = []
    for row in rows:
        if row["coverage"].get("result", {}).get("error_code") == "COVERAGE_REVISION_REQUIRED":
            actions.append({"operation": "pack reevaluate", "target": str(root), "slug": row["slug"],
                "revision_id": row["revision_id"], "requires": "explicit reviewed requirement template and removed-ID mapping"})
        elif row["slug"] in pending:
            actions.append({"operation": "agent next", "target": str(root), "slug": row["slug"], "requires": "current evidence and a new answer/review cycle"})
    if strict:
        actions.append({"operation": "strict prepare-review", "target": str(root), "requires": "current authenticated independent authority; never local approval synthesis"})
    if calculation:
        actions.append({"operation": "computation check", "target": str(root), "requires": "explicit evaluation; writes and dispatch are separate commands"})
    if assessments["state"] != "not_configured":
        actions.append({"operation": "assessments plan-refresh", "target": str(root), "requires": "current host authority; follow scan cursor then explicitly sign invalidation"})
    return {"schema_version": "evidence-pack-revision-status/v1", "installed": installed,
        "revisions": [{**{k: r[k] for k in ("revision_id", "from", "to", "rationale", "qualification")},
            "removed_policies": r["impact"]["removed_policy_ids"], "requests": r["impact"]["requests"],
            "computation": r["impact"]["computation"], "unresolved_references": r["impact"]["unresolved_references"]}
            for r in state.get("research_revisions", [])],
        "questions": rows, "pending_questions": pending, "strict": strict, "assessments": assessments, "candidates": candidates,
        "computation": calculation,
        "improvement_certified": False, "release_owner": "current selected publication and strict evidence",
        "next_actions": actions + [{"operation": "agent research-export", "target": str(root), "requires": "current final release checks"}],
        "improvement_route": ["pack derive", "pack qualify", "pack freeze-cases", "pack assess", "pack accept",
                              "pack revision-plan", "pack revision-apply", "pack reevaluate", "agent research-export"],
        "limits": ["Catalog candidates are explicit alternatives; version ordering does not establish improvement.",
                   "Coverage migration does not issue independent reviews, change source permissions or certify answers."]}


def read_input(path):
    path = Path(path).expanduser().absolute()
    return read_file(path.parent, path.name)
