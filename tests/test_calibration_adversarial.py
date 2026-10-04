"""Independent calibration attacks; signed host doubles prove mechanics only."""

import copy
import hashlib
import hmac
from decimal import Decimal, localcontext

import pytest

from evidence_wiki import qualification_calibration as owner
from evidence_wiki.agent_execution import ExecutionAttempt, ExecutionProfile, ExecutionRole, _digest
from evidence_wiki.agent_qualification import QualificationError
from evidence_wiki.mutation_contracts import canonical
from evidence_wiki.reference_oracles import assess_references, oracle_label, protected_cases, reproduce_label
from evidence_wiki.run_policy import RunPolicy
from tests.test_run_policy import policy_document
from tests.test_workflow_contracts import job

NOW = "2026-10-01T12:00:00Z"
EXPIRY = "2026-10-02T12:00:00Z"


def integer_oracle(document):
    facts, claim = document["facts"], document["claim"]
    if facts is None:
        return "unknown"
    if any(claim[key] != facts[key] for key in ("units", "scope", "time")) or facts["contradiction"]:
        return "reject"
    a, b = map(int, facts["values"])
    parts = claim["value"].split("/")
    numerator, denominator = int(parts[0]), int(parts[1]) if len(parts) == 2 else 1
    operation = facts["operation"]
    equal = {"sum": lambda: numerator == (a + b) * denominator,
             "difference": lambda: numerator == (a - b) * denominator,
             "product": lambda: numerator == a * b * denominator,
             "mean": lambda: numerator * 2 == (a + b) * denominator,
             "ratio": lambda: numerator * b == a * denominator}[operation]()
    return "accept" if equal else "reject"


def frozen_suite(cases):
    return owner.calibration_suite(cases, suite_ref="audit-reference", split_ref="held-out",
        domains=[cases[0]["domain_ref"]], rubric={"id": "audit-rubric", "revision": "1",
        "criteria": {"support": "Check the exact retained finite facts."}}, model_settings={"seed": 17, "temperature": "0"})


def test_every_finite_reference_is_reproduced_by_integer_cross_multiplication():
    cases = protected_cases()
    assert len(cases) == 600
    for case in cases:
        assert integer_oracle(case["input_document"]) == case["expected_label"]


def test_material_clones_cannot_be_independent_by_salted_reference_identity():
    originals = protected_cases()
    representatives = {label: next(case for case in originals if case["kind"] == "baseline" and case["expected_label"] == label)
                       for label in ("accept", "reject", "unknown")}
    clones = []
    for index in range(100):
        base = copy.deepcopy(representatives[("accept", "reject", "unknown")[index % 3]])
        base.update(case_id=f"clone-{index}", unit_ref=f"unit-{index}", family_id=f"family-{index % 10}")
        base["reference"].update(reference_ref=f"renamed-oracle-{index}", basis_sha256=owner.digest({"salt": index}))
        clones.append(base)
        for axis in owner.AXES:
            variant = copy.deepcopy(base)
            variant.update(case_id=f"{axis}-{index}", kind=axis, base_case_id=base["case_id"])
            if axis != "repeat":
                variant["input_document"]["presentation"]["instruction"] = axis
            clones.append(variant)
    try:
        selected = frozen_suite(clones)
    except QualificationError:
        return
    results = {case["case_id"]: case["expected_label"] for case in selected["cases"]}
    score = owner._stratum(selected["cases"], results, RunPolicy(policy_document()).to_dict())
    assert score["state"] != "qualified", "Three actual materials became 100 independent references across ten relabelled families."


def test_repeat_axis_requires_the_original_input_material():
    cases = protected_cases()
    variant = next(case for case in cases if case["kind"] == "repeat")
    variant["input_document"]["facts"]["values"] = ["999", "998"]
    with pytest.raises(QualificationError):
        frozen_suite(cases)


