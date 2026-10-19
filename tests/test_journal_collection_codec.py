"""Collection transport integrity stays distinct from native authentication."""

import hashlib
import json
from copy import deepcopy

import pytest

from evidence_wiki import research_journal_collections as codec
from evidence_wiki.errors import UsageError
from evidence_wiki.research_journal_contracts import (
    JOURNAL,
    canonical_record,
    canonical_records,
    digest_record,
    digest_records,
)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _record(index=0, data=None):
    return {"schema_version": JOURNAL, "kind": "execution_native", "binding_id": "sha256:" + "1" * 64,
        "request_id": "request", "sequence": index, "previous_record_ref": None,
        "data": {"label": "café"} if data is None else data, "authentication": None}


def _large():
    return [_record(index, {"chunks": [str(index) + "x" * 59999] * 5}) for index in range(4)]


def test_small_array_and_record_hashes_keep_exact_legacy_bytes():
    records = [_record()]
    expected = _json(records)
    encoded = codec.encode_collection(records)
    assert encoded["artifact_bytes"] == canonical_records(records) == expected
    assert encoded["manifest"] is None and encoded["pages"] == (expected,)
    assert encoded["summary"]["records_sha256"] == digest_records(records)
    assert digest_record(records[0]) == "sha256:" + hashlib.sha256(canonical_record(records[0])).hexdigest()
    decoded = codec.decode_collection(expected)
    assert decoded["records"] == records and decoded["artifact_kind"] == "legacy_array"
    assert decoded["artifact_bytes"] == len(expected) and decoded["page_inventory"] == []


def test_large_ordered_array_pages_preserve_the_virtual_canonical_identity():
    records = _large()
    with pytest.raises(UsageError) as refused:
        canonical_records(records)
    assert refused.value.error_code == "MUTATION_LIMIT"
    encoded = codec.encode_collection(records)
    assert len(encoded["pages"]) == 2
    assert all(len(page) <= codec.PAGE_BYTES for page in encoded["pages"])
    expected = b"[" + b",".join(canonical_record(record) for record in records) + b"]"
    assert encoded["summary"]["records_sha256"] == "sha256:" + hashlib.sha256(expected).hexdigest()
    assert encoded["summary"]["records_bytes"] == len(expected)
    decoded = codec.decode_collection(encoded["artifact_bytes"], pages=encoded["pages"])
    assert decoded["records"] == records and decoded["summary"] == encoded["summary"]
    assert decoded["artifact_sha256"] != decoded["summary"]["records_sha256"]
    assert decoded["artifact_bytes"] == len(encoded["artifact_bytes"])
    assert decoded["page_inventory"] == encoded["manifest"]["pages"]


def test_largest_individual_legacy_record_fits_its_own_page_without_native_cap_changes():
    records = [_record(index, {"chunks": ["x" * 60000] * 17}) for index in range(2)]
    assert 1_000_000 < len(canonical_record(records[0])) < codec.PAGE_BYTES
    encoded = codec.encode_collection(records)
    assert [page["record_count"] for page in encoded["manifest"]["pages"]] == [1, 1]
    assert codec.decode_collection(encoded["artifact_bytes"], pages=encoded["pages"])["records"] == records


@pytest.mark.parametrize("mutation", ["missing", "extra", "reordered", "duplicate", "truncated", "substituted"])
def test_page_inventory_rejects_missing_extra_reordered_duplicate_or_changed_bytes(mutation):
    encoded = codec.encode_collection(_large())
    pages = list(encoded["pages"])
    if mutation == "missing":
        pages.pop()
    elif mutation == "extra":
        pages.append(pages[-1])
    elif mutation == "reordered":
        pages.reverse()
    elif mutation == "duplicate":
        pages[1] = pages[0]
    elif mutation == "truncated":
        pages[1] = pages[1][:-1]
    else:
        pages[1] = pages[1].replace(b"execution_native", b"execution_change", 1)
    with pytest.raises(UsageError):
        codec.decode_collection(encoded["artifact_bytes"], pages=pages)


@pytest.mark.parametrize("mutation", ["version", "unknown", "overlap", "gap", "duplicate_index", "count", "size", "hash", "nodes", "expansion"])
def test_closed_manifest_rejects_incomplete_ranges_and_wrong_collection_statistics(mutation):
    encoded = codec.encode_collection(_large())
    manifest = deepcopy(encoded["manifest"])
    if mutation == "version":
        manifest["schema_version"] += "-unknown"
    elif mutation == "unknown":
        manifest["extra"] = True
    elif mutation == "overlap":
        manifest["pages"][1]["record_start"] -= 1
    elif mutation == "gap":
        manifest["pages"][1]["record_start"] += 1
    elif mutation == "duplicate_index":
        manifest["pages"][1]["index"] = 0
    else:
        key = {"count": "record_count", "size": "records_bytes", "hash": "records_sha256",
            "nodes": "physical_nodes", "expansion": "expanded_bytes"}[mutation]
        manifest[key] = "sha256:" + "0" * 64 if mutation == "hash" else manifest[key] + 1
    with pytest.raises(UsageError):
        codec.decode_collection(_json(manifest), pages=encoded["pages"])


def test_noncanonical_or_duplicate_json_keys_are_refused():
    with pytest.raises(UsageError):
        codec.decode_collection(b'[{"schema_version":"wrong","schema_version":"wrong"}]')
    with pytest.raises(UsageError):
        codec.decode_collection(b"[ ]")


def test_record_count_individual_codec_and_physical_node_bounds_are_enforced():
    with pytest.raises(UsageError):
        codec.encode_collection([_record()] * 4097)
    with pytest.raises(UsageError):
        codec.encode_collection([_record(data={"unknown": object()})])
    with pytest.raises(UsageError):
        codec.encode_collection([_record(index, {"values": [None] * 4000}) for index in range(17)])


