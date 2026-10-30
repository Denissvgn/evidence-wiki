"""Authenticated auxiliary calibration stays separate from required first passes."""

import copy
import hashlib
import hmac

import pytest

from evidence_wiki.agent_execution import ExecutionAttempt, ExecutionError, ExecutionProfile, ExecutionRole, _digest
from evidence_wiki.calibration_registry import CalibratedEvaluators, CalibrationPlan, CalibrationSelection
from evidence_wiki.disagreement_configuration import DisagreementPlan, auxiliary_observation
from evidence_wiki.execution_roles import ExecutionRoleRegistry, ExecutionRoleScope
from evidence_wiki.finite_domain import DESCRIPTOR, DOMAIN, observe_domain
from evidence_wiki.mutation_contracts import canonical
from evidence_wiki.qualification_calibration import CalibrationAuthority, calibration_suite, digest
from evidence_wiki.reference_oracles import protected_cases, reproduce_label
from evidence_wiki.workflow_contracts import EXECUTION_OBSERVATION, JOB_V3
from tests.test_disagreement import selected_policy
from tests.test_finite_domain import inputs
from tests.test_workflow_contracts import job

NOW = "2026-10-01T12:00:00Z"


class ProtectedHost:
    """Controlled HMAC metadata/receipt host; no live model/provider is invoked."""

    key = b"h" * 32

    def __init__(self, profile):
        self.profile = profile

    def calibration_configuration(self, config):
        role, selected = self.profile.role(config), self.profile.calibration.selection(config).document()
        return {"execution_profile_sha256": self.profile.sha256, "config_ref": config,
            "controller_ref": role.controller_ref, "model_ref": role.model_ref, "prompt_version": role.prompt_version,
            "model_revision": selected["model_revision"], "model_settings_sha256": digest(selected["model_settings"]),
            "rubric_sha256": selected["rubric_sha256"], "suite_sha256": selected["suite_sha256"], "split_ref": selected["split_ref"],
            "reference_assessment_ref": selected["reference_assessment"]["assessment_ref"],
            "domain_descriptor_sha256": digest(DESCRIPTOR), "domains": [DOMAIN], "active": True, "execution_kind": "model"}

    def receipt(self, attempt, case):
        role = self.profile.role(attempt.config_ref)
        output = {"case_id": case["case_id"], "label": reproduce_label(case["input_document"])}
        value = {"schema_version": "research-execution-observation/v1", "run_id": attempt.run_id,
            "binding_id": attempt.binding_id, "profile_sha256": self.profile.sha256, "attempt_id": attempt.attempt_id,
            "nonce": attempt.nonce, "job_id": attempt.job["id"], "job_sha256": attempt.job_sha256, "input_sha256": attempt.input_sha256,
            "config_ref": role.config_ref, "controller_ref": role.controller_ref, "model_ref": role.model_ref,
            "prompt_version": role.prompt_version, "sequence": 1, "issued_utc": attempt.job["issued_utc"],
            "deadline_utc": attempt.job["deadline_utc"], "started_utc": "2026-10-01T11:59:55Z",
            "completed_utc": "2026-10-01T11:59:56Z", "observed_utc": "2026-10-01T11:59:56Z",
            "output": output, "output_sha256": _digest(output), "currency": self.profile.currency,
            "pricing_ref": self.profile.pricing_ref, "state": "succeeded", "reason": "completed",
            "observed_usage": {"seconds": 1, "model_calls": 1, "tool_calls": 0, "tokens": 1, "cost": "0"},
            "final_accounting": True, "cancellation_confirmed": False}
        value["authentication"] = hmac.new(self.key, canonical(value), hashlib.sha256).hexdigest()
        return value

    def authenticate(self, value, attempt):
        unsigned = {key: part for key, part in value.items() if key != "authentication"}
        expected = hmac.new(self.key, canonical(unsigned), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, value["authentication"]):
            raise ValueError("host_authentication_refused")
        return copy.deepcopy(value)


