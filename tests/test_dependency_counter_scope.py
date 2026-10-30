"""Question reads expire with their owning native evaluation."""

from contextvars import copy_context
from types import SimpleNamespace

import pytest

from evidence_wiki import _review_dependency_inputs as native
from evidence_wiki.errors import EvidenceWikiError
from tests.test_coverage_examination import prepared as prepared


def native_evaluation(holder, args):
    return holder.counter_scope(*args)


@pytest.fixture(autouse=True)
def evaluator_owner(monkeypatch):
    from evidence_wiki import pack_discovery
    original = pack_discovery.owner
    def owner(name):
        if name == "_strict_evidence":
            return SimpleNamespace(_evaluate_dependency_observations=native_evaluation)
        return original(name)
    monkeypatch.setattr(pack_discovery, "owner", owner)


@pytest.fixture
def scope_inputs(monkeypatch, tmp_path):
    holder = object.__new__(native._Inputs)
    holder._root = tmp_path
    checks = []
    holder._scope = lambda *args: checks.append(args)
    config = {"configured": True}
    capture = SimpleNamespace(files={"research.yml": b"frozen"})
    manifest = {"claims": [{"id": "a", "question_slug": "first"}, {"id": "b", "question_slug": "second"}]}
    view = SimpleNamespace(state=SimpleNamespace(checkpoint="current", revisions={}), trust={"hash": "trusted"})
    sources = [{"source_id": "source", "record_sha256": "hash", "content": "body"}]
    calls = []

    def observe(root, *, question_slugs, source_ids):
        calls.append((root, list(question_slugs), list(source_ids)))
        return [{"question_slug": slug, "nested": {"text": "original"}} for slug in question_slugs]

    monkeypatch.setattr("evidence_wiki.coverage_observation.observe_questions", observe)
    return holder, (tmp_path, config, capture, manifest, view), sources, calls, checks


def test_scope_batches_native_questions_and_returns_independent_copies(scope_inputs):
    holder, args, sources, calls, checks = scope_inputs
    with native_evaluation(holder, args):
        scope = native._COUNTERS.get()
        first = scope.instructions(holder, *args, sources, ["first"])
        first[0]["nested"]["text"] = "caller mutation"
        second = scope.instructions(holder, *args, sources, ["second", "first"])
        assert [row["question_slug"] for row in second] == ["second", "first"]
        assert second[1]["nested"]["text"] == "original"
    assert len(calls) == 1
    assert calls[0][1:] == (["first", "second"], ["source"])
    assert len(checks) >= 5
    assert native._COUNTERS.get() is None
    with pytest.raises(EvidenceWikiError):
        scope.instructions(holder, *args, sources, ["first"])
    with native_evaluation(holder, args):
        native._COUNTERS.get().instructions(holder, *args, sources, ["first"])
    assert len(calls) == 2


@pytest.mark.parametrize("target", ["configuration", "capture", "manifest", "state", "trust", "sources"])
def test_mutated_inputs_invalidate_scope(scope_inputs, target):
    holder, args, sources, calls, _ = scope_inputs
    with pytest.raises(EvidenceWikiError), native_evaluation(holder, args):
        scope = native._COUNTERS.get()
        scope.instructions(holder, *args, sources, ["first"])
        if target == "configuration":
            args[1]["configured"] = False
        elif target == "capture":
            args[2].files["research.yml"] = b"changed"
        elif target == "manifest":
            args[3]["claims"][0]["id"] = "changed"
        elif target == "state":
            args[4].state.revisions["changed"] = {}
        elif target == "trust":
            args[4].trust["hash"] = "changed"
        else:
            sources[0]["content"] = "changed"
        scope.instructions(holder, *args, sources, ["first"])
    assert len(calls) == 1


def test_copied_context_cannot_reuse_scope(scope_inputs):
    holder, args, sources, calls, _ = scope_inputs
    with pytest.raises(EvidenceWikiError), native_evaluation(holder, args):
        scope = native._COUNTERS.get()
        scope.instructions(holder, *args, sources, ["first"])
        with pytest.raises(ValueError):
            copy_context().run(scope.instructions, holder, *args, sources, ["first"])
        scope.instructions(holder, *args, sources, ["first"])
    assert len(calls) == 1


def test_closed_context_copy_cannot_reuse_scope(scope_inputs):
    holder, args, sources, calls, _ = scope_inputs
    with native_evaluation(holder, args):
        scope = native._COUNTERS.get()
        scope.instructions(holder, *args, sources, ["first"])
        copied = copy_context()
    with pytest.raises(EvidenceWikiError):
        copied.run(scope.instructions, holder, *args, sources, ["first"])
    assert len(calls) == 1


