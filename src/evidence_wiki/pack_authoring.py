"""Deterministic guidance scaffolds and new candidate copies with explicit ancestry."""

from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path

import yaml

from ._pack_io import PackSnapshot, capture_pack, relative_path, yaml_document
from ._script_host import shared_assets_root
from .pack_authoring_contracts import DERIVE, SPEC, decode, digest, refuse
from .pack_authoring_store import create
from .pack_discovery import _materialize, owner, safe_name, select
from .source_commands import _no_secret_values


def _slug(value):
    import re

    if not isinstance(value, str) or re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value) is None:
        refuse("authoring_slug_invalid")
    return value


def _markdown(title, sections):
    lines = ["# " + title, "", "Caller-declared reusable guidance; domain review remains separate from structural validation.", ""]
    for name, value in sections.items():
        lines += ["## " + name, "", "````json", json.dumps(value, ensure_ascii=False, indent=2), "````", ""]
    return "\n".join(lines).encode()


def _requirements(rows):
    ids = [row["id"] for row in rows]
    if len(ids) != len(set(ids)):
        refuse("authoring_duplicate_requirement")


def validate_files(name, files):
    """Apply the canonical inert-tree boundary before any caller-directory writes."""
    if len(files) > 256 or sum(map(len, files.values())) > 8_388_608:
        refuse("authoring_tree_bound")
    guidance_files(files)
    with tempfile.TemporaryDirectory(prefix="evidence-pack-shape-") as temporary:
        path = Path(temporary) / name
        for relative, raw in files.items():
            relative_path(relative)
            if len(raw) > 1_048_576 or b"\0" in raw:
                refuse("authoring_file_bound_or_binary")
        _materialize(path, files)
        snapshot = capture_pack(path)
        initializer = owner("init_research_workspace")
        initializer.validate_domain_pack_references(path)
        initializer.validate_domain_pack_data_model(yaml_document(files["research.overlay.yml"]))
        from .domain_pack_validator import validate_domain_pack

        report = validate_domain_pack(str(path), root=shared_assets_root())
        if not report["ok"]:
            failed = next((row["id"] for row in report["checks"] if row["status"] != "pass"), "canonical_validation")
            refuse("authoring_preflight/" + failed)
        return snapshot.tree_sha256


def _namespaces(config):
    from . import domain_pack_validator

    scripts = domain_pack_validator.load_scripts(shared_assets_root() / "workspace-template")
    _, check = domain_pack_validator.policy_vocabularies_check(scripts, config["domain_pack"])
    if check["status"] != "pass":
        refuse("authoring_policy_vocabulary_invalid")
    owner("coverage_manifest").merged_policy_vocabularies(config)
    owner("_policy_primitives").pack_policy_rules(config)
    owner("_request_kinds").declared_pack_kinds(config)


def guidance_files(files):
    """Authoring admits guidance/configuration data, never execution configuration."""
    overlay = yaml_document(files["research.overlay.yml"])
    _no_secret_values([raw.decode("utf-8") for raw in files.values()])
    if not isinstance(overlay, dict) or set(overlay) - {"domain_pack", "project", "wiki", "taxonomy", "computation"}:
        refuse("authoring_overlay_outside_guidance_scope")
    wiki = overlay.get("wiki", {})
    if not isinstance(wiki, dict) or set(wiki) - {"required_dirs", "allowed_page_types", "frontmatter_type_rules"}:
        refuse("authoring_workspace_mechanics_override")
    for relative in files:
        if Path(relative).parts[0] in {"scripts", "raw", "sources", "runs", "wiki", "tests", "fixtures"} or relative == "records.json":
            refuse("authoring_runtime_or_evaluation_data_inside_pack")
    return overlay


