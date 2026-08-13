#!/usr/bin/env python3
"""Track, inspect, adopt, and safely refresh workspace domain packs.

The lifecycle state is deliberately workspace-local and private.  It records the
pack values and files that EvidenceWiki may update; everything else remains
operator-owned.  Refresh uses that provenance as the base of a three-way merge
and never guesses through a conflict.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import math
import os
import shutil
import stat
import sys
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

try:
    from ruamel.yaml import YAML
except ImportError as exc:  # pragma: no cover - package dependency guard
    raise SystemExit("ruamel.yaml is required for domain-pack lifecycle operations") from exc

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from _workspace_locks import workspace_lock

STATE_SCHEMA_VERSION = "1.0"
REFRESH_SCHEMA_VERSION = "1.0"
TRANSACTION_SCHEMA_VERSION = "1.0"
STATE_RELATIVE = PurePosixPath("domain-packs/.evidence-wiki-state.yml")
TRANSACTION_RELATIVE = PurePosixPath("domain-packs/.evidence-wiki-transaction.yml")
LOCK_RELATIVE = PurePosixPath(".locks/domain-pack-refresh.lock")
LOG_LOCK_RELATIVE = PurePosixPath(".locks/log.lock")
BACKUP_ROOT_RELATIVE = PurePosixPath(".replaced/domain-packs")
PACK_PATH_FIELDS = ("taxonomy_doc", "claims_doc")
PACK_PATH_MAPPING_FIELDS = ("scaffolds", "coverage_templates")
PACK_PATH_LIST_FIELDS = ("implemented_files", "planned_files")
PACK_IDENTITY_POINTERS = {
    "/domain_pack/name",
    "/domain_pack/version",
    "/domain_pack/compatible_research_yml_contract",
}
RESTRICTIVE_FILE_MODE = 0o600
RESTRICTIVE_DIR_MODE = 0o700

_MISSING = object()


class LifecycleFailure(RuntimeError):
    """A stable, renderable refusal from a pack lifecycle operation."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        exit_code: int = 2,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.exit_code = exit_code
        self.details = details or {}


@dataclass(frozen=True)
class Fallback:
    known: bool
    present: bool = False
    value: Any = None


