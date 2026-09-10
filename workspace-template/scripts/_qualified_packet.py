"""Opt-in, offline intake of original qualified-context packet bytes.

Delivery hashes bind deposited files. Native validation checks the producer's
canonical envelope and internal qualifications. Neither authenticates a worker
nor reconciles its source observations with a live checkout.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from _evidence_revision import observation, read_observed_file
from _normalized_contract import safe_source_id
from _packet_vendor_services_context_packet import ContextPacketError, validate_context_packet
from _script_errors import ScriptRefusal
from _workspace_module_loader import load_workspace_module

PROFILE = "qualified_context_packet/v1"
VALIDATOR = "agent-wiki-cli/1.8.0:offline-validation-closure/v1"
MANIFEST = "artifact-manifest.json"
MAX_FILES = 128
MAX_ENTRIES = 512
MAX_BYTES = 16 * 1024 * 1024
MAX_DEPTH = 64


class IntakeInvalid(ValueError):
    """Stable refusal reason for an inert delivery, without echoing its contents."""


def profiles() -> dict[str, Any]:
    return {
        "schema_version": "evidence-intake-profiles/v1",
        "profiles": [{"name": PROFILE, "validator": VALIDATOR,
                      "packet_schemas": ["llm-wiki-qualified-context-packet/v1", "llm-wiki-qualified-context-packet/v2"],
                      "network_io_executed": False, "producer_execution": False,
                      "live_reconciliation": "unsupported", "worker_authentication": "not_established",
                      "max_files": MAX_FILES, "max_entries": MAX_ENTRIES, "max_bytes": MAX_BYTES}],
    }


def profile_for(config: dict[str, Any], record: dict[str, Any], normalized: dict[str, Any] | None = None) -> str | None:
    metadata = record.get("metadata") or {}
    metadata = metadata if isinstance(metadata, dict) else {}
    integrations = config.get("integrations") or {}
    codebase = integrations.get("codebase_analysis") or {} if isinstance(integrations, dict) else {}
    codebase = codebase if isinstance(codebase, dict) else {}
    declared = []
    if record.get("kind") == "codebase_architecture" and "intake_profile" in codebase:
        declared.append(codebase["intake_profile"])
    if "codebase_intake_profile" in metadata:
        declared.append(metadata["codebase_intake_profile"])
    if normalized is not None and "qualified_context" in normalized and normalized["qualified_context"] is not None:
        report = normalized["qualified_context"]
        declared.append(report.get("profile") if isinstance(report, dict) else None)
    if not declared:
        return None
    if any(value != PROFILE for value in declared):
        raise IntakeInvalid("unsupported_or_conflicting_intake_profile")
    return PROFILE


def portable_path(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise IntakeInvalid("unsafe_delivery_path")
    for part in value.split("/"):
        if (not part or part.startswith(".") or part.endswith((".", " "))
                or any(character in part for character in '\\:<>"|?*')
                or any(ord(character) < 32 or ord(character) == 127 for character in part)):
            raise IntakeInvalid("unsafe_delivery_path")
        if re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part):
            raise IntakeInvalid("unsafe_delivery_path")
    return value


def artifact_relative(config: dict[str, Any], record: dict[str, Any]) -> str:
    integrations = config.get("integrations") or {}
    settings = integrations.get("codebase_analysis") or {} if isinstance(integrations, dict) else {}
    settings = settings if isinstance(settings, dict) else {}
    output = portable_path(settings.get("output_dir", "sources/code_wikis"))
    if not output.startswith("sources/"):
        raise IntakeInvalid("unsafe_delivery_path")
    expected = portable_path(f"{output}/{safe_source_id(record['id'])}")
    metadata = record.get("metadata") or {}
    if isinstance(metadata, dict) and metadata.get("codebase_output_dir", expected) != expected:
        raise IntakeInvalid("delivery_directory_mismatch")
    return expected


@contextmanager
def open_directory(root: Path, relative: str):
    if os.open not in os.supports_dir_fd or os.scandir not in os.supports_fd or not hasattr(os, "O_NOFOLLOW"):
        raise IntakeInvalid("delivery_capture_unsupported")
    descriptors = []
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        current = os.open(root, flags)
        descriptors.append(current)
        for part in portable_path(relative).split("/"):
            current = os.open(part, flags, dir_fd=current)
            descriptors.append(current)
        yield current
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def observe_delivery(root: Path, relative: str) -> dict[str, tuple[int, ...]]:
    """Bound membership before sorting, traversing only anchored directory descriptors."""
    observed = {}
    entries_seen = 0
    files_seen = 0
    bytes_seen = 0

    def visit(descriptor: int, prefix: str, depth: int) -> None:
        nonlocal entries_seen, files_seen, bytes_seen
        if depth > MAX_DEPTH:
            raise IntakeInvalid("delivery_bound_exceeded")
        observed[prefix] = observation(os.fstat(descriptor))
        with os.scandir(descriptor) as entries:
            names = []
            for entry in entries:
                entries_seen += 1
                if entries_seen > MAX_ENTRIES:
                    raise IntakeInvalid("delivery_bound_exceeded")
                names.append(entry.name)
        folded = set()
        for name in sorted(names):
            portable_path(name)
            if name.casefold() in folded:
                raise IntakeInvalid("delivery_path_collision")
            folded.add(name.casefold())
            path = f"{prefix}/{name}"
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                try:
                    if observation(os.fstat(child)) != observation(info):
                        raise IntakeInvalid("delivery_changed")
                    visit(child, path, depth + 1)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and Path(name).suffix.lower() in {".json", ".md", ".txt"}:
                files_seen += 1
                bytes_seen += info.st_size
                if files_seen > MAX_FILES + 1 or bytes_seen > MAX_BYTES:
                    raise IntakeInvalid("delivery_bound_exceeded")
                observed[path] = observation(info)
            else:
                raise IntakeInvalid("unsafe_delivery_entry")

    with open_directory(root, relative) as descriptor:
        visit(descriptor, relative, 0)
    return observed


def capture_delivery(root: Path, relative: str) -> dict[str, bytes]:
    """Capture one bounded directory generation; never reopen members for rendering."""
    for _attempt in range(3):
        try:
            before = observe_delivery(root, relative)
            files = {name[len(relative) + 1:]: read_observed_file(root, name, identity)
                     for name, identity in before.items() if stat.S_ISREG(identity[2])}
            if before == observe_delivery(root, relative):
                return files
        except ScriptRefusal as exc:
            if exc.error_code != "EVIDENCE_REVISION_CHANGED":
                raise IntakeInvalid("delivery_capture_refused") from exc
    raise IntakeInvalid("delivery_changed")


def strict_json(data: bytes) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise IntakeInvalid("duplicate_delivery_key")
            result[key] = value
        return result

    def nonfinite(_value):
        raise IntakeInvalid("nonfinite_delivery_number")

    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=pairs, parse_constant=nonfinite)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise IntakeInvalid("invalid_delivery_json") from exc
    pending = [(value, 0)]
    count = 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if count > 250_000 or depth > MAX_DEPTH:
            raise IntakeInvalid("delivery_json_bound_exceeded")
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
    return value


def validate_delivery(files: dict[str, bytes], source_id: str) -> tuple[dict[str, Any], str]:
    if MANIFEST not in files:
        raise IntakeInvalid("delivery_manifest_missing")
    manifest = strict_json(files[MANIFEST])
    if (not isinstance(manifest, dict) or manifest.get("schema_version") != "1"
            or manifest.get("artifact_kind") != "codebase_evidence" or manifest.get("source_id") != source_id
            or manifest.get("intake_profile") != PROFILE):
        raise IntakeInvalid("delivery_manifest_invalid")
    producer = manifest.get("producer")
    invocation = manifest.get("invocation")
    if (not isinstance(manifest.get("generated_at"), str) or not manifest["generated_at"].strip()
            or not isinstance(producer, dict)
            or not all(isinstance(producer.get(key), str) and producer[key].strip() for key in ("name", "version"))
            or not isinstance(invocation, dict) or invocation.get("executed_by") != "external_worker"
            or not isinstance(invocation.get("argv"), list) or not invocation["argv"]
            or not all(isinstance(item, str) and item.strip() for item in invocation["argv"])
            or any(invocation.get(key) is not False for key in ("plugins_enabled", "hooks_enabled", "network_access"))):
        raise IntakeInvalid("delivery_provenance_invalid")
    declared = manifest.get("files")
    if not isinstance(declared, list) or not 1 <= len(declared) <= MAX_FILES:
        raise IntakeInvalid("delivery_members_invalid")
    paths = set()
    for item in declared:
        if not isinstance(item, dict):
            raise IntakeInvalid("delivery_members_invalid")
        path = portable_path(item.get("path"))
        if path == MANIFEST or path in paths or path not in files:
            raise IntakeInvalid("delivery_members_mismatch")
        paths.add(path)
        if (type(item.get("size_bytes")) is not int or item["size_bytes"] != len(files[path])
                or item.get("sha256") != hashlib.sha256(files[path]).hexdigest()):
            raise IntakeInvalid("delivery_checksum_mismatch")
    if paths != set(files) - {MANIFEST}:
        raise IntakeInvalid("delivery_members_mismatch")
    packet_path = portable_path(manifest.get("packet_path"))
    if packet_path not in paths:
        raise IntakeInvalid("delivery_packet_missing")
    return manifest, packet_path


def inspect_packet(root: Path, config: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    """Validate the original bytes and preserve every qualification in a loss-explicit view."""
    usage = load_workspace_module(Path(__file__).resolve().parent, "_usage_gate")
    EvidenceInvalid = usage.EvidenceInvalid

    report: dict[str, Any] = {
        "profile": PROFILE, "validator": VALIDATOR, "source_id": record.get("id"), "valid": False,
        "delivery_integrity": "not_validated", "native_validation": None,
        "host_reconciliation": {"state": "unevaluated", "reason": "no-live-source-or-trusted-reconciliation"},
        "worker_authentication": "not_established", "network_io_executed": False,
    }
    try:
        if profile_for(config, record) != PROFILE:
            raise IntakeInvalid("intake_profile_not_enabled")
        raw_intake = (record.get("metadata") or {}).get("codebase_intake")
        if isinstance(raw_intake, dict) and raw_intake.get("bounded") is False:
            raise IntakeInvalid("raw_snapshot_bound_exceeded")
        relative = artifact_relative(config, record)
        files = usage.original_artifacts(root, config, record)
        if files is None:
            files = capture_delivery(root, relative)
        manifest, packet_path = validate_delivery(files, record["id"])
        report["delivery_integrity"] = "valid"
        report["delivery_manifest"] = {"path": f"{relative}/{MANIFEST}", "sha256": hashlib.sha256(files[MANIFEST]).hexdigest(), "provenance": manifest}
        data = files[packet_path]
        report["original"] = {"path": f"{relative}/{packet_path}", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        validation = validate_context_packet(data)
        payload = validation.packet.to_payload()
        omitted = []
        if "content" in payload["response"]:
            del payload["response"]["content"]
            omitted.append("/response/content")
        report.update(valid=True, native_validation=validation.to_payload(), packet_id=validation.packet_id,
                      qualifications=payload, omitted_fields=omitted)
        settings = (config.get("integrations") or {}).get("codebase_analysis") or {}
        required = settings.get("require_live_reconciliation", False)
        if type(required) is not bool:
            raise IntakeInvalid("invalid_live_reconciliation_policy")
        report["policy_satisfied"] = not required
        report["policy_reason"] = "live_reconciliation_required" if required else "structural_intake_accepted"
    except ContextPacketError as exc:
        report["reason"] = getattr(exc, "code", "native_packet_invalid")
    except (IntakeInvalid, EvidenceInvalid) as exc:
        report["valid"] = False
        report["reason"] = str(exc)
    except (OSError, KeyError, TypeError, AttributeError, UnicodeError, RecursionError):
        report["valid"] = False
        report["reason"] = "delivery_unreadable_or_invalid"
    return report


def normalized_issues(root: Path | None, config: dict[str, Any], record: dict[str, Any], normalized: dict[str, Any]) -> list[str]:
    """Recheck original evidence for both native and external normalized records."""
    try:
        if profile_for(config, record, normalized) is None:
            return []
    except IntakeInvalid as exc:
        return [str(exc)]
    if root is None:
        return ["packet_original_context_required"]
    report = inspect_packet(root, config, record)
    if report != normalized.get("qualified_context"):
        return ["packet_normalized_binding_mismatch"]
    if not report["valid"]:
        return [report.get("reason", "native_packet_invalid")]
    if not report["policy_satisfied"]:
        return [report["policy_reason"]]
    return []
