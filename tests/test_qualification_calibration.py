"""Protected calibration arithmetic and host-double correlation, without live calls."""

import copy
import hashlib
import hmac
from decimal import localcontext

import pytest

from evidence_wiki import qualification_calibration as owner
from evidence_wiki.agent_execution import ExecutionAttempt, ExecutionProfile, ExecutionRole, _digest
from evidence_wiki.agent_qualification import QualificationError
from evidence_wiki.mutation_contracts import canonical
from evidence_wiki.run_policy import RunPolicy
from tests.test_run_policy import policy_document
from tests.test_workflow_contracts import job

NOW = "2026-10-01T12:00:00Z"
EXPIRY = "2026-10-02T12:00:00Z"
RUBRIC = {"id": "reference-rubric", "revision": "1", "criteria": {"support": "Check retained evidence."}}
SETTINGS = {"temperature": "0", "seed": 1}


def references():
    cases = []
    for index in range(100):
        label = "accept" if index < 45 else "reject" if index < 90 else "unknown"
        base = {"case_id": f"case-{index}", "unit_ref": f"unit-{index}", "family_id": f"family-{index % 10}",
            "domain_ref": "domain", "kind": "baseline", "base_case_id": None,
            "input_document": {"case": index, "presentation": "baseline"}, "expected_label": label,
            "critical": label == "reject", "reference": {"tier": "controlled_mutation",
                "reference_ref": f"oracle-{index}", "basis_sha256": owner.digest({"oracle": index})}}
        cases.append(base)
        for axis in owner.AXES:
            variant = copy.deepcopy(base)
            variant.update(case_id=f"{axis}-{index}", kind=axis, base_case_id=base["case_id"])
            if axis != "repeat":
                variant["input_document"]["presentation"] = axis
            cases.append(variant)
    return cases


def suite(cases=None):
    return owner.calibration_suite(references() if cases is None else cases, suite_ref="protected-suite",
        split_ref="held-out", domains=["domain"], rubric=RUBRIC, model_settings=SETTINGS)


def score(cases=None, changes=None):
    cases = references() if cases is None else cases
    results = {case["case_id"]: case["expected_label"] for case in cases}
    results.update(changes or {})
    return owner._stratum(cases, results, RunPolicy(policy_document()).to_dict())


def test_baseline_units_do_not_inflate_with_sensitivity_variants():
    result = score()
    assert result["state"] == "qualified"
    assert result["independent_reference_units"] == 100
    assert result["all_references"]["counts"]["expected_cases"] == 600
    assert all(axis["independent_units"] == 100 for axis in result["axes"].values())
    assert result["metrics"] == {"positive_acceptance": "1", "necessary_abstention": "1", "unnecessary_abstention": "0"}


@pytest.mark.parametrize("axis", owner.AXES)
def test_critical_false_acceptance_on_any_variant_is_disqualifying(axis):
    result = score(changes={f"{axis}-45": "accept"})
    assert result["state"] == "unqualified"
    assert "critical_false_acceptance" in result["reasons"]


def test_meaning_sensitivity_limit_is_preserved_for_every_axis():
    result = score(changes={f"identity-{index}": "reject" for index in range(6)})
    assert result["axes"]["identity"]["flip_rate"] == "0.06"
    assert result["state"] == "unqualified"
    boundary = score(changes={f"identity-{index}": "reject" for index in range(5)})
    assert boundary["axes"]["identity"]["flip_rate"] == "0.05"
    assert boundary["state"] == "qualified"


@pytest.mark.parametrize("precision", [1, 6, 28])
def test_score_ignores_ambient_decimal_context(precision):
    with localcontext() as context:
        context.prec = precision
        assert score(changes={f"meaning-{index}": "reject" for index in range(6)})["state"] == "unqualified"


def test_abstentions_have_separate_necessary_and_unnecessary_denominators():
    result = score(changes={"case-0": "unknown"})
    assert result["metrics"]["unnecessary_abstention"] == "0.011112"
    assert result["metrics"]["necessary_abstention"] == "1"
    assert result["state"] == "unknown"
    assert "unnecessary_abstention_coverage" in result["reasons"]
    assert score(changes={"verbosity-90": "reject"})["state"] == "unknown"