@dataclass
class LifecyclePlan:
    report: dict[str, Any]
    state: dict[str, Any] | None = None
    config_text: str | None = None
    desired_files: dict[str, bytes | None] = field(default_factory=dict)
    config_dirty: bool = False
    state_dirty: bool = False
    log_entry: str | None = None
    input_fingerprint: str = ""
    candidate_fingerprint: str = ""


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return copy.deepcopy(value)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        _plain(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _json_model_issue(value: Any, path: str = "$") -> str | None:
    """Return the first value that cannot be represented in lifecycle state."""

    def inspect(current: Any, current_path: str, ancestors: frozenset[int]) -> str | None:
        if current is None or isinstance(current, (str, bool, int)):
            return None
        if isinstance(current, float):
            return None if math.isfinite(current) else f"{current_path}: non-finite float"
        if isinstance(current, (list, Mapping)):
            identity = id(current)
            if identity in ancestors:
                return f"{current_path}: recursive YAML alias"
            descendants = ancestors | {identity}
            if isinstance(current, list):
                for index, item in enumerate(current):
                    issue = inspect(item, f"{current_path}[{index}]", descendants)
                    if issue is not None:
                        return issue
                return None
            for key, item in current.items():
                if not isinstance(key, str):
                    return f"{current_path}: non-string mapping key"
                issue = inspect(item, f"{current_path}.{key}", descendants)
                if issue is not None:
                    return issue
            return None
        return f"{current_path}: unsupported {type(current).__name__} value"

    return inspect(value, path, frozenset())


def overlay_sha256(overlay: Mapping[str, Any]) -> str:
    """Return the semantic overlay identity shared with the package contract."""
    return hashlib.sha256(_canonical_bytes(overlay)).hexdigest()


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _safe_pack_files(pack_root: Path) -> list[Path]:
    if pack_root.is_symlink() or not pack_root.is_dir():
        raise LifecycleFailure("DOMAIN_PACK_INVALID", f"Domain pack is not a safe directory: {pack_root}")
    files: list[Path] = []
    for path in sorted(pack_root.rglob("*"), key=lambda item: item.relative_to(pack_root).as_posix()):
        relative = path.relative_to(pack_root).as_posix()
        if "\\" in relative:
            raise LifecycleFailure(
                "DOMAIN_PACK_INVALID",
                f"Domain pack contains a non-portable path: {relative}",
            )
        if path.is_symlink():
            raise LifecycleFailure(
                "DOMAIN_PACK_INVALID",
                f"Domain pack contains a symbolic link: {relative}",
            )
        if path.is_dir():
            continue
        if not path.is_file():
            raise LifecycleFailure(
                "DOMAIN_PACK_INVALID",
                f"Domain pack contains a special filesystem entry: {relative}",
            )
        files.append(path)
    return files


def file_inventory(pack_root: Path, *, include_bytes: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for path in _safe_pack_files(pack_root):
        relative = path.relative_to(pack_root).as_posix()
        content = path.read_bytes()
        result[relative] = content if include_bytes else sha256_bytes(content)
    return result


def tree_sha256(pack_root: Path) -> str:
    return _inventory_sha256(file_inventory(pack_root))


def _inventory_sha256(inventory: Mapping[str, str]) -> str:
    digest = hashlib.sha256()
    for relative, file_hash in sorted(inventory.items()):
        digest.update(f"{relative}\0{file_hash}\n".encode())
    return digest.hexdigest()


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _valid_transaction_id(value: Any) -> bool:
    """Accept only one bounded, portable transaction-directory component."""
    return bool(
        isinstance(value, str)
        and value
        and len(value) <= 200
        and value not in {".", ".."}
        and PurePosixPath(value).name == value
        and all(
            character.isascii()
            and (character.isalnum() or character in {"-", "_", "."})
            for character in value
        )
    )


def load_mapping(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise LifecycleFailure("DOMAIN_PACK_STATE_INVALID", f"{label} is missing or is not a regular file")
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise LifecycleFailure("DOMAIN_PACK_STATE_INVALID", f"Could not read {label}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise LifecycleFailure("DOMAIN_PACK_STATE_INVALID", f"Invalid YAML in {label}: {exc}") from exc
    if not isinstance(document, dict):
        raise LifecycleFailure("DOMAIN_PACK_STATE_INVALID", f"{label} must contain a mapping")
    return document


def _load_overlay_content(content: bytes, label: str) -> dict[str, Any]:
    try:
        document = yaml.safe_load(content.decode("utf-8")) or {}
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise LifecycleFailure("DOMAIN_PACK_INVALID", f"Invalid domain-pack overlay at {label}: {exc}") from exc
    if not isinstance(document, dict):
        raise LifecycleFailure("DOMAIN_PACK_INVALID", "research.overlay.yml must contain a mapping")
    pack = document.get("domain_pack")
    if not isinstance(pack, dict):
        raise LifecycleFailure("DOMAIN_PACK_INVALID", "research.overlay.yml must declare domain_pack")
    for key in ("name", "version", "compatible_research_yml_contract"):
        if not isinstance(pack.get(key), str) or not pack[key].strip():
            raise LifecycleFailure("DOMAIN_PACK_INVALID", f"domain_pack.{key} must be a non-empty string")
    return document


def load_overlay(pack_root: Path) -> dict[str, Any]:
    path = pack_root / "research.overlay.yml"
    if path.is_symlink() or not path.is_file():
        raise LifecycleFailure(
            "DOMAIN_PACK_INVALID",
            f"Domain-pack overlay is missing or is not a regular file: {path}",
        )
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise LifecycleFailure("DOMAIN_PACK_INVALID", f"Invalid domain-pack overlay at {path}: {exc}") from exc
    return _load_overlay_content(content, str(path))


def prefix_pack_path(value: Any, target_relative: str) -> Any:
    if not isinstance(value, str) or not value.strip():
        return value
    if "://" in value or value.startswith("/") or value.startswith(f"{target_relative}/"):
        return value
    return f"{target_relative}/{value}"


def normalize_overlay_paths(overlay: Mapping[str, Any], target_relative: str) -> dict[str, Any]:
    result = copy.deepcopy(_plain(overlay))
    domain_pack = result.get("domain_pack")
    if not isinstance(domain_pack, dict):
        return result
    for field_name in PACK_PATH_FIELDS:
        if field_name in domain_pack:
            domain_pack[field_name] = prefix_pack_path(domain_pack[field_name], target_relative)
    for field_name in PACK_PATH_MAPPING_FIELDS:
        values = domain_pack.get(field_name)
        if isinstance(values, dict):
            for key, value in list(values.items()):
                values[key] = prefix_pack_path(value, target_relative)
    for field_name in PACK_PATH_LIST_FIELDS:
        values = domain_pack.get(field_name)
        if isinstance(values, list):
            domain_pack[field_name] = [prefix_pack_path(value, target_relative) for value in values]
    return result


def _pointer(parts: tuple[str, ...]) -> str:
    if not parts:
        return ""
    return "/" + "/".join(part.replace("~", "~0").replace("/", "~1") for part in parts)


def _pointer_parts(pointer: str) -> tuple[str, ...]:
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ValueError(f"invalid RFC 6901 pointer: {pointer!r}")
    if pointer == "/":
        return ("",)
    result: list[str] = []
    for raw in pointer[1:].split("/"):
        index = 0
        text = ""
        while index < len(raw):
            if raw[index] != "~":
                text += raw[index]
                index += 1
                continue
            if index + 1 >= len(raw) or raw[index + 1] not in {"0", "1"}:
                raise ValueError(f"invalid RFC 6901 escape in {pointer!r}")
            text += "~" if raw[index + 1] == "0" else "/"
            index += 2
        result.append(text)
    return tuple(result)


def _lookup(root: Any, parts: tuple[str, ...]) -> tuple[bool, Any]:
    current = root
    for part in parts:
        if not isinstance(current, Mapping) or part not in current:
            return False, None
        current = current[part]
    return True, current


def _ensure_parent(root: Any, parts: tuple[str, ...]) -> Mapping[str, Any]:
    current = root
    for part in parts:
        if not isinstance(current, Mapping):
            raise LifecycleFailure(
                "DOMAIN_PACK_REFRESH_CONFLICT",
                f"Configuration shape changed before {_pointer(parts)}",
                exit_code=3,
            )
        if part not in current:
            current[part] = {}
        child = current[part]
        if not isinstance(child, Mapping):
            raise LifecycleFailure(
                "DOMAIN_PACK_REFRESH_CONFLICT",
                f"Configuration shape changed before {_pointer(parts)}",
                exit_code=3,
            )
        current = child
    return current


def _set_value(root: Any, parts: tuple[str, ...], value: Any) -> None:
    if not parts:
        raise LifecycleFailure("DOMAIN_PACK_STATE_INVALID", "Domain-pack state cannot own the document root")
    parent = _ensure_parent(root, parts[:-1])
    parent[parts[-1]] = copy.deepcopy(value)


def _delete_value(root: Any, parts: tuple[str, ...]) -> None:
    if not parts:
        raise LifecycleFailure("DOMAIN_PACK_STATE_INVALID", "Domain-pack state cannot delete the document root")
    present, parent = _lookup(root, parts[:-1])
    if present and isinstance(parent, Mapping):
        parent.pop(parts[-1], None)


def _same(a_present: bool, a_value: Any, b_present: bool, b_value: Any) -> bool:
    if a_present != b_present:
        return False
    if not a_present:
        return True
    # Python considers ``True == 1 == 1.0``. Those are distinct YAML values,
    # while ruamel's quoted strings and mappings should compare like their
    # ordinary YAML counterparts. Live operator YAML may also contain dates or
    # other safe-loader values that are deliberately outside the pack's JSON
    # model; those compare unequal instead of crashing lifecycle inspection.
    def equal(left: Any, right: Any, active: set[tuple[int, int]]) -> bool:
        pair = (id(left), id(right))
        if pair in active:
            return False
        if isinstance(left, Mapping) or isinstance(right, Mapping):
            if not isinstance(left, Mapping) or not isinstance(right, Mapping):
                return False
            if any(not isinstance(key, str) for key in left) or any(
                not isinstance(key, str) for key in right
            ):
                return False
            if set(left) != set(right):
                return False
            active.add(pair)
            try:
                return all(equal(left[key], right[key], active) for key in left)
            finally:
                active.remove(pair)
        if isinstance(left, list) or isinstance(right, list):
            if not isinstance(left, list) or not isinstance(right, list):
                return False
            if len(left) != len(right):
                return False
            active.add(pair)
            try:
                return all(equal(a, b, active) for a, b in zip(left, right, strict=True))
            finally:
                active.remove(pair)
        if isinstance(left, bool) or isinstance(right, bool):
            return isinstance(left, bool) and isinstance(right, bool) and left == right
        if isinstance(left, int) or isinstance(right, int):
            return (
                isinstance(left, int)
                and not isinstance(left, bool)
                and isinstance(right, int)
                and not isinstance(right, bool)
                and left == right
            )
        if isinstance(left, float) or isinstance(right, float):
            return isinstance(left, float) and isinstance(right, float) and left == right
        if isinstance(left, str) or isinstance(right, str):
            return isinstance(left, str) and isinstance(right, str) and left == right
        if left is None or right is None:
            return left is None and right is None
        return type(left) is type(right) and left == right

    try:
        return equal(a_value, b_value, set())
    except (RecursionError, TypeError, ValueError):
        return False


def _paths_overlap(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    common = min(len(left), len(right))
    return left[:common] == right[:common]


def overlay_write_pointers(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> list[str]:
    """Return ownership units written by an overlay under the mapping/shape rule."""
    result: list[str] = []

    def visit(old: Any, incoming: Mapping[str, Any], prefix: tuple[str, ...]) -> None:
        old_mapping = old if isinstance(old, Mapping) else {}
        for key, value in incoming.items():
            parts = (*prefix, str(key))
            old_value = old_mapping.get(key, _MISSING)
            if isinstance(value, Mapping) and isinstance(old_value, Mapping):
                visit(old_value, value, parts)
            else:
                result.append(_pointer(parts))

    visit(base, overlay, ())
    return sorted(result)


def _initial_ownership(
    base: Mapping[str, Any],
    overlay: Mapping[str, Any],
    final_config: Mapping[str, Any],
    unowned_pointers: set[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    entries: list[dict[str, Any]] = []
    overrides: list[str] = []
    unowned_parts = [_pointer_parts(pointer) for pointer in unowned_pointers]
    for pointer in overlay_write_pointers(base, overlay):
        parts = _pointer_parts(pointer)
        if parts and parts[0] == "project":
            continue
        if any(_paths_overlap(parts, other) for other in unowned_parts):
            overrides.append(f"config:{pointer}")
            continue
        applied_present, applied = _lookup(overlay, parts)
        current_present, current = _lookup(final_config, parts)
        if not applied_present or not _same(current_present, current, True, applied):
            overrides.append(f"config:{pointer}")
            continue
        fallback_present, fallback = _lookup(base, parts)
        entry: dict[str, Any] = {
            "path": pointer,
            "last_applied": _plain(applied),
            "fallback_known": True,
            "fallback_present": fallback_present,
        }
        if fallback_present:
            entry["fallback"] = _plain(fallback)
        entries.append(entry)
    return sorted(entries, key=lambda item: item["path"]), sorted(set(overrides))


def create_initial_state(
    *,
    base_config: Mapping[str, Any],
    normalized_overlay: Mapping[str, Any],
    final_config: Mapping[str, Any],
    installed_pack_root: Path,
    source_pack_root: Path | None = None,
    target_relative: str,
    source_kind: str,
    unowned_pointers: set[str] | None = None,
) -> dict[str, Any]:
    source_root = source_pack_root or installed_pack_root
    raw_overlay = load_overlay(source_root)
    domain_pack = raw_overlay["domain_pack"]
    ownership, overrides = _initial_ownership(
        base_config,
        normalized_overlay,
        final_config,
        unowned_pointers or set(),
    )
    inventory = file_inventory(source_root)
    installed_inventory = file_inventory(installed_pack_root)
    mismatched = [
        path for path, digest in inventory.items() if installed_inventory.get(path) != digest
    ]
    if mismatched:
        raise LifecycleFailure(
            "DOMAIN_PACK_WRITE_FAILED",
            "Installed domain-pack bytes differ from the validated source revision",
            details={"paths": mismatched},
        )
    state = {
        "schema_version": STATE_SCHEMA_VERSION,
        "pack": {
            "name": domain_pack["name"].strip(),
            "installed_version": domain_pack["version"].strip(),
            "compatible_research_yml_contract": domain_pack[
                "compatible_research_yml_contract"
            ].strip(),
            "target_relative": target_relative,
            "source_kind": source_kind,
            "overlay_sha256": overlay_sha256(raw_overlay),
            "normalized_overlay_sha256": overlay_sha256(normalized_overlay),
            "tree_sha256": _inventory_sha256(inventory),
        },
        "normalized_overlay": _plain(normalized_overlay),
        "pre_pack_config": _plain(base_config),
        "config_ownership": ownership,
        "managed_files": [
            {"path": path, "sha256": digest} for path, digest in sorted(inventory.items())
        ],
        "revision_files": [
            {"path": path, "sha256": digest} for path, digest in sorted(inventory.items())
        ],
        "local_overrides": overrides,
        "transaction_id": None,
    }
    return _validate_state(state)


def _state_path(project_root: Path) -> Path:
    return project_root.joinpath(*STATE_RELATIVE.parts)


def _transaction_path(project_root: Path) -> Path:
    return project_root.joinpath(*TRANSACTION_RELATIVE.parts)


def _lock_path(project_root: Path) -> Path:
    return project_root.joinpath(*LOCK_RELATIVE.parts)


def _log_lock_path(project_root: Path) -> Path:
    return project_root.joinpath(*LOG_LOCK_RELATIVE.parts)


def _workspace_relative_path_is_safe(project_root: Path, relative: PurePosixPath) -> bool:
    root = project_root.resolve()
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return False
    try:
        return current.resolve().is_relative_to(root)
    except OSError:
        return False


def _installed_pack_root(
    project_root: Path,
    target_relative: str,
    *,
    error_code: str,
) -> Path:
    relative = PurePosixPath(target_relative)
    if not _workspace_relative_path_is_safe(project_root, relative):
        raise LifecycleFailure(
            error_code,
            "Installed domain-pack path has a symbolic-link or containment boundary",
            details={"path": target_relative},
        )
    installed = project_root.joinpath(*relative.parts)
    if not installed.is_dir() or installed.is_symlink():
        raise LifecycleFailure(
            error_code,
            "Installed domain-pack directory is missing or unsafe",
            details={"path": target_relative},
        )
    return installed


def _mkdir_private(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(RESTRICTIVE_DIR_MODE)
    except OSError:
        pass


def _atomic_write(path: Path, content: bytes, *, mode: int = RESTRICTIVE_FILE_MODE) -> None:
    _mkdir_private(path.parent)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _yaml_bytes(value: Mapping[str, Any]) -> bytes:
    return yaml.safe_dump(_plain(value), sort_keys=False, allow_unicode=True).encode("utf-8")


def write_initial_state(project_root: Path, state: Mapping[str, Any]) -> None:
    _atomic_write(_state_path(project_root), _yaml_bytes(_validate_state(dict(state))))


def _validate_state(document: dict[str, Any]) -> dict[str, Any]:
    if document.get("schema_version") != STATE_SCHEMA_VERSION:
        raise LifecycleFailure(
            "DOMAIN_PACK_STATE_INVALID",
            f"Unsupported domain-pack state schema: {document.get('schema_version')!r}",
        )
    pack = document.get("pack")
    if not isinstance(pack, dict):
        raise LifecycleFailure("DOMAIN_PACK_STATE_INVALID", "Domain-pack state is missing pack metadata")
    for key in (
        "name",
        "installed_version",
        "compatible_research_yml_contract",
        "target_relative",
        "source_kind",
        "overlay_sha256",
        "normalized_overlay_sha256",
        "tree_sha256",
    ):
        if (
            not isinstance(pack.get(key), str)
            or not pack[key]
            or pack[key] != pack[key].strip()
        ):
            raise LifecycleFailure("DOMAIN_PACK_STATE_INVALID", f"Domain-pack state has invalid pack.{key}")
    try:
        target_path = PurePosixPath(pack["target_relative"])
    except TypeError as exc:
        raise LifecycleFailure("DOMAIN_PACK_STATE_INVALID", "Invalid pack target path") from exc
    if (
        target_path.is_absolute()
        or ".." in target_path.parts
        or len(target_path.parts) != 2
        or target_path.as_posix() != pack["target_relative"]
        or "\\" in pack["target_relative"]
    ):
        raise LifecycleFailure("DOMAIN_PACK_STATE_INVALID", "Pack target path must stay under domain-packs/")
    if target_path.parts[0] != "domain-packs":
        raise LifecycleFailure("DOMAIN_PACK_STATE_INVALID", "Pack target path must stay under domain-packs/")
    if target_path.name != pack["name"]:
        raise LifecycleFailure("DOMAIN_PACK_STATE_INVALID", "Pack target path and name disagree")
    if (
        pack["name"] in {".", ".."}
        or "/" in pack["name"]
        or "\\" in pack["name"]
        or any(ord(character) < 32 for character in pack["name"])
    ):
        raise LifecycleFailure("DOMAIN_PACK_STATE_INVALID", "Pack name is not portable")
    if pack["source_kind"] not in {"bundled", "path", "adopted"}:
        raise LifecycleFailure("DOMAIN_PACK_STATE_INVALID", "Domain-pack state has invalid source_kind")
    for key in ("overlay_sha256", "normalized_overlay_sha256", "tree_sha256"):
        if not _is_sha256(pack[key]):
            raise LifecycleFailure("DOMAIN_PACK_STATE_INVALID", f"Domain-pack state has invalid pack.{key}")
    overlay = document.get("normalized_overlay")
    if not isinstance(overlay, dict):
        raise LifecycleFailure("DOMAIN_PACK_STATE_INVALID", "Domain-pack state is missing normalized_overlay")
    issue = _json_model_issue(document)
    if issue is not None:
        raise LifecycleFailure(
            "DOMAIN_PACK_STATE_INVALID",
            f"Domain-pack state contains a non-JSON lifecycle value: {issue}",
        )
    overlay_identity = overlay.get("domain_pack")
    if not isinstance(overlay_identity, dict) or (
        overlay_identity.get("name"),
        overlay_identity.get("version"),
        overlay_identity.get("compatible_research_yml_contract"),
    ) != (
        pack["name"],
        pack["installed_version"],
        pack["compatible_research_yml_contract"],
    ):
        raise LifecycleFailure("DOMAIN_PACK_STATE_INVALID", "Normalized overlay and pack identity disagree")
    if overlay_sha256(overlay) != pack["normalized_overlay_sha256"]:
        raise LifecycleFailure("DOMAIN_PACK_STATE_INVALID", "Normalized overlay digest does not match state")
    pre_pack = document.get("pre_pack_config")
    if pre_pack is not None and not isinstance(pre_pack, dict):
        raise LifecycleFailure("DOMAIN_PACK_STATE_INVALID", "Invalid pre-pack configuration provenance")
    ownership = document.get("config_ownership")
    managed = document.get("managed_files")
    revision = document.get("revision_files")
    overrides = document.get("local_overrides", [])
    if (
        not isinstance(ownership, list)
        or not isinstance(managed, list)
        or not isinstance(revision, list)
        or not isinstance(overrides, list)
    ):
        raise LifecycleFailure("DOMAIN_PACK_STATE_INVALID", "Domain-pack ownership lists are invalid")
    seen: list[tuple[str, ...]] = []
    for entry in ownership:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise LifecycleFailure("DOMAIN_PACK_STATE_INVALID", "Invalid configuration ownership entry")
        try:
            parts = _pointer_parts(entry["path"])
        except ValueError as exc:
            raise LifecycleFailure("DOMAIN_PACK_STATE_INVALID", str(exc)) from exc
        if not parts or "last_applied" not in entry or not isinstance(entry.get("fallback_known"), bool):
            raise LifecycleFailure("DOMAIN_PACK_STATE_INVALID", "Incomplete configuration ownership entry")
        if parts[0] == "project":
            raise LifecycleFailure("DOMAIN_PACK_STATE_INVALID", "Project identity cannot be pack-owned")
        fallback_known = entry["fallback_known"]
        if fallback_known:
            if not isinstance(entry.get("fallback_present"), bool):
                raise LifecycleFailure("DOMAIN_PACK_STATE_INVALID", "Invalid configuration fallback metadata")
            if entry["fallback_present"] != ("fallback" in entry):
                raise LifecycleFailure("DOMAIN_PACK_STATE_INVALID", "Inconsistent configuration fallback metadata")
        elif "fallback_present" in entry or "fallback" in entry:
            raise LifecycleFailure("DOMAIN_PACK_STATE_INVALID", "Unknown fallbacks cannot contain guessed values")
        overlay_present, overlay_value = _lookup(overlay, parts)
        if not _same(overlay_present, overlay_value, True, entry["last_applied"]):
            raise LifecycleFailure("DOMAIN_PACK_STATE_INVALID", "Owned configuration differs from normalized overlay")
        if any(_paths_overlap(parts, previous) for previous in seen):
            raise LifecycleFailure("DOMAIN_PACK_STATE_INVALID", "Configuration ownership paths overlap")
        seen.append(parts)

    def validate_file_entries(entries: list[Any], label: str) -> dict[str, str]:
        values: dict[str, str] = {}
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                raise LifecycleFailure("DOMAIN_PACK_STATE_INVALID", f"Invalid {label} file entry")
            relative = PurePosixPath(entry["path"])
            digest = entry.get("sha256")
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or relative.as_posix() in {"", "."}
                or relative.as_posix() != entry["path"]
                or "\\" in entry["path"]
            ):
                raise LifecycleFailure("DOMAIN_PACK_STATE_INVALID", f"Invalid {label} file path")
            if entry["path"] in values or not _is_sha256(digest):
                raise LifecycleFailure("DOMAIN_PACK_STATE_INVALID", f"Invalid {label} file hash entry")
            values[entry["path"]] = digest
        return values

    managed_files = validate_file_entries(managed, "managed")
    revision_files = validate_file_entries(revision, "revision")
    if _inventory_sha256(revision_files) != pack["tree_sha256"]:
        raise LifecycleFailure("DOMAIN_PACK_STATE_INVALID", "Revision inventory does not match pack tree digest")
    if any(revision_files.get(path) != digest for path, digest in managed_files.items()):
        raise LifecycleFailure("DOMAIN_PACK_STATE_INVALID", "Managed files are not a subset of the pack revision")

    if len(set(overrides)) != len(overrides):
        raise LifecycleFailure("DOMAIN_PACK_STATE_INVALID", "Duplicate local override target")
    owned_paths = [_pointer_parts(entry["path"]) for entry in ownership]
    config_override_paths: list[tuple[str, ...]] = []
    file_override_paths: set[str] = set()
    for value in overrides:
        if not isinstance(value, str):
            raise LifecycleFailure("DOMAIN_PACK_STATE_INVALID", "Invalid local override target")
        if value.startswith("config:"):
            try:
                pointer = value.removeprefix("config:")
                parts = _pointer_parts(pointer)
            except ValueError as exc:
                raise LifecycleFailure("DOMAIN_PACK_STATE_INVALID", str(exc)) from exc
            present, _value = _lookup(overlay, parts)
            if not parts or parts[0] == "project" or not present:
                raise LifecycleFailure("DOMAIN_PACK_STATE_INVALID", "Configuration override is not in the revision")
            if any(_paths_overlap(parts, owned) for owned in owned_paths):
                raise LifecycleFailure("DOMAIN_PACK_STATE_INVALID", "Configuration override overlaps owned state")
            config_override_paths.append(parts)
            continue
        if value.startswith("file:"):
            relative = value.removeprefix("file:")
            if relative not in revision_files or relative in managed_files:
                raise LifecycleFailure("DOMAIN_PACK_STATE_INVALID", "File override is not released revision content")
            file_override_paths.add(relative)
            continue
        raise LifecycleFailure("DOMAIN_PACK_STATE_INVALID", "Invalid local override target")
    if set(revision_files) - set(managed_files) != file_override_paths:
        raise LifecycleFailure(
            "DOMAIN_PACK_STATE_INVALID",
            "Every unowned revision file must have exactly one file override",
        )

    def overlay_leaf_units(value: Mapping[str, Any], prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
        units: list[tuple[str, ...]] = []
        for key, item in value.items():
            parts = (*prefix, str(key))
            if isinstance(item, Mapping) and item:
                units.extend(overlay_leaf_units(item, parts))
            else:
                units.append(parts)
        return units

    base_for_units = pre_pack if isinstance(pre_pack, Mapping) else None
    required_units = (
        [_pointer_parts(pointer) for pointer in overlay_write_pointers(base_for_units, overlay)]
        if base_for_units is not None
        else overlay_leaf_units(overlay)
    )
    for parts in required_units:
        if parts and parts[0] == "project":
            continue
        if not any(_paths_overlap(parts, owned) for owned in owned_paths) and not any(
            _paths_overlap(parts, override) for override in config_override_paths
        ):
            raise LifecycleFailure(
                "DOMAIN_PACK_STATE_INVALID",
                f"Overlay configuration unit {_pointer(parts)} has no ownership provenance",
            )
    if document.get("transaction_id") is not None:
        raise LifecycleFailure(
            "DOMAIN_PACK_STATE_INVALID",
            "Completed domain-pack state must not retain a transaction identity",
        )
    return document


def load_state(project_root: Path) -> dict[str, Any]:
    path = _state_path(project_root)
    if not _workspace_relative_path_is_safe(project_root, STATE_RELATIVE):
        raise LifecycleFailure(
            "DOMAIN_PACK_STATE_INVALID",
            "Domain-pack lifecycle state crosses a symbolic-link boundary",
        )
    if not path.exists() and not path.is_symlink():
        raise LifecycleFailure("DOMAIN_PACK_UNTRACKED", "Workspace domain pack has no lifecycle state")
    if not path.is_file() or path.is_symlink():
        raise LifecycleFailure("DOMAIN_PACK_STATE_INVALID", "Domain-pack lifecycle state is not a regular file")
    return _validate_state(load_mapping(path, "domain-pack lifecycle state"))


def _research_round_trip(path: Path) -> tuple[Any, str]:
    if path.is_symlink() or not path.is_file():
        raise LifecycleFailure("DOMAIN_PACK_STATE_INVALID", "research.yml is missing or is not a regular file")
    round_trip = YAML(typ="rt", pure=True)
    round_trip.preserve_quotes = True
    try:
        original = path.read_text(encoding="utf-8")
        document = round_trip.load(original)
    except (OSError, Exception) as exc:  # ruamel exposes several scanner subclasses
        if isinstance(exc, LifecycleFailure):
            raise
        raise LifecycleFailure("DOMAIN_PACK_STATE_INVALID", f"Could not parse research.yml: {exc}") from exc
    if not isinstance(document, Mapping):
        raise LifecycleFailure("DOMAIN_PACK_STATE_INVALID", "research.yml must contain a mapping")
    return document, original


def _render_round_trip(document: Any) -> str:
    round_trip = YAML(typ="rt", pure=True)
    round_trip.preserve_quotes = True
    stream = io.StringIO()
    round_trip.dump(document, stream)
    return stream.getvalue()


def _empty_inspection(state: str) -> dict[str, Any]:
    return {
        "state": state,
        "name": None,
        "installed_version": None,
        "overlay_sha256": None,
        "tree_sha256": None,
        "source_comparison_performed": False,
        "local_override_count": 0,
        "conflict_count": 0,
        "transaction_id": None,
    }


def _config_pack(config: Mapping[str, Any]) -> dict[str, Any] | None:
    value = config.get("domain_pack")
    return value if isinstance(value, dict) else None


def inspect_workspace(project_root: Path) -> dict[str, Any]:
    """Return lifecycle health without comparing or contacting an upstream source."""
    root = Path(project_root).expanduser().resolve()
    transaction_path = _transaction_path(root)
    if transaction_path.exists() or transaction_path.is_symlink():
        result = _empty_inspection("transaction_incomplete")
        try:
            journal = load_mapping(transaction_path, "domain-pack transaction journal")
            transaction_id = journal.get("transaction_id")
            result["transaction_id"] = transaction_id if _valid_transaction_id(transaction_id) else None
        except LifecycleFailure:
            pass
        return result
    research_path = root / "research.yml"
    try:
        config = load_mapping(research_path, "research.yml")
    except LifecycleFailure:
        return _empty_inspection("state_invalid")
    try:
        workspace_contract = _workspace_contract(root)
    except LifecycleFailure:
        # Workspace contract metadata is one of the three local identities the
        # inspector reconciles.  Missing, unreadable, or malformed metadata is
        # not a pack-tree disagreement: there is no trustworthy value to
        # compare, so the lifecycle state itself cannot be established.
        return _empty_inspection("state_invalid")
    configured_pack = _config_pack(config)
    state_path = _state_path(root)
    if configured_pack is None:
        malformed_declaration = "domain_pack" in config
        return _empty_inspection(
            "state_invalid"
            if malformed_declaration or state_path.exists() or state_path.is_symlink()
            else "none"
        )
    configured_contract = configured_pack.get("compatible_research_yml_contract")
    configured_name = configured_pack.get("name")
    configured_version = configured_pack.get("version")
    if (
        not isinstance(configured_name, str)
        or not configured_name.strip()
        or configured_name in {".", ".."}
        or "/" in configured_name
        or "\\" in configured_name
        or any(ord(character) < 32 for character in configured_name)
        or not isinstance(configured_version, str)
        or not configured_version.strip()
        or not isinstance(configured_contract, str)
        or not configured_contract.strip()
    ):
        return _empty_inspection("state_invalid")
    if not state_path.exists() and not state_path.is_symlink():
        candidate_relative = PurePosixPath("domain-packs") / configured_name
        if not _workspace_relative_path_is_safe(root, candidate_relative):
            return _empty_inspection("state_invalid")
        result = _empty_inspection(
            "legacy_untracked"
            if configured_contract == workspace_contract
            else "config_tree_skew"
        )
        result["name"] = configured_pack.get("name")
        result["installed_version"] = configured_pack.get("version")
        if result["state"] == "config_tree_skew":
            result["conflict_count"] = 1
        return result
    try:
        state = load_state(root)
    except LifecycleFailure:
        return _empty_inspection("state_invalid")
    pack = state["pack"]
    result = {
        "state": "current",
        "name": pack["name"],
        "installed_version": pack["installed_version"],
        "overlay_sha256": pack["overlay_sha256"],
        "tree_sha256": pack["tree_sha256"],
        "source_comparison_performed": False,
        "local_override_count": len(state.get("local_overrides", [])),
        "conflict_count": 0,
        "transaction_id": state.get("transaction_id"),
    }
    target_relative = pack["target_relative"]
    target_path = PurePosixPath(target_relative)
    if not _workspace_relative_path_is_safe(root, target_path):
        result["state"] = "state_invalid"
        result["conflict_count"] = 1
        return result
    try:
        installed_root = _installed_pack_root(
            root,
            target_relative,
            error_code="DOMAIN_PACK_STATE_INVALID",
        )
    except LifecycleFailure:
        result["state"] = "pack_missing"
        result["conflict_count"] = len(state.get("managed_files", []))
        return result
    try:
        _safe_pack_files(installed_root)
    except LifecycleFailure:
        result["state"] = "state_invalid"
        result["conflict_count"] = 1
        return result
    configured_identity = (
        configured_pack.get("name"),
        configured_pack.get("version"),
        configured_pack.get("compatible_research_yml_contract"),
    )
    tracked_identity = (
        pack["name"],
        pack["installed_version"],
        pack["compatible_research_yml_contract"],
    )
    config_override_parts = [
        _pointer_parts(target.removeprefix("config:"))
        for target in state.get("local_overrides", [])
        if target.startswith("config:")
    ]
    identity_parts = (
        ("domain_pack", "name"),
        ("domain_pack", "version"),
        ("domain_pack", "compatible_research_yml_contract"),
    )
    unacknowledged_identity_skew = any(
        configured != tracked
        and not any(_paths_overlap(parts, override) for override in config_override_parts)
        for configured, tracked, parts in zip(
            configured_identity,
            tracked_identity,
            identity_parts,
            strict=True,
        )
    )
    if (
        unacknowledged_identity_skew
        or pack["compatible_research_yml_contract"] != workspace_contract
    ):
        result["state"] = "config_tree_skew"
        result["conflict_count"] = 1
        return result
    file_overrides = [
        target for target in state.get("local_overrides", []) if target.startswith("file:")
    ]
    if not file_overrides:
        managed_tree_digest = hashlib.sha256()
        for entry in sorted(state["managed_files"], key=lambda item: item["path"]):
            managed_tree_digest.update(f"{entry['path']}\0{entry['sha256']}\n".encode())
        if managed_tree_digest.hexdigest() != pack["tree_sha256"]:
            result["state"] = "state_invalid"
            result["conflict_count"] = 1
            return result
    overlay_entry = next(
        (entry for entry in state["managed_files"] if entry["path"] == "research.overlay.yml"),
        None,
    )
    if overlay_entry is not None:
        try:
            installed_overlay_path = _safe_pack_member(installed_root, "research.overlay.yml")
        except LifecycleFailure:
            result["state"] = "state_invalid"
            result["conflict_count"] = 1
            return result
        if installed_overlay_path.is_file():
            try:
                installed_overlay = load_overlay(installed_root)
            except LifecycleFailure:
                result["state"] = "config_tree_skew"
                result["conflict_count"] = 1
                return result
            if overlay_sha256(installed_overlay) != pack["overlay_sha256"]:
                result["state"] = "config_tree_skew"
                result["conflict_count"] = 1
                return result
    missing_files = 0
    modifications = 0
    for entry in state["managed_files"]:
        try:
            path = _safe_pack_member(installed_root, entry["path"])
        except LifecycleFailure:
            result["state"] = "state_invalid"
            result["conflict_count"] = 1
            return result
        if not path.is_file():
            missing_files += 1
            continue
        try:
            if sha256_bytes(path.read_bytes()) != entry["sha256"]:
                modifications += 1
        except OSError:
            missing_files += 1
    for entry in state["config_ownership"]:
        present, value = _lookup(config, _pointer_parts(entry["path"]))
        if not _same(present, value, True, entry["last_applied"]):
            modifications += 1
    if missing_files:
        result["state"] = "pack_missing"
        result["conflict_count"] = missing_files + modifications
    elif modifications:
        result["state"] = "local_modifications"
        result["conflict_count"] = modifications
    elif state.get("local_overrides"):
        # Explicit releases are healthy provenance, but they are still local
        # divergence operators and doctor/fleet need to surface for review.
        result["state"] = "local_modifications"
    return result


def _deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(_plain(base))
    for key, value in overlay.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(_plain(value))
    return result


def _fallback_from_entry(entry: Mapping[str, Any]) -> Fallback:
    known = bool(entry.get("fallback_known", False))
    present = bool(entry.get("fallback_present", False)) if known else False
    return Fallback(known=known, present=present, value=copy.deepcopy(entry.get("fallback")))


def _fallback_child(fallback: Fallback, key: str) -> Fallback:
    if not fallback.known:
        return Fallback(False)
    if not fallback.present:
        return Fallback(True, False)
    if not isinstance(fallback.value, Mapping):
        return Fallback(False)
    if key not in fallback.value:
        return Fallback(True, False)
    return Fallback(True, True, copy.deepcopy(fallback.value[key]))


def _ownership_entry(parts: tuple[str, ...], value: Any, fallback: Fallback) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "path": _pointer(parts),
        "last_applied": _plain(value),
        "fallback_known": fallback.known,
    }
    if fallback.known:
        entry["fallback_present"] = fallback.present
        if fallback.present:
            entry["fallback"] = _plain(fallback.value)
    return entry


def _target_for_config(parts: tuple[str, ...]) -> str:
    return f"config:{_pointer(parts)}"


def _target_for_file(relative: str) -> str:
    return f"file:{relative}"


class _ConfigPlanner:
    def __init__(
        self,
        document: Any,
        *,
        keep_local: set[str],
        accept_pack: set[str],
        local_overrides: set[str],
    ) -> None:
        self.document = document
        self.keep_local = keep_local
        self.accept_pack = accept_pack
        self.local_overrides = local_overrides
        self.ownership: list[dict[str, Any]] = []
        self.changes: list[dict[str, str]] = []
        self.conflicts: list[dict[str, str]] = []
        self.conflict_targets: set[str] = set()
        self.config_dirty = False

    def _change(self, target: str, action: str) -> None:
        item = {"path": target, "action": action}
        if item not in self.changes:
            self.changes.append(item)

    def _discard_overlapping_overrides(self, parts: tuple[str, ...]) -> None:
        for target in list(self.local_overrides):
            if not target.startswith("config:"):
                continue
            try:
                other = _pointer_parts(target.removeprefix("config:"))
            except ValueError:
                continue
            if _paths_overlap(parts, other):
                self.local_overrides.discard(target)

    def overlapping_override_targets(self, parts: tuple[str, ...]) -> set[str]:
        """Return released config targets at, above, or below ``parts``."""
        result: set[str] = set()
        for target in self.local_overrides:
            if not target.startswith("config:"):
                continue
            try:
                other = _pointer_parts(target.removeprefix("config:"))
            except ValueError:
                continue
            if _paths_overlap(parts, other):
                result.add(target)
        return result

    def _retain(self, parts: tuple[str, ...], value: Any, fallback: Fallback) -> None:
        self._discard_overlapping_overrides(parts)
        self.ownership.append(_ownership_entry(parts, value, fallback))

    def _release(self, parts: tuple[str, ...]) -> None:
        target = _target_for_config(parts)
        self._discard_overlapping_overrides(parts)
        self.local_overrides.add(target)
        self._change(target, "release")

    def _identity_conflict(
        self,
        parts: tuple[str, ...],
        *,
        incoming: Any,
        fallback: Fallback,
    ) -> None:
        target = _target_for_config(parts)
        self.conflict_targets.add(target)
        if target in self.keep_local:
            self._release(parts)
            return
        if target in self.accept_pack:
            self._write_candidate(
                parts,
                incoming_present=True,
                incoming=incoming,
                fallback=fallback,
                action="accept_pack",
            )
            return
        self.conflicts.append(
            {
                "target": target,
                "kind": "config",
                "reason": "workspace pack identity differs from the candidate-managed identity",
            }
        )

    def mapping_shape_conflict(
        self,
        parts: tuple[str, ...],
        *,
        incoming: Mapping[str, Any],
        fallback: Fallback,
    ) -> None:
        """Resolve a locally removed/non-mapping parent as one explicit conflict."""
        target = _target_for_config(parts)
        self.conflict_targets.add(target)
        if target in self.keep_local:
            self._release(parts)
            return
        if target not in self.accept_pack:
            self.conflicts.append(
                {
                    "target": target,
                    "kind": "config",
                    "reason": "workspace configuration changed the shape of a pack-managed mapping",
                }
            )
            return

        if fallback.known and fallback.present and isinstance(fallback.value, Mapping):
            desired = _deep_merge(fallback.value, incoming)
            units = overlay_write_pointers(fallback.value, incoming)
            ownership: list[tuple[tuple[str, ...], Any, Fallback]] = []
            for pointer in units:
                relative_parts = _pointer_parts(pointer)
                _present, applied = _lookup(incoming, relative_parts)
                fallback_present, fallback_value = _lookup(fallback.value, relative_parts)
                ownership.append(
                    (
                        (*parts, *relative_parts),
                        applied,
                        Fallback(True, fallback_present, copy.deepcopy(fallback_value)),
                    )
                )
        else:
            desired = copy.deepcopy(_plain(incoming))
            ownership = [(parts, incoming, fallback)]

        current_present, current = _lookup(self.document, parts)
        if not _same(current_present, current, True, desired):
            _set_value(self.document, parts, desired)
            self.config_dirty = True
        self._discard_overlapping_overrides(parts)
        for owned_parts, applied, owned_fallback in ownership:
            self.ownership.append(_ownership_entry(owned_parts, applied, owned_fallback))
        self._change(target, "accept_pack")

    def released_shape_transition(
        self,
        parts: tuple[str, ...],
        *,
        incoming: Any,
        fallback: Fallback,
    ) -> None:
        """Require an ancestor-level choice before replacing released descendants."""
        target = _target_for_config(parts)
        self.conflict_targets.add(target)
        if target in self.keep_local:
            # Consolidate any released descendants into the whole transition
            # unit so future revisions cannot claim newly introduced children.
            self._release(parts)
            return
        if target in self.accept_pack:
            self._write_candidate(
                parts,
                incoming_present=True,
                incoming=incoming,
                fallback=fallback,
                action="accept_pack",
            )
            return
        self.conflicts.append(
            {
                "target": target,
                "kind": "config",
                "reason": (
                    "pack mapping changes shape across released local configuration; "
                    "resolve the whole transition with --keep-local or --accept-pack"
                ),
            }
        )

    def _write_candidate(
        self,
        parts: tuple[str, ...],
        *,
        incoming_present: bool,
        incoming: Any,
        fallback: Fallback,
        action: str | None = None,
    ) -> None:
        target = _target_for_config(parts)
        current_present, current = _lookup(self.document, parts)
        if incoming_present:
            if not _same(current_present, current, True, incoming):
                _set_value(self.document, parts, incoming)
                self.config_dirty = True
            self._retain(parts, incoming, fallback)
            self._change(target, action or ("update" if current_present else "add"))
            return
        if current_present:
            _delete_value(self.document, parts)
            self.config_dirty = True
        self.local_overrides.discard(target)
        self._change(target, action or "delete")

    def _restore_fallback(self, parts: tuple[str, ...], fallback: Fallback) -> None:
        target = _target_for_config(parts)
        current_present, current = _lookup(self.document, parts)
        if fallback.present:
            if not _same(current_present, current, True, fallback.value):
                _set_value(self.document, parts, fallback.value)
                self.config_dirty = True
            self._change(target, "restore")
        else:
            if current_present:
                _delete_value(self.document, parts)
                self.config_dirty = True
            self._change(target, "delete")
        self.local_overrides.discard(target)

    def _conflict(
        self,
        parts: tuple[str, ...],
        reason: str,
        *,
        incoming_present: bool,
        incoming: Any,
        fallback: Fallback,
        retired: bool = False,
    ) -> None:
        target = _target_for_config(parts)
        self.conflict_targets.add(target)
        if target in self.keep_local:
            self._release(parts)
            return
        if target in self.accept_pack:
            if retired:
                # An unknown fallback cannot be reconstructed. Explicitly accepting
                # the pack is the reviewed choice to remove the retired declaration.
                if fallback.known:
                    self._restore_fallback(parts, fallback)
                else:
                    self._write_candidate(
                        parts,
                        incoming_present=False,
                        incoming=None,
                        fallback=fallback,
                    )
                return
            self._write_candidate(
                parts,
                incoming_present=incoming_present,
                incoming=incoming,
                fallback=fallback,
                action="accept_pack",
            )
            return
        self.conflicts.append({"target": target, "kind": "config", "reason": reason})

    def owned(
        self,
        parts: tuple[str, ...],
        *,
        base: Any,
        incoming_present: bool,
        incoming: Any,
        fallback: Fallback,
    ) -> None:
        current_present, current = _lookup(self.document, parts)
        if (
            _pointer(parts) in PACK_IDENTITY_POINTERS
            and incoming_present
            and not _same(current_present, current, True, incoming)
            and not (
                not _same(True, base, True, incoming)
                and _same(current_present, current, True, base)
            )
        ):
            self._identity_conflict(parts, incoming=incoming, fallback=fallback)
            return
        if (
            incoming_present
            and isinstance(base, Mapping)
            and isinstance(incoming, Mapping)
            and not incoming
            and current_present
            and isinstance(current, Mapping)
            and _same(True, current, True, base)
            and fallback.known
            and not fallback.present
        ):
            # An explicitly delivered empty mapping still owns the mapping node
            # when the pack originally introduced that node. Keeping this whole
            # unit lets a later retirement restore the known-absent fallback
            # instead of stranding an unowned ``parent: {}`` in research.yml.
            self._write_candidate(
                parts,
                incoming_present=True,
                incoming=incoming,
                fallback=fallback,
            )
            return
        if (
            incoming_present
            and isinstance(base, Mapping)
            and isinstance(incoming, Mapping)
            and current_present
            and isinstance(current, Mapping)
            and (not fallback.present or isinstance(fallback.value, Mapping))
        ):
            for key in sorted(set(base) | set(incoming)):
                if key not in base:
                    self.new(
                        (*parts, str(key)),
                        incoming=incoming[key],
                        fallback=_fallback_child(fallback, str(key)),
                    )
                else:
                    self.owned(
                        (*parts, str(key)),
                        base=base[key],
                        incoming_present=key in incoming,
                        incoming=incoming.get(key),
                        fallback=_fallback_child(fallback, str(key)),
                    )
            return

        candidate_unchanged = incoming_present and _same(True, base, True, incoming)
        if candidate_unchanged:
            if _same(current_present, current, True, base):
                self._retain(parts, base, fallback)
            else:
                self._release(parts)
            return

        if not incoming_present:
            if not fallback.known:
                self._conflict(
                    parts,
                    "retired declaration has no known pre-pack fallback",
                    incoming_present=False,
                    incoming=None,
                    fallback=fallback,
                    retired=True,
                )
                return
            if _same(current_present, current, fallback.present, fallback.value) or _same(
                current_present,
                current,
                True,
                base,
            ):
                # Retirement's effective incoming value is the known pre-pack
                # fallback, not simply absence.  A workspace already at that
                # fallback converges; an unchanged pack value restores it.
                self._restore_fallback(parts, fallback)
                return
            self._conflict(
                parts,
                "workspace and candidate both changed since the last applied pack value",
                incoming_present=False,
                incoming=None,
                fallback=fallback,
                retired=True,
            )
            return
        if incoming_present and _same(current_present, current, True, incoming):
            self._retain(parts, incoming, fallback)
            return
        if _same(current_present, current, True, base):
            self._write_candidate(
                parts,
                incoming_present=True,
                incoming=incoming,
                fallback=fallback,
            )
            return

        self._conflict(
            parts,
            "workspace and candidate both changed since the last applied pack value",
            incoming_present=incoming_present,
            incoming=incoming,
            fallback=fallback,
            retired=False,
        )

    def new(self, parts: tuple[str, ...], *, incoming: Any, fallback: Fallback | None = None) -> None:
        target = _target_for_config(parts)
        current_present, current = _lookup(self.document, parts)
        fallback = fallback or Fallback(True, current_present, copy.deepcopy(current))
        if not current_present:
            self._write_candidate(
                parts,
                incoming_present=True,
                incoming=incoming,
                fallback=Fallback(True, False),
            )
            return
        self.conflict_targets.add(target)
        if target in self.keep_local:
            self.local_overrides.add(target)
            self._change(target, "keep_local")
            return
        if target in self.accept_pack:
            self._write_candidate(
                parts,
                incoming_present=True,
                incoming=incoming,
                fallback=fallback,
                action="accept_pack",
            )
            return
        self.conflicts.append(
            {
                "target": target,
                "kind": "config",
                "reason": "new pack declaration collides with existing local configuration",
            }
        )


def _new_overlay_units(
    old: Mapping[str, Any], incoming: Mapping[str, Any], prefix: tuple[str, ...] = ()
) -> list[tuple[tuple[str, ...], Any]]:
    result: list[tuple[tuple[str, ...], Any]] = []
    for key in sorted(incoming):
        parts = (*prefix, str(key))
        if key not in old:
            result.append((parts, incoming[key]))
        elif isinstance(old[key], Mapping) and isinstance(incoming[key], Mapping):
            result.extend(_new_overlay_units(old[key], incoming[key], parts))
    return result


def _mapping_to_whole_transitions(
    old: Mapping[str, Any], incoming: Mapping[str, Any], prefix: tuple[str, ...] = ()
) -> list[tuple[tuple[str, ...], Mapping[str, Any], Any]]:
    """Return minimal mapping-to-nonmapping transitions as whole ownership units."""
    result: list[tuple[tuple[str, ...], Mapping[str, Any], Any]] = []
    for key in sorted(set(old) & set(incoming)):
        parts = (*prefix, str(key))
        old_value = old[key]
        incoming_value = incoming[key]
        if isinstance(old_value, Mapping) and not isinstance(incoming_value, Mapping):
            result.append((parts, old_value, incoming_value))
        elif isinstance(old_value, Mapping) and isinstance(incoming_value, Mapping):
            result.extend(_mapping_to_whole_transitions(old_value, incoming_value, parts))
    return result


def _fallback_at(state: Mapping[str, Any], parts: tuple[str, ...]) -> Fallback:
    base = state.get("pre_pack_config")
    if not isinstance(base, Mapping):
        return Fallback(False)
    present, value = _lookup(base, parts)
    return Fallback(True, present, copy.deepcopy(value))


def _previous_whole_value(old_value: Mapping[str, Any], fallback: Fallback) -> tuple[bool, Any]:
    if fallback.present and isinstance(fallback.value, Mapping):
        return True, _deep_merge(fallback.value, old_value)
    return True, copy.deepcopy(_plain(old_value))


def _workspace_mapping_shape_conflicts(
    document: Mapping[str, Any],
    old: Mapping[str, Any],
    incoming: Mapping[str, Any],
    prefix: tuple[str, ...] = (),
) -> list[tuple[tuple[str, ...], Mapping[str, Any]]]:
    """Find minimal pack mappings whose live parent was removed or made non-mapping."""
    result: list[tuple[tuple[str, ...], Mapping[str, Any]]] = []
    for key in sorted(set(old) & set(incoming)):
        old_value = old[key]
        incoming_value = incoming[key]
        if not isinstance(old_value, Mapping) or not isinstance(incoming_value, Mapping):
            continue
        parts = (*prefix, str(key))
        current_present, current = _lookup(document, parts)
        if not current_present or not isinstance(current, Mapping):
            result.append((parts, incoming_value))
            continue
        result.extend(
            _workspace_mapping_shape_conflicts(document, old_value, incoming_value, parts)
        )
    return result


def _adoption_units(
    current: Any, incoming: Mapping[str, Any], prefix: tuple[str, ...] = ()
) -> list[tuple[tuple[str, ...], Any]]:
    """Split adoption ownership wherever both live and pack values are mappings."""
    result: list[tuple[tuple[str, ...], Any]] = []
    current_mapping = current if isinstance(current, Mapping) else {}
    for key in sorted(incoming):
        parts = (*prefix, str(key))
        current_value = current_mapping.get(key, _MISSING)
        incoming_value = incoming[key]
        if (
            isinstance(current_value, Mapping)
            and isinstance(incoming_value, Mapping)
            and incoming_value
        ):
            result.extend(_adoption_units(current_value, incoming_value, parts))
        else:
            result.append((parts, incoming_value))
    return result


def _pack_metadata(overlay: Mapping[str, Any]) -> dict[str, str]:
    pack = overlay.get("domain_pack")
    if not isinstance(pack, Mapping):
        raise LifecycleFailure("DOMAIN_PACK_INVALID", "Candidate overlay has no domain_pack mapping")
    result: dict[str, str] = {}
    for key in ("name", "version", "compatible_research_yml_contract"):
        value = pack.get(key)
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
        ):
            raise LifecycleFailure("DOMAIN_PACK_INVALID", f"Candidate domain_pack.{key} is invalid")
        result[key] = value
    return result


def _workspace_contract(project_root: Path) -> str:
    metadata = load_mapping(project_root / "workspace-system.yml", "workspace-system.yml")
    workspace_system = metadata.get("workspace_system")
    if not isinstance(workspace_system, dict):
        raise LifecycleFailure("DOMAIN_PACK_STATE_INVALID", "workspace-system.yml has no workspace_system mapping")
    value = workspace_system.get("compatible_research_yml_contract")
    if not isinstance(value, str) or not value.strip():
        raise LifecycleFailure("DOMAIN_PACK_STATE_INVALID", "Workspace research contract is missing")
    return value.strip()


def _workspace_fingerprint(
    project_root: Path,
    state: Mapping[str, Any] | None = None,
    pack_root: Path | None = None,
) -> str:
    digest = hashlib.sha256()
    for relative in (
        "research.yml",
        "workspace-system.yml",
        "log.md",
        STATE_RELATIVE.as_posix(),
        TRANSACTION_RELATIVE.as_posix(),
    ):
        path = project_root.joinpath(*PurePosixPath(relative).parts)
        digest.update(relative.encode() + b"\0")
        if path.is_file() and not path.is_symlink():
            digest.update(sha256_bytes(path.read_bytes()).encode())
        else:
            digest.update(b"missing")
        digest.update(b"\n")
    if state is not None:
        pack_root = project_root.joinpath(*PurePosixPath(state["pack"]["target_relative"]).parts)
    if pack_root is not None:
        if pack_root.is_dir() and not pack_root.is_symlink():
            for relative, file_hash in file_inventory(pack_root).items():
                digest.update(f"pack:{relative}\0{file_hash}\n".encode())
        else:
            digest.update(b"pack:missing\n")
    return digest.hexdigest()


def _candidate_kind(candidate_root: Path, source_kind: str | None) -> str:
    if source_kind in {"bundled", "path"}:
        return source_kind
    return "path"


def _validate_resolution_flags(keep_local: list[str], accept_pack: list[str]) -> tuple[set[str], set[str]]:
    keep = set(keep_local)
    accept = set(accept_pack)
    duplicates = sorted(
        {value for value in keep_local if keep_local.count(value) > 1}
        | {value for value in accept_pack if accept_pack.count(value) > 1}
    )
    contradictory = sorted(keep & accept)
    invalid = sorted(
        value
        for value in keep | accept
        if not value.startswith("config:/") and not value.startswith("file:")
    )
    if duplicates or contradictory or invalid:
        details: dict[str, Any] = {}
        if duplicates:
            details["duplicates"] = duplicates
        if contradictory:
            details["contradictory"] = contradictory
        if invalid:
            details["invalid"] = invalid
        raise LifecycleFailure(
            "DOMAIN_PACK_REFRESH_CONFLICT",
            "Conflict resolutions must be unique, path-specific, and non-contradictory",
            exit_code=3,
            details=details,
        )
    return keep, accept


def _state_file_map(state: Mapping[str, Any]) -> dict[str, str]:
    return {entry["path"]: entry["sha256"] for entry in state["managed_files"]}


def _state_revision_file_map(state: Mapping[str, Any]) -> dict[str, str]:
    return {entry["path"]: entry["sha256"] for entry in state["revision_files"]}


def _safe_pack_member(pack_root: Path, relative: str) -> Path:
    relative_path = PurePosixPath(relative)
    if (
        relative_path.is_absolute()
        or ".." in relative_path.parts
        or relative_path.as_posix() in {"", "."}
        or relative_path.as_posix() != relative
    ):
        raise LifecycleFailure(
            "DOMAIN_PACK_STATE_INVALID",
            f"Installed domain-pack path is unsafe: {relative}",
            details={"path": relative},
        )
    current = pack_root
    for index, part in enumerate(relative_path.parts):
        current = current / part
        if current.is_symlink():
            raise LifecycleFailure(
                "DOMAIN_PACK_STATE_INVALID",
                f"Installed domain pack contains a symbolic link: {relative}",
                details={"path": relative},
            )
        if index < len(relative_path.parts) - 1 and current.exists() and not current.is_dir():
            raise LifecycleFailure(
                "DOMAIN_PACK_STATE_INVALID",
                f"Installed domain-pack path has a non-directory ancestor: {relative}",
                details={
                    "path": relative,
                    "ancestor_collision": True,
                    "ancestor": PurePosixPath(*relative_path.parts[: index + 1]).as_posix(),
                },
            )
    try:
        if not current.resolve().is_relative_to(pack_root.resolve()):
            raise LifecycleFailure(
                "DOMAIN_PACK_STATE_INVALID",
                f"Installed domain-pack path escapes its root: {relative}",
            )
    except OSError as exc:
        raise LifecycleFailure(
            "DOMAIN_PACK_STATE_INVALID",
            f"Could not resolve installed domain-pack path {relative}: {exc}",
        ) from exc
    return current


def _refresh_file_plan(
    *,
    installed_root: Path,
    candidate_root: Path,
    state: Mapping[str, Any],
    keep: set[str],
    accept: set[str],
    local_overrides: set[str],
    candidate_bytes: Mapping[str, bytes] | None = None,
) -> tuple[list[dict[str, str]], list[dict[str, str]], set[str], dict[str, bytes | None], list[dict[str, str]]]:
    changes: list[dict[str, str]] = []
    conflicts: list[dict[str, str]] = []
    conflict_targets: set[str] = set()
    desired: dict[str, bytes | None] = {}
    old_files = _state_file_map(state)
    old_revision = _state_revision_file_map(state)
    candidate_bytes = dict(candidate_bytes) if candidate_bytes is not None else file_inventory(
        candidate_root,
        include_bytes=True,
    )
    next_managed: dict[str, str] = {}
    directory_replacements: dict[str, dict[str, str]] = {}
    blocking_representatives: dict[str, str] = {}
    for relative in candidate_bytes:
        try:
            candidate_destination = _safe_pack_member(installed_root, relative)
        except LifecycleFailure as exc:
            if exc.details.get("ancestor_collision"):
                blocking_representatives.setdefault(
                    exc.details["ancestor"],
                    relative,
                )
                continue
            raise
        if not candidate_destination.is_dir() or candidate_destination.is_symlink():
            continue
        current_descendants: dict[str, str] = {}
        for path in _safe_pack_files(candidate_destination):
            installed_relative = path.relative_to(installed_root).as_posix()
            current_descendants[installed_relative] = sha256_bytes(path.read_bytes())
        directory_replacements[relative] = current_descendants

    def current(relative: str) -> tuple[bool, str | None]:
        try:
            path = _safe_pack_member(installed_root, relative)
        except LifecycleFailure as exc:
            if exc.details.get("ancestor_collision"):
                # A local file occupying an incoming directory is existing local
                # data, not an absent path the pack may silently replace.
                return True, f"non-directory-ancestor:{exc.details['ancestor']}"
            raise
        if path.exists() and not path.is_file():
            raise LifecycleFailure(
                "DOMAIN_PACK_STATE_INVALID",
                f"Installed domain-pack path is not a regular file: {relative}",
            )
        if not path.is_file():
            return False, None
        try:
            return True, sha256_bytes(path.read_bytes())
        except OSError as exc:
            raise LifecycleFailure(
                "DOMAIN_PACK_STATE_INVALID",
                f"Installed domain-pack file could not be read: {relative}: {exc}",
            ) from exc

    def add_change(target: str, action: str) -> None:
        item = {"path": target, "action": action}
        if item not in changes:
            changes.append(item)

    def add_conflict(target: str, reason: str) -> None:
        item = {"target": target, "kind": "file", "reason": reason}
        if not any(existing["target"] == target for existing in conflicts):
            conflicts.append(item)

    for relative, old_hash in sorted(old_files.items()):
        if any(relative.startswith(f"{parent}/") for parent in directory_replacements):
            # A directory-to-file transition is planned as one stable parent
            # target below, so child conflicts and resolutions cannot drift.
            continue
        target = _target_for_file(relative)
        current_present, current_hash = current(relative)
        incoming = candidate_bytes.get(relative, _MISSING)
        incoming_present = incoming is not _MISSING
        incoming_hash = sha256_bytes(incoming) if incoming_present else None
        if incoming_present and incoming_hash == old_hash:
            if current_present and current_hash == old_hash:
                next_managed[relative] = old_hash
            else:
                local_overrides.add(target)
                add_change(target, "release")
            continue
        if incoming_present and current_present and current_hash == incoming_hash:
            next_managed[relative] = incoming_hash
            local_overrides.discard(target)
            continue
        if current_present and current_hash == old_hash:
            if incoming_present:
                desired[relative] = incoming
                next_managed[relative] = incoming_hash
                local_overrides.discard(target)
                add_change(target, "update")
            else:
                desired[relative] = None
                local_overrides.discard(target)
                add_change(target, "delete")
            continue
        if not incoming_present and not current_present:
            local_overrides.discard(target)
            continue
        conflict_targets.add(target)
        if target in keep:
            local_overrides.add(target)
            add_change(target, "keep_local")
        elif target in accept:
            if incoming_present:
                desired[relative] = incoming
                next_managed[relative] = incoming_hash
                local_overrides.discard(target)
                add_change(target, "accept_pack")
            else:
                desired[relative] = None
                local_overrides.discard(target)
                add_change(target, "delete")
        else:
            add_conflict(
                target,
                "workspace and candidate both changed the tracked pack file",
            )

    for relative, content in sorted(candidate_bytes.items()):
        if relative in old_files:
            continue
        target = _target_for_file(relative)
        if relative in old_revision and target in local_overrides:
            # Explicitly released files remain operator-owned across pack
            # revisions, including when the live operator value is a directory.
            continue
        directory_contents = directory_replacements.get(relative)
        if directory_contents is not None:
            prefix = f"{relative}/"
            prior_descendants = {
                path: digest
                for path, digest in old_revision.items()
                if path.startswith(prefix)
            }
            untracked = sorted(set(directory_contents) - set(prior_descendants))
            locally_changed = any(
                (
                    path in old_files
                    and directory_contents.get(path) != old_files[path]
                )
                or (
                    path not in old_files
                    and path in directory_contents
                )
                for path in prior_descendants
            ) or any(
                path in old_files and path not in directory_contents
                for path in prior_descendants
            ) or any(
                _target_for_file(path) in local_overrides
                for path in prior_descendants
            )
            incoming_hash = sha256_bytes(content)
            if not untracked and not locally_changed:
                for descendant in sorted(directory_contents):
                    desired[descendant] = None
                    local_overrides.discard(_target_for_file(descendant))
                    add_change(_target_for_file(descendant), "delete")
                desired[relative] = content
                next_managed[relative] = incoming_hash
                local_overrides.discard(target)
                add_change(target, "add")
                continue
            conflict_targets.add(target)
            if target in keep:
                local_overrides.add(target)
                add_change(target, "keep_local")
                continue
            if target in accept:
                for descendant in sorted(directory_contents):
                    desired[descendant] = None
                    local_overrides.discard(_target_for_file(descendant))
                    add_change(_target_for_file(descendant), "delete")
                desired[relative] = content
                next_managed[relative] = incoming_hash
                local_overrides.discard(target)
                add_change(target, "accept_pack")
                continue
            reason = (
                "incoming pack file would displace untracked files from an existing directory"
                if untracked
                else "incoming pack file collides with locally changed prior pack content"
            )
            add_conflict(target, reason)
            continue
        current_present, current_hash = current(relative)
        incoming_hash = sha256_bytes(content)
        blocking_ancestor = (
            current_hash.removeprefix("non-directory-ancestor:")
            if isinstance(current_hash, str)
            and current_hash.startswith("non-directory-ancestor:")
            else None
        )
        blocking_target = (
            _target_for_file(blocking_ancestor)
            if blocking_ancestor is not None
            else None
        )
        if (
            blocking_ancestor is not None
            and desired.get(blocking_ancestor, _MISSING) is None
            and blocking_target not in conflict_targets
        ):
            # A prior tracked file at this path is already scheduled for
            # retirement, so the pack's file->directory shape transition is not
            # a collision with operator-owned data.
            current_present = False
        if not current_present:
            desired[relative] = content
            next_managed[relative] = incoming_hash
            local_overrides.discard(target)
            add_change(target, "add")
            continue
        resolution_target = (
            blocking_target
            if blocking_target in conflict_targets
            else _target_for_file(blocking_representatives[blocking_ancestor])
            if blocking_ancestor is not None
            else target
        )
        conflict_targets.add(resolution_target)
        if resolution_target in keep:
            # Persist releases using actual candidate revision paths, even
            # though one stable ancestor target resolves the whole collision.
            local_overrides.add(target)
            add_change(resolution_target, "keep_local")
        elif resolution_target in accept:
            if blocking_ancestor is not None:
                # Explicit acceptance displaces the local file that blocks this
                # incoming child. It is staged and backed up as part of the same
                # transaction, so rollback restores it and no data is discarded.
                desired[blocking_ancestor] = None
                add_change(_target_for_file(blocking_ancestor), "delete")
            desired[relative] = content
            next_managed[relative] = incoming_hash
            local_overrides.discard(target)
            add_change(target, "accept_pack")
        else:
            add_conflict(
                resolution_target,
                "new pack path collides with an existing local file",
            )

    for target in list(local_overrides):
        if not target.startswith("file:"):
            continue
        relative = target.removeprefix("file:")
        if relative in old_revision and relative not in candidate_bytes:
            local_overrides.discard(target)

    managed = [{"path": path, "sha256": digest} for path, digest in sorted(next_managed.items())]
    return changes, conflicts, conflict_targets, desired, managed


def _refresh_report(
    *,
    mode: str,
    target: Path,
    status: str,
    pack: dict[str, Any],
    changes: list[dict[str, str]],
    conflicts: list[dict[str, str]],
    warnings: list[str] | None = None,
    log_appended: bool = False,
) -> dict[str, Any]:
    return {
        "schema_version": REFRESH_SCHEMA_VERSION,
        "operation": "refresh",
        "mode": mode,
        "target": str(target),
        "status": status,
        "pack": pack,
        "changes": sorted(changes, key=lambda item: (item["path"], item["action"])),
        "conflicts": sorted(conflicts, key=lambda item: item["target"]),
        "warnings": sorted(set(warnings or [])),
        "log_appended": log_appended,
    }


def _adopt_report(
    *,
    mode: str,
    target: Path,
    status: str,
    pack: dict[str, Any],
    changes: list[dict[str, str]],
    conflicts: list[dict[str, str]],
    warnings: list[str] | None = None,
    log_appended: bool = False,
) -> dict[str, Any]:
    return {
        "schema_version": REFRESH_SCHEMA_VERSION,
        "operation": "adopt",
        "mode": mode,
        "target": str(target),
        "status": status,
        "pack": pack,
        "changes": sorted(changes, key=lambda item: (item["path"], item["action"])),
        "conflicts": sorted(conflicts, key=lambda item: item["target"]),
        "warnings": sorted(set(warnings or [])),
        "log_appended": log_appended,
    }


def _raise_conflicts(report: dict[str, Any]) -> None:
    raise LifecycleFailure(
        "DOMAIN_PACK_REFRESH_CONFLICT",
        "Domain-pack operation has unresolved conflicts; no workspace files were written",
        exit_code=3,
        details={"report": report, "conflict_targets": [item["target"] for item in report["conflicts"]]},
    )


def plan_refresh(
    project_root: Path,
    candidate_root: Path,
    *,
    keep_local: list[str] | None = None,
    accept_pack: list[str] | None = None,
    dry_run: bool = True,
    source_kind: str | None = None,
    warnings: list[str] | None = None,
) -> LifecyclePlan:
    root = Path(project_root).expanduser().resolve()
    if _transaction_path(root).exists() or _transaction_path(root).is_symlink():
        raise LifecycleFailure(
            "DOMAIN_PACK_TRANSACTION_INCOMPLETE",
            "An interrupted domain-pack transaction must be recovered before refresh planning",
            details={"transaction": TRANSACTION_RELATIVE.as_posix()},
        )
    candidate_input = Path(candidate_root).expanduser()
    if candidate_input.is_symlink():
        raise LifecycleFailure("DOMAIN_PACK_INVALID", "Candidate domain-pack root must not be a symbolic link")
    candidate = candidate_input.resolve()
    keep, accept = _validate_resolution_flags(keep_local or [], accept_pack or [])
    candidate_snapshot = file_inventory(candidate, include_bytes=True)
    candidate_inventory = {
        relative: sha256_bytes(content)
        for relative, content in candidate_snapshot.items()
    }
    initial_candidate_fingerprint = _inventory_sha256(candidate_inventory)
    overlay_content = candidate_snapshot.get("research.overlay.yml")
    if overlay_content is None:
        raise LifecycleFailure("DOMAIN_PACK_INVALID", "Candidate pack has no research.overlay.yml")
    initial_workspace_shallow = _workspace_fingerprint(root)
    state = load_state(root)
    if _workspace_fingerprint(root) != initial_workspace_shallow:
        raise LifecycleFailure(
            "DOMAIN_PACK_REFRESH_CONFLICT",
            "Workspace changed while domain-pack refresh was being planned",
            exit_code=3,
        )
    try:
        initial_workspace_fingerprint = _workspace_fingerprint(root, state)
    except LifecycleFailure as exc:
        raise LifecycleFailure(
            "DOMAIN_PACK_STATE_INVALID",
            f"Tracked installed pack tree is unsafe: {exc}",
            details=exc.details,
        ) from exc
    if _workspace_fingerprint(root) != initial_workspace_shallow:
        raise LifecycleFailure(
            "DOMAIN_PACK_REFRESH_CONFLICT",
            "Workspace changed while domain-pack refresh was being planned",
            exit_code=3,
        )
    old_pack = state["pack"]
    raw_candidate = _load_overlay_content(
        overlay_content,
        str(candidate / "research.overlay.yml"),
    )
    candidate_meta = _pack_metadata(raw_candidate)
    if candidate.name != candidate_meta["name"]:
        raise LifecycleFailure(
            "DOMAIN_PACK_INVALID",
            "Candidate pack directory name must match domain_pack.name",
            details={"directory": candidate.name, "declared_name": candidate_meta["name"]},
        )
    if candidate_meta["name"] != old_pack["name"]:
        raise LifecycleFailure(
            "DOMAIN_PACK_INVALID",
            f"Refresh cannot switch pack {old_pack['name']!r} to {candidate_meta['name']!r}",
        )
    workspace_contract = _workspace_contract(root)
    if candidate_meta["compatible_research_yml_contract"] != workspace_contract:
        raise LifecycleFailure(
            "DOMAIN_PACK_INVALID",
            "Candidate pack is incompatible with the workspace research contract",
            details={
                "workspace_contract": workspace_contract,
                "pack_contract": candidate_meta["compatible_research_yml_contract"],
            },
        )
    target_relative = old_pack["target_relative"]
    if PurePosixPath(target_relative).name != old_pack["name"]:
        raise LifecycleFailure("DOMAIN_PACK_STATE_INVALID", "Tracked pack path does not match its name")
    installed_root = _installed_pack_root(
        root,
        target_relative,
        error_code="DOMAIN_PACK_STATE_INVALID",
    )
    try:
        _safe_pack_files(installed_root)
    except LifecycleFailure as exc:
        raise LifecycleFailure(
            "DOMAIN_PACK_STATE_INVALID",
            f"Tracked installed pack tree is unsafe: {exc}",
            details=exc.details,
        ) from exc
    incoming_overlay = normalize_overlay_paths(raw_candidate, target_relative)
    old_overlay = state["normalized_overlay"]
    candidate_overlay_digest = overlay_sha256(raw_candidate)
    candidate_tree_digest = initial_candidate_fingerprint
    if (
        not keep
        and not accept
        and candidate_overlay_digest == old_pack["overlay_sha256"]
        and candidate_tree_digest == old_pack["tree_sha256"]
        and inspect_workspace(root)["state"] == "current"
    ):
        report = _refresh_report(
            mode="dry-run" if dry_run else "write",
            target=root,
            status="no_changes",
            pack={
                "name": old_pack["name"],
                "installed_version": old_pack["installed_version"],
                "candidate_version": candidate_meta["version"],
                "installed_overlay_sha256": old_pack["overlay_sha256"],
                "candidate_overlay_sha256": candidate_overlay_digest,
                "installed_tree_sha256": old_pack["tree_sha256"],
                "candidate_tree_sha256": candidate_tree_digest,
                "source_kind": _candidate_kind(candidate, source_kind),
            },
            changes=[],
            conflicts=[],
            warnings=warnings,
        )
        final_workspace_fingerprint = _workspace_fingerprint(root, state)
        if final_workspace_fingerprint != initial_workspace_fingerprint:
            raise LifecycleFailure(
                "DOMAIN_PACK_REFRESH_CONFLICT",
                "Workspace changed while domain-pack refresh was being planned",
                exit_code=3,
            )
        if tree_sha256(candidate) != candidate_tree_digest:
            raise LifecycleFailure(
                "DOMAIN_PACK_INVALID",
                "Candidate domain pack changed while refresh was being planned",
            )
        return LifecyclePlan(
            report=report,
            state=state,
            input_fingerprint=final_workspace_fingerprint,
            candidate_fingerprint=candidate_tree_digest,
        )
    document, original_config_text = _research_round_trip(root / "research.yml")
    configured_pack = _config_pack(document)
    if not isinstance(configured_pack, Mapping):
        raise LifecycleFailure(
            "DOMAIN_PACK_STATE_INVALID",
            "research.yml has no valid domain-pack identity mapping",
        )

    local_overrides = set(state.get("local_overrides", []))
    planner = _ConfigPlanner(
        document,
        keep_local=keep,
        accept_pack=accept,
        local_overrides=local_overrides,
    )
    owned_parts: list[tuple[str, ...]] = []
    transition_parts: list[tuple[str, ...]] = []
    for parts, old_value, incoming in _mapping_to_whole_transitions(
        old_overlay, incoming_overlay
    ):
        if parts and parts[0] == "project":
            continue
        transition_parts.append(parts)
        fallback = _fallback_at(state, parts)
        previous_known, previous = _previous_whole_value(old_value, fallback)
        if planner.overlapping_override_targets(parts):
            planner.released_shape_transition(
                parts,
                incoming=incoming,
                fallback=fallback,
            )
        elif previous_known:
            planner.owned(
                parts,
                base=previous,
                incoming_present=True,
                incoming=incoming,
                fallback=fallback,
            )
        else:
            planner.new(parts, incoming=incoming, fallback=fallback)
        owned_parts.append(parts)

    for parts, incoming_mapping in _workspace_mapping_shape_conflicts(
        document, old_overlay, incoming_overlay
    ):
        if parts and parts[0] == "project":
            continue
        if any(_paths_overlap(parts, transition) for transition in transition_parts):
            continue
        transition_parts.append(parts)
        planner.mapping_shape_conflict(
            parts,
            incoming=incoming_mapping,
            fallback=_fallback_at(state, parts),
        )
        owned_parts.append(parts)

    for entry in sorted(state["config_ownership"], key=lambda item: item["path"]):
        parts = _pointer_parts(entry["path"])
        if any(_paths_overlap(parts, transition) for transition in transition_parts):
            continue
        owned_parts.append(parts)
        incoming_present, incoming = _lookup(incoming_overlay, parts)
        planner.owned(
            parts,
            base=entry["last_applied"],
            incoming_present=incoming_present,
            incoming=incoming,
            fallback=_fallback_from_entry(entry),
        )
    for parts, incoming in _new_overlay_units(old_overlay, incoming_overlay):
        if parts and parts[0] == "project":
            continue
        if any(_paths_overlap(parts, owned) for owned in owned_parts):
            continue
        if planner.overlapping_override_targets(parts):
            # A declaration released by the operator stays unowned while it
            # remains present in consecutive pack revisions.  An ancestor
            # release also covers children introduced by a later revision,
            # and a released descendant prevents claiming a new whole parent.
            continue
        planner.new(parts, incoming=incoming)

    # Once a released declaration is genuinely retired, it is no longer part
    # of lifecycle state. A later reintroduction is therefore a new collision.
    for target in list(planner.local_overrides):
        if not target.startswith("config:"):
            continue
        try:
            parts = _pointer_parts(target.removeprefix("config:"))
        except ValueError:
            continue
        old_present, _old_value = _lookup(old_overlay, parts)
        incoming_present, _incoming_value = _lookup(incoming_overlay, parts)
        if old_present and not incoming_present:
            planner.local_overrides.discard(target)

    file_changes, file_conflicts, file_conflict_targets, desired_files, managed_files = _refresh_file_plan(
        installed_root=installed_root,
        candidate_root=candidate,
        state=state,
        keep=keep,
        accept=accept,
        local_overrides=planner.local_overrides,
        candidate_bytes=candidate_snapshot,
    )
    all_conflict_targets = planner.conflict_targets | file_conflict_targets
    unknown_resolutions = sorted((keep | accept) - all_conflict_targets)
    if unknown_resolutions:
        raise LifecycleFailure(
            "DOMAIN_PACK_REFRESH_CONFLICT",
            "One or more conflict resolutions do not name a conflict in this plan",
            exit_code=3,
            details={"unknown_resolutions": unknown_resolutions},
        )
    conflicts = [*planner.conflicts, *file_conflicts]
    changes = [*planner.changes, *file_changes]
    pack_report = {
        "name": old_pack["name"],
        "installed_version": old_pack["installed_version"],
        "candidate_version": candidate_meta["version"],
        "installed_overlay_sha256": old_pack["overlay_sha256"],
        "candidate_overlay_sha256": candidate_overlay_digest,
        "installed_tree_sha256": old_pack["tree_sha256"],
        "candidate_tree_sha256": candidate_tree_digest,
        "source_kind": _candidate_kind(candidate, source_kind),
    }
    if conflicts:
        report = _refresh_report(
            mode="dry-run" if dry_run else "write",
            target=root,
            status="conflict",
            pack=pack_report,
            changes=[],
            conflicts=conflicts,
            warnings=warnings,
        )
        _raise_conflicts(report)

    config_text = _render_round_trip(document)
    config_dirty = config_text != original_config_text
    next_state = copy.deepcopy(state)
    next_state["pack"] = {
        "name": candidate_meta["name"],
        "installed_version": candidate_meta["version"],
        "compatible_research_yml_contract": candidate_meta["compatible_research_yml_contract"],
        "target_relative": target_relative,
        "source_kind": _candidate_kind(candidate, source_kind),
        "overlay_sha256": pack_report["candidate_overlay_sha256"],
        "normalized_overlay_sha256": overlay_sha256(incoming_overlay),
        "tree_sha256": pack_report["candidate_tree_sha256"],
    }
    next_state["normalized_overlay"] = _plain(incoming_overlay)
    next_state["config_ownership"] = sorted(planner.ownership, key=lambda item: item["path"])
    next_state["managed_files"] = managed_files
    next_state["revision_files"] = [
        {"path": path, "sha256": digest}
        for path, digest in sorted(candidate_inventory.items())
    ]
    next_state["local_overrides"] = sorted(planner.local_overrides)
    next_state["transaction_id"] = None
    next_state = _validate_state(next_state)
    state_dirty = _canonical_bytes(next_state) != _canonical_bytes(state)
    material = config_dirty or bool(desired_files) or state_dirty
    status = "planned" if material and dry_run else "ready" if material else "no_changes"
    report = _refresh_report(
        mode="dry-run" if dry_run else "write",
        target=root,
        status=status,
        pack=pack_report,
        changes=changes if material else [],
        conflicts=[],
        warnings=warnings,
    )
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_entry = None
    if material:
        log_entry = (
            f"\n## [{timestamp}] domain-pack-refresh | Refreshed {old_pack['name']}\n\n"
            f"- Pack revision: `{old_pack['installed_version']}` -> `{candidate_meta['version']}`.\n"
            f"- Overlay SHA-256: `{pack_report['candidate_overlay_sha256']}`.\n"
            f"- Managed changes: {len(changes)}; local overrides: {len(planner.local_overrides)}.\n"
        )
    final_workspace_fingerprint = _workspace_fingerprint(root, state)
    if final_workspace_fingerprint != initial_workspace_fingerprint:
        raise LifecycleFailure(
            "DOMAIN_PACK_REFRESH_CONFLICT",
            "Workspace changed while domain-pack refresh was being planned",
            exit_code=3,
        )
    final_candidate_fingerprint = tree_sha256(candidate)
    if final_candidate_fingerprint != candidate_tree_digest:
        raise LifecycleFailure(
            "DOMAIN_PACK_INVALID",
            "Candidate domain pack changed while refresh was being planned",
        )
    return LifecyclePlan(
        report=report,
        state=next_state,
        config_text=config_text if config_dirty else None,
        desired_files=desired_files,
        config_dirty=config_dirty,
        state_dirty=state_dirty,
        log_entry=log_entry,
        input_fingerprint=final_workspace_fingerprint,
        candidate_fingerprint=candidate_tree_digest,
    )


def _legacy_pack_from_config(config: Mapping[str, Any]) -> dict[str, str]:
    pack = _config_pack(config)
    if not isinstance(pack, Mapping):
        raise LifecycleFailure("DOMAIN_PACK_UNTRACKED", "Workspace has no installed domain pack to adopt")
    result: dict[str, str] = {}
    for key in ("name", "version", "compatible_research_yml_contract"):
        value = pack.get(key)
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
        ):
            raise LifecycleFailure(
                "DOMAIN_PACK_UNTRACKED",
                f"research.yml domain_pack.{key} is required before adoption",
            )
        result[key] = value
    if (
        result["name"] in {".", ".."}
        or "/" in result["name"]
        or "\\" in result["name"]
        or any(ord(character) < 32 for character in result["name"])
    ):
        raise LifecycleFailure("DOMAIN_PACK_UNTRACKED", "Configured domain-pack name is not portable")
    return result


def plan_adopt(
    project_root: Path,
    *,
    accept_local_overrides: bool = False,
    dry_run: bool = True,
    warnings: list[str] | None = None,
) -> LifecyclePlan:
    root = Path(project_root).expanduser().resolve()
    if _transaction_path(root).exists() or _transaction_path(root).is_symlink():
        raise LifecycleFailure(
            "DOMAIN_PACK_TRANSACTION_INCOMPLETE",
            "An interrupted domain-pack transaction must be recovered before adoption planning",
            details={"transaction": TRANSACTION_RELATIVE.as_posix()},
        )
    initial_workspace_shallow = _workspace_fingerprint(root)
    if _state_path(root).exists() or _state_path(root).is_symlink():
        state = load_state(root)
        if _workspace_fingerprint(root) != initial_workspace_shallow:
            raise LifecycleFailure(
                "DOMAIN_PACK_REFRESH_CONFLICT",
                "Workspace changed while domain-pack adoption was being planned",
                exit_code=3,
            )
        input_fingerprint = _workspace_fingerprint(root, state)
        if _workspace_fingerprint(root) != initial_workspace_shallow:
            raise LifecycleFailure(
                "DOMAIN_PACK_REFRESH_CONFLICT",
                "Workspace changed while domain-pack adoption was being planned",
                exit_code=3,
            )
        pack = state["pack"]
        return LifecyclePlan(
            report=_adopt_report(
                mode="dry-run" if dry_run else "write",
                target=root,
                status="no_changes",
                pack={
                    "name": pack["name"],
                    "installed_version": pack["installed_version"],
                    "overlay_sha256": pack["overlay_sha256"],
                    "tree_sha256": pack["tree_sha256"],
                    "source_kind": pack["source_kind"],
                },
                changes=[],
                conflicts=[],
                warnings=warnings,
            ),
            state=state,
            input_fingerprint=input_fingerprint,
        )
    document, _original = _research_round_trip(root / "research.yml")
    configured = _legacy_pack_from_config(document)
    if configured["compatible_research_yml_contract"] != _workspace_contract(root):
        raise LifecycleFailure(
            "DOMAIN_PACK_UNTRACKED",
            "Installed pack contract does not match the workspace contract",
        )
    target_relative = f"domain-packs/{configured['name']}"
    installed_root = _installed_pack_root(
        root,
        target_relative,
        error_code="DOMAIN_PACK_UNTRACKED",
    )
    try:
        installed_snapshot = file_inventory(installed_root, include_bytes=True)
    except LifecycleFailure as exc:
        raise LifecycleFailure(
            "DOMAIN_PACK_UNTRACKED",
            f"Installed legacy pack tree is unsafe: {exc}",
            details=exc.details,
        ) from exc
    inventory = {
        relative: sha256_bytes(content)
        for relative, content in installed_snapshot.items()
    }
    installed_tree_digest = _inventory_sha256(inventory)
    overlay_content = installed_snapshot.get("research.overlay.yml")
    if overlay_content is None:
        raise LifecycleFailure(
            "DOMAIN_PACK_UNTRACKED",
            "Installed legacy pack has no research.overlay.yml",
        )
    input_fingerprint = _workspace_fingerprint(root, pack_root=installed_root)
    if (
        _workspace_fingerprint(root) != initial_workspace_shallow
        or tree_sha256(installed_root) != installed_tree_digest
    ):
        raise LifecycleFailure(
            "DOMAIN_PACK_REFRESH_CONFLICT",
            "Workspace changed while domain-pack adoption was being planned",
            exit_code=3,
        )
    raw_overlay = _load_overlay_content(
        overlay_content,
        str(installed_root / "research.overlay.yml"),
    )
    declared = _pack_metadata(raw_overlay)
    if installed_root.name != declared["name"] or declared != configured:
        raise LifecycleFailure(
            "DOMAIN_PACK_UNTRACKED",
            "The installed pack directory, overlay metadata, and research.yml identity must match before adoption",
            details={"configured": configured, "installed": declared, "directory": installed_root.name},
        )
    normalized = normalize_overlay_paths(raw_overlay, target_relative)
    ownership: list[dict[str, Any]] = []
    local_overrides: set[str] = set()
    conflicts: list[dict[str, str]] = []
    changes: list[dict[str, str]] = []
    for parts, expected in _adoption_units(document, normalized):
        if parts and parts[0] == "project":
            continue
        current_present, current = _lookup(document, parts)
        target = _target_for_config(parts)
        if _same(current_present, current, True, expected):
            ownership.append(_ownership_entry(parts, expected, Fallback(False)))
            changes.append({"path": target, "action": "adopt"})
            continue
        if accept_local_overrides:
            local_overrides.add(target)
            changes.append({"path": target, "action": "keep_local"})
            continue
        conflicts.append(
            {
                "target": target,
                "kind": "config",
                "reason": "workspace value differs from the installed pack overlay",
            }
        )
    pack_report = {
        "name": declared["name"],
        "installed_version": declared["version"],
        "overlay_sha256": overlay_sha256(raw_overlay),
        "tree_sha256": installed_tree_digest,
        "source_kind": "adopted",
    }
    if conflicts:
        if (
            _workspace_fingerprint(root, pack_root=installed_root) != input_fingerprint
            or tree_sha256(installed_root) != installed_tree_digest
        ):
            raise LifecycleFailure(
                "DOMAIN_PACK_REFRESH_CONFLICT",
                "Workspace changed while domain-pack adoption was being planned",
                exit_code=3,
            )
        report = _adopt_report(
            mode="dry-run" if dry_run else "write",
            target=root,
            status="conflict",
            pack=pack_report,
            changes=[],
            conflicts=conflicts,
            warnings=warnings,
        )
        _raise_conflicts(report)
    state = {
        "schema_version": STATE_SCHEMA_VERSION,
        "pack": {
            "name": declared["name"],
            "installed_version": declared["version"],
            "compatible_research_yml_contract": declared["compatible_research_yml_contract"],
            "target_relative": target_relative,
            "source_kind": "adopted",
            "overlay_sha256": pack_report["overlay_sha256"],
            "normalized_overlay_sha256": overlay_sha256(normalized),
            "tree_sha256": pack_report["tree_sha256"],
        },
        "normalized_overlay": _plain(normalized),
        "pre_pack_config": None,
        "config_ownership": sorted(ownership, key=lambda item: item["path"]),
        "managed_files": [
            {"path": path, "sha256": digest} for path, digest in sorted(inventory.items())
        ],
        "revision_files": [
            {"path": path, "sha256": digest} for path, digest in sorted(inventory.items())
        ],
        "local_overrides": sorted(local_overrides),
        "transaction_id": None,
    }
    changes.extend(
        {"path": _target_for_file(path), "action": "adopt"}
        for path in inventory
    )
    state = _validate_state(state)
    report = _adopt_report(
        mode="dry-run" if dry_run else "write",
        target=root,
        status="planned" if dry_run else "ready",
        pack=pack_report,
        changes=changes,
        conflicts=[],
        warnings=warnings,
    )
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_entry = (
        f"\n## [{timestamp}] domain-pack-adopt | Adopted {declared['name']} lifecycle state\n\n"
        f"- Installed revision: `{declared['version']}`.\n"
        f"- Overlay SHA-256: `{pack_report['overlay_sha256']}`.\n"
        f"- Managed values: {len(ownership)}; local overrides: {len(local_overrides)}.\n"
    )
    final_input_fingerprint = _workspace_fingerprint(root, pack_root=installed_root)
    if (
        final_input_fingerprint != input_fingerprint
        or tree_sha256(installed_root) != installed_tree_digest
    ):
        raise LifecycleFailure(
            "DOMAIN_PACK_REFRESH_CONFLICT",
            "Workspace changed while domain-pack adoption was being planned",
            exit_code=3,
        )
    return LifecyclePlan(
        report=report,
        state=state,
        state_dirty=True,
        log_entry=log_entry,
        input_fingerprint=final_input_fingerprint,
        candidate_fingerprint=pack_report["tree_sha256"],
    )


def _safe_relative(value: str, *, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise LifecycleFailure("DOMAIN_PACK_TRANSACTION_INCOMPLETE", f"Invalid {label} in transaction journal")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or relative.as_posix() in {"", "."}
        or relative.as_posix() != value
        or "\\" in value
    ):
        raise LifecycleFailure("DOMAIN_PACK_TRANSACTION_INCOMPLETE", f"Unsafe {label} in transaction journal")
    return relative


def _safe_workspace_destination(project_root: Path, relative: PurePosixPath) -> Path:
    root = project_root.resolve()
    destination = root.joinpath(*relative.parts)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise LifecycleFailure(
                "DOMAIN_PACK_WRITE_FAILED",
                f"Refusing to write through workspace symlink: {relative.as_posix()}",
                details={"path": relative.as_posix()},
            )
    try:
        if not destination.resolve().is_relative_to(root):
            raise LifecycleFailure(
                "DOMAIN_PACK_WRITE_FAILED",
                f"Refusing domain-pack write outside the workspace: {relative.as_posix()}",
            )
    except OSError as exc:
        raise LifecycleFailure(
            "DOMAIN_PACK_WRITE_FAILED",
            f"Could not resolve workspace destination {relative.as_posix()}: {exc}",
        ) from exc
    return destination


def _journal_pack_target(value: Any, operation: str) -> PurePosixPath | None:
    if operation == "adopt":
        if value is not None:
            raise LifecycleFailure(
                "DOMAIN_PACK_TRANSACTION_INCOMPLETE",
                "Adoption transaction journal must not declare a pack write prefix",
            )
        return None
    if operation != "refresh" or not isinstance(value, str):
        raise LifecycleFailure(
            "DOMAIN_PACK_TRANSACTION_INCOMPLETE",
            "Refresh transaction journal is missing its pack write prefix",
        )
    relative = _safe_relative(value, label="pack write prefix")
    if (
        len(relative.parts) != 2
        or relative.parts[0] != "domain-packs"
        or relative.parts[1] in {"", ".", ".."}
    ):
        raise LifecycleFailure(
            "DOMAIN_PACK_TRANSACTION_INCOMPLETE",
            "Transaction pack write prefix must name one pack under domain-packs/",
        )
    return relative


def _journal_destination_allowed(
    operation: str,
    pack_target: PurePosixPath | None,
    relative: PurePosixPath,
) -> bool:
    common = {STATE_RELATIVE, PurePosixPath("log.md")}
    if relative in common:
        return True
    if operation != "refresh":
        return False
    if relative == PurePosixPath("research.yml"):
        return True
    return bool(
        pack_target is not None
        and len(relative.parts) > len(pack_target.parts)
        and relative.parts[: len(pack_target.parts)] == pack_target.parts
    )


def _journal_document(project_root: Path) -> dict[str, Any]:
    path = _transaction_path(project_root)
    if path.is_symlink() or not path.is_file():
        raise LifecycleFailure(
            "DOMAIN_PACK_TRANSACTION_INCOMPLETE",
            "Domain-pack transaction journal is not a regular workspace file",
        )
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise LifecycleFailure(
            "DOMAIN_PACK_TRANSACTION_INCOMPLETE",
            f"Could not read interrupted transaction journal: {exc}",
        ) from exc
    if not isinstance(document, dict) or document.get("schema_version") != TRANSACTION_SCHEMA_VERSION:
        raise LifecycleFailure("DOMAIN_PACK_TRANSACTION_INCOMPLETE", "Domain-pack transaction journal is invalid")
    transaction_id = document.get("transaction_id")
    operation = document.get("operation")
    entries = document.get("entries")
    if (
        not _valid_transaction_id(transaction_id)
        or document.get("phase") != "prepared"
        or operation not in {"adopt", "refresh"}
        or not isinstance(entries, list)
        or not entries
    ):
        raise LifecycleFailure("DOMAIN_PACK_TRANSACTION_INCOMPLETE", "Domain-pack transaction journal is incomplete")
    pack_target = _journal_pack_target(document.get("pack_target_relative"), operation)
    seen_destinations: set[str] = set()
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("path"), str)
            or not isinstance(entry.get("existed"), bool)
            or not isinstance(entry.get("directory_shell", False), bool)
            or isinstance(entry.get("mode"), bool)
            or not isinstance(entry.get("mode"), int)
            or not 0 <= entry["mode"] <= 0o7777
        ):
            raise LifecycleFailure("DOMAIN_PACK_TRANSACTION_INCOMPLETE", "Domain-pack transaction entry is invalid")
        if entry.get("directory_shell", False) and (
            entry["existed"] or entry.get("staged") is None
        ):
            raise LifecycleFailure(
                "DOMAIN_PACK_TRANSACTION_INCOMPLETE",
                "Transaction directory-shell metadata is inconsistent",
            )
        destination_relative = _safe_relative(entry["path"], label="destination path")
        if (
            entry["path"] in seen_destinations
            or not _journal_destination_allowed(operation, pack_target, destination_relative)
        ):
            raise LifecycleFailure(
                "DOMAIN_PACK_TRANSACTION_INCOMPLETE",
                "Transaction journal contains an unauthorized or duplicate destination",
            )
        seen_destinations.add(entry["path"])
        backup = entry.get("backup")
        backup_sha256 = entry.get("backup_sha256")
        staged = entry.get("staged")
        staged_sha256 = entry.get("staged_sha256")
        transaction_prefix = BACKUP_ROOT_RELATIVE / transaction_id
        if entry["existed"]:
            if not isinstance(backup, str) or not _is_sha256(backup_sha256):
                raise LifecycleFailure(
                    "DOMAIN_PACK_TRANSACTION_INCOMPLETE",
                    "Transaction backup path or digest is missing",
                )
            backup_relative = _safe_relative(backup, label="backup path")
            expected = transaction_prefix / "backup" / destination_relative
            if backup_relative != expected:
                raise LifecycleFailure("DOMAIN_PACK_TRANSACTION_INCOMPLETE", "Transaction backup path escaped its transaction")
        elif backup is not None or backup_sha256 is not None:
            raise LifecycleFailure(
                "DOMAIN_PACK_TRANSACTION_INCOMPLETE",
                "Transaction backup metadata exists for a destination that was originally absent",
            )
        if staged is not None:
            if not isinstance(staged, str) or not _is_sha256(staged_sha256):
                raise LifecycleFailure(
                    "DOMAIN_PACK_TRANSACTION_INCOMPLETE",
                    "Transaction staged path or digest is invalid",
                )
            staged_relative = _safe_relative(staged, label="staged path")
            expected = transaction_prefix / "staged" / destination_relative
            if staged_relative != expected:
                raise LifecycleFailure("DOMAIN_PACK_TRANSACTION_INCOMPLETE", "Transaction staged path escaped its transaction")
        elif staged_sha256 is not None:
            raise LifecycleFailure(
                "DOMAIN_PACK_TRANSACTION_INCOMPLETE",
                "Transaction staged digest exists without staged content",
            )
    if pack_target is not None:
        state_entry = next(
            (entry for entry in entries if entry["path"] == STATE_RELATIVE.as_posix()),
            None,
        )
        expected_target: PurePosixPath | None = None
        if state_entry is not None:
            if not state_entry["existed"]:
                raise LifecycleFailure(
                    "DOMAIN_PACK_TRANSACTION_INCOMPLETE",
                    "Refresh transaction cannot create previously absent lifecycle state",
                )
            state_content = _read_transaction_artifact(
                project_root,
                _safe_relative(state_entry["backup"], label="state backup path"),
                state_entry["backup_sha256"],
                label="domain-pack state backup",
            )
            try:
                prior_state = yaml.safe_load(state_content.decode("utf-8")) or {}
                prior_state = _validate_state(prior_state)
            except (UnicodeDecodeError, yaml.YAMLError, LifecycleFailure) as exc:
                raise LifecycleFailure(
                    "DOMAIN_PACK_TRANSACTION_INCOMPLETE",
                    "Could not validate pre-transaction lifecycle state backup",
                ) from exc
            expected_target = PurePosixPath(prior_state["pack"]["target_relative"])

        research_path = project_root / "research.yml"
        research_entry = next(
            (entry for entry in entries if entry["path"] == "research.yml"),
            None,
        )
        if research_entry is not None and research_entry["existed"]:
            backup_relative = _safe_relative(
                research_entry["backup"],
                label="research.yml backup path",
            )
            backup_content = _read_transaction_artifact(
                project_root,
                backup_relative,
                research_entry["backup_sha256"],
                label="research.yml backup",
            )
            try:
                research = yaml.safe_load(backup_content.decode("utf-8")) or {}
            except (UnicodeDecodeError, yaml.YAMLError) as exc:
                raise LifecycleFailure(
                    "DOMAIN_PACK_TRANSACTION_INCOMPLETE",
                    "Could not parse pre-transaction research.yml backup",
                ) from exc
            if not isinstance(research, dict):
                raise LifecycleFailure(
                    "DOMAIN_PACK_TRANSACTION_INCOMPLETE",
                    "Pre-transaction research.yml backup must contain a mapping",
                )
        elif research_entry is not None:
            raise LifecycleFailure(
                "DOMAIN_PACK_TRANSACTION_INCOMPLETE",
                "Refresh transaction cannot create a previously absent research.yml",
            )
        else:
            try:
                research = load_mapping(research_path, "pre-transaction research.yml")
            except LifecycleFailure as exc:
                raise LifecycleFailure(
                    "DOMAIN_PACK_TRANSACTION_INCOMPLETE",
                    "Could not bind the transaction pack prefix to research.yml",
                ) from exc
        if expected_target is None:
            configured_pack = _config_pack(research)
            configured_name = configured_pack.get("name") if configured_pack is not None else None
            expected_target = (
                PurePosixPath("domain-packs") / configured_name
                if isinstance(configured_name, str) and configured_name
                else None
            )
        if pack_target != expected_target:
            raise LifecycleFailure(
                "DOMAIN_PACK_TRANSACTION_INCOMPLETE",
                "Transaction pack write prefix does not match the configured pack",
            )
    return document


def _read_transaction_artifact(
    project_root: Path,
    relative: PurePosixPath,
    expected_sha256: str,
    *,
    label: str,
) -> bytes:
    """Read one journal artifact only after containment and digest checks."""
    try:
        path = _safe_workspace_destination(project_root, relative)
    except LifecycleFailure as exc:
        raise LifecycleFailure(
            "DOMAIN_PACK_TRANSACTION_INCOMPLETE",
            f"Unsafe {label} path in interrupted transaction",
        ) from exc
    if path.is_symlink() or not path.is_file():
        raise LifecycleFailure(
            "DOMAIN_PACK_TRANSACTION_INCOMPLETE",
            f"Missing or unsafe {label} {relative.as_posix()}",
        )
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise LifecycleFailure(
            "DOMAIN_PACK_TRANSACTION_INCOMPLETE",
            f"Could not read {label} {relative.as_posix()}: {exc}",
        ) from exc
    if sha256_bytes(content) != expected_sha256:
        raise LifecycleFailure(
            "DOMAIN_PACK_TRANSACTION_INCOMPLETE",
            f"Digest mismatch for {label} {relative.as_posix()}",
        )
    return content


def _transaction_destination(
    project_root: Path,
    relative: PurePosixPath,
) -> Path:
    try:
        return _safe_workspace_destination(project_root, relative)
    except LifecycleFailure as exc:
        raise LifecycleFailure(
            "DOMAIN_PACK_TRANSACTION_INCOMPLETE",
            f"Unsafe destination in interrupted transaction: {relative.as_posix()}",
        ) from exc


def _preflight_transaction_entries(
    project_root: Path,
    document: Mapping[str, Any],
    *,
    verify_staged: bool,
) -> list[dict[str, Any]]:
    """Validate every restoration input before any live destination is changed."""
    prepared: list[dict[str, Any]] = []
    for index, entry in enumerate(document["entries"]):
        relative = _safe_relative(entry["path"], label="destination path")
        destination = _transaction_destination(project_root, relative)
        if destination.is_symlink():
            raise LifecycleFailure(
                "DOMAIN_PACK_TRANSACTION_INCOMPLETE",
                f"Unsafe transaction destination {relative.as_posix()}",
            )
        if destination.exists() and not destination.is_file() and not (
            (entry["existed"] or entry.get("directory_shell", False))
            and destination.is_dir()
        ):
            raise LifecycleFailure(
                "DOMAIN_PACK_TRANSACTION_INCOMPLETE",
                f"Transaction destination is not recoverable: {relative.as_posix()}",
            )
        backup_content: bytes | None = None
        if entry["existed"]:
            backup_content = _read_transaction_artifact(
                project_root,
                _safe_relative(entry["backup"], label="backup path"),
                entry["backup_sha256"],
                label="transaction backup",
            )
        staged_content: bytes | None = None
        if verify_staged and entry["staged"] is not None:
            staged_content = _read_transaction_artifact(
                project_root,
                _safe_relative(entry["staged"], label="staged path"),
                entry["staged_sha256"],
                label="staged transaction content",
            )
        prepared.append(
            {
                "entry": entry,
                "index": index,
                "relative": relative,
                "destination": destination,
                "backup_content": backup_content,
                "staged_content": staged_content,
            }
        )
    removal_indexes = {
        item["relative"].as_posix(): item["index"]
        for item in prepared
        if not item["entry"]["existed"]
    }
    root = project_root.resolve()
    for item in prepared:
        destination = item["destination"]
        if not destination.is_dir() or destination.is_symlink():
            continue
        # A file restored over a transaction-created directory is safe only
        # when every current file below it is an originally absent destination
        # that reverse-order recovery will remove first. Conversely, a pack
        # file replacing an original directory may contain only files covered
        # by authenticated deletion entries in this transaction.
        try:
            descendants = sorted(destination.rglob("*"))
        except OSError as exc:
            raise LifecycleFailure(
                "DOMAIN_PACK_TRANSACTION_INCOMPLETE",
                f"Could not inspect transaction-created directory {item['relative'].as_posix()}: {exc}",
            ) from exc
        for descendant in descendants:
            if descendant.is_symlink() or (not descendant.is_file() and not descendant.is_dir()):
                raise LifecycleFailure(
                    "DOMAIN_PACK_TRANSACTION_INCOMPLETE",
                    f"Transaction-created directory contains unsafe content: {item['relative'].as_posix()}",
                )
            if descendant.is_dir():
                continue
            descendant_relative = descendant.relative_to(root).as_posix()
            if item["entry"].get("directory_shell", False):
                deletion = next(
                    (
                        prepared_item["entry"]
                        for prepared_item in prepared
                        if prepared_item["relative"].as_posix() == descendant_relative
                    ),
                    None,
                )
                if (
                    deletion is None
                    or not deletion["existed"]
                    or deletion["staged"] is not None
                ):
                    raise LifecycleFailure(
                        "DOMAIN_PACK_TRANSACTION_INCOMPLETE",
                        f"Original directory contains untracked content: {descendant_relative}",
                    )
                continue
            removal_index = removal_indexes.get(descendant_relative)
            if removal_index is None or removal_index <= item["index"]:
                raise LifecycleFailure(
                    "DOMAIN_PACK_TRANSACTION_INCOMPLETE",
                    f"Transaction-created directory contains untracked content: {descendant_relative}",
                )
    return prepared


def _remove_empty_directory_tree(path: Path) -> None:
    """Remove directory shells left by a transaction that replaced a file."""
    for child in sorted(path.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if child.is_symlink() or not child.is_dir():
            raise OSError(f"transaction-created directory contains unexpected content: {path}")
        child.rmdir()
    path.rmdir()


def recover_transaction(project_root: Path) -> str | None:
    """Restore the pre-transaction snapshot recorded by an interrupted writer."""
    root = Path(project_root).expanduser().resolve()
    journal_path = _transaction_path(root)
    if not journal_path.exists() and not journal_path.is_symlink():
        return None
    try:
        journal_path = _safe_workspace_destination(root, TRANSACTION_RELATIVE)
    except LifecycleFailure as exc:
        raise LifecycleFailure(
            "DOMAIN_PACK_TRANSACTION_INCOMPLETE",
            f"Interrupted transaction journal is outside the safe workspace tree: {exc}",
        ) from exc
    document = _journal_document(root)
    transaction_id = document["transaction_id"]
    try:
        # Read and authenticate every backup, and prove every destination is
        # recoverable, before restoring even the first byte.  A damaged later
        # entry therefore cannot leave an earlier destination partly restored.
        prepared = _preflight_transaction_entries(root, document, verify_staged=False)
        for item in reversed(prepared):
            entry = item["entry"]
            relative = item["relative"]
            destination = item["destination"]
            if entry["existed"]:
                if destination.is_dir() and not destination.is_symlink():
                    _remove_empty_directory_tree(destination)
                restore_mode = (
                    RESTRICTIVE_FILE_MODE
                    if relative == STATE_RELATIVE
                    else int(entry["mode"])
                )
                _atomic_write(destination, item["backup_content"], mode=restore_mode)
            elif destination.exists():
                if entry.get("directory_shell", False) and destination.is_dir():
                    # The original directory shell is reconstructed naturally
                    # as descendant backups are restored below.
                    continue
                if destination.is_symlink() or not destination.is_file():
                    raise OSError(f"cannot remove non-file destination {relative.as_posix()}")
                destination.unlink()
        journal_path.unlink()
    except (OSError, LifecycleFailure) as exc:
        raise LifecycleFailure(
            "DOMAIN_PACK_TRANSACTION_INCOMPLETE",
            f"Could not restore interrupted domain-pack transaction {transaction_id}: {exc}",
            details={"transaction_id": transaction_id},
        ) from exc
    return transaction_id


def _transaction_payloads(
    project_root: Path,
    plan: LifecyclePlan,
    *,
    installed_target_relative: str | None,
) -> list[tuple[PurePosixPath, bytes | None]]:
    payloads: list[tuple[PurePosixPath, bytes | None]] = []
    if installed_target_relative is not None:
        pack_prefix = PurePosixPath(installed_target_relative)
        ordered_files = sorted(
            plan.desired_files.items(),
            key=lambda item: (item[1] is not None, item[0]),
        )
        for relative, content in ordered_files:
            payloads.append((pack_prefix / PurePosixPath(relative), content))
    if plan.config_text is not None:
        payloads.append((PurePosixPath("research.yml"), plan.config_text.encode("utf-8")))
    if plan.state is not None and plan.state_dirty:
        state = copy.deepcopy(plan.state)
        # The durable journal is the sole in-progress marker. A successfully
        # installed state must never retain a stale transaction identity after
        # the journal has been removed.
        state["transaction_id"] = None
        state = _validate_state(state)
        payloads.append((STATE_RELATIVE, _yaml_bytes(state)))
    if plan.log_entry is not None:
        log_path = project_root / "log.md"
        prior = log_path.read_bytes() if log_path.is_file() and not log_path.is_symlink() else b""
        payloads.append((PurePosixPath("log.md"), prior + plan.log_entry.encode("utf-8")))
    seen: set[str] = set()
    for relative, _content in payloads:
        if relative.as_posix() in seen:
            raise LifecycleFailure("DOMAIN_PACK_STATE_INVALID", "Domain-pack transaction contains duplicate outputs")
        seen.add(relative.as_posix())
    return payloads


def apply_plan(
    project_root: Path,
    plan: LifecyclePlan,
    *,
    installed_target_relative: str | None,
    candidate_root: Path | None = None,
) -> dict[str, Any]:
    root = Path(project_root).expanduser().resolve()
    fingerprint_state = (
        load_state(root)
        if installed_target_relative is not None
        else plan.state if plan.report.get("operation") == "adopt" else None
    )
    if _workspace_fingerprint(root, fingerprint_state) != plan.input_fingerprint:
        raise LifecycleFailure(
            "DOMAIN_PACK_REFRESH_CONFLICT",
            "Workspace changed after domain-pack planning; no writes were performed",
            exit_code=3,
        )
    if candidate_root is not None and tree_sha256(candidate_root) != plan.candidate_fingerprint:
        raise LifecycleFailure(
            "DOMAIN_PACK_REFRESH_CONFLICT",
            "Candidate pack changed after validation; no writes were performed",
            exit_code=3,
        )
    if plan.report["status"] == "no_changes":
        return plan.report
    transaction_id = uuid.uuid4().hex
    payloads = _transaction_payloads(
        root,
        plan,
        installed_target_relative=installed_target_relative,
    )
    transaction_relative = BACKUP_ROOT_RELATIVE / transaction_id
    # Resolve every workspace and staging destination before creating any
    # transaction directories. In particular, a planted .replaced symlink must
    # produce zero writes outside the workspace.
    payload_map = {relative.as_posix(): content for relative, content in payloads}
    for relative, content in payloads:
        destination = _safe_workspace_destination(root, relative)
        if destination.is_dir() and not destination.is_symlink() and content is not None:
            unsafe_descendants = [
                path.relative_to(root).as_posix()
                for path in destination.rglob("*")
                if path.is_file()
                and payload_map.get(path.relative_to(root).as_posix(), _MISSING) is not None
            ]
            if unsafe_descendants:
                raise LifecycleFailure(
                    "DOMAIN_PACK_WRITE_FAILED",
                    "Domain-pack directory replacement contains untracked files",
                    details={"paths": sorted(unsafe_descendants)},
                )
            continue
        if destination.exists() and (destination.is_symlink() or not destination.is_file()):
            raise LifecycleFailure(
                "DOMAIN_PACK_WRITE_FAILED",
                f"Domain-pack destination is not a regular file: {relative.as_posix()}",
            )
    _safe_workspace_destination(root, TRANSACTION_RELATIVE)
    transaction_root = _safe_workspace_destination(root, transaction_relative)
    backup_root = _safe_workspace_destination(root, transaction_relative / "backup")
    staged_root = _safe_workspace_destination(root, transaction_relative / "staged")
    entries: list[dict[str, Any]] = []
    journal_written = False
    try:
        _mkdir_private(backup_root)
        _mkdir_private(staged_root)
        for relative, content in payloads:
            destination = _safe_workspace_destination(root, relative)
            directory_shell = (
                destination.is_dir()
                and not destination.is_symlink()
                and content is not None
            )
            if destination.exists() and (
                destination.is_symlink()
                or (not destination.is_file() and not directory_shell)
            ):
                raise OSError(f"destination is not a regular file: {relative.as_posix()}")
            existed = destination.is_file()
            mode = (
                RESTRICTIVE_FILE_MODE
                if relative == STATE_RELATIVE
                else stat.S_IMODE(destination.stat().st_mode)
                if existed
                else RESTRICTIVE_FILE_MODE
            )
            backup_relative: PurePosixPath | None = None
            backup_digest: str | None = None
            if existed:
                backup_relative = transaction_relative / "backup" / relative
                backup = _safe_workspace_destination(root, backup_relative)
                backup_content = destination.read_bytes()
                backup_digest = sha256_bytes(backup_content)
                _atomic_write(backup, backup_content, mode=mode)
            staged_relative: PurePosixPath | None = None
            staged_digest: str | None = None
            if content is not None:
                staged_relative = transaction_relative / "staged" / relative
                staged = _safe_workspace_destination(root, staged_relative)
                staged_digest = sha256_bytes(content)
                _atomic_write(staged, content, mode=mode)
            entries.append(
                {
                    "path": relative.as_posix(),
                    "existed": existed,
                    "directory_shell": directory_shell,
                    "mode": mode,
                    "backup": backup_relative.as_posix() if backup_relative else None,
                    "backup_sha256": backup_digest,
                    "staged": staged_relative.as_posix() if staged_relative else None,
                    "staged_sha256": staged_digest,
                }
            )
        journal = {
            "schema_version": TRANSACTION_SCHEMA_VERSION,
            "transaction_id": transaction_id,
            "operation": plan.report["operation"],
            "phase": "prepared",
            "pack_target_relative": installed_target_relative,
            "entries": entries,
        }
        # Validate the exact authorization envelope we are about to persist so
        # generated and recovered journals share one destination contract.
        journal_path = _safe_workspace_destination(root, TRANSACTION_RELATIVE)
        _atomic_write(journal_path, _yaml_bytes(journal))
        journal_written = True
        persisted_journal = _journal_document(root)
        # Authenticate all backups and staged outputs before the first live
        # replacement.  Application uses the authenticated byte snapshots.
        prepared = _preflight_transaction_entries(root, persisted_journal, verify_staged=True)
        for item in prepared:
            entry = item["entry"]
            relative = item["relative"]
            destination = item["destination"]
            if entry["staged"] is None:
                if destination.exists():
                    if destination.is_symlink() or not destination.is_file():
                        raise OSError(f"cannot retire non-file destination {relative.as_posix()}")
                    destination.unlink()
                continue
            if destination.is_dir() and entry.get("directory_shell", False):
                _remove_empty_directory_tree(destination)
            _atomic_write(destination, item["staged_content"], mode=int(entry["mode"]))
        journal_path.unlink()
    except Exception as exc:  # ordinary failures roll back synchronously; BaseException leaves the journal
        if journal_written:
            try:
                recover_transaction(root)
            except LifecycleFailure as recovery_exc:
                raise LifecycleFailure(
                    "DOMAIN_PACK_TRANSACTION_INCOMPLETE",
                    f"Domain-pack write failed and rollback did not complete: {recovery_exc}",
                    details={"transaction_id": transaction_id},
                ) from exc
        else:
            shutil.rmtree(transaction_root, ignore_errors=True)
        if isinstance(exc, LifecycleFailure):
            raise exc
        raise LifecycleFailure(
            "DOMAIN_PACK_WRITE_FAILED",
            f"Could not apply domain-pack transaction: {exc}",
            details={"transaction_id": transaction_id},
        ) from exc
    report = copy.deepcopy(plan.report)
    report["status"] = "applied"
    report["log_appended"] = plan.log_entry is not None
    return report


def _preflight_workspace_directory(project_input: Path, root: Path, *, error_code: str) -> None:
    """Reject a missing or redirected target without creating lock artifacts."""
    if project_input.is_symlink() or not project_input.is_dir() or not root.is_dir():
        raise LifecycleFailure(
            error_code,
            "Workspace target must be an existing, non-symbolic-link directory",
            details={"target": str(project_input)},
        )


def _preflight_refresh_workspace(project_input: Path, root: Path) -> None:
    _preflight_workspace_directory(
        project_input,
        root,
        error_code="DOMAIN_PACK_STATE_INVALID",
    )
    state = load_state(root)
    _workspace_contract(root)
    document, _original = _research_round_trip(root / "research.yml")
    if not isinstance(_config_pack(document), Mapping):
        raise LifecycleFailure(
            "DOMAIN_PACK_STATE_INVALID",
            "research.yml has no valid domain-pack identity mapping",
        )
    installed_root = _installed_pack_root(
        root,
        state["pack"]["target_relative"],
        error_code="DOMAIN_PACK_STATE_INVALID",
    )
    try:
        _safe_pack_files(installed_root)
    except LifecycleFailure as exc:
        raise LifecycleFailure(
            "DOMAIN_PACK_STATE_INVALID",
            f"Tracked installed pack tree is unsafe: {exc}",
            details=exc.details,
        ) from exc


def _preflight_adopt_workspace(
    project_input: Path,
    root: Path,
    *,
    accept_local_overrides: bool,
) -> None:
    _preflight_workspace_directory(
        project_input,
        root,
        error_code="DOMAIN_PACK_UNTRACKED",
    )
    # Adoption's planner is entirely read-only, and is the canonical legacy
    # workspace validation. Replanning under the lock still protects against a
    # concurrent edit between this preflight and application.
    plan_adopt(
        root,
        accept_local_overrides=accept_local_overrides,
        dry_run=True,
    )


def run_refresh(
    project_root: Path,
    candidate_root: Path,
    *,
    keep_local: list[str] | None = None,
    accept_pack: list[str] | None = None,
    dry_run: bool = False,
    source_kind: str | None = None,
    validated_candidate_fingerprint: str | None = None,
    candidate_validator: Callable[[Path], str | None] | None = None,
) -> dict[str, Any]:
    project_input = Path(project_root).expanduser()
    if project_input.is_symlink():
        raise LifecycleFailure(
            "DOMAIN_PACK_STATE_INVALID",
            "Workspace target must not be a symbolic link",
            details={"target": str(project_input)},
        )
    try:
        root = project_input.resolve()
    except (OSError, RuntimeError) as exc:
        raise LifecycleFailure(
            "DOMAIN_PACK_STATE_INVALID",
            f"Could not resolve workspace target safely: {exc}",
            details={"target": str(project_input)},
        ) from exc
    candidate = Path(candidate_root).expanduser()
    if dry_run:
        if _transaction_path(root).exists() or _transaction_path(root).is_symlink():
            # Report the interrupted workspace before inspecting a candidate
            # against partially replaced state. Dry-run never performs recovery.
            return plan_refresh(
                root,
                candidate,
                keep_local=keep_local,
                accept_pack=accept_pack,
                dry_run=True,
                source_kind=source_kind,
            ).report
        validated_tree = (
            candidate_validator(candidate)
            if candidate_validator is not None
            else validated_candidate_fingerprint
        )
        plan = plan_refresh(
            root,
            candidate,
            keep_local=keep_local,
            accept_pack=accept_pack,
            dry_run=True,
            source_kind=source_kind,
        )
        if (
            validated_tree is not None
            and plan.candidate_fingerprint != validated_tree
        ):
            raise LifecycleFailure(
                "DOMAIN_PACK_INVALID",
                "Candidate domain pack changed after canonical validation",
            )
        return plan.report
    transaction_pending = _transaction_path(root).exists() or _transaction_path(root).is_symlink()
    if not transaction_pending:
        _preflight_refresh_workspace(project_input, root)
    lock_path = _safe_workspace_destination(root, LOCK_RELATIVE)
    log_lock_path = _safe_workspace_destination(root, LOG_LOCK_RELATIVE)
    _mkdir_private(lock_path.parent)
    with workspace_lock(lock_path, purpose="domain-pack refresh"):
        with workspace_lock(log_lock_path, purpose="activity log append"):
            recovered = recover_transaction(root)
        validated_tree = (
            candidate_validator(candidate)
            if candidate_validator is not None
            else validated_candidate_fingerprint
        )
        with workspace_lock(log_lock_path, purpose="activity log append"):
            warnings = [f"Recovered interrupted transaction {recovered} before replanning."] if recovered else []
            plan = plan_refresh(
                root,
                candidate,
                keep_local=keep_local,
                accept_pack=accept_pack,
                dry_run=False,
                source_kind=source_kind,
                warnings=warnings,
            )
            if validated_tree is not None and plan.candidate_fingerprint != validated_tree:
                raise LifecycleFailure(
                    "DOMAIN_PACK_INVALID",
                    "Candidate domain pack changed while the refresh was being planned",
                )
            return apply_plan(
                root,
                plan,
                installed_target_relative=plan.state["pack"]["target_relative"] if plan.state else None,
                candidate_root=candidate,
            )


def run_adopt(
    project_root: Path,
    *,
    accept_local_overrides: bool = False,
    dry_run: bool = False,
    validator: Callable[[Path], str | None] | None = None,
) -> dict[str, Any]:
    project_input = Path(project_root).expanduser()
    if project_input.is_symlink():
        raise LifecycleFailure(
            "DOMAIN_PACK_UNTRACKED",
            "Workspace target must not be a symbolic link",
            details={"target": str(project_input)},
        )
    try:
        root = project_input.resolve()
    except (OSError, RuntimeError) as exc:
        raise LifecycleFailure(
            "DOMAIN_PACK_UNTRACKED",
            f"Could not resolve workspace target safely: {exc}",
            details={"target": str(project_input)},
        ) from exc
    if dry_run:
        if _transaction_path(root).exists() or _transaction_path(root).is_symlink():
            # Reuse the planner's stable refusal without validating a mixed tree.
            return plan_adopt(
                root,
                accept_local_overrides=accept_local_overrides,
                dry_run=True,
            ).report
        validated_tree = validator(root) if validator is not None else None
        plan = plan_adopt(
            root,
            accept_local_overrides=accept_local_overrides,
            dry_run=True,
        )
        if (
            validated_tree is not None
            and plan.state is not None
            and plan.state["pack"]["tree_sha256"] != validated_tree
        ):
            raise LifecycleFailure(
                "DOMAIN_PACK_UNTRACKED",
                "Installed domain pack changed after canonical validation",
            )
        return plan.report
    transaction_pending = _transaction_path(root).exists() or _transaction_path(root).is_symlink()
    if not transaction_pending:
        _preflight_adopt_workspace(
            project_input,
            root,
            accept_local_overrides=accept_local_overrides,
        )
    lock_path = _safe_workspace_destination(root, LOCK_RELATIVE)
    log_lock_path = _safe_workspace_destination(root, LOG_LOCK_RELATIVE)
    _mkdir_private(lock_path.parent)
    with workspace_lock(lock_path, purpose="domain-pack adoption"):
        with workspace_lock(log_lock_path, purpose="activity log append"):
            recovered = recover_transaction(root)
        validated_tree = validator(root) if validator is not None else None
        with workspace_lock(log_lock_path, purpose="activity log append"):
            warnings = [f"Recovered interrupted transaction {recovered} before replanning."] if recovered else []
            plan = plan_adopt(
                root,
                accept_local_overrides=accept_local_overrides,
                dry_run=False,
                warnings=warnings,
            )
            if (
                validated_tree is not None
                and plan.state is not None
                and plan.state["pack"]["tree_sha256"] != validated_tree
            ):
                raise LifecycleFailure(
                    "DOMAIN_PACK_UNTRACKED",
                    "Installed domain pack changed after canonical validation",
                )
            return apply_plan(root, plan, installed_target_relative=None)
