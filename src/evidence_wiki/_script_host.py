"""Load packaged workspace scripts for every in-process caller.

The workspace tooling ships as package *assets* (``workspace-template/scripts``),
not as importable modules, so every caller has to load them through
``_workspace_module_loader.load_workspace_module``. Two callers now need that:
the argparse CLI in :mod:`evidence_wiki.cli` and the embeddable library API.
The API cannot import ``cli`` -- that would be circular once ``cli`` grows an
API-backed command, and ``cli`` is argparse-shaped rather than call-shaped --
so the loader and its caches live here, below both, and there is exactly one
module cache per interpreter instead of one per front end.
"""

from __future__ import annotations

import atexit
import contextlib
import hashlib
import importlib.util
import sys
import threading
from pathlib import Path
from types import ModuleType

from . import resources

_SCRIPT_MODULE_CACHE: dict[str, ModuleType] = {}
_LOADER_MODULE_CACHE: dict[str, ModuleType] = {}

_SHARED_ASSETS_LOCK = threading.Lock()
# The entered stack is held here as well as by the ``atexit`` registry, so the
# module that owns the assets root is also the module that owns its release.
_SHARED_ASSETS_STACK: contextlib.ExitStack | None = None
_SHARED_ASSETS_ROOT: Path | None = None


def _load_workspace_loader(script_dir: Path) -> ModuleType:
    root = script_dir.expanduser().resolve()
    path = root / "_workspace_module_loader.py"
    if not path.is_file():
        raise SystemExit(f"Missing packaged script loader: {path}")
    content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    key = f"{root}\0{content_hash}"
    if key in _LOADER_MODULE_CACHE:
        return _LOADER_MODULE_CACHE[key]
    module_name = f"_evidence_wiki_loader_{abs(hash(key))}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"Cannot load packaged script loader: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
    _LOADER_MODULE_CACHE[key] = module
    return module


def _load_script(script_path: Path, module_name: str) -> ModuleType:
    if not script_path.is_file():
        raise SystemExit(f"Missing packaged script: {script_path}")
    del module_name  # retained for the stable internal call signature
    loader = _load_workspace_loader(script_path.parent)
    return loader.load_workspace_module(script_path.parent, script_path.stem, cache=_SCRIPT_MODULE_CACHE)


def load_packaged_script(assets_root: Path, stem: str) -> ModuleType:
    """Load ``<assets_root>/workspace-template/scripts/<stem>.py``.

    Callers hold an assets root rather than a script path, so this spares each
    of them from rebuilding the same starter-relative path by hand.
    """
    script_path = Path(assets_root) / resources.STARTER_DIR / "scripts" / f"{stem}.py"
    return _load_script(script_path, f"evidence_wiki_{stem}")


def shared_assets_root() -> Path:
    """Return one process-wide assets root, entered once and released at exit.

    :func:`evidence_wiki.resources.assets_root` is a context manager because a
    zip or otherwise non-extracted install has to materialize the assets into a
    temporary directory that only survives inside the ``with`` block. For a
    normal on-disk install it just yields a stable path, so the CLI's habit of
    entering it per command costs nothing there.

    The library API is different: a host process holds many workspace handles
    at once. If each entered its own ``assets_root()``, a zip install would pay
    a full asset extraction per handle and every extraction would seed a
    disjoint family of cached script modules, since the module cache is keyed by
    the resolved script directory. Entering exactly once bounds both the
    extraction cost and the cache. The CLI deliberately keeps its per-command
    ``with assets_root() as root:`` entry; this function is for the API.
    """
    global _SHARED_ASSETS_STACK, _SHARED_ASSETS_ROOT

    with _SHARED_ASSETS_LOCK:
        if _SHARED_ASSETS_ROOT is None:
            stack = contextlib.ExitStack()
            try:
                root = stack.enter_context(resources.assets_root())
            except BaseException:
                # Leave the module state untouched so a later call can retry a
                # transient failure rather than inherit a half-entered stack.
                stack.close()
                raise
            atexit.register(stack.close)
            _SHARED_ASSETS_STACK = stack
            _SHARED_ASSETS_ROOT = root
        return _SHARED_ASSETS_ROOT
