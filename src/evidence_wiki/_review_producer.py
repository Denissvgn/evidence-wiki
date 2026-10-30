"""Private bounded observation of the installed review producer.

Filesystem observations bind a trusted import generation; they do not attest
Python code objects, dependencies, the interpreter, or the operating system.
"""
from __future__ import annotations

import hashlib
import importlib.machinery
import json
import os
import re
import stat
import sys
import weakref
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

MAX_MEMBERS = 1024
MAX_MEMBER_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_DOCUMENT_BYTES = 1024 * 1024
INVENTORY_PATH = "package/evidence_wiki/_review_producer_inventory.json"
ASSET_NAMES = ("workspace-template", "domain-packs", "orchestrator")
_PACKAGE = None
_PACKAGE_BASELINE = None
_ASSET_BASELINE = None
_PAIR = None
_UNAVAILABLE = None
_NATIVE_GENERATIONS = set()
_LOADER_GENERATIONS = set()


class ProducerUnavailable(ValueError):
    """The current import generation cannot provide selective producer custody."""


def _fail(reason):
    raise ProducerUnavailable("review_producer_unavailable: " + reason)


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                       allow_nan=False) + "\n").encode("utf-8")


def content_id(domain, value):
    return "sha256:" + hashlib.sha256(domain.encode("utf-8") + b"\0" + canonical(value)).hexdigest()


def _component(name):
    if (type(name) is not str or not name or name in {".", ".."} or name.endswith((".", " "))
            or any(ord(c) < 32 or c in '\\/:<>"|?*' for c in name)):
        _fail("unsafe_member_path")
    try:
        name.encode("utf-8")
    except UnicodeError:
        _fail("unsafe_member_path")


def validate_path(path):
    if type(path) is not str:
        _fail("unsafe_member_path")
    for part in path.split("/"):
        _component(part)
    if len(path.encode("utf-8")) > 1024 or not any(path.startswith(prefix) for prefix in (
            "package/evidence_wiki/", *("assets/" + name + "/" for name in ASSET_NAMES))):
        _fail("unsafe_member_path")
    if path.startswith("package/evidence_wiki/assets/"):
        _fail("duplicate_assets_mapping")
    return path


def _observation(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns, getattr(info, "native_change_time_ns", None))


def _directory_identity(info):
    return (info.st_dev, info.st_ino, info.st_mode)


def _safe_type(info, directory=False):
    if getattr(info, "st_file_attributes", 0) & 0x400:
        _fail("reparse_point")
    if directory:
        if not stat.S_ISDIR(info.st_mode):
            _fail("directory_required")
    elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        _fail("unlinked_regular_file_required")


