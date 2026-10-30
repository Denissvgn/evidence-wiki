"""History projection preserves uncertainty and cannot issue current approval."""

from types import SimpleNamespace

import pytest

from evidence_wiki import history_operations as history
from evidence_wiki.errors import UsageError

REVISION = "sha256:" + "a" * 64


def packet(revision=REVISION):
    return {"source_id": "source", "source_binding": {"source_id": "source", "closure_id": revision}}


def records():
    review = {"snapshot": {"source_revisions": {"source": REVISION}},
        "verdicts": {"support": "pass"}, "rationale": "The retained source supports the scoped claim."}
    return [
        {"kind": "execution_attempt", "sequence": 1, "data": {"attempt": {
            "attempt_id": "generate", "input": {"sources": [packet() | {"record_sha256": REVISION}]}}}},
        {"kind": "execution_application", "sequence": 2, "data": {"attempt_id": "generate", "operation": "generate",
            "original_id": "original", "proposal": {"inventory": {"claims": [{"id": "claim",
                "evidence": [{"source_id": "source", "record_sha256": REVISION}]}]}}}},
        {"kind": "execution_application", "sequence": 3, "data": {"attempt_id": "review", "operation": "evaluate",
            "original_id": "original", "claim_id": "claim", "profile_ref": "critic", "strict_basis_id": REVISION,
            "envelope": {"payload": {"body": {"review": review}}}}},
        {"kind": "execution_applied", "sequence": 4, "data": {"attempt_id": "review"}},
        {"kind": "execution_native", "sequence": 5, "data": {"operation": "artifact.deliver",
            "reservation": {"attempt_id": "delivery"}, "arguments": {"proposal": {}}}},
    ]


def test_known_native_projection_distinguishes_proposal_and_recorded_review():
    projected = history._items(records())
    assert [item["kind"] for item in projected] == ["source", "claim", "review", "brief"]
    assert not projected[1]["applied"] and projected[2]["applied"]
    assert projected[2]["payload"]["verdicts"] == {"support": "pass"}
    assert projected[-1]["dependencies"] == {"source": sorted([REVISION, "material:" + REVISION])}
    assert "authentication" not in projected[2]["payload"]


def test_brief_dependencies_conservatively_preserve_all_observed_revisions():
    original = records()
    original.insert(-1, {"kind": "execution_attempt", "sequence": 5, "data": {"attempt": {
        "attempt_id": "later", "input": {"sources": [packet("sha256:" + "b" * 64)]}}}})
    assert history._items(original)[-1]["dependencies"] == {"source": sorted([
        REVISION, "material:" + REVISION, "material:sha256:" + "b" * 64])}


def test_missing_source_binding_is_not_an_empty_permitted_closure():
    original = records()
    original[0]["data"]["attempt"]["input"]["sources"] = [{"source_id": "source"}]
    projected = history._items(original)
    assert projected[0]["kind"] == "claim" and projected[0]["dependencies"] is None


@pytest.mark.parametrize("fields", [{"kind": "approval"}, {"after_index": True}, {"after_index": -1},
    {"max_records": True}, {"max_records": 33}])
def test_invalid_selections_refuse_before_host_or_owner_access(fields):
    with pytest.raises(UsageError) as caught:
        history.current(None, run_id="history", execution=None, **fields)
    assert caught.value.details["reason"] == "selection_or_page_bound"


def test_retained_source_bound_is_an_unavailable_record_without_truncation():
    retained = {"descriptor": {"normalized_path": "normalized.md"},
        "files": {"normalized.md": b"x" * (history.MAX_SOURCE_BYTES + 1)}}
    payload, reason = history._source(SimpleNamespace(state=SimpleNamespace(revisions={REVISION: retained})),
        {"source_id": "source", "source_revision": REVISION})
    assert payload is None and reason == "retained_normalized_source_missing_or_bounded"


def test_unknown_historical_review_encoding_is_a_typed_refusal():
    original = records()
    original[2]["data"]["envelope"] = {"payload": {"unsupported": True}}
    with pytest.raises(UsageError) as caught:
        history._items(original)
    assert caught.value.details["reason"] == "historical_record_schema_unsupported"


