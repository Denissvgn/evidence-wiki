"""Blinded mechanical facts and exact counter material confer no review authority."""

import re

from evidence_wiki._pack_io import bounded
from evidence_wiki.mutation_contracts import detached, digest
from evidence_wiki.onboarding_contract import _matches
from evidence_wiki.pack_discovery import owner
from evidence_wiki.review_context import ReviewContext

from .packet_pages import refuse


def create(context, sources, diagnostics, counter_material=(), *, counter_instruction=None):
    """Project native diagnostics and reproduce every supplied source slice.

    The parent must obtain diagnostics from the current native owner and retain
    authentic search receipts. A reference string here does not authenticate a
    search, establish exhaustive absence or grant source access.
    """
    if type(context) is not ReviewContext or type(sources) is not list:
        refuse("section_context_invalid")
    try:
        _matches(diagnostics, owner("_claim_diagnostics").schema_document())
    except (KeyError, ValueError, TypeError):
        refuse("mechanical_diagnostics_invalid")
    aliases = context.binding()["aliases"]
    packet = context.document()
    claims = {row["id"]: row for row in [packet["claim"], *packet["premises"]]}
    rows = {}
    for row in diagnostics["claims"]:
        alias = aliases["claims"].get(row["claim_id"])
        if alias not in claims:
            continue
        if alias in rows:
            refuse("mechanical_claim_duplicate")
        claim = claims[alias]
        if (aliases["question"].get(row["question_slug"]) != claim["question_slug"]
                or any(row[name] != claim[name] for name in ("scope", "time", "units"))
                or [aliases["claims"].get(identifier) for identifier in row["premises"]] != claim["premises"]):
            refuse("mechanical_claim_changed")
        evidence = []
        seen = set()
        for item in row["evidence"]:
            index = item["evidence_index"]
            if index in seen or not 0 <= index < len(claim["evidence"]):
                refuse("mechanical_evidence_changed")
            seen.add(index)
            source = aliases["sources"].get(item["source_id"])
            expected = claim["evidence"][index]
            if source != expected["source_id"] or item["record_sha256"] != expected["record_sha256"]:
                refuse("mechanical_evidence_changed")
            evidence.append({"evidence_index": index, "source_id": source,
                **{key: detached(item[key]) for key in ("record_sha256", "form", "result", "reason", "mechanical", "current_usage")}})
        if seen != set(range(len(claim["evidence"]))):
            refuse("mechanical_evidence_missing")
        rows[alias] = {"claim_id": alias, "mechanical_result": row["mechanical_result"], "evidence": evidence,
            "semantic_approval": "not_evaluated", "accepted": False}
    if set(rows) != set(claims):
        refuse("mechanical_claim_missing")
    if type(counter_material) not in {list, tuple} or len(counter_material) > 32:
        refuse("counter_material_bound")
    bodies = {}
    for source in sources:
        if type(source) is not dict or type(source.get("source_id")) is not str:
            refuse("counter_source_invalid")
        if source["source_id"] in bodies:
            refuse("counter_source_duplicate")
        bodies[source["source_id"]] = source
    counter, retained_refs = [], []
    fields = {"source_id", "record_sha256", "start", "end", "quote", "truncated", "search_ref"}
    for item in counter_material:
        bounded(item)
        if (type(item) is not dict or set(item) != fields or type(item["source_id"]) is not str
                or item["source_id"] not in aliases["sources"] or type(item["start"]) is not int
                or type(item["end"]) is not int or type(item["quote"]) is not str
                or not 0 < len(item["quote"]) <= 4096 or type(item["truncated"]) is not bool
                or type(item["search_ref"]) is not str
                or re.fullmatch(r"sha256:[a-f0-9]{64}", item["search_ref"]) is None):
            refuse("counter_material_invalid")
        source = bodies.get(item["source_id"])
        if (source is None or type(source.get("content")) is not str
                or source["record_sha256"] != item["record_sha256"]
                or not 0 <= item["start"] < item["end"] <= len(source["content"])
                or source["content"][item["start"]:item["end"]] != item["quote"]):
            refuse("counter_material_changed")
        counter.append({"source_id": aliases["sources"][item["source_id"]],
            **{key: item[key] for key in ("record_sha256", "start", "end", "quote", "truncated")},
            "grounding": "exact_current_source_slice", "semantic_approval": "not_evaluated", "accepted": False})
        retained_refs.append(item["search_ref"])
    counter_scope = {"status": "not_observed", "criteria": [], "scope": {}, "alternative_sources": []}
    instruction_ref = None
    if counter_instruction is not None:
        from evidence_wiki.counter_context import alternative_aliases
        from evidence_wiki.coverage_examination import check_instruction
        from evidence_wiki.review_context import _metadata
        instruction = check_instruction(counter_instruction)
        if aliases["question"].get(instruction["question_slug"]) != packet["question"]["slug"]:
            refuse("counter_instruction_question_changed")
        instructed_claims = {row["id"]: row for row in instruction["claims"]}
        for identifier, alias in aliases["claims"].items():
            if alias not in claims:
                continue
            supplied = instructed_claims.get(identifier)
            if supplied is None or any(supplied.get(name) != claims[alias][name] for name in ("text", "scope", "time", "units")):
                refuse("counter_instruction_claim_changed")
        retained = {row["source_id"]: row for row in instruction["source_contexts"]}
        for identifier in aliases["sources"]:
            source = bodies.get(identifier)
            actual = retained.get(identifier)
            if (actual is None or source is None or source["record_sha256"] != actual["record_sha256"]
                    or source["content"] != actual["text"]):
                refuse("counter_instruction_source_changed")
        facet_aliases = {row["facet_id"]: "facet-" + str(index + 1) for index, row in enumerate(instruction["facets"])}
        alternatives = []
        for identifier, alias in alternative_aliases(instruction, context).items():
            source = bodies.get(identifier)
            actual = retained[identifier]
            if (source is None or source["record_sha256"] != actual["record_sha256"]
                    or source["content"] != actual["text"]):
                refuse("counter_instruction_source_changed")
            alternatives.append({"source_id": alias, "record_sha256": source["record_sha256"],
                "metadata": _metadata(source.get("metadata", {})), "content": source["content"]})
        counter_scope = {"status": "current_native_instruction", "scope": detached(instruction["scope"]),
            "criteria": [{**detached(row), "facet_id": facet_aliases[row["facet_id"]]} for row in instruction["criteria"]],
            "alternative_sources": alternatives}
        instruction_ref = digest(instruction)
    visible = {"schema_version": "research-review-sections/v1", "context_sha256": context.packet_id,
        "mechanical": [rows[identifier] for identifier in claims], "counter_material": counter,
        "counter_context": counter_scope,
        "counter_search_completeness": "not_established", "semantic_approval": "not_evaluated", "accepted": False}
    bounded(visible)
    return detached(visible), {"section_sha256": digest(visible), "diagnostics_sha256": digest(diagnostics),
        "search_refs": retained_refs, "source_input_sha256": digest(sources), "counter_instruction_ref": instruction_ref}
