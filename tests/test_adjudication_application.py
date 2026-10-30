"""Adjudication intake effects using real closed contexts and host receipt mechanics.

Canonical current/read/grant observations are controlled stubs. These cases do
not establish live model/provider qualification or full public-session isolation.
"""

import copy
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from evidence_wiki import execution_session as driver
from evidence_wiki.agent_execution import DeterministicOfflineExecutor, ExecutionAttempt, ExecutionRole
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.execution_disagreement import adjudication_packet, attempt_metadata, material_basis, project_claim
from evidence_wiki.mutation_contracts import digest
from tests.test_agent_execution import attempt, profile
from tests.test_disagreement import gates
from tests.test_execution_disagreement import journal as journal_fixture


@contextmanager
def no_gate(*_args, **_kwargs):
    yield


def setup_case(tmp_path, monkeypatch, output):
    session, append, events, prepared, sources, claim_id = journal_fixture.__wrapped__(tmp_path, monkeypatch)
    selected = replace(session.execution.profile, roles=session.execution.profile.roles + (
        ExecutionRole("adjudicator", "evaluator", "adjudicator-controller", "offline/adjudicator", "prompt/v1"),))
    session.execution._profile = selected
    sign_calls, mutations = [], []
    session.execution._review_signer = SimpleNamespace(generator=SimpleNamespace(controller_ref="generator"),
        registration=lambda *_args, **_kwargs: sign_calls.append("unexpected-native-signing"))
    prepared["snapshot"]["policy"]["max_review_age_seconds"] = 3600
    prepared["result"]["claims"] = [{"claim": prepared["snapshot"]["claim"], "accepted": False,
        "observation": {"reasons": []}, "review": {"passed": False, "reason": "strict_review_not_passed"}}]
    for config, support in (("profile-a", "pass"), ("critic", "fail")):
        role = selected.role(config)
        verdicts = dict.fromkeys(("support", "source_suitability", "scope", "time", "units", "counterevidence"), "pass")
        verdicts["support"] = support
        answer = {"verdicts": verdicts, "rationale": "Bounded evidence-linked observation."}
        reference = attempt(profile()).job | {"role": "evaluator", "controller_ref": role.controller_ref,
            "operation": "evaluate", "payload": {"claim_id": claim_id, "packet_id": prepared["result"]["basis_id"]}}
        value = ExecutionAttempt.create(attempt_id=config, nonce="nonce-" + config, run_id=session.run_id,
            binding_id=digest(session.binding), profile=selected, config_ref=config, job=reference)
        host = DeterministicOfflineExecutor(selected, outputs={config: answer}, receipt_key=b"e" * 32)
        observed = host.authenticate(host.invoke(value), value)
        append("execution_attempt", {"attempt": value.document(), "purpose": "evaluate", "claim_id": claim_id,
            "review_material_basis": material_basis(prepared["snapshot"], prepared["rubric"], sources)})
        append("execution_observation", {"attempt_id": value.attempt_id, "observation": observed})
        now = datetime.now(timezone.utc).isoformat()
        review = {"claim_id": claim_id, "basis_id": prepared["result"]["basis_id"], "verdicts": verdicts,
            "rationale": answer["rationale"], "reviewed_at": now}
        app = {"attempt_id": config, "operation": "evaluate", "claim_id": claim_id, "profile_ref": config,
            "strict_basis_id": prepared["result"]["basis_id"], "envelope": {"payload": {"body": {"review": review}},
                "authentication": {"issued_at": now}}}
        append("execution_application", app)
        append("execution_applied", {key: app[key] for key in ("attempt_id", "operation", "claim_id", "profile_ref", "strict_basis_id")})
    bundle = adjudication_packet(session, claim_id=claim_id, prepared=prepared, sources=sources,
        context_id="adjudication", order_seed=b"r" * 32, gates=gates())
    projection = project_claim(session, claim_id=claim_id, prepared=prepared, sources=sources)
    action = {"operation": "adjudicate", "claim_id": claim_id, "basis_id": projection["basis_id"],
        "defect_sha256": "sha256:" + "d" * 64}
    view = {"basis_id": prepared["result"]["basis_id"], "qualification": {"state": "qualified",
        "auxiliary_profiles": [{"profile_ref": "adjudicator", "qualified": True, "independent": True}]},
        "execution": {"accounting": {"usage_unknown": False, "overrun": []}, "steps_used": 0, "steps_limit": 8,
            "source_bindings": []}, "authority": {"terminal_grants_available": True},
        "usage": {"repair_cycles": 0, "adjudication_cycles": 1, "acquisition_attempts": 0, "claims": 1, "sources": 2}}
    reference = attempt(profile()).job | {"role": "evaluator", "controller_ref": "adjudicator-controller", "operation": "evaluate",
        "basis_id": view["basis_id"], "payload": {"claim_id": claim_id, "packet_id": digest(prepared["snapshot"])}}
    value = ExecutionAttempt.create(attempt_id="investigator", nonce="adjudication-nonce", run_id=session.run_id,
        binding_id=digest(session.binding), profile=selected, config_ref="adjudicator", job=reference,
        input_document={"packet": bundle["packet"]["packet"], "investigation": bundle["packet"]})
    host = DeterministicOfflineExecutor(selected, outputs={"adjudicator": output}, receipt_key=b"h" * 32)
    observed = host.authenticate(host.invoke(value), value)
    append("execution_attempt", {"attempt": value.document(), "purpose": "adjudicate", "original_id": "original",
        "claim_id": claim_id, "review_context": bundle["binding"]["review_context"], "adjudication_binding": bundle["binding"],
        "disagreement": attempt_metadata(action, cycle_ref="investigation-cycle")})
    append("execution_observation", {"attempt_id": value.attempt_id, "observation": observed})
    session.execution.authenticate = host.authenticate
    monkeypatch.setattr(driver, "gate", no_gate)
    monkeypatch.setattr(driver, "current", lambda *_args: copy.deepcopy(view))
    monkeypatch.setattr(driver, "_sources", lambda *_args: (copy.deepcopy(sources), []))
    monkeypatch.setattr(driver, "append", lambda _session, kind, _request, data: append(kind, data))
    monkeypatch.setattr(driver, "_mutation", lambda *_args, **_kwargs: mutations.append("unexpected-native-mutation"))
    owner = driver.owner
    monkeypatch.setattr(driver, "owner", lambda name: SimpleNamespace(prepare_review=lambda *_args: prepared)
        if name == "strict_evidence" else owner(name))
    verified = []
    monkeypatch.setattr(driver, "_verify_review_controller", lambda _session, attempt, _envelope: verified.append(attempt.config_ref))
    return SimpleNamespace(session=session, append=append, events=events, view=view, prepared=prepared, sources=sources,
        attempt=value, verified=verified, sign_calls=sign_calls, mutations=mutations, bundle=bundle)


