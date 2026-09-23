#!/usr/bin/env python3
"""Bounded, inert host text captures with declared origin and coverage limits."""

from __future__ import annotations

import copy
import hashlib
import os
import re
import stat
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import yaml
from _workspace_module_loader import load_workspace_module

SCHEMA = "evidence-host-capture/v1"
MAX_BYTES = 524_288
CONTENT_KINDS = ("primary", "excerpt", "search_snippet", "generated_summary")
SUFFIXES = frozenset({".md", ".markdown", ".mdown", ".txt"})
FIELDS = frozenset({"schema_version", "capture_id", "tool_id", "tool_version", "origin_url", "title", "retrieved_at",
                    "capture_method", "content_format", "content_kind", "completeness", "completeness_note", "rights",
                    "scope", "request_id", "content_sha256", "content_bytes"})
_SCRIPT_DIR = Path(__file__).resolve().parent


def schema():
    def string(maximum=1024):
        return {"type": "string", "minLength": 1, "maxLength": maximum}
    def obj(**fields):
        return {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}
    nullable = lambda item: {"anyOf": [item, {"type": "null"}]}
    identity = {**string(64), "pattern": r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$"}
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", **obj(
        schema_version={"const": SCHEMA}, capture_id=identity, tool_id=identity, tool_version=string(128),
        origin_url=string(4096), title=string(), retrieved_at=string(64),
        capture_method={"enum": ["browser_visible_text", "connector_export", "manual_transcription"]},
        content_format={"enum": ["markdown", "plain_text"]}, content_kind={"enum": list(CONTENT_KINDS)},
        completeness={"enum": ["complete", "partial", "unknown"]}, completeness_note=string(),
        rights=obj(status={"enum": ["allowed", "restricted", "unknown"]}, license=nullable(string(256)),
                   terms_url=nullable(string(4096)), note=string()),
        scope={"type": "object", "maxProperties": 16, "additionalProperties": string()}, request_id=nullable(identity),
        content_sha256={"type": "string", "pattern": r"^sha256:[a-f0-9]{64}$"},
        content_bytes={"type": "integer", "minimum": 0, "maximum": MAX_BYTES})}


class CaptureInvalid(ValueError):
    """A content-free capture refusal; supplied source text is never a diagnostic."""


def require(condition, reason):
    if not condition:
        raise CaptureInvalid(reason)


def text(value, maximum=1024, *, empty=False):
    require(isinstance(value, str) and len(value) <= maximum and (empty or bool(value.strip()))
            and not any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in value), "capture_text_invalid")
    return value


def identifier(value):
    text(value, 64)
    require(re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}", value) is not None, "capture_id_invalid")
    return value


def origin(value):
    text(value, 4096)
    try:
        parsed = urlsplit(value)
        require(parsed.scheme in {"https", "http", "connector"} and bool(parsed.hostname)
                and parsed.username is None and parsed.password is None, "capture_origin_invalid")
        require(not any(re.search(r"(?i)token|secret|password|credential|api.?key|signature", key)
                        for key, _ in parse_qsl(parsed.query)), "capture_origin_credential_forbidden")
    except ValueError:
        raise CaptureInvalid("capture_origin_invalid") from None
    return value


def validate(value):
    require(isinstance(value, dict) and set(value) == FIELDS and value.get("schema_version") == SCHEMA, "capture_shape_invalid")
    for key in ("capture_id", "tool_id"):
        identifier(value[key])
    text(value["tool_version"], 128)
    origin(value["origin_url"])
    text(value["title"])
    text(value["retrieved_at"], 64)
    try:
        timestamp = datetime.fromisoformat(value["retrieved_at"].replace("Z", "+00:00"))
        require(timestamp.tzinfo is not None, "capture_time_requires_timezone")
    except ValueError:
        raise CaptureInvalid("capture_time_invalid") from None
    require(value["capture_method"] in {"browser_visible_text", "connector_export", "manual_transcription"}, "capture_method_invalid")
    require(value["content_format"] in {"markdown", "plain_text"} and value["content_kind"] in CONTENT_KINDS, "capture_content_kind_invalid")
    require(value["completeness"] in {"complete", "partial", "unknown"}, "capture_completeness_invalid")
    text(value["completeness_note"])
    rights = value["rights"]
    require(isinstance(rights, dict) and set(rights) == {"status", "license", "terms_url", "note"}, "capture_rights_invalid")
    require(rights["status"] in {"allowed", "restricted", "unknown"}, "capture_rights_invalid")
    text(rights["note"])
    if rights["license"] is not None:
        text(rights["license"], 256)
    if rights["terms_url"] is not None:
        origin(rights["terms_url"])
    scope = value["scope"]
    require(isinstance(scope, dict) and len(scope) <= 16, "capture_scope_invalid")
    for key, item in scope.items():
        require(isinstance(key, str) and re.fullmatch(r"[a-z0-9_][a-z0-9._-]{0,63}", key) is not None, "capture_scope_invalid")
        text(item)
    if value["request_id"] is not None:
        identifier(value["request_id"])
    require(isinstance(value["content_sha256"], str) and re.fullmatch(r"sha256:[a-f0-9]{64}", value["content_sha256"]) is not None,
            "capture_digest_invalid")
    require(type(value["content_bytes"]) is int and 0 <= value["content_bytes"] <= MAX_BYTES, "capture_bytes_bound")
    return copy.deepcopy(value)


