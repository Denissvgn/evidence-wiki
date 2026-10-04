"""Public canonical journeys through actual bounded reference host processes."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone

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
    QualificationAuthority,
    QualifiedEvaluators,
    ResearchExecution,
    RunPolicy,
    SigningIdentity,
)
from evidence_wiki._script_host import shared_assets_root
from evidence_wiki.agent_qualification import reference_suite
from evidence_wiki.cli import main
from evidence_wiki.host_context import HostContext
from evidence_wiki.pack_discovery import owner
from evidence_wiki.run_policy import POLICY_V2
from evidence_wiki.workflow_contracts import (
    EXECUTION_OBSERVATION,
    JOB_V2,
    JOB_V3,
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
    directory = tmp_path / "authority"
    directory.mkdir()
    authority = UsageFixture(directory, monkeypatch)
    if options.get("role_isolation"):
        authority.policy["principals"]["counter-reviewer"] = {"controller": "counter-controller", "roles": ["evaluator"],
            "keys": {"counter-key": "44" * 32}}
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
    source_note.write_text("---\ntype: source\ncreated: '2026-10-03'\nupdated: '2026-10-03'\nsource_ids:\n  - "
        + source_id + "\n---\n# Captured study\n\nMeasured solar reflectance was 0.74.\n")
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
    if options.get("calculation"):
        from tests._computation_fixture import definition
        declared = definition()
        declared["graphs"]["worksheet"] = {"description": "Configured arithmetic", "constants": {"amount": {"value": "3", "unit": "units"}}, "inputs": {}, "nodes": {"total": {"expr": "amount * 2", "unit": "units"}}, "output_mapping": {"total": "total"}, "output_page": None}
        config["computation"] = declared
        (root / "research.yml").write_text(yaml.safe_dump(config, sort_keys=False))
        proposal["inventory"]["schema_version"] = "evidence-strict-claims/v2"
        for claim in proposal["inventory"]["claims"]: claim["calculations"] = []
        (root / config["strict_evidence"]["claims_path"]).write_bytes(authenticated_bytes(proposal["inventory"]))
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
    policy["budgets"].update(seconds=1800, model_calls=100, tokens=1000000, cost_limit="0")
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
    if options.get("simulated_model_usage"):
        roles = tuple(ExecutionRole(role.config_ref, role.role, role.controller_ref,
            "simulated/" + role.model_ref, role.prompt_version) for role in roles)
    if options.get("counter_offline"):
        roles = tuple(ExecutionRole(role.config_ref, role.role, role.controller_ref,
            "offline/" + role.model_ref if role.config_ref == "profile-b" else role.model_ref,
            role.prompt_version) for role in roles)
    if options.get("calculation"):
        roles += (ExecutionRole("calculator", "host_worker", "runner", "controlled-calculation-process", "calculation/v1"),)
        registry = ExecutionRoleRegistry((*registry.scopes, ExecutionRoleScope("calculator", "calculation", ("model", "calculation.propose"))))
    selected_meter = HostProtocolSimulationMeter if options.get("simulated_model_usage") else LocalMeter
    profile = ExecutionProfile("local-reference", options.get("total_steps", 256), "EUR", "native-no-provider-charge/v1",
        roles, 1, 1, 1000, "0", 1, schedule, registry, ("control", "context") if registry else (), disagreement=disagreement)
    generated = {"proposal": proposal, "coverage_links": [{"slug": "q", "facet_id": "primary", "source_ids": [source_id]}]}
    generator = "import json,sys; json.load(sys.stdin); print(" + repr(json.dumps(generated)) + ")"
    if options.get("calculated_generation"):
        generator = ("import json,sys,copy; d=json.load(sys.stdin)['input']; g=" + repr(generated)
            + "; c=copy.deepcopy(g['proposal']['inventory']['claims'][0]); r=d['calculation_selections'][0]['reference']; "
            + "c.update(id='derived',qualification='inference',premises=[c['id']],derivation='Apply the configured multiplication.',"
            + "text='Declared result is '+r['expected']+' units.',units=r['unit'],calculations=[r]); "
            + "g['proposal']['inventory']['claims'].append(c); print(json.dumps(g))")
        if options.get("omit_calculation_reference"):
            generator = generator.replace("calculations=[r]", "calculations=[]")
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
        generator = ("import json,sys; d=json.load(sys.stdin)['input']; print(json.dumps(" + repr(repaired)
            + " if d['purpose']=='repair' else " + repr(unsupported) + "))")
        evaluator = (
            "import json,sys; d=json.load(sys.stdin)['input']; "
            "r=({'case_id':d['case_id'],'label':'accept' if d['supported'] else 'reject'} if 'case_id' in d else "
            "{'verdicts':dict.fromkeys(['support','source_suitability','scope','time','units','counterevidence'],'pass'),"
            "'rationale':'A single retained measurement cannot establish a universal value.'}); "
            "r['verdicts'].update(support='fail',scope='fail') if 'packet' in d and 'always' in d['packet']['claim']['text'] else None; "
            "print(json.dumps(r))"
        )
    worker = tmp_path / "workers"
    worker.mkdir()
    commands = {"researcher": (sys.executable, "-c", generator), "profile-a": (sys.executable, "-c", evaluator)}
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
    if options.get("calculation"):
        calc = "import json,sys; d=json.load(sys.stdin)['input']['calculation']; print(json.dumps({'schema_version':'evidence-calculation-proposal/v1','result_id':d['result_id'],'definition_id':d['definition_id'],'pointer':'/graphs/worksheet/outputs/total','form':'value','expression':'amount * 2','unit':'units','as_of':d['clock']['as_of'],'assumptions':['Explicit configured units only'],'rationale':'Independent interpretation verifier required'}))"
        if options.get("invalid_calculation"):
            calc = calc.replace("amount * 2", "amount * 200")
        commands["calculator"] = (sys.executable, "-c", calc)
    if options.get("calculated_generation") and not options.get("preloaded_calculation_premise"):
        empty = json.loads(json.dumps(proposal["inventory"]))
        empty["claims"] = []
        (root / config["strict_evidence"]["claims_path"]).write_bytes(authenticated_bytes(empty))
    executor = BoundedSubprocessExecutor(profile, commands=commands,
        cwd=worker, receipt_key=b"e" * 32, meter=selected_meter())
    calibration_commands = {config: commands[config]
        for config in ["researcher", *[row["profile_ref"] for row in policy.to_dict()["required_evaluators"]]]}
    cases = [{"case_id": f"case-{index}", "family_id": f"family-{index // 10}",
        "input_document": {"case_id": f"case-{index}", "supported": index // 2 % 2 == 0, "wording": index % 2},
        "expected_label": "accept" if index // 2 % 2 == 0 else "reject", "critical": index // 2 % 2 != 0,
        "pair_id": f"pair-{index // 2}"} for index in range(100)]
    suite = reference_suite(cases, suite_ref="controlled-reference", split_ref="held-out", domain_ref="synthetic-study",
        rubric=config["strict_evidence"]["rubric"])
    issuer = QualificationAuthority("reference-issuer", b"q" * 32, "runner")
    expires = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    qualifications, reviewers = [], []
    for required in policy.to_dict()["required_evaluators"]:
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
            calibration_executor, expires_utc=expires))
        reviewers.append(SigningIdentity(config_ref, "evaluator" if config_ref == "profile-a" else "counter-reviewer",
            "evaluator-key" if config_ref == "profile-a" else "counter-key", required["controller_ref"], "evaluator",
            bytes.fromhex(KEYS["evaluator"] if config_ref == "profile-a" else "44" * 32)))
    signer = HostReviewSigner("lab-authority", "1", SigningIdentity("researcher", "runner", "runner-key", "runner", "generator", bytes.fromhex(KEYS["runner"])),
        reviewers, "2027-01-01T00:00:00Z")
    execution = ResearchExecution(executor, qualifications=QualifiedEvaluators(qualifications, issuer), review_signer=signer, observer_key=b"o" * 32)
    context = HostContext([root], GRANTS | {"research_session.calculate", "research_session.propose_calculation", "calculation_session.current", "calculation_agent.propose"}, "caller", [root], authority_file=authority.policy_path,
        state_directory=authority.host, mutation_control_directory=control,
        controller_refs={"host_worker": "runner", "generator": "runner", "evaluator": "evaluator"},
        role_controller_refs=tuple((role.config_ref, role.role, role.controller_ref) for role in roles) if registry else ())
    return root, policy, execution, context

