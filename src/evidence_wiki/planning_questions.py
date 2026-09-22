"""Lossless question accounting and owner-validated intake and coverage actions."""

from __future__ import annotations

import copy
import json

from .pack_discovery import owner
from .planning_contracts import blocker, digest, owned_call, refuse


def intent(request, blockers):
    payload, decisions = request["request"]["payload"], request["decisions"]
    for field in ("goal",):
        if not payload[field].strip():
            refuse("/request/payload/" + field)
    for index, scope in enumerate(payload["scope"]):
        if not scope["value"].strip():
            refuse(f"/request/payload/scope/{index}/value")
    for index, source in enumerate(payload["sources"]):
        for field in ("kind", "locator"):
            if not source[field].strip():
                refuse(f"/request/payload/sources/{index}/{field}")
    original, derived = payload["questions"], payload["derived_questions"]
    rows = original + derived
    ids = [item["id"] for item in rows]
    if len(ids) != len(set(ids)):
        refuse("/request/payload/questions/duplicate_id")
    originals = {item["id"] for item in original}
    for index, row in enumerate(derived):
        refs = row["original_ids"]
        if len(refs) != len(set(refs)) or not set(refs) <= originals:
            refuse(f"/request/payload/derived_questions/{index}/original_ids")
    intake = owner("intake_questions")
    seen = {}
    for index, row in enumerate(rows):
        if not row["text"].strip():
            refuse(f"/request/payload/questions/{index}/text")
        normal = intake.normalize_question_text(row["text"])
        if normal in seen:
            blockers.append(blocker("duplicate_question_text_requires_decision", questions=[seen[normal], row["id"]], field="/request/payload/questions"))
        seen.setdefault(normal, row["id"])
    scope = [item["name"] for item in payload["scope"]]
    if len(set(scope)) != len(scope):
        refuse("/request/payload/scope/duplicate_name")
    if not scope:
        blockers.append(blocker("material_scope_unspecified", questions=ids, field="/request/payload/scope"))
    for field, key in (("sources", "id"), ("host_tools", "id")):
        keys = [item[key] for item in payload[field]]
        if len(set(keys)) != len(keys):
            refuse("/request/payload/" + field + "/duplicate_id")
    for source in payload["sources"]:
        if not set(source["question_ids"]) <= set(ids) or len(source["question_ids"]) != len(set(source["question_ids"])):
            refuse("/request/payload/sources/question_ids")
    for field, key in (("question_plans", "question_id"), ("source_requirements", "source_id")):
        values = [row[key] for row in decisions[field]]
        allowed = ids if field == "question_plans" else [row["id"] for row in payload["sources"]]
        if len(values) != len(set(values)) or not set(values) <= set(allowed):
            refuse("/decisions/" + field + "/identity")
    if decisions["codebase"] and not set(decisions["codebase"]["question_ids"]) <= set(ids):
        refuse("/decisions/codebase/question_ids")
    if len(rows) > payload["budgets"]["questions"] or decisions["mode"] == "strict" and len(rows) > 100:
        blockers.append(blocker("question_budget_or_strict_bundle_limit", questions=ids, field="/request/payload/budgets/questions"))
    for index, _value in enumerate(payload["open_decisions"]):
        blockers.append(blocker("caller_decision_unresolved", questions=ids, field=f"/request/payload/open_decisions/{index}"))
    return rows


