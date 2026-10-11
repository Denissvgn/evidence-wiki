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
import os
import stat
import sys
import threading
from collections import OrderedDict
from pathlib import Path
from types import CodeType, ModuleType

from . import host_context, resources

_SCRIPT_MODULE_CACHE: dict[str, ModuleType] = {}
_LOADER_MODULE_CACHE: dict[str, ModuleType] = {}
# The loader file's content hash, remembered behind the stat signature it was
# read under, so a warm load does not reread the loader to find its own cache key.
_LOADER_HASH_MEMO: dict[Path, tuple[tuple[int, int, int, int], str]] = {}

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
_COMPILED_SOURCE_LOCK = threading.RLock()
_COMPILED_SOURCE: OrderedDict[tuple[str, bytes, str | None, int], tuple[CodeType, int]] = OrderedDict()
_COMPILED_SOURCE_BYTES = 0
_COMPILED_SOURCE_MAX_ENTRIES = 512
_COMPILED_SOURCE_MAX_BYTES = 32 * 1024 * 1024
_SCRIPT_TREE_MEMO: OrderedDict[Path, tuple[tuple, tuple, str, int]] = OrderedDict()
_SCRIPT_TREE_MEMO_BYTES = 0
_SCRIPT_TREE_MEMO_MAX_ROOTS = 128
_SCRIPT_TREE_MEMO_MAX_BYTES = 2 * 1024 * 1024


def _script_tree_signature(root: Path) -> tuple:
    """Observe the loader's original name/size/time/inode signature freshly."""
    entries = []
    with os.scandir(root) as children:
        for child in children:
            if not child.name.endswith(".py"):
                continue
            try:
                observed = child.stat(follow_symlinks=True)
            except OSError:
                continue
            if not stat.S_ISREG(observed.st_mode):
                continue
            entries.append((child.name, observed.st_size, observed.st_mtime_ns,
                            observed.st_ctime_ns, observed.st_ino))
    return tuple(sorted(entries))


def _script_content_tree_hash(root: Path) -> str:
    value = hashlib.sha256()
    for child in sorted(root.glob("*.py"), key=lambda item: item.name):
        if not child.is_file():
            continue
        value.update(child.name.encode("utf-8", errors="surrogatepass"))
        value.update(b"\0")
        value.update(hashlib.sha256(child.read_bytes()).digest())
    return value.hexdigest()


def script_tree_hash(root: Path) -> str:
    """Share only an owner-computed code digest behind fresh original metadata.

    This retains the copied loader's stat-signature limitations. No source bytes,
    parsed configuration, permissions or current eligibility are remembered.
    The private memo bounds payload bytes and root count; larger trees still hash
    normally without being retained. No caller supplies computed cache material.
    """
    global _SCRIPT_TREE_MEMO_BYTES
    root = Path(root).expanduser().resolve()
    with _LOAD_LOCK:
        owner_code = (_script_tree_signature.__code__, _script_content_tree_hash.__code__)
        before = _script_tree_signature(root)
        previous = _SCRIPT_TREE_MEMO.get(root)
        if previous is not None and previous[0] == owner_code and previous[1] == before:
            _SCRIPT_TREE_MEMO.move_to_end(root)
            return previous[2]
        if previous is not None:
            _SCRIPT_TREE_MEMO_BYTES -= previous[3]
            del _SCRIPT_TREE_MEMO[root]
        digest = _script_content_tree_hash(root)
        after = _script_tree_signature(root)
        if after == before:
            size = (sys.getsizeof(before) + sum(sys.getsizeof(row) +
                    sum(sys.getsizeof(item) for item in row) for row in before) +
                    len(str(root).encode("utf-8", errors="surrogatepass")) + sys.getsizeof(digest))
            if size <= _SCRIPT_TREE_MEMO_MAX_BYTES:
                while _SCRIPT_TREE_MEMO and (len(_SCRIPT_TREE_MEMO) >= _SCRIPT_TREE_MEMO_MAX_ROOTS or
                        _SCRIPT_TREE_MEMO_BYTES + size > _SCRIPT_TREE_MEMO_MAX_BYTES):
                    _key, dropped = _SCRIPT_TREE_MEMO.popitem(last=False)
                    _SCRIPT_TREE_MEMO_BYTES -= dropped[3]
                _SCRIPT_TREE_MEMO[root] = (owner_code, after, digest, size)
                _SCRIPT_TREE_MEMO_BYTES += size
        return digest


