"""Reuse actual observations only inside their original protected interval."""

import copy
from contextlib import nullcontext
from contextvars import copy_context
from types import SimpleNamespace

import pytest

from evidence_wiki import review_page_operations as pages
from evidence_wiki.pack_discovery import owner


def test_held_material_is_detached_and_does_not_survive_scope(monkeypatch):
    context = object()
    monkeypatch.setattr(pages, "current_context", lambda: context)
    reads = []
    monkeypatch.setattr(pages, "current_review_material", lambda *args: reads.append(args) or {"fresh": True})
    prepared, material = {"snapshot": {"claim": "actual"}}, {"claims": ["actual"]}
    with pages._material_scope("root", prepared, material):
        result = pages._held_material("root", prepared)
        result["claims"].append("changed")
        assert pages._held_material("root", prepared) == material
        assert reads == []
    assert pages._held_material("root", prepared) == {"fresh": True}
    assert len(reads) == 1


@pytest.mark.parametrize("exceptional", [False, True])
def test_copied_context_cannot_extend_the_original_material_interval(monkeypatch, exceptional):
    context = object()
    monkeypatch.setattr(pages, "current_context", lambda: context)
    monkeypatch.setattr(pages, "current_review_material", lambda *_: {"fresh": True})
    prepared = {"snapshot": {"claim": "actual"}}
    try:
        with pages._material_scope("root", prepared, {"held": True}):
            captured = copy_context()
            assert captured.run(pages._held_material, "root", prepared) == {"held": True}
            if exceptional:
                raise RuntimeError("leave the owner interval")
    except RuntimeError:
        pass
    assert captured.run(pages._held_material, "root", prepared) == {"fresh": True}


@pytest.mark.parametrize("changed", ["identity", "content", "root", "context", "task", "thread"])
def test_held_material_requires_exact_owner_interval(monkeypatch, changed):
    context = object()
    monkeypatch.setattr(pages, "current_context", lambda: context)
    prepared = {"snapshot": {"claim": "actual"}}
    monkeypatch.setattr(pages, "current_review_material", lambda *_: {"fresh": True})
    with pages._material_scope("root", prepared, {"held": True}):
        selected, root = prepared, "root"
        if changed == "identity":
            selected = copy.deepcopy(prepared)
        elif changed == "content":
            prepared["snapshot"]["claim"] = "changed"
        elif changed == "root":
            root = "other"
        elif changed == "context":
            monkeypatch.setattr(pages, "current_context", object)
        elif changed == "task":
            monkeypatch.setattr(pages, "_material_task", object)
        else:
            monkeypatch.setattr(pages.threading, "get_ident", lambda: -1)
        assert pages._held_material(root, selected) == {"fresh": True}


def test_prepare_reuses_actual_evaluation_and_closes_every_observation(monkeypatch, tmp_path):
    native = owner("_strict_evidence")
    policy = {"schema_version": native.POLICY_SCHEMA_V2, "rubric": {}, "human_review": False}
    state = SimpleNamespace(state_id="state", binding={}, checkpoint="checkpoint")
    view = SimpleNamespace(state=state)
    current = {"material": {"claim_id": "target"}, "snapshot": {"claim": {"id": "target"}},
        "dependency_basis": {"semantic_material_id": "semantic"}}
    other = {"material": {"claim_id": "other"}}
    currents, reviews = [other, current], [object()]
    capture, producer = object(), object()
    result = {"schema_version": native.RESULT_SCHEMA_V3, "basis_id": "basis", "claim_schema_version": "claims",
        "review_producer_id": "producer", "claims": [{"claim": {"id": "target"},
            "claim_basis_id": "claim-basis", "required_review_role": "evaluator"}]}
    monkeypatch.setattr(native, "configuration", lambda *_: {})
    monkeypatch.setattr(native, "resolve_policy", lambda *_: policy)
    monkeypatch.setattr(native, "sibling", lambda name: SimpleNamespace(current_view=lambda *_: nullcontext(view)))
    monkeypatch.setattr(native, "_evaluate_dependency_observations",
        lambda *_args, **_kwargs: (result, (capture, producer, currents, reviews)))
    closed = []
    monkeypatch.setattr(native, "_dependency_closing", lambda *args: closed.append(args))
    def unexpected(*_args, **_kwargs):
        pytest.fail("Preparation repeated dependency projection")
    monkeypatch.setattr(native, "dependency_current", unexpected)
    monkeypatch.setattr(native, "review_snapshot", unexpected)
    prepared = native.prepare_review(tmp_path, "target")
    assert prepared["snapshot"] is current["snapshot"]
    assert prepared["claim_basis_id"] == "claim-basis"
    assert closed[0][-2] is currents
    assert closed[0][-1] is reviews
    assert closed[0][4:7] == (capture, "basis", producer)
