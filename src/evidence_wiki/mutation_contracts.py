"""Closed bounded mutation intents and historical receipts; neither grants authority."""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from typing import Any, NoReturn

from .errors import UsageError
from .onboarding_contract import _bounded_nesting, _bounded_tree

REQUEST = "research-mutation-request/v1"
INTENT = "research-mutation-intent/v1"
STATE = "research-mutation-state/v1"
RECEIPT = "research-mutation-receipt/v1"
REVISION = "research-mutation-revision/v1"
INSPECTION = "research-mutation-inspection/v1"
RECONCILIATION = "research-mutation-reconciliation/v1"
MAX_BYTES = 1_048_576
STATUSES = ("prepared", "executing", "owner_completed", "committed", "refused", "partial", "commit_unknown", "reconciled")
TERMINAL = ("committed", "refused", "partial", "commit_unknown", "reconciled")
OPERATIONS = (
    "coverage.init", "coverage.link", "coverage.evaluate", "coverage.revision", "questions.add_batch",
    "source_requests.add", "source_requests.fulfill", "sources.inventory", "sources.capture", "normalize.sources",
    "strict.register_review",
    "strict.admit",
    "telemetry.record",
    "requirements.revise",
    "sources.capture_revision",
    "artifact.deliver",
    "source_requests.link_current", "source_requests.fulfill_current",
    "questions.block_current", "questions.reopen_current",
    "sources.build_index",
    "questions.refresh",
)


class MutationError(UsageError):
    """A bounded mutation request, binding, receipt or replay was refused."""


def refuse(reason: str, field: str = "/") -> NoReturn:
    raise MutationError(f"MUTATION_{reason}", "Mutation request refused.", recoverable=False,
                        details={"field": field},
                        remediation="Use the same immutable request and inspect retained canonical effects before retrying.") from None


def _obj(**fields: Any) -> dict[str, Any]:
    return {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}


def _text(maximum: int = 4096) -> dict[str, Any]:
    return {"type": "string", "minLength": 1, "maxLength": maximum, "pattern": r"^[^\x00-\x1f\x7f]+$(?![\s\S])"}


def _content(maximum: int = 4096) -> dict[str, Any]:
    return {"type": "string", "minLength": 1, "maxLength": maximum, "pattern": r"^[^\x00]+$(?![\s\S])"}


def _null(shape: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [shape, {"type": "null"}]}


def _array(shape: dict[str, Any], maximum: int = 256) -> dict[str, Any]:
    return {"type": "array", "items": shape, "maxItems": maximum}


def argument_schemas() -> dict[str, dict[str, Any]]:
    opaque = {"type": "object", "maxProperties": 128, "additionalProperties": True}
    boolean = {"type": "boolean"}
    selectors = _null(_array(_text(512)))
    scope = {"type": "object", "maxProperties": 16, "additionalProperties": _text()}
    integer = lambda maximum: {"type": "integer", "minimum": 1, "maximum": maximum}
    return {
        "coverage.init": _obj(slug=_text(128), template=_null(opaque), coverage_profile=_null(_text()), force=boolean),
        "coverage.link": _obj(slug=_text(128), facet_id=_text(128), accepted_source_ids=selectors,
            blocking_request_ids=selectors, clear_accepted_source_ids=boolean, clear_blocking_request_ids=boolean),
        "coverage.evaluate": _obj(slug=_text(128)),
        "coverage.revision": _obj(request=opaque),
        "questions.refresh": _obj(run_id=_text(96), previous_run_id=_text(96), request_id=_text(96),
            expected_basis_id=_text(71), expected_marker_sha256=_null(_text(71)),
            original_ids=_array(_text(128), 100)),
        "questions.add_batch": _obj(from_file=_text(), dry_run=boolean),
        "source_requests.add": _obj(kind=_text(128), query_or_identifier=_content(), rationale=_content(),
            priority={"enum": ["high", "medium", "low"]}, question_slugs=selectors, scope=_null(scope)),
        "source_requests.fulfill": _obj(request_id=_text(128), source_id=_text(512), match_scope=_null(scope), require_scope=boolean),
        "source_requests.link_current": _obj(request_id=_text(128), question_slugs=_array(_text(128), 128), scope=_null(scope)),
        "source_requests.fulfill_current": _obj(request_id=_text(128), source_id=_text(512), match_scope=_null(scope),
            require_scope={"const": True}, needs_complete=boolean),
        "questions.block_current": _obj(slug=_text(128), blocking_request_ids=_array(_text(128), 128), reason=_content()),
        "questions.reopen_current": _obj(slug=_text(128), needs_complete=boolean),
        "sources.inventory": _obj(dry_run=boolean, append_log=boolean, require_checksum=boolean, reject_mismatch=boolean,
            max_records=integer(10_000), max_entries=integer(100_000), max_input_bytes=integer(67_108_864)),
        "sources.capture": _obj(path=_text(512), delivery=opaque),
        "sources.capture_revision": _obj(path=_text(512), delivery=opaque),
        "artifact.deliver": _obj(plan_sha256=_text(71), proposal=opaque),
        "sources.build_index": _obj(source_ids=_array(_text(256), 32), index_path=_text(512),
            expected_sha256=_null({"type": "string", "pattern": r"^sha256:[a-f0-9]{64}$"})),
        "strict.register_review": _obj(envelope=opaque),
        "strict.admit": _obj(proposal=opaque),
        "telemetry.record": _obj(run_id=_text(96), agent_id=_text(128), event=opaque),
        "requirements.revise": _obj(proposal=opaque, envelope=opaque),
        "normalize.sources": _obj(source_ids=_null(_array(_text(), 1000)), all_sources=boolean, dry_run=boolean,
            force=boolean, append_log=boolean, pdf_extractor=_null({"enum": ["pypdf", "poppler"]}),
            max_sources=integer(1000), allow_external={"const": False}, max_manifest_bytes=integer(16_777_216),
            max_manifest_records=integer(10_000), max_entries=integer(100_000), max_input_bytes=integer(67_108_864)),
    }


