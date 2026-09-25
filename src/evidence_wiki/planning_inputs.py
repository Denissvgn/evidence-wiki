"""Stable local preconditions for read-only setup compilation and saved plans."""

from __future__ import annotations

import hashlib
import os
import platform
import stat
import sys
from pathlib import Path

from ._pack_io import capture_pack, file_reader, identity, read_file, relative_path
from ._script_host import shared_assets_root
from .agent_resources import installation_metadata
from .pack_catalog import _outside_assets
from .pack_discovery import owner, snapshot_metadata
from .planning_contracts import digest, refuse


def target_basis(selection, *, owned_basis=None):
    root = Path(selection["writable_root"]).expanduser().resolve(strict=True)
    if not root.is_dir():
        refuse("/request/payload/target/writable_root")
    relative = relative_path(selection["relative_path"])
    if ".evidence-wiki" in Path(relative).parts:
        refuse("target_reserved_setup_state")
    owner("init_research_workspace").validate_workspace_relative_path(relative, "target.relative_path")
    target = root / relative
    _outside_assets(target, additional_roots=(Path(__file__).parent,))
    if any((parent / "research.yml").is_file() and (parent / "workspace-system.yml").is_file() for parent in target.parents):
        refuse("target_inside_existing_workspace", "ONBOARDING_TARGET_CONFLICT")
    for path in reversed([target, *list(target.parents)[:len(Path(relative).parts) - 1]]):
        if path.is_symlink():
            refuse("target_symlink_forbidden")
    if not target.parent.is_dir():
        refuse("target_parent_missing")
    if owned_basis is not None:
        if (owned_basis["target"] != {"writable_root": str(root), "relative_path": relative}
                or identity(root) != owned_basis["root_identity"]
                or identity(target.parent) != owned_basis["parent_identity"]):
            refuse("setup_parent_changed", "ONBOARDING_OWNERSHIP_CONFLICT")
        return target, owned_basis
    state = "absent"
    directory = None
    if target.exists():
        if not target.is_dir() or next(target.iterdir(), None) is not None:
            refuse("target_requires_absent_or_empty_directory")
        state, directory = "empty", identity(target)
    return target, {"target": {"writable_root": str(root), "relative_path": relative},
        "root_identity": identity(root), "parent_identity": identity(target.parent),
        "state": state, "directory_identity": directory}


def tree_identity(root, *, reader=None):
    """Hash bounded installed inputs, omitting only initializer-excluded caches."""
    rows, size, pending, entries = [], 0, [root], 0
    read = read_file if reader is None else reader
    excluded = owner("init_research_workspace").EXCLUDED_NAMES | {"__pycache__"}
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as children:
            for child in children:
                entries += 1
                if entries > 8192:
                    refuse("installed_tree_entry_bound", "ONBOARDING_LIMIT")
                if child.name in excluded or child.name.endswith((".pyc", ".pyo")):
                    continue
                path = Path(child.path)
                if child.is_symlink():
                    refuse("installed_tree_symlink")
                if child.is_dir(follow_symlinks=False):
                    pending.append(path)
                    continue
                if not child.is_file(follow_symlinks=False):
                    refuse("installed_tree_special_file")
                relative = path.relative_to(root).as_posix()
                raw = read(root, relative, 2_097_152)
                size += len(raw)
                if size > 33_554_432 or len(rows) >= 4096:
                    refuse("installed_tree_bound", "ONBOARDING_LIMIT")
                rows.append({"path": relative, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
                             "executable": bool(path.stat().st_mode & 0o111)})
    return {"sha256": digest(sorted(rows, key=lambda row: row["path"])), "files": len(rows), "bytes": size}


