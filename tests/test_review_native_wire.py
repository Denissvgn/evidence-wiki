"""Native wire bytes remain canonical, bounded and explicitly typed."""

import base64
import copy
import hashlib
import json
import zlib

import pytest

from evidence_wiki._review_dependency_wire import WIRE, pack, schema, unpack


def _authorship():
    return {"schema_version": "evidence-strict-authorship/v2", "basis_id": "sha256:" + "1" * 64,
        "claim_id": "claim", "claim_schema_version": "evidence-strict-claims/v1",
        "observed_workspace_basis_id": "sha256:" + "2" * 64, "semantic_material_id": "sha256:" + "3" * 64}


def test_wire_roundtrip_detaches_and_requires_the_declared_kind():
    value = _authorship()
    wire = pack(value, "journal_data")
    assert wire["schema_version"] == WIRE and schema()["additionalProperties"] is False
    result = unpack(wire, "journal_data")
    assert result == value and result is not value
    with pytest.raises(ValueError):
        unpack(wire, "native_envelope")


@pytest.mark.parametrize("field,value", [("raw_size_bytes", True), ("raw_size_bytes", 1048577),
    ("raw_sha256", "sha256:" + "0" * 64), ("encoding", "base64"), ("encoded_chunks", ["!!!!"])])
def test_wire_rejects_invalid_closed_fields(field, value):
    wire = pack(_authorship(), "journal_data")
    wire[field] = value
    with pytest.raises(ValueError):
        unpack(wire, "journal_data")


def test_wire_rejects_trailing_compressed_stream_and_opaque_json():
    import base64
    import zlib

    wire = pack(_authorship(), "journal_data")
    altered = copy.deepcopy(wire)
    compressed = base64.b64decode("".join(wire["encoded_chunks"]))
    altered["encoded_chunks"] = [base64.b64encode(compressed + zlib.compress(b"{}")).decode()]
    with pytest.raises(ValueError):
        unpack(altered, "journal_data")
    with pytest.raises(ValueError):
        pack({"opaque": "value"}, "journal_data")
    with pytest.raises(ValueError):
        pack(wire, "journal_data")


def _raw_wire(raw, *, declared_size=None):
    value = pack(_authorship(), "journal_data")
    value.update(raw_sha256="sha256:" + hashlib.sha256(raw).hexdigest(),
        raw_size_bytes=len(raw) if declared_size is None else declared_size,
        encoded_chunks=[base64.b64encode(zlib.compress(raw, 9)).decode("ascii")])
    return value


@pytest.mark.parametrize("raw", [
    b'{"schema_version":"evidence-strict-authorship/v2","schema_version":"ordinary/v1"}',
    b'{"schema_version":"evidence-strict-authorship/v2","basis_id":NaN}',
    json.dumps(_authorship(), indent=2).encode("utf-8"),
])
def test_wire_rejects_ambiguous_or_noncanonical_raw_json(raw):
    with pytest.raises(ValueError):
        unpack(_raw_wire(raw), "journal_data")


def test_declared_small_expansion_cannot_hide_a_large_compressed_payload():
    wire = _raw_wire(b" " * (2 * 1048576), declared_size=16)
    with pytest.raises(ValueError):
        unpack(wire, "journal_data")


def test_wire_rejects_a_noncanonical_equivalent_compression_stream():
    raw = json.dumps(_authorship(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    wire = _raw_wire(raw)
    wire["encoded_chunks"] = [base64.b64encode(zlib.compress(raw, 0)).decode("ascii")]
    with pytest.raises(ValueError):
        unpack(wire, "journal_data")


def test_each_unpack_owns_its_returned_value_and_revalidates_the_frame():
    wire = pack(_authorship(), "journal_data")
    first = unpack(wire, "journal_data")
    first["claim_id"] = "changed"
    assert unpack(wire, "journal_data") == _authorship()
    wire["encoded_chunks"][0] = "!!!!"
    with pytest.raises(ValueError, match="^review_native_wire_invalid$"):
        unpack(wire, "journal_data")


def test_canonical_compression_does_not_bypass_native_schema_validation():
    value = _authorship()
    del value["claim_id"]
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(ValueError, match="^review_native_wire_invalid$"):
        unpack(_raw_wire(raw), "journal_data")
