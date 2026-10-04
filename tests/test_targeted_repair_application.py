"""Real admission-path scope/merge oracles with isolated native mutation effects."""

import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from evidence_wiki import draft_contracts
from evidence_wiki import execution_session as driver
from evidence_wiki.agent_execution import ExecutionAttempt, ExecutionProfile, ExecutionRole
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.run_policy import RunPolicy
from tests.test_execution_permission_adversarial import closed_proposal, isolated_boundaries
from tests.test_run_policy_scope import DIGEST, native_policy_document
from tests.test_workflow_contracts import job


def application_case(monkeypatch, *, full=True, changed_peer=False, coverage=False):
    isolated = isolated_boundaries(monkeypatch, RunPolicy(native_policy_document()))
    originals = isolated.view["accounting"]["original_outcomes"]
    first = closed_proposal(originals[0]["original_id"], originals[0]["original_text"], 1)
    target = first["inventory"]["claims"][0]
    peer = copy.deepcopy(target) | {"id": "peer", "question_slug": "second", "text": "Independent peer observation."}
    previous = [copy.deepcopy(target), copy.deepcopy(peer)]
    proposal = copy.deepcopy(first)
    proposal["originals"].append({"original_id": originals[1]["original_id"], "text": originals[1]["original_text"], "question_slugs": ["second"]})
    proposal["inventory"]["questions"].append({"slug": "second", "original_id": originals[1]["original_id"], "question": originals[1]["original_text"].strip()})
    proposal["inventory"]["claims"][0]["text"] = "A scoped material repair of the target observation."
    if full:
        proposal["inventory"]["claims"].append(copy.deepcopy(peer))
    if changed_peer:
        proposal["inventory"]["claims"][-1]["text"] = "Unauthorized replacement of peer evidence."
    proposal = draft_contracts.validate_document(draft_contracts.PROPOSAL, proposal)
    output = {"proposal": proposal}
    if coverage:
        output["coverage_links"] = [{"slug": "first", "facet_id": "facet", "source_ids": ["captured-source"]}]
    now = datetime.now(timezone.utc)
    reference = job() | {"id": "target-repair", "policy_id": isolated.session.policy.policy_id, "basis_id": DIGEST,
        "revision_precondition": DIGEST, "original_text": originals[0]["original_text"], "scope": [originals[0]["original_id"]],
        "payload": {"question": originals[0]["original_text"]}, "issued_utc": now.isoformat(),
        "deadline_utc": (now + timedelta(seconds=300)).isoformat()}
    profile = ExecutionProfile("repair-host", 8, "EUR", "repair-price", (
        ExecutionRole("researcher", "host_worker", "registered-controller", "repair-model", "prompt/v1"),), 0, 5, 0, "0")
    attempt = ExecutionAttempt.create(attempt_id="target-repair", nonce="repair-nonce", run_id=isolated.session.run_id,
        binding_id=DIGEST, profile=profile, config_ref="researcher", job=reference, input_document={})
    receipt = {"state": "succeeded", "sequence": 1, "final_accounting": True, "output": output}
    isolated.append(isolated.session, "execution_attempt", "repair-intent", {"attempt": attempt.document(),
        "original_id": originals[0]["original_id"], "purpose": "repair", "target_claim_id": target["id"], "claim_id": target["id"]})
    isolated.append(isolated.session, "execution_observation", "repair-result", {"attempt_id": attempt.attempt_id, "observation": receipt})
    isolated.session.execution.attempt = lambda _document: attempt
    isolated.session.execution.authenticate = lambda supplied, _attempt: copy.deepcopy(supplied)
    monkeypatch.setattr(driver, "ResearchObservation", lambda *_args, **_kwargs: SimpleNamespace(
        strict={"result": {"claims": [{"claim": claim} for claim in previous]}}, finish=lambda: None))
    mutations = []
    monkeypatch.setattr(driver, "_mutation", lambda _session, _identifier, operation, arguments, **_kwargs:
        mutations.append((operation, copy.deepcopy(arguments))))
    return isolated, previous, mutations


@pytest.mark.parametrize("full", [True, False], ids=["complete-native-inventory", "target-original-claims"])
def test_full_or_target_original_repair_admits_exact_preserved_peer_inventory(monkeypatch, full):
    isolated, previous, mutations = application_case(monkeypatch, full=full)
    driver._apply(isolated.session, "target-repair")
    assert len(mutations) == 1 and mutations[0][0] == "strict.admit"
    actual = mutations[0][1]["proposal"]["inventory"]["claims"]
    assert {claim["id"] for claim in actual} == {claim["id"] for claim in previous}
    assert next(claim for claim in actual if claim["id"] == "peer") == previous[1]
    assert next(claim for claim in actual if claim["id"] != "peer")["text"] != previous[0]["text"]


def test_full_repair_inventory_cannot_change_other_original_peer(monkeypatch):
    isolated, _, mutations = application_case(monkeypatch, changed_peer=True)
    with pytest.raises((EvidenceWikiError, ValueError)):
        driver._apply(isolated.session, "target-repair")
    assert mutations == []
    assert not any(item["kind"] == "execution_application" for item in isolated.records)


def test_targeted_repair_cannot_admit_claim_then_change_coverage(monkeypatch):
    isolated, _, mutations = application_case(monkeypatch, coverage=True)
    with pytest.raises((EvidenceWikiError, ValueError)):
        driver._apply(isolated.session, "target-repair")
    assert mutations == []
    assert not any(item["kind"] == "execution_application" for item in isolated.records)
