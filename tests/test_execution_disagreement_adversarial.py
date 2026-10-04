"""Independent intrinsic-graph and authenticated continuation-join attacks."""

import copy

import pytest

from evidence_wiki.agent_execution import DeterministicOfflineExecutor, ExecutionAttempt, ExecutionError
from evidence_wiki.execution_disagreement import (
    attempt_metadata,
    material_basis,
    project_claim,
    validate_targeted_repair,
)
from evidence_wiki.mutation_contracts import digest
from tests.test_agent_execution import attempt, profile
from tests.test_execution_disagreement import journal as journal_fixture
from tests.test_review_context import inputs


@pytest.fixture
def journal(tmp_path, monkeypatch):
    return journal_fixture.__wrapped__(tmp_path, monkeypatch)


def graph_context():
    value = inputs()
    snapshot = value["owner_snapshot"]
    prototype = copy.deepcopy(snapshot["claim"])
    snapshot["claim"].update(qualification="inference", premises=["inference-left", "inference-right"],
        derivation="Use the explicitly linked intermediate observations.", evidence=[])
    premises = []
    for identifier, text, children in (("inference-left", "Left intermediate conclusion.", ["fact-left"]),
        ("inference-right", "Right intermediate conclusion.", ["fact-right"]),
        ("fact-left", "First independent declared observation.", []), ("fact-right", "Second independent declared observation.", [])):
        claim = copy.deepcopy(prototype)
        claim.update(id=identifier, text=text, qualification="inference" if children else "supported", premises=children,
            derivation="Use the linked observation." if children else None)
        if children:
            claim["evidence"] = []
        premises.append(claim)
    snapshot["premises"] = premises
    return value


def test_rewired_dependency_graph_is_genuine_material_change_with_same_premise_bag():
    value = graph_context()
    before = material_basis(value["owner_snapshot"], value["rubric"], value["sources"])
    value["owner_snapshot"]["premises"][0]["premises"] = ["fact-right"]
    value["owner_snapshot"]["premises"][1]["premises"] = ["fact-left"]
    assert material_basis(value["owner_snapshot"], value["rubric"], value["sources"]) != before


def test_graph_alias_rename_without_scientific_change_is_not_progress():
    value = graph_context()
    before = material_basis(value["owner_snapshot"], value["rubric"], value["sources"])
    claims = [value["owner_snapshot"]["claim"], *value["owner_snapshot"]["premises"]]
    mapping = {claim["id"]: "renamed-" + str(index) for index, claim in enumerate(claims)}
    for claim in claims:
        claim["id"] = mapping[claim["id"]]
        claim["premises"] = [mapping[identifier] for identifier in claim["premises"]]
    assert material_basis(value["owner_snapshot"], value["rubric"], value["sources"]) == before


@pytest.mark.parametrize("bad", ["missing", "cycle", "duplicate"])
def test_invalid_premise_graph_is_typed_refusal(bad):
    value = graph_context()
    if bad == "missing":
        value["owner_snapshot"]["claim"]["premises"] = ["unavailable"]
    elif bad == "cycle":
        value["owner_snapshot"]["premises"][0]["premises"] = [value["owner_snapshot"]["claim"]["id"]]
    else:
        value["owner_snapshot"]["premises"].append(copy.deepcopy(value["owner_snapshot"]["premises"][0]))
    with pytest.raises(ExecutionError):
        material_basis(value["owner_snapshot"], value["rubric"], value["sources"])


def continuation_case(journal):
    session, append, _, prepared, sources, claim_id = journal
    before = material_basis(prepared["snapshot"], prepared["rubric"], sources)
    action = {"operation": "repair", "claim_id": claim_id, "basis_id": before, "defect_sha256": "sha256:" + "d" * 64}
    reference = attempt(profile()).job | {"controller_ref": "generator"}
    value = ExecutionAttempt.create(attempt_id="repair", nonce="unique-nonce", run_id=session.run_id,
        binding_id=digest(session.binding), profile=session.execution.profile, config_ref="repair", job=reference)
    host = DeterministicOfflineExecutor(session.execution.profile, outputs={"repair": {"proposal": "inert"}}, receipt_key=b"e" * 32)
    receipt = host.authenticate(host.invoke(value), value)
    append("execution_attempt", {"attempt": value.document(), "purpose": "repair", "claim_id": claim_id,
        "target_claim_id": claim_id, "disagreement": attempt_metadata(action, cycle_ref="repair-cycle")})
    return session, append, prepared, sources, claim_id, value, receipt