@pytest.mark.parametrize("mode", ["missing_axis", "duplicate_basis", "few_families", "agent_only"])
def test_inadequate_reference_protection_or_sampling_remains_unknown(mode):
    cases = references()
    if mode == "missing_axis":
        cases = [case for case in cases if case["kind"] != "position"]
    elif mode == "duplicate_basis":
        for case in cases:
            case["reference"]["basis_sha256"] = "a" * 64
    elif mode == "few_families":
        for case in cases:
            case["family_id"] = "one-family"
    else:
        for case in cases:
            case["reference"]["tier"] = "agent_judgment"
    result = score([case for case in cases if case["reference"]["tier"] in owner.ORACLE_TIERS])
    assert result["state"] == "unknown"


def test_same_logical_case_cannot_be_relabelled_as_independent():
    cases = references()
    clone = copy.deepcopy(cases[0])
    clone["case_id"] = "renamed"
    with pytest.raises(QualificationError, match="logical_unit_duplicate"):
        suite(cases + [clone])


def test_reference_labels_are_never_in_worker_input_including_nested_data():
    cases = references()
    cases[0]["input_document"]["nested"] = [{"reference_label": "accept"}]
    with pytest.raises(QualificationError, match="label_exposed"):
        suite(cases)


class AuthenticatedHost:
    """Controlled host metadata and HMAC receipts, not model-execution proof."""

    key = b"h" * 32

    def __init__(self, profile, selected):
        self.profile = profile
        self.configuration = {"execution_profile_sha256": profile.sha256, "config_ref": "profile-a",
            "controller_ref": "controller-a", "model_ref": "reference-model", "prompt_version": "prompt/v1",
            "model_settings_sha256": owner.digest(selected["model_settings"]), "rubric_sha256": owner.digest(selected["rubric"]),
            "domains": ["domain"], "suite_sha256": selected["suite_sha256"], "split_ref": selected["split_ref"], "execution_kind": "model", "reference_assessment_ref": "host-assessment"}

    def calibration_configuration(self, config_ref):
        assert config_ref == "profile-a"
        return copy.deepcopy(self.configuration)

    def receipt(self, attempt, case):
        output = {"case_id": case["case_id"], "label": case["expected_label"]}
        result = {"schema_version": "research-execution-observation/v1", "run_id": attempt.run_id,
            "binding_id": attempt.binding_id, "profile_sha256": self.profile.sha256,
            "attempt_id": attempt.attempt_id, "nonce": attempt.nonce, "job_id": attempt.job["id"],
            "job_sha256": attempt.job_sha256, "input_sha256": attempt.input_sha256,
            "controller_ref": "controller-a", "config_ref": "profile-a", "model_ref": "reference-model",
            "prompt_version": "prompt/v1", "sequence": 1, "issued_utc": attempt.job["issued_utc"],
            "deadline_utc": attempt.job["deadline_utc"], "started_utc": "2026-10-01T11:59:55Z",
            "completed_utc": "2026-10-01T11:59:56Z", "observed_utc": "2026-10-01T11:59:56Z",
            "output": output, "output_sha256": _digest(output), "currency": "EUR", "pricing_ref": "host-price",
            "state": "succeeded", "reason": "completed", "observed_usage": {"seconds": 1, "model_calls": 1,
                "tool_calls": 0, "tokens": 1, "cost": "0"}, "final_accounting": True,
            "cancellation_confirmed": False}
        result["authentication"] = hmac.new(self.key, canonical(result), hashlib.sha256).hexdigest()
        return result

    def authenticate(self, supplied, attempt):
        result = copy.deepcopy(supplied)
        signature = result.pop("authentication")
        if not hmac.compare_digest(signature, hmac.new(self.key, canonical(result), hashlib.sha256).hexdigest()):
            raise QualificationError("host_authentication_refused")
        return copy.deepcopy(supplied)


