#!/usr/bin/env python3
"""Pinned offline packet qualifications carried without authority promotion."""

from __future__ import annotations

from typing import Any

from _packet_vendor_services_context_packet import ContextPacketError, validate_context_packet
from _qualified_packet import IntakeInvalid, validate_delivery
from _snapshot_verifier import SnapshotInvalid, binding, document, fields, items, require

VALIDATOR = "agent-wiki-cli/1.8.0:offline-validation-closure/v1"


def packet_qualification(path: str, data: bytes) -> dict[str, Any]:
    try:
        result = validate_context_packet(data)
    except (ContextPacketError, ValueError, TypeError) as exc:
        raise SnapshotInvalid("snapshot_context_packet_invalid") from exc
    payload = result.packet.to_payload()
    omitted = []
    if "content" in payload["response"]:
        del payload["response"]["content"]
        omitted.append("/response/content")
    return {"path": path, "original": binding(data), "validator": VALIDATOR,
            "packet_id": result.packet_id, "native_validation": result.to_payload(),
            "qualifications": payload, "omitted_fields": omitted,
            "worker_authentication": "not_established", "live_reconciliation": "not_evaluated"}


def qualify_source(source: dict[str, Any], files: dict[str, bytes]) -> list[dict[str, Any]]:
    """Inspect captured bytes only; no originating paths or tools are opened."""
    prefix = source["descriptor"]["evidence_root"]
    if prefix is None:
        return []
    originals = {path[len(prefix) + 1:]: data for path, data in files.items() if path.startswith(prefix + "/")}
    paths = set()
    if "artifact-manifest.json" in originals:
        try:
            _manifest, packet_path = validate_delivery(originals, source["source_id"])
        except IntakeInvalid as exc:
            raise SnapshotInvalid("snapshot_packet_delivery_invalid") from exc
        paths.add(packet_path)
    if "execution-record.json" in originals:
        record = document(originals["execution-record.json"])
        for member in items(record.get("artifacts"), 255):
            fields(member, {"path", "content_hash", "size_bytes", "role"})
            if member["role"] not in {"context", "context-packet"}:
                continue
            path = member["path"]
            require(isinstance(path, str) and path in originals, "snapshot_packet_artifact_missing")
            required = member["role"] == "context-packet"
            packet = document(originals[path]) if required or path.endswith(".json") else {}
            if required or str(packet.get("schema_version", "")).startswith("llm-wiki-qualified-context-packet/"):
                paths.add(path)
    return [packet_qualification(prefix + "/" + path, originals[path]) for path in sorted(paths)]