@pytest.fixture(scope="module")
def calibrated():
    context, policy = inputs(), selected_policy()
    suite = calibration_suite(protected_cases(), suite_ref="protected", split_ref="held-out", domains=[DOMAIN],
        rubric=context["rubric"], model_settings={"temperature": "0"})
    selectors = ("profile-a", "critic", "investigator")
    calibration = CalibrationPlan(tuple(CalibrationSelection(config, suite, model_revision="observed-revision") for config in selectors))
    scopes = ExecutionRoleRegistry((ExecutionRoleScope("generator", "researcher", ("model",)),
        ExecutionRoleScope("profile-a", "evaluator", ("model",)), ExecutionRoleScope("critic", "critic", ("model",)),
        ExecutionRoleScope("investigator", "adjudicator", ("model",))))
    profile = ExecutionProfile("protected-profile", 1024, "EUR", "test-price", (
        ExecutionRole("generator", "host_worker", "generator", "generator-model", "generate/v1"),
        ExecutionRole("profile-a", "evaluator", "controller-a", "evidence-model", "review/v1"),
        ExecutionRole("critic", "evaluator", "critic-controller", "critic-model", "review/v1"),
        ExecutionRole("investigator", "evaluator", "investigator-controller", "investigator-model", "review/v1")),
        1, 0, 1, "0", 1, role_scopes=scopes, calibration=calibration, disagreement=DisagreementPlan("investigator"))
    host, authority = ProtectedHost(profile), CalibrationAuthority("protected-issuer", b"q" * 32, "generator")
    records = []
    for config in selectors:
        role = profile.role(config)
        observations = []
        for index, case in enumerate(suite["cases"]):
            document = job("evaluate") | {"schema_version": JOB_V3, "expected_result_schema": EXECUTION_OBSERVATION,
                "id": f"job-{index}", "role": "evaluator", "controller_ref": role.controller_ref,
                "prompt_version": role.prompt_version, "policy_id": policy.policy_id, "scope": policy.to_dict()["scope"]["question_ids"],
                "budgets": profile.document()["job_budgets"], "issued_utc": "2026-10-01T11:59:40Z",
                "deadline_utc": "2026-10-01T12:00:10Z", "required_capabilities": ["model"],
                "task_role": scopes.scope(config).task_role, "role_scope_sha256": scopes.scope(config).sha256,
                "context_id": f"context-{index}", "packet_sha256": _digest(case["input_document"])}
            value = ExecutionAttempt.create(attempt_id=f"attempt-{index}", nonce=f"nonce-{index}", run_id="calibration",
                binding_id="protected", profile=profile, config_ref=config, job=document, input_document=case["input_document"])
            observations.append((value, host.receipt(value, case)))
        records.append(authority.issue(policy, profile, config, suite, observations, host, observed_utc=NOW,
            expires_utc="2026-10-02T12:00:00Z", verification_seconds=30, auxiliary=config == "investigator"))
    registry = CalibratedEvaluators(records, authority)
    domain = observe_domain(**context)
    probe = {"profile_sha256": profile.sha256, "registry_sha256": registry.sha256,
        "semantic_basis_id": domain["semantic_basis_id"], "observed_utc": "2026-10-01T11:59:59Z",
        "expires_utc": "2026-10-01T12:00:20Z", "configurations": {config: host.calibration_configuration(config) for config in selectors},
        "configuration_reasons": {}, "reasons": []}
    return policy, profile, registry, authority, records, context, domain, probe


def rows(calibrated, probe=None, **kwargs):
    policy, profile, registry, _, _, context, domain, observed = calibrated
    return registry.observations(policy, profile, NOW, context["rubric"]["id"], rubric_sha256=digest(context["rubric"]),
        rubric=context["rubric"], domain=domain, probe=observed if probe is None else probe, **kwargs)


def test_actual_auxiliary_record_never_becomes_a_mandatory_firstpass(calibrated):
    policy, profile, registry, authority, records, context, domain, probe = calibrated
    assert all(row["qualified"] is True for row in rows(calibrated))
    assert {row["role"] for row in rows(calibrated)} == {"evaluator", "critic"}
    selected = profile.disagreement.auxiliary_profile(policy, profile)
    auxiliary = rows(calibrated, selected_profiles=[selected], auxiliary=True)
    assert len(auxiliary) == 1 and auxiliary[0]["qualified"] is True
    assert records[-1]["schema_version"] != records[0]["schema_version"]
    assert authority.verify(records[-1]) == records[-1]
    result = auxiliary_observation(profile.disagreement, registry, policy, profile, NOW, context["rubric"]["id"],
        rubric_sha256=digest(context["rubric"]), rubric=context["rubric"], domain=domain, probe=probe)
    assert result["qualified"] is True and not result["native_review_allowed"]


@pytest.mark.parametrize("change", ["missing", "failed", "inactive", "model_revision"])
def test_optional_failure_or_drift_does_not_poison_required_firstpasses(calibrated, change):
    policy, profile, _, _, _, _, _, original = calibrated
    probe = copy.deepcopy(original)
    if change == "missing":
        probe["configurations"].pop("investigator")
    elif change == "failed":
        probe["configuration_reasons"]["investigator"] = ["host_wait_expired"]
    elif change == "inactive":
        probe["configurations"]["investigator"]["active"] = False
    else:
        probe["configurations"]["investigator"]["model_revision"] = "different-revision"
    assert all(row["qualified"] is True for row in rows(calibrated, probe))
    selected = profile.disagreement.auxiliary_profile(policy, profile)
    assert rows(calibrated, probe, selected_profiles=[selected], auxiliary=True)[0]["qualified"] is None


