"""Closed transport intents retain exact bytes and never assert authority."""

from __future__ import annotations

import copy
import json

import pytest

from evidence_wiki import mutation_contracts as contracts
from evidence_wiki.mutation_contracts import (
    INSPECTION,
    INTENT,
    RECEIPT,
    RECONCILIATION,
    REQUEST,
    REVISION,
    STATE,
    MutationError,
)

HASH = "sha256:" + "1" * 64
OTHER_HASH = "sha256:" + "2" * 64


def request():
    return {"schema_version": REQUEST, "request_id": "local-request", "operation": "source_requests.add",
        "arguments": contracts.arguments("source_requests.add", {
            "kind": "other", "query_or_identifier": "  原文\nobservation  ", "rationale": "Line one.\nLine two.",
        }), "expected_revision": HASH, "input_files": [], "host_context_sha256": HASH, "owner_generation": {
            "script_root": "/installed/scripts", "script_sha256": HASH, "package_sha256": HASH}}


def documents():
    requested = request()
    intent = contracts.make_intent(requested, {"path": "/workspace", "device": "1", "inode": "2"})
    state = {"schema_version": STATE, "request_id": requested["request_id"], "input_sha256": intent["input_sha256"],
        "status": "committed", "before_revision": HASH, "after_revision": OTHER_HASH,
        "result": {"created": True, "owner_verdict": "inert observation"}, "error": None,
        "owner_basis": None, "reconciliation": None}
    receipt = {key: value for key, value in state.items() if key not in {"reconciliation", "owner_basis"}}
    receipt.update(schema_version=RECEIPT, operation=requested["operation"], historical=True,
                   authorized=False, evidence_accepted=False)
    inspection = {"schema_version": INSPECTION, "request_id": requested["request_id"],
        "record": {"intent": intent, "state": state, "receipt": receipt},
        "historical": True, "authorized": False, "evidence_accepted": False}
    reconciliation = {"schema_version": RECONCILIATION, "request_id": requested["request_id"],
        "status": "commit_unknown", "current_revision": HASH, "owner_observation": None,
        "completion_proved": False, "recovery_supported": False,
        "desired_state_proved": False, "proof_kind": "none",
        "operation_outcome": "original_unknown",
        "historical": True, "authorized": False, "evidence_accepted": False}
    revision = {"schema_version": REVISION, "revision_id": HASH, "workspace_binding": intent["workspace_binding"],
                "input_files": []}
    return {REQUEST: requested, INTENT: intent, STATE: state, RECEIPT: receipt, REVISION: revision,
            INSPECTION: inspection, RECONCILIATION: reconciliation}


@pytest.mark.parametrize("schema", [REQUEST, INTENT, STATE, RECEIPT, REVISION, INSPECTION, RECONCILIATION])
def test_closed_round_trip_and_detached_schema_and_data(schema):
    original = documents()[schema]
    raw = contracts.encode_document(schema, original)
    assert contracts.decode_document(schema, raw) == original
    detached = contracts.validate_document(schema, original)
    detached["schema_version"] = "caller edit"
    assert original["schema_version"] == schema
    shape = contracts.schema_document(schema)
    shape["properties"].clear()
    assert contracts.schema_document(schema)["properties"]
    assert not raw.endswith(b"\n")


def test_exact_original_argument_text_and_full_immutable_identity():
    original = request()
    frozen = contracts.make_intent(original, {"path": "/workspace", "device": "1", "inode": "2"})
    assert frozen["arguments"]["query_or_identifier"] == "  原文\nobservation  "
    assert frozen["arguments"]["rationale"] == "Line one.\nLine two."
    original["arguments"]["scope"] = {"facet_id": "caller mutation"}
    assert frozen["arguments"]["scope"] is None
    mutations = {
        "request_id": "other", "expected_revision": OTHER_HASH,
        "host_context_sha256": OTHER_HASH,
        "owner_generation": {**frozen["owner_generation"], "package_sha256": OTHER_HASH},
        "input_files": [{"path": "batch.yml", "sha256": OTHER_HASH, "bytes": 10}],
        "workspace_binding": {**frozen["workspace_binding"], "inode": "3"},
        "arguments": {**frozen["arguments"], "rationale": "Different."},
    }
    for field, value in mutations.items():
        altered = copy.deepcopy(frozen)
        altered[field] = value
        with pytest.raises(MutationError) as error:
            contracts.validate_document(INTENT, altered)
        assert error.value.error_code == "MUTATION_INPUT_CONFLICT"
    assert contracts.digest({"b": 2, "a": 1}) == contracts.digest({"a": 1, "b": 2})


@pytest.mark.parametrize("schema", [REQUEST, INTENT, STATE, RECEIPT, REVISION, INSPECTION, RECONCILIATION])
@pytest.mark.parametrize("kind", ["unknown", "missing", "version"])
def test_unknown_missing_and_versioned_fields_refuse(schema, kind):
    value = documents()[schema]
    if kind == "unknown":
        value["unexpected"] = "caller value"
    elif kind == "missing":
        del value["schema_version"]
    else:
        value["schema_version"] = "future/v9"
    with pytest.raises(MutationError) as error:
        contracts.validate_document(schema, value)
    assert "caller value" not in str(error.value)
    assert error.value.error_code.startswith("MUTATION_")


