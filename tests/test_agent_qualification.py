"""Protected labels, authenticated execution identity and canonical review signing."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import sys
import time
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

import pytest

from evidence_wiki.agent_execution import (
    BoundedSubprocessExecutor,
    ExecutionAttempt,
    ExecutionError,
    ExecutionProfile,
    ExecutionRole,
)
from evidence_wiki.agent_qualification import (
    HostReviewSigner,
    QualificationAuthority,
    QualificationError,
    QualifiedEvaluators,
    SigningIdentity,
    reference_suite,
)
from evidence_wiki.run_policy import RunPolicy
from tests._execution_fixture import KEYS, canonical
from tests.test_agent_execution import LocalMeter
from tests.test_run_policy import policy_document
from tests.test_strict_evidence import CONTRACT, CORE, USAGE
from tests.test_strict_evidence import host as strict_host_fixture
from tests.test_workflow_contracts import job


def digest(value):
    return hashlib.sha256(canonical(value).rstrip(b"\n")).hexdigest()


@pytest.fixture(scope="module")
def calibrated(tmp_path_factory):
    if os.name != "posix":
        pytest.skip("Reference qualification execution requires POSIX process groups.")
    policy = RunPolicy(policy_document())
    profile = ExecutionProfile("reference-host", 1024, "EUR", "local-process/v1",
        (ExecutionRole("profile-a", "evaluator", "controller-a", "reference-process", "prompt/v1"),),
        0, 5, 0, "0", 0.2)
    cases = []
    for index in range(100):
        pair = index // 2
        supported = pair % 2 == 0
        cases.append({"case_id": f"case-{index}", "family_id": f"family-{pair % 10}",
            "input_document": {"case_id": f"case-{index}", "evidence_supports_claim": supported, "wording": index % 2},
            "expected_label": "accept" if supported else "reject", "critical": not supported,
            "pair_id": f"pair-{pair}"})
    rubric = {"id": "reference-rubric", "revision": "1", "criteria": {"support": "Require evidence support."}}
    suite = reference_suite(cases, suite_ref="protected-reference", split_ref="held-out", domain_ref="reference-domain", rubric=rubric)
    script = "import json,sys; d=json.load(sys.stdin)['input']; print(json.dumps({'case_id': d['case_id'], 'label': 'accept' if d['evidence_supports_claim'] else 'reject'}))"
    executor = BoundedSubprocessExecutor(profile, commands={"profile-a": (sys.executable, "-c", script)},
        cwd=tmp_path_factory.mktemp("qualified-execution"), receipt_key=b"e" * 32, meter=LocalMeter())
    observations = []
    for index, case in enumerate(cases):
        now = datetime.now(timezone.utc)
        selected = job("evaluate")
        selected.update(id=f"reference-job-{index}", controller_ref="controller-a", policy_id=policy.policy_id,
            scope=policy.to_dict()["scope"]["question_ids"], issued_utc=(now - timedelta(seconds=1)).isoformat(),
            deadline_utc=(now + timedelta(seconds=10)).isoformat(), timeout_seconds=5)
        attempt = ExecutionAttempt.create(attempt_id=f"attempt-{index}", nonce=f"nonce-{index}", run_id="references",
            binding_id="protected-binding", profile=profile, config_ref="profile-a", job=selected,
            input_document=case["input_document"])
        observed = executor.invoke(attempt)
        until = time.monotonic() + 5
        while observed["state"] == "running" and time.monotonic() < until:
            time.sleep(0.005)
            observed = executor.poll(attempt)
        assert observed["state"] == "succeeded"
        observations.append((attempt, observed))
    authority = QualificationAuthority("protected-issuer", b"q" * 32, "generator-controller")
    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    record = authority.issue(policy, profile, "profile-a", suite, cases, observations, executor, expires_utc=expires)
    return policy, profile, cases, suite, observations, executor, authority, record


def rows(calibrated, *, record=None, policy=None, profile=None, rubric=None, now=None):
    original, execution, _, suite, _, _, authority, issued = calibrated
    return QualifiedEvaluators([record or issued], authority).observations(policy or original, profile or execution,
        now or datetime.now(timezone.utc).isoformat(), (rubric or suite["rubric"])["id"],
        rubric_sha256=digest(rubric or suite["rubric"]))


def test_actual_reference_execution_computes_qualified_metrics(calibrated):
    policy, _, cases, _, observations, _, _, record = calibrated
    assert len(observations) == len(cases) == 100
    assert record["counts"]["reference_cases"] == 100 and record["counts"]["families"] == 10
    assert record["counts"]["critical_false_acceptances"] == 0
    assert record["metrics"] == {"positive_acceptance": "1", "meaning_preserving_flip": "0"}
    result = rows(calibrated)[0]
    assert result["qualified"] and result["independent"]
    assert result["profile_ref"] == policy.to_dict()["required_evaluators"][0]["profile_ref"]
    assert "expected_label" not in json.dumps([attempt.input_document for attempt, _ in observations])


def test_registry_is_immutable_and_does_not_retain_mutable_inputs(calibrated):
    policy, profile, _, suite, _, _, authority, record = calibrated
    detached = copy.deepcopy(record)
    registry = QualifiedEvaluators([detached], authority)
    original = registry.sha256
    detached["counts"]["reference_cases"] = 0
    assert registry.sha256 == original
    assert registry.observations(policy, profile, datetime.now(timezone.utc).isoformat(), suite["rubric"]["id"],
        rubric_sha256=digest(suite["rubric"]))[0]["qualified"]
    with pytest.raises(FrozenInstanceError):
        registry.authority = None


def test_metrics_cannot_be_asserted_or_changed_by_worker(calibrated):
    *_, authority, record = calibrated
    tampered = copy.deepcopy(record)
    tampered["metrics"]["positive_acceptance"] = "0.99"
    with pytest.raises(QualificationError, match="authentication"):
        QualifiedEvaluators([tampered], authority)


def test_reference_input_mismatch_cannot_be_qualified(calibrated):
    policy, profile, cases, suite, observations, executor, authority, record = calibrated
    cases = copy.deepcopy(cases)
    cases[0]["input_document"]["wording"] = "different-reference"
    changed_suite = reference_suite(cases, **{key: suite[key] for key in ("suite_ref", "split_ref", "domain_ref", "rubric")})
    with pytest.raises(QualificationError, match="input_mismatch"):
        authority.issue(policy, profile, "profile-a", changed_suite, cases, observations, executor,
            expires_utc=record["expires_utc"])


def test_unauthenticated_judgment_cannot_be_used(calibrated):
    policy, profile, cases, suite, observations, executor, authority, record = calibrated
    altered = copy.deepcopy(observations[0][1])
    altered["output"]["label"] = "reject"
    with pytest.raises(ExecutionError):
        authority.issue(policy, profile, "profile-a", suite, cases, [(observations[0][0], altered), *observations[1:]],
            executor, expires_utc=record["expires_utc"])


def test_missing_reference_coverage_remains_unknown(calibrated):
    policy, profile, cases, suite, observations, executor, authority, issued = calibrated
    incomplete = authority.issue(policy, profile, "profile-a", suite, cases, observations[:-1], executor,
        expires_utc=issued["expires_utc"])
    assert incomplete["counts"]["reference_cases"] == 99 and not incomplete["complete"]
    incomplete_row = rows(calibrated, record=incomplete)[0]
    assert incomplete_row["qualified"] is None
    assert incomplete_row["critical_false_acceptances"] is None
    assert incomplete_row["positive_acceptance"] is None
    assert incomplete_row["meaning_preserving_flip"] is None


def test_critical_false_acceptance_disqualifies_observed_judgments(calibrated):
    policy, profile, cases, suite, observations, executor, authority, issued = calibrated
    cases = copy.deepcopy(cases)
    # A protected rejecting reference exposes the accepting worker judgment.
    for case in cases[:2]:
        case.update(expected_label="reject", critical=True)
    changed_suite = reference_suite(cases, **{key: suite[key] for key in ("suite_ref", "split_ref", "domain_ref", "rubric")})
    failed = authority.issue(policy, profile, "profile-a", changed_suite, cases, observations, executor,
        expires_utc=issued["expires_utc"])
    assert failed["counts"]["critical_false_acceptances"] == 2
    assert rows(calibrated, record=failed)[0]["qualified"] is False


@pytest.mark.parametrize("precision", [1, 6, 28])
def test_qualification_ratios_ignore_ambient_decimal_precision(precision):
    from decimal import ROUND_CEILING, localcontext

    from evidence_wiki.agent_qualification import _ratio

    with localcontext() as context:
        context.prec = precision
        context.rounding = ROUND_CEILING
        assert _ratio(89, 99) == "0.898989"
        assert _ratio(89, 99, upper=True) == "0.89899"
        assert _ratio(0, 99) == "0"
        assert _ratio(99, 99) == "1"
        assert _ratio(0, 0) is None


def test_positive_acceptance_is_computed_from_full_protected_denominator(calibrated):
    policy, profile, cases, suite, observations, executor, authority, issued = calibrated
    cases = copy.deepcopy(cases)
    for start in (2, 6, 10, 14):
        for case in cases[start:start + 2]:
            case.update(expected_label="accept", critical=False)
    changed_suite = reference_suite(cases, **{key: suite[key] for key in ("suite_ref", "split_ref", "domain_ref", "rubric")})
    failed = authority.issue(policy, profile, "profile-a", changed_suite, cases, observations, executor,
        expires_utc=issued["expires_utc"])
    assert failed["counts"]["positive_cases"] == 58
    assert failed["counts"]["positive_acceptances"] == 50
    assert failed["metrics"]["positive_acceptance"] == "0.862068"
    assert rows(calibrated, record=failed)[0]["qualified"] is False


def test_incomplete_meaning_preserving_pair_is_rejected(calibrated):
    _, _, cases, suite, *_ = calibrated
    with pytest.raises(QualificationError, match="pair_invalid"):
        reference_suite(cases[:-1], **{key: suite[key] for key in ("suite_ref", "split_ref", "domain_ref", "rubric")})


def test_reference_labels_cannot_be_included_in_worker_inputs(calibrated):
    _, _, cases, suite, *_ = calibrated
    cases = copy.deepcopy(cases)
    cases[0]["input_document"]["metadata"] = {"expected_label": "accept"}
    with pytest.raises(QualificationError, match="label_exposed"):
        reference_suite(cases, **{key: suite[key] for key in ("suite_ref", "split_ref", "domain_ref", "rubric")})


def test_generator_controller_cannot_qualify_its_own_evaluator(calibrated):
    policy, profile, cases, suite, observations, executor, _, issued = calibrated
    authority = QualificationAuthority("protected-issuer", b"q" * 32, "controller-a")
    failed = authority.issue(policy, profile, "profile-a", suite, cases, observations, executor,
        expires_utc=issued["expires_utc"])
    result = QualifiedEvaluators([failed], authority).observations(policy, profile,
        datetime.now(timezone.utc).isoformat(), suite["rubric"]["id"], rubric_sha256=digest(suite["rubric"]))[0]
    assert result["independent"] is False and result["qualified"] is False


def test_lost_or_duplicate_cases_are_not_removed_to_match_observations(calibrated):
    policy, profile, cases, suite, observations, executor, authority, issued = calibrated
    with pytest.raises(QualificationError, match="attempt_mismatch"):
        authority.issue(policy, profile, "profile-a", suite, cases, [observations[0], *observations[:-1]], executor,
            expires_utc=issued["expires_utc"])
    changed = copy.deepcopy(suite)
    changed["suite_sha256"] = "0" * 64
    with pytest.raises(QualificationError, match="digest_mismatch"):
        authority.issue(policy, profile, "profile-a", changed, cases, observations, executor,
            expires_utc=issued["expires_utc"])


def test_changed_model_rubric_policy_or_scope_is_refused(calibrated):
    policy, profile, _, suite, *_ = calibrated
    with pytest.raises(QualificationError, match="binding_mismatch"):
        rows(calibrated, profile=replace(profile, roles=(replace(profile.roles[0], model_ref="other-model"),)))
    with pytest.raises(QualificationError, match="rubric_changed"):
        rows(calibrated, rubric={**suite["rubric"], "revision": "2"})
    changed = policy.to_dict()
    changed["policy_id"] = None
    changed["scope"]["source_scope"] = ["other-source"]
    with pytest.raises(QualificationError, match="binding_mismatch"):
        rows(calibrated, policy=RunPolicy(changed))


def test_expired_and_unbound_rubric_cannot_claim_qualification(calibrated):
    policy, profile, _, suite, _, _, authority, issued = calibrated
    with pytest.raises(QualificationError, match="expired"):
        rows(calibrated, now=issued["expires_utc"])
    result = QualifiedEvaluators([issued], authority).observations(policy, profile,
        datetime.now(timezone.utc).isoformat(), suite["rubric"]["id"])
    assert result[0]["qualified"] is None and result[0]["qualification_ref"] is None


def test_distinct_labels_cannot_claim_host_enforced_isolation(calibrated):
    policy, profile, cases, suite, observations, executor, authority, issued = calibrated
    document = policy.to_dict()
    document.update(policy_id=None, assurance="host_enforced")
    changed = RunPolicy(document)
    # Different policy identity requires authentic reference attempts for that policy.
    with pytest.raises(QualificationError, match="attempt_mismatch"):
        authority.issue(changed, profile, "profile-a", suite, cases, observations, executor,
            expires_utc=issued["expires_utc"])
    assert issued["assurance"] == "artifact_checked"


def identity(principal, role, *, config_ref=None, controller=None, key=None):
    return SigningIdentity(config_ref or principal, principal, principal + "-key", controller or principal,
                           role, key or bytes.fromhex(KEYS[principal]))


def signer():
    return HostReviewSigner("lab-authority", "1", identity("runner", "generator"),
        (identity("evaluator", "evaluator"),), "2027-01-01T00:00:00Z", "2026-01-01T00:00:00Z")


@pytest.fixture
def strict_host(tmp_path, monkeypatch, request):
    return strict_host_fixture.__wrapped__(tmp_path, monkeypatch, request)


def test_parent_signer_authenticates_exact_prepared_strict_registration(strict_host):
    prepared = CORE.prepare_review(strict_host.root, "product-source")
    now = datetime.now(timezone.utc).isoformat()
    envelope = signer().registration(prepared, dict.fromkeys(CONTRACT.CHECKS, "pass"),
        "The exact retained product page supports its attributed statement.", "autonomous-review", now)
    assert prepared["registration"]["request_id"] is None
    receipt = USAGE.transact(strict_host.root, strict_host.config, envelope)
    assert receipt["checkpoint"]
    assert CORE.publication(strict_host.root)["questions"][0]["claims"]
    assert bytes.fromhex(KEYS["evaluator"]).hex() not in json.dumps(envelope)


def test_signer_signature_and_nested_authorship_cover_entire_payload():
    host = signer()
    envelope = host.authorship("sha256:" + "a" * 64, "claim", "2026-10-01T00:00:00Z")
    signature = envelope["authentication"].pop("signature")
    expected = hmac.new(bytes.fromhex(KEYS["runner"]), b"evidence-attestation/v1\0" + canonical(envelope), hashlib.sha256).hexdigest()
    assert signature == expected
    assert bytes.fromhex(KEYS["runner"]).hex() not in repr(host)


@pytest.mark.parametrize("controller,key", [("runner", None), ("different", bytes.fromhex(KEYS["runner"]))])
def test_parent_signer_refuses_shared_controller_or_credential(controller, key):
    with pytest.raises(QualificationError, match="not_independent"):
        HostReviewSigner("lab-authority", "1", identity("runner", "generator"),
            (identity("evaluator", "evaluator", controller=controller, key=key),), "2027-01-01T00:00:00Z")


def test_parent_signer_cannot_route_to_human_or_expired_authority(strict_host):
    prepared = CORE.prepare_review(strict_host.root, "product-source")
    prepared["registration"]["action"] = "register-strict-human-review"
    with pytest.raises(QualificationError, match="human_role"):
        signer().registration(prepared, dict.fromkeys(CONTRACT.CHECKS, "pass"), "Reference judgment.", "review-request",
                              "2026-10-01T00:00:00Z")
    with pytest.raises(QualificationError, match="expired"):
        signer().authorship("sha256:" + "a" * 64, "claim", "2027-01-01T00:00:00Z")


def test_malformed_prepared_review_produces_typed_refusal():
    with pytest.raises(QualificationError, match="prepared_invalid"):
        signer().registration({}, dict.fromkeys(CONTRACT.CHECKS, "pass"), "Reference judgment.", "review-request",
                              "2026-10-01T00:00:00Z")