def test_reference_proof_cannot_rename_sum_material_as_other_arithmetic_families():
    cases = protected_cases()
    for case in cases:
        facts = case["input_document"]["facts"]
        if facts is not None:
            facts["operation"] = "sum"
            case["input_document"]["claim"]["value"] = str(sum(map(int, facts["values"])) + (case["expected_label"] == "reject"))
    with pytest.raises(ValueError):
        assess_references(cases)


def test_equivalent_rational_spelling_cannot_create_independent_reference_materials():
    original = next(case for case in protected_cases() if case["kind"] == "baseline" and case["expected_label"] == "accept")
    cases = []
    for index in range(1, 101):
        clone = copy.deepcopy(original)
        clone.update(case_id=f"numeric-clone-{index}", unit_ref=f"numeric-unit-{index}")
        facts = clone["input_document"]["facts"]
        facts["values"] = [f"{int(value) * index}/{index}" for value in facts["values"]]
        claim = clone["input_document"]["claim"]
        claim["value"] = f"{int(claim['value']) * index}/{index}"
        clone["reference"]["basis_sha256"] = owner.digest({"spelling": index})
        cases.append(clone)
    with pytest.raises(ValueError):
        assess_references(cases)


def test_independent_oracles_agree_when_ratio_is_undefined_and_metadata_differs():
    document = copy.deepcopy(protected_cases()[0]["input_document"])
    document["facts"].update(operation="ratio", values=["1", "0"])
    document["claim"]["units"] = "different-units"
    assert oracle_label(document) == reproduce_label(document)


@pytest.mark.parametrize("operation,values,claim", [
    ("product", ["999999999999/999999999998", "999999999997/999999999996"], "1"),
    ("sum", ["-999999999999/999999999998", "999999999997/999999999996"], "0"),
    ("ratio", ["999999999999/999999999998", "-999999999997/999999999996"], "-1"),
])
@pytest.mark.parametrize("precision", [1, 6, 28])
def test_oracle_numeric_boundary_reproduction_ignores_ambient_precision(operation, values, claim, precision):
    document = copy.deepcopy(protected_cases()[0]["input_document"])
    document["facts"].update(operation=operation, values=values)
    document["claim"]["value"] = claim
    with localcontext() as context:
        context.prec = precision
        assert oracle_label(document) == reproduce_label(document)


class SignedObservationHostDouble:
    """Host assertion and receipt correlation double; no live model is invoked."""

    def __init__(self, profile, suite):
        self.profile, self.suite = profile, suite
        self.key = b"a" * 32
        role = profile.roles[0]
        self.configuration = {"execution_profile_sha256": profile.sha256, "config_ref": role.config_ref,
            "controller_ref": role.controller_ref, "model_ref": role.model_ref, "prompt_version": role.prompt_version,
            "model_settings_sha256": owner.digest(suite["model_settings"]), "rubric_sha256": owner.digest(suite["rubric"]),
            "domains": suite["domains"], "suite_sha256": suite["suite_sha256"], "split_ref": suite["split_ref"],
            "execution_kind": "model", "reference_assessment_ref": "host-selected-reference"}

    def calibration_configuration(self, config_ref):
        return copy.deepcopy(self.configuration)

    def signed(self, value):
        result = {key: copy.deepcopy(part) for key, part in value.items() if key != "authentication"}
        result["authentication"] = hmac.new(self.key, canonical(result), hashlib.sha256).hexdigest()
        return result

    def authenticate(self, supplied, attempt):
        if supplied != self.signed(supplied):
            raise QualificationError("audit_host_authentication_refused")
        return copy.deepcopy(supplied)

    def receipt(self, attempt, case):
        role = self.profile.roles[0]
        output = {"case_id": case["case_id"], "label": integer_oracle(case["input_document"])}
        return self.signed({"schema_version": "research-execution-observation/v1", "run_id": attempt.run_id,
            "binding_id": attempt.binding_id, "profile_sha256": self.profile.sha256, "attempt_id": attempt.attempt_id,
            "nonce": attempt.nonce, "job_id": attempt.job["id"], "job_sha256": attempt.job_sha256,
            "input_sha256": attempt.input_sha256, "controller_ref": role.controller_ref, "config_ref": role.config_ref,
            "model_ref": role.model_ref, "prompt_version": role.prompt_version, "sequence": 1,
            "issued_utc": attempt.job["issued_utc"], "deadline_utc": attempt.job["deadline_utc"],
            "started_utc": "2026-10-01T11:59:55Z", "completed_utc": "2026-10-01T11:59:56Z",
            "observed_utc": "2026-10-01T11:59:56Z", "output": output, "output_sha256": _digest(output),
            "currency": self.profile.currency, "pricing_ref": self.profile.pricing_ref, "state": "succeeded", "reason": "completed",
            "observed_usage": {"seconds": 1, "model_calls": 1, "tool_calls": 0, "tokens": 1, "cost": "0"},
            "final_accounting": True, "cancellation_confirmed": False})


