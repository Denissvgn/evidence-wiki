"""Selective transport preserves semantic values and explicit version boundaries."""

from copy import deepcopy

import pytest

from evidence_wiki.agent_execution import ExecutionError
from evidence_wiki.execution_roles import ExecutionRoleRegistry, ExecutionRoleScope
from evidence_wiki.pack_discovery import owner
from evidence_wiki.review_context import _translate_dependency_material
from evidence_wiki.review_delivery import ReviewDelivery
from tests.test_agent_execution import profile
from tests.test_question_dependencies import _manifest


def _transport_case():
    typed = owner("_question_dependencies").typed_node
    manifest = _manifest()
    question = manifest["questions"][0]
    claim = manifest["claims"][0]
    source = {"source_id": "source", "record_sha256": "sha256:" + "a" * 64,
        "metadata": typed({"title": "source", "arbitrary": "measurement"}), "content": "measurement"}
    local = {"sources": [source], "mechanical": [{"claim_id": "measurement", "question_slug": "question",
        "original_id": "original", "premises": [], "evidence": [{"source_id": "source"}]}],
        "mechanical_reasons": [{"claim_id": "measurement", "reasons": []}],
        "counterevidence": {"questions": [{"question_slug": "question", "claim_ids": ["measurement"],
            "scope": typed({"literal": "measurement"}), "criteria": typed([{"facet_id": "facet", "text": "measurement"}]),
            "alternative_sources": []}], "slices": []},
        "completeness": {"scope_qualifiers": {"claim_ids": ["measurement"], "question_slugs": ["question"],
            "source_ids": ["source"], "counter_question_slugs": ["question"]}}}
    material = {"claim_id": "measurement", "claims": [claim], "questions": [{"manifest_question": question,
        "path": "wiki/questions/question.md", "semantic_frontmatter": typed({"metadata": {"literal": "measurement"}}),
        "body": "measurement", "canonical_mirrors": {"grounding": [{"claim": "measurement", "source_id": "source"}],
            "source_ids": ["source"], "answer_page": "question.md", "coverage_manifest": "sources/coverage/question.yml"}}],
        "global_material": {"material": {"global_dependencies": [
            {"kind": "claims_inventory", "coordinate": "claims", "value": {"ids": ["measurement", "unrelated"]}},
            {"kind": "questions_inventory", "coordinate": "questions", "value": {"ids": ["question"]}}],
            "worker_semantics": {"instructions": [{"text": "measurement"}], "completeness": {
                "scope_qualifiers": {"global_dependency_coordinates": []}}}}}, "local_context": local}
    aliases = {"claims": {"measurement": "claim-1", "unrelated": "claim-2"},
        "question": {"question": "question-1"}, "original": {"original": "original-1"},
        "sources": {"source": "source-1"}, "facets": {"question:facet": "facet-1"},
        "paths": {"wiki/questions/question.md": "questions/question-1.md", "question.md": "question-1.md",
            "sources/coverage/question.yml": "coverage/question-1.yml"}}
    return material, aliases


def test_reversible_aliases_never_rewrite_free_semantic_strings_equal_to_ids():
    material, aliases = _transport_case()
    frozen = deepcopy(material)
    visible = _translate_dependency_material(material, aliases)
    assert visible["claim_id"] == "claim-1"
    assert visible["local_context"]["mechanical"][0]["original_id"] == "original-1"
    assert visible["questions"][0]["body"] == "measurement"
    assert visible["questions"][0]["canonical_mirrors"]["grounding"][0]["claim"] == "measurement"
    assert visible["questions"][0]["semantic_frontmatter"] == material["questions"][0]["semantic_frontmatter"]
    assert visible["local_context"]["sources"][0]["metadata"] == material["local_context"]["sources"][0]["metadata"]
    assert _translate_dependency_material(visible, aliases, inverse=True) == frozen == material


def test_full_inventory_alias_collision_and_unmapped_identity_refuse():
    material, aliases = _transport_case()
    aliases["claims"]["unrelated"] = "claim-1"
    with pytest.raises(ExecutionError, match="alias_collision"):
        _translate_dependency_material(material, aliases)
    material, aliases = _transport_case()
    del aliases["claims"]["unrelated"]
    with pytest.raises(ExecutionError, match="alias_missing"):
        _translate_dependency_material(material, aliases)


def test_dependencies_profile_and_delivery_have_explicit_additive_versions():
    scopes = ExecutionRoleRegistry((ExecutionRoleScope("researcher", "researcher", ("model",)),))
    legacy = profile(role_scopes=scopes, review_delivery=ReviewDelivery())
    complete = profile(role_scopes=scopes, review_delivery=ReviewDelivery(context_version="complete"))
    selective = profile(role_scopes=scopes, review_delivery=ReviewDelivery(context_version="dependencies"))
    assert legacy.document()["schema_version"] == "research-execution-profile/v7"
    assert complete.document()["schema_version"] == "research-execution-profile/v14"
    assert selective.document()["schema_version"] == "research-execution-profile/v15"
    assert selective.document()["review_delivery"]["schema_version"] == "research-review-delivery/v3"
    assert selective.document()["review_delivery"]["context_version"] == "research-review-context/v4"
    assert "context_version" not in legacy.document()["review_delivery"]
