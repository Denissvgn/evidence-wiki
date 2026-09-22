"""Caller-selected pack revision catalogs, separate from workspace lifecycle state."""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import uuid
from pathlib import Path

from ._pack_io import MAX_FILE, canonical, capture_pack, identity, json_document, read_file, refuse, relative_path
from ._script_host import shared_assets_root
from .errors import EvidenceWikiError

SCHEMA = "evidence-pack-catalog/v1"
MAX_ROOTS = 8
MAX_REVISIONS = 32


def _id(value):
    if not isinstance(value, str) or re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}", value) is None:
        refuse("catalog_id_invalid")
    return value


def _hash(value):
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        refuse("catalog_hash_invalid")
    return value


def _outside_assets(path: Path | int, *, additional_roots=()):
    assets = shared_assets_root().resolve()
    protected_paths = [assets / name for name in ("workspace-template", "domain-packs", "orchestrator")]
    protected_paths.extend(Path(root).resolve() for root in additional_roots)
    if type(path) is int:
        protected_ids = {(item.stat().st_dev, item.stat().st_ino) for item in protected_paths if item.is_dir()}
        descriptor = os.dup(path)
        try:
            for _ in range(128):
                observed = os.fstat(descriptor)
                current = (observed.st_dev, observed.st_ino)
                if current in protected_ids:
                    refuse("catalog_installed_assets_forbidden")
                parent = os.open("..", _writer_flags(), dir_fd=descriptor)
                parent_stat = os.fstat(parent)
                os.close(descriptor)
                descriptor = parent
                if current == (parent_stat.st_dev, parent_stat.st_ino):
                    return
            refuse("catalog_ancestry_bound")
        finally:
            os.close(descriptor)
    candidate = path.resolve()
    for protected in protected_paths:
        if candidate == protected or protected in candidate.parents:
            refuse("catalog_installed_assets_forbidden")


def _read(root: Path):
    from .pack_discovery import safe_name

    value = json_document(read_file(root, "catalog.json"))
    try:
        if (set(value) != {"schema_version", "roots", "revisions"} or value["schema_version"] != SCHEMA
                or not isinstance(value["roots"], dict) or not 1 <= len(value["roots"]) <= MAX_ROOTS
                or not isinstance(value["revisions"], dict) or len(value["revisions"]) > MAX_REVISIONS):
            refuse("catalog_invalid")
        for key, row in value["roots"].items():
            _id(key)
            if (set(row) != {"path", "identity"} or not isinstance(row["path"], str)
                    or not Path(row["path"]).is_absolute() or set(row["identity"]) != {"device", "inode"}
                    or any(not isinstance(item, str) or not item.isdigit() for item in row["identity"].values())):
                refuse("catalog_root_invalid")
        for key, row in value["revisions"].items():
            _id(key)
            if (set(row) - {"assessment"} != {"root_id", "path", "name", "version", "identity", "scope", "derived_from", "receipt"}
                    or row["root_id"] not in value["roots"] or not isinstance(row["scope"], str) or not 1 <= len(row["scope"]) <= 1024):
                refuse("catalog_revision_invalid")
            safe_name(row["name"])
            if not isinstance(row["version"], str) or not 1 <= len(row["version"]) <= 128:
                refuse("catalog_revision_invalid")
            relative_path(row["path"])
            if set(row["identity"]) != {"tree_sha256", "overlay_sha256"}:
                refuse("catalog_revision_invalid")
            for item in row["identity"].values():
                _hash(item)
            _hash(row["receipt"])
            if "assessment" in row:
                _hash(row["assessment"])
            _derivation(row["derived_from"])
        return value
    except (ValueError, TypeError, KeyError, AttributeError):
        refuse("catalog_invalid")


def _derivation(value):
    if value is None:
        return
    if (not isinstance(value, dict) or set(value) != {"selector", "tree_sha256", "basis"}
            or value["basis"] != "caller_declared" or not isinstance(value["selector"], str)
            or len(value["selector"]) > 256 or ":" not in value["selector"]
            or not value["selector"].split(":", 1)[1].strip() or value["selector"].split(":", 1)[0] not in {"bundled", "installed", "local"}):
        refuse("catalog_derivation_invalid")
    _hash(value["tree_sha256"])


def _write_file(directory: int, name: str, content: bytes, *, replace=False):
    if len(content) > MAX_FILE:
        refuse("catalog_document_bound")
    temporary = ".catalog-" + uuid.uuid4().hex if replace else name
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        if replace:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=directory)


