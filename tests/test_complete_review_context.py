"""Complete review material binds transitive prerequisites without changing native review."""

import copy

import pytest

from evidence_wiki.agent_execution import ExecutionError, _digest
from evidence_wiki.review_context import (
    COMPLETE_REVIEW_MATERIAL,
    CONTEXT_BINDING_V3,
    REVIEW_CONTEXT_V3,
    ReviewContext,
    create_review_context,
)
from tests.test_calculation_context import owner_case as _owner_case_fixture
from tests.test_calculation_context import projected as _projected_fixture
from tests.test_review_context import inputs

owner_case = _owner_case_fixture
projected = _projected_fixture


def complete_inputs():
    supplied = inputs(1)
    subject = supplied["owner_snapshot"]["claim"]
    for key in ("desired_outcome", "generator"):
        del subject[key]
    ancestor = copy.deepcopy(subject)
    ancestor.update(id="ancestor", qualification="attributed", question_slug="other-question")
    middle = copy.deepcopy(subject)
    middle.update(id="middle", qualification="inference", question_slug="other-question",
                  premises=[ancestor["id"]], derivation="Apply the retained ancestor observation.")
    subject.update(qualification="inference", premises=[middle["id"]], derivation="Combine the prerequisite evidence.")
    supplied["owner_snapshot"]["premises"] = [middle]
    question = supplied["owner_snapshot"]["question"]
    material = {"schema_version": COMPLETE_REVIEW_MATERIAL, "basis_id": supplied["basis_id"],
        "native_snapshot_sha256": "sha256:" + _digest(supplied["owner_snapshot"]), "claim_id": subject["id"],
        "claims": [subject, middle, ancestor], "questions": [
            {**question, "metadata": {"scope": "Exact subject scope"}, "body": "Subject semantic instructions."},
            {"slug": "other-question", "original_id": "other-original", "question": "What observation is retained?",
             "metadata": {"units": ["ratio"], "nested": {"semantic": True}}, "body": "Retain prerequisite counterevidence."}]}
    return supplied, material


def test_complete_material_preserves_graph_semantics_and_native_snapshot_identity():
    supplied, material = complete_inputs()
    context = create_review_context(**supplied, complete_material=material)
    packet, binding = context.document(), context.binding()
    assert packet["schema_version"] == REVIEW_CONTEXT_V3
    assert binding["schema_version"] == CONTEXT_BINDING_V3
    assert binding["owner_snapshot_sha256"] == "sha256:" + _digest(supplied["owner_snapshot"])
    assert binding["complete_material_sha256"] == "sha256:" + _digest(material)
    assert packet["manifest"]["context_complete"]
    assert packet["computation"] is None
    assert packet["claim"]["premises"] == ["claim-2"]
    assert packet["premises"][0]["premises"] == ["claim-3"]
    assert packet["premises"][1]["qualification"] == "attributed"
    assert packet["premises"][1]["kind"] == "assertion"
    for projected_claim, native in zip([packet["claim"], *packet["premises"]], material["claims"], strict=True):
        assert projected_claim["evidence"][0] == {**native["evidence"][0], "source_id": "source-1"}
    aliases = binding["aliases"]
    for row, native in zip(packet["questions"], material["questions"], strict=True):
        assert row == {**native, "slug": aliases["question"][native["slug"]],
                       "original_id": aliases["original"][native["original_id"]]}
    restored = ReviewContext.restore(packet, binding)
    restored.check(supplied["owner_snapshot"], supplied["rubric"], supplied["sources"], complete_material=material)
    with pytest.raises(ExecutionError, match="owner_changed"):
        restored.check(supplied["owner_snapshot"], supplied["rubric"], supplied["sources"])


@pytest.mark.parametrize("mutate", [
    lambda material: material["claims"].pop(),
    lambda material: material["claims"].append(copy.deepcopy(material["claims"][-1])),
    lambda material: material["claims"][-1].update(premises=[material["claim_id"]], qualification="inference", derivation="Cycle."),
    lambda material: material["claims"].append({**material["claims"][-1], "id": "outside"}),
    lambda material: material["questions"].pop(),
    lambda material: material["questions"].append(copy.deepcopy(material["questions"][-1])),
    lambda material: material["questions"][0].update(lifecycle="answered"),
    lambda material: material["claims"][-1].update(qualification="unknown"),
    lambda material: material["claims"][-1]["evidence"][0].update(extra="hidden"),
    lambda material: material.update(native_snapshot_sha256="sha256:" + "0" * 64),
    lambda material: material.update(basis_id="sha256:" + "0" * 64),
    lambda material: material.update(extra="hidden"),
])
def test_complete_material_refuses_missing_extra_duplicate_or_unbound_context(mutate):
    supplied, material = complete_inputs()
    material = copy.deepcopy(material)
    mutate(material)
    with pytest.raises(ExecutionError):
        create_review_context(**supplied, complete_material=material)