def read(root: Path, relative: str, maximum=MAX_BYTES):
    require(isinstance(relative, str) and not Path(relative).is_absolute() and "\\" not in relative
            and all(part not in {"", ".", ".."} for part in relative.split("/")), "capture_path_invalid")
    path = root / relative
    try:
        info = path.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= maximum, "capture_file_unsafe_or_large")
        revision = load_workspace_module(_SCRIPT_DIR, "_evidence_revision")
        if os.name == "nt":
            reader = load_workspace_module(_SCRIPT_DIR, "_windows_files")
            raw = reader.read_legacy_file(root, relative, revision.observation(info), maximum)
        else:
            raw = revision.read_observed_file(root, relative, revision.observation(info))
        require(len(raw) <= maximum, "capture_bytes_bound")
        return raw
    except CaptureInvalid:
        raise
    except Exception:
        raise CaptureInvalid("capture_file_unavailable_or_changed") from None


def sidecar(raw):
    require(isinstance(raw, bytes) and 0 < len(raw) <= MAX_BYTES, "capture_sidecar_bound")
    class Loader(yaml.SafeLoader):
        pass
    def mapping(loader, node):
        result = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node)
            require(isinstance(key, str) and key not in result, "capture_sidecar_key_invalid")
            result[key] = loader.construct_object(value_node)
        return result
    Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    try:
        depth = 0
        for count, event in enumerate(yaml.parse(raw)):
            require(count < 4096 and not isinstance(event, yaml.AliasEvent), "capture_sidecar_bound")
            if isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
                depth += 1
            elif isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
                depth -= 1
            require(depth <= 12, "capture_sidecar_bound")
        value = yaml.load(raw, Loader=Loader)  # noqa: S506 -- bounded SafeLoader subclass with duplicate-key refusal.
        require(isinstance(value, dict), "capture_sidecar_invalid")
        return value
    except (yaml.YAMLError, TypeError, RecursionError, UnicodeError):
        raise CaptureInvalid("capture_sidecar_invalid") from None


def inspect_record(root: Path, record):
    provenance = record.get("provenance")
    require(isinstance(provenance, dict) and "host_capture" in provenance, "capture_metadata_missing")
    profile = validate(provenance["host_capture"])
    paths = record.get("raw_paths")
    require(isinstance(paths, list) and len(paths) == 1 and isinstance(paths[0], str), "capture_raw_path_invalid")
    relative = paths[0]
    require(relative.startswith("raw/") and Path(relative).suffix.lower() in SUFFIXES, "capture_raw_path_invalid")
    require((profile["content_format"] == "plain_text") == (Path(relative).suffix.lower() == ".txt"), "capture_format_mismatch")
    raw = read(root, relative)
    require(profile["content_bytes"] == len(raw) and profile["content_sha256"] == "sha256:" + hashlib.sha256(raw).hexdigest(),
            "capture_original_bytes_changed")
    declared = sidecar(read(root, relative + ".provenance.yml"))
    require(declared.get("host_capture") == profile, "capture_metadata_changed")
    for key, expected in (("checksum", profile["content_sha256"]), ("origin_url", profile["origin_url"]),
                          ("retrieved_at", profile["retrieved_at"]), ("license", profile["rights"]["license"]),
                          ("request_id", profile["request_id"]), ("scope", profile["scope"])):
        require(declared.get(key, {} if key == "scope" else None) == expected, "capture_provenance_mismatch")
    try:
        content = raw.decode("utf-8")
    except UnicodeError:
        raise CaptureInvalid("capture_utf8_required") from None
    require("\x00" not in content, "capture_binary_content")
    reasons = []
    if not content.strip():
        reasons.append("host_capture_empty")
    if profile["content_kind"] in {"search_snippet", "generated_summary"}:
        reasons.append("host_capture_" + profile["content_kind"])
    if profile["rights"]["status"] != "allowed":
        reasons.append("host_capture_rights_" + profile["rights"]["status"])
    complete = profile["content_kind"] == "primary" and profile["completeness"] == "complete"
    return {"profile": profile, "content": content, "complete": complete, "unusable_reasons": reasons,
            "assurance": "caller_declared_capture_with_checked_bytes"}


def normalized_issues(root, record, frontmatter, body):
    try:
        require(root is not None, "capture_original_root_missing")
        inspected = inspect_record(root, record)
        require(frontmatter.get("host_capture") == inspected["profile"], "capture_normalized_metadata_mismatch")
        require(frontmatter.get("extraction_method") == "host_text", "capture_normalized_method_mismatch")
        if inspected["unusable_reasons"]:
            require(frontmatter.get("evidence_usable") is False, "capture_unusable_claim")
        if not inspected["complete"]:
            require(frontmatter.get("status") in {"partial", "failed"}, "capture_completeness_overstated")
        if inspected["content"].strip():
            content = inspected["content"].replace("\r\n", "\n").replace("\r", "\n")
            require("## Extracted Text\n\n" + content + "\n\n## Figures and Tables" in body,
                    "capture_normalized_text_mismatch")
        normalizer = load_workspace_module(_SCRIPT_DIR, "normalize_sources")
        contract = load_workspace_module(_SCRIPT_DIR, "_normalized_contract")
        source = normalizer.normalize_host_capture_record(root, copy.deepcopy(record))
        _, expected_body, error = contract.split_record(normalizer.render_markdown(source, frontmatter))
        require(error is None and body.strip() == expected_body.strip(), "capture_normalized_rendering_mismatch")
        return []
    except (CaptureInvalid, ValueError, TypeError, KeyError):
        if (frontmatter.get("status") == "failed" and frontmatter.get("evidence_usable") is False
                and frontmatter.get("host_capture") is None and "host_capture_invalid" in (frontmatter.get("unusable_evidence_reasons") or [])):
            return []
        return ["host_capture_original_or_qualifications_invalid"]
