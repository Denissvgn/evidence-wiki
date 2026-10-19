"""Physical journal spelling preserves existing small logical records."""

import json

import pytest

from evidence_wiki._review_dependency_wire import pack
from evidence_wiki.research_journal_contracts import JOURNAL, canonical_record, detached_record, storage_record


def _record(data):
    return {"schema_version": JOURNAL, "kind": "execution_native", "binding_id": "sha256:" + "1" * 64,
        "request_id": "request", "sequence": 0, "previous_record_ref": None, "data": data, "authentication": None}


def test_legacy_record_keeps_exact_bytes():
    record = _record({"label": "café"})
    expected = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    assert canonical_record(record) == expected
    assert storage_record(record) == record


def test_legacy_codec_capacity_remains_separate_from_physical_record_capacity():
    from evidence_wiki.errors import UsageError

    record = _record({"chunks": ["x" * 60000] * 5})
    expected = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    assert canonical_record(record) == expected
    with pytest.raises(UsageError):
        storage_record(record)


def test_explicit_wire_is_decoded_before_logical_record_use():
    data = {"schema_version": "evidence-strict-authorship/v2", "basis_id": "sha256:" + "1" * 64,
        "claim_id": "claim", "claim_schema_version": "evidence-strict-claims/v1",
        "observed_workspace_basis_id": "sha256:" + "2" * 64, "semantic_material_id": "sha256:" + "3" * 64}
    assert detached_record(_record(pack(data, "journal_data")))["data"] == data


def test_journal_wire_cannot_encode_ordinary_opaque_data():
    with pytest.raises(ValueError):
        pack({"arbitrary": ["value"]}, "journal_data")


def _owned_data():
    return {"observation": None, "error_code": None,
        "result": {"schema_version": "evidence-strict-authorship/v2", "basis_id": "sha256:" + "1" * 64,
            "claim_id": "claim", "claim_schema_version": "evidence-strict-claims/v1",
            "observed_workspace_basis_id": "sha256:" + "2" * 64,
            "semantic_material_id": "sha256:" + "3" * 64},
        "padding": ["x" * 60000] * 4 + [""]}


@pytest.mark.parametrize("size", [262143, 262144, 262145])
@pytest.mark.parametrize("authentication", [None, "a" * 64])
def test_owned_transport_threshold_counts_the_exact_header(size, authentication):
    record = _record(_owned_data())
    record["authentication"] = authentication
    raw = lambda: json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    record["data"]["padding"][-1] = "x" * (size - len(raw()))
    logical = raw()
    assert len(logical) == size
    encoded = canonical_record(record)
    stored = storage_record(record)
    assert encoded == json.dumps(stored, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    if size <= 262144:
        assert encoded == logical
    else:
        assert stored["data"]["schema_version"] == "research-review-native-bytes/v1"
    assert canonical_record(_record(pack(record["data"], "journal_data")) | {"authentication": authentication}) == encoded


def test_wire_record_returns_fresh_owned_logical_data():
    original = _owned_data()
    record = _record(pack(original, "journal_data"))
    first = detached_record(record)
    first["data"]["padding"][0] = "changed"
    first["data"]["result"]["claim_id"] = "changed"
    assert detached_record(record)["data"] == original
    record["data"]["encoded_chunks"][0] = "!!!!"
    assert first["data"]["padding"][0] == "changed"
    with pytest.raises(ValueError):
        from evidence_wiki._review_dependency_wire import unpack
        unpack(record["data"], "journal_data")


def test_wire_mutated_during_validation_is_refused(monkeypatch):
    from evidence_wiki import _review_dependency_wire as wire
    record = _record(wire.pack(_owned_data(), "journal_data"))
    validate = wire._validated
    def mutate(value, kind):
        raw = validate(value, kind)
        record["data"]["encoded_chunks"][0] = "!!!!"
        return raw
    monkeypatch.setattr(wire, "_validated", mutate)
    from evidence_wiki.mutation_contracts import MutationError
    with pytest.raises(MutationError):
        detached_record(record)
