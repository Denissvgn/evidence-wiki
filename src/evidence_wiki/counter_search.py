"""Actual bounded retrieval observations qualify search, never exhaustive absence."""

from evidence_wiki import Workspace
from evidence_wiki import execution_session as native
from evidence_wiki.mutation_contracts import validate_request_id
from evidence_wiki.research_session import ResearchSession

from .coverage_examination import fail, rows, text, unique
from .coverage_observation import observe


def observation(root, *, question_slug, query, source_ids, exclusions, expected_instruction_basis, limit=8):
    """Use the installed retrieval owner and bracket exact current source context."""
    source_ids = unique(list(source_ids))
    if not 1 <= len(source_ids) <= 32 or type(limit) is not int or not 1 <= limit <= 32:
        fail("counter_search_bound")
    query = text(query)
    exclusions = [text(item, 512) for item in rows(exclusions, 32)]
    before = observe(root, question_slug=question_slug, source_ids=source_ids)
    if before["basis_id"] != expected_instruction_basis:
        fail("counter_search_material_changed")
    with Workspace.open(root) as workspace:
        report = workspace.sources.search(query, source_ids=source_ids, engine="lexical", limit=limit, excerpt_chars=2048)
    closing = observe(root, question_slug=question_slug, source_ids=source_ids)
    if closing["basis_id"] != before["basis_id"]:
        fail("counter_search_material_changed")
    return {"schema_version": "evidence-counter-search-observation/v1", "query": query, "exclusions": exclusions,
        "selected_source_ids": source_ids, "limit": limit, "instruction_basis": before["basis_id"], "report": report,
        "search_execution": "observed", "absence_conclusion": "unknown", "accepted": False,
        "provider_calls": False, "index_writes": False}


def retained(session, *, request_id, question_slug, query, source_ids, exclusions, expected_instruction_basis, limit=8):
    """Use existing native run accounting and journal for the concrete query."""
    if type(session) is not ResearchSession or session.execution is None:
        fail("bound_execution_session_required")
    request_id = validate_request_id(request_id)
    session._check("advance", write=True)
    with native.gate(session):
        current = native.current(session)
        if not set(source_ids) <= {row["source_id"] for row in current["execution"]["source_bindings"]}:
            fail("counter_search_outside_run_sources")
        questions = {question["slug"] for original in current["accounting"]["original_outcomes"] for question in original["questions"]}
        if question_slug not in questions:
            fail("counter_search_outside_run_question")
    arguments = {"question_slug": question_slug, "query": query, "source_ids": source_ids,
        "exclusions": exclusions, "expected_instruction_basis": expected_instruction_basis, "limit": limit}
    def invoke(_control):
        return observation(session.root, **arguments)
    value = native._native_call(session, request_id, "coverage.counter_search", arguments, invoke, recover=invoke)
    # Completed query replay is historical. Recheck current material before using
    # its snippets or attributing its no-result observation to today's evidence.
    if observe(session.root, question_slug=question_slug, source_ids=source_ids)["basis_id"] != expected_instruction_basis:
        fail("counter_search_material_changed")
    return value