def arguments(operation: str, value: Any) -> dict[str, Any]:
    """Detach arguments and expose documented owner defaults before hashing."""
    defaults = {
        "coverage.init": {"template": None, "coverage_profile": None, "force": False},
        "coverage.link": {"accepted_source_ids": None, "blocking_request_ids": None,
                          "clear_accepted_source_ids": False, "clear_blocking_request_ids": False},
        "coverage.evaluate": {}, "coverage.revision": {},
        "questions.add_batch": {"dry_run": False},
        "questions.refresh": {},
        "source_requests.add": {"priority": "medium", "question_slugs": None, "scope": None},
        "source_requests.fulfill": {"match_scope": None, "require_scope": False},
        "source_requests.link_current": {"scope": None},
        "source_requests.fulfill_current": {"match_scope": None, "require_scope": True, "needs_complete": True},
        "questions.block_current": {}, "questions.reopen_current": {"needs_complete": True},
        "sources.inventory": {"dry_run": False, "append_log": False, "require_checksum": False,
            "reject_mismatch": False, "max_records": 1000, "max_entries": 10_000, "max_input_bytes": 16_777_216},
        "sources.capture": {}, "sources.capture_revision": {}, "artifact.deliver": {},
        "sources.build_index": {"index_path": "sources/indexes/evidence.sqlite", "expected_sha256": None},
        "strict.register_review": {},
        "strict.admit": {},
        "telemetry.record": {},
        "requirements.revise": {},
        "normalize.sources": {"source_ids": None, "all_sources": False, "dry_run": False, "force": False,
            "append_log": False, "pdf_extractor": None, "max_sources": 128, "allow_external": False,
            "max_manifest_bytes": 1_048_576, "max_manifest_records": 1000,
            "max_entries": 10_000, "max_input_bytes": 16_777_216},
    }
    if type(operation) is not str or operation not in defaults or type(value) is not dict:
        refuse("OPERATION_UNSUPPORTED", "/operation")
    result = {**defaults[operation], **detached(value)}
    _shape(result, argument_schemas()[operation], "/arguments")
    if operation == "questions.add_batch" and result["from_file"] == "-":
        refuse("INPUT_UNBOUND", "/arguments/from_file")
    if operation == "questions.refresh":
        for field in ("run_id", "previous_run_id", "request_id"):
            validate_request_id(result[field])
    if operation == "sources.capture":
        from .source_contracts import DELIVERY, decode
        try:
            decode(DELIVERY, canonical(result["delivery"]))
        except UsageError:
            refuse("INPUT_INVALID", "/arguments/delivery")
    if operation == "sources.capture_revision":
        from .source_capture_contracts import validate
        try:
            validate(result["delivery"])
        except (ValueError, UsageError):
            refuse("INPUT_INVALID", "/arguments/delivery")
    if operation == "strict.register_review":
        payload = result["envelope"].get("payload")
        if type(payload) is not dict or payload.get("action") not in {"register-strict-review", "register-strict-human-review"}:
            refuse("INPUT_INVALID", "/arguments/envelope")
    if operation == "strict.admit":
        from .draft_contracts import PROPOSAL, validate_document

        result["proposal"] = validate_document(PROPOSAL, result["proposal"])
    if operation == "telemetry.record":
        from .telemetry_contracts import EVENT, validate_document

        result["event"] = validate_document(EVENT, result["event"])
        validate_request_id(result["run_id"])
        if result["event"]["run_id"] != result["run_id"] or result["event"]["agent_id"] != result["agent_id"]:
            refuse("INPUT_INVALID", "/arguments/event")
    return result