@contextmanager
def _open_root(path):
    if (os.name != "posix" or not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd
            or os.stat not in os.supports_dir_fd or os.listdir not in os.supports_fd):
        _fail("filesystem_capability_unsupported")
    path = Path(path)
    if not path.is_absolute() or any(part in {".", ".."} for part in path.parts):
        _fail("origin_path_invalid")
    descriptors, chain = [], []
    try:
        descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(descriptor)
        chain.append(_directory_identity(os.fstat(descriptor)))
        for part in path.parts[1:]:
            _component(part)
            named = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            _safe_type(named, True)
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            descriptors.append(child)
            if _observation(named) != _observation(os.fstat(child)):
                _fail("root_changed")
            descriptor = child
            chain.append(_directory_identity(named))
        yield descriptor, tuple(chain)
        for index, part in enumerate(path.parts[1:]):
            named = os.stat(part, dir_fd=descriptors[index], follow_symlinks=False)
            if _directory_identity(named) != chain[index + 1]:
                _fail("root_changed")
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def observe_roots(roots, *, read=False, prospective_inventory=False):
    """Enumerate complete projected membership and optionally read exact bytes."""
    files, directories, data, chains = {}, {}, {}, {}
    entry_count = total = 0
    for prefix, root in roots:
        with _open_root(root) as (descriptor, chain):
            chains[prefix] = chain

            def visit(parent, relative, depth, prefix=prefix):
                nonlocal entry_count, total
                if depth > 64:
                    _fail("depth_bound")
                before = os.fstat(parent)
                _safe_type(before, True)
                coordinate = prefix + ("/" + relative if relative else "")
                directories[coordinate] = _observation(before)
                names = os.listdir(parent)
                entry_count += len(names)
                if entry_count > 20000:
                    _fail("directory_entry_bound")
                for name in sorted(names, key=lambda value: value.encode("utf-8")):
                    _component(name)
                    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    child_relative = relative + "/" + name if relative else name
                    logical = prefix + "/" + child_relative
                    if stat.S_ISDIR(info.st_mode):
                        _safe_type(info, True)
                        if name == "__pycache__":
                            continue
                        if prefix == "package/evidence_wiki" and not relative and name == "assets":
                            continue
                        child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                        try:
                            if _observation(info) != _observation(os.fstat(child)):
                                _fail("directory_changed")
                            visit(child, child_relative, depth + 1)
                        finally:
                            os.close(child)
                    else:
                        _safe_type(info)
                        if name.endswith((".pyc", ".pyo")):
                            continue
                        if prefix == "package/evidence_wiki" and name.endswith(".orig"):
                            continue
                        validate_path(logical)
                        if logical in files or len(files) >= MAX_MEMBERS or info.st_size > MAX_MEMBER_BYTES:
                            _fail("member_bound_or_duplicate")
                        total += info.st_size
                        if total > MAX_TOTAL_BYTES:
                            _fail("total_byte_bound")
                        files[logical] = _observation(info)
                        if read:
                            held = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                            try:
                                if _observation(info) != _observation(os.fstat(held)):
                                    _fail("member_changed")
                                chunks, count = [], 0
                                while True:
                                    chunk = os.read(held, min(65536, MAX_MEMBER_BYTES + 1 - count))
                                    if not chunk:
                                        break
                                    count += len(chunk)
                                    if count > MAX_MEMBER_BYTES:
                                        _fail("member_byte_bound")
                                    chunks.append(chunk)
                                if count != info.st_size or _observation(info) != _observation(os.fstat(held)):
                                    _fail("member_changed")
                                data[logical] = b"".join(chunks)
                            finally:
                                os.close(held)
                        if _observation(info) != _observation(os.stat(name, dir_fd=parent, follow_symlinks=False)):
                            _fail("member_changed")
                if _observation(before) != _observation(os.fstat(parent)) or sorted(names) != sorted(os.listdir(parent)):
                    _fail("directory_changed")
            visit(descriptor, "", 0)
    if prospective_inventory and INVENTORY_PATH not in files:
        files[INVENTORY_PATH] = None
    return files, directories, chains, data


def _interval(snapshot):
    files, directories, chains, _data = snapshot
    return files, {key: value[:3] for key, value in directories.items()}, chains


def _package_root():
    package = _PACKAGE
    spec = getattr(package, "__spec__", None)
    locations = getattr(spec, "submodule_search_locations", None)
    filename = getattr(package, "__file__", None)
    if (package is None or sys.modules.get("evidence_wiki") is not package or spec is None
            or not isinstance(spec.loader, importlib.machinery.SourceFileLoader)
            or not filename or spec.origin != filename or not locations or len(locations) != 1):
        _fail("unsupported_package_origin")
    path = Path(filename)
    if not path.is_absolute() or path.name != "__init__.py" or str(path.parent) != locations[0]:
        _fail("inconsistent_package_origin")
    return path.parent


def initialize_package(package):
    """Record metadata before any lazy public owner can be imported."""
    global _PACKAGE, _PACKAGE_BASELINE, _UNAVAILABLE
    if _PACKAGE is not None:
        _UNAVAILABLE = "package_import_generation_replaced"
        return
    _PACKAGE = package
    try:
        _PACKAGE_BASELINE = observe_roots((("package/evidence_wiki", _package_root()),))
    except (OSError, ValueError, UnicodeError) as error:
        _UNAVAILABLE = str(error)