@pytest.fixture(scope="module")
def candidate():
    policy = RunPolicy(policy_document())
    profile = ExecutionProfile("audit-host", 1000, "EUR", "audit-price", (
        ExecutionRole("profile-a", "evaluator", "controller-a", "declared-host-model", "review/v1"),), 1, 0, 1, "0", 1)
    selected = frozen_suite(protected_cases())
    host = SignedObservationHostDouble(profile, selected)
    observations = []
    for index, case in enumerate(selected["cases"]):
        document = job("evaluate") | {"id": f"audit-job-{index}", "controller_ref": "controller-a", "prompt_version": "review/v1",
            "policy_id": policy.policy_id, "scope": policy.to_dict()["scope"]["question_ids"],
            "issued_utc": "2026-10-01T11:59:40Z", "deadline_utc": "2026-10-01T12:00:10Z",
            "budgets": {"model_calls": 1, "tool_calls": 0, "tokens": 1, "cost": "0"}}
        attempt = ExecutionAttempt.create(attempt_id=f"audit-attempt-{index}", nonce=f"audit-nonce-{index}",
            run_id="calibration-audit", binding_id="audit-binding", profile=profile, config_ref="profile-a",
            job=document, input_document=case["input_document"])
        observations.append((attempt, host.receipt(attempt, case)))
    authority = owner.CalibrationAuthority("audit-issuer", b"i" * 32, "generator")
    return policy, profile, selected, host, observations, authority


def issue(candidate, observations=None, host=None):
    policy, profile, selected, chosen_host, supplied, authority = candidate
    return authority.issue(policy, profile, "profile-a", selected, supplied if observations is None else observations,
                           chosen_host if host is None else host, expires_utc=EXPIRY, observed_utc=NOW, verification_seconds=30)


@pytest.mark.parametrize("field,value", [("run_id", "different-run"), ("binding_id", "different-binding"),
    ("job_id", "different-job"), ("issued_utc", "2026-10-01T11:59:41Z"),
    ("deadline_utc", "2026-10-01T12:00:11Z"), ("currency", "USD"), ("pricing_ref", "different-price")])
def test_authentication_cannot_substitute_for_exact_receipt_correlation(candidate, field, value):
    supplied = list(candidate[4])
    attempt, observed = supplied[0]
    changed = candidate[3].signed(observed | {field: value})
    supplied[0] = (attempt, changed)
    try:
        record = issue(candidate, supplied)
    except QualificationError:
        return
    assert record["state"] != "qualified", f"A mismatched {field} receipt qualified."


def test_receipt_overrun_cannot_qualify(candidate):
    supplied = list(candidate[4])
    attempt, observed = supplied[0]
    usage = observed["observed_usage"] | {"model_calls": 2, "tokens": 2, "cost": "1"}
    supplied[0] = (attempt, candidate[3].signed(observed | {"observed_usage": usage}))
    try:
        record = issue(candidate, supplied)
    except QualificationError:
        return
    assert record["state"] != "qualified", "Observed consumption exceeded both attempt and profile ceilings."


