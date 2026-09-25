#!/usr/bin/env python3
"""Prepare and publish host-authorized snapshots; verify frozen evidence offline."""

from __future__ import annotations

import argparse
import contextlib
import json
import stat
import sys
from pathlib import Path
from typing import Any

from _evidence_authority import EvidenceInvalid
from _native_fs import os
from _script_errors import ScriptRefusal, emit_refusal
from _snapshot_export import check, export, prepare
from _snapshot_qualifications import qualify_source
from _snapshot_verifier import BOUNDS, SnapshotInvalid, document, require, verify_snapshot
from evidence_usage import configuration


class SnapshotRefusal(ScriptRefusal, SystemExit):
    """A typed refusal that survives isolated script-module families."""


def refusal(reason: str) -> SnapshotRefusal:
    return SnapshotRefusal(
        "EVIDENCE_SNAPSHOT_REFUSED", "Evidence snapshot operation refused.",
        exit_code=2, recoverable=True,
        details={"reason": reason},
        remediation="Check the declared selection, independent authority and current host registration before retrying.",
    )


def run_operation(project_root: Path, operation, *args) -> dict[str, Any]:
    root = Path(project_root).resolve()
    try:
        return operation(root, configuration(root), *args)
    except (EvidenceInvalid, SnapshotInvalid) as exc:
        raise refusal(str(exc)) from exc
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
        raise refusal("snapshot_invalid_or_unreadable_input") from exc


def run_prepare(project_root: Path, *, selection: dict[str, Any]) -> dict[str, Any]:
    return run_operation(project_root, prepare, selection)


def run_export(project_root: Path, *, selection: dict[str, Any], registration_request_id: str) -> dict[str, Any]:
    return run_operation(project_root, export, selection, registration_request_id)


def run_check(project_root: Path, *, data: bytes) -> dict[str, Any]:
    return run_operation(project_root, check, data)


def run_verify(*, data: bytes, trust_policy_bytes: bytes) -> dict[str, Any]:
    """No originating workspace or host-state access occurs in this operation."""
    return verify_snapshot(data, trust_policy_bytes=trust_policy_bytes, qualify_source=qualify_source)


def read_trust(path: str) -> bytes:
    """Read an explicitly selected private regular file through anchored directories."""
    selected = Path(path).expanduser().absolute()
    require(".." not in selected.parts, "snapshot_trust_path_invalid")
    require(hasattr(os, "O_NOFOLLOW") and os.open in os.supports_dir_fd, "snapshot_trust_file_platform_unsupported")
    with contextlib.ExitStack() as stack:
        directory = os.open(selected.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        stack.callback(os.close, directory)
        anchors = []
        for component in selected.parts[1:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            stack.callback(os.close, child)
            anchors.append((directory, component, os.fstat(child)))
            directory = child
        descriptor = os.open(selected.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        stack.callback(os.close, descriptor)
        before = os.fstat(descriptor)
        private = (before.native_private if hasattr(before, "native_private")
                   else not before.st_mode & 0o077 and before.st_uid == os.getuid())
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and private
                and 0 < before.st_size <= 1024 * 1024, "snapshot_trust_file_unsafe")
        chunks, size = [], 0
        while True:
            block = os.read(descriptor, min(65536, 1024 * 1024 + 1 - size))
            if not block:
                break
            chunks.append(block)
            size += len(block)
            require(size <= 1024 * 1024, "snapshot_trust_file_bound_exceeded")
        require(before == os.fstat(descriptor)
                and before == os.stat(selected.name, dir_fd=directory, follow_symlinks=False), "snapshot_trust_file_changed")
        for parent, component, observed in anchors:
            current = os.stat(component, dir_fd=parent, follow_symlinks=False)
            require((current.st_dev, current.st_ino) == (observed.st_dev, observed.st_ino), "snapshot_trust_path_changed")
        return b"".join(chunks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("prepare", "export", "check", "verify"))
    parser.add_argument("--project-root")
    parser.add_argument("--registration-request-id")
    parser.add_argument("--trust-policy")
    parser.add_argument("--format", choices=("json",), default="json")
    args = parser.parse_args(argv)
    if args.operation == "verify":
        if not args.trust_policy or args.project_root is not None or args.registration_request_id is not None:
            parser.error("verify requires --trust-policy and accepts no workspace or registration options")
    elif args.trust_policy is not None or (args.operation == "export") != (args.registration_request_id is not None):
        parser.error("only export requires --registration-request-id; --trust-policy belongs to verify")
    try:
        maximum = 1024 * 1024 if args.operation in {"prepare", "export"} else BOUNDS["bundle_bytes"]
        data = sys.stdin.buffer.read(maximum + 1)
        require(len(data) <= maximum, "snapshot_transport_bound_exceeded")
        if args.operation == "verify":
            report = run_verify(data=data, trust_policy_bytes=read_trust(args.trust_policy))
        else:
            root = Path(args.project_root or ".")
            if args.operation == "prepare":
                report = run_prepare(root, selection=document(data, maximum))
            elif args.operation == "export":
                report = run_export(root, selection=document(data, maximum), registration_request_id=args.registration_request_id)
            else:
                report = run_check(root, data=data)
        print(json.dumps(report, sort_keys=True, indent=2))
        return int(args.operation == "verify" and not report["valid"] or args.operation == "check" and not report["eligible"])
    except ScriptRefusal as exc:
        return emit_refusal(exc, json_mode=True)
    except (OSError, SnapshotInvalid) as exc:
        return emit_refusal(refusal(str(exc) if isinstance(exc, SnapshotInvalid) else "snapshot_trust_unreadable"), json_mode=True)


if __name__ == "__main__":
    raise SystemExit(main())