@pytest.mark.parametrize("schema", [RECEIPT, INSPECTION, RECONCILIATION])
@pytest.mark.parametrize("field", ["authorized", "evidence_accepted", "historical"])
def test_inert_records_cannot_escalate_authority_or_become_current(schema, field):
    value = documents()[schema]
    value[field] = field != "historical"
    with pytest.raises(MutationError):
        contracts.validate_document(schema, value)


@pytest.mark.parametrize("schema", [STATE, RECEIPT])
def test_committed_requires_original_owner_result(schema):
    value = documents()[schema]
    value["result"] = None
    with pytest.raises(MutationError):
        contracts.validate_document(schema, value)


@pytest.mark.parametrize("target,field,new", [
    ("intent", "request_id", "other"), ("state", "input_sha256", OTHER_HASH),
    ("state", "before_revision", OTHER_HASH), ("receipt", "operation", "coverage.evaluate"),
    ("receipt", "before_revision", OTHER_HASH),
])
def test_inspection_checks_nested_immutable_correlation(target, field, new):
    value = documents()[INSPECTION]
    value["record"][target][field] = new
    if target == "intent":
        value["record"][target]["input_sha256"] = contracts.intent_digest(value["record"][target])
    with pytest.raises(MutationError):
        contracts.validate_document(INSPECTION, value)


@pytest.mark.parametrize("path", ["/outside", "../escape", "a/../b", "a//b", "./file", "a/./b", "a\\b"])
def test_input_file_paths_are_canonical_relative_names(path):
    value = request()
    value["input_files"] = [{"path": path, "sha256": HASH, "bytes": 1}]
    with pytest.raises(MutationError) as error:
        contracts.validate_document(REQUEST, value)
    assert error.value.error_code == "MUTATION_INPUT_UNBOUND"


@pytest.mark.parametrize("paths", [["b", "a"], ["a", "a"]])
def test_input_file_order_and_duplicates_refuse(paths):
    value = request()
    value["input_files"] = [{"path": path, "sha256": HASH, "bytes": 1} for path in paths]
    with pytest.raises(MutationError):
        contracts.validate_document(REQUEST, value)


@pytest.mark.parametrize("kind", ["nan", "infinity", "cycle", "depth", "bytes", "nodes", "custom", "dict_subclass",
                                 "list_subclass", "nonstring_key", "huge_integer", "surrogate"])
def test_hostile_opaque_data_stays_bounded_without_custom_coercion(kind):
    value = documents()[STATE]
    if kind == "nan":
        payload = {"cost": float("nan")}
    elif kind == "infinity":
        payload = {"cost": float("inf")}
    elif kind == "cycle":
        payload = {}
        payload["cycle"] = payload
    elif kind == "depth":
        payload = {}
        for _ in range(20):
            payload = {"nested": payload}
    elif kind == "bytes":
        payload = {"x": "x" * (contracts.MAX_BYTES + 1)}
    elif kind == "nodes":
        payload = {"many": [["x"] * 4096 for _ in range(17)]}
    elif kind == "huge_integer":
        payload = {"integer": 1 << 100_000}
    elif kind == "surrogate":
        payload = {"text": "\ud800"}
    elif kind == "nonstring_key":
        payload = {1: "not a JSON key"}
    else:
        class Untouched(dict if kind == "dict_subclass" else list if kind == "list_subclass" else object):
            def __str__(self):
                pytest.fail("custom coercion")
            def items(self):
                pytest.fail("custom traversal")
            def __iter__(self):
                pytest.fail("custom traversal")
        payload = Untouched()
    value["result"] = payload
    with pytest.raises(MutationError):
        contracts.validate_document(STATE, value)


@pytest.mark.parametrize("raw", [
    b'{"schema_version":"a","schema_version":"b"}', b'{"value":NaN}', b'{"value":Infinity}',
    b'{"value":-Infinity}', b'{"value":', b"\xff", b"[" * 100 + b"]" * 100, b"",
])
def test_json_decode_refuses_duplicate_keys_nonfinite_invalid_and_deep_bytes(raw):
    with pytest.raises(MutationError):
        contracts.decode_document(REQUEST, raw)


@pytest.mark.parametrize("operation,values", [
    ("questions.add_batch", {"from_file": "-"}), ("normalize.sources", {"allow_external": True}),
    ("sources.inventory", {"dry_run": 1}), ("sources.inventory", {"max_entries": True}),
    ("sources.inventory", {"max_records": 0}), ("sources.inventory", {"max_input_bytes": 67_108_865}),
    ("coverage.evaluate", {"slug": "sample", "shell": "echo unsafe"}),
    ("unknown.operation", {}), ("source_requests.add", {"kind": "other", "query_or_identifier": "x"}),
])
def test_closed_operation_args_do_not_create_external_or_unbounded_effects(operation, values):
    with pytest.raises(MutationError):
        contracts.arguments(operation, values)


def test_request_requires_explicit_canonical_defaults_and_stable_json():
    value = request()
    del value["arguments"]["scope"]
    with pytest.raises(MutationError):
        contracts.validate_document(REQUEST, value)
    complete = request()
    raw = contracts.encode_document(REQUEST, complete)
    assert json.loads(raw)["arguments"]["scope"] is None
    assert contracts.encode_document(REQUEST, contracts.decode_document(REQUEST, raw)) == raw
