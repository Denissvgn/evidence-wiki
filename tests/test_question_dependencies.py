"""Question dependency semantics preserve types, exact bodies and native mirrors."""

import copy
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from evidence_wiki.pack_discovery import owner


@pytest.fixture
def projection():
    return owner("_question_dependencies")


def _manifest():
    return {"schema_version": "evidence-strict-claims/v1",
            "questions": [{"slug": "question", "original_id": "original", "question": "What was measured?"}],
            "claims": [{"id": "measurement", "question_slug": "question", "qualification": "supported",
                        "text": "The measured value is seven.", "scope": "retained record", "time": "current record",
                        "units": "count", "evidence": [{"source_id": "source", "record_sha256": "sha256:" + "a" * 64,
                            "observed_at": "2026-01-01T00:00:00Z", "capture": "primary", "quote": "Measured value: 7",
                            "location_hint": None, "anchor": None}], "premises": [], "derivation": None, "limitations": []}]}


def _frontmatter():
    return {"type": "question", "status": "open", "created": date(2026, 1, 1), "updated": date(2026, 1, 1),
            "question": "What was measured?", "metadata": {"instructions": {"preserve": [True, 1, "1"]}}}


def _project(projection, frontmatter, *, manifest=None, body="\n# Question\n\nWhole body.\n", newline="\n"):
    document = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)
    raw = ("---\n" + document + "---\n" + body).replace("\n", newline).encode()
    capture = SimpleNamespace(files={"wiki/questions/question.md": raw})
    return projection.project(Path("/workspace"), {"wiki": {"root": "wiki"}}, {"human_review": False},
                              capture, manifest or _manifest(), "question", coverage_summary={"policy_results": []})


def test_exact_body_keeps_every_original_line_ending(projection):
    raw = b"---\r\ntype: question\r\nstatus: open\r\n---\r\n\r\nBody\r\n  \r\n"
    frontmatter, body = projection.parse_page(raw)
    assert frontmatter == {"type": "question", "status": "open"}
    assert body.encode() == b"\r\nBody\r\n  \r\n"
    assert projection.parse_page(raw.replace(b"\r\n", b"\n"))[1] != body


def test_typed_nodes_preserve_native_scalar_types_and_signed_zero(projection):
    values = [None, False, 0, 0.0, -0.0, "2026-01-01", date(2026, 1, 1),
              datetime(2026, 1, 1, 1, 2, 3, 456, tzinfo=timezone.utc)]
    nodes = [projection.typed_node(value) for value in values]
    assert len({repr(node) for node in nodes}) == len(values)
    for node in nodes:
        assert projection.typed_node(projection.typed_value(node)) == node
    for value in [float("nan"), float("inf"), {1: "ambiguous"}, (1, 2), b"binary"]:
        with pytest.raises((ValueError, projection.sibling("_strict_evidence").ScriptRefusal)):
            projection.typed_node(value)


@pytest.mark.parametrize("yaml_body", ["type: question\ntype: source\n", "metadata: &value {x: 1}\ncopy: *value\n",
                                      "metadata: {<<: {x: 1}}\n", "metadata: {1: value}\n", "metadata: !!set {x: null}\n"])
def test_ambiguous_or_unsupported_yaml_refuses(projection, yaml_body):
    with pytest.raises((ValueError, projection.sibling("_strict_evidence").ScriptRefusal)):
        projection.parse_page(("---\n" + yaml_body + "---\nBody\n").encode())


def test_valid_coordination_changes_raw_custody_without_changing_semantics(projection):
    before = _frontmatter()
    after = {**before, "status": "in_progress", "updated": "2026-02-02", "claimed_by": "reviewer",
             "claimed_at": "2026-02-02T12:00:00Z", "human_review_status": "pending",
             "human_reviews": [{"policy": "policy", "verdict": "rejected", "reviewed_by": "reviewer",
                                "reviewed_at": "2026-02-02T12:00:00Z", "note": "private inert history"}]}
    assert _project(projection, before) == _project(projection, after)


@pytest.mark.parametrize("field,value", [("claimed_at", date(2026, 1, 1)), ("claimed_at", "2026-02-30T12:00:00Z"),
                                        ("updated", datetime(2026, 1, 1)), ("human_review_approved", 1),
                                        ("claimed_by", " "), ("human_reviews", [{"policy": "p", "extension": None}])])
