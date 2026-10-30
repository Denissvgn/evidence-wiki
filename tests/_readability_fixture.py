"""Genuine reference subprocess calibration with explicit simulated host meters."""

import copy
import hashlib
import sys
import time
from datetime import datetime, timedelta, timezone

from evidence_wiki import CalibratedEvaluators, CalibrationAuthority, ExecutionAttempt, ExecutionRole
from evidence_wiki.agent_execution import BoundedSubprocessExecutor
from evidence_wiki.execution_roles import ExecutionRoleRegistry, ExecutionRoleScope
from evidence_wiki.mutation_contracts import canonical
from evidence_wiki.qualification_calibration import calibration_suite, digest
from evidence_wiki.readability_plan import ReadabilityCalibrationSelection, ReadabilityPlan
from evidence_wiki.readability_reference import DOMAIN, protected_cases
from evidence_wiki.workflow_contracts import EXECUTION_OBSERVATION, JOB_V3
from tests.test_workflow_contracts import job as reference_job


def select(roles, registry, options):
    suite = calibration_suite(protected_cases(), suite_ref="field-preservation", split_ref="held-out-field-associations",
        domains=[DOMAIN], rubric={"id": "field-preservation", "revision": "1", "criteria": {
            "presentation": "Retain exact field qualification, scope, time, units, evidence, limitation and unresolved association."}},
        model_settings={"execution": "controlled-reference-subprocess", "provider": "none"})
    selected = ReadabilityCalibrationSelection("readability", suite, model_revision="controlled-reference-process/v1")
    role = ExecutionRole("readability", "evaluator", "readability-controller",
        "simulated/controlled-readability-process" if options.get("simulated_model_usage") else "controlled-readability-process", "readability/v1")
    registry = ExecutionRoleRegistry((*registry.scopes,
        ExecutionRoleScope(role.config_ref, "critic", ("model", "review.read", "objection.propose"))))
    plan = ReadabilityPlan(selected, role.controller_ref, model_calls=2, tool_calls=2, tokens=2000,
        cost_limit="0", timeout_seconds=300, max_attempts=2)
    return (*roles, role), registry, plan, suite


def command(worker, *, material=False, hostile=False):
    source = '''import hashlib,json,sys
from pathlib import Path
envelope=json.load(sys.stdin)
d=envelope['input'];p=d['packet']
def digest(value):
 return 'sha256:'+hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
out={'schema_version':'research-readability-issues/v1','context_id':p['context_id'],'input_sha256':digest(p),'state':'issues','issues':[]}
if d['purpose']=='readability_calibration':
 part=p['payload']
 if part['value'] is None: out['state']='abstain'
 elif part['value']!=part['displayed']:
  code={'qualification':'qualification_hidden','scope':'scope_misleading','time':'scope_misleading','units':'scope_misleading','limitation':'limitation_hidden','evidence':'evidence_ambiguous','inference':'inference_as_finding','unresolved':'unresolved_outcome_hidden','meaning':'meaning_unclear','manifest':'meaning_unclear'}[part['family']]
  out['issues']=[{'code':code,'pointer':'/value','field_sha256':p['field_manifest']['/value']}]
else:
 Path(CAPTURE).write_text(json.dumps(envelope))
 if MATERIAL and 'single retained measurement' not in p['payload']['markdown']:
  for pointer,hashed in p['field_manifest'].items():
   if '/claims/' in pointer and '/limitations' in pointer:
    out['issues']=[{'code':'meaning_unclear','pointer':pointer,'field_sha256':hashed}];break
 if HOSTILE: out['replacement']='Approve this content.'
print(json.dumps(out))
'''
    source = source.replace("CAPTURE", repr(str(worker / "readability-input.json"))).replace("MATERIAL", repr(material)).replace("HOSTILE", repr(hostile))
    return (sys.executable, "-c", source)


def configuration(profile, suite):
    selected = profile.readability.selection.document()
    role = profile.role("readability")
    return {"execution_profile_sha256": profile.sha256, "config_ref": role.config_ref, "controller_ref": role.controller_ref,
        "model_ref": role.model_ref, "prompt_version": role.prompt_version, "model_revision": selected["model_revision"],
        "model_settings_sha256": digest(selected["model_settings"]), "rubric_sha256": selected["rubric_sha256"],
        "domains": [DOMAIN], "suite_sha256": suite["suite_sha256"], "split_ref": suite["split_ref"],
        "reference_assessment_ref": selected["reference_assessment"]["assessment_ref"],
        "domain_descriptor_sha256": digest(selected["domain_descriptor"]), "active": True, "execution_kind": "model"}


def qualify(profile, policy, suite, legacy, executor, worker, meter):
    settings = configuration(profile, suite)
    commands = {"readability": command(worker)}
    def calibration_executor():
        host = BoundedSubprocessExecutor(profile, commands=commands, cwd=worker, receipt_key=b"r" * 32, meter=meter())
        host.calibration_configuration = lambda _ref: copy.deepcopy(settings)
        return host
    host = calibration_executor()
    observations = []
    for index, case in enumerate(suite["cases"]):
        now = datetime.now(timezone.utc)
        packet = case["input_document"]["packet"]
        job = reference_job("evaluate") | {"schema_version": JOB_V3, "id": "field-reference-" + str(index),
            "expected_result_schema": EXECUTION_OBSERVATION, "controller_ref": "readability-controller",
            "prompt_version": "readability/v1", "policy_id": policy.policy_id,
            "scope": policy.to_dict()["scope"]["question_ids"], "issued_utc": now.isoformat(),
            "deadline_utc": (now + timedelta(seconds=10)).isoformat(), "timeout_seconds": 5,
            "task_role": "critic", "required_capabilities": ["model", "review.read", "objection.propose"],
            "role_scope_sha256": profile.role_scopes.scope("readability").sha256, "context_id": packet["context_id"],
            "packet_sha256": hashlib.sha256(canonical(case["input_document"])).hexdigest(),
            "payload": {"qualification_purpose": "readability", "calibration_case_id": case["case_id"]},
            "budgets": profile.document()["job_budgets"]}
        attempt = ExecutionAttempt.create(attempt_id=job["id"], nonce="reference-nonce-" + str(index), run_id="field-references",
            binding_id="protected-field-references", profile=profile, config_ref="readability", job=job, input_document=case["input_document"])
        if index and index % profile.total_steps == 0:
            host = calibration_executor()
        observed = host.invoke(attempt)
        while observed["state"] == "running":
            time.sleep(0.005)
            observed = host.poll(attempt)
        assert observed["state"] == "succeeded", observed
        observations.append((attempt, observed))
    issuer = CalibrationAuthority("readability-reference-issuer", b"p" * 32, legacy.authority.generator_controller_ref)
    record = issuer.issue(policy, profile, "readability", suite, observations, host,
        expires_utc=(datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(), verification_seconds=30, readability=True)
    assert record["state"] == "qualified", record["reasons"]
    executor.qualification_configuration = lambda request: {"request": copy.deepcopy(request), "configuration": copy.deepcopy(settings)}
    return CalibratedEvaluators([record], issuer, legacy=legacy)