def intake_plan(rows, config, blockers):
    intake, init = owner("intake_questions"), owner("init_research_workspace")
    batch = {"schema_version": "1.0", "questions": [{"id": row["id"], "question": row["text"], "origin": "parent_agent",
        "metadata": {"research_id": row["id"], "original_ids_json": json.dumps(row.get("original_ids", [row["id"]]), ensure_ascii=False), "original_text": row["text"]}}
        for row in rows]}
    owned_call("/questions/intake/envelope", intake.validate_batch_envelope, batch)
    normalized = owned_call("/questions/intake/items", init.normalize_question_items, batch["questions"],
        allowed_keys=intake.INTAKE_QUESTION_ITEM_KEYS, error_prefix="research request", metadata_normalizer=intake.normalize_intake_metadata)
    owned_call("/questions/intake/configuration", intake.validate_against_config, config, normalized, init)
    for index, item in enumerate(batch["questions"]):
        try:
            intake.validate_intake_field_lengths([item])
        except (Exception, SystemExit):
            blockers.append(blocker("question_exceeds_intake_limit", questions=[rows[index]["id"]], field=f"/request/payload/questions/{index}/text"))
    normalized_rows = [{"id": row["id"], "slug": item["slug"], "original_ids": row.get("original_ids", [row["id"]]),
                       "original_text": row["text"], "intake_text": item["question"], "state": "planned"}
                       for row, item in zip(rows, normalized, strict=True)]
    original_map = [{"original_id": row["id"], "question_slugs": [item["slug"] for item in normalized_rows if row["id"] in item["original_ids"]],
                     "outcome": "pending"} for row in rows if "original_ids" not in row]
    limit = owner("_intake_limits")
    for field, default in (("max_open_questions_total", limit.DEFAULT_MAX_OPEN_QUESTIONS_TOTAL),
                           ("max_intake_per_hour", limit.DEFAULT_MAX_INTAKE_PER_HOUR)):
        if len(rows) > limit.positive_int_config(config, field, default):
            blockers.append(blocker("intake_capacity_exceeded", questions=[row["id"] for row in rows], field="/profile/run/" + field))
    return {"strategy": "post_initialization_intake", "initializer_seeds": [], "batch": batch,
            "rows": normalized_rows, "original_map": original_map, "original_count": len(original_map),
            "derived_count": len(rows) - len(original_map), "total_count": len(rows), "validation": "owner_validated_with_reported_limits",
            "text_rule": "Original text retained verbatim; owner trims outer whitespace only for intake pages."}


