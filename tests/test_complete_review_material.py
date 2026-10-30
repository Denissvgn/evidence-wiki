"""Native complete material requires the current full basis and exact review snapshot."""

from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from evidence_wiki.agent_execution import _digest
from tests.test_complete_review_context import complete_inputs
from tests.test_strict_evidence import CORE


@pytest.fixture
def native_material(tmp_path, monkeypatch):
    supplied, material = complete_inputs()
    snapshot = supplied["owner_snapshot"]
    policy, config = {"schema_version": "evidence-strict-policy/v1", "policy": "current"}, {"configuration": "current"}
    files = {}
    for question in material["questions"]:
        files[f"wiki/questions/{question['slug']}.md"] = (
            "---\nquestion: Editable page title\nstatus: answered\nmetadata:\n  scientific_scope: exact\n---\n"
            + question["body"]).encode()
    capture = SimpleNamespace(files=files)
    manifest = {"claims": list(reversed(material["claims"])),
                "questions": [{key: row[key] for key in ("slug", "original_id", "question")} for row in material["questions"]]}
    checked = []
    view = SimpleNamespace(revalidate=lambda root, selected: checked.append((root, selected)))
    original_sibling = CORE.sibling

    def sibling(name):
        if name == "_evidence_usage":
            return SimpleNamespace(current_view=lambda root, selected: nullcontext(view))
        if name == "question_status":
            return SimpleNamespace(questions_directory=lambda root, selected: root / "wiki/questions")
        return original_sibling(name)

    monkeypatch.setattr(CORE, "sibling", sibling)
    monkeypatch.setattr(CORE, "configuration", lambda root: config)
    monkeypatch.setattr(CORE, "resolve_policy", lambda root, selected: policy)
    monkeypatch.setattr(CORE, "selection_inputs", lambda *args, **kwargs: (capture, manifest, supplied["basis_id"]))
    monkeypatch.setattr(CORE, "review_snapshot", lambda *args, **kwargs: snapshot)
    return SimpleNamespace(root=tmp_path, supplied=supplied, material=material, snapshot=snapshot,
        manifest=manifest, checked=checked, files=files)


def prepare(case, **changes):
    expected = {"expected_basis_id": case.supplied["basis_id"],
                "expected_native_snapshot_sha256": "sha256:" + _digest(case.snapshot)}
    return CORE.prepare_review_material(case.root, case.material["claim_id"], **(expected | changes))


def test_native_material_matches_compact_snapshot_hash_and_semantic_question_rows(native_material):
    case = native_material
    material = prepare(case)
    assert material["claims"] == case.material["claims"]
    assert material["native_snapshot_sha256"] == "sha256:" + _digest(case.snapshot)
    assert case.checked
    assert {row["slug"] for row in material["questions"]} == {row["slug"] for row in case.material["questions"]}
    for row in material["questions"]:
        assert set(row) == {"slug", "original_id", "question", "metadata", "body"}
        assert row["metadata"] == {"scientific_scope": "exact"}
        assert row["question"] != "Editable page title"
    before = material
    for path, raw in case.files.items():
        case.files[path] = raw.replace(b"status: answered", b"status: blocked")
    assert prepare(case) == before


@pytest.mark.parametrize("field", ["expected_basis_id", "expected_native_snapshot_sha256"])
def test_native_material_refuses_changed_basis_or_snapshot(native_material, field):
    with pytest.raises(Exception) as refused:
        prepare(native_material, **{field: "sha256:" + "0" * 64})
    assert refused.value.details["reason"] in {"strict_review_basis_changed", "strict_review_snapshot_changed"}
    assert not native_material.checked


def test_native_material_refuses_missing_or_cyclic_ancestor(native_material):
    case = native_material
    ancestor = case.manifest["claims"].pop(0)
    with pytest.raises(Exception) as refused:
        prepare(case)
    assert refused.value.details["reason"] == "strict_premise_missing_or_cyclic"
    case.manifest["claims"].append(ancestor)
    ancestor["premises"] = [case.material["claim_id"]]
    with pytest.raises(Exception) as refused:
        prepare(case)
    assert refused.value.details["reason"] == "strict_premise_missing_or_cyclic"