def schemas() -> dict[str, dict[str, Any]]:
    hashed = {"type": "string", "pattern": r"^sha256:[a-f0-9]{64}$(?![\s\S])"}
    identifier = {**_text(96), "pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$(?![\s\S])"}
    binding = _obj(path=_text(), device=_text(64), inode=_text(64))
    generation = _obj(script_root=_text(), script_sha256=hashed, package_sha256=hashed)
    file = _obj(path=_text(), sha256=hashed, bytes={"type": "integer", "minimum": 0, "maximum": 16_777_216})
    opaque = {"type": "object", "maxProperties": 128, "additionalProperties": True}
    proof = dict(current_revision=hashed, owner_observation=_null(opaque), completion_proved={"type": "boolean"},
        desired_state_proved={"type": "boolean"}, recovery_supported={"type": "boolean"},
        operation_outcome={"enum": ["original_unknown", "desired_state_only", "owner_return_success",
            "owner_return_partial", "owner_return_with_inputs_changed", "owner_return_with_transport_uncertainty",
            "authenticated_event"]},
        proof_kind={"enum": ["none", "owner_return", "capture_pair", "coverage_postconditions", "refresh_postconditions", "request_postconditions", "strict_checkpoint", "source_postconditions", "draft_postconditions", "requirements_postconditions"]})
    common = dict(request_id=identifier, operation={"enum": list(OPERATIONS)}, arguments=opaque,
                  expected_revision=hashed, input_files=_array(file, 64), owner_generation=generation,
                  host_context_sha256=hashed)
    request = _obj(schema_version={"const": REQUEST}, **common)
    intent = _obj(schema_version={"const": INTENT}, **common, workspace_binding=binding, input_sha256=hashed)
    state = _obj(schema_version={"const": STATE}, request_id=identifier, input_sha256=hashed,
        status={"enum": list(STATUSES)}, before_revision=hashed, after_revision=_null(hashed),
        result=_null(opaque), error=_null(opaque), owner_basis=_null(opaque), reconciliation=_null(_obj(**proof)))
    receipt = _obj(schema_version={"const": RECEIPT}, request_id=identifier, input_sha256=hashed,
        operation={"enum": list(OPERATIONS)}, status={"enum": list(TERMINAL)},
        before_revision=hashed, after_revision=_null(hashed), result=_null(opaque), error=_null(opaque),
        historical={"const": True}, authorized={"const": False}, evidence_accepted={"const": False})
    inert = dict(historical={"const": True}, authorized={"const": False}, evidence_accepted={"const": False})
    revision = _obj(schema_version={"const": REVISION}, revision_id=hashed, workspace_binding=binding, input_files=_array(file, 64))
    inspection = _obj(schema_version={"const": INSPECTION}, request_id=identifier,
        record=_null(_obj(intent=intent, state=_null(state), receipt=_null(receipt))), **inert)
    reconciliation = _obj(schema_version={"const": RECONCILIATION}, request_id=identifier,
        status={"enum": ["commit_unknown", "partial", "reconciled"]}, **proof, **inert)
    return {name: {"$schema": "https://json-schema.org/draft/2020-12/schema", **shape}
            for name, shape in ((REQUEST, request), (INTENT, intent), (STATE, state), (RECEIPT, receipt),
                                (REVISION, revision), (INSPECTION, inspection), (RECONCILIATION, reconciliation))}


