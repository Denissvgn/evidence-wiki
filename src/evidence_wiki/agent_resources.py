"""Bounded, content-only access to the installation's closed resource catalog.

No extraction, workspace imports, external paths, or network resolution occurs.
The catalog is exported from owning contracts by tools/sync_agent_resources.py.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import zipfile
from importlib import resources
from pathlib import Path, PurePosixPath

from ._agent_catalog import CATALOG_PATH, resource_paths
from .errors import UsageError

MAX_BYTES = 1_048_576
MAX_RESOURCES = 64


def _refuse(reason: str, *, unknown: bool = False) -> None:
    raise UsageError(
        "ONBOARDING_RESOURCE_UNKNOWN" if unknown else "ONBOARDING_ENVIRONMENT_INCOMPATIBLE",
        "Installed resource unavailable.", recoverable=False,
        remediation="Use an exact catalog ID; reinstall a complete compatible package if an asset is unavailable.",
        details={"field": reason},
    )


def _asset_tree():
    tree = resources.files("evidence_wiki").joinpath("assets")
    if tree.is_dir():
        return tree
    # Editable/source installs only. A partial packaged tree never falls back.
    checkout = Path(__file__).resolve().parents[2]
    if (checkout / "pyproject.toml").is_file() and (checkout / "workspace-template").is_dir():
        return checkout
    _refuse("assets_missing")


def _read(tree, relative: str) -> bytes:
    path = PurePosixPath(relative)
    if (not relative or path.is_absolute() or path.as_posix() != relative
            or any(part in {".", ".."} for part in path.parts) or "\\" in relative or ":" in relative):
        _refuse("asset_path_invalid")
    item = tree
    try:
        if isinstance(tree, Path) and os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"):
            directory = os.open(tree, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                for part in path.parts[:-1]:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                    os.close(directory)
                    directory = child
                descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                with os.fdopen(descriptor, "rb") as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        _refuse("asset_not_regular")
                    data = stream.read(MAX_BYTES + 1)
            finally:
                os.close(directory)
            if len(data) > MAX_BYTES:
                _refuse("asset_too_large")
            return data
        if isinstance(tree, Path) and tree.is_symlink():
            _refuse("asset_symlink")
        for part in path.parts:
            item = item.joinpath(part)
            if isinstance(item, Path) and item.is_symlink():
                _refuse("asset_symlink")
        if isinstance(item, Path) and not stat.S_ISREG(item.stat().st_mode):
            _refuse("asset_not_regular")
        with item.open("rb") as stream:
            data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            _refuse("asset_too_large")
        return data
    except (OSError, ValueError, zipfile.BadZipFile):
        _refuse("asset_missing_or_unreadable")


def _catalog():
    try:
        tree = _asset_tree()
        value = json.loads(_read(tree, CATALOG_PATH))
        entries = value["resources"]
        if (value["schema_version"] != "evidence-agent-catalog/v1" or not isinstance(entries, dict)
                or not 1 <= len(entries) <= MAX_RESOURCES or set(entries) != set(resource_paths())):
            _refuse("catalog_invalid")
        for key, relative in resource_paths().items():
            entry = entries[key]
            if (not isinstance(entry, dict)
                    or set(entry) != {"id", "path", "version", "media_type", "sha256"}
                    or not all(isinstance(part, str) for part in entry.values())
                    or entry["id"] != key or entry["path"] != relative
                    or entry["version"] != key.rsplit("/v", 1)[1] + ".0"
                    or entry["media_type"] not in {"text/markdown", "text/x-python", "application/json", "application/schema+json"}
                    or re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) is None):
                _refuse("catalog_invalid")
        metadata = value["installation"]
        if (set(metadata) != {"package_version", "starter_version", "library_api_version", "profile_schema_version", "research_contract_version"}
                or any(not isinstance(part, str) or not 1 <= len(part) <= 64 for part in metadata.values())):
            _refuse("catalog_invalid")
        runtime = value["runtime_files"]
        if (not isinstance(runtime, list) or not 1 <= len(runtime) <= 256
                or any(not isinstance(name, str) or re.fullmatch(r"[A-Za-z0-9_]+\.py", name) is None for name in runtime)
                or len(set(runtime)) != len(runtime)):
            _refuse("catalog_invalid")
        return tree, value
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        _refuse("catalog_invalid")


def resource_index() -> dict:
    """Return fresh metadata, with no filesystem locators or resource bodies."""
    _, catalog = _catalog()
    entries = [{key: entry[key] for key in ("id", "version", "media_type", "sha256")}
               for entry in catalog["resources"].values()]
    return {"schema_version": "evidence-agent-resources/v1", "resources": entries,
            "bounds": {"total": len(entries), "returned": len(entries), "truncated": False}}


def resource_document(resource_id: str) -> dict:
    """Read only the requested content; its lifetime belongs to the caller."""
    tree, catalog = _catalog()
    if not isinstance(resource_id, str) or resource_id not in catalog["resources"]:
        _refuse("resource_id", unknown=True)
    entry = catalog["resources"][resource_id]
    try:
        data = _read(tree, entry["path"])
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            _refuse("resource_digest_mismatch")
        result = {key: entry[key] for key in ("id", "version", "media_type", "sha256")}
        result["content"] = data.decode("utf-8")
        if len(result["content"]) > 65_536:
            _refuse("resource_content_bound")
        return result
    except (UnicodeError, KeyError, TypeError):
        _refuse("resource_invalid")


def installation_metadata() -> dict:
    """Return exported installation versions without importing workspace tools."""
    _, catalog = _catalog()
    return catalog["installation"]


def resource_availability() -> dict[str, bool]:
    """Observe inventory presence without loading content or executing helpers."""
    tree, catalog = _catalog()
    result = {}
    for resource_id, entry in catalog["resources"].items():
        try:
            item = tree
            for part in PurePosixPath(entry["path"]).parts:
                item = item.joinpath(part)
                if isinstance(item, Path) and item.is_symlink():
                    break
            else:
                result[resource_id] = item.is_file()
                continue
        except OSError:
            pass
        result[resource_id] = False
    return result


def runtime_available() -> bool:
    """Require the complete exported helper inventory, including dynamic siblings.

    Presence is intentionally weaker than runtime qualification. This avoids
    importing plugins or extracting a ZIP tree merely to negotiate capabilities.
    """
    tree, catalog = _catalog()
    try:
        directory = tree.joinpath("workspace-template").joinpath("scripts")
        if isinstance(directory, Path) and directory.is_symlink():
            return False
        return all((not isinstance(item, Path) or not item.is_symlink()) and item.is_file()
                   for item in (directory.joinpath(name) for name in catalog["runtime_files"]))
    except OSError:
        return False
