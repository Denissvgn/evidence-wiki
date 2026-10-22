"""Lossless question semantics with independently derived native mirrors."""

from __future__ import annotations

import math
import re
from datetime import date, datetime, timezone
from pathlib import Path

import yaml
from _workspace_module_loader import load_workspace_module

SCRIPT_DIR = Path(__file__).resolve().parent
_SIBLINGS = {}
SCHEMA = "research-question-dependency-projection/v1"
COORDINATION = frozenset({
    "status", "claimed_by", "answered_by", "verified_by", "approved_by",
    "claimed_at", "grounding_verified_at", "approved_at", "human_review_requested_at",
    "updated", "human_review_status", "human_review_approved", "human_reviews",
})
MIRRORS = frozenset({
    "grounding", "source_ids", "answer_page", "coverage_required", "grounding_required",
    "coverage_manifest", "human_review_required", "human_review_policies",
})
STATUSES = frozenset({"open", "in_progress", "answered", "human_review", "blocked", "deferred", "rejected"})
_TIMES = frozenset({"claimed_at", "grounding_verified_at", "approved_at", "human_review_requested_at"})
_PEOPLE = frozenset({"claimed_by", "answered_by", "verified_by", "approved_by"})


def sibling(name):
    return load_workspace_module(SCRIPT_DIR, name, cache=_SIBLINGS)


def require(condition, reason):
    if not condition:
        raise sibling("_strict_evidence").refusal(reason)


def text(value):
    require(type(value) is str, "selective_question_string_required")
    value.encode("utf-8", errors="strict")
    require(len(value) <= 65_536, "selective_question_string_bound")
    return value


def typed_node(value, *, _depth=1, _count=None):
    """Encode native parsed values without scalar-type or presence collisions."""
    count = [0] if _count is None else _count
    count[0] += 1
    require(_depth <= 16 and count[0] <= 65_536, "selective_question_node_bound")
    kind = type(value)
    if value is None:
        return {"t": "null"}
    if kind is bool:
        return {"t": "boolean", "v": value}
    if kind is int:
        encoded = str(value)
        require(len(encoded) <= 4096, "selective_question_integer_bound")
        return {"t": "integer", "v": encoded}
    if kind is float:
        require(math.isfinite(value), "selective_question_nonfinite_float")
        return {"t": "float", "v": value.hex()}
    if kind is str:
        return {"t": "string", "v": text(value)}
    if kind is datetime:
        return {"t": "timestamp", "v": value.isoformat()}
    if kind is date:
        return {"t": "date", "v": value.isoformat()}
    if kind is list:
        require(len(value) <= 4096, "selective_question_list_bound")
        return {"t": "list", "v": [typed_node(item, _depth=_depth + 1, _count=count) for item in value]}
    require(kind is dict and len(value) <= 4096, "selective_question_mapping_unsupported")
    require(all(type(key) is str for key in value), "selective_question_mapping_key_unsupported")
    keys = sorted(value, key=lambda key: text(key).encode("utf-8"))
    count[0] += len(keys)
    require(count[0] <= 65_536, "selective_question_node_bound")
    return {"t": "mapping", "v": [[key, typed_node(value[key], _depth=_depth + 1, _count=count)] for key in keys]}


class _QuestionLoader(yaml.SafeLoader):
    """Reject mapping ambiguity before native YAML construction can collapse it."""

    def construct_mapping(self, node, deep=False):
        require(isinstance(node, yaml.MappingNode), "selective_question_mapping_invalid")
        keys = set()
        for key, _value in node.value:
            require(key.tag == "tag:yaml.org,2002:str", "selective_question_mapping_key_unsupported")
            require(key.value not in keys, "selective_question_duplicate_key")
            keys.add(key.value)
        return super().construct_mapping(node, deep=deep)