def _shape(value: Any, shape: dict[str, Any], field: str = "/") -> None:
    import math
    import re

    if "anyOf" in shape:
        for child in shape["anyOf"]:
            try:
                _shape(value, child, field)
                return
            except MutationError:
                continue
        refuse("INVALID", field)
    types = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool, "null": type(None)}
    if "type" in shape:
        expected = shape["type"]
        if expected == "number":
            if type(value) not in (int, float) or type(value) is float and not math.isfinite(value):
                refuse("INVALID", field)
        elif expected not in types or type(value) is not types[expected]:
            refuse("INVALID", field)
    if "const" in shape and (type(value) is not type(shape["const"]) or value != shape["const"]):
        refuse("INVALID", field)
    if "enum" in shape and value not in shape["enum"]:
        refuse("INVALID", field)
    if type(value) is dict:
        props = shape.get("properties", {})
        if len(value) > shape.get("maxProperties", 128) or set(shape.get("required", ())) - value.keys():
            refuse("INVALID", field)
        additional = shape.get("additionalProperties", True)
        if additional is False and value.keys() - props.keys():
            refuse("INVALID", field)
        for key, child in value.items():
            if key in props:
                _shape(child, props[key], field.rstrip("/") + "/" + key)
            elif isinstance(additional, dict):
                _shape(child, additional, field)
    elif type(value) is list:
        if not shape.get("minItems", 0) <= len(value) <= shape.get("maxItems", 4096):
            refuse("LIMIT", field)
        for index, child in enumerate(value):
            _shape(child, shape.get("items", {}), field.rstrip("/") + f"/{index}")
    elif type(value) is str:
        if not shape.get("minLength", 0) <= len(value) <= shape.get("maxLength", 65536):
            refuse("LIMIT", field)
        if "pattern" in shape and re.search(shape["pattern"], value) is None:
            refuse("INVALID", field)
    elif type(value) in (int, float):
        if value < shape.get("minimum", value) or value > shape.get("maximum", value):
            refuse("INVALID", field)


def detached(value: Any) -> Any:
    try:
        pending = [(value, 1)]
        count = 0
        while pending:
            item, depth = pending.pop()
            count += 1
            if depth > 16 or count > 65536:
                refuse("LIMIT")
            if type(item) is dict:
                if any(type(key) is not str for key in item) or len(item) > 128:
                    refuse("INVALID")
                pending.extend((child, depth + 1) for pair in item.items() for child in pair)
            elif type(item) is list:
                if len(item) > 4096:
                    refuse("LIMIT")
                pending.extend((child, depth + 1) for child in item)
            elif type(item) is int and item.bit_length() > 256:
                refuse("LIMIT")
            elif item is not None and type(item) not in (str, bool, int, float):
                refuse("INVALID")
        _bounded_tree(value)
        return json.loads(_serialized(value))
    except MutationError:
        raise
    except UsageError:
        refuse("INVALID")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        refuse("INVALID")


def _serialized(value: Any) -> bytes:
    chunks, size = [], 0
    for text in json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).iterencode(value):
        raw = text.encode()
        size += len(raw)
        if size > MAX_BYTES:
            refuse("LIMIT")
        chunks.append(raw)
    return b"".join(chunks)


def _inspection_detached(value: Any) -> Any:
    """Keep each retained document's bounds independent of its fixed wrappers."""
    if type(value) is not dict or value.get("record") is None:
        return detached(value)
    record = value["record"]
    if (type(record) is not dict or any(type(key) is not str for key in record)
            or record.keys() != {"intent", "state", "receipt"}):
        refuse("INVALID", "/record")
    result = detached({**value, "record": None})
    result["record"] = {
        key: checked(schema, record[key]) if key == "intent" or record[key] is not None else None
        for schema, key in ((INTENT, "intent"), (STATE, "state"), (RECEIPT, "receipt"))
    }
    # The two closed wrappers consume aggregate capacity, but never reduce the
    # already checked child's depth budget or increase its individual limits.
    pending, count = [result], 0
    while pending:
        item = pending.pop()
        count += 1
        if count > 65536:
            refuse("LIMIT")
        if type(item) is dict:
            pending.extend(child for pair in item.items() for child in pair)
        elif type(item) is list:
            pending.extend(item)
    return json.loads(_serialized(result))


