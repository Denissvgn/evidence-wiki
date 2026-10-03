#!/usr/bin/env python3
"""Closed unaccepted draft proposals over the canonical strict claim contract."""

from __future__ import annotations

import math
from copy import deepcopy

from _evidence_revision import canonical_bytes
from _record_artifacts import json_document
from _script_errors import ScriptRefusal, is_refusal
from _strict_contract import CLAIMS_SCHEMA, CLAIMS_SCHEMA_V2, claims_document, identifier, text, validate_shape
from _strict_contract import schema_documents as strict_schemas

PROPOSAL = "evidence-draft-proposal/v1"
RESULT = "evidence-draft-admission/v1"
MAX_BYTES = 1_048_576
MAX_ORIGINALS = 100


def refusal(reason):
    return ScriptRefusal("DRAFT_ADMISSION_INVALID", "The draft proposal cannot be admitted.", exit_code=2,
        recoverable=False, details={"reason": reason},
        remediation="Use the current closed proposal contract and exact original/source bindings; retain unaccepted drafts.")


def _require(condition, reason):
    if not condition:
        raise refusal(reason)


def _bounded(value):
    pending, nodes, strings = [(value, 0)], 0, 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        _require(nodes <= 65536 and depth <= 20, "document_bound")
        if type(item) is dict:
            _require(len(item) <= 128 and all(type(key) is str for key in item), "object_bound")
            pending.extend((child, depth + 1) for pair in item.items() for child in pair)
        elif type(item) is list:
            _require(len(item) <= 4096, "array_bound")
            pending.extend((child, depth + 1) for child in item)
        elif type(item) is str:
            _require(len(item) <= 65536 and not any(0xD800 <= ord(char) <= 0xDFFF for char in item), "text_bound")
            strings += len(item.encode("utf-8"))
            _require(strings <= MAX_BYTES, "document_bound")
        elif type(item) is float:
            _require(math.isfinite(item), "nonfinite_value")
        else:
            _require(type(item) in {int, bool, type(None)} and (type(item) is not int or item.bit_length() <= 64), "plain_data_required")


def schema_documents():
    def obj(**fields):
        return {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}
    ident = {"type": "string", "minLength": 1, "maxLength": 128, "pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]*$(?![\s\S])"}
    string = {"type": "string", "minLength": 1, "maxLength": 4096}
    hashed = {"type": "string", "pattern": r"^sha256:[a-f0-9]{64}$(?![\s\S])"}
    array = lambda shape, maximum=100, minimum=0: {"type": "array", "items": shape, "minItems": minimum, "maxItems": maximum}
    strict = strict_schemas()
    proposal = obj(schema_version={"const": PROPOSAL}, inventory={"anyOf": [strict[CLAIMS_SCHEMA], strict[CLAIMS_SCHEMA_V2]]},
        originals=array(obj(original_id=ident, text=string, question_slugs=array(ident, 100, 1)), MAX_ORIGINALS, 1))
    result = obj(schema_version={"const": RESULT}, status={"const": "admitted"}, proposal_sha256=hashed,
        inventory_sha256=hashed, claims_path={"type": "string", "minLength": 1, "maxLength": 512},
        admitted_at={"type": "string", "minLength": 1, "maxLength": 40}, question_slugs=array(ident, 100, 1),
        original_ids=array(ident, 100, 1), claim_ids=array(ident),
        mechanical_check_count={"type": "integer", "minimum": 0, "maximum": 1600},
        semantic_approval={"const": "not_evaluated"}, accepted={"const": False},
        review_registered={"const": False}, question_status_changed={"const": False})
    return {name: {"$schema": "https://json-schema.org/draft/2020-12/schema", **shape}
            for name, shape in ((PROPOSAL, proposal), (RESULT, result))}


def schema_document(name):
    _require(name in {PROPOSAL, RESULT}, "schema_unknown")
    return deepcopy(schema_documents()[name])


def validate_document(name, value):
    try:
        _bounded(value)
        raw = canonical_bytes(value)
        _require(len(raw) <= MAX_BYTES, "document_bound")
        value = json_document(raw)
        validate_shape(value, schema_document(name))
        if name == PROPOSAL:
            value["inventory"] = claims_document(value["inventory"])
            originals = set()
            mapped = set()
            for row in value["originals"]:
                identifier(row["original_id"])
                text(row["text"])
                _require(row["original_id"] not in originals, "original_duplicate")
                originals.add(row["original_id"])
                _require(len(row["question_slugs"]) == len(set(row["question_slugs"])), "question_mapping_duplicate")
                for slug in row["question_slugs"]:
                    identifier(slug)
                    _require(slug not in mapped, "question_mapping_multiple_origins")
                    mapped.add(slug)
            expected = {row["slug"]: row["original_id"] for row in value["inventory"]["questions"]}
            _require(mapped == set(expected), "question_accounting_incomplete")
            _require(all(expected[slug] == row["original_id"] for row in value["originals"] for slug in row["question_slugs"]),
                     "original_mapping_changed")
        return value
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, UnicodeError) as exc:
        if is_refusal(exc):
            raise
        raise refusal("document_invalid") from None


def encode_document(name, value):
    return canonical_bytes(validate_document(name, value))


def decode_document(name, raw):
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_BYTES:
        raise refusal("document_bound")
    try:
        return validate_document(name, json_document(raw))
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        if is_refusal(exc):
            raise
        raise refusal("document_invalid") from None


def proposal_document(value):
    return validate_document(PROPOSAL, value)