@pytest.mark.parametrize("recommendation", ["repair", "abstain"])
def test_valid_investigator_findings_only_retain_inert_application_without_native_review(tmp_path, monkeypatch, recommendation):
    case = setup_case(tmp_path, monkeypatch, {"schema_version": "research-adjudication-findings/v1",
        "recommendation": recommendation, "rationale": "Inspect the retained conflicting passage."})
    driver._apply(case.session, "investigator")
    records = case.session.execution.records(case.session)
    application = next(item["data"] for item in records if item["kind"] == "execution_application" and item["data"]["attempt_id"] == "investigator")
    applied = next(item["data"] for item in records if item["kind"] == "execution_applied" and item["data"]["attempt_id"] == "investigator")
    assert application["operation"] == applied["operation"] == "adjudicate"
    assert application["findings"]["recommendation"] == recommendation
    assert "envelope" not in application
    assert case.sign_calls == case.mutations == []
    assert set(case.verified) == {"profile-a", "critic"}


@pytest.mark.parametrize("bad", ["approve", "authority", "malformed", "missing-rationale"])
def test_investigator_cannot_create_approval_authority_or_malformed_native_effect(tmp_path, monkeypatch, bad):
    output = {"schema_version": "research-adjudication-findings/v1", "recommendation": "repair", "rationale": "Bounded finding."}
    if bad == "approve":
        output["recommendation"] = "approve"
    elif bad == "authority":
        output["authorized"] = True
    elif bad == "malformed":
        output["recommendation"] = ["repair"]
    else:
        output.pop("rationale")
    case = setup_case(tmp_path, monkeypatch, output)
    with pytest.raises((EvidenceWikiError, ValueError, TypeError)):
        driver._apply(case.session, "investigator")
    assert case.sign_calls == case.mutations == []
    assert not any(item["kind"] == "execution_applied" and item["data"]["attempt_id"] == "investigator"
        for item in case.session.execution.records(case.session))


