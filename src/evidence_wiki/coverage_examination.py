"""Inert bounded coverage findings over owner-supplied frozen observations.

This codec authenticates nothing. The adapter must supply actual protected
requirements, admitted inventory and current source/usage observations, then
authenticate model findings and recheck those owners before effects.
"""

from functools import wraps

from evidence_wiki._pack_io import bounded
from evidence_wiki.errors import UsageError
from evidence_wiki.mutation_contracts import detached, digest
from evidence_wiki.pack_discovery import owner
from evidence_wiki.requirements_projection import _base

INSTRUCTION = "evidence-coverage-instruction/v1"
COVERAGE = "evidence-coverage-proposal/v1"
COUNTER = "evidence-counterevidence-proposal/v1"


def fail(reason):
    raise UsageError("COVERAGE_EXAMINATION_INVALID", "Coverage examination refused.", details={"reason": reason})


def codec_boundary(function):
    @wraps(function)
    def checked(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (KeyError, TypeError, AttributeError, UnicodeError, OverflowError):
            fail("invalid_field_type")
    return checked


def exact(value, fields):
    if type(value) is not dict or set(value) != set(fields):
        fail("closed_shape_required")
    return value


def rows(value, maximum):
    if type(value) is not list or len(value) > maximum:
        fail("row_bound")
    return value


def text(value, maximum=4096):
    if type(value) is not str or not value.strip() or len(value.encode()) > maximum:
        fail("text_invalid")
    return value


def unique(values):
    rows(values, 128)
    if any(type(value) is not str for value in values) or len(values) != len(set(values)):
        fail("duplicate_or_invalid_reference")
    return values


@codec_boundary
def prepare(effective_requirements, *, question_id, question_slug, admitted_inventory, anchor_observations, source_contexts):
    """Bind exact native criteria; mechanical source facts confer no approval."""
    frozen = _base(effective_requirements)
    inventory = owner("_strict_contract").claims_document(detached(admitted_inventory))
    plans = [row for row in frozen["decisions"]["question_plans"] if row["question_id"] == question_id]
    if len(plans) != 1 or plans[0]["template"] is not None:
        fail("frozen_inline_plan_required")
    plan = plans[0]
    facets = {row["facet_id"]: row for row in plan["facets"]}
    criteria = {row["facet_id"]: row for row in plan["criteria"]}
    if len(facets) != len(plan["facets"]) or len(criteria) != len(plan["criteria"]) or set(facets) != set(criteria):
        fail("frozen_facet_criteria_mismatch")
    payload = frozen["request"]["payload"]
    questions = [row for row in [*payload["questions"], *payload["derived_questions"]] if row["id"] == question_id]
    mapped = [row for row in inventory["questions"] if row["slug"] == question_slug]
    if len(questions) != 1 or len(mapped) != 1 or mapped[0]["question"] != questions[0]["text"].strip():
        fail("question_identity_changed")
    originals = questions[0].get("original_ids", [question_id])
    if mapped[0]["original_id"] not in originals:
        fail("original_identity_changed")
    claims = [row for row in inventory["claims"] if row["question_slug"] == question_slug]
    scopes = {row["name"]: row["value"] for row in payload["scope"]}
    contexts = {}
    for source in rows(source_contexts, 32):
        exact(source, {"source_id", "record_sha256", "text", "scope", "eligible"})
        text(source["text"], 49152)
        if source["source_id"] in contexts or type(source["scope"]) is not dict or type(source["eligible"]) is not bool:
            fail("source_context_invalid")
        if source["eligible"]:
            contexts[source["source_id"]] = detached(source)
    anchors, seen = [], set()
    by_claim = {claim["id"]: claim for claim in claims}
    for observed in rows(anchor_observations, 1600):
        exact(observed, {"claim_id", "evidence_index", "record_sha256", "mechanical_result", "eligible"})
        claim = by_claim.get(observed["claim_id"])
        index = observed["evidence_index"]
        if claim is None or type(index) is not int or not 0 <= index < len(claim["evidence"]):
            fail("foreign_claim_or_anchor")
        key = (claim["id"], index)
        if key in seen or observed["mechanical_result"] not in {"verified", "not_established"} or type(observed["eligible"]) is not bool:
            fail("anchor_observation_invalid")
        seen.add(key)
        evidence = claim["evidence"][index]
        if observed["record_sha256"] != evidence["record_sha256"]:
            fail("source_revision_changed")
        source = contexts.get(evidence["source_id"])
        if observed["eligible"] and observed["mechanical_result"] == "verified":
            if source is None or source["record_sha256"] != evidence["record_sha256"]:
                fail("current_source_context_required")
            anchors.append({"claim_id": claim["id"], "source_id": evidence["source_id"], "record_sha256": evidence["record_sha256"]})
    public_claims = []
    for claim in claims:
        if all(item["source_id"] in contexts for item in claim["evidence"]):
            public_claims.append(claim)
        else:
            public_claims.append({"id": claim["id"], "question_slug": claim["question_slug"],
                "qualification": claim["qualification"], "premises": claim["premises"], "context_state": "unavailable"})
    value = {"schema_version": INSTRUCTION, "question_id": question_id, "question_slug": question_slug,
        "original_ids": originals, "facets": detached(plan["facets"]), "criteria": detached(plan["criteria"]),
        "scope": scopes, "claims": public_claims, "anchors": anchors,
        "source_contexts": list(contexts.values()),
        "semantic_approval": "not_evaluated", "accepted": False, "write_authority": "not_established"}
    bounded(value)
    value["basis_id"] = digest(value)
    return value


@codec_boundary
def check_instruction(value):
    value = detached(value)
    bounded(value)
    basis = value.pop("basis_id", None)
    if value.get("schema_version") != INSTRUCTION or digest(value) != basis:
        fail("instruction_changed")
    return {**value, "basis_id": basis}


@codec_boundary
def reduce_coverage(instruction, proposal):
    """Return proposals for native links; every facet remains semantically unknown."""
    instruction = check_instruction(instruction)
    bounded(proposal)
    exact(proposal, {"schema_version", "basis_id", "links", "missing"})
    if proposal["schema_version"] != COVERAGE or proposal["basis_id"] != instruction["basis_id"]:
        fail("proposal_basis_changed")
    facets = {row["facet_id"]: row for row in instruction["facets"]}
    criteria = {row["facet_id"]: row for row in instruction["criteria"]}
    sources = {row["source_id"]: row for row in instruction["source_contexts"]}
    links, seen = [], set()
    for link in rows(proposal["links"], 32):
        exact(link, {"facet_id", "claim_ids", "source_ids"})
        fid = link["facet_id"]
        if fid not in facets or fid in seen:
            fail("facet_unknown_or_duplicate")
        seen.add(fid)
        claims, selected = unique(link["claim_ids"]), unique(link["source_ids"])
        if not claims or not selected:
            fail("empty_link")
        permitted = {row["source_id"] for row in instruction["anchors"] if row["claim_id"] in claims}
        known = {row["id"] for row in instruction["claims"]}
        if (not set(claims) <= known or not set(selected) <= permitted
                or any(not any(anchor["claim_id"] == claim_id and anchor["source_id"] in selected
                    for anchor in instruction["anchors"]) for claim_id in claims)):
            fail("unsupported_link")
        required = criteria[fid]["required_scope"]
        if any(key not in instruction["scope"] or any(sources[sid]["scope"].get(key) != instruction["scope"][key] for sid in selected) for key in required):
            fail("frozen_required_scope_missing")
        links.append({"slug": instruction["question_slug"], "facet_id": fid, "source_ids": selected, "claim_ids": claims})
    missing = []
    for action in rows(proposal["missing"], 32):
        exact(action, {"facet_id", "query"})
        if action["facet_id"] not in facets:
            fail("facet_unknown")
        missing.append({"facet_id": action["facet_id"], "query": text(action["query"])})
    return {"basis_id": instruction["basis_id"], "link_proposals": links, "missing_evidence": missing,
        "facets": [{"facet_id": fid, "required": row["required"], "state": "unknown"} for fid, row in facets.items()],
        "semantic_approval": "not_evaluated", "accepted": False, "write_authority": "not_established"}


@codec_boundary
def reduce_counterevidence(instruction, proposal):
    """Retain bounded searches and exact opposing quotes; absence stays unknown."""
    instruction = check_instruction(instruction)
    bounded(proposal)
    exact(proposal, {"schema_version", "basis_id", "findings"})
    if proposal["schema_version"] != COUNTER or proposal["basis_id"] != instruction["basis_id"]:
        fail("proposal_basis_changed")
    known = {row["id"] for row in instruction["claims"]}
    facets = {row["facet_id"] for row in instruction["facets"]}
    sources = {row["source_id"]: row for row in instruction["source_contexts"]}
    findings, objections = [], []
    for finding in rows(proposal["findings"], 32):
        exact(finding, {"claim_id", "facet_id", "kind", "query", "exclusions", "source_ids", "evidence", "rationale"})
        if finding["claim_id"] not in known or finding["facet_id"] not in facets or finding["kind"] not in {"opposing", "alternative_hypothesis", "no_result", "examined_no_unresolved_objection"}:
            fail("finding_identity_invalid")
        text(finding["query"])
        text(finding["rationale"])
        selected = unique(finding["source_ids"])
        if not set(selected) <= sources.keys():
            fail("search_scope_changed")
        exclusions = [text(value) for value in rows(finding["exclusions"], 32)]
        evidence = rows(finding["evidence"], 16)
        for item in evidence:
            exact(item, {"source_id", "record_sha256", "quote", "start", "end"})
            source = sources.get(item["source_id"])
            quote = text(item["quote"])
            if (source is None or item["source_id"] not in selected or item["record_sha256"] != source["record_sha256"] or type(item["start"]) is not int or type(item["end"]) is not int
                    or not 0 <= item["start"] < item["end"] <= len(source["text"]) or source["text"][item["start"]:item["end"]] != quote):
                fail("opposing_anchor_changed")
        if finding["kind"] == "no_result" and evidence or finding["kind"] in {"opposing", "examined_no_unresolved_objection"} and not evidence:
            fail("finding_evidence_invalid")
        projected = {**detached(finding), "exclusions": exclusions, "absence_conclusion": "unknown",
            "search_execution": "not_established", "search_completeness": "not_established", "semantic_approval": "not_evaluated"}
        findings.append(projected)
        if finding["kind"] in {"opposing", "alternative_hypothesis"}:
            objections.append({"claim_id": finding["claim_id"], "facet_id": finding["facet_id"], "finding_ref": digest(projected)})
    return {"basis_id": instruction["basis_id"], "findings": findings, "material_objection_candidates": objections,
        "acceptance_gate": "blocked" if objections else "not_blocked_candidate" if findings and all(
            finding["kind"] == "examined_no_unresolved_objection" for finding in findings) else "unknown", "accepted": False, "write_authority": "not_established"}


def examined_subject(findings, claim_id, required_facets):
    """Premise findings cannot stand in for the requested subject's disposition."""
    own = [finding for finding in findings if finding["claim_id"] == claim_id]
    return bool(own) and set(required_facets) <= {
        finding["facet_id"] for finding in own if finding["kind"] == "examined_no_unresolved_objection"}