def test_job_budget_cannot_expand_the_frozen_execution_profile(candidate):
    supplied = list(candidate[4])
    attempt, _ = supplied[0]
    expanded = ExecutionAttempt.create(attempt_id=attempt.attempt_id, nonce=attempt.nonce, run_id=attempt.run_id,
        binding_id=attempt.binding_id, profile=candidate[1], config_ref=attempt.config_ref,
        job=attempt.job | {"budgets": {"model_calls": 2, "tool_calls": 0, "tokens": 2, "cost": "1"}}, input_document=attempt.input_document)
    observation = candidate[3].receipt(expanded, candidate[2]["cases"][0])
    supplied[0] = (expanded, candidate[3].signed(observation | {"observed_usage": observation["observed_usage"] | {
        "model_calls": 2, "tokens": 2, "cost": "1"}}))
    try:
        record = issue(candidate, supplied)
    except QualificationError:
        return
    assert record["state"] != "qualified", "A larger job budget bypassed the frozen per-job profile ceiling."


def test_calibration_job_cannot_use_a_different_controller_or_prompt(candidate):
    supplied = list(candidate[4])
    attempt, _ = supplied[0]
    changed = ExecutionAttempt.create(attempt_id=attempt.attempt_id, nonce=attempt.nonce, run_id=attempt.run_id,
        binding_id=attempt.binding_id, profile=candidate[1], config_ref=attempt.config_ref,
        job=attempt.job | {"controller_ref": "generator", "prompt_version": "other-prompt"}, input_document=attempt.input_document)
    supplied[0] = (changed, candidate[3].receipt(changed, candidate[2]["cases"][0]))
    try:
        record = issue(candidate, supplied)
    except QualificationError:
        return
    assert record["state"] != "qualified", "The dispatched job identity differs from the asserted evaluator."


def test_provenance_assessment_reference_is_a_typed_host_identity(candidate):
    host = SignedObservationHostDouble(candidate[1], candidate[2])
    host.configuration["reference_assessment_ref"] = {"arbitrary": "truthy"}
    try:
        record = issue(candidate, host=host)
    except QualificationError:
        return
    assert record["state"] != "qualified", "An arbitrary object substituted for provenance identity."


@pytest.mark.parametrize("change", ["settings", "hash", "split", "label"])
def test_calibration_selection_reconstructs_the_exact_suite_before_freezing(change):
    from evidence_wiki.calibration_registry import CalibrationSelection

    selected = frozen_suite(protected_cases())
    if change == "settings":
        selected["model_settings"]["seed"] = 99
    elif change == "hash":
        selected["suite_sha256"] = "f" * 64
    elif change == "split":
        selected["split_ref"] = "training"
    else:
        selected["cases"][0]["expected_label"] = "reject"
    with pytest.raises((ValueError, QualificationError)):
        CalibrationSelection("profile-a", selected, model_revision="exact-revision")


@pytest.mark.parametrize("bound", [float("nan"), float("inf"), float("-inf"), True, Decimal("1"), 0, 31])
def test_nonfinite_or_unbounded_verification_waits_are_refused(candidate, bound):
    policy, profile, selected, host, supplied, authority = candidate
    with pytest.raises(QualificationError):
        authority.issue(policy, profile, "profile-a", selected, [], host, expires_utc=EXPIRY,
                        observed_utc=NOW, verification_seconds=bound)


@pytest.mark.parametrize("precision", [1, 3, 28])
def test_policy_boundary_uses_exact_decimal_ratios(precision):
    cases = protected_cases()
    results = {case["case_id"]: integer_oracle(case["input_document"]) for case in cases}
    positive = [case for case in cases if case["kind"] == "identity" and case["expected_label"] == "accept"][:6]
    for case in positive:
        results[case["case_id"]] = "reject"
    with localcontext() as context:
        context.prec = precision
        report = owner._stratum(cases, results, RunPolicy(policy_document()).to_dict())
    assert report["state"] == "unqualified" and report["axes"]["identity"]["flip_rate"] == "0.06"


