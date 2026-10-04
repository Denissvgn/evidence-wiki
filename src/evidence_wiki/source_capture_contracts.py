"""Closed versioned capture delivery; declarations carry no custody or grants."""

from __future__ import annotations

import base64
import copy
from pathlib import Path

from ._pack_io import canonical, identity, json_document, relative_path
from .host_context import HostContext
from .pack_discovery import owner
from .source_capture_inputs import FIELDS, FORMATS, SCHEMA, CaptureInput, fail, metadata_document, observe_path

DELIVERY = "evidence-host-delivery/v2"
CAPTURE = "evidence-host-capture/v2"
INLINE_BYTES = 49152


def schemas():
    """Publish inert closed data contracts without execution or signing authority."""
    capture = copy.deepcopy(owner("_host_capture").schema())
    properties = capture["properties"]
    properties["schema_version"] = {"const": CAPTURE}
    properties["content_format"] = {"enum": list(FORMATS)}
    properties["capture_method"]["enum"].append("host_approved_path")
    revision = {"type": "string", "pattern": r"^sha256:[a-f0-9]{64}$"}
    nullable = lambda value: {"anyOf": [value, {"type": "null"}]}
    def closed(**fields):
        return {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}
    properties["parents"] = {"type": "array", "items": revision, "maxItems": 16, "uniqueItems": True}
    properties["sanitization"] = closed(required={"type": "boolean"}, approved_revision_id=nullable(revision),
        receipt_ref=nullable(properties["capture_id"]))
    capture["required"].extend(["parents", "sanitization"])
    properties["content_bytes"] = {"type": "integer", "minimum": 1, "maximum": 8388608}
    capture["allOf"] = [{"if": {"properties": {"content_format": {"const": format}}},
        "then": {"properties": {"content_bytes": {"maximum": bound}}}} for format, (_, bound) in FORMATS.items()]
    source = {"anyOf": [closed(kind={"const": "bytes"}, content_base64={"type": "string", "maxLength": 65536}),
        closed(kind={"const": "approved_path"}, root={"type": "string", "minLength": 1, "maxLength": 4096},
            root_identity=closed(device={"type": "string", "pattern": r"^[0-9]{1,32}$"}, inode={"type": "string", "pattern": r"^[0-9]{1,32}$"}),
            relative={"type": "string", "minLength": 1, "maxLength": 512})]}
    delivery = {"$schema": "https://json-schema.org/draft/2020-12/schema", **closed(schema_version={"const": DELIVERY}, capture=capture, input=source)}
    supplied = copy.deepcopy(capture)
    supplied["properties"]["schema_version"] = {"const": SCHEMA}
    for field in ("content_sha256", "content_bytes"):
        supplied["properties"].pop(field)
        supplied["required"].remove(field)
    supplied.pop("allOf")
    return {DELIVERY: delivery, CAPTURE: capture, SCHEMA: supplied}


def metadata(profile):
    if type(profile) is not dict or set(profile) != FIELDS | {"content_sha256", "content_bytes"} or profile.get("schema_version") != CAPTURE:
        fail("profile_invalid")
    value = metadata_document({key: SCHEMA if key == "schema_version" else profile[key] for key in FIELDS})
    if (type(profile["content_bytes"]) is not int or not 0 < profile["content_bytes"] <= FORMATS[value["content_format"]][1]
            or type(profile["content_sha256"]) is not str or len(profile["content_sha256"]) != 71
            or not profile["content_sha256"].startswith("sha256:")
            or any(char not in "0123456789abcdef" for char in profile["content_sha256"][7:])):
        fail("profile_identity_invalid")
    return value


def profile(candidate):
    return {**candidate.metadata(), "schema_version": CAPTURE, "content_sha256": candidate.content_sha256, "content_bytes": len(candidate.content)}


def from_bytes(raw, supplied):
    candidate = CaptureInput(raw, supplied)
    if len(raw) > INLINE_BYTES:
        fail("inline_bound_use_approved_path")
    return {"schema_version": DELIVERY, "capture": profile(candidate),
            "input": {"kind": "bytes", "content_base64": base64.b64encode(raw).decode("ascii")}}


def from_path(context, *, root, relative, supplied, expected_content_sha256, expected_bytes):
    candidate = observe_path(context, root=root, relative=relative, metadata=supplied,
        expected_content_sha256=expected_content_sha256, expected_bytes=expected_bytes)
    root = context.require_path(Path(root)).resolve(strict=True)
    return {"schema_version": DELIVERY, "capture": profile(candidate), "input": {
        "kind": "approved_path", "root": str(root), "root_identity": identity(root), "relative": relative}}


def validate(value):
    if type(value) is not dict or set(value) != {"schema_version", "capture", "input"} or value["schema_version"] != DELIVERY:
        fail("delivery_invalid")
    # The full document fits the existing immutable mutation record bound.
    value = json_document(canonical(value))
    from .onboarding_contract import _matches
    _matches(value, schemas()[DELIVERY])
    supplied = metadata(value["capture"])
    source = value["input"]
    if type(source) is not dict:
        fail("delivery_input_invalid")
    if source.get("kind") == "bytes":
        if set(source) != {"kind", "content_base64"} or type(source["content_base64"]) is not str or len(source["content_base64"]) > 65536:
            fail("inline_bound")
        try:
            raw = base64.b64decode(source["content_base64"], validate=True)
        except (ValueError, UnicodeError):
            fail("inline_encoding_invalid")
        if base64.b64encode(raw).decode("ascii") != source["content_base64"] or len(raw) > INLINE_BYTES:
            fail("inline_encoding_invalid")
        candidate = CaptureInput(raw, supplied)
        if candidate.content_sha256 != value["capture"]["content_sha256"] or len(raw) != value["capture"]["content_bytes"]:
            fail("delivery_identity_changed")
    elif source.get("kind") == "approved_path":
        if set(source) != {"kind", "root", "root_identity", "relative"} or type(source["root"]) is not str or not Path(source["root"]).is_absolute():
            fail("approved_root_invalid")
        relative_path(source["relative"])
        if (type(source["root_identity"]) is not dict or set(source["root_identity"]) != {"device", "inode"}
                or any(type(item) is not str or not item.isdecimal() or len(item) > 32 for item in source["root_identity"].values())):
            fail("approved_root_identity_invalid")
    else:
        fail("delivery_input_invalid")
    return value


def read_input(value):
    value = validate(value)
    supplied, source = metadata(value["capture"]), value["input"]
    if source["kind"] == "bytes":
        raw = base64.b64decode(source["content_base64"], validate=True)
        return CaptureInput(raw, supplied)
    from .host_context import current_context
    context = current_context()
    if type(context) is not HostContext:
        fail("approved_context_required")
    root = context.require_path(Path(source["root"])).resolve(strict=True)
    if str(root) != source["root"] or identity(root) != source["root_identity"]:
        fail("approved_root_changed")
    return observe_path(context, root=root, relative=source["relative"], metadata=supplied,
        expected_content_sha256=value["capture"]["content_sha256"], expected_bytes=value["capture"]["content_bytes"])