def compiled_source_code(source: bytes, filename: str) -> CodeType:
    """Reuse compilation only for exact source bytes, filename and interpreter flags.

    The loader still reads and validates source and executes a fresh module.
    No module globals, context, evidence, permissions or eligibility are cached.
    """
    global _COMPILED_SOURCE_BYTES
    if type(source) is not bytes or type(filename) is not str:
        raise TypeError("Source compilation requires bytes and a filename")
    key = (filename, hashlib.sha256(source).digest(), sys.implementation.cache_tag, sys.flags.optimize)
    with _COMPILED_SOURCE_LOCK:
        found = _COMPILED_SOURCE.get(key)
        if found is not None:
            _COMPILED_SOURCE.move_to_end(key)
            return found[0]
        code = compile(source, filename, "exec", dont_inherit=True, optimize=sys.flags.optimize)
        if len(source) <= _COMPILED_SOURCE_MAX_BYTES:
            _COMPILED_SOURCE[key] = (code, len(source))
            _COMPILED_SOURCE_BYTES += len(source)
            while (len(_COMPILED_SOURCE) > _COMPILED_SOURCE_MAX_ENTRIES
                    or _COMPILED_SOURCE_BYTES > _COMPILED_SOURCE_MAX_BYTES):
                _discarded, (_, size) = _COMPILED_SOURCE.popitem(last=False)
                _COMPILED_SOURCE_BYTES -= size
        return code

#: Marks a ``load_workspace_module`` binding this module has already wrapped.
_SHARED_LOCK_MARKER = "_evidence_wiki_shared_load_lock"

_SHARED_ASSETS_LOCK = threading.Lock()
# The entered stack is held here as well as by the ``atexit`` registry, so the
# module that owns the assets root is also the module that owns its release.
_SHARED_ASSETS_STACK: contextlib.ExitStack | None = None
_SHARED_ASSETS_ROOT: Path | None = None


def _loader_signature(path: Path) -> tuple[int, int, int, int]:
    stat = path.stat()
    return (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino)


def _loader_content_hash(path: Path) -> str:
    """Hash the loader file, rereading it only when its stat signature moves.

    Observed before and after the read, remembered only when both agree, for the
    same reason the loader's own tree hash is: a file edited mid-read must never be
    remembered under a signature it does not match.
    """
    before = _loader_signature(path)
    remembered = _LOADER_HASH_MEMO.get(path)
    if remembered is not None and remembered[0] == before:
        return remembered[1]
    content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    if _loader_signature(path) == before:
        _LOADER_HASH_MEMO[path] = (before, content_hash)
    return content_hash


def _load_workspace_loader(script_dir: Path) -> ModuleType:
    root = script_dir.expanduser().resolve()
    path = root / "_workspace_module_loader.py"
    if not path.is_file():
        raise SystemExit(f"Missing packaged script loader: {path}")
    content_hash = _loader_content_hash(path)
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
        # Timestamp-based pyc validation is weaker than this loader's content
        # identity. Compile the actual loader bytes even when an old pyc exists.
        exec(compiled_source_code(path.read_bytes(), str(path)), module.__dict__)  # noqa: S102 -- trusted packaged code
    finally:
        sys.modules.pop(module_name, None)
    for old_key in list(_LOADER_MODULE_CACHE):
        if old_key.startswith(f"{root}\0"):
            del _LOADER_MODULE_CACHE[old_key]
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
            kwargs["host_context_bridge"] = host_context
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
        module = loader.load_workspace_module(script_path.parent, script_path.stem, cache=_SCRIPT_MODULE_CACHE,
                                            host_context_bridge=host_context)
        return _share_load_lock(module)


def owner_generation() -> tuple[Path, str]:
    """Select the exact owner tree used by packaged in-process calls."""
    root = shared_assets_root() / resources.STARTER_DIR / "scripts"
    with _LOAD_LOCK:
        _load_workspace_loader(root)
        return root.resolve(), script_tree_hash(root)


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
    extraction cost and the cache. The CLI's API-backed commands reach this same
    root, so the module the shell renders is the module whose seam produced the
    document; only the commands that are not operations on an open handle
    (``init``/``deploy``, ``upgrade``, ``pack validate``, ``pack refresh``,
    ``pack adopt``, ``serve-mcp``, ``orchestrator-guide``) still keep their own per-command
    ``with assets_root() as root:`` entry.
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