def canonical(value: Any) -> bytes:
    return json.dumps(detached(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical(value)).hexdigest()


def schema_document(name: str) -> dict[str, Any]:
    if type(name) is not str or name not in schemas():
        refuse("VERSION_UNSUPPORTED", "/schema_version")
    return schemas()[name]


def intent_digest(value: dict[str, Any]) -> str:
    return digest({key: child for key, child in value.items() if key != "input_sha256"})


def checked(name: str, value: Any) -> dict[str, Any]:
    value = _inspection_detached(value) if name == INSPECTION else detached(value)
    if type(value) is dict and "schema_version" in value and value["schema_version"] != name:
        refuse("VERSION_UNSUPPORTED", "/schema_version")
    _shape(value, schema_document(name))
    if name in {REQUEST, INTENT}:
        if arguments(value["operation"], value["arguments"]) != value["arguments"]:
            refuse("INVALID", "/arguments")
        paths = [row["path"] for row in value["input_files"]]
        if len(paths) != len(set(paths)) or paths != sorted(paths):
            refuse("INVALID", "/input_files")
        for path in paths:
            if (PurePosixPath(path).is_absolute() or "\\" in path
                    or any(part in {"", ".", ".."} for part in path.split("/"))):
                refuse("INPUT_UNBOUND", "/input_files")
        if name == INTENT and intent_digest(value) != value["input_sha256"]:
            refuse("INPUT_CONFLICT", "/input_sha256")
    if name in {STATE, RECEIPT} and value["status"] in ({"committed", "owner_completed"} if name == STATE else {"committed", "reconciled"}) and value["result"] is None:
        refuse("INVALID", "/result")
    if name == STATE and value["status"] == "reconciled" and (
            value["reconciliation"] is None or not (value["reconciliation"]["completion_proved"]
                                                   or value["reconciliation"]["desired_state_proved"])):
        refuse("INVALID", "/reconciliation")
    if name == INSPECTION and value["record"] is not None:
        record = value["record"]
        intent = checked(INTENT, record["intent"])
        if intent["request_id"] != value["request_id"]:
            refuse("INPUT_CONFLICT", "/request_id")
        for schema, key in ((STATE, "state"), (RECEIPT, "receipt")):
            if record[key] is not None:
                child = checked(schema, record[key])
                if (child["request_id"] != intent["request_id"]
                        or child["input_sha256"] != intent["input_sha256"]
                        or child["before_revision"] != intent["expected_revision"]
                        or key == "receipt" and child["operation"] != intent["operation"]):
                    refuse("INPUT_CONFLICT", "/record")
    proof = value.get("reconciliation") if name == STATE else value if name == RECONCILIATION else None
    if proof is not None:
        if ((proof["completion_proved"] or proof["desired_state_proved"]) and
                (not proof["recovery_supported"] or proof["proof_kind"] == "none")
                or proof["proof_kind"] == "none" and proof["recovery_supported"]):
            refuse("INVALID", "/reconciliation")
        resolved = proof["desired_state_proved"] or proof["completion_proved"] and proof["operation_outcome"] in {
            "owner_return_success", "authenticated_event"}
        if name == RECONCILIATION and (value["status"] == "reconciled") != resolved:
            refuse("INVALID", "/status")
    return value


def validate_document(name: str, value: Any) -> dict[str, Any]:
    """Validate a fixed closed envelope and return detached plain data."""
    return checked(name, value)


def validate_request_id(value: Any) -> str:
    """Refuse invalid identifiers before reserving a private request directory."""
    _shape(value, schema_document(REQUEST)["properties"]["request_id"], "/request_id")
    return value


def make_intent(request: Any, workspace_binding: dict[str, Any]) -> dict[str, Any]:
    request = checked(REQUEST, request)
    result = {**request, "schema_version": INTENT, "workspace_binding": detached(workspace_binding)}
    result["input_sha256"] = intent_digest(result)
    return checked(INTENT, result)


def encode_document(name: str, value: Any) -> bytes:
    value = checked(name, value)
    return _serialized(value) if name == INSPECTION else canonical(value)


def _document_nesting(name: str, text: str) -> None:
    if name != INSPECTION:
        _bounded_nesting(text)
        return
    depth, quoted, escaped = 0, False, False
    for character in text:
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character in "[{":
            depth += 1
            if depth > 18:
                refuse("LIMIT")
        elif character in "]}":
            depth -= 1


def decode_document(name: str, raw: bytes) -> dict[str, Any]:
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_BYTES:
        refuse("LIMIT")

    def unique(pairs):
        result = {}
        for key, child in pairs:
            if key in result:
                refuse("INVALID")
            result[key] = child
        return result

    try:
        text = raw.decode()
        _document_nesting(name, text)
        value = json.loads(text, object_pairs_hook=unique, parse_constant=lambda _: refuse("INVALID"))
    except MutationError:
        raise
    except UsageError:
        refuse("INVALID")
    except (ValueError, UnicodeError, RecursionError):
        refuse("INVALID")
    return checked(name, value)