def _writer_flags():
    if os.name != "posix" or os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
        refuse("catalog_write_platform_unsupported", "ONBOARDING_ENVIRONMENT_INCOMPATIBLE")
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def initialize(path: Path, roots: dict[str, str]) -> dict:
    if not isinstance(roots, dict) or not 1 <= len(roots) <= MAX_ROOTS:
        refuse("catalog_roots_bound")
    selected = {}
    for key, location in roots.items():
        _id(key)
        directory = Path(location).expanduser().resolve(strict=True)
        if not directory.is_dir():
            refuse("catalog_root_not_directory")
        _outside_assets(directory)
        selected[key] = {"path": str(directory), "identity": identity(directory)}
    path = path.expanduser().absolute()
    _outside_assets(path)
    flags = _writer_flags()
    parent = os.open(path.parent.resolve(strict=True), flags)
    descriptor = None
    try:
        _outside_assets(parent)
        os.mkdir(path.name, mode=0o700, dir_fd=parent)
        descriptor = os.open(path.name, flags, dir_fd=parent)
        _write_file(descriptor, "catalog.lock", b"")
        _write_file(descriptor, "catalog.json", canonical({"schema_version": SCHEMA, "roots": selected, "revisions": {}}))
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)
    return {"schema_version": SCHEMA, "status": "created", "roots": sorted(selected), "revisions": 0}


def _root(value, key):
    row = value["roots"][key]
    path = Path(row["path"])
    if not path.exists():
        refuse("catalog_root_missing")
    if path.is_symlink() or not path.is_dir() or identity(path) != row["identity"]:
        refuse("catalog_root_changed")
    _outside_assets(path)
    return path


def _candidate(value, row):
    path = _root(value, row["root_id"])
    for part in relative_path(row["path"]).split("/"):
        path = path / part
        if path.is_symlink():
            refuse("catalog_candidate_symlink")
    _outside_assets(path)
    return path


def resolve_entry(path: Path, revision: str) -> Path:
    value = _read(path)
    if revision not in value["revisions"]:
        refuse("catalog_revision_unknown")
    return _candidate(value, value["revisions"][revision])


