"""Complete delivery preserves cross-question dependencies under explicit bounds."""

import copy
from dataclasses import replace

import pytest

from evidence_wiki.agent_execution import ExecutionError, ExecutionRole, _digest
from evidence_wiki.errors import UsageError
from evidence_wiki.execution_roles import ExecutionRoleRegistry, ExecutionRoleScope
from evidence_wiki.mutation_contracts import digest
from evidence_wiki.onboarding_contract import _matches
from evidence_wiki.packet_pages import create as create_pages
from evidence_wiki.review_context import create_review_context
from evidence_wiki.review_delivery import ReviewDelivery
from evidence_wiki.review_packet_contracts import schemas, validate
from evidence_wiki.review_packet_sections import create as create_sections
from evidence_wiki.review_page_operations import snapshot
from evidence_wiki.workflow_contracts import JOB_V3, JOB_V4
from tests.test_agent_execution import profile
from tests.test_research_planning import question_plan
from tests.test_review_context import inputs
from tests.test_review_packet_sections import case as section_case


@pytest.fixture
def complete_case():
    values = inputs(1)
    snapshot = values["owner_snapshot"]
    subject = snapshot["claim"]
    for name in ("desired_outcome", "generator"):
        subject.pop(name)
    middle, leaf = copy.deepcopy(subject), copy.deepcopy(subject)
    middle.update(id="middle-claim", question_slug="other-question", qualification="inference",
        premises=["leaf-claim"], derivation="Combine the retained observations.")
    leaf.update(id="leaf-claim", question_slug="other-question")
    subject.update(qualification="inference", premises=[middle["id"]], derivation="Apply the measured relationship.")
    snapshot["premises"] = [middle]
    questions = [{**snapshot["question"], "metadata": {"measurement": "reflectance"}, "body": "Full measured scope."},
        {"slug": "other-question", "original_id": "other-original", "question": "How was the value measured?",
            "metadata": None, "body": "Retain the original measurement conditions."}]
    material = {"schema_version": "research-complete-review-material/v1", "basis_id": values["basis_id"],
        "native_snapshot_sha256": digest(snapshot), "claim_id": subject["id"],
        "claims": [subject, middle, leaf], "questions": questions}
    context = create_review_context(**values, complete_material=material)
    _old, _sources, diagnostics, _counter = section_case.__wrapped__()
    base = diagnostics["claims"][0]
    diagnostics["claims"] = [{**copy.deepcopy(base), "claim_id": claim["id"], "question_slug": claim["question_slug"],
        "qualification": claim["qualification"], **{name: claim[name] for name in ("scope", "time", "units", "premises")}}
        for claim in material["claims"]]
    instructions = []
    for question in questions:
        instruction = {"schema_version": "evidence-coverage-instruction/v1", "question_id": question["original_id"],
            "question_slug": question["slug"], "original_ids": [question["original_id"]],
            "facets": question_plan("question")["facets"], "criteria": question_plan("question")["criteria"],
            "scope": {"method": question["question"]},
            "claims": [row for row in material["claims"] if row["question_slug"] == question["slug"]],
            "anchors": [], "source_contexts": [{"source_id": row["source_id"], "record_sha256": row["record_sha256"],
                "text": row["content"], "scope": {}, "eligible": True} for row in values["sources"]],
            "semantic_approval": "not_evaluated", "accepted": False, "write_authority": "not_established"}
        instruction["basis_id"] = digest(instruction)
        instructions.append(instruction)
    return values, context, diagnostics, instructions


def test_delivery_opt_in_is_additive_and_legacy_document_is_unchanged():
    legacy = ReviewDelivery().document()
    assert legacy == {"schema_version": "research-review-delivery/v1", "mode": "bounded_host_assembled",
        "page_chars": 4096, "max_packet_bytes": 196608, "max_input_bytes": 524288,
        "required_sections": ["sources", "computation_if_configured", "mechanical", "counter_context"],
        "worker_comprehension": "not_observed", "semantic_approval": "not_evaluated"}
    complete = ReviewDelivery(context_version="complete").document()
    assert validate("research-review-delivery/v2", complete) == complete
    with pytest.raises(UsageError):
        validate("research-review-delivery/v1", complete)
    with pytest.raises(UsageError):
        ReviewDelivery(context_version="unknown")