def parse_page(raw):
    """Retain the exact body slice, including its original newline bytes."""
    require(type(raw) is bytes and len(raw) <= 1_048_576, "selective_question_page_bound")
    source = raw.decode("utf-8", errors="strict")
    lines = source.splitlines(keepends=True)
    require(bool(lines) and lines[0] in {"---\n", "---\r\n", "---\r"}, "selective_question_frontmatter_missing")
    offset = len(lines[0])
    closing = None
    for line in lines[1:]:
        if line.rstrip("\r\n") == "---":
            closing = (offset, offset + len(line))
            break
        offset += len(line)
    require(closing is not None, "selective_question_frontmatter_missing")
    block = source[len(lines[0]):closing[0]]
    depth = count = 0
    for event in yaml.parse(block, Loader=yaml.SafeLoader):
        count += 1
        require(count <= 131_072, "selective_question_node_bound")
        require(not isinstance(event, yaml.AliasEvent) and getattr(event, "anchor", None) is None,
                "selective_question_yaml_alias_unsupported")
        if isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
            depth += 1
            require(depth <= 64, "selective_question_node_bound")
        elif isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
            depth -= 1
    frontmatter = yaml.load(block, Loader=_QuestionLoader)  # noqa: S506 -- restricted native SafeLoader
    require(type(frontmatter) is dict, "selective_question_frontmatter_invalid")
    typed_node(frontmatter)
    return frontmatter, source[closing[1]:]


def _nonempty(value):
    return type(value) is str and bool(value.strip())


def _timestamp(value):
    require(type(value) is str and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value) is not None,
            "selective_question_coordination_invalid")
    parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    require(parsed.strftime("%Y-%m-%dT%H:%M:%SZ") == value, "selective_question_coordination_invalid")


def validate_coordination(frontmatter):
    """Only exact, correctly typed lifecycle coordinates can be omitted."""
    require(type(frontmatter.get("status")) is str and frontmatter["status"] in STATUSES,
            "selective_question_status_invalid")
    for key in _PEOPLE & frontmatter.keys():
        require(_nonempty(frontmatter[key]), "selective_question_coordination_invalid")
    for key in _TIMES & frontmatter.keys():
        _timestamp(frontmatter[key])
    if "updated" in frontmatter:
        value = frontmatter["updated"]
        require(type(value) in {str, date}, "selective_question_coordination_invalid")
        if type(value) is str:
            require(re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is not None,
                    "selective_question_coordination_invalid")
            require(date.fromisoformat(value).isoformat() == value, "selective_question_coordination_invalid")
    if "human_review_status" in frontmatter:
        require(type(frontmatter["human_review_status"]) is str and frontmatter["human_review_status"] in {"pending", "approved", "rejected"},
                "selective_question_coordination_invalid")
    if "human_review_approved" in frontmatter:
        require(type(frontmatter["human_review_approved"]) is bool, "selective_question_coordination_invalid")
    if "human_reviews" in frontmatter:
        entries = frontmatter["human_reviews"]
        require(type(entries) is list and len(entries) <= 4096, "selective_question_coordination_invalid")
        required = {"policy", "verdict", "reviewed_by", "reviewed_at"}
        for entry in entries:
            require(type(entry) is dict and required <= entry.keys() <= required | {"review_ref", "note"},
                    "selective_question_coordination_invalid")
            require(all(_nonempty(value) for value in entry.values()), "selective_question_coordination_invalid")
            require(entry["verdict"] in {"accepted", "rejected"}, "selective_question_coordination_invalid")
            _timestamp(entry["reviewed_at"])


def validate_grounding(entries):
    """Validate closed entries before the permissive native verifier sees them."""
    require(type(entries) is list and len(entries) <= 1600, "selective_question_grounding_invalid")
    for entry in entries:
        require(type(entry) is dict, "selective_question_grounding_invalid")
        if "anchor" in entry:
            require(set(entry) == {"claim", "source_id", "anchor"}, "selective_question_grounding_invalid")
            anchor = entry["anchor"]
            require(type(anchor) is dict and set(anchor) == {"pointer", "expected"}, "selective_question_grounding_invalid")
            require(all(_nonempty(item) for item in anchor.values()), "selective_question_grounding_invalid")
        else:
            require({"claim", "source_id", "quote"} <= entry.keys() <= {"claim", "source_id", "quote", "location_hint"},
                    "selective_question_grounding_invalid")
            require(_nonempty(entry["quote"]), "selective_question_grounding_invalid")
            if "location_hint" in entry:
                require(type(entry["location_hint"]) is str, "selective_question_grounding_invalid")
        require(_nonempty(entry["claim"]) and _nonempty(entry["source_id"]), "selective_question_grounding_invalid")