def _strict_pair():
    package = _package_root()
    assets = package / "assets"
    if os.path.lexists(assets):
        with _open_root(assets) as (descriptor, _chain):
            if set(os.listdir(descriptor)) != set(ASSET_NAMES):
                _fail("unsupported_assets_membership")
        return package, assets
    if package.name != "evidence_wiki" or package.parent.name != "src":
        _fail("unpaired_package_origin")
    project = package.parent.parent
    with _open_root(project) as (descriptor, _chain):
        info = os.stat("pyproject.toml", dir_fd=descriptor, follow_symlinks=False)
        _safe_type(info)
        if info.st_size > 65536:
            _fail("project_metadata_bound")
        held = os.open("pyproject.toml", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
        try:
            text = os.read(held, 65537).decode("utf-8")
            if (_observation(info) != _observation(os.fstat(held))
                    or _observation(info) != _observation(os.stat("pyproject.toml", dir_fd=descriptor, follow_symlinks=False))):
                _fail("project_metadata_changed")
        finally:
            os.close(held)
    # The supported source layout is deliberately exact and needs no TOML
    # dependency during early bootstrap (including Python 3.10).
    sections = re.split(r"(?m)^\[([^\]\n]+)\]\s*$", text)
    table = dict(zip(sections[1::2], sections[2::2], strict=True))
    if len(table) != len(sections[1::2]):
        _fail("ambiguous_project_metadata")
    project_section = table.get("project", "")
    if (len(re.findall(r"(?m)^name\s*=", project_section)) != 1
            or not re.search(r'(?m)^name\s*=\s*"evidence-wiki"\s*$', project_section)):
        _fail("unpaired_project_metadata")
    wheel_section = "\n".join(line for line in table.get("tool.hatch.build.targets.wheel", "").splitlines()
                              if line.strip() and not line.lstrip().startswith("#"))
    if re.fullmatch(r'packages\s*=\s*\[\s*"src/evidence_wiki"\s*\]\s*', wheel_section) is None:
        _fail("unsupported_package_mapping")
    expected = {name: "evidence_wiki/assets/" + name for name in ASSET_NAMES}
    force = table.get("tool.hatch.build.targets.wheel.force-include", "")
    force_lines = [line for line in force.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    force_rows = [re.fullmatch(r'"([^"\n]+)"\s*=\s*"([^"\n]+)"\s*', line) for line in force_lines]
    if any(row is None for row in force_rows):
        _fail("unsupported_asset_mapping")
    observed = dict(row.groups() for row in force_rows)
    if len(force_rows) != len(expected) or observed != expected:
        _fail("unsupported_asset_mapping")
    return package, project


def initialize_assets():
    """Resolve only the package-paired origin before legacy asset selection."""
    global _UNAVAILABLE
    if _UNAVAILABLE is None:
        try:
            note_assets_root(_strict_pair()[1])
        except (OSError, ValueError, UnicodeError) as error:
            _UNAVAILABLE = str(error)


def note_assets_root(selected):
    """Observe the strict paired assets once, before any native owner loads."""
    global _ASSET_BASELINE, _PAIR, _UNAVAILABLE
    if _UNAVAILABLE is not None:
        return
    if _PAIR is not None and Path(selected) == _PAIR[1]:
        return
    try:
        pair = _strict_pair()
        if Path(selected) != pair[1]:
            _fail("mixed_assets_origin")
        current_package = observe_roots((("package/evidence_wiki", pair[0]),))
        if _interval(current_package) != _interval(_PACKAGE_BASELINE):
            _fail("package_baseline_changed")
        if _PAIR is not None:
            if _PAIR != pair:
                _fail("assets_origin_changed")
            return
        _PAIR = pair
        _ASSET_BASELINE = observe_roots(tuple(("assets/" + name, pair[1] / name) for name in ASSET_NAMES))
    except (OSError, ValueError, UnicodeError) as error:
        _UNAVAILABLE = str(error)


def register_native(root, tree_hash, bridge, *, loader_hash=None):
    """Remember actual centralized loader generations, including cache hits."""
    from . import host_context

    global _UNAVAILABLE
    if bridge is not host_context or bridge is not sys.modules.get("evidence_wiki.host_context"):
        _fail("untrusted_native_bridge")
    if _UNAVAILABLE is not None:
        return
    expected = _PAIR[1] / "workspace-template/scripts" if _PAIR is not None else None
    if Path(root) != expected:
        note_assets_root(Path(root).parent.parent)
        return
    registry = _NATIVE_GENERATIONS if loader_hash is None else _LOADER_GENERATIONS
    generation = (Path(root), tree_hash if loader_hash is None else loader_hash, bridge)
    if registry and generation not in registry:
        _UNAVAILABLE = "native_load_generation_changed"
        return
    # Registration precedes loader construction. Retain the genuine bridge
    # itself, and pin actual loaded modules when an operation captures custody.
    registry.add(generation)


def inventory_document(raw):
    if type(raw) is not bytes or len(raw) > MAX_DOCUMENT_BYTES:
        _fail("inventory_byte_bound")

    def pairs(rows):
        result = {}
        for key, value in rows:
            if key in result:
                _fail("duplicate_inventory_key")
            result[key] = value
        return result

    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _value: _fail("nonfinite_inventory"))
    if (type(value) is not dict or set(value) != {"schema_version", "paths"}
            or value["schema_version"] != "evidence-review-producer-inventory/v1"
            or type(value["paths"]) is not list or not 1 <= len(value["paths"]) <= MAX_MEMBERS):
        _fail("inventory_shape")
    paths = [validate_path(path) for path in value["paths"]]
    if (paths != sorted(set(paths), key=lambda path: path.encode("utf-8")) or INVENTORY_PATH not in paths
            or canonical(value) != raw):
        _fail("inventory_canonical_membership")
    return value


@dataclass(frozen=True, eq=False)
class _Generation:
    producer_id: str
    legacy_producer_id: str
    script_generation: tuple
    _document_bytes: bytes
    _observations: tuple
    _origin: tuple
    _loader: object
    _modules: tuple

    def document(self):
        return json.loads(self._document_bytes)


_GENERATIONS = weakref.WeakSet()


def _native_modules(script_host, script_root, tree_hash, loader_hash):
    """Pin live cache objects without mistaking a numeric token for identity."""
    loader = script_host._LOADER_MODULE_CACHE.get(f"{script_root}\0{loader_hash}")
    path = script_root / "_workspace_module_loader.py"
    if (type(loader) is not ModuleType or Path(getattr(loader, "__file__", "")) != path
            or getattr(getattr(loader, "__spec__", None), "origin", None) != str(path)
            or not isinstance(getattr(loader.__spec__, "loader", None), importlib.machinery.SourceFileLoader)):
        _fail("native_loader_unavailable")
    modules = []
    for key, module in tuple(script_host._SCRIPT_MODULE_CACHE.items()):
        parts = key.split("\0")
        if not parts or parts[0] != str(script_root):
            continue
        if len(parts) != 4 or parts[2] != tree_hash:
            _fail("native_load_generation_changed")
        module_path = script_root / (parts[1] + ".py")
        if (type(module) is not ModuleType or Path(getattr(module, "__file__", "")) != module_path
                or getattr(getattr(module, "__spec__", None), "origin", None) != str(module_path)
                or not isinstance(getattr(module.__spec__, "loader", None), importlib.machinery.SourceFileLoader)):
            _fail("native_module_origin_changed")
        modules.append((key, module))
    return loader, tuple(sorted(modules, key=lambda row: row[0]))


def _capture():
    from . import _script_host, host_context

    selected = _script_host.shared_assets_root()
    note_assets_root(selected)
    if _UNAVAILABLE is not None or _PACKAGE_BASELINE is None or _ASSET_BASELINE is None:
        _fail(_UNAVAILABLE or "baseline_absent")
    if _strict_pair() != _PAIR:
        _fail("origin_changed")
    script_root, observed_tree = _script_host.owner_generation()
    roots = (("package/evidence_wiki", _PAIR[0]), *tuple(("assets/" + name, _PAIR[1] / name) for name in ASSET_NAMES))
    opening = observe_roots(roots, read=True)
    closing = observe_roots(roots)
    if _strict_pair() != _PAIR:
        _fail("origin_changed")
    if opening[:3] != closing[:3]:
        _fail("capture_changed")
    baseline_files = {**_PACKAGE_BASELINE[0], **_ASSET_BASELINE[0]}
    baseline_dirs = {**_PACKAGE_BASELINE[1], **_ASSET_BASELINE[1]}
    baseline_chains = {**_PACKAGE_BASELINE[2], **_ASSET_BASELINE[2]}
    if _interval(opening) != _interval((baseline_files, baseline_dirs, baseline_chains, {})):
        _fail("import_baseline_changed")
    data = opening[3]
    inventory = inventory_document(data[INVENTORY_PATH])
    if set(inventory["paths"]) != set(data):
        _fail("inventory_membership_changed")
    tree = hashlib.sha256()
    legacy_rows = []
    for path in sorted(data):
        prefix = "assets/workspace-template/scripts/"
        if path.startswith(prefix) and "/" not in path[len(prefix):] and path.endswith(".py"):
            name = path[len(prefix):]
            digest = hashlib.sha256(data[path]).digest()
            tree.update(name.encode("utf-8") + b"\0" + digest)
            legacy_rows.append({"path": name, "sha256": digest.hex()})
    fresh_tree = tree.hexdigest()
    expected_generation = (script_root, fresh_tree, host_context)
    loader_digest = hashlib.sha256(data["assets/workspace-template/scripts/_workspace_module_loader.py"]).hexdigest()
    if (script_root != _PAIR[1] / "workspace-template/scripts" or observed_tree != fresh_tree
            or _NATIVE_GENERATIONS != {expected_generation}
            or _LOADER_GENERATIONS != {(script_root, loader_digest, host_context)}
            or sys.modules.get("evidence_wiki.host_context") is not host_context):
        _fail("native_load_generation_changed")
    loader, modules = _native_modules(_script_host, script_root, fresh_tree, loader_digest)
    manifest = {"schema_version": "evidence-review-producer-manifest/v1", "members": [
        {"path": path, "size_bytes": len(data[path]), "sha256": "sha256:" + hashlib.sha256(data[path]).hexdigest()}
        for path in inventory["paths"]]}
    if len(canonical(manifest)) > MAX_DOCUMENT_BYTES:
        _fail("manifest_byte_bound")
    identity = {"schema_version": "evidence-review-producer/v1",
                "manifest_id": content_id("evidence-review-producer-manifest/v1", manifest)}
    legacy_preimage = canonical(legacy_rows)
    legacy_id = "sha256:" + hashlib.sha256(b"evidence-publication-producer/v1\0" + legacy_preimage).hexdigest()
    return _Generation(content_id("evidence-review-producer/v1", identity), legacy_id,
                       (script_root, fresh_tree), canonical({"identity": identity, "manifest": manifest}),
                       _interval(opening), _PAIR, loader, modules)


def capture():
    """Capture all actual owned bytes using only the bound installed origin."""
    try:
        generation = _capture()
    except (OSError, KeyError, UnicodeError, json.JSONDecodeError) as error:
        raise ProducerUnavailable("review_producer_unavailable: capture_failed") from error
    _GENERATIONS.add(generation)
    return generation


def assert_current(generation):
    """Close a genuine operation generation with a complete fresh capture."""
    if type(generation) is not _Generation or generation not in _GENERATIONS:
        _fail("unowned_generation")
    fresh = _capture()
    if (fresh._document_bytes != generation._document_bytes or fresh._observations != generation._observations
            or fresh.script_generation != generation.script_generation or fresh._origin != generation._origin
            or fresh._loader is not generation._loader):
        _fail("operation_generation_changed")
    current_modules = dict(fresh._modules)
    if any(current_modules.get(key) is not module for key, module in generation._modules):
        _fail("native_module_generation_changed")