@pytest.mark.parametrize("version", [None, True, 1, [], {}])
def test_context_version_rejects_nonstrings_with_typed_errors(tmp_path, version):
    with pytest.raises(UsageError) as delivery_error:
        ReviewDelivery(context_version=version)
    assert delivery_error.value.error_code == "REVIEW_DELIVERY_INVALID"
    assert delivery_error.value.details == {"field": "context_version"}
    with pytest.raises(UsageError) as page_error:
        with snapshot(tmp_path, "subject", context_version=version):
            pytest.fail("An invalid version reached source custody.")
    assert page_error.value.error_code == "REVIEW_PAGE_REFUSED"
    assert page_error.value.details == {"reason": "context_version_invalid"}


def test_complete_pages_retain_each_question_and_every_transitive_claim(complete_case):
    values, context, diagnostics, instructions = complete_case
    visible, _binding = create_sections(context, values["sources"], diagnostics, counter_instruction=instructions)
    assert visible["schema_version"] == "research-review-sections/v2"
    assert [row["claim_ids"] for row in visible["counter_context"]["questions"]] == [["claim-1"], ["claim-2", "claim-3"]]
    assert [row["scope"] for row in visible["counter_context"]["questions"]] == [row["scope"] for row in instructions]
    assert [row["criteria"][0]["counterevidence"] for row in visible["counter_context"]["questions"]] == [
        row["criteria"][0]["counterevidence"] for row in instructions]
    pages = create_pages(context, values["sources"], page_chars=256, sections=visible)
    header = validate("research-paged-review/v2", pages.header())
    delivered = [pages.page(row["page_id"]) for row in header["pages"]]
    packet = {"schema_version": "research-review-packet/v2", "header": header, "pages": delivered,
        "complete": pages.completeness(delivered)}
    assert validate("research-review-packet/v2", packet) == packet
    assert packet["complete"]["complete"]
    assert len(header["premises"]) == 2 and len(header["questions"]) == 2
    assert header["questions"][1]["body"] == "Retain the original measurement conditions."
    assert not pages.completeness(delivered[:-1])["complete"]


def test_complete_wire_question_bounds_match_context_capacity_and_allow_empty_bodies(complete_case):
    _values, context, _diagnostics, _instructions = complete_case
    contracts = schemas()
    question = {**context.document()["questions"][0], "body": ""}
    header_questions = contracts["research-paged-review/v2"]["properties"]["questions"]
    input_questions = contracts["research-blinded-review-input/v2"]["properties"]["packet"]["properties"]["questions"]
    section_questions = contracts["research-review-sections/v2"]["properties"]["counter_context"]["properties"]["questions"]
    section = {"question_slug": "question-1", "claim_ids": ["claim-1"], "criteria": [], "scope": {}, "alternative_sources": []}
    for shape, row in ((header_questions, question), (input_questions, question), (section_questions, section)):
        _matches([copy.deepcopy(row) for _ in range(32)], shape)
        for count in (0, 33):
            with pytest.raises(UsageError):
                _matches([copy.deepcopy(row) for _ in range(count)], shape)


@pytest.mark.parametrize("change", ["missing", "duplicate", "other_question", "transitive_claim"])
def test_complete_instruction_must_match_its_own_question(complete_case, change):
    values, context, diagnostics, instructions = complete_case
    altered = copy.deepcopy(instructions)
    if change == "missing":
        altered.pop()
    elif change == "duplicate":
        altered.append(copy.deepcopy(altered[0]))
    elif change == "other_question":
        altered[1]["claims"] = copy.deepcopy(altered[0]["claims"])
    else:
        altered[1]["claims"][1]["text"] = "Changed measurement."
    for row in altered:
        row["basis_id"] = digest({key: value for key, value in row.items() if key != "basis_id"})
    with pytest.raises(UsageError):
        create_sections(context, values["sources"], diagnostics, counter_instruction=altered)