def test_projection_reuses_raw_reads_but_keeps_closure_primary_and_claim_checks(scope_inputs, monkeypatch):
    holder, args, sources, calls, _ = scope_inputs
    root, config, capture, manifest, view = args
    sources[:] = [{"source_id": sid, "record_sha256": "hash", "content": sid, "metadata": {}}
        for sid in ("one", "two")]
    manifest["claims"] = [{"id": identifier, "question_slug": "first", "evidence": [{"source_id": sid}]}
        for identifier, sid in (("a", "one"), ("b", "two"))]
    holder.source_documents = lambda *unused: sources
    monkeypatch.setattr("evidence_wiki.coverage_examination.check_instruction", lambda instruction: instruction)
    monkeypatch.setattr("evidence_wiki.review_context._metadata", lambda metadata: metadata)

    def observe(root, *, question_slugs, source_ids):
        calls.append((root, list(question_slugs), list(source_ids)))
        return [{**dict.fromkeys(("schema_version", "question_id", "original_ids", "facets", "anchors",
                    "semantic_approval", "accepted", "write_authority", "basis_id", "owner_observation")),
            "question_slug": slug, "criteria": {"text": "criterion"}, "scope": {"text": "scope"},
            "claims": manifest["claims"], "source_contexts": [{"source_id": row["source_id"],
                "record_sha256": row["record_sha256"], "text": row["content"]} for row in sources]}
            for slug in question_slugs]

    monkeypatch.setattr("evidence_wiki.coverage_observation.observe_questions", observe)

    def project(identifier):
        return holder.counter_projection(root, config, capture, manifest, [identifier], ["first"], view)

    expected = [project("a"), project("b")]
    assert len(calls) == 2
    with native_evaluation(holder, args):
        actual = [project("a"), project("b")]
        assert actual == expected
        assert actual[0]["questions"][0]["alternative_sources"][0]["source_id"] == "two"
        assert actual[1]["questions"][0]["alternative_sources"][0]["source_id"] == "one"
        actual[0]["questions"][0]["criteria"]["text"] = "changed"
        assert project("a") == expected[0]
    assert len(calls) == 3
    assert project("a") == expected[0]
    assert len(calls) == 4


@pytest.mark.parametrize("escape", ["thread", "task"])
def test_scope_rejects_another_thread_or_task(scope_inputs, escape):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    holder, args, sources, calls, _ = scope_inputs
    with pytest.raises(EvidenceWikiError), native_evaluation(holder, args):
        scope = native._COUNTERS.get()
        scope.instructions(holder, *args, sources, ["first"])
        copied = copy_context()
        if escape == "thread":
            with ThreadPoolExecutor(max_workers=1) as executor:
                with pytest.raises(EvidenceWikiError):
                    executor.submit(copied.run, scope.instructions, holder, *args, sources, ["first"]).result()
        else:
            async def consume():
                scope.instructions(holder, *args, sources, ["first"])

            with pytest.raises(EvidenceWikiError):
                asyncio.run(consume())
        scope.instructions(holder, *args, sources, ["first"])
    assert len(calls) == 1


def test_unrelated_stale_question_does_not_change_requested_instruction(scope_inputs, prepared, monkeypatch):
    from copy import deepcopy

    from evidence_wiki import coverage_examination as codec

    holder, args, sources, calls, _ = scope_inputs
    frozen, original, _ = prepared
    inventory = deepcopy(original["admitted_inventory"])
    other_question = deepcopy(inventory["questions"][0])
    other_question["slug"] = "second"
    inventory["questions"].append(other_question)
    other_claim = deepcopy(inventory["claims"][0])
    other_claim.update(id="other", question_slug="second")
    inventory["claims"].append(other_claim)
    args[3].update(inventory)

    def observe(root, *, question_slugs, source_ids):
        calls.append(list(question_slugs))
        results = []
        for slug in question_slugs:
            observations = deepcopy(original["anchor_observations"])
            if slug == "second":
                observations[0].update(claim_id="other", record_sha256="sha256:" + "0" * 64)
            results.append(codec.prepare(frozen, **{**original, "question_slug": slug,
                "admitted_inventory": inventory, "anchor_observations": observations}))
        return results

    monkeypatch.setattr("evidence_wiki.coverage_observation.observe_questions", observe)
    expected = observe(args[0], question_slugs=["q1"], source_ids=[])
    calls.clear()
    with native_evaluation(holder, args):
        scope = native._COUNTERS.get()
        assert scope.instructions(holder, *args, sources, ["q1"]) == expected
        assert scope.instructions(holder, *args, sources, ["q1"]) == expected
        with pytest.raises(EvidenceWikiError) as refused:
            scope.instructions(holder, *args, sources, ["second"])
        assert refused.value.details["reason"] == "source_revision_changed"
        assert scope.instructions(holder, *args, sources, ["q1"]) == expected
    assert calls == [["q1", "second"], ["q1"], ["second"]]


@pytest.mark.parametrize("failure", [KeyboardInterrupt(), SystemExit(), RuntimeError("owner bug")])
def test_prefetch_does_not_retry_interruptions_or_owner_bugs(scope_inputs, monkeypatch, failure):
    holder, args, sources, calls, _ = scope_inputs

    def observe(*unused, **kwargs):
        calls.append(kwargs["question_slugs"])
        raise failure

    monkeypatch.setattr("evidence_wiki.coverage_observation.observe_questions", observe)
    with native_evaluation(holder, args):
        with pytest.raises(type(failure)):
            native._COUNTERS.get().instructions(holder, *args, sources, ["first"])
    assert len(calls) == 1


def test_prefetch_failure_cannot_hide_changed_scope(scope_inputs, monkeypatch):
    from evidence_wiki.coverage_examination import fail

    holder, args, sources, calls, _ = scope_inputs

    def observe(*unused, **kwargs):
        calls.append(kwargs["question_slugs"])
        args[4].state.revisions["changed"] = {}
        fail("source_revision_changed")

    monkeypatch.setattr("evidence_wiki.coverage_observation.observe_questions", observe)
    with pytest.raises(EvidenceWikiError), native_evaluation(holder, args):
        native._COUNTERS.get().instructions(holder, *args, sources, ["first"])
    assert len(calls) == 1
