"""Closed mutation wrappers retain selective child bytes and recovery custody."""

import copy
import hashlib
import json

import pytest

from evidence_wiki import _review_dependency_json as child_codec
from evidence_wiki import mutation_contracts as contracts
from evidence_wiki.errors import UsageError
from evidence_wiki.mutation_store import MutationStore, MutationStoreError, capture_revision
from evidence_wiki.pack_discovery import owner
from tests.test_mutation_contracts import HASH, request


def exact_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


@pytest.fixture
def owned_child(monkeypatch):
    """Substitute the child owner; mutation, digest and disk codecs remain real."""
    node = {"t": "string", "v": "Exact café\r\n"}
    for _ in range(14):
        node = {"t": "mapping", "v": [["child", node]]}
    review = {"schema_version": "evidence-strict-review/v3", "metadata": node}
    authentication = {"schema_version": "evidence-authentication/v1", "scheme": "hmac-sha256",
        "principal": "reviewer", "key_id": "reviewer-key", "role": "evaluator", "policy_id": "policy",
        "policy_revision": "1", "issued_at": "2026-10-16T12:00:00Z", "expires_at": "2026-10-16T13:00:00Z",
        "signature": "0" * 64}
    envelope = {"payload": {"schema_version": "evidence-usage-command/v1", "state_id": HASH,
        "workspace_binding": HASH, "request_id": "review", "expected_checkpoint": HASH,
        "action": "register-strict-review", "body": {"review": review}}, "authentication": authentication}
    native = owner("_review_dependency_contract")

    def checked_child(arguments):
        selected = arguments["envelope"]["payload"]["body"]["review"]
        assert set(selected) == {"schema_version", "metadata"}
        native.validate_node(selected["metadata"])
        return exact_bytes(arguments)

    monkeypatch.setattr(child_codec, "encoded", checked_child)
    return envelope


def selected_request(envelope):
    return {**request(), "operation": "strict.register_review", "arguments": {"envelope": envelope}}


def test_owned_child_round_trips_request_intent_and_exact_native_digest(owned_child):
    requested = selected_request(owned_child)
    assert contracts.arguments("strict.register_review", requested["arguments"]) == requested["arguments"]
    frozen = contracts.make_intent(requested, {"path": "/workspace", "device": "1", "inode": "2"})
    preimage = {key: value for key, value in frozen.items() if key != "input_sha256"}
    assert frozen["input_sha256"] == "sha256:" + hashlib.sha256(exact_bytes(preimage)).hexdigest()
    for version, value in ((contracts.REQUEST, requested), (contracts.INTENT, frozen)):
        raw = contracts.encode_document(version, value)
        assert raw == exact_bytes(value) and not raw.endswith(b"\n")
        assert contracts.decode_document(version, raw) == value
        assert contracts.canonical(value) == raw
    owned_child["authentication"]["principal"] = "caller edit"
    assert frozen["arguments"]["envelope"]["authentication"]["principal"] == "reviewer"


def test_owned_intent_round_trips_through_existing_inspection_wrapper(owned_child):
    frozen = contracts.make_intent(selected_request(owned_child), {"path": "/workspace", "device": "1", "inode": "2"})
    inspection = {"schema_version": contracts.INSPECTION, "request_id": frozen["request_id"],
        "record": {"intent": frozen, "state": None, "receipt": None},
        "historical": True, "authorized": False, "evidence_accepted": False}
    raw = contracts.encode_document(contracts.INSPECTION, inspection)
    assert raw == exact_bytes(inspection)
    assert contracts.decode_document(contracts.INSPECTION, raw) == inspection


def test_owned_child_persists_and_replays_through_existing_mutation_store(tmp_path, owned_child):
    workspace, control = tmp_path / "workspace", tmp_path / "control"
    workspace.mkdir()
    control.mkdir(mode=0o700)
    snapshot = capture_revision(workspace)
    requested = {**selected_request(owned_child), "expected_revision": snapshot["revision_id"]}
    frozen = contracts.make_intent(requested, snapshot["workspace_binding"])
    with MutationStore(workspace, control, frozen).locked() as store:
        assert store.prepare(frozen) == frozen
    observed = MutationStore(workspace, control).inspect(frozen["request_id"])
    assert observed["intent"] == frozen and observed["receipt"] is None
    with MutationStore(workspace, control, frozen).locked() as store:
        assert store.prepare(frozen) == frozen
    altered = copy.deepcopy(frozen)
    altered["arguments"]["envelope"]["authentication"]["principal"] = "changed"
    altered["input_sha256"] = contracts.intent_digest(altered)
    with MutationStore(workspace, control, altered).locked() as store, pytest.raises(MutationStoreError):
        store.prepare(altered)
    assert MutationStore(workspace, control).inspect(frozen["request_id"])["intent"] == frozen


@pytest.mark.parametrize("change", ["unknown_wrapper", "wrong_operation", "unowned_child", "unknown_review"])
def test_lookalikes_keep_existing_depth_and_closed_shape_limits(owned_child, change):
    value = selected_request(owned_child)
    if change == "unknown_wrapper":
        value["extra"] = None
    elif change == "wrong_operation":
        value["operation"] = "coverage.revision"
    elif change == "unowned_child":
        value["arguments"] = {"unowned": value["arguments"]["envelope"]}
    else:
        value["arguments"]["envelope"]["payload"]["body"]["review"]["schema_version"] = "evidence-strict-review/v99"
    with pytest.raises(UsageError):
        contracts.validate_document(contracts.REQUEST, value)
    with pytest.raises(UsageError):
        contracts.decode_document(contracts.REQUEST, exact_bytes(value))


def test_generic_detached_does_not_gain_a_child_budget(owned_child):
    with pytest.raises(UsageError):
        contracts.detached(selected_request(owned_child))
    old = request()
    assert contracts.canonical(old) == exact_bytes(old)
    assert contracts.decode_document(contracts.REQUEST, exact_bytes(old)) == old


def test_malformed_real_selective_review_never_gains_transport_exemption():
    envelope = {"payload": {"schema_version": "evidence-usage-command/v1", "state_id": HASH,
        "workspace_binding": HASH, "request_id": "review", "expected_checkpoint": HASH,
        "action": "register-strict-review", "body": {"review": {"schema_version": "evidence-strict-review/v3"}}},
        "authentication": dict.fromkeys(("schema_version", "scheme", "principal", "key_id", "role", "policy_id",
            "policy_revision", "issued_at", "expires_at", "signature"), "unqualified")}
    with pytest.raises(UsageError):
        contracts.arguments("strict.register_review", {"envelope": envelope})


def test_store_keeps_its_existing_aggregate_node_budget(owned_child):
    from evidence_wiki.mutation_store import _json

    metadata = {"t": "mapping", "v": [[str(index), {"t": "list", "v": [{"t": "string", "v": "x"}]}]
        for index in sorted(map(str, range(4096)))]}
    owned_child["payload"]["body"]["review"]["metadata"] = metadata
    frozen = contracts.make_intent(selected_request(owned_child), {"path": "/workspace", "device": "1", "inode": "2"})
    with pytest.raises(MutationStoreError):
        _json(frozen)