def test_profile_scope_checks_complete_noncomputed_context_without_job_upgrade(complete_case):
    _values, context, _diagnostics, _instructions = complete_case
    scope = ExecutionRoleScope("reviewer", "evaluator", ["model", "review.read", "verdict.propose"])
    selected = profile(roles=(ExecutionRole("reviewer", "evaluator", "controller", "model", "prompt/v1"),),
        role_scopes=ExecutionRoleRegistry([scope]), review_delivery=ReviewDelivery(context_version="complete"))
    assert selected.document()["schema_version"] == "research-execution-profile/v14"
    assert replace(selected, review_delivery=ReviewDelivery()).document()["schema_version"] == "research-execution-profile/v7"
    supplied = {"packet": context.document()}
    job = {"schema_version": JOB_V3, "task_role": "evaluator", "role": "evaluator",
        "role_scope_sha256": scope.sha256, "required_capabilities": ["model"], "operation": "evaluate",
        "packet_sha256": _digest(supplied), "context_id": context.document()["context_id"], "payload": {}}
    selected.check_job_scope("reviewer", job, supplied)
    with pytest.raises(ExecutionError):
        selected.check_job_scope("reviewer", job | {"schema_version": JOB_V4}, supplied)
    for version in ("research-review-context/v99", "unknown-packet/v1", "research-review-context/v1"):
        altered = {"packet": context.document() | {"schema_version": version}}
        with pytest.raises(ExecutionError):
            selected.check_job_scope("reviewer", job | {"packet_sha256": _digest(altered)}, altered)


@pytest.mark.parametrize("task_role", ["evaluator", "critic", "adjudicator"])
@pytest.mark.parametrize("change", ["schema", "context", "role"])
def test_release_label_cannot_bypass_reviewer_context_checks(complete_case, task_role, change):
    _values, context, _diagnostics, _instructions = complete_case
    scope = ExecutionRoleScope("reviewer", task_role, ["model"])
    selected = profile(roles=(ExecutionRole("reviewer", "evaluator", "controller", "model", "prompt/v1"),),
        role_scopes=ExecutionRoleRegistry([scope]), review_delivery=ReviewDelivery(context_version="complete"))
    packet = context.document() | {"task_role": task_role}
    if change == "schema":
        packet["schema_version"] = "unknown-packet/v1"
    elif change == "context":
        packet["context_id"] = "different-context"
    else:
        packet["task_role"] = "researcher"
    supplied = {"purpose": "release", "packet": packet}
    job = {"schema_version": JOB_V3, "task_role": task_role, "role": "evaluator",
        "role_scope_sha256": scope.sha256, "required_capabilities": ["model"], "operation": "evaluate",
        "packet_sha256": _digest(supplied), "context_id": context.document()["context_id"], "payload": {}}
    with pytest.raises(ExecutionError, match="execution_context_"):
        selected.check_job_scope("reviewer", job, supplied)


@pytest.mark.parametrize("task_role", ["release", "integration"])
def test_nonreview_release_packets_preserve_their_existing_scope(task_role):
    scope = ExecutionRoleScope("publisher", task_role, ["model"])
    selected = profile(role_scopes=ExecutionRoleRegistry([scope]),
        roles=(ExecutionRole("publisher", "host_worker", "controller", "model", "prompt/v1"),))
    supplied = {"purpose": "release", "packet": {"schema_version": "research-release-input/v1", "content": "Retained artifact."}}
    job = {"schema_version": JOB_V3, "task_role": task_role, "role": "host_worker",
        "role_scope_sha256": scope.sha256, "required_capabilities": ["model"], "operation": "generate",
        "packet_sha256": _digest(supplied), "context_id": "release-context", "payload": {}}
    selected.check_job_scope("publisher", job, supplied)
