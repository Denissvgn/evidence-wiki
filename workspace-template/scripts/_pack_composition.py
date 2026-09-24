#!/usr/bin/env python3
"""Verify pinned, non-executable composition members and compiled declarations."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import PurePosixPath

import yaml
from _computation_contract import validate_yaml
from _record_artifacts import json_document
from _request_kinds import declared_pack_kinds

SCHEMA = "evidence-pack-composition/v1"


def tree_id(files):
    value = hashlib.sha256()
    for name, raw in sorted(files.items()):
        value.update((name + "\0" + hashlib.sha256(raw).hexdigest() + "\n").encode())
    return value.hexdigest()


def verify(files):
    raw = files.get("composition.lock.json")
    if raw is None:
        return
    if len(raw) > 1_048_576:
        raise ValueError("composition_manifest_bound")
    value = json_document(raw)
    if not isinstance(value, dict) or set(value) != {"schema_version", "name", "version", "members", "generated", "policy_map", "scope", "semantic_adequacy"}:
        raise ValueError("composition_manifest_shape")
    if value["schema_version"] != SCHEMA or value["semantic_adequacy"] != "not_certified":
        raise ValueError("composition_manifest_version")
    members, generated = value["members"], value["generated"]
    if not isinstance(members, list) or not 2 <= len(members) <= 8 or not isinstance(generated, dict):
        raise ValueError("composition_members_bound")
    expected = dict(generated)
    aliases = set()
    sources = []
    for member in members:
        if not isinstance(member, dict) or set(member) != {"alias", "name", "version", "tree_sha256", "files", "applicability"}:
            raise ValueError("composition_member_shape")
        alias = member["alias"]
        if not isinstance(alias, str) or re.fullmatch(r"[a-z][a-z0-9-]{0,31}", alias) is None or alias in aliases:
            raise ValueError("composition_member_alias")
        aliases.add(alias)
        prefix = "members/" + alias + "/"
        selected = {name[len(prefix):]: data for name, data in files.items() if name.startswith(prefix)}
        hashes = {name: hashlib.sha256(data).hexdigest() for name, data in selected.items()}
        if hashes != member["files"] or tree_id(selected) != member["tree_sha256"]:
            raise ValueError("composition_member_changed")
        expected.update({prefix + name: sha for name, sha in hashes.items()})
        sources.append({"alias": alias, "applicability": member["applicability"], "files": selected})
    actual = {name: hashlib.sha256(data).hexdigest() for name, data in files.items() if name != "composition.lock.json"}
    if actual != expected or "research.overlay.yml" not in generated:
        raise ValueError("composition_compiled_output_changed")
    if build({key: value[key] for key in ("name", "version", "scope")}, sources) != files:
        raise ValueError("composition_does_not_match_member_contracts")


def refuse(reason, code=None):
    raise ValueError(reason)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def relative_path(name):
    if not isinstance(name, str) or PurePosixPath(name).is_absolute() or any(p in ("", ".", "..") for p in name.split("/")) or "\\" in name or ":" in name:
        refuse("composition_relative_path")
    return name


def yaml_document(raw):
    validate_yaml(raw.decode("utf-8"))
    value = yaml.safe_load(raw)
    if not isinstance(value, dict):
        refuse("composition_yaml_mapping_required")
    return value


def _merge(left, right, path=""):
    if isinstance(left, dict) and isinstance(right, dict):
        result = copy.deepcopy(left)
        for key, value in right.items():
            result[key] = _merge(result[key], value, path + "/" + key) if key in result else copy.deepcopy(value)
        return result
    if left == right:
        return copy.deepcopy(left)
    if path in {"/wiki/allowed_page_types", "/wiki/required_dirs"} and isinstance(left, list) and isinstance(right, list):
        return sorted(set(left) | set(right))
    refuse("composition_conflicting_declaration:" + path, "ONBOARDING_TARGET_CONFLICT")


def build(value, sources):
    files, members, policy_map = {}, [], {}
    effective = {}
    metadata = {"name": value["name"], "version": value["version"], "description": value["scope"],
        "compatible_research_yml_contract": None, "human_gated": False,
        "taxonomy_doc": "taxonomy.md", "claims_doc": "claims.md", "scaffolds": {}, "coverage_templates": {},
        "policy_vocabularies": {}, "policy_rules": {}, "request_kinds": [], "page_taxonomy": {}}
    computations = None
    selections = []
    for member in sources:
        source_files = member["files"]
        overlay = yaml_document(source_files["research.overlay.yml"])
        pack = overlay["domain_pack"]
        supported = {"name", "version", "description", "compatible_research_yml_contract", "human_gated",
            "taxonomy_doc", "claims_doc", "scaffolds", "coverage_templates", "policy_vocabularies", "policy_rules",
            "request_kinds", "page_taxonomy", "selection", "implemented_files", "planned_files",
            "applicable_source_types", "recommended_acquisition", "recommended_discovery",
            "extraction_targets", "recommended_synthesis_outputs"}
        if set(pack) - supported:
            refuse("composition_unsupported_member_metadata")
        if pack.get("composition") or "composition.lock.json" in source_files:
            refuse("composition_nested_member_unsupported")
        contract = pack["compatible_research_yml_contract"]
        if metadata["compatible_research_yml_contract"] not in (None, contract):
            refuse("composition_research_contract_mismatch")
        metadata["compatible_research_yml_contract"] = contract
        alias = member["alias"]
        prefix = "members/" + alias + "/"
        files.update({prefix + name: raw for name, raw in source_files.items()})
        binding = {"alias": alias, "name": pack["name"], "version": pack["version"],
            "tree_sha256": tree_id(source_files), "files": {name: hashlib.sha256(raw).hexdigest() for name, raw in source_files.items()},
            "applicability": member["applicability"]}
        members.append(binding)
        translations = {}
        for group in pack.get("policy_vocabularies", {}).values():
            translations.update({key: "pack:" + value["name"] + "/" + alias + "." + key.split("/", 1)[1] for key in group})
        kinds = declared_pack_kinds(overlay)
        translations.update({key: "pack:" + value["name"] + "/" + alias + "." + key.split("/", 1)[1] for key in kinds})
        policy_map[alias] = translations
        for field, declarations in pack.get("policy_vocabularies", {}).items():
            metadata["policy_vocabularies"].setdefault(field, {}).update({translations[key]: text for key, text in declarations.items()})
        metadata["policy_rules"].update({translations[key]: rule for key, rule in pack.get("policy_rules", {}).items()})
        metadata["request_kinds"].extend({"id": translations[key], **spec} for key, spec in kinds.items())
        metadata["page_taxonomy"].update({alias + "." + key: text for key, text in pack.get("page_taxonomy", {}).items()})
        metadata["human_gated"] |= bool(pack.get("human_gated", False))
        for field in ("applicable_source_types", "recommended_acquisition", "recommended_discovery"):
            metadata[field] = sorted(set(metadata.get(field, [])) | set(pack.get(field, [])))
        for field in ("extraction_targets", "recommended_synthesis_outputs"):
            metadata.setdefault(field, []).extend(alias + "." + item for item in pack.get(field, []))
        for key, path in pack.get("scaffolds", {}).items():
            metadata["scaffolds"][alias + "." + key] = prefix + relative_path(path)
        for key, path in pack.get("coverage_templates", {}).items():
            document = yaml_document(source_files[relative_path(path)])
            for facet in document.get("required_facets", []) + document.get("optional_facets", []):
                for field in ("source_policy", "freshness_policy", "identity_policy", "request_kind"):
                    if facet.get(field) in translations:
                        facet[field] = translations[facet[field]]
            output = "coverage-templates/" + alias + "-" + key + ".yml"
            relative_path(output)
            files[output] = yaml.safe_dump(document, sort_keys=False, allow_unicode=True).encode()
            metadata["coverage_templates"][alias + "." + key] = output
        if pack.get("selection"):
            selections.append((alias, pack["selection"]))
        remainder = {key: data for key, data in overlay.items() if key not in {"project", "domain_pack", "computation"}}
        effective = _merge(effective, remainder)
        computation = overlay.get("computation")
        if computation is not None:
            if computations is None:
                computations = copy.deepcopy(computation)
            else:
                sections = {"tables", "aggregations", "graphs", "invariants", "cadence"}
                common = _merge({k: v for k, v in computations.items() if k not in sections},
                                {k: v for k, v in computation.items() if k not in sections}, "/computation")
                computations.update(common)
                for section in sections:
                    if set(computations.get(section, {})) & set(computation.get(section, {})):
                        refuse("composition_computation_identifier_collision:" + section, "ONBOARDING_TARGET_CONFLICT")
                    computations.setdefault(section, {}).update(copy.deepcopy(computation.get(section, {})))
    if selections:
        metadata["selection"] = {"schema_version": "1.0",
            "typical_questions": ["[" + alias + "] " + text for alias, selection in selections for text in selection["typical_questions"]],
            "exclusions": ["[" + alias + "] " + text for alias, selection in selections for text in selection["exclusions"]],
            "required_scope_inputs": [{"id": alias + "-" + row["id"], "description": row["description"]}
                for alias, selection in selections for row in selection["required_scope_inputs"]],
            "review_requirements": ["[" + alias + "] " + text for alias, selection in selections for text in selection["review_requirements"]]}
    metadata["composition"] = {"schema_version": SCHEMA, "lock": "composition.lock.json"}
    metadata["implemented_files"] = ["taxonomy.md", "claims.md"]
    effective["domain_pack"] = metadata
    if computations is not None:
        effective["computation"] = computations
    text = "\n".join("- " + m["alias"] + ": " + m["applicability"] for m in members)
    for output, field, title in (("taxonomy.md", "taxonomy_doc", "Composed scope"), ("claims.md", "claims_doc", "Evidence requirements")):
        links = []
        for source in sources:
            pack = yaml_document(source["files"]["research.overlay.yml"])["domain_pack"]
            if pack.get(field):
                links.append("- [" + source["alias"] + "](members/" + source["alias"] + "/" + relative_path(pack[field]) + ")")
        files[output] = ("# " + title + "\n\n" + text + "\n\nRead every applicable member's retained guidance:\n\n" + "\n".join(links)
            + "\n\nAll applicable evidence and review requirements remain required. Use the lock's identifier mapping and compiled templates. Composition is not semantic certification.\n").encode()
    files["README.md"] = ("# " + value["name"] + "\n\n" + value["scope"] + "\n\nRead composition.lock.json for pinned members and effective identifier mappings. Recompose and review a new whole revision when any member changes.\n").encode()
    files["research.overlay.yml"] = yaml.safe_dump(effective, sort_keys=False, allow_unicode=True).encode()
    lock = {"schema_version": SCHEMA, "name": value["name"], "version": value["version"], "members": members,
            "generated": {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items() if not name.startswith("members/")},
            "policy_map": policy_map, "scope": value["scope"], "semantic_adequacy": "not_certified"}
    files["composition.lock.json"] = canonical(lock)
    return files
