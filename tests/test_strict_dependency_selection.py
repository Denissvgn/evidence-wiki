"""Selective review selection preserves current facts and original custody."""

import math
from copy import deepcopy
from types import SimpleNamespace

import pytest

from tests.test_strict_evidence import CORE

BASIS = "sha256:" + "1" * 64
WORKSPACE = "sha256:" + "2" * 64
PRODUCER = "sha256:" + "3" * 64
AUTHORITY = "sha256:" + "4" * 64


def candidate(name, verdict="pass", *, schema="evidence-strict-review/v3", basis=BASIS):
    payload = {"schema_version": schema, "review_basis_mode": "claim_dependencies_v1",
        "basis_id": basis, "observed_workspace_basis_id": WORKSPACE, "claim_id": "target",
        "claim_schema_version": "evidence-strict-claims/v1", "verdicts": {"support": verdict}, "name": name}
    return {"envelope": {"payload": {"body": {"review": payload}}}, "authority": {"content_hash": AUTHORITY}}


def selection(monkeypatch, records, *, reject=None, workspace=WORKSPACE):
    current = {"dependency_basis": {"basis_id": BASIS}, "material": {"claim_schema_version": "evidence-strict-claims/v1"},
        "required_review_role": "evaluator"}
    view = SimpleNamespace(state=SimpleNamespace(strict_reviews=dict(enumerate(records))), trust={"content_hash": AUTHORITY})
    checked = []
    monkeypatch.setattr(CORE, "review_document", lambda payload: payload)
    monkeypatch.setattr(CORE, "artifact_id", lambda payload: "sha256:" + ("5" if payload["name"] == "old" else "6") * 64)
    monkeypatch.setattr(CORE, "_dependency_authenticated", lambda *args: {"principal": "reviewer"})

    def original(_root, _config, _policy, payload, _current, *, purpose, envelope):
        assert envelope["payload"]["body"]["review"] is payload
        checked.append((payload["name"], purpose))
        if payload["name"] == reject:
            raise CORE.refusal("selective_original_custody_unavailable")
        return SimpleNamespace(assert_current=lambda: None, assert_review_envelope=lambda envelope: None)

    monkeypatch.setattr(CORE, "_dependency_original", original)
    result = CORE.dependency_review_result(None, {}, {}, workspace, {"id": "target"}, view, current)
    return result, checked


@pytest.mark.parametrize("verdict", ["fail", "unknown"])
def test_newest_applicable_negative_blocks_older_pass(monkeypatch, verdict):
    (review, observed), checked = selection(monkeypatch, [candidate("old"), candidate("new", verdict)])
    assert not review["passed"]
    assert review["reason"] == "strict_review_not_passed"
    assert review["review_id"] == "sha256:" + "6" * 64
    assert observed == WORKSPACE
    assert checked == [("new", "reuse")]


def test_newest_applicable_missing_custody_cannot_fall_back(monkeypatch):
    (review, observed), checked = selection(monkeypatch, [candidate("old"), candidate("new")], reject="new")
    assert review == {"passed": False, "reason": "selective_original_custody_unavailable"}
    assert observed is None
    assert checked == [("new", "reuse")]


def test_newest_applicable_changed_authority_cannot_fall_back(monkeypatch):
    newer = candidate("new")
    newer["authority"]["content_hash"] = "sha256:" + "9" * 64
    (review, observed), checked = selection(monkeypatch, [candidate("old"), newer])
    assert review == {"passed": False, "reason": "strict_review_authority_changed"}
    assert observed is None and checked == []


def test_whole_workspace_review_is_never_selective_fallback(monkeypatch):
    (review, observed), checked = selection(monkeypatch, [candidate("old", schema="evidence-strict-review/v1")])
    assert review == {"passed": False, "reason": "strict_review_missing_or_stale"}
    assert observed is None and checked == []


def test_inapplicable_newer_review_does_not_supersede_current_basis(monkeypatch):
    (review, observed), checked = selection(monkeypatch, [candidate("old"), candidate("new", basis=WORKSPACE)])
    assert review["passed"] and review["reuse"] == "fresh"
    assert observed == WORKSPACE and checked == [("old", "reuse")]


def test_selected_original_workspace_provenance_remains_historical(monkeypatch):
    (review, observed), checked = selection(monkeypatch, [candidate("old")], workspace="sha256:" + "7" * 64)
    assert review["passed"] and review["basis_id"] == BASIS and review["reuse"] == "reused"
    assert observed == WORKSPACE and checked == [("old", "reuse")]