def render(spec):
    name = _slug(spec["name"])
    if spec["version"] != spec["version"].strip():
        refuse("authoring_version_whitespace")
    _requirements(spec["requirements"])
    for field in ("intended_users", "question_classes", "source_classes", "claim_types", "extraction_targets"):
        if not spec[field]:
            refuse("authoring_required_guidance/" + field)
    if not spec["taxonomy"]:
        refuse("authoring_required_guidance/taxonomy")
    claim_names = [row["name"] for row in spec["claim_fields"]]
    if len(claim_names) != len(set(claim_names)):
        refuse("authoring_duplicate_claim_field")
    files = {
        "README.md": _markdown(name, {"Scope": spec["scope"], "Exclusions": spec["exclusions"], "Intended users": spec["intended_users"],
            "Question classes": spec["question_classes"], "Source classes": spec["source_classes"], "Review requirements": spec["review_requirements"],
            "Unresolved guidance": spec["unresolved"]}),
        "taxonomy.md": _markdown("Taxonomy", {"Page placement": spec["taxonomy"], "Extraction targets": spec["extraction_targets"],
            "Filing rules": spec["filing_rules"], "Outputs": spec["outputs"]}),
        "claims.md": _markdown("Claim requirements", {"Claim types": spec["claim_types"], "Fields": spec["claim_fields"],
            "Interpretation limits": spec["review_requirements"]}),
    }
    domain = {"name": name, "version": spec["version"], "description": spec["description"],
        "compatible_research_yml_contract": owner("init_research_workspace").SUPPORTED_RESEARCH_YML_CONTRACTS[0],
        "taxonomy_doc": "taxonomy.md", "claims_doc": "claims.md", "human_gated": spec["human_gated"],
        "selection": {"schema_version": "1.0", "typical_questions": spec["question_classes"], "exclusions": spec["exclusions"],
            "required_scope_inputs": spec["required_scope_inputs"], "review_requirements": spec["review_requirements"]},
        "applicable_source_types": spec["source_classes"], "page_taxonomy": {key: row["description"] for key, row in spec["taxonomy"].items()},
        "extraction_targets": spec["extraction_targets"], "recommended_synthesis_outputs": spec["outputs"],
        "policy_vocabularies": spec["policies"], "policy_rules": spec["policy_rules"], "request_kinds": spec["request_kinds"],
        "recommended_discovery": spec["recommended_providers"]["discovery"], "recommended_acquisition": spec["recommended_providers"]["acquisition"],
        "scaffolds": {}, "coverage_templates": {}, "planned_files": []}
    for key, content in spec["scaffolds"].items():
        relative = "scaffolds/" + _slug(key) + ".md"
        files[relative] = content.encode()
        domain["scaffolds"][key] = relative
    config = {"domain_pack": domain}
    _namespaces(config)
    coverage = owner("coverage_manifest")
    vocab = coverage.merged_policy_vocabularies(config)
    for key, declaration in spec["coverage_templates"].items():
        normalized = coverage.normalize_template_document(declaration, policy_vocabularies=vocab)
        ids = []
        for facet in normalized["required_facets"] + normalized["optional_facets"]:
            ids.append(facet["facet_id"])
            if facet["accepted_source_ids"] or facet["blocking_request_ids"] or facet["facet_verdict"] != "pending":
                refuse("authoring_template_contains_evidence")
        if len(ids) != len(set(ids)):
            refuse("authoring_duplicate_facet")
        relative = "coverage-templates/" + _slug(key) + ".yml"
        files[relative] = yaml.safe_dump(normalized, sort_keys=True, allow_unicode=True).encode()
        domain["coverage_templates"][key] = relative
    domain["implemented_files"] = sorted(files)
    starter = yaml_document((shared_assets_root() / "workspace-template/research.yml").read_bytes())
    directories, page_types = list(starter["wiki"]["required_dirs"]), list(starter["wiki"]["allowed_page_types"])
    for directory, declaration in spec["taxonomy"].items():
        _slug(directory)
        if directory not in directories:
            directories.append(directory)
        if declaration["page_type"] not in page_types:
            page_types.append(declaration["page_type"])
    existing = starter["wiki"]["frontmatter_type_rules"].get("claim", {})
    for field in spec["claim_fields"]:
        if field["name"] in existing.get("field_types", {}) and field["type"] != existing["field_types"][field["name"]]:
            refuse("authoring_core_claim_field_type_changed")
        if field["name"] in existing.get("required_fields", []) and not field["required"]:
            refuse("authoring_core_claim_requirement_weakened")
    rules = copy.deepcopy(existing)
    rules["required_fields"] = sorted(set(existing.get("required_fields", [])) | {row["name"] for row in spec["claim_fields"] if row["required"]})
    rules["field_types"] = {**existing.get("field_types", {}), **{row["name"]: row["type"] for row in spec["claim_fields"]}}
    rules.setdefault("allowed_values", {})["claim_type"] = spec["claim_types"]
    config["wiki"] = {"required_dirs": directories, "allowed_page_types": page_types, "frontmatter_type_rules": {"claim": rules}}
    config["taxonomy"] = {"claim_types": list(spec["claim_types"])}
    if spec["computation"] is not None:
        config["computation"] = spec["computation"]
        owner("_computation_runtime").load_definition(owner("init_research_workspace").deep_merge(starter, config))
    files["research.overlay.yml"] = yaml.safe_dump(config, sort_keys=True, allow_unicode=True).encode()
    validate_files(name, files)
    return files


