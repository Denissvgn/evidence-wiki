"""Public canonical journeys through actual bounded reference host processes."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sys
import time
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import yaml

from evidence_wiki import (
    BoundedSubprocessExecutor,
    ExecutionAttempt,
    ExecutionProfile,
    ExecutionRole,
    ExecutionRoleLimit,
    ExecutionRoleRegistry,
    ExecutionRoleScope,
    ExecutionSchedule,
    HostReviewSigner,
    NativeInputHandoff,
    QualificationAuthority,
    QualifiedEvaluators,
    ResearchExecution,
    ReviewDelivery,
    ReviewServicePlan,
    ReviewServiceRoute,
    RunPolicy,
    ServiceRepairBinding,
    SigningIdentity,
    Workspace,
)
from evidence_wiki._script_host import shared_assets_root
from evidence_wiki.agent_qualification import reference_suite
from evidence_wiki.cli import main
from evidence_wiki.host_context import HostContext
from evidence_wiki.pack_discovery import owner
from evidence_wiki.run_policy import POLICY_V2, POLICY_V3
from evidence_wiki.workflow_contracts import (
    EXECUTION_OBSERVATION,
    JOB,
    JOB_V2,
    JOB_V3,
    RESULT,
    WorkflowContractError,
    validate_document,
    validate_job,
)
from tests._execution_fixture import KEYS, authenticate, closure, identifier
from tests._execution_fixture import canonical as authenticated_bytes
from tests._usage_fixture import UsageFixture
from tests.test_draft_admission import draft_root as draft_fixture
from tests.test_draft_admission import update_policy
from tests.test_research_session import GRANTS as SESSION_GRANTS
from tests.test_run_policy import policy_document
from tests.test_workflow_contracts import job as reference_job

GRANTS = SESSION_GRANTS | {"research_session.intake_observation", "research_session.report", "telemetry.report",
    "question_claim.run_claim", "question_resolve.run_answer", "strict_evidence.run_admit_document",
    "strict_evidence.run_operation.review", "source_inventory.run_inventory_report", "normalize_sources.run_normalization_report",
    "coverage_manifest.run_link", "coverage_manifest.run_evaluate", "evidence_usage.run_transact"} | {
    "mutation_operations." + name for name in ("revision", "prepare_request", "submit", "inspect", "reconcile")}


class LocalMeter:
    """Observe a native reference process with no model/token/provider charge."""

    def finalize(self, _attempt, summary):
        return {"seconds": summary["seconds"], "model_calls": 0, "tool_calls": 1, "tokens": 0, "cost": "0"}


class HostProtocolSimulationMeter(LocalMeter):
    """Simulate a host model meter for protocol mechanics, never real model use."""

    def finalize(self, attempt, summary):
        return {**super().finalize(attempt, summary), "model_calls": 1}


@pytest.fixture
def fixture(tmp_path, monkeypatch, request=None):
    if os.name != "posix":
        pytest.skip("The reference journey requires POSIX process groups.")
    options = getattr(request, "param", {})
    initialized_root = tmp_path / "workspace"
    assert main(["init", "--target", str(initialized_root), "--project-name", "Controlled study",
        "--project-description", "Retain a scoped local study observation.", "--domain-pack", "general-science"]) == 0
    initialized_config = yaml.safe_load((initialized_root / "research.yml").read_bytes())
    initialized_metadata = (initialized_root / "workspace-system.yml").read_bytes()
    root, control, proposal, normalized = draft_fixture.__wrapped__(tmp_path, monkeypatch)
    if options.get("semantic_finite"):
        from tests._semantic_fixture import prepare_native
        normalized = prepare_native(root, proposal, options)
    (root / "workspace-system.yml").write_bytes(initialized_metadata)
    template_root = shared_assets_root() / "workspace-template"
    for path in template_root.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        destination = root / path.relative_to(template_root)
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
    for relative in ("skills/research-run.md", "docs/caller-research.md"):
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(shared_assets_root() / "workspace-template" / relative, destination)
    if options.get("partial"):
        question = "What unavailable independent observation was retained?"
        path = root / "docs/research-requirements.json"
        requirements = json.loads(path.read_bytes())
        requirements["request"]["payload"]["questions"].append({"id": "unavailable", "text": question})
        path.write_bytes(authenticated_bytes(requirements))
        owner("intake_questions").run_intake_document(root, {"schema_version": "1.0", "questions": [
            {"id": "unavailable", "question": question, "origin": "caller", "metadata": {"research_id": "unavailable",
                "original_ids_json": '["unavailable"]', "original_text": question}}]}, dry_run=False, from_file_label="local-study")
        proposal["originals"].append({"original_id": "unavailable", "text": question, "question_slugs": ["unavailable"]})
        proposal["inventory"]["questions"].append({"slug": "unavailable", "original_id": "unavailable", "question": question})
        update_policy(root)
    if options.get("derived_partial"):
        question = "What unavailable independent observation would refine the retained result?"
        path = root / "docs/research-requirements.json"
        requirements = json.loads(path.read_bytes())
        requirements["request"]["payload"]["derived_questions"].append({"id": "refinement", "text": question, "original_ids": ["q"]})
        path.write_bytes(authenticated_bytes(requirements))
        owner("intake_questions").run_intake_document(root, {"schema_version": "1.0", "questions": [
            {"id": "refinement", "question": question, "origin": "caller", "metadata": {"research_id": "refinement",
                "original_ids_json": '["q"]', "original_text": question}}]}, dry_run=False, from_file_label="local-study")
        proposal["originals"][0]["question_slugs"].append("refinement")
        proposal["inventory"]["questions"].append({"slug": "refinement", "original_id": "q", "question": question})
        update_policy(root)
    directory = tmp_path / "authority"
    directory.mkdir()
    authority = UsageFixture(directory, monkeypatch)
    if options.get("semantic_finite"):
        from tests._semantic_fixture import authorize
        authorize(authority)
    if options.get("role_isolation"):
        authority.policy["principals"]["counter-reviewer"] = {"controller": "counter-controller", "roles": ["evaluator"],
            "keys": {"counter-key": "44" * 32}}
        authority.save_policy()
    if options.get("review_services"):
        authority.policy["principals"]["backup-reviewer"] = {"controller": "backup-controller", "roles": ["evaluator"],
            "keys": {"backup-key": "55" * 32}}
        authority.save_policy()
    if options.get("readability"):
        authority.policy["principals"]["readability-reviewer"] = {"controller": "readability-controller", "roles": ["evaluator"],
            "keys": {"readability-key": "88" * 32}}
        authority.save_policy()
    authority.root = root
    authority.binding = identifier("evidence-host-workspace/v1", {"root": str(root)})
    local_config = yaml.safe_load((root / "research.yml").read_bytes())
    config = initialized_config
    def merge(base, supplied):
        for key, value in supplied.items():
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                merge(base[key], value)
            else:
                base[key] = value
    merge(config, local_config)
    config.update(authority.config)
    authority.config = config
    (root / "research.yml").write_text(yaml.safe_dump(config, sort_keys=False))
    (root / "sources/jurisdictions.yml").write_text(yaml.safe_dump({"jurisdiction_profiles": [
        {"jurisdiction_id": "synthetic-study", "official_domains": ["example.org"], "blocked_domains": []}]}))
    source_id = proposal["inventory"]["claims"][0]["evidence"][0]["source_id"]
    source_note = root / "wiki/sources" / normalized.name
    source_note.parent.mkdir(parents=True, exist_ok=True)
    note_body = "Retained finite structured observation." if options.get("semantic_finite") else "Measured solar reflectance was 0.74."
    source_note.write_text("---\ntype: source\ncreated: '2026-10-03'\nupdated: '2026-10-03'\nsource_ids:\n  - "
        + source_id + "\n---\n# Captured study\n\n" + note_body + "\n")
    with (root / "index.md").open("a") as stream:
        stream.write("\n- [Original question](wiki/questions/q.md)\n- [Captured study](wiki/sources/" + normalized.name + ")\n")
        if options.get("partial"):
            stream.write("- [Unavailable observation question](wiki/questions/unavailable.md)\n")
    body, _ = authority.source(source_id, normalized=normalized.read_bytes())
    descriptor = {"schema_version": "evidence-source-revision/v1", "source_id": source_id, "parents": [],
        "normalized_path": normalized.relative_to(root).as_posix(), "evidence_root": "evidence", "temporal": {}}
    files = {"source-record.json": authenticated_bytes(descriptor), descriptor["normalized_path"]: normalized.read_bytes()}
    files.update({"evidence/" + path.relative_to(root).as_posix(): path.read_bytes()
        for path in (root / "raw").rglob("*") if path.is_file() and not any(part.startswith(".") for part in path.relative_to(root).parts)})
    revision = closure(files)
    grant, scrub = body["grant"]["payload"], body["scrub"]["payload"]
    grant["source_revision"], scrub["sanitized_revision"] = revision, revision
    body.update(source_revision=revision, grant=authenticate(grant, "owner", "usage"), scrub=authenticate(scrub, "runner", "scrubber"))
    authority.transact(owner("_evidence_usage"), "initialize")
    authority.transact(owner("_evidence_usage"), "deposit", body, files)
    template = {"required_facets": [{"facet_id": "primary", "description": "Retain the observed primary study result.",
        "evidence_path": "official_guidance", "source_policy": "primary_or_official", "freshness_policy": "no_staleness_check",
        "identity_policy": "none", "min_sources": 1}]}
    owner("coverage_manifest").run_init_inline(root, slug="q", template=template)
    if options.get("derived_partial"):
        owner("coverage_manifest").run_init_inline(root, slug="refinement", template=template)
    if options.get("coverage_examination"):
        from tests.test_research_planning import question_plan
        requirements_path = root / "docs/research-requirements.json"
        requirements = json.loads(requirements_path.read_bytes())
        plan = question_plan("q")
        plan["facets"] = [{**facet, "required": True} for facet in template["required_facets"]]
        plan["criteria"][0]["required_scope"] = []
        requirements["decisions"] = {"mode": "legacy", "question_plans": [plan]}
        requirements_path.write_bytes(authenticated_bytes(requirements))
        update_policy(root)
    policy = policy_document()
    policy.update(schema_version=POLICY_V2)
    policy["scope"] = {"question_ids": ["q"], "source_scope": [str(root / "raw")], "source_scope_kind": "native_extent"}
    policy["required_evaluators"][0]["controller_ref"] = "evaluator"
    if options.get("role_isolation"):
        policy["required_evaluators"].append(dict(policy["required_evaluators"][0],
            role="critic", profile_ref="profile-b", controller_ref="counter-controller"))
    if options.get("partial"):
        policy["scope"]["question_ids"].append("unavailable")
        policy["source_rules"]["allow_partial"] = True
    if "allow_partial" in options:
        policy["source_rules"]["allow_partial"] = options["allow_partial"]
    policy["budgets"].update(seconds=1800, model_calls=100, tokens=1000000, cost_limit="0")
    if options.get("semantic_finite"):
        from tests._semantic_fixture import build_native
        return build_native(root, control, proposal, authority, policy, GRANTS, tmp_path, options)
    service_plan = None
    if options.get("review_services"):
        service_plan = ReviewServicePlan(tuple(ReviewServiceRoute(item["profile_ref"], item["role"],
            (item["profile_ref"], "profile-a-backup") if item["profile_ref"] == "profile-a" else (item["profile_ref"],),
            ExecutionRoleLimit("evaluator", item["controller_ref"], 10, options.get("service_slot_tools",10), 10000, "0", 1800))
            for item in policy["required_evaluators"]))
        policy.update(schema_version=POLICY_V3, review_services=service_plan.document())
    policy = RunPolicy(policy)
    roles = (
        ExecutionRole("researcher", "host_worker", "runner", "controlled-reference-process", "generate/v1"),
        ExecutionRole("profile-a", "evaluator", "evaluator", "controlled-reference-process", "review/v1"))
    schedule = None
    if options.get("worker_failover"):
        roles += (ExecutionRole("backup-researcher", "host_worker", "runner", "controlled-backup-process", "generate/v1"),)
        schedule = ExecutionSchedule((
            ExecutionRoleLimit("host_worker", "runner", 20, 20, 20000, "0", 1800),
            ExecutionRoleLimit("evaluator", "evaluator", 20, 20, 20000, "0", 1800)),
            ("worker_failed",), 1, (("researcher", "backup-researcher"),))
    registry = None
    if options.get("role_isolation"):
        roles += (ExecutionRole("profile-b", "evaluator", "counter-controller", "controlled-counter-process", "review/v1"),)
        registry = ExecutionRoleRegistry((ExecutionRoleScope("researcher", "researcher", ("model",)),
            ExecutionRoleScope("profile-a", "evaluator", ("model",)), ExecutionRoleScope("profile-b", "critic", ("model",))))
    disagreement = None
    if options.get("disagreement"):
        from evidence_wiki.disagreement_configuration import DisagreementPlan
        disagreement = DisagreementPlan()
        roles += (ExecutionRole("repairer", "host_worker", "runner", "controlled-repair-process", "generate/v1"),)
        registry = ExecutionRoleRegistry((*registry.scopes, ExecutionRoleScope("repairer", "repair", ("model",))))
    if options.get("planner"):
        roles += (ExecutionRole("planner", "host_worker", "planner-controller", "controlled-planning-process", "plan/v1"),)
        registry = ExecutionRoleRegistry((*(() if registry is None else registry.scopes),
            *(() if registry is not None else (ExecutionRoleScope("researcher", "researcher", ("model",)),
                ExecutionRoleScope("profile-a", "evaluator", ("model",)))),
            ExecutionRoleScope("planner", "planner", ("model", "plan.propose"))))
    if options.get("acquisition_choice"):
        registry = ExecutionRoleRegistry((ExecutionRoleScope("researcher", "researcher", ("model", "acquisition.propose")),
            ExecutionRoleScope("profile-a", "evaluator", ("model",))))
    if options.get("coverage_examination"):
        assert options.get("role_isolation")
        if not options.get("counter_only"):
            roles += (ExecutionRole("coverage-worker", "host_worker", "runner", "controlled-coverage-process", "coverage/v1"),)
        examination_scopes = [ExecutionRoleScope(scope.config_ref, scope.task_role,
            (*scope.capabilities, "objection.propose") if scope.task_role == "critic" else scope.capabilities)
            for scope in registry.scopes]
        if not options.get("counter_only"):
            examination_scopes.append(ExecutionRoleScope("coverage-worker", "coverage", ("model", "coverage.propose")))
        registry = ExecutionRoleRegistry(examination_scopes)
    if service_plan is not None:
        roles += (ExecutionRole("profile-a-backup", "evaluator", "backup-controller", "controlled-backup-reviewer", "review/v1"),)
        registry = ExecutionRoleRegistry((*registry.scopes, ExecutionRoleScope("profile-a-backup", "evaluator", ("model",))))
    if options.get("simulated_model_usage"):
        roles = tuple(ExecutionRole(role.config_ref, role.role, role.controller_ref,
            "simulated/" + role.model_ref, role.prompt_version) for role in roles)
    if options.get("counter_offline"):
        roles = tuple(ExecutionRole(role.config_ref, role.role, role.controller_ref,
            "offline/" + role.model_ref if role.config_ref == "profile-b" else role.model_ref,
            role.prompt_version) for role in roles)
    selected_meter = HostProtocolSimulationMeter if options.get("simulated_model_usage") else LocalMeter
    readability, readability_suite = None, None
    if options.get("readability"):
        from tests._readability_fixture import select
        roles, registry, readability, readability_suite = select(roles, registry, options)
    if options.get("service_zero_critic"):
        class ZeroCriticMeter(HostProtocolSimulationMeter):
            def finalize(self, attempt, summary):
                return {**super().finalize(attempt, summary), **({"model_calls":0} if attempt.config_ref == "profile-b" else {})}
        selected_meter = ZeroCriticMeter
    artifact_delivery = None
    if options.get("artifact_delivery"):
        from evidence_wiki.artifact_delivery_contracts import ArtifactDeliveryPlan, ArtifactDestination
        roles += (ExecutionRole("release", "host_worker", "runner", "controlled-release-process", "release/v1"),)
        registry = ExecutionRoleRegistry((*registry.scopes,
            ExecutionRoleScope("release", "release", ("model", "eligibility.read", "release.propose"))))
        artifact_delivery = ArtifactDeliveryPlan("release", "runner", (ArtifactDestination("local-brief"),),
            timeout_seconds=600, tokens=1000)
    profile = ExecutionProfile("local-reference", options.get("total_steps", 256), "EUR", "native-no-provider-charge/v1",
        roles, 1, 1, 1000, "0", 1, schedule, registry, ("control", "context") if registry else (), disagreement=disagreement,
        review_delivery=ReviewDelivery(**options.get("review_delivery_limits", {})) if options.get("review_delivery") else None,
        review_services=service_plan, service_repair=ServiceRepairBinding(service_plan.sha256,
            "sha256:" + disagreement.sha256) if options.get("service_repair") else None,
        input_handoff=NativeInputHandoff() if (options.get("repair") or options.get("readability")) and options.get("disagreement") else None,
        readability=readability, artifact_delivery=artifact_delivery)
    generated = {"proposal": proposal, "coverage_links": [{"slug": "q", "facet_id": "primary", "source_ids": [source_id]}]}
    generator = "import json,sys; json.load(sys.stdin); print(" + repr(json.dumps(generated)) + ")"
    if options.get("acquisition_choice"):
        generator = "import json,sys; d=json.load(sys.stdin)['input']; i=d['instruction']; print(json.dumps({'schema_version':'evidence-acquisition-choice/v1','request_id':i['request_id'],'request_basis':i['request_basis'],'action':'alternate' if i['alternatives'] else 'abstain','alternative_id':i['alternatives'][0]['alternative_id'] if i['alternatives'] else None,'reason':'Use a retained permitted source or abstain'}))"
    if options.get("acquisition_choice_hostile"):
        generator = "import json,sys; json.load(sys.stdin); print(json.dumps({'argv':['arbitrary-tool'],'authorized':True}))"
    evaluator = (
        "import json,sys; d=json.load(sys.stdin)['input']; "
        "r=({'case_id':d['case_id'],'label':'accept' if d['supported'] else 'reject'} if 'case_id' in d else "
        "{'verdicts':dict.fromkeys(['support','source_suitability','scope','time','units','counterevidence'],'pass'),"
        "'rationale':'The exact retained study quote supports the scoped observation.'}); print(json.dumps(r))"
    )
    if options.get("repair"):
        unsupported = json.loads(json.dumps(generated))
        unsupported["proposal"]["inventory"]["claims"][0]["text"] = "Solar reflectance is always 0.74."
        repaired = dict(generated, coverage_links=[]) if options.get("disagreement") else generated
        generator = ("import json,sys,copy; d=json.load(sys.stdin)['input']; g=" + repr(repaired)
            + "; g['proposal']['inventory']=copy.deepcopy(d['repair_scope']['before_inventory']) if d['purpose']=='repair' and 'repair_scope' in d else g['proposal']['inventory']; "
            + "target=next((c for c in g['proposal']['inventory']['claims'] if c['id']==d.get('repair_scope',{}).get('target_claim_id')),None); "
            + "target.update(next(c for c in " + repr(repaired["proposal"]["inventory"]["claims"])
            + " if c['id']==target['id'])) if target is not None else None; "
            + "print(json.dumps(g if d['purpose']=='repair' else " + repr(unsupported) + "))")
        evaluator = (
            "import json,sys; d=json.load(sys.stdin)['input']; "
            "r=({'case_id':d['case_id'],'label':'accept' if d['supported'] else 'reject'} if 'case_id' in d else "
            "{'verdicts':dict.fromkeys(['support','source_suitability','scope','time','units','counterevidence'],'pass'),"
            "'rationale':'A single retained measurement cannot establish a universal value.'}); "
            "r['verdicts'].update(support='fail',scope='fail') if 'packet' in d and 'always' in d['packet']['claim']['text'] else None; "
            "print(json.dumps(r))"
        )
    if options.get("readability_material"):
        generator = ("import json,sys,copy; d=json.load(sys.stdin)['input']; g=" + repr(generated) + "; "
            "g['proposal']['inventory']=copy.deepcopy(d['repair_scope']['before_inventory']) if d['purpose']=='repair' else g['proposal']['inventory']; "
            "target=next((c for c in g['proposal']['inventory']['claims'] if c['id']==d.get('repair_scope',{}).get('target_claim_id')),None); "
            "target['limitations'].append('This is a single retained measurement.') if target is not None else None; "
            "g.update(coverage_links=[]) if d['purpose']=='repair' else None; print(json.dumps(g))")
    worker = tmp_path / "workers"
    worker.mkdir()
    commands = {"researcher": (sys.executable, "-c", generator), "profile-a": (sys.executable, "-c", evaluator)}
    if options.get("complete_packet_capture"):
        capture_path = worker / "complete-input.json"
        capture_reader = "envelope=json.load(sys.stdin); __import__('pathlib').Path(" + repr(str(capture_path)) + ").write_text(json.dumps(envelope)); d=envelope['input']; "
        commands["profile-a"] = (sys.executable, "-c", evaluator.replace("d=json.load(sys.stdin)['input']; ", capture_reader))
    if options.get("role_isolation"):
        commands["profile-b"] = (sys.executable, "-c", evaluator)
    if options.get("disagreement"):
        commands["repairer"] = (sys.executable, "-c", generator)
    if options.get("worker_failover"):
        commands["backup-researcher"] = (sys.executable, "-c", generator)
        commands["researcher"] = (sys.executable, "-c", "raise SystemExit(1)")
    if options.get("planner"):
        planner = "import json,sys; d=json.load(sys.stdin)['input']; a=d['owner_actions'][0]; print(json.dumps({'schema_version':'research-work-proposal/v1','basis_id':d['basis_id'],'candidates':[dict(a,candidate_id='next',rationale='Exact permitted action')]}))"
        commands["planner"] = (sys.executable, "-c", planner)
    if options.get("coverage_examination"):
        if not options.get("counter_only"):
            commands["coverage-worker"] = (sys.executable, "-c", "import json,sys; i=json.load(sys.stdin)['input']['instruction']; a=i['anchors'][0]; f=i['facets'][0]['facet_id']; print(json.dumps({'schema_version':'evidence-coverage-proposal/v1','basis_id':i['basis_id'],'links':[{'facet_id':f,'claim_ids':[a['claim_id']],'source_ids':[a['source_id']]}],'missing':[]}))")
        critic = "import json,sys; d=json.load(sys.stdin)['input'];\n"
        critic += "if d.get('purpose')=='counterevidence':\n p=d['packet']; s=p['sources'][0]; q=s['content'].strip().split('\\n')[0]; b=s['content'].index(q); f={'claim_id':p['claim']['id'],'facet_id':d['examination']['criteria'][0]['facet_id'],'kind':" + repr(options.get("counter_disposition", "opposing")) + ",'query':'opposing retained observations','exclusions':[],'source_ids':[s['source_id']],'evidence':[{'source_id':s['source_id'],'record_sha256':s['record_sha256'],'quote':q,'start':b,'end':b+len(q)}],'rationale':'An unresolved contrary interpretation needs examination.'}; print(json.dumps({'schema_version':'evidence-counterevidence-proposal/v1','basis_id':d['examination']['basis_id'],'findings':[f]}))\n"
        critic += "else:\n " + evaluator.split("d=json.load(sys.stdin)['input']; ", 1)[1] + "\n"
        commands["profile-b"] = (sys.executable, "-c", critic)
    if service_plan is not None:
        commands["profile-a-backup"] = (sys.executable, "-c", evaluator)
    if readability is not None:
        from tests._readability_fixture import command
        commands["readability"] = command(worker, material=options.get("readability_material", False), hostile=options.get("readability_hostile", False))
    if artifact_delivery is not None:
        release_source = "import json,sys; d=json.load(sys.stdin)['input']['packet']; a=d['artifact']; print(json.dumps({'schema_version':'research-artifact-delivery-proposal/v1','artifact_id':a['artifact_id'],'artifact_sha256':a['artifact_sha256'],'artifact_bytes':a['bytes'],'destination_ref':d['approved_destination_refs'][0],'plan_sha256':d['plan_sha256']}))"
        commands["release"] = (sys.executable, "-c", release_source)
    executor = BoundedSubprocessExecutor(profile, commands=commands,
        cwd=worker, receipt_key=b"e" * 32, meter=selected_meter())
    from evidence_wiki.review_service_policy import approved_profiles
    approved_reviewers = approved_profiles(policy.to_dict(), profile)
    calibration_commands = {config: commands[config]
        for config in ["researcher", *[row["profile_ref"] for row in approved_reviewers]]}
    cases = [{"case_id": f"case-{index}", "family_id": f"family-{index // 10}",
        "input_document": {"case_id": f"case-{index}", "supported": index // 2 % 2 == 0, "wording": index % 2},
        "expected_label": "accept" if index // 2 % 2 == 0 else "reject", "critical": index // 2 % 2 != 0,
        "pair_id": f"pair-{index // 2}"} for index in range(100)]
    suite = reference_suite(cases, suite_ref="controlled-reference", split_ref="held-out", domain_ref="synthetic-study",
        rubric=config["strict_evidence"]["rubric"])
    issuer = QualificationAuthority("reference-issuer", b"q" * 32, "runner")
    expires = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    qualifications, reviewers = [], []
    for required in approved_reviewers:
        config_ref = required["profile_ref"]
        calibration_executor = BoundedSubprocessExecutor(profile, commands=calibration_commands,
            cwd=worker, receipt_key=b"e" * 32, meter=selected_meter())
        observations = []
        for index, case in enumerate(cases):
            now = datetime.now(timezone.utc)
            job = reference_job("evaluate")
            job.update(schema_version=JOB_V2, expected_result_schema=EXECUTION_OBSERVATION,
                id=f"calibration-{index}", controller_ref=required["controller_ref"], prompt_version="review/v1", policy_id=policy.policy_id,
                scope=policy.to_dict()["scope"]["question_ids"], issued_utc=now.isoformat(), deadline_utc=(now + timedelta(seconds=10)).isoformat(), timeout_seconds=5,
                budgets=profile.document()["job_budgets"])
            if registry:
                job.update(schema_version=JOB_V3, task_role=required["role"], required_capabilities=["model"],
                    role_scope_sha256=registry.scope(config_ref).sha256, context_id=f"reference-{index}",
                    packet_sha256=hashlib.sha256(authenticated_bytes(case["input_document"])[:-1]).hexdigest())
            attempt = ExecutionAttempt.create(attempt_id=f"calibration-{index}", nonce=f"nonce-{index}", run_id="references",
                binding_id="protected-reference", profile=profile, config_ref=config_ref, job=job, input_document=case["input_document"])
            if index and index % profile.total_steps == 0:
                calibration_executor = BoundedSubprocessExecutor(profile, commands=calibration_commands,
                    cwd=worker, receipt_key=b"e" * 32, meter=selected_meter())
            observed = calibration_executor.invoke(attempt)
            while observed["state"] == "running":
                time.sleep(0.005)
                observed = calibration_executor.poll(attempt)
            assert observed["state"] == "succeeded"
            observations.append((attempt, observed))
        qualifications.append(issuer.issue(policy, profile, config_ref, suite, cases, observations,
            calibration_executor, expires_utc=(datetime.now(timezone.utc)+timedelta(seconds=900)).isoformat()
            if options.get("service_short_backup") and config_ref == "profile-a-backup" else expires))
        if config_ref == "profile-a-backup":
            reviewers.append(SigningIdentity(config_ref, "backup-reviewer", "backup-key", required["controller_ref"], "evaluator", bytes.fromhex("77" * 32 if options.get("service_wrong_key") else "55" * 32)))
            continue
        reviewers.append(SigningIdentity(config_ref, "evaluator" if config_ref == "profile-a" else "counter-reviewer",
            "evaluator-key" if config_ref == "profile-a" else "counter-key", required["controller_ref"], "evaluator",
            bytes.fromhex("66" * 32 if options.get("service_wrong_key") and config_ref == "profile-a" else KEYS["evaluator"] if config_ref == "profile-a" else "44" * 32)))
    signer = HostReviewSigner("lab-authority", "1", SigningIdentity("researcher", "runner", "runner-key", "runner", "generator", bytes.fromhex(KEYS["runner"])),
        reviewers, "2027-01-01T00:00:00Z")
    registry_owner = QualifiedEvaluators(qualifications, issuer)
    if readability is not None:
        from tests._readability_fixture import qualify
        registry_owner = qualify(profile, policy, readability_suite, registry_owner, executor, worker, selected_meter)
        reviewers.append(SigningIdentity("readability", "readability-reviewer", "readability-key", "readability-controller", "evaluator", bytes.fromhex("88" * 32)))
        signer = HostReviewSigner("lab-authority", "1", signer.generator, reviewers, "2027-01-01T00:00:00Z")
    execution = ResearchExecution(executor, qualifications=registry_owner, review_signer=signer, observer_key=b"o" * 32)
    context = HostContext([root], GRANTS | ({"review_page_operations.prepare", "review_page_operations.page",
        "review_page_session.assembled", "review_packet_execution.bind", "review_packet_execution.reserve",
        "coverage_observation.observe", "research_session.assemble_review_packet"} if options.get("review_delivery") else set())
        | ({"research_session.review_services", "service_authentication.status"} if options.get("review_services") else set()) | ({"research_session.repair_progress", "repair_progress_owner.current"} if options.get("service_repair") else set())
        | ({"research_session.brief", "brief_operations.current", "research_session.resolution", "resolution_operations.current", "service_authentication.status"} if readability is not None or artifact_delivery is not None else set())
        | ({"research_session.prepare_delivery", "research_session.deliver", "artifact_delivery_operations.prepare", "artifact_delivery_operations.deliver",
            "research_session.delivery_status", "research_session.reconcile_delivery",
            "artifact_delivery_operations.delivery_status", "artifact_delivery_operations.reconcile_delivery", "history_operations.current"} if artifact_delivery is not None else set()), "caller", [root], authority_file=authority.policy_path,
        state_directory=authority.host, mutation_control_directory=control,
        controller_refs={"host_worker": "runner", "generator": "runner", "evaluator": "evaluator"},
        role_controller_refs=tuple((role.config_ref, role.role, role.controller_ref) for role in roles) if registry else ())
    return root, policy, execution, context


def test_public_reference_journey_generates_admits_reviews_and_releases_without_human(fixture, monkeypatch):
    root, policy, execution, context = fixture
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="journey", execution=execution) as session:
            before = session.current()
            assert before["qualification"]["state"] == "qualified", before["qualification"]
            result = session.run_until_terminal(request_id="drive", expected_basis_id=before["basis_id"], max_steps=64)
            assert result["terminal"] and result["outcome"] == "accepted", (result, session.report())
            assert result["canonical_run_state"] == "complete"
            report = session.report()
            assert report["accounting"]["totals"]["cost"] == "0"
            assert report["accounting"]["observed_human_interventions"] is None
            assert report["accounting"]["coverage"]["coverage_complete"]
            assert report["chain_complete"]
            current = session.current()
            assert current["accounting"]["accepted_count"] == 1 and current["accounting"]["unresolved_count"] == 0
            assert current["canonical_release"]["research_complete"]
            assert not result["authorized"] and not result["evidence_accepted"]
            assert "controlled-reference-process" in json.dumps(report)
            assert "Observed reflectance was 0.74." not in json.dumps(report)
            replay = session.run_until_terminal(request_id="drive", expected_basis_id=before["basis_id"], max_steps=64)
            assert replay["historical"] and replay["outcome"] == "accepted"


def test_execution_job_versions_preserve_exact_result_contracts():
    original = reference_job()
    upgraded = {**original, "schema_version": JOB_V2, "expected_result_schema": EXECUTION_OBSERVATION}
    assert validate_document(JOB, original)["expected_result_schema"] == RESULT
    assert validate_document(JOB_V2, upgraded)["expected_result_schema"] == EXECUTION_OBSERVATION
    assert validate_job(upgraded, now_utc="2026-01-01T00:00:05Z") == upgraded
    with pytest.raises(WorkflowContractError):
        validate_document(JOB, {**original, "expected_result_schema": EXECUTION_OBSERVATION})
    with pytest.raises(WorkflowContractError):
        validate_document(JOB_V2, {**upgraded, "expected_result_schema": RESULT})


def test_execution_job_control_cannot_change_parent_version_or_deadline():
    original = reference_job()
    parent = {**original, "schema_version": JOB_V2, "expected_result_schema": EXECUTION_OBSERVATION}
    poll = {**parent, "id": "poll-job", "operation": "poll", "original_text": None, "payload": {"job_id": parent["id"]}}
    assert validate_job(poll, now_utc="2026-01-01T00:00:05Z", parent_job=parent) == poll
    with pytest.raises(WorkflowContractError):
        validate_job(poll, now_utc="2026-01-01T00:00:05Z", parent_job=original)
    with pytest.raises(WorkflowContractError):
        validate_job({**poll, "deadline_utc": "2026-01-01T00:02:00Z"}, now_utc="2026-01-01T00:00:05Z", parent_job=parent)


def test_public_host_job_replay_and_cancel_keep_expected_inventory(fixture):
    root, policy, execution, context = fixture
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="handoff", execution=execution) as session:
            before = session.current()
            step = session.advance(request_id="prepare", expected_basis_id=before["basis_id"])
            assert not step["terminal"]
            original = session.current()["accounting"]["original_outcomes"][0]["original_text"]
            job = session.host_job(request_id="handoff-job", operation="generate", payload={"question": original})
            assert job["schema_version"] == JOB_V2
            assert session.host_job(request_id="handoff-job", operation="generate", payload={"question": original}) == job
            assert len([row for row in execution.records(session) if row["kind"] == "execution_attempt"]) == 1
            cancellation = session.cancel_pending(request_id="cancel")
            assert cancellation["cancellation_requested"]
            assert session.cancel_pending(request_id="cancel-again")["cancellation_requested"]
            before = session.current()
            terminal = session.advance(request_id="terminal", expected_basis_id=before["basis_id"])
            assert terminal["terminal"] and terminal["outcome"] in {"stop_budget", "blocked_external"}
            retained = session.current()
            assert retained["execution"]["pending"]["reservation_occupied"]
            assert retained["accounting"]["accepted_count"] == 0


def test_public_invalid_qualification_has_finite_replayable_outcome(fixture, monkeypatch):
    root, policy, execution, context = fixture
    def unavailable(*_args, **_kwargs):
        raise ValueError("qualification unavailable")
    monkeypatch.setattr(type(execution.qualifications), "observations", unavailable)
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="unqualified", execution=execution) as session:
            before = session.current()
            result = session.run_until_terminal(request_id="drive", expected_basis_id=before["basis_id"], max_steps=64)
            assert result["terminal"] and result["reason"] == "qualification_unknown"
            replay = session.advance(request_id=result["request_id"], expected_basis_id=result["before_basis_id"])
            assert replay["historical"] and replay["reason"] == result["reason"]
            assert not any(row["kind"] == "execution_attempt" for row in execution.records(session))


def test_public_restart_preserves_consumed_steps_and_original_deadline(tmp_path, monkeypatch):
    from evidence_wiki import execution_session
    root, policy, execution, context = fixture.__wrapped__(tmp_path, monkeypatch, SimpleNamespace(param={"total_steps": 1}))
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="bounded", execution=execution) as session:
            before = session.current()
            first = session.run_until_terminal(request_id="yield", expected_basis_id=before["basis_id"], max_steps=1)
            assert not first["terminal"]
            original_deadline = execution_session.deadline(session)
        with workspace.research.session(policy, run_id="bounded", execution=execution) as resumed:
            before = resumed.current()
            assert before["execution"]["steps_used"] == 1
            assert execution_session.deadline(resumed) == original_deadline
            stopped = resumed.run_until_terminal(request_id="stop", expected_basis_id=before["basis_id"], max_steps=64)
            assert stopped["terminal"] and stopped["reason"] == "budget_exhausted"
            assert resumed.current()["execution"]["steps_used"] == 1
            assert not any(row["kind"] == "execution_attempt" for row in execution.records(resumed))


def test_public_empty_authorized_extent_abstains_without_dispatch(fixture):
    root, policy, execution, context = fixture
    shutil.rmtree(root / "raw")
    (root / "raw").mkdir()
    for path in (root / "sources/normalized").iterdir():
        if path.is_file():
            path.unlink()
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="empty-evidence", execution=execution) as session:
            before = session.current()
            result = session.run_until_terminal(request_id="drive", expected_basis_id=before["basis_id"], max_steps=64)
            assert result["terminal"] and result["outcome"] == "abstained"
            assert result["reason"] == "insufficient_evidence"
            assert not any(row["kind"] == "execution_attempt" for row in execution.records(session))


def test_public_late_authenticated_completion_does_not_reopen_terminal_run(fixture, monkeypatch):
    root, policy, execution, context = fixture
    def unsupported_cancel(_attempt):
        raise NotImplementedError
    monkeypatch.setattr(execution.executor, "cancel", unsupported_cancel)
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="late-result", execution=execution) as session:
            before = session.current()
            session.advance(request_id="prepare", expected_basis_id=before["basis_id"])
            original = session.current()["accounting"]["original_outcomes"][0]["original_text"]
            session.host_job(request_id="external", operation="generate", payload={"question": original})
            row = next(row for row in execution.records(session) if row["kind"] == "execution_attempt")
            attempt = execution.attempt(row["data"]["attempt"])
            initial = execution.executor.invoke(attempt)
            cancelled = session.cancel_pending(request_id="cancel")
            assert cancelled["cancellation_requested"] and not cancelled["cancellation_confirmed"]
            before = session.current()
            terminal = session.advance(request_id="terminal", expected_basis_id=before["basis_id"])
            assert terminal["terminal"] and terminal["outcome"] == "blocked_external"
            observed = execution.executor.poll(attempt)
            assert observed["state"] == "succeeded" and observed["final_accounting"]
            assert session.intake_observation(observed, request_id="late") == observed
            assert session.intake_observation(observed, request_id="late-replay") == observed
            after = session.current()
            assert after["run"]["state"] == terminal["canonical_run_state"]
            assert after["accounting"]["accepted_count"] == 0
            assert after["execution"]["pending"]["reservation_occupied"]
            assert session.pending()["reservation_occupied"]
            assert session.intake_observation(initial, request_id="late-initial") == initial
            settled = session.current()
            assert not settled["execution"]["pending"]["reservation_occupied"]
            assert not session.pending()["reservation_occupied"]
            assert settled["run"]["state"] == terminal["canonical_run_state"]
            assert settled["accounting"]["accepted_count"] == 0
            assert not any(row["kind"] == "execution_application" for row in execution.records(session))


def test_public_failed_semantic_review_repairs_and_reevaluates(tmp_path, monkeypatch):
    root, policy, execution, context = fixture.__wrapped__(tmp_path, monkeypatch, SimpleNamespace(param={"repair": True}))
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="repair", execution=execution) as session:
            before = session.current()
            result = session.run_until_terminal(request_id="drive", expected_basis_id=before["basis_id"], max_steps=64)
            assert result["terminal"] and result["outcome"] == "accepted", (result, session.report())
            rows = execution.records(session)
            purposes = [row["data"]["purpose"] for row in rows if row["kind"] == "execution_attempt"]
            assert purposes.count("repair") == 1 and purposes.count("evaluate") == 2
            reviews = [row["data"] for row in rows if row["kind"] == "execution_applied" and row["data"]["operation"] == "evaluate"]
            assert [row["passed"] for row in reviews] == [False, True]
            publication = session.current()["canonical_release"]["publication"]
            claim = publication["questions"][0]["claims"][0]
            assert claim["text"] == "Observed reflectance was 0.74."
            assert publication["questions"][0]["original_id"] == "q"


def test_public_policy_permitted_partial_retains_every_original(tmp_path, monkeypatch):
    root, policy, execution, context = fixture.__wrapped__(tmp_path, monkeypatch, SimpleNamespace(param={"partial": True}))
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="partial", execution=execution) as session:
            before = session.current()
            result = session.run_until_terminal(request_id="drive", expected_basis_id=before["basis_id"], max_steps=64)
            assert result["terminal"] and result["outcome"] == "partial", (result, session.report())
            after = session.current()
            assert after["accounting"]["accepted_count"] == 1 and after["accounting"]["unresolved_count"] == 1
            outcomes = {row["original_id"]: row for row in after["accounting"]["original_outcomes"]}
            assert set(outcomes) == {"q", "unavailable"}
            assert outcomes["q"]["accepted"] and not outcomes["unavailable"]["accepted"]
            assert outcomes["unavailable"]["original_text"] == "What unavailable independent observation was retained?"
            assert not after["canonical_release"]["research_complete"]


def test_public_state_only_claim_recovery_keeps_invocation_unknown(fixture, monkeypatch):
    from evidence_wiki import execution_session
    root, policy, execution, context = fixture
    original_native = execution_session._native_call
    class LostHost(BaseException):
        pass
    def lose_before_owner(session, identifier, operation, arguments, invoke, **options):
        def unobserved(_control):
            raise LostHost
        return original_native(session, identifier, operation, arguments, unobserved, **options)
    with context.activate(), Workspace.open(root) as workspace:
        owner("question_claim").run_claim(root, slug="q", agent_id=context.actor_ref)
        with workspace.research.session(policy, run_id="state-only", execution=execution) as session:
            before = session.current()
            monkeypatch.setattr(execution_session, "_native_call", lose_before_owner)
            with pytest.raises(LostHost):
                session.advance(request_id="interrupted", expected_basis_id=before["basis_id"])
            monkeypatch.setattr(execution_session, "_native_call", original_native)
            recovered = session.advance(request_id="interrupted", expected_basis_id=before["basis_id"])
            assert recovered["terminal"] and recovered["reason"] == "usage_unknown"
            record = next(row for row in execution.records(session) if row["kind"] == "execution_native_observation")
            assert record["data"]["invocation_proof"] == "desired_state_only"
            assert record["data"]["active_execution_seconds"] is None
            observation = record["data"]["observation"]
            assert observation["usage"]["tool_calls"] is None and observation["usage"]["seconds"] is None
            assert observation["usage"]["cost"] == "0" and not observation["final_usage"]
            after = session.current()
            assert after["execution"]["accounting"]["usage_unknown"]
            assert not any(row["kind"] == "execution_attempt" for row in execution.records(session))
            assert not session.report()["accounting"]["coverage"]["coverage_complete"]


def test_public_pending_protected_normalization_refuses_without_changing_approved_source(fixture):
    root, policy, execution, context = fixture
    manifest = [json.loads(line) for line in (root / "sources/manifest.jsonl").read_text().splitlines() if line.strip()]
    assert len(manifest) == 1
    source_id = manifest[0]["id"]
    normalized, _label = owner("verify_quotes").normalized_record_path(root, yaml.safe_load((root / "research.yml").read_bytes()), source_id)
    raw = {path.relative_to(root).as_posix(): path.read_bytes() for path in (root / "raw").rglob("*")
        if path.is_file() and ".locks" not in path.parts}
    authority_before = context.authority_file.read_bytes()
    host_state_before = (context.state_directory / "evidence-state.json").read_bytes()
    normalized.unlink()
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="pending-normalization", execution=execution) as session:
            before = session.current()
            assert not before["execution"]["source_bindings"]
            assert session.next()["actions"][0]["operation"] == "normalize"
            prepared = session.advance(request_id="normalize", expected_basis_id=before["basis_id"])
            assert prepared["action"] == "normalize" and prepared["terminal"], (prepared, session.report())
            assert prepared["outcome"] == "blocked_external" and prepared["reason"] == "executor_unavailable"
            assert not normalized.exists()
            assert {path.relative_to(root).as_posix(): path.read_bytes() for path in (root / "raw").rglob("*")
                if path.is_file() and ".locks" not in path.parts} == raw
            assert context.authority_file.read_bytes() == authority_before
            assert (context.state_directory / "evidence-state.json").read_bytes() == host_state_before
            records = execution.records(session)
            native = [row["data"] for row in records if row["kind"] == "execution_native"]
            assert [row["operation"] for row in native] == ["sources.inventory"]
            assert native[0]["arguments"]["max_records"] <= policy.to_dict()["budgets"]["sources"]
            observed = [row["data"] for row in records if row["kind"] == "execution_native_observation"]
            assert len(observed) == 1
            assert observed[0]["error_code"] == "EVIDENCE_USAGE_REFUSED"
            assert observed[0]["owner_reason"] == "protected_capture_requires_host_sanitization"
            assert observed[0]["observation"]["usage"]["tool_calls"] == 1
            assert observed[0]["observation"]["final_usage"]
            assert not any(row["kind"] == "execution_attempt" for row in records)
            after = session.current()
            assert after["accounting"]["original_ids"] == ["q"]
            assert after["accounting"]["accepted_count"] == 0 and after["accounting"]["unresolved_count"] == 1
            assert after["accounting"]["original_outcomes"][0]["original_text"] == "  What observation was retained?  "
            replay = session.advance(request_id="normalize", expected_basis_id=before["basis_id"])
            assert replay["historical"] and replay["outcome"] == "blocked_external"
            assert len([row for row in execution.records(session) if row["kind"] == "execution_native"]) == 1
            assert session.report()["accounting"]["totals"]["tool_calls"] == 1
            assert session.report()["accounting"]["totals"]["cost"] == "0"


def test_native_source_preparation_bounds_both_owners_to_frozen_allowance(tmp_path, monkeypatch):
    from evidence_wiki import execution_session
    root = tmp_path.resolve()
    capture_path = root / "raw/web/capture.txt"
    capture_path.parent.mkdir(parents=True)
    capture_path.write_bytes(b"Retained observation.\n")
    record = {"id": "local:sample", "kind": "host_capture", "raw_paths": ["raw/web/capture.txt"]}
    observed = SimpleNamespace(config={"raw": {"source_roots": ["raw/web"]}, "sources": {
        "manifest_path": "sources/manifest.jsonl", "normalized_dir": "sources/normalized"}},
        capture=SimpleNamespace(files={"sources/manifest.jsonl": authenticated_bytes(record) + b"\n",
            "raw/web/capture.txt": capture_path.read_bytes()}), finish=lambda: None)
    policy = {"budgets": {"sources": 1}, "scope": {"source_scope": [str(root / "raw")]}}
    session = SimpleNamespace(root=root, run_id="bounded-sources", policy=SimpleNamespace(to_dict=lambda: policy))
    calls = []
    monkeypatch.setattr(execution_session, "ResearchObservation", lambda *_args, **_kwargs: observed)
    monkeypatch.setattr(execution_session, "_mutation", lambda _session, _identifier, operation, arguments: calls.append((operation, arguments)))
    monkeypatch.setattr(execution_session, "gate", lambda *_args: nullcontext())
    monkeypatch.setattr(execution_session, "append", lambda *_args: None)
    execution_session._source_prepare(session)
    assert [operation for operation, _arguments in calls] == ["sources.inventory", "normalize.sources"]
    assert calls[0][1]["max_records"] == 1
    assert calls[1][1]["max_sources"] == 1 and calls[1][1]["source_ids"] == ["local:sample"]
    assert not calls[1][1]["all_sources"]
