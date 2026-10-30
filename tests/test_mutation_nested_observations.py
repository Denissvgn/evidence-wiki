"""Closed historical wrappers retain the complete bounded native documents."""

from __future__ import annotations

import copy
import json

import pytest

from evidence_wiki import mutation_contracts as contracts
from evidence_wiki import mutation_operations as operations
from evidence_wiki.mutation_contracts import INSPECTION, INTENT, REQUEST, MutationError
from evidence_wiki.mutation_store import MutationStore
from tests.test_mutation_contracts import documents
from tests.test_mutation_operations import host
from tests.test_mutation_operations import roots as roots


def nested(depth, leaf="complete original observation"):
    result = leaf
    for _ in range(depth - 1):
        result = {"nested": result}
    return result


def boundary_request():
    value = documents()[REQUEST]
    value.update(operation="strict.register_review", arguments={"envelope": {
        "payload": {"action": "register-strict-review", "observation": nested(12)}}})
    return contracts.checked(REQUEST, value)


def boundary_inspection():
    value = documents()[INSPECTION]
    intent = contracts.make_intent(boundary_request(), value["record"]["intent"]["workspace_binding"])
    value["record"]["intent"] = intent
    for key in ("state", "receipt"):
        value["record"][key]["input_sha256"] = intent["input_sha256"]
        value["record"][key]["result"] = {"observation": nested(14)}
    value["record"]["receipt"]["operation"] = intent["operation"]
    return value


@pytest.mark.parametrize("leaf", ["complete original observation", {}, 'braces {[]} and escaped "quotes" \\'])
def test_boundary_inspection_round_trip_retains_all_child_records(leaf):
    value = boundary_inspection()
    for key in ("state", "receipt"):
        value["record"][key]["result"]["observation"] = nested(14, leaf)
    original = copy.deepcopy(value)
    with pytest.raises(MutationError):
        contracts.detached(value)
    with pytest.raises(MutationError):
        contracts.canonical(value)
    checked = contracts.checked(INSPECTION, value)
    assert checked == original
    raw = contracts.encode_document(INSPECTION, value)
    assert contracts.decode_document(INSPECTION, raw) == original
    assert contracts.encode_document(INSPECTION, contracts.decode_document(INSPECTION, raw)) == raw
    checked["record"]["intent"]["arguments"].clear()
    assert value == original


def test_boundary_request_executes_inspects_and_replays_in_native_store(roots, monkeypatch):
    root, control = roots
    arguments = boundary_request()["arguments"]
    result = {"owner_return": "executed", "observation": nested(14)}
    calls = []

    def invoke(_root, intent, captured, **options):
        assert isinstance(captured["mutation_store"], MutationStore)
        calls.append(copy.deepcopy(intent))
        return result

    monkeypatch.setattr(operations, "_invoke", invoke)
    with host(roots).activate():
        request = operations.prepare_request(root, request_id="boundary", operation="strict.register_review", arguments=arguments)
        original = copy.deepcopy(request)
        receipt = operations.submit(root, request)
        assert receipt["status"] == "committed" and receipt["result"] == result
        inspected = operations.inspect(root, "boundary")
        assert inspected["record"]["intent"] == calls[0]
        assert inspected["record"]["state"]["result"] == result
        assert inspected["record"]["receipt"] == receipt
        assert contracts.decode_document(INSPECTION, contracts.encode_document(INSPECTION, inspected)) == inspected
        assert operations.submit(root, request) == receipt
        assert len(calls) == 1 and request == original
        assert MutationStore(root, control).inspect("boundary") == inspected["record"]


def test_boundary_owner_completed_checkpoint_recovers_without_dispatch(roots, monkeypatch):
    root, control = roots
    with host(roots).activate():
        request = operations.prepare_request(root, request_id="boundary", operation="strict.register_review",
                                             arguments=boundary_request()["arguments"])
        info = root.stat()
        intent = contracts.make_intent(request, {"path": str(root), "device": str(info.st_dev), "inode": str(info.st_ino)})
        state = {"schema_version": contracts.STATE, "request_id": "boundary", "input_sha256": intent["input_sha256"],
                 "status": "owner_completed", "before_revision": request["expected_revision"], "after_revision": None,
                 "result": {"observation": nested(14)}, "error": None, "owner_basis": None, "reconciliation": None}
        with MutationStore(root, control, intent).locked() as store:
            store.prepare(intent)
            store.write(state)
        monkeypatch.setattr(operations, "_invoke", lambda *args, **kwargs: pytest.fail("completed owner redispatched"))
        receipt = operations.submit(root, request)
        assert receipt["status"] == "committed" and receipt["result"] == state["result"]
        assert operations.inspect(root, "boundary")["record"]["receipt"] == receipt
        assert operations.submit(root, request) == receipt