def test_auxiliary_mode_cannot_select_an_existing_required_profile(calibrated):
    policy, _, _, _, _, _, _, _ = calibrated
    with pytest.raises(ExecutionError, match="auxiliary_selection_invalid"):
        rows(calibrated, selected_profiles=[policy.to_dict()["required_evaluators"][0]], auxiliary=True)
    with pytest.raises(ExecutionError, match="required_selection_changed"):
        rows(calibrated, selected_profiles=[])


def test_auxiliary_record_requires_explicit_current_mode(calibrated):
    policy, profile, _, authority, records, context, _, _ = calibrated
    current = authority.current(records[-1], policy=policy, profile=profile, config_ref="investigator", rubric=context["rubric"],
        model_settings={"temperature": "0"}, domain_ref=DOMAIN, now_utc=NOW)
    assert current["state"] == "unknown"
    current = authority.current(records[-1], policy=policy, profile=profile, config_ref="investigator", rubric=context["rubric"],
        model_settings={"temperature": "0"}, domain_ref=DOMAIN, now_utc=NOW, auxiliary=True)
    assert current["state"] == "qualified"


@pytest.mark.parametrize("failure", ["exception", "uncorrelated", "invalid"])
def test_probe_refresh_checks_mandatory_first_and_keeps_optional_failure_local(calibrated, monkeypatch, failure):
    from contextlib import contextmanager
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    from evidence_wiki import calibration_probes as owner
    from evidence_wiki import execution_session as driver

    policy, profile, registry, _, _, context, domain, _ = calibrated
    fixed = datetime.fromisoformat(NOW.replace("Z", "+00:00"))
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz is not None else fixed.replace(tzinfo=None)
    monkeypatch.setattr(owner, "datetime", Clock)
    lock, calls, saved = [False], [], []
    @contextmanager
    def gate(_session):
        assert not lock[0]
        lock[0] = True
        try:
            yield
        finally:
            lock[0] = False
    host = ProtectedHost(profile)
    def configuration(request):
        assert not lock[0], "Configuration callback ran under coordination gate."
        calls.append(request["config_ref"])
        if request["config_ref"] == "investigator":
            if failure == "exception":
                raise ValueError("Optional service unavailable.")
            if failure == "uncorrelated":
                return {"request": {**request, "nonce": "stale"}, "configuration": host.calibration_configuration("investigator")}
            return {"request": request, "configuration": {"private_key": "never-persist-this"}}
        return {"request": request, "configuration": host.calibration_configuration(request["config_ref"])}
    grants = []
    session = SimpleNamespace(policy=policy, context=SimpleNamespace(require_grant=grants.append),
        execution=SimpleNamespace(profile=profile, qualifications=registry,
        executor=SimpleNamespace(qualification_configuration=configuration)))
    monkeypatch.setattr(driver, "gate", gate)
    monkeypatch.setattr(driver, "_begin", lambda _: None)
    # This pure routing fixture supplies the already checked native policy;
    # actual workspace machine compatibility is covered by native journeys.
    monkeypatch.setattr(driver, "machine_compatible", lambda _: True)
    monkeypatch.setattr(driver, "retained", lambda _: [])
    monkeypatch.setattr(driver, "execution_budget_overrun", lambda *_: False)
    monkeypatch.setattr(driver, "deadline", lambda _: fixed + timedelta(seconds=60))
    monkeypatch.setattr(owner, "domain_observation", lambda _: domain)
    monkeypatch.setattr(driver, "append", lambda _session, _kind, _request, data: saved.append(data))
    owner.refresh(session, "probe-request")
    assert grants == ["research_session.intake_observation", "research_session.host_job.evaluate"]
    assert calls[-1] == "investigator" and set(calls[:-1]) == {"profile-a", "critic"}
    probe = saved[0]["probe"]
    assert probe["reasons"] == [] and probe["configuration_reasons"]["investigator"]
    assert "investigator" not in probe["configurations"]
    assert "private_key" not in str(saved) and "never-persist-this" not in str(saved)
    mandatory = rows(calibrated, probe)
    assert all(row["qualified"] is True for row in mandatory)
    assert fixed.tzinfo == timezone.utc