def canonical_mirrors(root, config, policy, manifest, slug, coverage_summary):
    verifier = sibling("verify_quotes")
    entries = [sibling("_strict_evidence").grounding_entry(claim, evidence)
               for claim in manifest["claims"] if claim["question_slug"] == slug for evidence in claim["evidence"]]
    # Native evidence anchors may hold renderable scalars; the verifier owns their
    # conversion to the exact writable representation.
    parsed = verifier.grounding_entries({"grounding": entries}, slug)
    fields = sibling("question_resolve").GROUNDING_ENTRY_FIELDS
    grounding = [{key: entry[key] for key in fields if key in entry} for entry in parsed]
    validate_grounding(grounding)
    coverage_human, policies = sibling("question_resolve").requires_human_review(coverage_summary)
    path = sibling("coverage_manifest").manifest_path(root, config, slug).relative_to(root).as_posix()
    return {"grounding": grounding,
            "source_ids": sorted({item["source_id"] for claim in manifest["claims"] if claim["question_slug"] == slug for item in claim["evidence"]}),
            "answer_page": slug + ".md", "coverage_required": True, "grounding_required": True,
            "coverage_manifest": path, "human_review_required": policy["human_review"] or coverage_human,
            "human_review_policies": policies}, coverage_human


def _validate_mirrors(frontmatter, canonical, coverage_human, *, allow_stale_derived=False):
    pending = frontmatter["status"] in {"open", "in_progress"}
    materialized = frontmatter["status"] in {"answered", "human_review"}
    required = {"grounding", "source_ids", "answer_page", "coverage_required", "grounding_required", "coverage_manifest"}
    if materialized:
        require(required <= frontmatter.keys(), "selective_question_mirrors_missing")
        if coverage_human:
            require({"human_review_required", "human_review_policies"} <= frontmatter.keys(), "selective_question_mirrors_missing")
    for key in MIRRORS & frontmatter.keys():
        value = frontmatter[key]
        if key == "grounding":
            validate_grounding(value)
            if allow_stale_derived:
                for entry in value:
                    require(len(entry["claim"]) <= 4096 and len(entry["source_id"]) <= 256,
                            "selective_question_grounding_invalid")
                    if "anchor" in entry:
                        require(len(entry["anchor"]["pointer"]) <= 512 and len(entry["anchor"]["expected"]) <= 4096,
                                "selective_question_grounding_invalid")
                    else:
                        require(len(entry["quote"]) <= 4096 and len(entry.get("location_hint", "")) <= 512,
                                "selective_question_grounding_invalid")
        elif key in {"source_ids", "human_review_policies"}:
            require(type(value) is list and all(_nonempty(item) for item in value) and len(set(value)) == len(value),
                    "selective_question_mirror_invalid")
            if allow_stale_derived and key == "source_ids":
                require(len(value) <= 64 and all(len(item) <= 256 for item in value), "selective_question_mirror_invalid")
        elif key in {"coverage_required", "grounding_required", "human_review_required"}:
            require(type(value) is bool, "selective_question_mirror_invalid")
        else:
            require(_nonempty(value), "selective_question_mirror_invalid")
        if key in {"grounding", "source_ids"} and (allow_stale_derived or pending and value == []):
            continue
        require(value == canonical[key], "selective_question_mirror_mismatch")


def project(root, config, policy, capture, manifest, slug, *, coverage_summary):
    """Project a current captured question, independently of acceptance/history."""
    return _project(root, config, policy, capture, manifest, slug, coverage_summary=coverage_summary)


def project_dependencies(root, config, policy, capture, manifest, slug, target_claim_id, *, coverage_summary):
    """Derive review mirrors from the validated target and transitive premises.

    Materialized grounding and source IDs are derived display state. Their
    structure remains checked here; current publication verifies their evidence.
    """
    claims = sibling("_review_dependencies").closure(manifest, target_claim_id)
    return _project(root, config, policy, capture, manifest, slug,
                    coverage_summary=coverage_summary, dependency_claims=claims)


