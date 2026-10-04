#!/usr/bin/env python3
"""Versioned native capture inspection backed by the installed library codec."""

from __future__ import annotations

import hashlib
from pathlib import Path


def codec():
    from evidence_wiki import source_capture_contracts
    return source_capture_contracts


def validate(value):
    codec().metadata(value)
    return value


def inspect(root, record, native, *, read_input=None):
    from evidence_wiki.source_capture_inputs import FORMATS, CaptureInput
    profile = validate(record["provenance"]["host_capture"])
    paths = record.get("raw_paths")
    native.require(type(paths) is list and len(paths) == 1 and type(paths[0]) is str, "capture_raw_path_invalid")
    relative = paths[0]
    native.require(relative.startswith("raw/") and not relative.startswith("raw/links/")
        and Path(relative).suffix.lower() == FORMATS[profile["content_format"]][0], "capture_format_mismatch")
    raw = native.read(root, relative, maximum=FORMATS[profile["content_format"]][1], read_input=read_input)
    native.require(len(raw) == profile["content_bytes"] and "sha256:" + hashlib.sha256(raw).hexdigest() == profile["content_sha256"], "capture_original_bytes_changed")
    captured = CaptureInput(raw, codec().metadata(profile))
    from evidence_wiki.source_capture_operations import provenance_profile
    sidecar = native.sidecar(native.read(root, relative + ".provenance.yml", maximum=65536, read_input=read_input))
    native.require(sidecar == provenance_profile(profile), "capture_provenance_mismatch")
    provenance = record["provenance"]
    native.require(all(provenance.get(key) == value for key, value in sidecar.items())
        and set(provenance) - set(sidecar) <= {"checksum_verified", "path", "sidecar_path"}, "capture_record_provenance_mismatch")
    if "path" in provenance:
        native.require(provenance["path"] == relative, "capture_record_path_mismatch")
    if "sidecar_path" in provenance:
        native.require(provenance["sidecar_path"] == relative + ".provenance.yml", "capture_record_path_mismatch")
    if "checksum_verified" in provenance:
        native.require(type(provenance["checksum_verified"]) is bool, "capture_record_checksum_invalid")
    reasons = []
    if profile["content_kind"] in {"search_snippet", "generated_summary"}:
        reasons.append("host_capture_" + profile["content_kind"])
    if profile["rights"]["status"] != "allowed":
        reasons.append("host_capture_rights_" + profile["rights"]["status"])
    if profile["parents"] or profile["sanitization"]["required"] or profile["sanitization"]["approved_revision_id"] is not None:
        reasons.append("host_capture_protected_revision_owner_required")
    if profile["content_format"] not in {"markdown", "plain_text"}:
        reasons.append("host_capture_format_normalization_not_run")
    content = captured.content.decode("utf-8") if profile["content_format"] in {"markdown", "plain_text"} else ""
    return {"profile": profile, "content": content, "complete": profile["content_kind"] == "primary" and profile["completeness"] == "complete",
        "unusable_reasons": reasons, "assurance": "caller_declared_capture_with_checked_bytes"}