@pytest.fixture(scope="module")
def issued():
    policy = RunPolicy(policy_document())
    profile = ExecutionProfile("reference-host", 1000, "EUR", "host-price",
        (ExecutionRole("profile-a", "evaluator", "controller-a", "reference-model", "prompt/v1"),),
        1, 0, 1, "0", 1)
    selected = suite()
    host = AuthenticatedHost(profile, selected)
    observations = []
    for index, case in enumerate(selected["cases"]):
        selected_job = job("evaluate") | {"id": f"job-{index}", "controller_ref": "controller-a",
            "policy_id": policy.policy_id, "scope": policy.to_dict()["scope"]["question_ids"],
            "budgets": profile.document()["job_budgets"], "prompt_version": profile.role("profile-a").prompt_version,
            "issued_utc": "2026-10-01T11:59:40Z", "deadline_utc": "2026-10-01T12:00:10Z"}
        attempt = ExecutionAttempt.create(attempt_id=f"attempt-{index}", nonce=f"nonce-{index}", run_id="calibration",
            binding_id="protected-binding", profile=profile, config_ref="profile-a", job=selected_job,
            input_document=case["input_document"])
        observations.append((attempt, host.receipt(attempt, case)))
    authority = owner.CalibrationAuthority("issuer", b"q" * 32, "generator")
    record = authority.issue(policy, profile, "profile-a", selected, observations, host,
        expires_utc=EXPIRY, observed_utc=NOW, verification_seconds=30)
    return policy, profile, selected, host, observations, authority, record


def test_full_authenticated_scoring_freezes_exact_old_thresholds(issued):
    policy, _, _, _, _, authority, record = issued
    assert record["state"] == "qualified"
    assert record["limits"] == policy.to_dict()["thresholds"]
    assert record["domains"]["domain"]["independent_reference_units"] == 100
    assert len(record["observation_refs"]) == 600
    assert authority.verify(record) == record
    assert not record["authorized"] and not record["evidence_accepted"]
    changed = copy.deepcopy(record)
    changed["domains"]["domain"]["state"] = "qualified"
    changed["state"] = "unknown"
    with pytest.raises(QualificationError, match="authentication_refused"):
        authority.verify(changed)


def test_immutable_profile_budget_is_read_once_while_every_reference_is_authenticated(issued,monkeypatch):
    policy,profile,selected,_host,observations,authority,_record = issued
    host = AuthenticatedHost(profile,selected)
    authenticate = host.authenticate
    document = ExecutionProfile.document
    reads,calls = [],[]
    def profile_document(value):
        if value is profile:reads.append(value)
        return document(value)
    def authentication(receipt,attempt):
        calls.append(attempt.attempt_id)
        return authenticate(receipt,attempt)
    monkeypatch.setattr(ExecutionProfile,'document',profile_document)
    host.authenticate = authentication
    record = authority.issue(policy,profile,'profile-a',selected,observations,host,
        expires_utc=EXPIRY,observed_utc=NOW,verification_seconds=30)
    assert record['state'] == 'qualified'
    assert len(reads) == 1
    assert len(calls) == len(set(calls)) == len(observations) == len(record['observation_refs']) == 600
    assert {ref['attempt_id'] for ref in record['observation_refs']} == set(calls)
    assert record['domains']['domain']['independent_reference_units'] == 100


def test_hoisted_profile_budget_still_checks_the_last_authenticated_reference(issued):
    from dataclasses import replace

    from evidence_wiki.agent_execution import _json
    policy,profile,selected,_host,observations,authority,_record = issued
    host = AuthenticatedHost(profile,selected)
    attempt = observations[-1][0]
    job = attempt.job
    job['budgets']['model_calls'] = profile.model_calls+1
    changed = replace(attempt,_job_bytes=_json(job))
    current = [*observations[:-1],(changed,host.receipt(changed,selected['cases'][-1]))]
    assert changed.job_sha256 != attempt.job_sha256
    with pytest.raises(QualificationError,match='job_budget_exceeded'):
        authority.issue(policy,profile,'profile-a',selected,current,host,
            expires_utc=EXPIRY,observed_utc=NOW,verification_seconds=30)