def coverage_plan(request, questions, config, policy, metadata, computation, blockers):
    coverage = owner("coverage_manifest")
    vocab = owned_call("/coverage/policy_vocabularies", coverage.merged_policy_vocabularies, config)
    rules = owned_call("/coverage/policy_rules", owner("_policy_primitives").pack_policy_rules, config)
    scopes = {item["name"] for item in request["request"]["payload"]["scope"]}
    sources = {row["id"]: row for row in request["request"]["payload"]["sources"]}
    selections = {item["question_id"]: item for item in request["decisions"]["question_plans"]}
    result = []
    for question in questions["rows"]:
        qid = question["id"]
        selection = selections.get(qid)
        if selection is None:
            blockers.append(blocker("question_evidence_criteria_missing", questions=[qid], field="/decisions/question_plans"))
            selection = {"template": None, "facets": [], "criteria": []}
        template, template_identity = None, None
        if selection["template"] is not None:
            template = (metadata or {}).get("coverage_templates", {}).get(selection["template"])
            if template is None:
                blockers.append(blocker("coverage_template_unavailable", questions=[qid], field="/decisions/question_plans/template"))
            else:
                template_identity = {"name": selection["template"], "sha256": template["sha256"]}
                template = owned_call("/coverage/template", coverage.normalize_template_document,
                                      template["declaration"], policy_vocabularies=vocab)
        facets = []
        for required, field in ((True, "required_facets"), (False, "optional_facets")):
            for raw in (template or {}).get(field, []):
                facets.append(owned_call("/coverage/template", coverage.normalize_template_facet, raw, required=required, policy_vocabularies=vocab))
        for raw in selection["facets"]:
            facets.append(owned_call("/coverage/facets", coverage.normalize_template_facet, raw, required=raw.get("required", True), policy_vocabularies=vocab))
        facet_ids = [facet["facet_id"] for facet in facets]
        if len(set(facet_ids)) != len(facet_ids):
            refuse("/coverage/duplicate_facet_id")
        if not any(facet["required"] for facet in facets):
            blockers.append(blocker("required_evidence_facets_missing", questions=[qid], field="/decisions/question_plans/facets"))
        criteria = {row["facet_id"]: row for row in selection["criteria"]}
        if len(criteria) != len(selection["criteria"]) or not set(criteria) <= set(facet_ids):
            refuse("/coverage/criteria/facet_id")
        checks = []
        for facet in facets:
            fid = facet["facet_id"]
            if facet["accepted_source_ids"] or facet["blocking_request_ids"] or facet["facet_verdict"] != "pending":
                refuse("/coverage/facets/caller_acceptance_forbidden")
            if facet["required"] and facet["min_sources"] < 1:
                blockers.append(blocker("required_facet_source_minimum_missing", questions=[qid], facet=fid))
            criterion = criteria.get(fid)
            if criterion is None:
                blockers.append(blocker("facet_review_criteria_missing", questions=[qid], facet=fid, field="/decisions/question_plans/criteria"))
            else:
                for scope in sorted(set(criterion["required_scope"]) - scopes):
                    blockers.append(blocker("required_scope_missing", questions=[qid], facet=fid, field="/request/payload/scope/" + scope))
                quantitative = criterion["quantitative"]
                if quantitative is not None:
                    if computation is None:
                        blockers.append(blocker("computation_definition_missing", questions=[qid], facet=fid, field="/decisions/computation"))
                    else:
                        definition = computation["definition"]
                        for ref in quantitative["references"]:
                            ref_schema = owner("_computation_contract").schemas()[owner("_computation_contract").SCHEMA]["properties"]["graphs"]["additionalProperties"]["properties"]["inputs"]["additionalProperties"]
                            owned_call("/coverage/quantitative/reference", owner("_strict_contract").validate_shape, ref, ref_schema)
                            owned_call("/coverage/quantitative/reference", owner("_computation_contract").validate_reference, ref, definition)
                        if not set(quantitative["invariants"]) <= set(definition["invariants"]):
                            refuse("/coverage/quantitative/invariants")
                    for field in quantitative["fields"]:
                        if field["source_id"] not in sources or qid not in sources[field["source_id"]]["question_ids"]:
                            refuse("/coverage/quantitative/field_source")
                        owned_call("/coverage/quantitative/pointer", owner("_computation_contract").pointer, field["pointer"])
                    blockers.append(blocker("usable_numeric_evidence_required", questions=[qid], facet=fid, stage="research"))
            manual = []
            for field in ("source_policy", "freshness_policy", "identity_policy"):
                name = facet[field]
                if (name.startswith("pack:") and name not in rules or name in {"manual_review", "manual_review_required"}
                        or name in rules and rules[name].manual_review_required):
                    manual.append(name)
            checks.append({"facet_id": fid, "criteria": criterion, "manual_policies": manual,
                "semantic_review": "required" if policy else "caller_selected_legacy",
                "policy_sha256": digest(policy) if policy else None, "rubric_sha256": digest(policy["rubric"]) if policy else None,
                "criteria_sha256": digest(criterion) if criterion else None,
                "source_state": "no_accepted_evidence", "checks_executed": False})
            if facet["required"]:
                blockers.append(blocker("facet_evidence_not_observed", questions=[qid], facet=fid, stage="research"))
        # Times are supplied by the coverage writer at execution, never fabricated
        # as observations. A fixed placeholder is used only for owner shape checks.
        document = {"schema_version": coverage.SCHEMA_VERSION, "question_slug": question["slug"],
            "created_at": "1970-01-01T00:00:00Z", "updated_at": "1970-01-01T00:00:00Z",
            "coverage_profile": (template or {}).get("coverage_profile") or coverage.DEFAULT_COVERAGE_PROFILE,
            "coverage_verdict": "pending", "required_facets": [copy.deepcopy(f) for f in facets if f["required"]],
            "optional_facets": [copy.deepcopy(f) for f in facets if not f["required"]]}
        owned_call("/coverage/manifest", coverage.validate_manifest, document, expected_slug=question["slug"], policy_vocabularies=vocab)
        del document["created_at"], document["updated_at"]
        result.append({"question_id": qid, "question_slug": question["slug"], "template": template_identity,
            "manifest_fields": document, "timestamp_policy": "coverage_owner_at_execution", "review_requirements": checks,
            "template_document": {key: document[key] for key in ("coverage_profile", "required_facets", "optional_facets")},
            "operation": "coverage_setup", "owner": "coverage_manifest", "state": "planned"})
    return result