def test_malformed_coordination_never_becomes_a_harmless_omission(projection, field, value):
    with pytest.raises((ValueError, projection.sibling("_strict_evidence").ScriptRefusal)):
        _project(projection, {**_frontmatter(), field: value})


def test_all_current_claims_derive_mirrors_even_with_unresolved_sibling(projection):
    manifest = _manifest()
    sibling = copy.deepcopy(manifest["claims"][0])
    sibling["id"] = "sibling"
    sibling["qualification"] = "contested"
    sibling["text"] = "A separate unresolved claim."
    sibling["evidence"][0]["quote"] = "Another exact quote."
    manifest["claims"].append(sibling)
    page = _project(projection, _frontmatter(), manifest=manifest)
    assert [entry["claim"] for entry in page["canonical_mirrors"]["grounding"]] == [row["text"] for row in manifest["claims"]]
    assert page["canonical_mirrors"]["source_ids"] == ["source"]


def test_native_materialization_preserves_pending_projection(projection):
    pending = _project(projection, _frontmatter())
    answered = {**_frontmatter(), **pending["canonical_mirrors"], "status": "answered", "answered_by": "worker"}
    assert _project(projection, answered) == pending
    answered.pop("grounding")
    with pytest.raises((ValueError, projection.sibling("_strict_evidence").ScriptRefusal)):
        _project(projection, answered)


def test_unknown_grounding_keys_are_rejected_before_native_parser_ignores_them(projection):
    initial = _project(projection, _frontmatter())
    actual = {**_frontmatter(), "grounding": copy.deepcopy(initial["canonical_mirrors"]["grounding"])}
    actual["grounding"][0]["extension"] = None
    with pytest.raises((ValueError, projection.sibling("_strict_evidence").ScriptRefusal)):
        _project(projection, actual)


def test_unknown_instructions_and_full_metadata_remain_semantic(projection):
    before = _project(projection, _frontmatter())
    changed = _frontmatter()
    changed["metadata"]["instructions"]["preserve"].append("new instruction")
    assert _project(projection, changed) != before
    assert _project(projection, {**_frontmatter(), "claimed_by_instruction": "New instruction"}) != before
    assert _project(projection, {**_frontmatter(), "confidence": "high"}) != before
    assert _project(projection, {**_frontmatter(), "created": "2026-01-01"}) != before


@pytest.mark.parametrize("field,value", [("coverage_required", False), ("grounding_required", False),
                                        ("answer_page", "elsewhere.md"), ("human_review_required", True),
                                        ("coverage_manifest", "sources/coverage/other.yml"),
                                        ("human_review_policies", ["unexplained"])])
def test_unexplained_mirrors_refuse(projection, field, value):
    with pytest.raises((ValueError, projection.sibling("_strict_evidence").ScriptRefusal)):
        _project(projection, {**_frontmatter(), field: value})


def test_question_projection_has_exact_five_fields(projection):
    assert set(_project(projection, _frontmatter())) == {"manifest_question", "path", "semantic_frontmatter", "body", "canonical_mirrors"}



def test_typed_depth_boundary_matches_closed_semantic_node_budget(projection):
    supported = "leaf"
    for _index in range(15):
        supported = [supported]
    encoded = projection.typed_node(supported)
    assert projection.typed_value(encoded) == supported
    with pytest.raises(projection.sibling("_strict_evidence").ScriptRefusal):
        projection.typed_node([supported])
    with pytest.raises(projection.sibling("_strict_evidence").ScriptRefusal):
        projection.typed_value({"t": "list", "v": [encoded]})


def test_mapping_keys_count_toward_the_shared_semantic_node_budget(projection):
    # One list, sixteen mappings and their key/value nodes total 65,521.
    rows = [{str(index): None for index in range(2047)} for _row in range(16)]
    rows.append({str(index): None for index in range(6)})
    # The last mapping plus twelve key/value nodes makes the total 65,534.
    node = projection.typed_node(rows)
    assert projection.typed_value(node) == rows
    rows[-1]["another"] = None
    # Two extra key/value nodes still exactly fit the maximum.
    node = projection.typed_node(rows)
    assert projection.typed_value(node) == rows
    rows[-1]["overflow"] = None
    with pytest.raises(projection.sibling("_strict_evidence").ScriptRefusal):
        projection.typed_node(rows)
    invalid = {"t": "list", "v": [*node["v"], {"t": "null"}]}
    with pytest.raises(projection.sibling("_strict_evidence").ScriptRefusal):
        projection.typed_value(invalid)
