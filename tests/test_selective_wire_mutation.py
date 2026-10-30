"""Mutation identity retains selected physical wire and original signed content."""

import copy

import pytest

from evidence_wiki import _review_dependency_wire as wire
from evidence_wiki import mutation_contracts as contracts
from tests.test_mutation_contracts import HASH, request


@pytest.fixture
def selected(monkeypatch):
    from tests.test_selective_mutation_transport import owned_child

    envelope = owned_child.__wrapped__(monkeypatch)
    # The native child's tests own semantic validation. Keep compression,
    # physical wrapper validation, immutable identity and decode checks real.
    monkeypatch.setattr(wire, "_validated", lambda value, kind: contracts._serialized(value))
    return envelope


def test_new_transport_retains_signed_bytes_and_frozen_wire_identity(selected):
    arguments = {"envelope": selected}
    physical = contracts.transport_review_arguments("strict.register_review", arguments)
    assert physical["envelope"]["schema_version"] == wire.WIRE
    assert contracts.review_envelope(physical["envelope"]) == selected
    assert contracts.transport_review_arguments("strict.register_review", physical) == physical
    requested = {**request(), "operation": "strict.register_review", "arguments": physical}
    frozen = contracts.make_intent(requested, {"path": "/workspace", "device": "1", "inode": "2"})
    raw = contracts.encode_document(contracts.INTENT, frozen)
    assert contracts.decode_document(contracts.INTENT, raw) == frozen
    assert frozen["arguments"] == physical
    changed = copy.deepcopy(frozen)
    changed["arguments"]["envelope"] = wire.pack({**selected, "authentication": {
        **selected["authentication"], "signature": "1" * 64}}, kind="native_envelope")
    with pytest.raises(contracts.MutationError):
        contracts.checked(contracts.INTENT, changed)
    assert contracts.review_envelope(frozen["arguments"]["envelope"]) == selected


def test_wire_wrong_kind_cannot_enter_review_arguments(selected):
    physical = wire.pack(selected, kind="journal_data")
    with pytest.raises(contracts.MutationError):
        contracts.arguments("strict.register_review", {"envelope": physical})


def test_transport_selection_preserves_legacy_arguments():
    value = {"envelope": {"payload": {"action": "register-strict-review", "request_id": HASH}}}
    assert contracts.transport_review_arguments("strict.register_review", value) == value
    assert contracts.transport_review_arguments("requirements.revise", value) == value
