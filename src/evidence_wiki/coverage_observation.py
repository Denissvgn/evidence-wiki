"""Read actual frozen criteria and currently permitted mechanical claim anchors."""

from contextlib import nullcontext
from pathlib import Path

from evidence_wiki import claim_diagnostics
from evidence_wiki._pack_io import json_document
from evidence_wiki.host_context import current_context
from evidence_wiki.mutation_contracts import digest
from evidence_wiki.mutation_store import capture_revision
from evidence_wiki.pack_discovery import owner
from evidence_wiki.request_lifecycle_operations import _questions
from evidence_wiki.requirements_operations import REQUIREMENTS, instruction_matches, read_effective
from evidence_wiki.retrieval_operations import _documents, _ScopedView

from .coverage_examination import fail, prepare, unique


def observe(root, *, question_slug, source_ids=()):
    """No storage, model calls, coverage writes or semantic approvals occur here."""
    return observe_questions(root, question_slugs=[question_slug], source_ids=source_ids)[0]


def observe_questions(root, *, question_slugs, source_ids=()):
    """Observe bounded questions together through current native owners."""
    context = current_context()
    if context is None or context.mutation_control_directory is None:
        fail("current_host_control_required")
    context.require_grant("coverage_observation.observe")
    root = context.require_path(Path(root)).resolve(strict=True)
    if (type(question_slugs) not in {list, tuple} or not question_slugs
            or any(type(slug) is not str or not slug for slug in question_slugs)):
        fail("question_selector_invalid")
    if len(question_slugs) > 32:
        fail("question_selector_bound")
    requested = unique(list(question_slugs))
    if type(source_ids) not in {list, tuple}:
        fail("source_selector_invalid")
    selected = unique(list(source_ids))
    if len(selected) > 32:
        fail("source_selector_bound")
    private_before = capture_revision(root, excluded=(".locks", "raw/.locks"))["revision_id"]
    view = _ScopedView(root)
    raw = view.read(REQUIREMENTS)
    effective = read_effective(root, raw=raw)
    questions = _questions(root, view, effective)
    if any(slug not in questions for slug in requested):
        fail("question_outside_frozen_scope")
    strict = owner("_strict_evidence")
    policy = strict.resolve_policy(root, view.config)
    if policy is None or REQUIREMENTS not in policy["instructions"] or not instruction_matches(root, raw, policy["instructions"][REQUIREMENTS]):
        fail("strict_frozen_instruction_required")
    inventory_raw = view.read(policy["claims_path"])
    if inventory_raw is None:
        fail("admitted_inventory_required")
    inventory = owner("_strict_contract").claims_document(json_document(inventory_raw))
    claims_by_question = {slug: [row for row in inventory["claims"] if row["question_slug"] == slug]
        for slug in requested}
    sources_by_question = {slug: selected or list(dict.fromkeys(
        item["source_id"] for claim in claims for item in claim["evidence"]))
        for slug, claims in claims_by_question.items()}
    if any(len(sources) > 32 for sources in sources_by_question.values()):
        fail("source_selector_bound")
    selected = list(dict.fromkeys(sid for sources in sources_by_question.values() for sid in sources))
    if len(selected) > 32:
        fail("source_selector_bound")
    usage = owner("_evidence_usage")
    protected = usage.configured(view.config)
    live_context = usage.current_view(root, view.config) if protected else nullcontext(None)
    with live_context as live:
        _docs, facts, _unprocessed, _unknown = _documents(view, selected, usage_view=live)
        by_source = {}
        for fact in facts.values():
            body = fact["document"].body
            if len(body.encode()) > 49152:
                fail("source_context_bound")
            sid = fact["source_id"]
            source = {"source_id": sid, "record_sha256": fact["normalized_sha256"], "text": body,
                "scope": owner("source_requests").source_provenance_scope(root, view.config, sid), "eligible": True}
            by_source[sid] = source
        declared = effective["request"]["payload"]["questions"]
        originals = [{"original_id": original["id"], "text": original["text"],
            "question_slugs": [row["slug"] for row in inventory["questions"] if row["original_id"] == original["id"]]}
            for original in declared if any(row["original_id"] == original["id"] for row in inventory["questions"])]
        diagnosed = claim_diagnostics.current(root, {"schema_version": "evidence-draft-proposal/v1",
            "inventory": inventory, "originals": originals})
        by_claim = {row["claim_id"]: row for row in diagnosed["claims"]}
        results = []
        for question_slug, claims in claims_by_question.items():
            selected_sources = sources_by_question[question_slug]
            observed = []
            for claim in claims:
                for index, evidence in enumerate(claim["evidence"]):
                    source = by_source.get(evidence["source_id"]) if evidence["source_id"] in selected_sources else None
                    eligible = source is not None
                    status = "not_established"
                    revision = evidence["record_sha256"]
                    if eligible:
                        revision = source["record_sha256"]
                        diagnostic = by_claim[claim["id"]]["evidence"][index]
                        current_usage = diagnostic["current_usage"]
                        eligible = (not protected or current_usage is not None and current_usage["eligible"] is True)
                        status = "verified" if eligible and diagnostic["result"] == "verified" else "not_established"
                    observed.append({"claim_id": claim["id"], "evidence_index": index, "record_sha256": revision,
                        "mechanical_result": status, "eligible": eligible})
            result = prepare(effective, question_id=questions[question_slug]["id"], question_slug=question_slug,
                admitted_inventory=inventory, anchor_observations=observed, source_contexts=[by_source[sid] for sid in selected_sources if sid in by_source])
            results.append(result)
        view.recheck_scope()
        view.finish()
        if live is not None:
            live.revalidate(root, view.config)
        if current_context() is not context or capture_revision(root, excluded=(".locks", "raw/.locks"))["revision_id"] != private_before:
            fail("native_observation_changed")
        for result in results:
            result["owner_observation"] = {"state": "observed", "permission_basis": "native_current_usage" if protected else "legacy_compatibility",
                "mechanical_only": True, "semantic_approval": "not_evaluated"}
            result["basis_id"] = digest({key: value for key, value in result.items() if key != "basis_id"})
        return results
