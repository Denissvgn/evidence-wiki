"""Batched native observations retain independent instructions and closing checks."""

import copy
from dataclasses import replace

import pytest

from evidence_wiki import coverage_observation as adapter
from evidence_wiki import mutation_operations
from evidence_wiki._pack_io import canonical, json_document
from evidence_wiki.errors import ConfigError, UsageError
from evidence_wiki.host_context import HostContext
from evidence_wiki.mutation_store import capture_revision
from evidence_wiki.pack_discovery import owner
from tests.test_draft_admission import GRANTS, update_policy
from tests.test_draft_admission import draft_root as draft_root
from tests.test_research_planning import question_plan


@pytest.fixture
def batch_case(draft_root):
    root, control, proposal, normalized = draft_root
    path = root / "docs/research-requirements.json"
    frozen = json_document(path.read_bytes())
    derived = {"id": "detail", "text": "Which measured detail was retained?", "original_ids": ["q"]}
    frozen["request"]["payload"]["derived_questions"] = [derived]
    plans = [question_plan(slug) for slug in ("q", "detail")]
    for plan in plans:
        plan["criteria"][0]["required_scope"] = []
    frozen["decisions"] = {"mode": "legacy", "question_plans": plans}
    path.write_bytes(canonical(frozen))
    owner("intake_questions").run_intake_document(root, {"schema_version": "1.0", "questions": [
        {"id": "detail", "question": derived["text"], "origin": "caller", "metadata": {"research_id": "detail",
            "original_ids_json": '["q"]', "original_text": derived["text"]}}]}, dry_run=False, from_file_label="synthetic")
    proposal["inventory"]["questions"].append({"slug": "detail", "original_id": "q", "question": derived["text"]})
    proposal["originals"][0]["question_slugs"].append("detail")
    claim = copy.deepcopy(proposal["inventory"]["claims"][0])
    claim.update(id="detail-observation", question_slug="detail")
    proposal["inventory"]["claims"].append(claim)
    update_policy(root)
    context = HostContext([root], GRANTS | {"coverage_observation.observe"}, "caller", [root], mutation_control_directory=control)
    with context.activate():
        prepared = mutation_operations.prepare_request(root, request_id="admit-batch", operation="strict.admit", arguments={"proposal": proposal})
        receipt = mutation_operations.submit(root, prepared)
    assert receipt["status"] == "committed", receipt
    return root, normalized, context


def test_batch_equals_independent_native_instructions_with_one_full_diagnostic(batch_case, monkeypatch):
    root, _normalized, context = batch_case
    before = capture_revision(root)["revision_id"]
    with context.activate():
        independent = [adapter.observe(root, question_slug=slug) for slug in ("detail", "q")]
        original = adapter.claim_diagnostics.current
        calls = []

        def diagnosed(root, proposal):
            calls.append(copy.deepcopy(proposal))
            return original(root, proposal)

        monkeypatch.setattr(adapter.claim_diagnostics, "current", diagnosed)
        together = adapter.observe_questions(root, question_slugs=["detail", "q"])
    assert together == independent
    assert len(calls) == 1
    assert {row["question_slug"] for row in calls[0]["inventory"]["claims"]} == {"q", "detail"}
    assert capture_revision(root)["revision_id"] == before


@pytest.mark.parametrize("slugs,reason", [(["q", "foreign"], "question_outside_frozen_scope"),
    (["q", "q"], "duplicate_or_invalid_reference"), ([str(i) for i in range(33)], "question_selector_bound")])
def test_batch_validates_entire_question_scope_before_diagnostics(batch_case, monkeypatch, slugs, reason):
    root, _normalized, context = batch_case
    monkeypatch.setattr(adapter.claim_diagnostics, "current", lambda *_: pytest.fail("diagnostics before scope validation"))
    with context.activate(), pytest.raises(UsageError) as refused:
        adapter.observe_questions(root, question_slugs=slugs)
    assert refused.value.details["reason"] == reason


def test_batch_rechecks_private_revision_after_each_instruction_is_prepared(batch_case, monkeypatch):
    root, _normalized, context = batch_case
    original = adapter.prepare
    calls = []

    def prepare(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(kwargs["question_slug"])
        if len(calls) == 2:
            (root / "new-private-input.txt").write_text("changed while observing")
        return result

    monkeypatch.setattr(adapter, "prepare", prepare)
    with context.activate(), pytest.raises(UsageError) as refused:
        adapter.observe_questions(root, question_slugs=["detail", "q"])
    assert calls == ["detail", "q"]
    assert refused.value.details["reason"] == "native_observation_changed"


def test_batch_requires_same_native_grant_and_path_scope(batch_case, tmp_path):
    root, _normalized, context = batch_case
    restricted = replace(context, grants=context.grants - {"coverage_observation.observe"})
    with restricted.activate(), pytest.raises(ConfigError):
        adapter.observe_questions(root, question_slugs=["q", "detail"])
    outside = tmp_path / "outside"
    outside.mkdir()
    with context.activate(), pytest.raises(ConfigError):
        adapter.observe_questions(outside, question_slugs=["q", "detail"])
