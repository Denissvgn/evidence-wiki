"""Versioned native calculation inputs stay exact in blinded evaluator packets."""

import copy

import pytest

from evidence_wiki.agent_execution import ExecutionError
from evidence_wiki.calculation_context import check_projection, project
from evidence_wiki.errors import UsageError
from evidence_wiki.mutation_contracts import digest
from tests import test_calculation_contracts as cases


@pytest.fixture
def owner_case(tmp_path):
    return cases.owner_case.__wrapped__(tmp_path)


@pytest.fixture
def projected(owner_case):
    root, _declared, result, _proposal = owner_case
    from evidence_wiki import review_context as review
    source = result["sources"][0]
    reference = {"result_id":result["result_id"], "pointer":"/graphs/worksheet/outputs/total", "expected":"6",
        "form":"value", "unit":"units", "rounded":False}
    base = {"id":"source-observation", "question_slug":"q", "qualification":"supported", "text":"Retained input observation.",
        "scope":"Declared scope", "time":"Declared input time", "units":"units", "premises":[], "derivation":None,
        "limitations":[], "calculations":[], "evidence":[{"source_id":source["source_id"], "record_sha256":source["record_sha256"],
            "observed_at":"2026-09-21T12:00:00Z", "capture":"primary", "quote":"Retained local data.", "location_hint":None, "anchor":None}]}
    claim = {**base, "id":"computed", "qualification":"inference", "text":"Declared computation returns 6 units.",
        "premises":[base["id"]], "derivation":"Use the explicit configured multiplication.", "calculations":[reference]}
    snapshot = {"claim":claim, "question":{"slug":"q", "original_id":"q", "question":"What value was calculated?"},
        "premises":[base], "source_revisions":{source["source_id"]:source["record_sha256"]}, "computation":result,
        "metadata":{"generator_identity":"private-generator"}, "policy":{"key":"private-key"}}
    rubric = {"id":"native", "revision":"1", "criteria":dict.fromkeys(
        ("support", "source_suitability", "scope", "time", "units", "counterevidence"), "Current independent evidence required.")}
    raw = (root / source["record_path"]).read_text()
    sources = [{"source_id":source["source_id"], "record_sha256":source["record_sha256"], "content":raw, "metadata":{}}]
    context = review.create_review_context(context_id="computed-context", task_role="evaluator", basis_id=digest(snapshot),
        owner_snapshot=snapshot, rubric=rubric, sources=sources, order_seed=b"c" * 32)
    return review, context, snapshot, rubric, sources


def test_native_computation_and_refs_are_complete_versioned_and_blinded(projected):
    review, context, snapshot, rubric, sources = projected
    packet = context.document()
    assert packet["schema_version"] == review.REVIEW_CONTEXT_V2
    assert context.binding()["schema_version"] == review.CONTEXT_BINDING_V2
    assert packet["manifest"]["context_complete"]
    assert packet["claim"]["calculations"][0]["expected"] == "6"
    assert packet["computation"]["graphs"]["worksheet"]["outputs"]["total"]["value"] == "6"
    assert packet["computation"]["sources"][0]["source_id"] == "source-1"
    assert "private-key" not in str(packet) and "private-generator" not in str(packet)
    context.check(snapshot, rubric, sources)


def test_changed_native_value_or_rounding_cannot_rebind_packet(projected):
    _review, context, snapshot, rubric, sources = projected
    altered = copy.deepcopy(snapshot)
    altered["claim"]["calculations"][0]["rounded"] = True
    with pytest.raises((ExecutionError, ValueError, UsageError)):
        context.check(altered, rubric, sources)


def test_plain_context_v1_remains_unchanged_and_computation_projection_has_no_authority(projected):
    review, _context, snapshot, rubric, sources = projected
    del snapshot["computation"]
    for claim in [snapshot["claim"], *snapshot["premises"]]:
        del claim["calculations"]
    context = review.create_review_context(context_id="plain-context", task_role="evaluator", basis_id=digest(snapshot),
        owner_snapshot=snapshot, rubric=rubric, sources=sources, order_seed=b"c" * 32)
    assert context.document()["schema_version"] == review.REVIEW_CONTEXT
    assert context.document()["computation"] is None
    assert "calculations" not in context.document()["claim"]


def test_pure_projection_change_is_rejected_against_native_owner(owner_case):
    _root, _declared, result, _proposal = owner_case
    aliases = {row["source_id"]:"source-1" for row in result["sources"]}
    context = project(result, aliases)
    context["graphs"]["worksheet"]["outputs"]["total"]["value"] = "600"
    with pytest.raises(UsageError):
        check_projection(context, result, aliases)


def test_plain_packet_and_parent_binding_preserve_frozen_wire_digest():
    from evidence_wiki.review_context import create_review_context
    from tests.test_review_context import inputs
    context = create_review_context(**inputs())
    assert digest(context.document()) == "sha256:e789dff74487b9d6b3e5b5fa8414f3caa148bdc13ed537a6e124b22967e904f1"
    assert digest(context.binding()) == "sha256:e2bad7576e1963341bb8edc00468e33b4429b73a7cc1ee990b3834aff24c2504"


@pytest.mark.parametrize("change", [
    lambda packet: packet["sources"][0].update(record_sha256="invalid"),
    lambda packet: packet["sources"][0].update(structured_sha256=[]),
    lambda packet: packet["sources"][0].update(source_revision="invalid"),
    lambda packet: packet["clock"].update(schedules="hidden"),
    lambda packet: packet["definition"]["arithmetic"].update(precision=True),
    lambda packet: packet["graphs"]["worksheet"]["outputs"]["total"].update(rounded="false"),
    lambda packet: packet.update(invariants=[{"passed":True}]),
    lambda packet: packet.update(lineage={"unknown":{"kind":"constant", "definition_id":"invalid", "pointer":"/x"}}),
    lambda packet: packet["graphs"]["worksheet"]["outputs"]["total"].update(lineage=["unknown"]),
])
def test_inert_projection_rejects_malformed_decisive_fields(owner_case, change):
    from evidence_wiki.calculation_context import validate_context
    result = owner_case[2]
    packet = project(result, {row["source_id"]:"source-1" for row in result["sources"]})
    validate_context(packet)
    change(packet)
    with pytest.raises(UsageError) as refused:
        validate_context(packet)
    assert refused.value.details["reason"] == "calculation_context_invalid"


def test_legacy_job_and_next_versions_preserve_frozen_closed_shapes():
    from evidence_wiki.research_session_contracts import schema_document as next_schema
    from evidence_wiki.workflow_contracts import schema_document as job_schema
    assert digest(job_schema("research-host-job/v3")) == "sha256:416284eb9459f267b07f7e4a9b8e3cdfb7c9025e256c4e40c693a418cf334564"
    assert digest(next_schema("research-session-next/v3")) == "sha256:31c5c9e7648da0f1231ee786ab58b26fa77fe09c55ed61ecd99348ba54f26302"


@pytest.mark.parametrize("field,invalid", [("form", []), ("pointer", {}), ("expected", 6), ("unit", None)])
def test_malformed_calculation_reference_is_a_typed_refusal(projected, field, invalid):
    from evidence_wiki.calculation_context import validate_references
    packet = projected[1].document()
    reference = packet["claim"]["calculations"][0]
    reference[field] = invalid
    with pytest.raises(UsageError) as refused:
        validate_references([reference], packet["computation"])
    assert refused.value.details["reason"] == "calculation_reference_invalid"
