"""Bounded structural differences and conservative research dependency mapping."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml
from _evidence_revision import canonical_bytes, capture_workspace, content_id
from _pack_revision_guard import inputs, require, research_input, sibling

MAX_QUESTIONS = 300
MAX_REQUESTS = 200
MAX_CHANGES = 1024
MAX_HISTORY = 32


def digest(value):
    return content_id("evidence-pack-value/v1", value)


def differences(before, after, path=""):
    if before == after:
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        rows = []
        for key in sorted(set(before) | set(after)):
            pointer = path + "/" + str(key).replace("~", "~0").replace("/", "~1")
            if key not in before or key not in after:
                rows.append({"path": pointer, "change": "added" if key in after else "removed",
                    "before": digest(before[key]) if key in before else None,
                    "after": digest(after[key]) if key in after else None})
            else:
                rows.extend(differences(before[key], after[key], pointer))
        return rows
    if isinstance(before, list) and isinstance(after, list):
        items = before + after
        if items and all(isinstance(row, dict) and isinstance(row.get("id"), str) for row in items):
            return differences({r["id"]: r for r in before}, {r["id"]: r for r in after}, path)
        if all(isinstance(row, str) for row in items):
            # Reordering is separately reported; membership never hides removals.
            rows = differences(dict.fromkeys(before, True), dict.fromkeys(after, True), path)
            if rows:
                return rows
    return [{"path": path, "change": "changed", "before": digest(before), "after": digest(after)}]


def policies(config):
    pack = config.get("domain_pack", {})
    result = {}
    for field, declarations in pack.get("policy_vocabularies", {}).items():
        for key, value in declarations.items():
            result[key] = {"field": field, "description": value, "rule": pack.get("policy_rules", {}).get(key)}
    for key, rule in pack.get("policy_rules", {}).items():
        result.setdefault(key, {"rule": rule})
    return result


def changed_ids(before, after):
    return {key for key in set(before) | set(after) if before.get(key) != after.get(key)}


def computation_ids(config):
    definition = config.get("computation") or {}
    result = {"/" + section + "/" + key for section in ("tables", "aggregations", "graphs", "invariants", "cadence")
              for key in definition.get(section, {})}
    for section, fields in (("graphs", ("constants", "inputs", "nodes", "output_mapping")), ("aggregations", ("metrics",))):
        for name, value in definition.get(section, {}).items():
            result.update("/" + "/".join((section, name, field, key)) for field in fields for key in value[field])
    return result


def identifier_changes(before, after):
    return {"added": sorted(set(after) - set(before)), "removed": sorted(set(before) - set(after)),
            "changed": sorted(changed_ids(before, after) & set(before) & set(after))}


def request_basis(record):
    return digest({key: record.get(key) for key in ("request_id", "kind", "query_or_identifier", "scope", "question_slugs")})


def outstanding(root, state, slug, manifest):
    acknowledged = (manifest or {}).get("revision_basis", {}).get("revision_id")
    result = {"revisions": [], "removed_computations": set(), "changed_policies": set(), "requests": {}}
    for record in reversed(state.get("research_revisions", [])):
        if record["revision_id"] == acknowledged:
            try:
                sibling("_pack_revision_guard").verify_migration(root, slug, record, manifest["revision_basis"])
            except (Exception, SystemExit):
                acknowledged = None
            else:
                break
        impact = record["impact"]
        question = next((row for row in impact["questions"] if row["slug"] == slug), None)
        if question is None:
            continue
        result["revisions"].append(record["revision_id"])
        result["removed_computations"].update(impact["computation"]["removed_definitions"])
        result["changed_policies"].update(impact["policy_ids"])
        result["requests"].update((row["request_id"], row) for row in impact["requests"] if row["removed"] and slug in row["question_slugs"])
        carried = question.get("migration_requirements", {})
        result["removed_computations"].update(carried.get("removed_computations", []))
        result["changed_policies"].update(carried.get("changed_policies", []))
        result["requests"].update((row["request_id"], row) for row in carried.get("removed_requests", []))
    return result


def inspect(root, state, incoming, effective, candidate_files, desired_files):
    root = Path(root)
    capture = capture_workspace(root)
    config = yaml.safe_load(capture.files["research.yml"])
    base = state["normalized_overlay"]
    delta = differences(base, incoming)
    actual = differences(config, effective)
    old_files = {row["path"]: row["sha256"] for row in state["revision_files"]}
    new_files = {key: hashlib.sha256(raw).hexdigest() for key, raw in candidate_files.items()}
    files = differences(old_files, new_files)
    require(len(delta) + len(actual) + len(files) <= MAX_CHANGES, "revision_change_bound")
    old_policies, new_policies = policies(config), policies(effective)
    policy_ids = changed_ids(old_policies, new_policies)
    kinds = sibling("_request_kinds")
    old_kinds, new_kinds = kinds.declared_pack_kinds(config), kinds.declared_pack_kinds(effective)
    kind_ids = changed_ids(old_kinds, new_kinds)
    requests_owner = sibling("source_requests")
    requests_path = requests_owner.requests_path(root, config).relative_to(root).as_posix()
    raw_requests = capture.files.get(requests_path, b"")
    requests = [json.loads(line) for line in raw_requests.splitlines() if line.strip()]
    require(len(requests) <= MAX_REQUESTS and all(isinstance(row, dict) for row in requests), "revision_request_bound_or_shape")
    affected_requests = [{"request_id": row["request_id"], "kind": row["kind"],
        "requirement_sha256": request_basis(row),
        "question_slugs": row.get("question_slugs", []), "removed": row["kind"] not in kinds.valid_kinds(effective)}
        for row in requests if row.get("kind") in kind_ids]
    request_questions = {slug for row in affected_requests for slug in row["question_slugs"]}
    coverage = sibling("coverage_manifest")
    q = sibling("question_status")
    prefix = q.questions_directory(root, config).relative_to(root).as_posix() + "/"
    candidates = {name: raw for name, raw in capture.files.items() if Path(name).parent.as_posix() + "/" == prefix and name.endswith(".md")}
    frontmatter = {name: q.frontmatter_from_text(raw.decode("utf-8")) for name, raw in candidates.items()}
    question_files = {name: raw for name, raw in candidates.items() if (frontmatter[name] or {}).get("type") == "question"}
    require(len(question_files) <= MAX_QUESTIONS, "revision_question_bound")
    # With no recorded template provenance, any changed template may have been copied.
    templates_changed = any("/coverage_templates" in row["path"] for row in actual)
    template_paths = set(config.get("domain_pack", {}).get("coverage_templates", {}).values())
    pack_prefix = state["pack"]["target_relative"] + "/"
    templates_changed |= any(pack_prefix + name in template_paths for name in desired_files)
    prose_files = [name for name in desired_files if name.endswith((".md", ".txt"))]
    scoped = ("/domain_pack/version", "/domain_pack/policy_vocabularies", "/domain_pack/policy_rules",
              "/domain_pack/request_kinds", "/domain_pack/coverage_templates", "/computation")
    global_change = bool(prose_files) or any(not row["path"].startswith(scoped) for row in actual)
    computation_changed = config.get("computation") != effective.get("computation")
    strict = config.get("strict_evidence") or effective.get("strict_evidence")
    claims = []
    if strict:
        raw = capture.files.get(strict["claims_path"])
        if raw is not None:
            claims = sibling("_strict_contract").claims_document(json.loads(raw))["claims"]
    calculation_claims = {row["id"] for row in claims if row.get("calculations")} if computation_changed else set()
    while True:
        more = {row["id"] for row in claims if set(row["premises"]) & calculation_claims}
        if more <= calculation_claims:
            break
        calculation_claims |= more
    computation_questions = {row["question_slug"] for row in claims if row["id"] in calculation_claims}
    strict_basis_changed = bool(strict) and bool(delta or files or actual or desired_files)
    questions, coverage_files, unresolved = [], [], []
    for name, raw in question_files.items():
        slug = Path(name).stem
        fm = frontmatter[name]
        path = coverage.selected_manifest_path(root, config, slug, fm.get("coverage_manifest")).relative_to(root).as_posix()
        manifest = yaml.safe_load(capture.files[path]) if path in capture.files else None
        require(manifest is None or isinstance(manifest, dict), "revision_coverage_shape")
        facets = [*(manifest or {}).get("required_facets", []), *(manifest or {}).get("optional_facets", [])]
        refs = {facet.get(field) for facet in facets for field in ("source_policy", "freshness_policy", "identity_policy")}
        reasons = []
        carried = outstanding(root, state, slug, manifest)
        if carried["revisions"]:
            reasons.append("prior_revision_migration_pending")
        if refs & policy_ids:
            reasons.append("policy_changed")
        if slug in request_questions:
            reasons.append("request_kind_changed")
        if templates_changed and manifest is not None:
            reasons.append("copied_template_requires_explicit_mapping")
        if global_change:
            reasons.append("guidance_or_unscoped_requirements_need_judgment")
        if slug in computation_questions or computation_changed and fm.get("status") in {"answered", "human_review"}:
            reasons.append("computation_basis_changed")
        if strict_basis_changed:
            reasons.append("strict_receipt_workspace_basis_changed")
        for identifier in sorted(refs & (set(old_policies) - set(new_policies))):
            unresolved.append({"kind": "removed_policy", "id": identifier, "question_slug": slug})
        if reasons:
            required_requests = {**carried["requests"], **{row["request_id"]: row for row in affected_requests if row["removed"] and slug in row["question_slugs"]}}
            questions.append({"slug": slug, "status": fm.get("status"), "reasons": reasons,
                "answer_sha256": hashlib.sha256(raw).hexdigest(), "coverage": path if manifest is not None else None,
                "migration_requirements": {"prior_revisions": carried["revisions"],
                    "removed_computations": sorted(carried["removed_computations"] | (computation_ids(config) - computation_ids(effective))),
                    "changed_policies": sorted(carried["changed_policies"] | policy_ids),
                    "removed_requests": [required_requests[key] for key in sorted(required_requests)]}})
            if manifest is not None:
                coverage_files.append(path)
    known = {Path(name).stem for name in question_files}
    for row in affected_requests:
        if row["removed"]:
            unresolved.append({"kind": "removed_request_kind", "id": row["kind"], "request_id": row["request_id"]})
        for slug in row["question_slugs"]:
            if slug not in known:
                unresolved.append({"kind": "missing_question", "id": slug, "request_id": row["request_id"]})
    affected_claims = [row["id"] for row in claims if row["question_slug"] in {q["slug"] for q in questions}]
    output = {"schema_version": "evidence-pack-impact/v1", "declarations": delta, "effective_changes": actual,
        "managed_files": files, "questions": questions, "coverage_files": sorted(set(coverage_files)),
        "requests": affected_requests, "claims": affected_claims, "calculation_claims": sorted(calculation_claims),
        "computation": {"changed": computation_changed, "declarations": differences(config.get("computation"), effective.get("computation")),
                        "removed_definitions": sorted(computation_ids(config) - computation_ids(effective)),
                        "current_definitions": sorted(computation_ids(effective)),
                        "values": "all_results_and_transitive_claims" if computation_changed else "unchanged", "effects_executed": False},
        "policy_ids": sorted(policy_ids), "removed_policy_ids": sorted(set(old_policies) - set(new_policies)),
        "identifiers": {"namespace": "pack:" + state["pack"]["name"], "policies": identifier_changes(old_policies, new_policies),
                        "request_kinds": identifier_changes(old_kinds, new_kinds)},
        "unresolved_references": unresolved, "guidance": sorted(prose_files),
        "bounds": {"workspace_files": sum(research_input(name) for name in capture.files), "questions_scanned": len(question_files), "questions_affected": len(questions),
                   "question_glob": prefix + "*.md", "question_files_scanned": len(candidates), "non_question_files": sorted(set(candidates) - set(question_files)),
                   "requests_scanned": len(requests), "requests_affected": len(affected_requests), "truncated": False,
                   "max_questions": MAX_QUESTIONS, "max_requests": MAX_REQUESTS, "max_changes": MAX_CHANGES},
        "structural_compatibility": "validated_by_pack_owner", "semantic_equivalence": "not_established",
        "receipt_scope": "workspace_wide" if strict else "selected_owner_scope",
        "external_assessments": {"scanned": False, "affected_count": None, "owner": "assessments plan-refresh",
                                 "reason": "requires_current_external_authority"},
        "limitations": ["Unrecorded prose dependencies are conservatively scoped to all questions.",
                       "Copied templates without provenance require explicit mapping; similarity is not authority.",
                       "Historical signed receipts retain their original scope; local migration cannot renew a review."]}
    require(len(canonical_bytes(output)) <= 1_048_576, "revision_impact_bytes_bound")
    history = {}
    for row in questions:
        path = prefix + row["slug"] + ".md"
        history[path] = capture.files[path].decode("utf-8")
        if row["coverage"] is not None:
            history[row["coverage"]] = capture.files[row["coverage"]].decode("utf-8")
    return output, inputs(capture), history


def validate_history(state):
    records = state.get("research_revisions", [])
    require(isinstance(records, list) and len(records) <= MAX_HISTORY, "revision_history_bound")
    for record in records:
        require(isinstance(record, dict) and set(record) == {"revision_id", "from", "to", "impact", "rationale", "qualification", "research_before"}, "revision_record_invalid")
        require(isinstance(record["research_before"], dict) and len(record["research_before"]) <= MAX_QUESTIONS * 2
                and all(isinstance(k, str) and isinstance(v, str) for k, v in record["research_before"].items()), "revision_history_invalid")
        require(record["revision_id"] == digest({k: v for k, v in record.items() if k != "revision_id"}), "revision_record_changed")
        require(record["impact"]["schema_version"] == "evidence-pack-impact/v1", "revision_impact_version")
    require(len(canonical_bytes(records)) <= 4_194_304, "revision_history_bytes_bound")