def test_selective_snapshot_preserves_typed_metadata_exact_body_and_sorted_premises(tmp_path):
    question = CORE.sibling("_question_dependencies")
    target = b"---\ntype: question\nquestion: Target?\nstatus: open\nmetadata:\n  observed: 2026-10-16\n  at: 2026-10-16T12:34:56Z\n  label: caf\xc3\xa9 \xe6\xb5\xb7\n  ratio: -0.0\n---\n\nExact target.\r\n"
    premise = b"---\ntype: question\nquestion: Premise?\nstatus: open\ncreated: 2026-10-15\n---\nPremise body.\n"
    claims = [{"id": "target", "question_slug": "target"}, {"id": "z", "question_slug": "premise"},
        {"id": "a", "question_slug": "premise"}]
    records = [{"slug": slug, "original_id": slug, "question": slug + "?"} for slug in ("target", "premise")]
    material = {"claims": claims, "questions": [{"manifest_question": row, "path": row["slug"] + ".md"} for row in records]}
    copied = deepcopy(material)
    snapshot = CORE._dependency_snapshot(tmp_path, {}, {}, SimpleNamespace(files={"target.md": target, "premise.md": premise}),
        {"questions": records}, material, {"selected": {}}, None)
    assert snapshot["question_body"] == "\nExact target.\r\n"
    assert question.typed_value(snapshot["metadata"])["observed"].isoformat() == "2026-10-16"
    assert [row["id"] for row in snapshot["premises"]] == ["a", "z"]
    assert len(snapshot["premise_questions"]) == 1
    page = snapshot["premise_questions"][0]["page"]
    assert question.typed_value(page["frontmatter"])["created"].isoformat() == "2026-10-15"
    assert page["body"] == "Premise body.\n"
    assert snapshot["semantic_material"] == copied == material
    contract = CORE.sibling("_review_dependency_contract")
    raw = contract.canonical_bytes(snapshot)
    assert contract._exact_json_bytes(snapshot)[1]
    assert snapshot["claim"] == claims[0] and snapshot["claim"] is not claims[0]
    assert snapshot["claim"] is not snapshot["semantic_material"]["claims"][0]
    assert snapshot["question"] is not records[0]
    assert snapshot["premises"][0] is not snapshot["semantic_material"]["claims"][2]
    metadata = question.typed_value(snapshot["metadata"])
    assert metadata["at"].isoformat() == "2026-10-16T12:34:56+00:00"
    assert metadata["label"] == "café 海"
    assert math.copysign(1, metadata["ratio"]) == -1
    expected = deepcopy(snapshot)
    expected["claim"] = claims[0]
    expected["question"] = records[0]
    expected["premises"] = sorted(claims[1:], key=lambda row: row["id"].encode("utf-8"))
    expected["premise_questions"][0]["record"] = records[1]
    expected["semantic_material"] = material
    assert contract.canonical_bytes(expected) == raw
    assert contract.content_id("evidence-strict-review/v3", snapshot) == contract.content_id("evidence-strict-review/v3", expected)
    snapshot["claim"]["id"] = "changed-snapshot"
    assert snapshot["semantic_material"]["claims"][0]["id"] == claims[0]["id"] == "target"
    material["claims"][1]["id"] = "changed-source"
    assert snapshot["semantic_material"]["claims"][1]["id"] == "z"


def test_selective_authorship_binds_explicit_claim_schema():
    payload = {"basis_id": BASIS, "claim_id": "target", "claim_schema_version": "evidence-strict-claims/v2",
        "observed_workspace_basis_id": WORKSPACE, "dependency_basis": {"semantic_material_id": PRODUCER}}
    authorship = CORE._dependency_authorship(payload)
    assert set(authorship) == {"schema_version", "basis_id", "claim_id", "claim_schema_version",
        "observed_workspace_basis_id", "semantic_material_id"}
    assert authorship["claim_schema_version"] == "evidence-strict-claims/v2"


def test_fresh_admission_uses_admission_proof_without_evaluating_history(monkeypatch):
    current = {"snapshot": {"original": True}}
    payload = {"schema_version": "evidence-strict-review/v3", "observed_workspace_basis_id": WORKSPACE,
        "claim_schema_version": "evidence-strict-claims/v1", "claim_id": "target", "snapshot": current["snapshot"]}
    native_envelope = {"payload": {"body": {"review": payload}}}
    calls = []
    monkeypatch.setattr(CORE, "selection_inputs", lambda *args, **kwargs:
        (object(), {"schema_version": "evidence-strict-claims/v1"}, WORKSPACE))
    monkeypatch.setattr(CORE, "_review_producer", lambda: object())
    monkeypatch.setattr(CORE, "dependency_current", lambda *args: current)
    monkeypatch.setattr(CORE, "_dependency_authenticated", lambda *args: calls.append("authentication"))
    monkeypatch.setattr(CORE, "_dependency_closing", lambda *args: calls.append("current_closing"))

    def original(_root, _config, _policy, supplied, _current, *, purpose, envelope):
        assert supplied is payload and envelope is native_envelope
        calls.append(purpose)
        calls.append("native_envelope")
        return SimpleNamespace(assert_current=lambda: calls.append("proof_closing"))

    def history(*_args, **_kwargs):
        pytest.fail("fresh admission must not evaluate review history")

    monkeypatch.setattr(CORE, "_dependency_original", original)
    monkeypatch.setattr(CORE, "evaluate", history)
    monkeypatch.setattr(CORE, "review_result", history)
    CORE.validate_dependency_review(None, {}, {}, native_envelope, payload, object())
    assert "admission" in calls and "reuse" not in calls
    assert calls == ["authentication", "admission", "native_envelope", "current_closing", "proof_closing"]


def test_fresh_admission_cannot_claim_a_historical_workspace_basis(monkeypatch):
    monkeypatch.setattr(CORE, "selection_inputs", lambda *args, **kwargs: (object(), {}, WORKSPACE))
    payload = {"schema_version": "evidence-strict-review/v3", "observed_workspace_basis_id": BASIS}
    with pytest.raises(Exception) as caught:
        CORE.validate_dependency_review(None, {}, {}, {}, payload, object())
    assert caught.value.details["reason"] == "strict_review_basis_changed"