def register(path: Path, *, revision: str, root_id: str, relative: str, scope: str, derived_from=None,
             authoring_root=None, assessment_id=None) -> dict:
    from .pack_discovery import owner, validate_snapshot

    _id(revision)
    _id(root_id)
    relative_path(relative)
    _derivation(derived_from)
    if (authoring_root is None) != (assessment_id is None):
        refuse("catalog_assessment_options")
    if assessment_id is not None:
        _hash(assessment_id)
    if not isinstance(scope, str) or not 1 <= len(scope.strip()) <= 1024:
        refuse("catalog_scope_invalid")
    _outside_assets(path)
    directory = os.open(path.expanduser().absolute(), _writer_flags())
    lock_descriptor = None
    try:
        _outside_assets(directory)
        locks = owner("_workspace_locks")
        # Initialization publishes one stable lock before making the catalog visible.
        # Never recreate a missing lock beside an existing catalog or a live writer.
        lock_descriptor = os.open("catalog.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with locks.descriptor_lock(lock_descriptor):
            value = _read(directory)
            if root_id not in value["roots"] or revision in value["revisions"]:
                refuse("catalog_root_unknown_or_revision_collision")
            if len(value["revisions"]) >= MAX_REVISIONS:
                refuse("catalog_revision_bound")
            locator = {"root_id": root_id, "path": relative}
            candidate = _candidate(value, locator)
            metadata, receipt = validate_snapshot(candidate)
            assessment = None
            if authoring_root is not None:
                from .pack_acceptance import revalidate

                assessment = revalidate(authoring_root, assessment_id, candidate)
                if assessment["identity"] != metadata["identity"] or assessment["checker_sha256"] != receipt["checker_sha256"]:
                    refuse("catalog_assessment_binding_mismatch")
            receipt_bytes = canonical(receipt)
            receipt_id = hashlib.sha256(receipt_bytes).hexdigest()
            name = "receipt-" + receipt_id + ".json"
            try:
                _write_file(directory, name, receipt_bytes)
            except FileExistsError:
                if read_file(directory, name) != receipt_bytes:
                    refuse("catalog_receipt_collision")
            value["revisions"][revision] = {**locator, "name": metadata["name"], "version": metadata["version"],
                "identity": metadata["identity"], "scope": scope, "derived_from": derived_from, "receipt": receipt_id}
            if assessment is not None:
                from .pack_authoring_store import publish

                publish(directory, "assessment-" + assessment_id + ".json", canonical(assessment))
                value["revisions"][revision]["assessment"] = assessment_id
            # Recheck candidate bytes and root binding before the single catalog commit.
            if capture_pack(_candidate(value, locator)).tree_sha256 != receipt["tree_sha256"]:
                refuse("catalog_candidate_changed")
            observed = os.stat("catalog.lock", dir_fd=directory, follow_symlinks=False)
            held = os.fstat(lock_descriptor)
            if (observed.st_dev, observed.st_ino) != (held.st_dev, held.st_ino):
                refuse("catalog_lock_replaced")
            _write_file(directory, "catalog.json", canonical(value), replace=True)
            now = path.expanduser().absolute().lstat()
            held_root = os.fstat(directory)
            if (now.st_dev, now.st_ino) != (held_root.st_dev, held_root.st_ino):
                refuse("catalog_directory_changed")
            return {"schema_version": SCHEMA, "status": "registered", "selector": "local:" + revision,
                    "identity": metadata["identity"], "validation_receipt": receipt_id, "providers_enabled": False,
                    **({"assessment": assessment_id, "semantic_adequacy": "not_certified", "independent_review": "not_verified"} if assessment is not None else {})}
    except Exception as error:
        if getattr(error, "error_code", None) == "LOCK_UNAVAILABLE":
            refuse("catalog_lock_busy" if getattr(error, "contended", False) else "catalog_lock_unavailable",
                   "ONBOARDING_LOCK_BUSY" if getattr(error, "contended", False) else "ONBOARDING_ENVIRONMENT_INCOMPATIBLE")
        raise
    finally:
        if lock_descriptor is not None:
            os.close(lock_descriptor)
        os.close(directory)


def entries(path: Path, *, only: str | None = None) -> list[dict]:
    from .onboarding_contract import _matches
    from .pack_decisions import schema_document
    from .pack_discovery import checker_identity, inspect_pack

    value = _read(path)
    result = []
    checker = checker_identity()
    for revision, record in sorted(value["revisions"].items()):
        if only is not None and revision != only:
            continue
        row = {"selector": "local:" + revision, "origin": "local", "name": record["name"], "state": "unavailable",
               "metadata": None, "identity": None, "registered_identity": record["identity"], "scope": record["scope"],
               "derived_from": record["derived_from"], "validation": {"state": "not_current", "receipt": record["receipt"],
                   "authority": "caller_local_structural_observation", "semantic_adequacy": "not_evaluated"}, "newer_revision": "unknown"}
        try:
            candidate = _candidate(value, record)
            if not candidate.exists():
                refuse("catalog_candidate_missing")
            view = inspect_pack(candidate, origin="local", selector=row["selector"])
            row.update({key: view[key] for key in ("metadata", "identity", "state")})
            if view["state"] != "available":
                row["reason"] = view.get("reason", "catalog_candidate_invalid")
            elif (view["identity"] != record["identity"] or view["metadata"] is None
                    or view["metadata"]["name"] != record["name"] or view["metadata"]["version"] != record["version"]):
                row.update(state="mutated", reason="catalog_candidate_changed")
            else:
                raw = read_file(path, "receipt-" + record["receipt"] + ".json")
                receipt = json_document(raw)
                _matches(receipt, schema_document("evidence-pack-validation/v1"))
                if (hashlib.sha256(raw).hexdigest() != record["receipt"] or receipt.get("schema_version") != "evidence-pack-validation/v1"
                        or receipt.get("ok") is not True or receipt.get("tree_sha256") != record["identity"]["tree_sha256"]
                        or receipt.get("overlay_sha256") != record["identity"]["overlay_sha256"]):
                    refuse("catalog_receipt_invalid")
                if receipt.get("checker_sha256") == checker:
                    row["validation"]["state"] = "matching_observation"
                else:
                    row["validation"]["state"] = "checker_changed"
                if "assessment" in record:
                    from .pack_acceptance import registered_assessment

                    assessment = registered_assessment(path, record)
                    current = assessment["checker_sha256"] == checker and row["validation"]["state"] == "matching_observation"
                    row["assessment"] = {"sha256": record["assessment"], "state": "matching_observation" if current else "checker_changed",
                        "suite_sha256": assessment["suite_sha256"], "semantic_adequacy": "not_certified",
                        "independent_review": "not_verified", "limitations": assessment["limitations"]}
                    if not current:
                        row.update(state="assessment_stale", reason="catalog_assessment_checker_changed")
                if capture_pack(_candidate(value, record)).tree_sha256 != record["identity"]["tree_sha256"]:
                    row.update(state="mutated", reason="catalog_candidate_changed")
                    row["validation"]["state"] = "not_current"
        except (EvidenceWikiError, OSError, ValueError, TypeError, KeyError) as error:
            row["state"] = "unavailable"
            row["reason"] = getattr(error, "details", {}).get("field", "catalog_entry_unavailable")
            row["validation"]["state"] = "not_current"
        result.append(row)
    return result
