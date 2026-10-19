"""Resolution reuses only owned originals authenticated by current native gates."""

from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from evidence_wiki import counter_authority
from evidence_wiki import resolution_evidence as evidence
from evidence_wiki._review_dependency_json import digest
from tests.test_dependency_consumers import dependency_inputs

CURRENT = "sha256:" + "1" * 64
ORIGINAL = "sha256:" + "2" * 64
DEPENDENCY = "sha256:" + "3" * 64
REVIEW = "sha256:" + "4" * 64


@pytest.fixture
def retained_case(tmp_path, monkeypatch):
    publication = dependency_inputs()["current"]["canonical_release"]["publication"]
    question = publication["questions"][0]
    claim = question["claims"][0]
    claim.update(claim_basis_id=DEPENDENCY, observed_workspace_basis_id=ORIGINAL)
    native_claim = {key: value for key, value in claim.items() if key not in {
        "claim_basis_id", "observed_workspace_basis_id", "review_basis_mode", "verification"}}
    document = {"attempt_id": "attempt", "run_id": "run", "binding_id": CURRENT}
    attempt = SimpleNamespace(attempt_id="attempt", config_ref="evaluator", document=lambda: deepcopy(document),
        input_document={"packet": {"original": True}})
    role = SimpleNamespace(role="evaluator", controller_ref="evaluator-controller")
    profile = SimpleNamespace(role=lambda _: role, disagreement=None, review_services=None,
        role_scopes=SimpleNamespace(scope=lambda _: SimpleNamespace(task_role="evaluator")))
    session = SimpleNamespace(root=tmp_path, execution=SimpleNamespace(profile=profile,
        review_signer=SimpleNamespace(generator=SimpleNamespace(controller_ref="generator-controller"))))
    payload = {"schema_version": "evidence-strict-review/v3", "claim_id": claim["id"],
        "basis_id": DEPENDENCY, "observed_workspace_basis_id": ORIGINAL,
        "claim_schema_version": publication["claim_schema_version"],
        "reviewed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "verdicts": {"support": "pass"}, "dependency_basis": {"semantic_material_id": CURRENT},
        "delivery": {"attempt_id": attempt.attempt_id, "attempt_content_sha256": digest(document),
            "binding_sha256": CURRENT}}
    envelope = {"payload": {"body": {"review": payload}}}
    application = {"attempt_id": attempt.attempt_id, "claim_id": claim["id"], "original_id": question["original_id"],
        "profile_ref": attempt.config_ref, "strict_basis_id": DEPENDENCY, "envelope": envelope, "passed": True}
    review = {key: value for key, value in application.items() if key != "envelope"}
    prepared = {"claim_basis_id": DEPENDENCY, "observed_workspace_basis_id": CURRENT,
        "snapshot": {"claim": native_claim}, "result": {"basis_id": CURRENT}}
    observed = {"state": "eligible", "original_authenticated": True,
        "original_controller_ref": role.controller_ref, "original_generator_controller_ref": "generator-controller",
        "current": {"basis_id": DEPENDENCY, "workspace_basis_id": CURRENT,
            "review_producer_id": publication["review_producer_id"]},
        "custody": {"review_id": REVIEW, "basis_id": DEPENDENCY, "semantic_material_id": CURRENT,
            "delivery_binding_id": CURRENT, "original_verified": True},
        "strict": {"claims": [{"claim": deepcopy(native_claim), "accepted": True,
            "claim_basis_id": DEPENDENCY, "observed_workspace_basis_id": ORIGINAL}]}}
    calls = []

    def retained(root, supplied, *, claim_id, expected_review_id):
        assert root == session.root and supplied is envelope
        assert claim_id == claim["id"] and expected_review_id == REVIEW
        calls.append("native_reuse")
        return deepcopy(observed)

    def fresh(*args, **kwargs):
        pytest.fail("A retained resolution original reached fresh admission.")

    strict = SimpleNamespace(retained_review_eligibility=retained, validate_review=fresh)

    def owner(name):
        if name == "_strict_evidence":
            return strict
        if name == "_strict_contract":
            return SimpleNamespace(artifact_id=lambda supplied: REVIEW if supplied is payload else pytest.fail("Payload changed."))
        if name == "_evidence_authority":
            return SimpleNamespace(timestamp=lambda value: datetime.fromisoformat(value.replace("Z", "+00:00")))
        pytest.fail("Unexpected native owner: " + name)

    monkeypatch.setattr(evidence, "owner", owner)
    return SimpleNamespace(session=session, publication=publication, question=question, claim=claim, attempt=attempt,
        application=application, review=review, prepared=prepared, observed=observed, payload=payload, calls=calls)


def eligible(case):
    return evidence._retained_pass(case.session, case.publication, case.claim, case.question,
        case.attempt, case.application, case.review, case.prepared)


def test_resolution_asks_actual_native_owner_about_unchanged_dependency_with_historical_full_basis(retained_case):
    before = deepcopy(retained_case.payload)
    assert eligible(retained_case)
    assert retained_case.calls == ["native_reuse"]
    assert retained_case.payload == before
    assert before["observed_workspace_basis_id"] == ORIGINAL != CURRENT


@pytest.mark.parametrize("change", ["legacy_review", "basis", "attempt", "attempt_content", "original",
    "applied_original", "profile", "claim_schema", "prepared_basis", "prepared_workspace"])