@pytest.mark.parametrize("field", ["nonce", "job_sha256", "input_sha256", "binding_id", "controller_ref", "profile_sha256", "config_ref", "run_id", "job_id"])
def test_signed_owner_journal_cannot_join_foreign_receipt_to_completed_repair(journal, field):
    session, append, prepared, sources, claim_id, value, receipt = continuation_case(journal)
    changed = copy.deepcopy(receipt)
    changed[field] = "f" * 64 if field.endswith("sha256") else "foreign-reference"
    append("execution_observation", {"attempt_id": value.attempt_id, "observation": changed})
    append("execution_application", {"attempt_id": value.attempt_id, "operation": "generate", "original_id": "original"})
    append("execution_applied", {"attempt_id": value.attempt_id, "operation": "generate", "original_id": "original",
        "disagreement_result_basis": "sha256:" + "f" * 64})
    with pytest.raises(ExecutionError):
        project_claim(session, claim_id=claim_id, prepared=prepared, sources=sources)


def test_real_matching_continuation_receipt_can_observe_result_without_certification(journal):
    session, append, prepared, sources, claim_id, value, receipt = continuation_case(journal)
    append("execution_observation", {"attempt_id": value.attempt_id, "observation": receipt})
    append("execution_application", {"attempt_id": value.attempt_id, "operation": "generate", "original_id": "original"})
    append("execution_applied", {"attempt_id": value.attempt_id, "operation": "generate", "original_id": "original",
        "disagreement_result_basis": "sha256:" + "f" * 64})
    projected = project_claim(session, claim_id=claim_id, prepared=prepared, sources=sources)
    assert projected["history"][0]["result_basis_id"] == "sha256:" + "f" * 64
    assert projected["reservations"][0]["state"] == "succeeded"


@pytest.mark.parametrize("change", ["rename", "remove-peer", "change-peer", "move-question"])
def test_targeted_repair_cannot_reset_claim_lineage_or_expand_scope(change):
    target = inputs()["owner_snapshot"]["claim"]
    peer = copy.deepcopy(target) | {"id": "peer"}
    before = [target, peer]
    after = copy.deepcopy(before)
    after[0]["text"] = "A narrower material assertion."
    if change == "rename":
        after[0]["id"] = "fresh-claim"
    elif change == "remove-peer":
        after.pop()
    elif change == "change-peer":
        after[1]["text"] = "Unrequested peer change."
    else:
        after[0]["question_slug"] = "another-original"
    with pytest.raises(ExecutionError):
        validate_targeted_repair(before, after, target_claim_id=target["id"])


@pytest.mark.parametrize("reason", ["strict_review_authority_changed", "strict_review_authentication_failed", "strict_review_not_independent"])
def test_current_native_authentication_failure_is_not_projected_as_passing_gate(reason):
    from types import SimpleNamespace

    from evidence_wiki.execution_continuations import current_gates
    from tests.test_disagreement import selected_policy

    value = inputs()
    claim = value["owner_snapshot"]["claim"]
    prepared = {"snapshot": value["owner_snapshot"], "rubric": value["rubric"], "result": {
        "basis_id": value["basis_id"], "claims": [{"claim": claim, "accepted": False,
            "observation": {"reasons": []}, "review": {"passed": False, "reason": reason}}]}}
    session = SimpleNamespace(policy=selected_policy(), execution=SimpleNamespace(profile=SimpleNamespace(
        role_scopes=SimpleNamespace(scopes=[SimpleNamespace(task_role="repair")]),
        disagreement=SimpleNamespace(adjudicator_config_ref="adjudicator"))))
    view = {"qualification": {"state": "qualified", "auxiliary_profiles": []},
        "execution": {"accounting": {"usage_unknown": False, "overrun": []}, "steps_used": 0, "steps_limit": 8},
        "authority": {"terminal_grants_available": True}, "usage": {"repair_cycles": 0, "adjudication_cycles": 0, "acquisition_attempts": 0}}
    observed = current_gates(session, view, prepared, value["sources"])
    assert observed["authentication"] != "pass", "Current native authentication failure must not become passing admission data."


