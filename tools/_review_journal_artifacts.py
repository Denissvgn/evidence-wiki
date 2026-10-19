"""Explicit bounded journal retention and byte-only historical verification."""

from __future__ import annotations

import hashlib
from pathlib import Path

from evidence_wiki.research_journal_collections import (
    COLLECTION,
    decode_collection,
    encode_collection,
    inspect_collection_manifest,
)

POLICY = {"schema_version": "review-journal-collection-policy/v1",
    "collection_schema": COLLECTION, "page_bytes": 1_048_576,
    "collection_bytes": 16_777_216, "expanded_bytes": 16_777_216,
    "physical_nodes": 65_536, "record_count": 4096,
    "raw_source_bytes": 16_777_216}
RECEIPT = "review-journal-collection-owner-receipt/v1"
SUMMARY_FIELDS = ("records_sha256", "records_bytes", "record_count", "physical_nodes", "expanded_bytes")


def selected(value):
    policy = value.get("journal_collection_policy")
    if policy is not None and policy != POLICY:
        raise ValueError("workload_journal_collection_policy_invalid")
    return policy is not None


def page_directory(path):
    return path.with_name(path.stem + ".pages")


def page_name(index):
    return f"{index:04d}.json"


def raw_pin(path, raw):
    return {"path": str(path), "raw_sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)}


def retain_collection(originals, label, records, retain):
    encoded = encode_collection(records)
    path = originals.directory / (str(len(originals.pins)) + "-" + label + ".json")
    manifest = encoded["manifest"]
    originals.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if manifest is not None:
        directory = page_directory(path)
        directory.mkdir(mode=0o700, parents=True)
        for index, raw in enumerate(encoded["pages"]):
            pin = retain(directory / page_name(index), raw, raw=True)
            original = dict(pin, label=label + f"-page-{index:04d}")
            originals.pins.append(original)
    pin = retain(path, encoded["artifact_bytes"], raw=True)
    originals.pins.append(dict(pin, label=label))
    return pin


def read_collection(pin):
    """Verify exact artifact/page inventory without authenticating an old owner."""
    from tools.replay_qualification_native import _artifact_directory
    path = Path(pin["path"]).absolute()
    if path.resolve(strict=True) != path:
        raise ValueError("workload_artifact_path_invalid")
    with _artifact_directory(path.parent) as descriptor:
        return _read_collection_in_directory(pin, path, descriptor)


def _read_collection_in_directory(pin, path, descriptor):
    from tools.replay_qualification_native import _artifact_directory, _private_part
    raw = _private_part(path.name, 1_048_576, directory_fd=descriptor)
    if raw_pin(path, raw) != {key: pin[key] for key in ("path", "raw_sha256", "size_bytes")}:
        raise ValueError("workload_artifact_binding_invalid")
    manifest = inspect_collection_manifest(raw)
    pages, pins = [], []
    if manifest is not None:
        inventory = manifest["pages"]
        directory = page_directory(path)
        with _artifact_directory(directory) as descriptor:
            import os
            names = {page_name(index) for index in range(len(inventory))}
            if set(os.listdir(descriptor)) != names:
                raise ValueError("workload_artifact_binding_invalid")
            total = len(raw)
            for index, entry in enumerate(inventory):
                maximum = min(entry["size_bytes"], 16_777_216 - total)
                piece = _private_part(page_name(index), maximum, directory_fd=descriptor)
                total += len(piece)
                if len(piece) != entry["size_bytes"] or total > 16_777_216:
                    raise ValueError("workload_journal_collection_bound")
                pages.append(piece)
                pins.append(raw_pin(directory / page_name(index), piece))
    decoded = decode_collection(raw, pages=tuple(pages))
    metadata = {key: decoded[key] for key in ("artifact_kind", "artifact_sha256", "artifact_bytes", "page_inventory")}
    metadata.update(decoded["summary"], page_pins=pins)
    return decoded["records"], metadata


def verify_receipt(receipt, *, original_pins=None):
    if receipt.get("schema_version") == RECEIPT:
        from tools._review_dependency_accounting import validate
        validate("CollectionJournalReceipt", receipt)
    elif "schema_version" in receipt:
        raise ValueError("workload_pilot_measurement_invalid")
    records, metadata = read_collection(receipt["original_pin"])
    fields = SUMMARY_FIELDS if receipt.get("schema_version") == RECEIPT else SUMMARY_FIELDS[:2]
    if any(receipt.get(key) != metadata[key] for key in fields):
        raise ValueError("workload_pilot_measurement_invalid")
    if receipt.get("schema_version") == RECEIPT:
        if receipt.get("artifact_kind") != metadata["artifact_kind"] or receipt.get("page_pins") != metadata["page_pins"]:
            raise ValueError("workload_pilot_measurement_invalid")
    elif metadata["artifact_kind"] != "legacy_array":
        raise ValueError("workload_pilot_measurement_invalid")
    if "record_count" in receipt and receipt["record_count"] != metadata["record_count"]:
        raise ValueError("workload_pilot_measurement_invalid")
    from evidence_wiki.research_journal_contracts import digest_record
    if receipt.get("head_ref") != (digest_record(records[-1]) if records else None):
        raise ValueError("workload_pilot_measurement_invalid")
    if original_pins is not None:
        for expected in (receipt["original_pin"], *metadata["page_pins"]):
            if not any(all(pin.get(key) == expected[key] for key in ("path", "raw_sha256", "size_bytes")) for pin in original_pins):
                raise ValueError("workload_pilot_measurement_invalid")
    return records, metadata
