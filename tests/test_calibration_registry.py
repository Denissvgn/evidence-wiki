"""Opt-in protected qualification with exact current finite data and host state."""

import copy

import pytest

from evidence_wiki.agent_execution import ExecutionAttempt, ExecutionProfile, ExecutionRole, _digest
from evidence_wiki.calibration_registry import CalibratedEvaluators, CalibrationPlan, CalibrationSelection
from evidence_wiki.execution_roles import ExecutionRoleRegistry, ExecutionRoleScope
from evidence_wiki.finite_domain import DESCRIPTOR, DOMAIN, observe_domain
from evidence_wiki.qualification_calibration import CalibrationAuthority, calibration_suite, digest
from evidence_wiki.reference_oracles import protected_cases
from evidence_wiki.run_policy import RunPolicy
from evidence_wiki.workflow_contracts import EXECUTION_OBSERVATION, JOB_V3
from tests.test_finite_domain import inputs
from tests.test_qualification_calibration import AuthenticatedHost
from tests.test_run_policy import observation, policy_document
from tests.test_workflow_contracts import job

NOW = "2026-10-01T12:00:00Z"


@pytest.fixture(scope="module", params=("artifact_checked", "host_enforced"))
def calibrated(request):
    context = inputs()
    rubric = context["rubric"]
    suite = calibration_suite(protected_cases(), suite_ref="finite-references", split_ref="protected-holdout",
        domains=[DOMAIN], rubric=rubric, model_settings={"temperature": "0"})
    selection = CalibrationSelection("profile-a", suite, model_revision="explicit-revision")
    scopes = ExecutionRoleRegistry((ExecutionRoleScope("researcher", "researcher", ("model",)),
        ExecutionRoleScope("profile-a", "evaluator", ("model",))))
    profile = ExecutionProfile("calibrated-profile", 1000, "EUR", "host-price", (
        ExecutionRole("researcher", "host_worker", "generator", "generator-model", "generate/v1"),
        ExecutionRole("profile-a", "evaluator", "controller-a", "reference-model", "prompt/v1")),
        1, 0, 1, "0", 1, role_scopes=scopes, calibration=CalibrationPlan((selection,)))
    policy = RunPolicy(policy_document() | {"assurance": request.param})
    selected = selection.document()
    configuration = {"execution_profile_sha256": profile.sha256, "config_ref": "profile-a", "controller_ref": "controller-a",
        "model_ref": "reference-model", "prompt_version": "prompt/v1", "model_revision": selected["model_revision"],
        "model_settings_sha256": digest(selected["model_settings"]), "rubric_sha256": digest(rubric),
        "domains": [DOMAIN], "suite_sha256": suite["suite_sha256"], "split_ref": suite["split_ref"],
        "domain_descriptor_sha256": digest(DESCRIPTOR), "reference_assessment_ref": selected["reference_assessment"]["assessment_ref"],
        "execution_kind": "model", "active": True}
    host = AuthenticatedHost(profile, suite)
    host.calibration_configuration = lambda _config: copy.deepcopy(configuration)
    observations = []
    for index, case in enumerate(suite["cases"]):
        document = job("evaluate") | {"schema_version": JOB_V3, "expected_result_schema": EXECUTION_OBSERVATION,
            "id": f"reference-{index}", "controller_ref": "controller-a", "prompt_version": "prompt/v1", "policy_id": policy.policy_id,
            "scope": policy.to_dict()["scope"]["question_ids"], "required_capabilities": ["model"], "task_role": "evaluator",
            "role_scope_sha256": scopes.scope("profile-a").sha256, "context_id": f"context-{index}",
            "packet_sha256": _digest(case["input_document"]), "issued_utc": "2026-10-01T11:59:40Z",
            "deadline_utc": "2026-10-01T12:00:10Z", "budgets": profile.document()["job_budgets"]}
        attempt = ExecutionAttempt.create(attempt_id=f"attempt-{index}", nonce=f"nonce-{index}", run_id="reference-run",
            binding_id="protected-reference", profile=profile, config_ref="profile-a", job=document, input_document=case["input_document"])
        observations.append((attempt, host.receipt(attempt, case)))
    authority = CalibrationAuthority("protected-issuer", b"q" * 32, "generator")
    record = authority.issue(policy, profile, "profile-a", suite, observations, host,
        expires_utc="2026-10-02T12:00:00Z", observed_utc=NOW, verification_seconds=30)
    assert record["state"] == "qualified"
    registry = CalibratedEvaluators([record], authority)
    domain = observe_domain(**context)
    probe = {"semantic_basis_id": domain["semantic_basis_id"], "profile_sha256": profile.sha256,
        "registry_sha256": registry.sha256, "observed_utc": "2026-10-01T11:59:59Z",
        "expires_utc": "2026-10-01T12:00:20Z", "configurations": {"profile-a": configuration}, "reasons": []}
    return policy, profile, registry, context, domain, probe


def rows(calibrated, *, domain=None, probe=None, **kwargs):
    policy, profile, registry, context, original_domain, original_probe = calibrated
    return registry.observations(policy, profile, NOW, context["rubric"]["id"], rubric=context["rubric"],
        rubric_sha256=digest(context["rubric"]), domain=original_domain if domain is None else domain,
        probe=original_probe if probe is None else probe, **kwargs)


def test_new_qualification_requires_current_domain_probe_and_exact_protected_configuration(calibrated):
    row = rows(calibrated)[0]
    assert row["qualified"] and row["independent"] and row["reference_cases"] == 100 and row["families"] == 10
    assert rows(calibrated, domain={"state": "unknown"})[0]["qualified"] is None


def test_calibrated_metrics_cannot_claim_the_policy_host_boundary(calibrated):
    observed = rows(calibrated)[0]
    assert observed["qualified"] is True
    assert observed["assurance"] == "artifact_checked"
    policy = calibrated[0]
    owner = observation(policy)
    owner["evaluators"] = [observed]
    decision = policy.decide(owner)
    assert decision["outcome"] == ("blocked_external" if policy.to_dict()["assurance"] == "host_enforced" else "accepted")


@pytest.mark.parametrize("change", [{"active": False}, {"active": 1}, {"model_revision": "changed"},
    {"model_settings_sha256": "0" * 64}, {"reference_assessment_ref": "opaque-assertion"},
    {"suite_sha256": "0" * 64}, {"execution_kind": "offline"}])
def test_current_withdrawal_or_configuration_drift_never_qualifies(calibrated, change):
    probe = copy.deepcopy(calibrated[-1])
    probe["configurations"]["profile-a"].update(change)
    assert rows(calibrated, probe=probe)[0]["qualified"] is None


@pytest.mark.parametrize("change", [{"semantic_basis_id": "sha256:" + "0" * 64},
    {"registry_sha256": "0" * 64}, {"profile_sha256": "0" * 64},
    {"expires_utc": NOW}, {"reasons": ["host_wait_expired"]}])
def test_old_or_incomplete_probe_is_inert(calibrated, change):
    assert rows(calibrated, probe=copy.deepcopy(calibrated[-1]) | change)[0]["qualified"] is None


def test_unrestricted_prose_cannot_adopt_finite_reference_domain(calibrated):
    context = copy.deepcopy(calibrated[3])
    context["strict_claims"][0]["text"] = "A persuasive broad claim with no finite declaration."
    domain = observe_domain(**context)
    assert domain["state"] == "unknown"
    assert rows(calibrated, domain=domain)[0]["qualified"] is None