def test_completed_original_joins_precede_native_reuse(retained_case, change):
    case = retained_case
    if change == "legacy_review":
        case.payload["schema_version"] = "evidence-strict-review/v2"
    elif change == "basis":
        case.application["strict_basis_id"] = CURRENT
    elif change == "attempt":
        case.payload["delivery"]["attempt_id"] = "other-attempt"
    elif change == "attempt_content":
        case.payload["delivery"]["attempt_content_sha256"] = ORIGINAL
    elif change == "original":
        case.application["original_id"] = "other-original"
    elif change == "applied_original":
        case.review["original_id"] = "other-original"
    elif change == "profile":
        case.application["profile_ref"] = "other-profile"
    elif change == "claim_schema":
        case.payload["claim_schema_version"] = "evidence-strict-claims/v1"
    elif change == "prepared_basis":
        case.prepared["claim_basis_id"] = CURRENT
    else:
        case.prepared["observed_workspace_basis_id"] = ORIGINAL
    assert not eligible(case)
    assert not case.calls


@pytest.mark.parametrize("change", ["negative", "unknown", "unauthenticated", "controller", "generator",
    "basis", "workspace", "producer", "review", "material", "delivery", "custody", "claim",
    "acceptance", "original_workspace"])
def test_current_native_failure_cannot_be_replaced_by_an_original_pass(retained_case, change):
    value = retained_case.observed
    if change in {"negative", "unknown"}:
        value["state"] = "ineligible" if change == "negative" else "unknown"
    elif change == "unauthenticated":
        value["original_authenticated"] = False
    elif change in {"controller", "generator"}:
        key = "original_controller_ref" if change == "controller" else "original_generator_controller_ref"
        value[key] = "other-controller"
    elif change in {"basis", "workspace", "producer"}:
        key = {"basis": "basis_id", "workspace": "workspace_basis_id", "producer": "review_producer_id"}[change]
        value["current"][key] = ORIGINAL
    elif change in {"review", "material", "delivery", "custody"}:
        key = {"review": "review_id", "material": "semantic_material_id", "delivery": "delivery_binding_id",
            "custody": "original_verified"}[change]
        value["custody"][key] = False if change == "custody" else ORIGINAL
    elif change == "claim":
        value["strict"]["claims"][0]["claim"]["text"] = "Changed current evidence."
    elif change == "acceptance":
        value["strict"]["claims"][0]["accepted"] = False
    else:
        value["strict"]["claims"][0]["observed_workspace_basis_id"] = CURRENT
    assert not eligible(retained_case)
    assert retained_case.calls == ["native_reuse"]


def test_question_resolution_joins_claim_basis_and_uses_native_original_context_custody(retained_case, monkeypatch):
    case = retained_case
    wanted = {"profile_ref": "evaluator", "role": "evaluator", "controller_ref": "evaluator-controller"}
    case.session.policy = SimpleNamespace(to_dict=lambda: {"required_evaluators": [wanted]})
    case.session.execution.attempt = lambda _: case.attempt
    row = {"original_id": case.question["original_id"], "claim_id": case.claim["id"], "purpose": "evaluate",
        "attempt": {"attempt_id": case.attempt.attempt_id}, "review_context": {"original": True},
        "review_assembly_request_id": "original-assembly"}
    records = [{"kind": "execution_application", "data": {"operation": "evaluate", **case.application}},
        {"kind": "execution_applied", "data": {"operation": "evaluate", **case.review}}]
    observation = {"state": "succeeded", "final_accounting": True, "observed_usage": {"model_calls": 1},
        "started_utc": "2026-10-16T00:00:00Z", "reason": "observed", "output": {"verdicts": case.payload["verdicts"]}}
    current = {"canonical_release": {"publication": {**case.publication, "questions": [case.question]}},
        "qualification": {"profiles": [{"profile_ref": "evaluator", "qualified": True, "independent": True,
            "controller_ref": "evaluator-controller"}]}}
    facts = SimpleNamespace(config={}, policy={"max_review_age_seconds": 3600}, prepared=lambda _: case.prepared)
    monkeypatch.setattr(evidence.native, "_attempt_rows", lambda _: {case.attempt.attempt_id: row})
    monkeypatch.setattr(evidence.native, "_latest_observation", lambda *_: observation)
    monkeypatch.setattr(evidence.native, "_model_review_observed", lambda *_: True)
    monkeypatch.setattr(evidence.native, "_verify_review_controller", lambda *_: case.calls.append("controller"))
    monkeypatch.setattr(evidence.native, "_counter_stop", lambda *args, **kwargs: None)
    monkeypatch.setattr(evidence, "correlated", lambda *args: observation)
    monkeypatch.setattr(counter_authority, "require_current_producer", lambda *_: case.calls.append("producer"))
    result = evidence.questions(case.session, current, records, usage_view=object(), facts=facts)
    assert result == {case.question["slug"]: {"eligible": True, "claim_refs": [case.claim["id"]],
        "rejected": False, "reasons": []}}
    assert case.calls == ["producer", "controller", "native_reuse"]
    case.calls.clear()
    records[-1]["data"]["strict_basis_id"] = CURRENT
    refused = evidence.questions(case.session, current, records, usage_view=object(), facts=facts)
    assert refused[case.question["slug"]]["eligible"] is False
    assert not case.calls
