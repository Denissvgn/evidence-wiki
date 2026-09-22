"""Caller declarations and narrowly demonstrated capture artifacts, without tools."""

from __future__ import annotations

import os
import re
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

from .pack_discovery import owner
from .source_contracts import HOST_TOOLS, decode, refuse, reject_execution_and_secrets


def known_credentials(names=()):
    selected = {"OPENALEX_API_KEY", "GITHUB_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", *names}
    selected.update(name for name in os.environ if re.search(r"(?:TOKEN|SECRET|PASSWORD|API_KEY|ACCESS_KEY|PRIVATE_KEY)$", name))
    return tuple(os.environ.get(name, "") for name in selected)


def read_tools(raw=None):
    if raw is None:
        return []
    value = decode(HOST_TOOLS, raw)
    tools = value["tools"]
    refs = [name for tool in tools for name in tool["credential_refs"]]
    reject_execution_and_secrets(value, credential_values=known_credentials(refs))
    identifiers = [tool["id"] for tool in tools]
    if len(set(identifiers)) != len(identifiers):
        refuse("host_tool_ids_duplicate")
    for tool in tools:
        owner("_host_capture").identifier(tool["id"])
        owner("_host_capture").text(tool["version"], 128)
        for key in ("operations", "formats", "credential_refs", "claims"):
            if len(tool[key]) != len(set(tool[key])):
                refuse("host_tool_values_duplicate")
        for scope in tool["scope"]:
            if scope["kind"] == "uri_prefix":
                owner("_host_capture").origin(scope["value"])
                parsed = urlsplit(scope["value"])
                if parsed.query or parsed.fragment:
                    refuse("host_scope_query_or_fragment")
            else:
                path = PurePosixPath(scope["value"])
                if path.is_absolute() or any(part in {"", ".", ".."} for part in scope["value"].split("/")) or "\\" in scope["value"]:
                    refuse("host_scope_path_invalid")
    return tools


def scope_allows(tool, locator, kind="uri_prefix"):
    for scope in tool["scope"]:
        if scope["kind"] != kind:
            continue
        base = scope["value"].rstrip("/")
        if kind == "uri_prefix":
            expected, candidate = urlsplit(base), urlsplit(locator)
            if candidate.username is not None or candidate.password is not None:
                continue
            try:
                left_origin = (expected.scheme, expected.hostname, expected.port or (443 if expected.scheme == "https" else 80))
                right_origin = (candidate.scheme, candidate.hostname, candidate.port or (443 if candidate.scheme == "https" else 80))
            except ValueError:
                continue
            if left_origin != right_origin:
                continue
            left, right = unquote(expected.path).rstrip("/"), unquote(candidate.path).rstrip("/")
            if (any(part in {".", ".."} for path in (left, right) for part in path.split("/"))
                    or "\\" in left + right or re.search(r"(?i)%2e|%2f|%5c|%25", left + right)):
                continue
        else:
            left, right = base, locator
        if right == left or right.startswith(left + "/"):
            return True
    return False


def describe(tool, demonstrations=()):
    observed = [item for item in demonstrations if item["tool_id"] == tool["id"] and item["tool_version"] == tool["version"]
                and scope_allows(tool, item["origin_url"]) and item["content_format"] in tool["formats"]
                and {"capture", "export"}.intersection(tool["operations"]) and item["content_bytes"] <= tool["limits"]["max_bytes"]]
    return {**tool, "credential_presence": [{"reference": name, "present": bool(os.environ.get(name))} for name in tool["credential_refs"]],
        "demonstration": {"state": "capture_artifact_checked" if observed else "not_demonstrated",
                          "capture_ids": [item["capture_id"] for item in observed],
                          "captures": [{key: item[key] for key in ("capture_id", "origin_url", "content_format", "content_kind", "completeness", "content_bytes")} for item in observed],
                          "scope": "checked deposited bytes and declared origin only; no live account, permission or tool-identity proof"},
        "access": "not_probed", "review_authority": "not_verified", "host_protection": "not_verified", "tool_executed": False}