def _project(root, config, policy, capture, manifest, slug, *, coverage_summary, dependency_claims=None):
    root = Path(root)
    sibling("_strict_contract").claims_document(manifest)
    question = next((item for item in manifest["questions"] if item["slug"] == slug), None)
    require(question is not None, "selective_question_unknown")
    path = sibling("verify_quotes").question_path(root, config, slug).relative_to(root).as_posix()
    sibling("_record_artifacts").artifact_path(path)
    require(path in capture.files, "selective_question_missing")
    frontmatter, body = parse_page(capture.files[path])
    require(frontmatter.get("type") == "question", "selective_question_type_invalid")
    require((frontmatter.get("question") or frontmatter.get("summary")) == question["question"], "strict_question_text_changed")
    validate_coordination(frontmatter)
    if "confidence" in frontmatter:
        require(type(frontmatter["confidence"]) is str and frontmatter["confidence"] in {"high", "medium", "low"}, "selective_question_confidence_invalid")
    if "evidence_strength" in frontmatter:
        require(type(frontmatter["evidence_strength"]) is str and frontmatter["evidence_strength"] in {"corroborated", "single_source", "contested"},
                "selective_question_evidence_strength_invalid")
    selected_manifest = manifest if dependency_claims is None else {**manifest, "claims": dependency_claims}
    canonical, coverage_human = canonical_mirrors(root, config, policy, selected_manifest, slug, coverage_summary)
    _validate_mirrors(frontmatter, canonical, coverage_human, allow_stale_derived=dependency_claims is not None)
    semantic = {key: value for key, value in frontmatter.items() if key not in COORDINATION | MIRRORS}
    return {"manifest_question": question, "path": path, "semantic_frontmatter": typed_node(semantic),
            "body": body, "canonical_mirrors": canonical}


def typed_value(node, *, _depth=1, _count=None):
    """Decode only canonical closed nodes, checking a lossless round trip."""
    count = [0] if _count is None else _count
    count[0] += 1
    require(_depth <= 16 and count[0] <= 65_536 and type(node) is dict, "selective_question_node_invalid")
    tag = node.get("t")
    require(set(node) == ({"t"} if tag == "null" else {"t", "v"}), "selective_question_node_invalid")
    value = node.get("v")
    if tag == "null":
        result = None
    elif tag == "boolean":
        require(type(value) is bool, "selective_question_node_invalid")
        result = value
    elif tag == "integer":
        require(type(value) is str and len(value) <= 4096 and re.fullmatch(r"0|-?[1-9][0-9]*", value) is not None,
                "selective_question_node_invalid")
        result = int(value)
    elif tag == "float":
        require(type(value) is str and len(value) <= 64, "selective_question_node_invalid")
        result = float.fromhex(value)
    elif tag == "string":
        result = text(value)
    elif tag == "date":
        require(type(value) is str, "selective_question_node_invalid")
        result = date.fromisoformat(value)
    elif tag == "timestamp":
        require(type(value) is str and len(value) <= 128, "selective_question_node_invalid")
        result = datetime.fromisoformat(value)
    elif tag == "list":
        require(type(value) is list and len(value) <= 4096, "selective_question_node_invalid")
        result = [typed_value(item, _depth=_depth + 1, _count=count) for item in value]
    else:
        require(tag == "mapping" and type(value) is list and len(value) <= 4096, "selective_question_node_invalid")
        require(all(type(pair) is list and len(pair) == 2 and type(pair[0]) is str for pair in value), "selective_question_node_invalid")
        keys = [pair[0] for pair in value]
        count[0] += len(keys)
        require(count[0] <= 65_536, "selective_question_node_bound")
        require(keys == sorted(set(keys), key=lambda key: text(key).encode("utf-8")), "selective_question_node_invalid")
        result = {key: typed_value(item, _depth=_depth + 1, _count=count) for key, item in value}
    require(typed_node(result) == node, "selective_question_node_noncanonical")
    return result