@pytest.mark.parametrize("change", [{"model_calls": 0}, {"model_calls": None}, {"reason": "offline_fixture"}])
def test_current_application_rejects_authenticated_review_without_observed_model(candidate, monkeypatch, change):
    from dataclasses import replace
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    from evidence_wiki import execution_session as driver
    from evidence_wiki.agent_execution import validate_observation
    from evidence_wiki.calibration_registry import CalibrationPlan, CalibrationSelection
    from evidence_wiki.execution_roles import ExecutionRoleRegistry, ExecutionRoleScope
    from evidence_wiki.workflow_contracts import EXECUTION_OBSERVATION, JOB_V3, WorkflowContractError

    policy, base_profile, selected, _, _, _ = candidate
    scope = ExecutionRoleScope("profile-a", "evaluator", ("model",))
    profile = replace(base_profile, role_scopes=ExecutionRoleRegistry((scope,)),
        calibration=CalibrationPlan((CalibrationSelection("profile-a", selected, model_revision="exact-model"),)))
    now = datetime.now(timezone.utc)
    document = job("evaluate") | {"schema_version": JOB_V3, "expected_result_schema": EXECUTION_OBSERVATION,
        "controller_ref": "controller-a", "prompt_version": "review/v1", "policy_id": policy.policy_id,
        "issued_utc": now.isoformat(), "deadline_utc": (now + timedelta(seconds=30)).isoformat(), "timeout_seconds": 20,
        "budgets": profile.document()["job_budgets"], "task_role": "evaluator", "context_id": "current-context",
        "role_scope_sha256": scope.sha256, "packet_sha256": _digest(selected["cases"][0]["input_document"]),
        "required_capabilities": ["model"]}
    attempt = ExecutionAttempt.create(attempt_id="current-review", nonce="current-nonce", run_id="current-run",
        binding_id="current-binding", profile=profile, config_ref="profile-a", job=document,
        input_document=selected["cases"][0]["input_document"])
    host = SignedObservationHostDouble(profile, selected)
    observation = host.receipt(attempt, selected["cases"][0])
    observation.update(started_utc=now.isoformat(), completed_utc=now.isoformat(), observed_utc=now.isoformat())
    observed = validate_observation(host.authenticate(host.signed(observation), attempt))
    session = SimpleNamespace(policy=policy, execution=SimpleNamespace(profile=profile))
    view = {"basis_id": attempt.job["basis_id"], "qualification": {"state": "qualified"},
        "execution": {"accounting": {"overrun": False}}, "usage": {"claims": 0, "sources": 0}}
    monkeypatch.setattr(driver, "retained", lambda _: [])
    monkeypatch.setattr(driver, "current", lambda _: view)
    driver._validate_application(session, attempt, observed, None)
    if "model_calls" in change:
        observation["observed_usage"]["model_calls"] = change["model_calls"]
        observation["final_accounting"] = change["model_calls"] is not None
    else:
        observation["reason"] = change["reason"]
    changed = validate_observation(host.authenticate(host.signed(observation), attempt))
    with pytest.raises(WorkflowContractError, match="refused"):
        driver._validate_application(session, attempt, changed, None)


def test_unaccepted_generation_can_complete_without_model_review_qualification(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    from evidence_wiki import execution_session as driver

    now = datetime.now(timezone.utc)
    policy = RunPolicy(policy_document())
    session = SimpleNamespace(policy=policy, execution=SimpleNamespace(profile=SimpleNamespace(calibration=object())))
    attempt = SimpleNamespace(job={"operation": "generate", "basis_id": "basis",
        "deadline_utc": (now + timedelta(seconds=30)).isoformat()})
    view = {"basis_id": "basis", "qualification": {"state": "unknown"},
        "execution": {"accounting": {"overrun": False}}, "usage": {"claims": 0, "sources": 0}}
    monkeypatch.setattr(driver, "retained", lambda _: [])
    monkeypatch.setattr(driver, "current", lambda _: view)
    driver._validate_application(session, attempt, {"state": "succeeded", "final_accounting": True,
        "observed_usage": {"model_calls": 0}, "reason": "offline_fixture"}, None)