def test_current_required_key_revocation_blocks_investigator_before_signing_or_effect(tmp_path, monkeypatch):
    case = setup_case(tmp_path, monkeypatch, {"schema_version": "research-adjudication-findings/v1", "recommendation": "repair", "rationale": "Bounded finding."})
    def revoked(_session, value, _envelope):
        if value.config_ref == "profile-a":
            raise EvidenceWikiError("EVIDENCE_REVOKED", "Current native key is revoked.")
    monkeypatch.setattr(driver, "_verify_review_controller", revoked)
    with pytest.raises(EvidenceWikiError):
        driver._apply(case.session, "investigator")
    assert case.sign_calls == case.mutations == []


def test_new_authenticated_first_pass_after_packet_seal_blocks_stale_investigation(tmp_path, monkeypatch):
    case = setup_case(tmp_path, monkeypatch, {"schema_version": "research-adjudication-findings/v1", "recommendation": "repair", "rationale": "Bounded finding."})
    role = case.session.execution.profile.role("profile-a")
    answer = {"verdicts": dict.fromkeys(("support", "source_suitability", "scope", "time", "units", "counterevidence"), "pass"),
        "rationale": "Additional authenticated first-pass evidence changes retained context."}
    reference = attempt(profile()).job | {"role": "evaluator", "controller_ref": role.controller_ref, "operation": "evaluate",
        "payload": {"claim_id": case.prepared["snapshot"]["claim"]["id"], "packet_id": case.prepared["result"]["basis_id"]}}
    value = ExecutionAttempt.create(attempt_id="additional-evidence", nonce="additional-nonce", run_id=case.session.run_id,
        binding_id=digest(case.session.binding), profile=case.session.execution.profile, config_ref="profile-a", job=reference)
    host = DeterministicOfflineExecutor(case.session.execution.profile, outputs={"profile-a": answer}, receipt_key=b"e" * 32)
    receipt = host.authenticate(host.invoke(value), value)
    claim_id = case.prepared["snapshot"]["claim"]["id"]
    case.append("execution_attempt", {"attempt": value.document(), "purpose": "evaluate", "claim_id": claim_id,
        "review_material_basis": material_basis(case.prepared["snapshot"], case.prepared["rubric"], case.sources)})
    case.append("execution_observation", {"attempt_id": value.attempt_id, "observation": receipt})
    now = datetime.now(timezone.utc).isoformat()
    native = {"claim_id": claim_id, "basis_id": case.prepared["result"]["basis_id"], "verdicts": answer["verdicts"],
        "rationale": answer["rationale"], "reviewed_at": now}
    app = {"attempt_id": value.attempt_id, "operation": "evaluate", "claim_id": claim_id, "profile_ref": "profile-a",
        "strict_basis_id": case.prepared["result"]["basis_id"], "envelope": {"payload": {"body": {"review": native}}, "authentication": {"issued_at": now}}}
    case.append("execution_application", app)
    case.append("execution_applied", {key: app[key] for key in ("attempt_id", "operation", "claim_id", "profile_ref", "strict_basis_id")})
    with pytest.raises(EvidenceWikiError):
        driver._apply(case.session, "investigator")
    assert case.sign_calls == case.mutations == []
