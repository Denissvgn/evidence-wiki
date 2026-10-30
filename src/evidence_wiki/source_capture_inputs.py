"""Detached bounded originals, owner-derived closures and approved path reads.

This input helper performs no deposition, extraction, normalization, signing or
source acceptance. Its artifact closure becomes durable only through the
existing intake/usage owners; declarations and connector names grant no trust.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import importlib.util
import io
import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from evidence_wiki._pack_io import canonical, identity, relative_path
from evidence_wiki.agent_execution import _utc
from evidence_wiki.host_context import HostContext
from evidence_wiki.mutation_contracts import detached
from evidence_wiki.pack_discovery import owner
from evidence_wiki.source_contracts import DELIVERY, reject_execution_and_secrets

SCHEMA = "evidence-source-capture-input/v1"
FORMATS = {"plain_text": (".txt", 524288), "markdown": (".md", 524288), "json": (".json", 1048576),
    "csv": (".csv", 1048576), "pdf": (".pdf", 8388608)}
FIELDS = {"schema_version", "capture_id", "tool_id", "tool_version", "origin_url", "title", "retrieved_at",
    "capture_method", "content_format", "content_kind", "completeness", "completeness_note", "rights", "scope",
    "request_id", "parents", "sanitization"}
_HASH = re.compile(r"sha256:[a-f0-9]{64}\Z")


class CaptureInputError(ValueError):
    """Content-free refusal of a capture declaration or bound input."""


def fail(reason):
    raise CaptureInputError("capture_input_" + reason) from None


def _hash(value):
    if type(value) is not str or not _HASH.fullmatch(value):
        fail("revision_invalid")
    return value


def _plain(value):
    try:
        copied = detached(value)
        if len(canonical(copied)) > 65536:
            fail("metadata_bound")
        return copied
    except CaptureInputError:
        raise
    except (Exception, SystemExit):
        fail("metadata_invalid")


def metadata_document(value):
    value = _plain(value)
    if type(value) is not dict or set(value) != FIELDS or value["schema_version"] != SCHEMA:
        fail("metadata_invalid")
    if type(value["content_format"]) is not str or value["content_format"] not in FORMATS:
        fail("format_unsupported")
    native = owner("_host_capture")
    inherited = {key: value[key] for key in native.FIELDS - {"content_sha256", "content_bytes"}}
    inherited.update(schema_version=native.SCHEMA, content_format="plain_text", content_sha256="sha256:" + "0" * 64, content_bytes=0)
    if value["capture_method"] == "host_approved_path":
        inherited["capture_method"] = "manual_transcription"
    try:
        native.validate(inherited)
        for url in (value["origin_url"], value["rights"]["terms_url"]):
            if url is not None and any(re.search(r"(?i)token|secret|password|credential|api.?key|signature", key)
                    for key, _ in parse_qsl(urlsplit(url).fragment, keep_blank_values=True)):
                fail("provenance_invalid")
        reject_execution_and_secrets(value)
        _utc(value["retrieved_at"])
    except (Exception, SystemExit):
        fail("provenance_invalid")
    parents = value["parents"]
    if type(parents) is not list or len(parents) > 16 or len(set(map(_hash, parents))) != len(parents):
        fail("ancestry_invalid")
    sanitization = value["sanitization"]
    if (type(sanitization) is not dict or set(sanitization) != {"required", "approved_revision_id", "receipt_ref"}
            or type(sanitization["required"]) is not bool):
        fail("sanitization_invalid")
    if sanitization["approved_revision_id"] is not None:
        _hash(sanitization["approved_revision_id"])
    if sanitization["receipt_ref"] is not None:
        try:
            native.identifier(sanitization["receipt_ref"])
        except ValueError:
            fail("sanitization_invalid")
    if (sanitization["approved_revision_id"] is None) != (sanitization["receipt_ref"] is None):
        fail("sanitization_invalid")
    return value


def _content(raw, format):
    if type(raw) is not bytes or not 0 < len(raw) <= FORMATS[format][1]:
        fail("content_bound")
    if format == "pdf":
        if not raw.startswith(b"%PDF-"):
            fail("pdf_signature_invalid")
        return
    try:
        text = raw.decode("utf-8")
        if "\x00" in text:
            fail("binary_text")
        if format == "json":
            def unique(items):
                result = {}
                for key, value in items:
                    if key in result:
                        fail("json_duplicate_key")
                    result[key] = value
                return result
            document = json.loads(text, object_pairs_hook=unique, parse_constant=lambda _: fail("json_nonfinite"))
            detached(document)
            if type(document) not in (dict, list):
                fail("json_container_required")
        if format == "csv":
            width = None
            for index, row in enumerate(csv.reader(io.StringIO(text), strict=True)):
                if index >= 4096:
                    fail("csv_shape_invalid")
                if width is None:
                    if not row or len(row) > 128 or len(set(row)) != len(row):
                        fail("csv_shape_invalid")
                    width = len(row)
                elif len(row) != width:
                    fail("csv_shape_invalid")
            if width is None:
                fail("csv_shape_invalid")
    except CaptureInputError:
        raise
    except (Exception, SystemExit):
        fail("content_invalid")


@dataclass(frozen=True, init=False)
class CaptureInput:
    """Immutable capture candidate; native owners still determine rights/use."""

    _raw: bytes = field(repr=False)
    _metadata: bytes = field(repr=False)

    def __init__(self, raw, metadata):
        value = metadata_document(metadata)
        _content(raw, value["content_format"])
        object.__setattr__(self, "_raw", raw)
        object.__setattr__(self, "_metadata", canonical(value))

    def metadata(self):
        return json.loads(self._metadata)

    @property
    def content(self):
        return self._raw

    @property
    def content_sha256(self):
        return "sha256:" + hashlib.sha256(self._raw).hexdigest()

    def artifacts(self):
        value = self.metadata()
        provenance = dict(value, content_sha256=self.content_sha256, content_bytes=len(self._raw))
        return {"original" + FORMATS[value["content_format"]][0]: self._raw, "provenance.json": canonical(provenance)}

    @property
    def artifact_closure_id(self):
        return owner("_record_artifacts").closure_identity(self.artifacts())

    def observation(self):
        metadata = self.metadata()
        gaps = []
        if metadata["content_kind"] != "primary" or metadata["completeness"] != "complete":
            gaps.append("complete_primary_capture_unavailable")
        if metadata["rights"]["status"] != "allowed":
            gaps.append("rights_not_declared_allowed")
        if metadata["sanitization"]["required"] or metadata["sanitization"]["approved_revision_id"] is not None:
            gaps.append("native_sanitization_and_ancestry_verification_required")
        if metadata["parents"]:
            gaps.append("native_ancestry_verification_required")
        return {"schema_version": "evidence-source-capture-input-observation/v1", "capture_id": metadata["capture_id"],
            "artifact_closure_id": self.artifact_closure_id, "content_sha256": self.content_sha256,
            "content_bytes": len(self._raw), "gaps": gaps, "deposition": "not_run", "extraction": "not_run",
            "sanitization": "not_verified", "historical_revision": "not_deposited", "research_ready": False,
            "authority": "declared_provenance_with_checked_original_bytes", "authorized": False, "evidence_accepted": False}

    def legacy_delivery(self):
        metadata = self.metadata()
        if (metadata["content_format"] not in {"plain_text", "markdown"} or metadata["parents"]
                or metadata["sanitization"]["required"] or metadata["sanitization"]["approved_revision_id"] is not None
                or metadata["capture_method"] == "host_approved_path"):
            fail("legacy_route_unsupported")
        native = owner("_host_capture")
        profile = {key: metadata[key] for key in native.FIELDS - {"content_sha256", "content_bytes"}}
        profile.update(schema_version=native.SCHEMA, content_sha256=self.content_sha256, content_bytes=len(self._raw))
        native.validate(profile)
        return {"schema_version": DELIVERY, "capture": profile, "content_base64": base64.b64encode(self._raw).decode("ascii")}


def observe_path(context, *, root, relative, metadata, expected_content_sha256, expected_bytes):
    """Read only a host-approved no-follow original, rejecting path/content drift."""
    if type(context) is not HostContext or not Path(root).is_absolute():
        fail("host_scope_invalid")
    _hash(expected_content_sha256)
    if type(expected_bytes) is not int or not 0 < expected_bytes <= 8388608:
        fail("expected_bytes_invalid")
    relative_path(relative)
    root = Path(root)
    selected = context.require_path(root / relative)
    if selected != root / relative:
        fail("input_path_changed")
    before = identity(root)
    native = owner("_host_capture")
    try:
        raw = native.read(root, relative, maximum=expected_bytes)
    except native.CaptureInvalid:
        fail("approved_source_unavailable_or_changed")
    if (len(raw) != expected_bytes or "sha256:" + hashlib.sha256(raw).hexdigest() != expected_content_sha256
            or identity(root) != before or context.require_path(root / relative) != selected):
        fail("input_changed")
    return CaptureInput(raw, metadata)


def pdf_capability():
    """Observe local presence only; never install, launch or attest extraction."""
    try:
        pypdf = importlib.util.find_spec("pypdf") is not None
    except (ValueError, ImportError):
        pypdf = False
    poppler = shutil.which("pdftotext") is not None
    return {"pypdf_present": pypdf, "poppler_present": poppler,
        "availability": "present_not_executed" if pypdf or poppler else "unavailable",
        "extraction": "not_run", "gap": None if pypdf or poppler else "pdf_extractor_unavailable"}
