#!/usr/bin/env python3
"""Load one workspace script without leaking sibling modules across asset roots."""

from __future__ import annotations

import hashlib
import importlib.abc
import importlib.machinery
import importlib.util
import re
import sys
import threading
from collections.abc import MutableMapping
from pathlib import Path
from types import ModuleType

_STEM_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_IMPORT_LOCK = threading.RLock()
_MISSING = object()

# One validated content hash per script directory, remembered behind the stat
# signature it was computed under. The identity a cache key carries is still the
# content hash; the signature only decides whether that hash must be recomputed.
_TREE_HASH_LOCK = threading.Lock()
_TREE_HASH_MEMO: dict[Path, tuple[tuple[tuple[str, int, int, int, int], ...], str]] = {}


class _SourceLoader(importlib.machinery.SourceFileLoader):
    """Compile source bytes without consulting or altering timestamp-based bytecode."""

    def get_code(self, fullname: str):
        path = self.get_filename(fullname)
        return self.source_to_code(self.get_data(path), path)


class _SiblingFinder(importlib.abc.MetaPathFinder):
    def __init__(self, root: Path, names: set[str]) -> None:
        self.root = root
        self.names = names

    def find_spec(self, fullname: str, path=None, target=None):
        if path is not None or fullname not in self.names:
            return None
        source = self.root / f"{fullname}.py"
        return importlib.util.spec_from_file_location(fullname, source, loader=_SourceLoader(fullname, str(source)))


def _tree_signature(script_dir: Path) -> tuple[tuple[str, int, int, int, int], ...]:
    """Stat every sibling script: name, size, mtime, ctime, and inode.

    ``ctime`` is included because on POSIX it cannot be set from user space, so a
    rewrite that restores size and mtime still changes it; ``st_ino`` catches a
    file replaced by a new one. A change that preserves all four is not detected
    here -- that is the stated limit of a stat signature, not a content promise.
    """
    entries: list[tuple[str, int, int, int, int]] = []
    for child in sorted(script_dir.glob("*.py"), key=lambda item: item.name):
        try:
            stat = child.stat()
        except OSError:
            continue
        if not child.is_file():
            continue
        entries.append((child.name, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino))
    return tuple(entries)


def _content_tree_hash(script_dir: Path) -> str:
    digest = hashlib.sha256()
    for child in sorted(script_dir.glob("*.py"), key=lambda item: item.name):
        if not child.is_file():
            continue
        digest.update(child.name.encode("utf-8", errors="surrogatepass"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(child.read_bytes()).digest())
    return digest.hexdigest()


def _tree_hash(script_dir: Path) -> str:
    """Content hash of every sibling script, reread only when a stat signature moves.

    A warm load used to reread and rehash the whole script tree to prove nothing
    had changed. The signature is observed *before* and *after* hashing, and the
    hash is remembered only when the two agree, so a tree edited mid-hash is
    never remembered under a signature it does not match. One entry per
    directory bounds the memo; a changed tree replaces its predecessor.
    """
    before = _tree_signature(script_dir)
    with _TREE_HASH_LOCK:
        remembered = _TREE_HASH_MEMO.get(script_dir)
    if remembered is not None and remembered[0] == before:
        return remembered[1]
    tree_hash = _content_tree_hash(script_dir)
    after = _tree_signature(script_dir)
    if after == before:
        with _TREE_HASH_LOCK:
            _TREE_HASH_MEMO[script_dir] = (after, tree_hash)
    return tree_hash


def _identity(script_dir: Path, tree_hash: str) -> str:
    material = f"{script_dir}\0{tree_hash}".encode("utf-8", errors="surrogatepass")
    return hashlib.sha256(material).hexdigest()[:16]


def load_workspace_module(
    script_dir: Path,
    stem: str,
    *,
    cache: MutableMapping[str, ModuleType] | None = None,
) -> ModuleType:
    """Load ``<script_dir>/<stem>.py`` with target-root sibling isolation.

    Workspace scripts intentionally use plain sibling imports so copied
    workspaces remain executable. In a long-lived interpreter, however, Python's
    global ``sys.modules`` cache can otherwise resolve those imports from a
    previously loaded or already deleted workspace. This loader temporarily
    isolates every Python stem in the target script directory, then restores
    the caller's path and module table exactly.
    """

    if not isinstance(stem, str) or _STEM_RE.fullmatch(stem) is None:
        raise SystemExit(f"Invalid sibling workspace script name: {stem!r}")
    root = script_dir.expanduser().resolve()
    path = (root / f"{stem}.py").resolve()
    if path.parent != root or not path.is_file():
        raise SystemExit(f"Missing sibling workspace script: {path}")
    tree_hash = _tree_hash(root)
    cache_key = f"{root}\0{stem}\0{tree_hash}"
    if cache is not None and cache_key in cache:
        return cache[cache_key]

    sibling_names = {
        child.stem
        for child in root.glob("*.py")
        if child.is_file() and _STEM_RE.fullmatch(child.stem) is not None
    }
    unique_name = f"_evidence_wiki_{_identity(root, tree_hash)}_{stem}"
    names_to_restore = sibling_names | {unique_name}

    with _IMPORT_LOCK:
        original_path = list(sys.path)
        finder = _SiblingFinder(root, sibling_names)
        saved_modules = {name: sys.modules.get(name, _MISSING) for name in names_to_restore}
        try:
            for name in names_to_restore:
                sys.modules.pop(name, None)
            sys.path.insert(0, str(root))
            sys.meta_path.insert(0, finder)
            spec = importlib.util.spec_from_file_location(unique_name, path, loader=_SourceLoader(unique_name, str(path)))
            if spec is None or spec.loader is None:
                raise SystemExit(f"Cannot load sibling workspace script: {path}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[unique_name] = module
            spec.loader.exec_module(module)
        finally:
            if finder in sys.meta_path:
                sys.meta_path.remove(finder)
            for name in names_to_restore:
                sys.modules.pop(name, None)
            for name, previous in saved_modules.items():
                if previous is not _MISSING:
                    sys.modules[name] = previous
            sys.path[:] = original_path

    if cache is not None:
        # Callers may retain old module objects, but our cache owns only the latest
        # generation of a given target. Other roots and stems remain independent.
        prefix = f"{root}\0{stem}\0"
        for old_key in list(cache):
            if old_key.startswith(prefix):
                del cache[old_key]
        cache[cache_key] = module
    return module