def test_rehashed_semantic_forgery_does_not_rebind_current_complete_material():
    supplied, material = complete_inputs()
    context = create_review_context(**supplied, complete_material=material)
    packet, binding = context.document(), context.binding()
    packet["questions"][1]["body"] = "Forged prerequisite instructions."
    binding["packet_sha256"] = "sha256:" + _digest(packet)
    forged = ReviewContext.restore(packet, binding)
    with pytest.raises(ExecutionError, match="owner_changed"):
        forged.check(supplied["owner_snapshot"], supplied["rubric"], supplied["sources"], complete_material=material)
    context.check_owner(owner_snapshot=supplied["owner_snapshot"], basis_id=supplied["basis_id"])
    with pytest.raises(ExecutionError, match="owner_changed"):
        context.check_owner(owner_snapshot=supplied["owner_snapshot"], basis_id="sha256:" + "e" * 64)


def test_rehashed_packet_cannot_drop_qualification_or_question_coverage():
    supplied, material = complete_inputs()
    context = create_review_context(**supplied, complete_material=material)
    for change in (lambda packet: packet["premises"][-1].pop("qualification"),
                   lambda packet: packet["questions"].pop(),
                   lambda packet: packet["premises"][-1]["premises"].append("claim-1")):
        packet, binding = context.document(), context.binding()
        change(packet)
        binding["packet_sha256"] = "sha256:" + _digest(packet)
        with pytest.raises(ExecutionError):
            ReviewContext.restore(packet, binding)


@pytest.mark.parametrize("target,invalid", [
    ("claim", []), ("claim", None), ("claim", "invalid"),
    ("premise", []), ("premise", None), ("premise", "invalid"),
    ("evidence", None), ("evidence", {}), ("evidence", 1), ("evidence", "invalid"),
    ("edges", None), ("edges", {}), ("edges", [None]), ("edges", [[]]),
    ("qualification", []),
])
def test_rehashed_malformed_claim_containers_raise_typed_refusal(target, invalid):
    supplied, material = complete_inputs()
    context = create_review_context(**supplied, complete_material=material)
    packet, binding = context.document(), context.binding()
    if target == "claim":
        packet["claim"] = invalid
    elif target == "premise":
        packet["premises"][0] = invalid
    else:
        field = "premises" if target == "edges" else target
        packet["premises"][0][field] = invalid
    binding["packet_sha256"] = "sha256:" + _digest(packet)
    with pytest.raises(ExecutionError, match="review_context_claim_invalid"):
        ReviewContext.restore(packet, binding)


def test_complete_material_claim_bound_refuses_instead_of_truncating():
    supplied, material = complete_inputs()
    for index in range(30):
        material["claims"].append({**material["claims"][-1], "id": f"additional-{index}"})
    assert len(material["claims"]) == 33
    with pytest.raises(ExecutionError, match="material_invalid"):
        create_review_context(**supplied, complete_material=material)


def test_legacy_incomplete_cross_question_transitive_packet_is_unchanged():
    supplied, _material = complete_inputs()
    packet = create_review_context(**supplied).document()
    assert packet["schema_version"] == "research-review-context/v1"
    assert set(packet["manifest"]["limitations"]) == {"premise_context_missing", "premise_question_context_missing"}
    assert "questions" not in packet and "qualification" not in packet["claim"]


def complete_computed_context(projected):
    _review, _context, snapshot, rubric, sources = projected
    basis = "sha256:" + "a" * 64
    material = {"schema_version": COMPLETE_REVIEW_MATERIAL, "basis_id": basis,
        "native_snapshot_sha256": "sha256:" + _digest(snapshot), "claim_id": snapshot["claim"]["id"],
        "claims": [snapshot["claim"], *snapshot["premises"]],
        "questions": [{**snapshot["question"], "metadata": None, "body": ""}]}
    context = create_review_context(context_id="complete-calculation", task_role="evaluator", basis_id=basis,
        owner_snapshot=snapshot, rubric=rubric, sources=sources, order_seed=b"c" * 32, complete_material=material)
    return context, material


def test_computation_remains_qualified_independently_of_complete_context_version(projected):
    _review, _context, snapshot, rubric, sources = projected
    context, material = complete_computed_context(projected)
    assert context.document()["schema_version"] == REVIEW_CONTEXT_V3
    assert context.document()["manifest"]["context_complete"]
    assert context.document()["claim"]["calculations"] == snapshot["claim"]["calculations"]
    assert context.document()["computation"] is not None
    context.check(snapshot, rubric, sources, complete_material=material)


@pytest.mark.parametrize("target,invalid", [
    ("claim_missing", None), ("premise_missing", None),
    ("calculations", None), ("calculations", {}), ("calculations", [None]), ("calculations", [[]]),
    ("form", []), ("pointer", []), ("expected", {}), ("result_id", {}),
])
def test_rehashed_computed_packet_refuses_missing_or_malformed_calculations(projected, target, invalid):
    context, _material = complete_computed_context(projected)
    packet, binding = context.document(), context.binding()
    if target == "claim_missing":
        del packet["claim"]["calculations"]
    elif target == "premise_missing":
        del packet["premises"][0]["calculations"]
    elif target == "calculations":
        packet["claim"]["calculations"] = invalid
    else:
        packet["claim"]["calculations"][0][target] = invalid
    binding["packet_sha256"] = "sha256:" + _digest(packet)
    with pytest.raises(ExecutionError):
        ReviewContext.restore(packet, binding)