@pytest.mark.parametrize("change", ["domain", "rubric", "settings", "drift", "expired", "revoked"])
def test_current_qualification_cannot_outlive_its_exact_binding(issued, change):
    policy, profile, _, _, _, authority, record = issued
    kwargs = {"policy": policy, "profile": profile, "config_ref": "profile-a", "rubric": RUBRIC,
        "model_settings": SETTINGS, "domain_ref": "domain", "now_utc": NOW}
    assert authority.current(record, **kwargs)["state"] == "qualified"
    if change == "domain":
        kwargs["domain_ref"] = "elsewhere"
    elif change == "rubric":
        kwargs["rubric"] = RUBRIC | {"revision": "2"}
    elif change == "settings":
        kwargs["model_settings"] = SETTINGS | {"seed": 2}
    elif change == "drift":
        kwargs["drift_ref"] = "drifted"
    elif change == "expired":
        kwargs["now_utc"] = EXPIRY
    else:
        kwargs["revoked"] = True
    assert authority.current(record, **kwargs)["state"] == "unknown"


def test_legacy_host_without_configuration_service_is_unknown(issued):
    policy, profile, selected, host, _, authority, _ = issued
    from types import SimpleNamespace
    legacy = SimpleNamespace(profile=profile, authenticate=host.authenticate)
    result = authority.issue(policy, profile, "profile-a", selected, [], legacy,
        observed_utc=NOW, expires_utc=EXPIRY)
    assert result["state"] == "unknown"
    assert "configuration_unobserved" in result["reasons"]


def test_repeating_a_variant_cannot_dilute_sensitivity():
    cases = references()
    clone = copy.deepcopy(cases[1])
    clone["case_id"] = "renamed-variant"
    with pytest.raises(QualificationError, match="variant_unit_duplicate"):
        suite(cases + [clone])


def test_changed_host_heldout_split_cannot_qualify(issued):
    policy, profile, selected, _, observations, authority, _ = issued
    host = AuthenticatedHost(profile, selected)
    host.configuration["split_ref"] = "training"
    result = authority.issue(policy, profile, "profile-a", selected, observations, host,
        observed_utc=NOW, expires_utc=EXPIRY, verification_seconds=30)
    assert result["state"] == "unknown"
    assert "configuration_or_domain_unqualified" in result["reasons"]


def test_incomplete_suite_shape_has_typed_refusal(issued):
    policy, profile, _, host, _, authority, _ = issued
    with pytest.raises(QualificationError, match="suite_invalid"):
        authority.issue(policy, profile, "profile-a", {"schema_version": owner.SUITE}, [], host,
            observed_utc=NOW, expires_utc=EXPIRY)


def test_host_configuration_drift_during_verification_is_unknown(issued):
    policy, profile, selected, _, observations, authority, _ = issued
    host = AuthenticatedHost(profile, selected)
    count = 0

    def configuration(config_ref):
        nonlocal count
        count += 1
        value = copy.deepcopy(host.configuration)
        if count > 1:
            value["model_settings_sha256"] = "a" * 64
        return value

    host.calibration_configuration = configuration
    result = authority.issue(policy, profile, "profile-a", selected, observations, host,
        observed_utc=NOW, expires_utc=EXPIRY, verification_seconds=30)
    assert result["state"] == "unknown"
    assert "configuration_changed" in result["reasons"]


def test_host_authentication_failure_does_not_create_measured_qualification(issued):
    policy, profile, selected, _, observations, authority, _ = issued
    host = AuthenticatedHost(profile, selected)
    changed = copy.deepcopy(observations[0][1])
    changed["output"]["label"] = "reject"
    result = authority.issue(policy, profile, "profile-a", selected, [(observations[0][0], changed)], host,
        observed_utc=NOW, expires_utc=EXPIRY)
    assert result["state"] == "unknown"
    assert "observation_unverified" in result["reasons"]
    assert result["observation_refs"] == []


def test_verification_wait_expiry_remains_unknown(issued, monkeypatch):
    policy, profile, selected, host, observations, authority, _ = issued
    monkeypatch.setattr(owner, "bounded_host_call", lambda *_args: (None, "host_wait_expired"))
    result = authority.issue(policy, profile, "profile-a", selected, observations[:1], host,
        observed_utc=NOW, expires_utc=EXPIRY)
    assert result["state"] == "unknown"
    assert "configuration_unobserved" in result["reasons"]
    assert "observation_unverified" in result["reasons"]
    assert result["domains"]["domain"]["metrics"]["positive_acceptance"] is None
