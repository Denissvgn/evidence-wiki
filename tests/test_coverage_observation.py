"""Real canonical admission and current owner reads stay mechanically qualified."""

from dataclasses import replace

import pytest
import yaml

from evidence_wiki import mutation_operations
from evidence_wiki._pack_io import canonical, json_document
from evidence_wiki.errors import UsageError
from evidence_wiki.host_context import HostContext
from evidence_wiki.mutation_store import capture_revision
from evidence_wiki.pack_discovery import owner
from tests._execution_fixture import authenticate, closure, identifier
from tests._usage_fixture import UsageFixture
from tests.test_draft_admission import GRANTS, update_policy
from tests.test_draft_admission import draft_root as draft_root
from tests.test_research_planning import question_plan


@pytest.fixture
def native_case(draft_root, monkeypatch):
    from evidence_wiki import coverage_observation as adapter
    root, control, proposal, normalized = draft_root
    path = root / "docs/research-requirements.json"
    declaration = json_document(path.read_bytes())
    plan = question_plan("q")
    plan["criteria"][0]["required_scope"] = []
    declaration["decisions"] = {"mode": "legacy", "question_plans": [plan]}
    path.write_bytes(canonical(declaration))
    update_policy(root)
    context = HostContext([root], GRANTS | {"coverage_observation.observe"}, "caller", [root], mutation_control_directory=control)
    with context.activate():
        prepared = mutation_operations.prepare_request(root, request_id="admit-for-coverage", operation="strict.admit", arguments={"proposal": proposal})
        receipt = mutation_operations.submit(root, prepared)
    assert receipt["status"] == "committed", receipt
    return root, normalized, context, adapter


def test_actual_native_admitted_anchor_observation_has_no_effect(native_case):
    root, _normalized, context, adapter = native_case
    before = capture_revision(root)["revision_id"]
    with context.activate():
        result = adapter.observe(root, question_slug="q")
    assert result["anchors"]
    assert result["claims"][0]["qualification"] == "supported"
    assert not result["accepted"] and result["semantic_approval"] == "not_evaluated"
    assert result["owner_observation"]["permission_basis"] == "legacy_compatibility"
    assert capture_revision(root)["revision_id"] == before
    from evidence_wiki import coverage_examination as codec
    assert codec.check_instruction(result) == result


def test_foreign_question_is_not_a_caller_prose_scope(native_case):
    root, _normalized, context, adapter = native_case
    with context.activate(), pytest.raises(UsageError) as refused:
        adapter.observe(root, question_slug="unknown")
    assert refused.value.details["reason"] == "question_outside_frozen_scope"


def test_changed_normalized_record_does_not_retain_verified_anchor(native_case):
    root, normalized, context, adapter = native_case
    normalized.write_bytes(normalized.read_bytes().replace(b"0.74", b"0.60"))
    before = capture_revision(root)["revision_id"]
    with context.activate():
        try:
            result = adapter.observe(root, question_slug="q")
        except UsageError as refused:
            assert refused.details["reason"] == "source_revision_changed"
        else:
            assert not result["anchors"] and not result["accepted"]
    assert capture_revision(root)["revision_id"] == before


@pytest.mark.parametrize("retrieval", [True, False])
def test_current_signed_source_permission_controls_context(native_case, tmp_path, monkeypatch, retrieval):
    root, normalized, original_context, adapter = native_case
    directory = tmp_path / "permissions"
    directory.mkdir()
    authority = UsageFixture(directory, monkeypatch)
    authority.root = root
    authority.binding = identifier("evidence-host-workspace/v1", {"root": str(root)})
    config = yaml.safe_load((root / "research.yml").read_bytes())
    config.update(authority.config)
    authority.config = config
    (root / "research.yml").write_text(yaml.safe_dump(config, sort_keys=False))
    claims = json_document((root / config["strict_evidence"]["claims_path"]).read_bytes())
    source_id = claims["claims"][0]["evidence"][0]["source_id"]
    body, _files = authority.source(source_id, normalized=normalized.read_bytes(), retrieval=retrieval)
    descriptor = {"schema_version": "evidence-source-revision/v1", "source_id": source_id, "parents": [],
        "normalized_path": normalized.relative_to(root).as_posix(), "evidence_root": None, "temporal": {}}
    files = {"source-record.json": canonical(descriptor), descriptor["normalized_path"]: normalized.read_bytes()}
    revision = closure(files)
    body["source_revision"] = revision
    body["grant"] = authenticate({**body["grant"]["payload"], "source_revision": revision}, "owner", "usage")
    body["scrub"] = authenticate({**body["scrub"]["payload"], "sanitized_revision": revision}, "runner", "scrubber")
    usage = owner("_evidence_usage")
    authority.transact(usage, "initialize")
    authority.transact(usage, "deposit", body, files)
    selected = replace(original_context, authority_file=authority.policy_path, state_directory=authority.host)
    before = capture_revision(root)["revision_id"]
    checkpoint = (authority.host / "evidence-state.json").read_bytes()
    with selected.activate():
        result = adapter.observe(root, question_slug="q")
    assert result["owner_observation"]["permission_basis"] == "native_current_usage"
    if retrieval:
        assert result["anchors"] and result["source_contexts"]
    else:
        assert not result["anchors"] and not result["source_contexts"]
        assert "evidence" not in result["claims"][0] and "text" not in result["claims"][0]
    assert not result["accepted"]
    assert capture_revision(root)["revision_id"] == before
    assert (authority.host / "evidence-state.json").read_bytes() == checkpoint


def test_actual_bounded_empty_search_is_unknown_absence(native_case, monkeypatch):
    root, _normalized, context, adapter = native_case
    from evidence_wiki import counter_search as search
    selected = replace(context, grants=context.grants | {"retrieval_operations.search"})
    with selected.activate():
        instruction = adapter.observe(root, question_slug="q")
        sources = [source["source_id"] for source in instruction["source_contexts"]]
        before = capture_revision(root)["revision_id"]
        value = search.observation(root, question_slug="q", query="zz_no_retained_matching_measurement", source_ids=sources,
            exclusions=["Only selected retained local originals"], expected_instruction_basis=instruction["basis_id"], limit=1)
    assert value["search_execution"] == "observed" and value["absence_conclusion"] == "unknown"
    assert value["report"]["results"] == [] and value["report"]["coverage"]["selected"] == 1
    assert not value["provider_calls"] and not value["index_writes"] and not value["accepted"]
    assert capture_revision(root)["revision_id"] == before
