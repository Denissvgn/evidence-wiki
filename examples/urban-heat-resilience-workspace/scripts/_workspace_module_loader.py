#!/usr/bin/env python3
"""Load one workspace script without leaking sibling modules across asset roots."""

from __future__ import annotations

import hashlib
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


def _tree_hash(script_dir: Path) -> str:
    digest = hashlib.sha256()
    for child in sorted(script_dir.glob("*.py"), key=lambda item: item.name):
        if not child.is_file():
            continue
        digest.update(child.name.encode("utf-8", errors="surrogatepass"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(child.read_bytes()).digest())
    return digest.hexdigest()


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
        saved_modules = {name: sys.modules.get(name, _MISSING) for name in names_to_restore}
        try:
            for name in names_to_restore:
                sys.modules.pop(name, None)
            sys.path.insert(0, str(root))
            spec = importlib.util.spec_from_file_location(unique_name, path)
            if spec is None or spec.loader is None:
                raise SystemExit(f"Cannot load sibling workspace script: {path}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[unique_name] = module
            spec.loader.exec_module(module)
        finally:
            for name in names_to_restore:
                sys.modules.pop(name, None)
            for name, previous in saved_modules.items():
                if previous is not _MISSING:
                    sys.modules[name] = previous
            sys.path[:] = original_path

    if cache is not None:
        cache[cache_key] = module
    return module
