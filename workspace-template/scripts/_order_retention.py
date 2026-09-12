#!/usr/bin/env python3
"""Explicit session retirement and archive-backed cleanup of owned claim ledgers.

The marker and lock inodes are permanent. Cleanup removes live ledger payloads
only; archived bytes and all other workspace evidence remain available to their
consumers. Callers with a payload-erasure obligation must resolve that obligation
before requesting archive retention. This module never infers that evidence is
unused from a missing reference or from revocation.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from contextlib import ExitStack
from pathlib import Path
from typing import Any

MAX_ARCHIVE_FILES = 4096
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MARKER_TYPE = "orchestration_retirement"


def refuse(controller: Any, message: str) -> None:
    raise controller.OrchestrationControllerError(
        "ORCHESTRATION_RETENTION_UNSAFE", message,
        remediation="Retain the evidence and resolve the reported state; preserve all locks and retirement markers.",
    )


def safe_path(controller: Any, root: Path, path: Path) -> None:
    """Reject linked ancestors before creating locks, archives, or removing payloads."""
    for part in (path, *path.parents):
        if part == root:
            break
        try:
            metadata = part.lstat()
        except FileNotFoundError:
            continue
        if controller.path_is_link_like(part, metadata):
            refuse(controller, f"linked retention path: {part.relative_to(root)}")


def read_bytes(controller: Any, root: Path, path: Path) -> bytes:
    safe_path(controller, root, path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > MAX_ARCHIVE_BYTES:
        refuse(controller, f"unsafe or oversized retention evidence: {path.relative_to(root)}")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        payload = stream.read(MAX_ARCHIVE_BYTES + 1)
        after = os.fstat(stream.fileno())
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
    ) or (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino) or len(payload) != before.st_size:
        refuse(controller, "retention evidence changed while being read")
    return payload


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def terminal_session(controller: Any, root: Path, identity: str) -> dict:
    read_bytes(controller, root, controller.session_path(root, identity))
    session = controller.load_session(root, identity)
    if session["status"] not in controller.TERMINAL_STATUSES or not session.get("completed_at"):
        refuse(controller, "only a completed terminal session can be retired")
    if any(session.get(key) is not None for key in (
        "pending_action_id", "pending_submission", "pending_trusted_static_inputs", "active_run_id",
    )):
        refuse(controller, "pending work or an active child prevents retirement")
    recovery = session.get("recovery", {})
    if not isinstance(recovery, dict) or recovery.get("state") not in (None, "none"):
        refuse(controller, "unresolved recovery prevents retirement")
    return session


def inventory(controller: Any, claims: Any, root: Path, identity: str) -> tuple[dict, dict]:
    terminal_session(controller, root, identity)
    session_root = controller.session_dir(root, identity)
    payloads: dict[str, bytes] = {}
    for path in sorted(session_root.rglob("*")):
        if ".locks" in path.relative_to(session_root).parts:
            continue
        safe_path(controller, root, path)
        if not path.is_dir():
            payloads[path.relative_to(root).as_posix()] = read_bytes(controller, root, path)
        if len(payloads) > MAX_ARCHIVE_FILES or sum(map(len, payloads.values())) > MAX_ARCHIVE_BYTES:
            refuse(controller, "session archive exceeds the bounded retention inventory")
    eligible, retained = [], []
    directory = claims.claims_dir(root, identity)
    safe_path(controller, root, directory)
    for count, path in enumerate(sorted(directory.iterdir()) if directory.exists() else [], 1):
        if count > MAX_ARCHIVE_FILES:
            refuse(controller, "claim directory exceeds the bounded retention inventory")
        if path.name in {".locks", ".retired.json"}:
            continue
        try:
            action = claims.require_safe_id(path.stem, "action_id")
            if path.suffix != ".json" or path.name.startswith("."):
                raise ValueError("unknown member or incomplete write")
            payload = read_bytes(controller, root, path)
            document = claims.load_claims(path)
            if document.get("orchestration_id") != identity or document.get("action_id") != action:
                raise ValueError("ambiguous ledger ownership")
            order_path = controller.work_order_path(root, identity, action).relative_to(root).as_posix()
            order = json.loads(payloads.get(order_path, b"null"))
            if not isinstance(order, dict) or (
                order.get("schema_version") != controller.SCHEMA_VERSION
                or order.get("artifact_type") != controller.WORK_ORDER_ARTIFACT_TYPE
                or order.get("orchestration_id") != identity or order.get("action_id") != action
            ):
                raise ValueError("no retained work order proves ownership")
        except (ValueError, OSError, claims.OrderClaimError, controller.OrchestrationControllerError) as error:
            retained.append({"path": path.relative_to(root).as_posix(), "reason": str(error)})
            continue
        relative = path.relative_to(root).as_posix()
        payloads[relative] = payload
        eligible.append(relative)
    if len(payloads) > MAX_ARCHIVE_FILES or sum(map(len, payloads.values())) > MAX_ARCHIVE_BYTES:
        refuse(controller, "claim archive exceeds the bounded retention inventory")
    return {"evidence": {path: digest(value) for path, value in payloads.items()},
            "eligible": eligible, "retained": retained}, payloads


def archive_path(root: Path, identity: str, sha256: str) -> Path:
    return root / "runs" / "order-claim-archives" / identity / sha256


def publish_bytes(path: Path, payload: bytes) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".retention-", delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    if os.name != "nt":
        descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def write_archive(controller: Any, root: Path, identity: str, payloads: dict[str, bytes]) -> None:
    for payload in payloads.values():
        path = archive_path(root, identity, digest(payload))
        safe_path(controller, root, path)
        if os.path.lexists(path):
            if read_bytes(controller, root, path) != payload:
                refuse(controller, "existing archive differs from its content identity")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic publication; an interrupted temporary is preserved, never credited.
        publish_bytes(path, payload)
        if read_bytes(controller, root, path) != payload:
            refuse(controller, "archive verification failed")


def read_marker(controller: Any, claims: Any, root: Path, identity: str) -> dict:
    marker = json.loads(read_bytes(controller, root, claims.retirement_path(root, identity)))
    if not isinstance(marker, dict) or marker.get("schema_version") != 1 or (
        marker.get("artifact_type") != MARKER_TYPE or marker.get("orchestration_id") != identity
        or marker.get("payload_policy") != "retain" or not isinstance(marker.get("evidence"), dict)
        or not isinstance(marker.get("eligible"), list) or not isinstance(marker.get("retained"), list)
        or not isinstance(marker.get("reason"), str) or not marker["reason"].strip()
        or not isinstance(marker.get("retired_at"), str) or not marker["retired_at"]
    ):
        refuse(controller, "retirement marker is not a supported retirement proof")
    if any(len(marker[key]) > MAX_ARCHIVE_FILES for key in ("evidence", "eligible", "retained")):
        refuse(controller, "retirement proof exceeds the bounded inventory")
    if any(not isinstance(row, dict) or not isinstance(row.get("path"), str)
           or not isinstance(row.get("reason"), str) for row in marker["retained"]):
        refuse(controller, "retirement proof has invalid retained members")
    session_prefix = f"runs/orchestrations/{identity}/"
    claims_prefix = f"runs/order-claims/{identity}/"
    archived = {}
    total_bytes = 0
    for relative, sha in marker["evidence"].items():
        if not isinstance(sha, str) or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            refuse(controller, "invalid archive identity")
        if (not isinstance(relative, str) or not relative.startswith((session_prefix, claims_prefix))
                or ".." in relative or "\\" in relative or Path(relative).as_posix() != relative):
            refuse(controller, "retirement proof names evidence outside this session")
        payload = read_bytes(controller, root, archive_path(root, identity, sha))
        total_bytes += len(payload)
        if total_bytes > MAX_ARCHIVE_BYTES:
            refuse(controller, "retirement archive exceeds the byte bound")
        if digest(payload) != sha:
            refuse(controller, "required retirement archive is missing or changed")
        archived[relative] = payload
    session_relative = controller.session_path(root, identity).relative_to(root).as_posix()
    if session_relative not in marker["evidence"]:
        refuse(controller, "retirement proof has no session evidence")
    for relative in marker["eligible"]:
        if not isinstance(relative, str) or relative not in marker["evidence"]:
            refuse(controller, "retirement proof has an unarchived ledger")
        action = claims.require_safe_id(Path(relative).stem, "action_id")
        if relative != claims.claims_path(root, identity, action).relative_to(root).as_posix():
            refuse(controller, "retirement proof names an unknown ledger")
        order = controller.work_order_path(root, identity, action).relative_to(root).as_posix()
        if order not in marker["evidence"]:
            refuse(controller, "retirement proof has no owning order")
        order_document, ledger = json.loads(archived[order]), json.loads(archived[relative])
        if not isinstance(order_document, dict) or not isinstance(ledger, dict) or any(
            document.get("orchestration_id") != identity or document.get("action_id") != action
            for document in (order_document, ledger)
        ) or (order_document.get("schema_version") != controller.SCHEMA_VERSION
              or order_document.get("artifact_type") != controller.WORK_ORDER_ARTIFACT_TYPE):
            refuse(controller, "archived ledger and order do not prove ownership")
    if len(set(marker["eligible"])) != len(marker["eligible"]):
        refuse(controller, "retirement proof repeats a ledger")
    # Neither a tombstone nor an archive permits cleanup after session drift.
    for relative, sha in marker["evidence"].items():
        if relative.startswith(session_prefix) and digest(read_bytes(controller, root, root / relative)) != sha:
            refuse(controller, "retired session evidence changed")
    terminal_session(controller, root, identity)
    return marker


def cleanup_plan(controller: Any, claims: Any, root: Path, identity: str, marker: dict) -> dict:
    eligible, absent, retained = [], [], list(marker.get("retained", []))
    for relative in marker["eligible"]:
        path = root / relative
        if not os.path.lexists(path):
            absent.append(relative)
        elif digest(read_bytes(controller, root, path)) != marker["evidence"][relative]:
            retained.append({"path": relative, "reason": "ledger differs from retirement archive"})
        else:
            eligible.append(relative)
    directory = claims.claims_dir(root, identity)
    known = set(marker["eligible"]) | {row["path"] for row in retained}
    for path in sorted(directory.iterdir()):
        relative = path.relative_to(root).as_posix()
        if path.name not in {".locks", ".retired.json"} and relative not in known:
            retained.append({"path": relative, "reason": "not in the retirement proof"})
    return {"eligible": eligible, "already_absent": absent, "retained": retained}


def execute(controller: Any, root: Path, args: Any) -> dict:
    claims = controller.load_sibling_module("_order_claims")
    identity = controller.require_safe_id(args.orchestration_id, "orchestration_id")
    result = {"schema_version": 1, "artifact_type": "orchestration_retention_result",
              "orchestration_id": identity, "operation": args.command, "applied": args.apply,
              "payload_policy": "retain", "locks_and_tombstone": "retained"}
    if getattr(args, "payload_policy", "retain") != "retain":
        refuse(controller, "payload deletion is required; archive retention cannot satisfy that obligation")
    if args.command == "retire" and (not isinstance(args.reason, str) or not args.reason.strip()):
        refuse(controller, "retirement requires a nonempty reason")
    try:
        with ExitStack() as stack:
            safe_path(controller, root, controller.session_lock_path(root, identity))
            safe_path(controller, root, claims.retention_lock_path(root, identity))
            # Dry run creates no directories, lock files, holder records or archives.
            if args.apply:
                stack.enter_context(controller.driver_session_lock(
                    root, identity, command=args.command, agent_id=None,
                    wait_seconds=args.driver_wait_seconds,
                ))
                stack.enter_context(controller.workspace_lock(
                    claims.retention_lock_path(root, identity), timeout_seconds=0, purpose="claim retention",
                ))
            marker_path = claims.retirement_path(root, identity)
            if os.path.lexists(marker_path):
                marker = read_marker(controller, claims, root, identity)
                if args.command == "retire":
                    return dict(result, status="already_retired", **cleanup_plan(controller, claims, root, identity, marker))
            elif args.command == "cleanup-claims":
                refuse(controller, "explicit retirement with verified archives is required before cleanup")
            else:
                plan, payloads = inventory(controller, claims, root, identity)
                if not args.apply:
                    return dict(result, status="retirement_planned", **plan)
            if args.apply:
                # Also coordinate with callers holding an individual action lock.
                paths = marker["eligible"] if args.command == "cleanup-claims" else plan["eligible"]
                for relative in sorted(paths):
                    lock = claims.claims_lock_path(root, identity, Path(relative).stem)
                    safe_path(controller, root, lock)
                    stack.enter_context(controller.workspace_lock(lock, timeout_seconds=0, purpose="claim cleanup"))
            if args.command == "retire":
                plan, payloads = inventory(controller, claims, root, identity)
                write_archive(controller, root, identity, payloads)
                # No deletion precedes a fully archived, revalidated proof.
                if inventory(controller, claims, root, identity)[0] != plan:
                    refuse(controller, "retirement inventory changed during archive creation")
                marker = dict(plan, schema_version=1, artifact_type=MARKER_TYPE, orchestration_id=identity,
                              retired_at=controller.timestamp_utc(), reason=args.reason, payload_policy="retain")
                publish_bytes(marker_path, (json.dumps(marker, indent=2, sort_keys=True) + "\n").encode())
                return dict(result, status="retired", **plan)
            # Revalidate proof and live bytes after all relevant locks are held.
            marker = read_marker(controller, claims, root, identity)
            plan = cleanup_plan(controller, claims, root, identity, marker)
            if args.apply:
                for relative in plan["eligible"]:
                    (root / relative).unlink()
            return dict(result, status="cleaned" if args.apply else "cleanup_planned", **plan)
    except (ValueError, TypeError, KeyError, OSError, claims.OrderClaimError) as error:
        refuse(controller, f"retention evidence is unavailable or invalid: {error}")
    raise AssertionError("unreachable")