@pytest.mark.parametrize("schema", [REQUEST, INTENT])
def test_generic_request_and_intent_depth_limit_remains_unchanged(schema):
    value = boundary_request()
    if schema == INTENT:
        value = contracts.make_intent(value, {"path": "/workspace", "device": "1", "inode": "2"})
    value["arguments"]["envelope"]["payload"]["observation"] = nested(13)
    with pytest.raises(MutationError) as error:
        contracts.checked(schema, value)
    assert error.value.error_code == "MUTATION_LIMIT"
    raw = json.dumps(value).encode()
    with pytest.raises(MutationError):
        contracts.decode_document(schema, raw)


@pytest.mark.parametrize("kind", ["extra", "bytes", "nodes", "cycle"])
def test_untrusted_requests_keep_original_closed_and_aggregate_limits(kind):
    value = boundary_request()
    if kind == "extra":
        value["arguments"]["unexpected"] = "caller value"
    elif kind == "bytes":
        value["arguments"]["envelope"]["payload"]["observation"] = ["x" * 65536] * 17
    elif kind == "nodes":
        value["arguments"]["envelope"]["payload"]["observation"] = [["x"] * 4096 for _ in range(17)]
    else:
        value["arguments"]["envelope"]["payload"]["observation"] = value
    with pytest.raises(MutationError):
        contracts.checked(REQUEST, value)


@pytest.mark.parametrize("target", ["intent", "state", "receipt"])
def test_inspection_children_still_have_original_depth_limits(target):
    value = boundary_inspection()
    if target == "intent":
        value["record"][target]["arguments"]["envelope"]["payload"]["observation"] = nested(13)
    else:
        value["record"][target]["result"]["observation"] = nested(15)
    with pytest.raises(MutationError):
        contracts.checked(INSPECTION, value)
    with pytest.raises(MutationError):
        contracts.decode_document(INSPECTION, json.dumps(value).encode())


@pytest.mark.parametrize("kind", ["extra", "record_extra", "child_extra", "bytes", "cycle", "duplicate", "deep_raw"])
def test_closed_inspection_wrappers_do_not_admit_hostile_data(kind):
    value = boundary_inspection()
    if kind == "extra":
        value["unexpected"] = nested(17)
    elif kind == "record_extra":
        value["record"]["unexpected"] = {"retained": True}
    elif kind == "child_extra":
        value["record"]["intent"]["unexpected"] = "caller data"
    elif kind == "bytes":
        value["record"]["state"]["result"] = {"many": ["x" * 65536] * 17}
    elif kind == "cycle":
        value["record"]["state"]["result"] = value
    elif kind == "duplicate":
        raw = b'{"schema_version":"research-mutation-inspection/v1","record":null,"record":null}'
    else:
        raw = b"[" * 19 + b"]" * 19
    with pytest.raises(MutationError):
        if kind in {"duplicate", "deep_raw"}:
            contracts.decode_document(INSPECTION, raw)
        else:
            contracts.checked(INSPECTION, value)


@pytest.mark.parametrize("kind", ["bytes", "nodes"])
def test_inspection_wrappers_preserve_aggregate_limits(kind):
    value = boundary_inspection()
    payload = {"many": ["x" * 60000] * 9} if kind == "bytes" else {"many": [["x"] * 4000 for _ in range(9)]}
    for schema, key in ((contracts.STATE, "state"), (contracts.RECEIPT, "receipt")):
        value["record"][key]["result"] = payload
        contracts.checked(schema, value["record"][key])
    with pytest.raises(MutationError) as error:
        contracts.checked(INSPECTION, value)
    assert error.value.error_code == "MUTATION_LIMIT"
