#!/usr/bin/env python3
"""Pinned offline packet qualifications carried without authority promotion."""

from __future__ import annotations

from typing import Any

import yaml
from _evidence_authority import EvidenceInvalid
from _market_evidence import validate_captured
from _market_evidence import validate_closure as validate_market
from _packet_vendor_services_context_packet import ContextPacketError, validate_context_packet
from _qualified_packet import IntakeInvalid, validate_delivery
from _snapshot_verifier import ClaimLoader, SnapshotInvalid, binding, document, fields, items, require
from _usage_gate import requires_authority

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
    originals = {} if prefix is None else {path[len(prefix) + 1:]: data for path, data in files.items() if path.startswith(prefix + "/")}
    normalized = files.get(source["descriptor"]["normalized_path"], b"")
    metadata = {}
    try:
        lines = normalized.decode("utf-8").splitlines()
        if lines and lines[0] == "---" and "---" in lines[1:4098]:
            end = lines.index("---", 1)
            parsed = yaml.load("\n".join(lines[1:end]), Loader=ClaimLoader)  # noqa: S506 -- restricted SafeLoader subclass
            metadata = parsed if isinstance(parsed, dict) else {}
    except (UnicodeError, yaml.YAMLError, RecursionError) as exc:
        raise SnapshotInvalid("snapshot_normalized_metadata_invalid") from exc
    market_required = "market-record.json" in originals or requires_authority([metadata])
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
    qualifications = [packet_qualification(prefix + "/" + path, originals[path]) for path in sorted(paths)]
    if market_required:
        try:
            market = validate_market(source["source_id"], originals)
            temporal = source["descriptor"]["temporal"]
            validate_captured(market, metadata, files, temporal.get("record_path"))
        except (EvidenceInvalid, ValueError, TypeError, KeyError, AttributeError, yaml.YAMLError, RecursionError) as exc:
            raise SnapshotInvalid("snapshot_market_delivery_invalid") from exc
        qualifications.append({"path": prefix + "/market-record.json", "validator": "market_evidence/v1",
                               "qualifications": market, "provider_authentication": "not_established"})
    return qualifications