def test_legacy_scope_requires_exact_protected_bytes_and_existing_request_schema(tmp_path, monkeypatch):
    import hashlib

    from evidence_wiki.mutation_contracts import canonical
    from tests.test_draft_admission import request

    declaration = {"schema_version": "evidence-research-requirements/v1", "request": request(tmp_path), "decisions": {}}
    raw = canonical(declaration)
    source = SimpleNamespace(read=lambda *_a: raw)
    policy = {"instructions": {"docs/research-requirements.json": "sha256:" + hashlib.sha256(raw).hexdigest()}}
    selected, scope = history._current_requirements(tmp_path, source, policy)
    assert selected == {"request": declaration["request"]}
    assert scope == "protected_legacy_instruction_read_only"
    def native_required(*_a):
        raise UsageError("NATIVE_LINEAGE_REQUIRED", "Protected native lineage is required.")
    monkeypatch.setattr(history, "_requirements", native_required)
    for selected_policy in ({}, {"instructions": {"docs/research-requirements.json": REVISION}}):
        with pytest.raises(UsageError) as refused:
            history._current_requirements(tmp_path, source, selected_policy)
        assert refused.value.error_code == "NATIVE_LINEAGE_REQUIRED"


def test_material_commitments_do_not_become_usage_revisions():
    import hashlib

    raw = b"---\nsource_id: source\nraw_paths: [raw/input.txt]\nprovenance: {scope: {}}\n---\nRetained normalized evidence."
    original = b"Original evidence."
    material = history.digest({"source_id": "source", "normalized_sha256": hashlib.sha256(raw).hexdigest(),
        "originals": {"raw/input.txt": hashlib.sha256(original).hexdigest()}, "scope": {}})
    normalized = "sha256:" + "b" * 64
    retained = {REVISION: {"source_id": "source", "descriptor": {"normalized_path": None, "parents": []}, "files": {}},
        normalized: {"source_id": "source", "descriptor": {"normalized_path": "normalized.md", "parents": [REVISION],
            "evidence_root": "evidence"}, "files": {"normalized.md": raw, "evidence/raw/input.txt": original}}}
    live = SimpleNamespace(state=SimpleNamespace(revisions=retained))
    exact = packet(material) | {"record_sha256": "sha256:" + hashlib.sha256(raw).hexdigest()}
    item = {"dependencies": {"source": "material:" + material}, "packets": [exact]}
    dependencies, selected = history._material_dependencies(item, live)
    assert dependencies == {"source": [normalized]}
    assert selected == {("source", material): normalized}
    retained[normalized]["descriptor"]["parents"] = []
    assert history._material_dependencies(item, live)[0] == {"source": [normalized]}
    retained[normalized]["files"]["evidence/raw/input.txt"] = b"Replacement original."
    assert history._material_dependencies(item, live) == (None, {})
    retained[normalized]["files"]["evidence/raw/input.txt"] = original
    retained["sha256:" + "c" * 64] = retained[normalized]
    assert history._material_dependencies(item, live) == (None, {})


def test_unresolved_claim_dependencies_withhold_the_entire_later_brief():
    original = records()
    original[1]["data"]["proposal"]["inventory"]["claims"][0]["premises"] = ["outside-context"]
    projected = history._items(original)
    assert projected[1]["dependencies"] is None
    assert projected[-1]["dependencies"] is None


@pytest.mark.parametrize("link", [
    {"source_id": "source", "record_sha256": "sha256:" + "b" * 64},
    {"source_id": "unscoped-peer", "record_sha256": REVISION},
    {"source_id": "source"},
])
def test_merged_peer_evidence_cannot_borrow_a_new_generating_packet(link):
    original = records()
    original[1]["data"]["proposal"]["inventory"]["claims"].append({"id": "peer", "evidence": [link]})
    projected = history._items(original)
    assert projected[1]["dependencies"] is None and projected[-1]["dependencies"] is None


def test_computation_snapshot_cannot_disclose_an_unsupported_dependency_closure():
    original = records()
    original[2]["data"]["envelope"]["payload"]["body"]["review"]["snapshot"]["computation"] = {
        "external_private_input": "unscoped"}
    projected = history._items(original)
    assert projected[2]["dependencies"] is None and projected[-1]["dependencies"] is None


@pytest.mark.parametrize("fields, reason", [
    ({"after_index": 1}, "history_basis_required_for_continuation"),
    ({"expected_history_basis_id": True}, "history_basis_invalid"),
    ({"expected_history_basis_id": "forged"}, "history_basis_invalid"),
])
def test_continuations_require_a_valid_basis_before_owner_access(fields, reason):
    with pytest.raises(UsageError) as caught:
        history.current(None, run_id="history", execution=None, **fields)
    assert caught.value.details["reason"] == reason
