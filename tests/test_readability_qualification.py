"""Controlled HMAC host doubles check purpose isolation, not genuine model use."""

import copy
import hashlib
import hmac
from dataclasses import replace

import pytest

from evidence_wiki.agent_execution import ExecutionAttempt
from evidence_wiki.agent_qualification import QualificationAuthority, QualifiedEvaluators
from evidence_wiki.calibration_registry import CalibratedEvaluators
from evidence_wiki.mutation_contracts import canonical, digest
from evidence_wiki.qualification_calibration import REPORT_READABILITY, CalibrationAuthority
from evidence_wiki.readability_contracts import OUTPUT
from evidence_wiki.readability_reference import DOMAIN, expected_issues, observe_domain
from evidence_wiki.workflow_contracts import EXECUTION_OBSERVATION, JOB_V3
from tests.test_disagreement import selected_policy
from tests.test_qualification_calibration import EXPIRY, NOW, AuthenticatedHost
from tests.test_readability_context import context
from tests.test_readability_profile import profile as profile
from tests.test_workflow_contracts import job


@pytest.fixture(scope="module")
def issued(profile):
    selected = replace(profile[1], calibration=None)
    suite = profile[2]
    policy = selected_policy()
    descriptor = selected.readability.selection.document()
    configuration = {"execution_profile_sha256": selected.sha256, "config_ref": "readability",
        "controller_ref": "presentation-controller", "model_ref": "presentation-model", "prompt_version": "presentation/v1",
        "model_revision": descriptor["model_revision"], "model_settings_sha256": hashlib.sha256(canonical(descriptor["model_settings"])).hexdigest(),
        "rubric_sha256": descriptor["rubric_sha256"], "domains": [DOMAIN], "suite_sha256": descriptor["suite_sha256"],
        "split_ref": descriptor["split_ref"], "reference_assessment_ref": descriptor["reference_assessment"]["assessment_ref"],
        "domain_descriptor_sha256": hashlib.sha256(canonical(descriptor["domain_descriptor"])).hexdigest(), "active": True, "execution_kind": "model"}
    host = AuthenticatedHost(selected, suite)
    host.calibration_configuration = lambda _ref: copy.deepcopy(configuration)
    observations = []
    scope = selected.role_scopes.scope("readability")
    for index, case in enumerate(suite["cases"]):
        packet = case["input_document"]["packet"]
        value = job("evaluate") | {"schema_version": JOB_V3, "expected_result_schema": EXECUTION_OBSERVATION,
            "id": "calibration-" + str(index), "controller_ref": "presentation-controller", "prompt_version": "presentation/v1",
            "policy_id": policy.policy_id, "scope": policy.to_dict()["scope"]["question_ids"], "task_role": "critic",
            "required_capabilities": ["model", "review.read", "objection.propose"], "role_scope_sha256": scope.sha256,
            "context_id": packet["context_id"], "packet_sha256": digest(case["input_document"]).removeprefix("sha256:"),
            "payload": {"qualification_purpose": "readability", "calibration_case_id": case["case_id"]},
            "issued_utc": "2026-10-01T11:59:40Z", "deadline_utc": "2026-10-01T12:00:10Z", "timeout_seconds": 30,
            "budgets": selected.document()["job_budgets"]}
        attempt = ExecutionAttempt.create(attempt_id=value["id"], nonce="nonce-" + str(index), run_id="references",
            binding_id="protected-references", profile=selected, config_ref="readability", job=value, input_document=case["input_document"])
        response = host.receipt(attempt, case)
        issues = expected_issues(case["input_document"])
        output = {"schema_version": OUTPUT, "context_id": packet["context_id"], "input_sha256": digest(packet),
            "state": "abstain" if issues is None else "issues", "issues": issues or []}
        response.update(config_ref="readability", controller_ref="presentation-controller", model_ref="presentation-model",
            prompt_version="presentation/v1", pricing_ref=selected.pricing_ref, output=output,
            output_sha256=digest(output).removeprefix("sha256:"))
        response.pop("authentication")
        response["authentication"] = hmac.new(host.key, canonical(response), hashlib.sha256).hexdigest()
        observations.append((attempt, response))
    authority = CalibrationAuthority("presentation-issuer", b"p" * 32, "generator-controller")
    record = authority.issue(policy, selected, "readability", suite, observations, host,
        observed_utc=NOW, expires_utc=EXPIRY, verification_seconds=30, readability=True)
    return policy, selected, authority, record, configuration


def test_protected_purpose_cannot_become_an_ordinary_or_auxiliary_vote(issued):
    policy, profile, authority, record, _configuration = issued
    assert record["schema_version"] == REPORT_READABILITY and record["state"] == "qualified"
    assert record["binding"]["qualification_purpose"] == "readability"
    selected = profile.readability.selection.document()
    arguments = dict(policy=policy, profile=profile, config_ref="readability", rubric=selected["rubric"],
        model_settings=selected["model_settings"], domain_ref=DOMAIN, now_utc=NOW, drift_ref=selected["drift_ref"])
    assert authority.current(record, **arguments, readability=True)["state"] == "qualified"
    assert "qualification_purpose_mismatch" in authority.current(record, **arguments)["reasons"]
    assert authority.current(record, **arguments, revoked=True, readability=True)["state"] == "unknown"


def test_composite_keeps_exact_legacy_owner_and_current_purpose_expiry(issued):
    policy, profile, authority, record, configuration = issued
    legacy = QualifiedEvaluators([], QualificationAuthority("legacy-issuer", b"l" * 32, "generator-controller"))
    registry = CalibratedEvaluators([record], authority, legacy=legacy)
    assert registry.observations(policy, profile, NOW, "science") == legacy.observations(policy, profile, NOW, "science")
    brief, _packet, _binding = context()
    domain = observe_domain(brief)
    probe = {"profile_sha256": profile.sha256, "registry_sha256": registry.sha256,
        "semantic_basis_id": domain["semantic_basis_id"], "observed_utc": NOW, "expires_utc": "2026-10-01T12:00:20Z",
        "configuration": configuration, "reasons": []}
    assert registry.readability_observation(policy, profile, NOW, domain=domain, probe=probe)["state"] == "qualified"
    assert registry.readability_observation(policy, profile, "2026-10-01T12:00:20Z", domain=domain, probe=probe)["state"] == "unknown"
    assert registry.readability_application_observation(policy, profile, "2026-10-01T12:01:00Z",
        domain=domain, probe=probe, checked_utc="2026-10-01T12:00:10Z")["state"] == "qualified"
    assert registry.readability_application_observation(policy, profile, EXPIRY,
        domain=domain, probe=probe, checked_utc="2026-10-01T12:00:10Z")["state"] == "unknown"
    for checked in ["2026-10-01T11:59:59Z", "2026-10-01T12:00:20Z", "2026-10-01T12:01:01Z"]:
        assert registry.readability_application_observation(policy, profile, "2026-10-01T12:01:00Z",
            domain=domain, probe=probe, checked_utc=checked)["state"] == "unknown"
    wrong = QualifiedEvaluators([], QualificationAuthority("foreign-issuer", b"f" * 32, "foreign-generator"))
    with pytest.raises(ValueError, match="legacy_owner"):
        CalibratedEvaluators([record], authority, legacy=wrong)
