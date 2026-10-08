"""Synthetic finite host observations through real native calibration owners.

The protected cases and HMAC material are test-host fixtures. The subprocess
meter describes synthetic protocol activity, never provider calls or general
semantic qualification.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import yaml

from evidence_wiki import (
    BoundedSubprocessExecutor,
    CalibratedEvaluators,
    CalibrationAuthority,
    CalibrationPlan,
    CalibrationSelection,
    DisagreementPlan,
    ExecutionAttempt,
    ExecutionProfile,
    ExecutionRole,
    ExecutionRoleRegistry,
    ExecutionRoleScope,
    HostReviewSigner,
    ResearchExecution,
    RunPolicy,
    SemanticJudgingPlan,
    SigningIdentity,
)
from evidence_wiki.agent_execution import _digest
from evidence_wiki.finite_domain import DESCRIPTOR, DOMAIN, SOURCE
from evidence_wiki.host_context import HostContext
from evidence_wiki.pack_discovery import owner
from evidence_wiki.qualification_calibration import calibration_suite
from evidence_wiki.reference_oracles import protected_cases
from evidence_wiki.workflow_contracts import EXECUTION_OBSERVATION, JOB_V3
from tests._execution_fixture import KEYS
from tests._execution_fixture import canonical as authenticated_bytes
from tests.test_auxiliary_calibration import ProtectedHost
from tests.test_draft_admission import update_policy
from tests.test_host_capture import deposit
from tests.test_host_capture import profile as capture_profile
from tests.test_workflow_contracts import job as reference_job

ORIGINAL = "Assess the exact declared finite observation for observed-cohort-1 at observation-1."
REVIEWERS = (
    ("evidence-config", "evaluator", "evidence-controller", "evidence-reviewer", "44" * 32),
    ("scope-config", "evaluator", "scope-controller", "scope-reviewer", "55" * 32),
    ("counter-config", "critic", "counter-controller", "counter-reviewer", "66" * 32),
    ("adjudicator-config", "adjudicator", "adjudicator-controller", "adjudicator-reviewer", "77" * 32),
)


class SyntheticProtocolMeter:
    """Observe subprocess mechanics with a synthetic model meter and no provider."""

    def finalize(self, _attempt, summary):
        return {"seconds": summary["seconds"], "model_calls": 1, "tool_calls": 1, "tokens": 1, "cost": "0"}


def prepare_native(root, proposal, options):
    """Regenerate exact finite capture bytes through native source owners."""
    requirements_path = root / "docs/research-requirements.json"
    requirements = json.loads(requirements_path.read_bytes())
    requirements["request"]["payload"]["questions"][0]["text"] = ORIGINAL
    requirements_path.write_bytes(authenticated_bytes(requirements))
    page = root / "wiki/questions/q.md"
    metadata, body = owner("verify_quotes").split_page(page.read_text())
    old_text = metadata["metadata"]["original_text"]
    metadata["question"] = ORIGINAL
    metadata["metadata"]["original_text"] = ORIGINAL
    page.write_text("---\n" + yaml.safe_dump(metadata, sort_keys=False) + "---\n" + body.replace(old_text.strip(), ORIGINAL))
    proposal["originals"][0]["text"] = ORIGINAL
    proposal["inventory"]["questions"][0]["question"] = ORIGINAL
    reference = protected_cases()[0]["input_document"]
    facts, candidate = copy.deepcopy(reference["facts"]), copy.deepcopy(reference["claim"])
    if options.get("semantic_wrong_value"):
        candidate["value"] = "999"
    if options.get("semantic_out_of_domain"):
        data = b"A prose observation cannot qualify as exact finite evidence.\n"
    else:
        data = authenticated_bytes({"schema_version": SOURCE, "facts": facts})
    capture = capture_profile(data, title="Synthetic finite observation",
        retrieved_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat())
    deposit(root, data, capture)
    owner("source_inventory").run_inventory_report(root, append_log=False)
    records = [json.loads(line) for line in (root / "sources/manifest.jsonl").read_text().splitlines()]
    source = next(row for row in reversed(records) if row["kind"] == "host_capture")
    owner("normalize_sources").run_normalization_report(root, source_ids=[source["id"]], force=True, append_log=False)
    normalized = root / "sources/normalized" / (owner("_normalized_contract").safe_source_id(source["id"]) + ".md")
    claim = proposal["inventory"]["claims"][0]
    claim.update(text=DESCRIPTOR["claim_grammar"].format(operation=facts["operation"], **candidate),
        scope=candidate["scope"], time=candidate["time"], units=candidate["units"],
        limitations=["Synthetic exact declaration; broader source truth is unqualified."])
    claim["evidence"] = [{"source_id": source["id"], "record_sha256": "sha256:" + hashlib.sha256(normalized.read_bytes()).hexdigest(),
        "observed_at": capture["retrieved_at"], "capture": "primary", "quote": data.decode().strip(),
        "location_hint": None, "anchor": None}]
    update_policy(root)
    return normalized


def authorize(authority):
    """Keep independent reviewer signing material in the protected host policy."""
    for _config, _task_role, controller, principal, key in REVIEWERS:
        authority.policy["principals"][principal] = {"controller": controller, "roles": ["evaluator"],
            "keys": {principal + "-key": key}}
    authority.save_policy()


def reviewer_program(worker, config, *, advisory="pass", forged=False):
    source = '''import json,sys
from pathlib import Path
envelope=json.load(sys.stdin)
d=envelope['input'];s=d['semantic']
Path(CAPTURE).write_text(json.dumps(envelope))
def anchor(pointer,text):
 return {'pointer':pointer,'start':0,'end':len(text),'quote':text}
checks={}
for dimension,units in s['units'].items():
 rows=[]
 for index,unit in enumerate(units):
  applicable=unit['applicable']
  if dimension=='support':
   citation=anchor('/semantic/sources/0/content',s['sources'][0]['content'])
  elif dimension=='scoped_correctness':
   citation=anchor('/packet/claim/text',d['packet']['claim']['text'])
  elif dimension=='missing_material_evidence':
   citation=anchor('/semantic/required_units/'+str(index)+'/text',s['required_units'][index]['text'])
  else:
   citation=anchor('/semantic/original_text',s['original_text'])
  rows.append({'unit_id':unit['unit_id'],'verdict':'pass' if applicable else 'not_applicable',
   'reason':'Exact supplied field was checked by '+CONFIG+'.','severity':'none','uncertainty':'low',
   'anchors':[citation] if applicable else []})
 checks[dimension]=rows
if FORGED:
 checks['support'][0]['anchors'][0]['quote']='Forged source text.'
out={'schema_version':'research-semantic-adjudication/v1' if s['role']=='adjudicator' else 'research-semantic-findings/v1',
 'rationale':CONFIG+' assessed the exact supplied observation.','checks':checks}
if s['role']=='adjudicator':
 if ADVISORY=='unknown':
  checks['support'][0].update(verdict='unknown',severity='unknown',uncertainty='unknown',anchors=[])
else:
 out.update(verdicts=dict.fromkeys(['support','source_suitability','scope','time','units','counterevidence'],'pass'),objections=[])
print(json.dumps(out))
'''
    return source.replace("CAPTURE", repr(str(worker / (config + "-input.json")))).replace("CONFIG", repr(config)).replace(
        "ADVISORY", repr(advisory)).replace("FORGED", repr(forged))


def _qualify(profile, policy, suite, *, mode):
    """Score authenticated synthetic receipts with actual protected authorities."""
    host = ProtectedHost(profile)
    issuer = CalibrationAuthority("synthetic-finite-issuer", b"p" * 32, "runner")
    records = []
    if mode == "absent" or any(row["human_required"] for row in policy.to_dict()["required_evaluators"]):
        return CalibratedEvaluators(records, issuer), host
    now = datetime.now(timezone.utc)
    for config, task_role, controller, _principal, _key in REVIEWERS:
        if mode == "adjudicator_missing" and task_role == "adjudicator":
            continue
        observations = []
        role = profile.role(config)
        scope = profile.role_scopes.scope(config)
        for index, case in enumerate(suite["cases"]):
            document = reference_job("evaluate") | {"schema_version": JOB_V3,
                "expected_result_schema": EXECUTION_OBSERVATION, "id": f"reference-job-{index}",
                "controller_ref": controller, "prompt_version": role.prompt_version, "policy_id": policy.policy_id,
                "scope": policy.to_dict()["scope"]["question_ids"], "budgets": profile.document()["job_budgets"],
                "issued_utc": "2026-10-01T11:59:40Z", "deadline_utc": "2026-10-01T12:00:10Z",
                "task_role": task_role, "required_capabilities": ["model"], "role_scope_sha256": scope.sha256,
                "context_id": f"reference-context-{index}", "packet_sha256": _digest(case["input_document"])}
            attempt = ExecutionAttempt.create(attempt_id=f"reference-attempt-{index}", nonce=f"reference-nonce-{index}",
                run_id="synthetic-calibration", binding_id="protected-finite-fixture", profile=profile,
                config_ref=config, job=document, input_document=case["input_document"])
            observations.append((attempt, host.receipt(attempt, case)))
        observed = now - timedelta(hours=3) if mode == "expired" else now
        record = issuer.issue(policy, profile, config, suite, observations, host, observed_utc=observed.isoformat(),
            expires_utc=(observed + timedelta(hours=2)).isoformat(), verification_seconds=30,
            auxiliary=task_role == "adjudicator")
        assert record["state"] == "qualified", record["reasons"]
        records.append(record)
    return CalibratedEvaluators(records, issuer), host


def build_native(root, control, proposal, authority, policy_document, grants, tmp_path, options):
    """Explicit finite opt-in; legacy prose and legacy qualification stay separate."""
    required = [{"role": task_role, "profile_ref": config, "controller_ref": controller,
        "human_required": bool(options.get("semantic_human_required"))}
        for config, task_role, controller, _principal, _key in REVIEWERS if task_role != "adjudicator"]
    policy = RunPolicy(policy_document | {"required_evaluators": required})
    rubric = owner("verify_quotes").load_config(root)["strict_evidence"]["rubric"]
    suite = calibration_suite(protected_cases(), suite_ref="synthetic-finite-protected", split_ref="held-out-finite-artifacts",
        domains=[DOMAIN], rubric=rubric, model_settings={"execution": "synthetic-protocol-observation", "provider": "none", "temperature": "0"})
    worker = tmp_path / "workers"
    worker.mkdir()
    generated = {"proposal": proposal, "coverage_links": [{"slug": "q", "facet_id": "primary",
        "source_ids": [proposal["inventory"]["claims"][0]["evidence"][0]["source_id"]]}]}
    generator = "import json,sys; json.load(sys.stdin); print(" + repr(json.dumps(generated)) + ")"
    programs = {config: reviewer_program(worker, config, advisory=options.get("semantic_advisory", "pass"),
        forged=bool(options.get("semantic_forged_anchor")) and task_role == "evaluator")
        for config, task_role, _controller, _principal, _key in REVIEWERS}
    calibration = CalibrationPlan(tuple(CalibrationSelection(config, suite,
        model_revision="synthetic-program/" + hashlib.sha256(programs[config].encode()).hexdigest())
        for config, _task_role, _controller, _principal, _key in REVIEWERS))
    roles = (ExecutionRole("researcher", "host_worker", "runner", "synthetic/finite-generator-process", "generate/v1"),
        *(ExecutionRole(config, "evaluator", controller, "synthetic/" + config + "-process", "semantic-review/v1")
            for config, _task_role, controller, _principal, _key in REVIEWERS))
    scopes = ExecutionRoleRegistry((ExecutionRoleScope("researcher", "researcher", ("model",)),
        *(ExecutionRoleScope(config, task_role, ("model",)) for config, task_role, _controller, _principal, _key in REVIEWERS)))
    profile = ExecutionProfile("synthetic-finite-host", options.get("total_steps", 256), "EUR", "synthetic-protocol-observation/v1",
        roles, 1, 1, 1000, "0", 1, role_scopes=scopes, required_diversity=("control", "context"),
        disagreement=DisagreementPlan("adjudicator-config"), calibration=calibration,
        semantic_judging=SemanticJudgingPlan("evidence-config", "scope-config", "counter-config"))
    commands = {"researcher": (sys.executable, "-c", generator),
        **{config: (sys.executable, "-c", program) for config, program in programs.items()}}
    if options.get("semantic_advisory") == "missing":
        commands.pop("adjudicator-config")
    qualifications, protected_host = _qualify(profile, policy, suite, mode=options.get("semantic_qualification", "current"))
    executor = BoundedSubprocessExecutor(profile, commands=commands, cwd=worker, receipt_key=b"s" * 32,
        meter=SyntheticProtocolMeter())
    executor.calibration_configuration = protected_host.calibration_configuration
    executor.qualification_configuration = lambda request: {"request": copy.deepcopy(request),
        "configuration": protected_host.calibration_configuration(request["config_ref"])}
    reviewers = [SigningIdentity(config, principal, principal + "-key", controller, "evaluator", bytes.fromhex(key))
        for config, _task_role, controller, principal, key in REVIEWERS]
    signer = HostReviewSigner("lab-authority", "1", SigningIdentity("researcher", "runner", "runner-key", "runner",
        "generator", bytes.fromhex(KEYS["runner"])), reviewers, "2027-01-01T00:00:00Z")
    execution = ResearchExecution(executor, qualifications=qualifications, review_signer=signer, observer_key=b"j" * 32)
    context = HostContext([root], grants | {"research_session.semantic_report", "research_session.resolution"}, "caller", [root],
        authority_file=authority.policy_path, state_directory=authority.host, mutation_control_directory=control,
        controller_refs={"host_worker": "runner", "generator": "runner", "evaluator": "evidence-controller"},
        role_controller_refs=tuple((role.config_ref, role.role, role.controller_ref) for role in roles))
    return root, policy, execution, context


def native_fixture(tmp_path, monkeypatch, **options):
    from tests.test_execution_session import fixture

    return fixture.__wrapped__(tmp_path, monkeypatch, SimpleNamespace(param={"semantic_finite": True, **options}))