def dependency_implementations(*, reader=None):
    """Bind required distribution bytes, including data used by timezone checks."""
    import importlib.metadata

    from .source_inspection import DEPENDENCIES

    result, total = [], 0
    read = read_file if reader is None else reader
    for name in DEPENDENCIES:
        try:
            distribution = importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            result.append({"distribution": name, "state": "missing", "sha256": None})
            continue
        rows = []
        files = distribution.files
        if files is None or len(files) > 4096:
            refuse("dependency_implementation_unavailable", "ONBOARDING_ENVIRONMENT_INCOMPATIBLE")
        for item in sorted(files):
            if item.suffix in {".pyc", ".pyo"} or "__pycache__" in item.parts:
                continue
            relative_path(str(item))
            path = Path(distribution.locate_file(item)).absolute()
            raw = read(path.parent, path.name, 16_777_216)
            total += len(raw)
            if total > 67_108_864:
                refuse("dependency_implementation_bytes_bound", "ONBOARDING_LIMIT")
            rows.append({"path": str(item), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
        result.append({"distribution": name, "state": "observed", "sha256": digest(rows), "files": len(rows)})
    return result


def installation_basis():
    executable = Path(sys.executable).resolve(strict=True)
    from .source_inspection import dependencies

    with file_reader() as read:
        raw = read(executable.parent, executable.name, 67_108_864)
        return {"installation": installation_metadata(),
            "starter": tree_identity(shared_assets_root() / "workspace-template", reader=read),
            "package_code": tree_identity(Path(__file__).parent, reader=read),
            "interpreter": {"path": str(executable), "invocation": str(Path(sys.executable).absolute()),
                            "prefix": sys.prefix, "sha256": hashlib.sha256(raw).hexdigest(),
                            "version": platform.python_version(), "implementation": platform.python_implementation()},
            "dependencies": dependencies(), "dependency_implementations": dependency_implementations(reader=read)}


def selected_pack(domain):
    selection = domain["pack"]
    if (domain["mode"] == "domain_pack") != (selection is not None):
        refuse("/request/payload/domain/pack_mode_mismatch")
    if selection is None:
        return None, None
    if selection["origin"] == "workspace_installed":
        refuse("domain_pack_requires_bundled_or_caller_local_origin")
    if selection["origin"] == "bundled":
        locator = selection["locator"]
        if locator not in {selection["name"], "bundled:" + selection["name"]}:
            refuse("/request/payload/domain/pack/locator")
        path = shared_assets_root() / "domain-packs" / relative_path(selection["name"])
    else:
        path = Path(selection["locator"]).expanduser().absolute()
    captured = capture_pack(path)
    metadata = snapshot_metadata(captured)
    if (not metadata["metadata_valid"] or not metadata["compatible"]
            or any(selection[key] != metadata[key] for key in ("name", "version"))
            or selection["research_contract_version"] != metadata["compatible_research_yml_contract"]
            or any(selection[key] != metadata["identity"][key] for key in ("tree_sha256", "overlay_sha256"))):
        refuse("domain_pack_identity_or_compatibility_changed", "ONBOARDING_PLAN_STALE")
    return captured, metadata


def local_input(locator, scopes, *, remaining=33_554_432):
    """Observe explicit file hints only inside caller-declared local read scopes."""
    path = Path(locator).expanduser().absolute()
    selected = []
    for value in scopes:
        if "://" in value or not Path(value).is_absolute():
            continue
        declared = Path(value).expanduser().absolute()
        root = declared.resolve(strict=True)
        if not root.is_dir():
            continue
        # An explicitly selected root can use a system spelling alias. Rebase
        # only its lexical descendant, then reject every symlink below it.
        for spelling in (declared, root):
            if path != spelling and spelling in path.parents:
                selected.append((len(spelling.parts), root, path.relative_to(spelling).as_posix()))
    if not selected:
        return {"path": str(path), "state": "outside_declared_scope", "sha256": None}
    _, root, relative = max(selected, key=lambda item: item[0])
    path = root / relative
    relative_path(relative)
    for part in [path, *path.parents]:
        if part == root:
            break
        if part.is_symlink():
            refuse("local_source_symlink_forbidden")
    if not path.exists():
        return {"root": str(root), "root_identity": identity(root), "path": relative, "state": "absent", "sha256": None}
    if not stat.S_ISREG(path.stat().st_mode):
        refuse("local_source_requires_regular_file")
    if path.stat().st_size > remaining:
        refuse("planning_local_input_bytes_bound", "ONBOARDING_LIMIT")
    raw = read_file(root, relative, 16_777_216)
    return {"root": str(root), "root_identity": identity(root), "path": relative, "state": "present",
            "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
