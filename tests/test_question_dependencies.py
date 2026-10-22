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


def _project(projection, frontmatter, *, manifest=None, dependency_target=None, body="\n# Question\n\nWhole body.\n", newline="\n"):
    document = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)
    raw = ("---\n" + document + "---\n" + body).replace("\n", newline).encode()
    capture = SimpleNamespace(files={"wiki/questions/question.md": raw})
    arguments = (Path("/workspace"), {"wiki": {"root": "wiki"}}, {"human_review": False},
                 capture, manifest or _manifest(), "question")
    if dependency_target is not None:
        return projection.project_dependencies(*arguments, dependency_target, coverage_summary={"policy_results": []})
    return projection.project(*arguments, coverage_summary={"policy_results": []})


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


def test_dependency_projection_rebuilds_stale_materialized_mirrors(projection):
    manifest = _manifest()
    canonical = _project(projection, _frontmatter(), manifest=manifest)["canonical_mirrors"]
    answered = {**_frontmatter(), **canonical, "status": "answered", "answered_by": "worker"}
    manifest["claims"][0]["text"] = "The current measurement is seven."
    manifest["claims"][0]["evidence"][0]["source_id"] = "current-source"
    actual = _project(projection, answered, manifest=manifest, dependency_target="measurement")
    expected = _project(projection, _frontmatter(), manifest=manifest)
    assert actual == expected
    assert actual["canonical_mirrors"]["source_ids"] == ["current-source"]
    with pytest.raises(projection.sibling("_strict_evidence").ScriptRefusal) as caught:
        _project(projection, answered, manifest=manifest)
    assert caught.value.details["reason"] == "selective_question_mirror_mismatch"


@pytest.mark.parametrize("change", ["missing", "unknown_grounding", "grounding_type", "source_type", "duplicate_source",
                                    "coverage_path", "coverage_flag", "answer_path", "human_policy"])
def test_dependency_projection_preserves_materialized_shape_and_policy_checks(projection, change):
    canonical = _project(projection, _frontmatter())["canonical_mirrors"]
    answered = {**_frontmatter(), **canonical, "status": "answered", "answered_by": "worker"}
    if change == "missing":
        answered.pop("grounding")
    elif change == "unknown_grounding":
        answered["grounding"][0]["extension"] = "unsupported"
    elif change == "grounding_type":
        answered["grounding"] = "unsupported"
    elif change == "source_type":
        answered["source_ids"] = "source"
    elif change == "duplicate_source":
        answered["source_ids"] = ["source", "source"]
    elif change == "coverage_path":
        answered["coverage_manifest"] = "sources/coverage/elsewhere.yml"
    elif change == "coverage_flag":
        answered["coverage_required"] = False
    elif change == "answer_path":
        answered["answer_page"] = "elsewhere.md"
    else:
        answered["human_review_required"] = True
    with pytest.raises(projection.sibling("_strict_evidence").ScriptRefusal) as caught:
        _project(projection, answered, dependency_target="measurement")
    assert caught.value.details["reason"] in {"selective_question_mirrors_missing",
        "selective_question_grounding_invalid", "selective_question_mirror_invalid", "selective_question_mirror_mismatch"}


def test_dependency_projection_derives_transitive_closure_instead_of_sibling_mirrors(projection):
    manifest = _manifest()
    base = manifest["claims"][0]
    premise = {**copy.deepcopy(base), "id": "premise", "text": "The retained premise is seven."}
    target = {**copy.deepcopy(base), "id": "derived", "qualification": "inference", "premises": ["premise"],
              "derivation": "Use the retained premise.", "text": "The derived value is seven."}
    unrelated = {**copy.deepcopy(base), "id": "unrelated", "text": "Another observation.",
                 "evidence": [{**base["evidence"][0], "source_id": "other-source"}]}
    manifest["claims"] = [premise, target, unrelated]
    actual = _project(projection, _frontmatter(), manifest=manifest, dependency_target="derived")
    mirrors = actual["canonical_mirrors"]
    assert [row["claim"] for row in mirrors["grounding"]] == [target["text"], premise["text"]]
    assert mirrors["source_ids"] == ["source"]
    assert len(_project(projection, _frontmatter(), manifest=manifest)["canonical_mirrors"]["grounding"]) == 3
    with pytest.raises(projection.sibling("_review_dependencies").sibling("_strict_evidence").ScriptRefusal) as caught:
        _project(projection, _frontmatter(), manifest=manifest, dependency_target="missing")
    assert caught.value.details["reason"] == "selective_claim_unknown"


@pytest.mark.parametrize("field", ["source_count", "source_length", "claim", "grounding_source", "quote",
                                   "location_hint", "anchor_pointer", "anchor_expected"])
def test_dependency_projection_refuses_oversized_stale_mirrors(projection, field):
    canonical = _project(projection, _frontmatter())["canonical_mirrors"]
    answered = {**_frontmatter(), **canonical, "status": "answered", "answered_by": "worker"}
    if field == "source_count":
        answered["source_ids"] = ["source-" + str(index) for index in range(65)]
    elif field == "source_length":
        answered["source_ids"] = ["s" * 257]
    elif field in {"anchor_pointer", "anchor_expected"}:
        answered["grounding"][0] = {"claim": "Retained claim.", "source_id": "source",
            "anchor": {"pointer": "/value", "expected": "7"}}
        key, length = ("pointer", 513) if field == "anchor_pointer" else ("expected", 4097)
        answered["grounding"][0]["anchor"][key] = "x" * length
    else:
        key, length = {"claim": ("claim", 4097), "grounding_source": ("source_id", 257),
                       "quote": ("quote", 4097), "location_hint": ("location_hint", 513)}[field]
        answered["grounding"][0][key] = "x" * length
    with pytest.raises(projection.sibling("_strict_evidence").ScriptRefusal) as caught:
        _project(projection, answered, dependency_target="measurement")
    assert caught.value.details["reason"] in {"selective_question_grounding_invalid", "selective_question_mirror_invalid"}