def test_expansion_physical_bytes_and_manifest_overhead_use_explicit_total_bounds(monkeypatch):
    records = _large()
    encoded = codec.encode_collection(records)
    whole = encoded["summary"]["records_bytes"]
    monkeypatch.setattr(codec, "MAX_BYTES", whole - 1)
    with pytest.raises(UsageError):
        codec.encode_collection(records)
    monkeypatch.setattr(codec, "MAX_BYTES", whole)
    with pytest.raises(UsageError):
        codec.encode_collection(records)
    with pytest.raises(UsageError):
        codec.collection_summary(records)
    monkeypatch.setattr(codec, "MAX_BYTES", 16_777_216)
    monkeypatch.setattr(codec, "MAX_NODES", encoded["summary"]["physical_nodes"])
    with pytest.raises(UsageError):
        codec.encode_collection(records)
    with pytest.raises(UsageError):
        codec.decode_collection(encoded["artifact_bytes"], pages=encoded["pages"])


def test_same_record_duplicates_keep_legacy_transport_semantics():
    records = [_record(), _record()]
    assert codec.decode_collection(codec.encode_collection(records)["artifact_bytes"])["records"] == records


def _decode_spies(monkeypatch):
    events = []
    for name in ("_load", "_hash", "_prepared"):
        original = getattr(codec, name)
        def wrapper(value, *args, _name=name, _original=original, **kwargs):
            events.append((_name, len(value)))
            return _original(value, *args, **kwargs)
        monkeypatch.setattr(codec, name, wrapper)
    return events


@pytest.mark.parametrize("bound", ["bytes", "nodes", "last_page_shape", "last_page_range"])
def test_manifest_and_all_page_shapes_are_preflighted_before_any_page_hash_or_parse(monkeypatch, bound):
    encoded = codec.encode_collection(_large())
    artifact, pages = encoded["artifact_bytes"], encoded["pages"]
    if bound == "bytes":
        monkeypatch.setattr(codec, "MAX_BYTES", len(artifact) + sum(map(len, pages)) - 1)
    elif bound == "nodes":
        monkeypatch.setattr(codec, "MAX_NODES", encoded["summary"]["physical_nodes"])
    elif bound == "last_page_shape":
        pages = (*pages[:-1], bytearray(pages[-1]))
    else:
        manifest = deepcopy(encoded["manifest"])
        manifest["pages"][-1]["record_start"] -= 1
        artifact = _json(manifest)
    events = _decode_spies(monkeypatch)
    with pytest.raises(UsageError):
        codec.decode_collection(artifact, pages=pages)
    assert events == [("_load", len(artifact))]


def test_actual_page_nodes_are_charged_before_hashing_or_parsing_later_pages(monkeypatch):
    encoded = codec.encode_collection(_large())
    manifest = deepcopy(encoded["manifest"])
    manifest["physical_nodes"] = 1
    artifact = _json(manifest)
    monkeypatch.setattr(codec, "MAX_NODES", codec._nodes(manifest) + len(encoded["pages"]) + 1)
    events = _decode_spies(monkeypatch)
    with pytest.raises(UsageError):
        codec.decode_collection(artifact, pages=encoded["pages"])
    assert events == [("_load", len(artifact)), ("_hash", len(encoded["pages"][0])),
        ("_load", len(encoded["pages"][0]))]


def test_logical_expansion_is_charged_before_retaining_or_encoding_later_children(monkeypatch):
    records = [_record(index) for index in range(4)]
    monkeypatch.setattr(codec, "MAX_BYTES", len(_json(records[:2])))
    events = []
    for name in ("detached_record", "canonical_record"):
        original = getattr(codec, name)
        def wrapper(value, _name=name, _original=original):
            events.append((_name, value["sequence"]))
            return _original(value)
        monkeypatch.setattr(codec, name, wrapper)
    with pytest.raises(UsageError):
        codec.encode_collection(records)
    assert events == [("detached_record", 0), ("canonical_record", 0),
        ("detached_record", 1), ("canonical_record", 1), ("detached_record", 2)]


def test_manifest_inspection_preflights_without_page_io_hashes_or_record_decoding(monkeypatch):
    import builtins
    from pathlib import Path

    encoded = codec.encode_collection(_large())
    def forbidden(*args, **kwargs):
        pytest.fail("manifest inspection touched pages or record codecs")
    with monkeypatch.context() as isolated:
        isolated.setattr(builtins, "open", forbidden)
        isolated.setattr(Path, "open", forbidden)
        isolated.setattr(codec, "_hash", forbidden)
        isolated.setattr(codec, "_prepared", forbidden)
        manifest = codec.inspect_collection_manifest(encoded["artifact_bytes"])
    assert manifest == encoded["manifest"]
    assert codec.inspect_collection_manifest(b"[]") is None


@pytest.mark.parametrize("bound", ["bytes", "nodes", "range"])
def test_manifest_inspection_rejects_impossible_declared_transport_before_pages(monkeypatch, bound):
    encoded = codec.encode_collection(_large())
    artifact = encoded["artifact_bytes"]
    if bound == "bytes":
        monkeypatch.setattr(codec, "MAX_BYTES", len(artifact) + sum(map(len, encoded["pages"])) - 1)
    elif bound == "nodes":
        monkeypatch.setattr(codec, "MAX_NODES", encoded["summary"]["physical_nodes"])
    else:
        manifest = deepcopy(encoded["manifest"])
        manifest["pages"][-1]["record_start"] -= 1
        artifact = _json(manifest)
    events = _decode_spies(monkeypatch)
    with pytest.raises(UsageError):
        codec.inspect_collection_manifest(artifact)
    assert events == [("_load", len(artifact))]
