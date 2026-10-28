"""Explicit bounded collection transport; byte integrity conveys no authority.

Summary sizes and nodes describe the virtual canonical array, and expansion
describes its logical records. Acceptance also charges every physical page
wrapper and the manifest to the unchanged 16 MiB/65,536-node transport totals.
The manifest and each page separately fit one MiB. Individual records always
pass their owning codec; this route changes no mutation or native child cap.
"""

from __future__ import annotations

import hashlib
import json
import re

from .mutation_contracts import refuse
from .research_journal_contracts import _canonical_owned_record, expanded_size

COLLECTION = "research-execution-journal-collection/v1"
RECEIPT = "research-execution-journal-collection-receipt/v1"
PAGE_BYTES = 1_048_576
MAX_BYTES = 16_777_216
MAX_RECORDS = 4096
MAX_NODES = 65_536
_FIELDS = {"schema_version", "records_sha256", "records_bytes", "record_count", "physical_nodes", "expanded_bytes", "pages"}
_PAGE_FIELDS = {"index", "record_start", "record_end", "record_count", "size_bytes", "sha256"}
_IDENTITY_FIELDS = ("records_sha256", "records_bytes", "record_count", "physical_nodes", "expanded_bytes")


def _hash(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _nodes(value):
    pending, count = [value], 0
    while pending:
        item = pending.pop()
        count += 1
        if count > MAX_NODES:
            refuse("LIMIT")
        if type(item) is dict:
            pending.extend(child for pair in item.items() for child in pair)
        elif type(item) is list:
            pending.extend(item)
    return count


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            refuse("INVALID")
        result[key] = value
    return result


def _load(raw, maximum):
    if type(raw) is not bytes or len(raw) > maximum:
        refuse("LIMIT")
    try:
        value = json.loads(raw, object_pairs_hook=_pairs,
            parse_constant=lambda value: refuse("INVALID"))
        if _json(value) != raw:
            refuse("INVALID")
        return value
    except (TypeError, ValueError, UnicodeError, RecursionError):
        refuse("INVALID")


def _prepared(records):
    if type(records) is not list or len(records) > MAX_RECORDS:
        refuse("LIMIT")
    logical, encoded = [], []
    total = expanded = 2
    nodes = 1
    for record in records:
        raw, detached = _canonical_owned_record(record, expanded_limit=max(0, MAX_BYTES - expanded - bool(encoded)))
        expanded += expanded_size(detached) + bool(encoded)
        if expanded > MAX_BYTES:
            refuse("LIMIT")
        if len(raw) + 2 > PAGE_BYTES:
            refuse("LIMIT")
        item = json.loads(raw)
        nodes += _nodes(item)
        total += len(raw) + bool(encoded)
        if nodes > MAX_NODES or total > MAX_BYTES:
            refuse("LIMIT")
        logical.append(detached)
        encoded.append(raw)
    whole = b"[" + b",".join(encoded) + b"]"
    summary = {"records_sha256": _hash(whole), "records_bytes": len(whole), "record_count": len(records),
        "physical_nodes": nodes, "expanded_bytes": expanded}
    return logical, encoded, summary


def collection_summary(records):
    """Summarize independently validated records without retaining files."""
    return {"schema_version": RECEIPT, **encode_collection(records)["summary"]}


def encode_collection(records):
    """Keep legacy arrays exact; explicitly page collections above one MiB."""
    _, encoded, summary = _prepared(records)
    if summary["records_bytes"] <= PAGE_BYTES:
        raw = b"[" + b",".join(encoded) + b"]"
        return {"artifact_bytes": raw, "manifest": None, "pages": (raw,), "summary": summary}
    pages, inventory = [], []
    start, size, current = 0, 2, []
    for index, raw in enumerate(encoded):
        if size + len(raw) + bool(current) > PAGE_BYTES:
            page = b"[" + b",".join(current) + b"]"
            pages.append(page)
            inventory.append(_page(len(pages) - 1, start, index, page))
            start, size, current = index, 2, []
        current.append(raw)
        size += len(raw) + (len(current) > 1)
    if current:
        page = b"[" + b",".join(current) + b"]"
        pages.append(page)
        inventory.append(_page(len(pages) - 1, start, len(encoded), page))
    manifest = {"schema_version": COLLECTION, **summary, "pages": inventory}
    raw = _json(manifest)
    if (len(raw) > PAGE_BYTES or sum(map(len, pages)) + len(raw) > MAX_BYTES
            or summary["physical_nodes"] - 1 + len(pages) + _nodes(manifest) > MAX_NODES):
        refuse("LIMIT")
    return {"artifact_bytes": raw, "manifest": manifest, "pages": tuple(pages), "summary": summary}


def _page(index, start, end, raw):
    return {"index": index, "record_start": start, "record_end": end, "record_count": end - start,
        "size_bytes": len(raw), "sha256": _hash(raw)}


def _integer(value, minimum, maximum):
    return type(value) is int and minimum <= value <= maximum


def _digest(value):
    return type(value) is str and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is not None


def _manifest(artifact, artifact_size):
    if (type(artifact) is not dict or set(artifact) != _FIELDS
            or artifact.get("schema_version") != COLLECTION
            or not _digest(artifact.get("records_sha256"))
            or not _integer(artifact.get("records_bytes"), PAGE_BYTES + 1, MAX_BYTES)
            or not _integer(artifact.get("record_count"), 1, MAX_RECORDS)
            or not _integer(artifact.get("physical_nodes"), 1, MAX_NODES)
            or not _integer(artifact.get("expanded_bytes"), 2, MAX_BYTES)
            or type(artifact.get("pages")) is not list or not 1 <= len(artifact["pages"]) <= MAX_RECORDS):
        refuse("INVALID")
    manifest_nodes = _nodes(artifact)
    end, total = 0, artifact_size
    for index, expected in enumerate(artifact["pages"]):
        if (type(expected) is not dict or set(expected) != _PAGE_FIELDS
                or not all(_integer(expected.get(key), 0, MAX_RECORDS)
                    for key in ("index", "record_start", "record_end", "record_count"))
                or not _integer(expected.get("size_bytes"), 2, PAGE_BYTES)
                or not _digest(expected.get("sha256"))
                or expected["index"] != index or expected["record_start"] != end
                or expected["record_count"] < 1
                or expected["record_end"] != end + expected["record_count"]):
            refuse("INVALID")
        total += expected["size_bytes"]
        end = expected["record_end"]
    if (end != artifact["record_count"]
            or total - artifact_size != artifact["records_bytes"] + len(artifact["pages"]) - 1):
        refuse("INVALID")
    if (total > MAX_BYTES
            or artifact["physical_nodes"] - 1 + len(artifact["pages"]) + manifest_nodes > MAX_NODES):
        refuse("LIMIT")
    return manifest_nodes


def inspect_collection_manifest(artifact_bytes):
    """Preflight declared transport without page I/O, hashes, or authority.

    A bounded canonical JSON array returns None to select the legacy reader;
    its records still require decode_collection validation. A manifest returns
    its closed declared inventory after all ranges and transport totals pass.
    """
    artifact = _load(artifact_bytes, PAGE_BYTES)
    if type(artifact) is list:
        return None
    _manifest(artifact, len(artifact_bytes))
    return artifact


def decode_collection(artifact_bytes, *, pages=()):
    """Read explicit metadata and records, never equating a manifest with its array."""
    artifact = _load(artifact_bytes, PAGE_BYTES)
    if type(pages) not in (tuple, list) or len(pages) > MAX_RECORDS:
        refuse("LIMIT")
    if type(artifact) is list:
        if pages:
            refuse("INVALID")
        records, encoded, summary = _prepared(artifact)
        if b"[" + b",".join(encoded) + b"]" != artifact_bytes:
            refuse("INVALID")
        inventory = []
        kind = "legacy_array"
    else:
        manifest_nodes = _manifest(artifact, len(artifact_bytes))
        if len(pages) != len(artifact["pages"]):
            refuse("INVALID")
        for expected, raw in zip(artifact["pages"], pages, strict=True):
            if type(raw) is not bytes or len(raw) != expected["size_bytes"]:
                refuse("INVALID")
        records, inventory, virtual_nodes = [], [], 1
        for expected, raw in zip(artifact["pages"], pages, strict=True):
            if _hash(raw) != expected["sha256"]:
                refuse("INVALID")
            page = _load(raw, PAGE_BYTES)
            if type(page) is not list or len(page) != expected["record_count"]:
                refuse("INVALID")
            virtual_nodes += _nodes(page) - 1
            if virtual_nodes - 1 + len(pages) + manifest_nodes > MAX_NODES:
                refuse("LIMIT")
            if virtual_nodes > artifact["physical_nodes"]:
                refuse("INVALID")
            records.extend(page)
            inventory.append(dict(expected))
        records, encoded, summary = _prepared(records)
        offset = 0
        for page, expected in zip(pages, inventory, strict=True):
            count = expected["record_count"]
            if b"[" + b",".join(encoded[offset:offset + count]) + b"]" != page:
                refuse("INVALID")
            offset += count
        if any(artifact[key] != summary[key] for key in _IDENTITY_FIELDS):
            refuse("INVALID")
        kind = "paged_collection"
    return {"records": records, "summary": summary, "artifact_kind": kind,
        "artifact_sha256": _hash(artifact_bytes), "artifact_bytes": len(artifact_bytes),
        "page_inventory": inventory}