def scaffold(raw, *, output):
    spec = decode(raw, SPEC)
    files = render(spec)
    tree = PackSnapshot(Path(spec["name"]), files, {}).tree_sha256
    draft = {"schema_version": "evidence-pack-draft/v1", "kind": "new", "name": spec["name"], "pack_relative": "packs/" + spec["name"],
        "specification": spec, "specification_sha256": digest(spec), "initial_tree_sha256": tree, "base": None,
        "requirements": spec["requirements"], "rationale": spec["scope"], "unresolved": spec["unresolved"],
        "classification": {"kind": "new_guidance", "routine_repair": False, "domain_review": "required"}}
    return create(output, spec["name"], files, draft)


def derive(raw, *, output):
    request = decode(raw, DERIVE)
    if request["version"] != request["version"].strip():
        refuse("authoring_version_whitespace")
    _requirements(request["requirements"])
    base = request["base"]
    if (base["selector"] is None) == (base["path"] is None):
        refuse("authoring_base_requires_one_locator")
    row, path = select(base["selector"], path=base["path"], target=base["target"], catalog=base["catalog"])
    if row["state"] != "available":
        refuse("authoring_base_unavailable")
    if row["origin"] == "installed" and row["lifecycle"]["state"] not in {"current", "local_modifications", "legacy_untracked"}:
        refuse("authoring_installed_base_unstable")
    before = capture_pack(path)
    if before.tree_sha256 != base["tree_sha256"]:
        refuse("authoring_base_changed", "ONBOARDING_PLAN_STALE")
    prior = yaml_document(before.files["research.overlay.yml"])
    old_name, name = prior["domain_pack"]["name"], request["name"]
    safe_name(name)
    if request["mode"] == "specialization":
        _slug(name)
    if (request["mode"] == "revision") != (name == old_name):
        refuse("authoring_identity_requires_explicit_specialization")
    if request["mode"] == "revision" and request["version"] == prior["domain_pack"]["version"]:
        refuse("authoring_revision_version_unchanged")
    files = dict(before.files)
    if name != old_name:
        files = {key: value.replace(("pack:" + old_name + "/").encode(), ("pack:" + name + "/").encode()) for key, value in files.items()}
    seen = set()
    for change in request["changes"]:
        relative = relative_path(change["path"])
        if relative in seen:
            refuse("authoring_duplicate_changed_path")
        seen.add(relative)
        if change["content"] is None:
            if relative not in files:
                refuse("authoring_delete_missing_file")
            del files[relative]
        else:
            files[relative] = change["content"].encode()
    if "research.overlay.yml" not in files:
        refuse("authoring_overlay_required")
    overlay = yaml_document(files["research.overlay.yml"])
    overlay["domain_pack"].update(name=name, version=request["version"])
    _namespaces(overlay)
    if name != old_name and any(("pack:" + old_name + "/").encode() in raw for raw in files.values()):
        refuse("authoring_old_namespace_retained")
    lifecycle = owner("_domain_pack_lifecycle")
    reader = lifecycle.YAML(typ="rt", pure=True)
    reader.preserve_quotes = True
    document = reader.load(files["research.overlay.yml"].decode("utf-8"))
    document["domain_pack"]["name"] = name
    document["domain_pack"]["version"] = request["version"]
    files["research.overlay.yml"] = lifecycle._render_round_trip(document).encode("utf-8")
    tree = validate_files(name, files)
    if capture_pack(path).tree_sha256 != before.tree_sha256:
        refuse("authoring_base_changed", "ONBOARDING_PLAN_STALE")
    frozen_base = {"selection": base, "name": old_name, "version": prior["domain_pack"]["version"], "tree_sha256": before.tree_sha256,
        "files": {key: digest(value.decode()) for key, value in before.files.items()}, "overlay": prior}
    draft = {"schema_version": "evidence-pack-draft/v1", "kind": request["mode"], "name": name, "pack_relative": "packs/" + name,
        "specification": request, "specification_sha256": digest(request), "initial_tree_sha256": tree, "base": frozen_base,
        "requirements": request["requirements"], "rationale": request["rationale"], "unresolved": request["unresolved"],
        "classification": {"kind": "requirements_change", "routine_repair": False, "weakening": "unassessed", "domain_review": "required",
            "namespace_rewrite": {"from": "pack:" + old_name + "/", "to": "pack:" + name + "/"} if name != old_name else None,
            "changed_files": sorted(key for key in set(files) | set(before.files) if files.get(key) != before.files.get(key))}}
    return create(output, name, files, draft)