@pytest.mark.parametrize("revoked", ["profile-a", "critic"])
def test_current_claim_bridge_rechecks_each_first_pass_even_when_latest_native_review_is_valid(journal, monkeypatch, revoked):
    from types import SimpleNamespace

    from evidence_wiki import execution_continuations as bridge
    from evidence_wiki import execution_session as driver
    from tests.test_execution_disagreement import add_review

    session, _, _, prepared, sources, claim_id = journal
    add_review(journal)
    add_review(journal, "critic", support="fail")
    session.execution._review_signer = SimpleNamespace(generator=SimpleNamespace(controller_ref="generator"))
    prepared["result"]["claims"] = [{"claim": prepared["snapshot"]["claim"], "observation": {"reasons": []},
        "review": {"passed": False, "reason": "strict_review_not_passed"}}]
    monkeypatch.setattr(bridge, "owner", lambda *_args: SimpleNamespace(prepare_review=lambda *_args: prepared))
    checked = []
    def current_verify(_session, actual_attempt, _envelope):
        checked.append(actual_attempt.config_ref)
        if actual_attempt.config_ref == revoked:
            raise ExecutionError("native_current_key_revoked")
    monkeypatch.setattr(driver, "_verify_review_controller", current_verify)
    view = {"qualification": {"state": "qualified", "auxiliary_profiles": []},
        "execution": {"accounting": {"usage_unknown": False, "overrun": []}, "steps_used": 0, "steps_limit": 8},
        "authority": {"terminal_grants_available": True}, "usage": {"repair_cycles": 0, "adjudication_cycles": 0, "acquisition_attempts": 0}}
    result = bridge.claim_action(session, view, claim_id, "original", sources)
    assert checked == ["profile-a", "critic"]
    assert result["operation"] == "terminal"
    assert result["disagreement_decision"]["reason"] == "untrusted_observation"
    assert result["disagreement_decision"]["evidence_accepted"] is False


@pytest.mark.parametrize("mode,expected", [("fresh", "pass"), ("evidence-expired", "unknown"), ("critic-expired", "unknown"),
    ("boundary", "pass"), ("future", "fail"), ("after-signature", "fail"), ("missing-date", "fail"),
    ("missing-limit", "unknown"), ("boolean-limit", "unknown")])
def test_each_retained_first_pass_has_its_own_native_review_age(mode, expected, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    from evidence_wiki import execution_continuations as bridge
    from evidence_wiki import execution_session as driver

    now = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)
    records, refs, checked = [], [], []
    for config in ("evidence", "critic"):
        age = 61 if mode == config + "-expired" else 60 if mode == "boundary" and config == "evidence" else 1
        reviewed = now - timedelta(seconds=age)
        issued = now - timedelta(seconds=1)
        if mode == "future" and config == "evidence":
            reviewed = now + timedelta(seconds=1)
        if mode == "after-signature" and config == "evidence":
            issued = reviewed - timedelta(seconds=1)
        review = {"claim_id": "claim", "basis_id": "sha256:" + "a" * 64, "reviewed_at": reviewed.isoformat()}
        if mode == "missing-date" and config == "evidence":
            review.pop("reviewed_at")
        envelope = {"payload": {"body": {"review": review}}, "authentication": {"issued_at": issued.isoformat()}}
        records.extend([{"kind": "execution_attempt", "data": {"attempt": {"attempt_id": config, "config_ref": config}}},
            {"kind": "execution_application", "data": {"operation": "evaluate", "attempt_id": config, "envelope": envelope}}])
        refs.append({"review_ref": digest(review)})
    session = SimpleNamespace(execution=SimpleNamespace(records=lambda *_: records, attempt=lambda document: SimpleNamespace(config_ref=document["config_ref"])))
    prepared = {"snapshot": {"policy": {"max_review_age_seconds": 60}}}
    if mode == "missing-limit":
        prepared["snapshot"]["policy"] = {}
    elif mode == "boolean-limit":
        prepared["snapshot"]["policy"]["max_review_age_seconds"] = True
    monkeypatch.setattr(bridge, "datetime", SimpleNamespace(now=lambda *_: now))
    monkeypatch.setattr(driver, "_verify_review_controller", lambda _session, attempt, _envelope: checked.append(attempt.config_ref))
    result = bridge.authenticated_gates(session, {"reviews": refs}, {"authentication": "pass"}, prepared=prepared)
    assert checked == ["evidence", "critic"]
    assert result["authentication"] == expected
