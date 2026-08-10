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

# Serializes every workspace-script load in this process.
#
# Loading a workspace script temporarily inserts its directory into ``sys.path``
# and pops its siblings out of ``sys.modules``, then restores both. That is how
# a copied workspace keeps its plain sibling imports working, and both are
# process-global: two threads doing it at once can interleave one's insert with
# the other's restore, and the script being executed then fails to import
# ``_workspace_module_loader`` or ``_script_errors``, or runs against a module
# whose globals have already been torn down.
#
# The packaged loader does guard its critical section -- with an ``RLock`` that
# lives *on the loader module object*. That is not enough here, because sibling
# isolation gives almost every participant its own copy of that object:
#
# * two threads racing on the very first load each build their own loader module
#   and hold two different locks;
# * every script that goes through the loader gets its own copy of
#   ``_workspace_module_loader`` as well, so ``question_claim``'s lazy sibling
#   loads and ``workspace_status``'s lazy sibling loads are guarded by two
#   unrelated locks -- and those loads happen at *call* time, inside a seam.
#
# One process-wide reentrant lock covers all of it. ``_share_load_lock`` below is
# what reaches the per-script copies. Reentrant because loads nest: a script
# loaded under this lock loads its own siblings while it executes.
#
# This is a load-time lock only. A seam runs after its module is in hand, and
# sibling caches make a warm operation take no loads at all, so concurrent
# operations stay concurrent.
_LOAD_LOCK = threading.RLock()

#: Marks a ``load_workspace_module`` binding this module has already wrapped.
_SHARED_LOCK_MARKER = "_evidence_wiki_shared_load_lock"

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


def _share_load_lock(module: ModuleType) -> ModuleType:
    """Route a loaded script's own sibling loads through :data:`_LOAD_LOCK`.

    Every wrapped script imports the loader the same way -- ``from
    _workspace_module_loader import load_workspace_module`` -- and calls it
    through a local ``load_sibling_module`` helper *while a seam is running*,
    long after this module handed the script over. Because sibling isolation
    gives each script its own copy of the loader, those calls would otherwise be
    serialized by a lock nothing else holds.

    Replacing the module-level binding is what reaches them: the helper reads it
    as a global on every call, so one assignment covers every lazy sibling load
    the script will ever make. The wrapper is applied to each freshly loaded
    module in turn, so a sibling's own siblings are covered too, however deep the
    graph goes.

    Behaviour is otherwise untouched: the same loader is called with the same
    arguments and the same cache, so module identity, sibling isolation and the
    refusal-class-per-script property all stay exactly as they were. Idempotent,
    and a no-op for a module that does not use the loader at all.
    """
    original = module.__dict__.get("load_workspace_module")
    if not callable(original) or getattr(original, _SHARED_LOCK_MARKER, False):
        return module

    def load_workspace_module(*args: object, **kwargs: object) -> ModuleType:
        with _LOAD_LOCK:
            return _share_load_lock(original(*args, **kwargs))

    load_workspace_module.__doc__ = getattr(original, "__doc__", None)
    setattr(load_workspace_module, _SHARED_LOCK_MARKER, True)
    module.__dict__["load_workspace_module"] = load_workspace_module
    return module


def _load_script(script_path: Path, module_name: str) -> ModuleType:
    if not script_path.is_file():
        raise SystemExit(f"Missing packaged script: {script_path}")
    del module_name  # retained for the stable internal call signature
    with _LOAD_LOCK:
        loader = _load_workspace_loader(script_path.parent)
        module = loader.load_workspace_module(script_path.parent, script_path.stem, cache=_SCRIPT_MODULE_CACHE)
        return _share_load_lock(module)


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
