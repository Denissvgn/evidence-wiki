"""Explicit bounded observations of selected installed providers and OS tools."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import importlib.metadata
import io
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

from ._pack_io import bounded, canonical, json_document
from .pack_discovery import owner
from .source_contracts import refuse, reject_execution_and_secrets

GROUPS = {"acquisition": "evidence_wiki.acquisition_providers", "discovery": "evidence_wiki.discovery_providers"}
TOOL_ARGS = {"git": ["--version"], "pdftotext": ["-v"]}


def registrations():
    rows, points = [], {}
    bounds = {"limit_per_phase": 64, "observed": 0, "returned": 0, "invalid_metadata": 0, "truncated": False}
    for phase, group in GROUPS.items():
        entries = tuple(importlib.metadata.entry_points(group=group))
        bounds["observed"] += len(entries)
        bounds["truncated"] |= len(entries) > 64
        for entry in entries[:64]:
            if not isinstance(entry.name, str) or re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}", entry.name) is None:
                bounds["invalid_metadata"] += 1
                continue
            dist = entry.dist
            name = dist.metadata.get("Name", "unknown") if dist is not None else "unknown"
            version = dist.version if dist is not None else "unknown"
            if not isinstance(name, str) or not isinstance(version, str) or len(name) > 128 or len(version) > 128:
                refuse("provider_metadata_invalid")
            row = {"id": entry.name, "phase": phase, "distribution": name, "version": version,
                   "entry_point": entry.name,
                   "selector": re.sub(r"[-_.]+", "-", name).lower() + "/" + entry.name,
                   "basis": "entry_point_metadata_not_provider_id", "loaded": False, "capabilities": None}
            rows.append(row)
            points.setdefault((phase, entry.name), []).append(entry)
            points.setdefault((phase, row["selector"]), []).append(entry)
    bounds["returned"] = len(rows)
    return rows, points, bounds


def environment():
    return {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL") if key in os.environ}


def provider_probe(phase, provider_id, request=None):
    if phase not in GROUPS or re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}(?:/[a-zA-Z0-9][a-zA-Z0-9._-]{0,63})?", provider_id) is None:
        refuse("provider_probe_selection_invalid")
    from .orchestration import _execute_bounded

    declared, points, _ = registrations()
    matches = points.get((phase, provider_id), [])
    if len(matches) != 1:
        return {"state": "unavailable", "reason": "provider_selection_not_resolved", "loaded": False, "host_enforced": False}
    identity = next(row for row in declared if row["phase"] == phase and row["id"] == matches[0].name
                    and row["distribution"] == matches[0].dist.metadata.get("Name", "unknown"))
    payload = {"phase": phase, "id": identity["selector"], "request": request}
    with tempfile.TemporaryDirectory(prefix="evidence-wiki-provider-probe-") as temporary:
        result = _execute_bounded([sys.executable, "-B", "-I", "-m", "evidence_wiki.source_probe"], cwd=Path(temporary),
            stdin_text=canonical(payload).decode(), timeout_seconds=5, capture_limit=65_536,
            environment=environment(), inherit_environment=False, preserve_stdout=True)
    if result.returncode or result.timed_out or result.stdout_truncated:
        return {"state": "unavailable", "reason": "provider_probe_failed_or_bounded", "loaded": None,
                "effect": "explicit local process; plugin loading attempted", "host_enforced": False}
    try:
        value = json_document(result.stdout.encode())
        if (not isinstance(value, dict) or value.get("entry_point_selector") != identity["selector"] or value.get("phase") != phase
                or value.get("request_sha256") != hashlib.sha256(canonical(request)).hexdigest()):
            refuse("provider_probe_result_mismatch")
        return value
    except Exception:
        return {"state": "unavailable", "reason": "provider_probe_result_invalid", "loaded": False, "host_enforced": False}


def tool_probe(name):
    if name not in TOOL_ARGS:
        refuse("tool_probe_not_supported")
    from .orchestration import _execute_bounded

    executable = shutil.which(name)
    if executable is None:
        return {"id": name, "state": "missing", "executed": False, "version": None}
    with tempfile.TemporaryDirectory(prefix="evidence-wiki-tool-probe-") as temporary:
        result = _execute_bounded([executable, *TOOL_ARGS[name]], cwd=Path(temporary), stdin_text="", timeout_seconds=3,
            capture_limit=4096, environment=environment(), inherit_environment=False, preserve_stdout=True)
    pattern = r"^git version ([0-9][0-9a-zA-Z.+_-]{0,63})" if name == "git" else r"^pdftotext version ([0-9][0-9a-zA-Z.+_-]{0,63})"
    version = re.search(pattern, result.stdout or result.stderr, re.M)
    ok = result.returncode == 0 and not result.timed_out and not result.stdout_truncated and version is not None
    return {"id": name, "state": "observed" if ok else "unavailable", "executed": True,
            "version": version.group(1) if ok else None, "effect": "explicit selected version process", "host_enforced": False}


class _ProbeOutput(io.StringIO):
    def write(self, value):
        if self.tell() + len(value) > 65_536:
            raise ValueError("provider_probe_output_bound")
        return super().write(value)


def _deny_implicit_io(event, args):
    if event.startswith(("socket.", "subprocess.", "os.exec", "os.spawn")) or event in {"os.system", "os.fork", "pty.fork"}:
        raise ValueError("provider_probe_io_refused")
    if event in {"os.remove", "os.rename", "os.rmdir", "os.mkdir", "os.symlink", "os.link", "os.truncate", "os.chmod", "os.chown", "os.utime"}:
        raise ValueError("provider_probe_write_refused")
    if event == "open":
        mode, flags = args[1:3]
        if isinstance(mode, str) and any(char in mode for char in "wa+") or isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
            raise ValueError("provider_probe_write_refused")


def main():
    try:
        payload = json_document(sys.stdin.buffer.read(65_537))
        if not isinstance(payload, dict) or set(payload) != {"phase", "id", "request"}:
            refuse("provider_probe_input_invalid")
        _, points, _ = registrations()
        key = (payload["phase"], payload["id"])
        if len(points.get(key, [])) != 1:
            refuse("provider_probe_missing_or_collision")
        plugins = owner("_provider_plugins")
        # This is an ordinary selected-plugin probe, not a certified host sandbox.
        # The audit guard prevents accidental network/process/write side effects.
        sys.addaudithook(_deny_implicit_io)
        with contextlib.redirect_stdout(_ProbeOutput()), contextlib.redirect_stderr(_ProbeOutput()):
            registered = plugins._registration_or_reason(points[key][0], payload["phase"])
            if not isinstance(registered, plugins.Registration):
                refuse("provider_probe_registration_invalid")
            summary = registered.capabilities.as_dict()
            request = payload["request"]
            if request is not None:
                if not isinstance(request, dict):
                    refuse("provider_request_shape_invalid")
                reject_execution_and_secrets(request)
                validated = registered.provider.validate_request(copy.deepcopy(request))
                if not isinstance(validated, dict):
                    refuse("provider_validated_request_invalid")
                bounded(validated)
            result = {"id": registered.provider_id, "entry_point_selector": payload["id"], "phase": payload["phase"], "state": "observed", "loaded": True,
                "registration": registered.registration_block(), "capabilities": summary,
                "request_validation": "passed" if request is not None else "not_requested",
                "request_sha256": hashlib.sha256(canonical(request)).hexdigest(), "host_enforced": False,
                "effect": "explicit selected-plugin import and optional request validation; no fetch/search invocation"}
            bounded(result)
        print(json.dumps(result, allow_nan=False))
        return 0
    except BaseException:
        print('{"state":"refused","reason":"provider_probe_refused"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
