"""One module object per workspace script for every test that loads one through here.

Every test module used to carry its own ``importlib.util.spec_from_file_location``
helper, so two test modules that loaded the same script under two different names
held two unrelated module objects. A patch applied to one was invisible to work
driven through the other, and the failure is silent rather than loud: a memoisation
test in this suite once observed *zero* calls to the collaborator it had patched,
because the submit it drove ran against the other copy. Only an exact-count
assertion turned that into a failure instead of a pass.

The loaders here close that off by keying the cache on the **resolved script path**.
Names are what differ across this corpus -- almost every test invents its own -- so
a name-keyed cache would have handed back the same separate copies it was meant to
merge. The path key is what makes ``load_module("a_under_test", p)`` and
``load_module("b_under_test", p)`` the same object.

Two kinds of caller must *not* share, and both are served explicitly:

* Tests that re-execute a script on purpose, so each case starts against clean
  module-level state -- a secret-redaction registry, a sibling-module cache, a
  transport hook assigned directly into the module's globals. They call
  :func:`load_module_uncached` or :func:`load_script_uncached`, and the call site
  says why.
* Scripts that do not live in this repository. A test that initialises a workspace
  into a temporary directory loads the *copy* under that directory. Those paths are
  per-test artifacts: they are removed when the case ends, and the operating system
  is free to hand the same name back for an unrelated workspace, so a path does not
  identify them. The cached entry points fall through to a fresh load for any path
  outside the repository tree -- which is also exactly what those call sites did
  before -- and the cache stays bounded by the number of scripts in the tree.

``sys.modules`` handling is deliberately unchanged from the helpers this replaces: a
module is registered under the caller's chosen name before it executes, and left
registered afterwards, so a script that looks itself up while executing still finds
itself. A cache *hit* registers nothing. The module already executed under the first
name that asked for it, and nothing in this suite reaches these private names
through ``sys.modules`` -- no import, no string-target ``mock.patch`` -- so binding
further aliases would add entries with no reader.

Sibling-isolated loading is a different regime and keeps its own cache:
:func:`load_isolated_module` goes through the packaged
``_workspace_module_loader``, which gives each script its own copy of every sibling
stem. A script loaded that way is intentionally not the same object as the same
script loaded directly, because the isolation boundary is the thing those callers
are exercising.

One boundary is outside this module's reach, and the guarantee stops at it. Workspace
scripts import some of their helpers as plain siblings (``from _provider_registry
import ...``), so a directly loaded script pulls those in under their bare stems and
leaves them in ``sys.modules``. A helper reached that way is a different object from
the one this module hands back for the same file, and no cache key here can merge
them: unifying them would mean overriding how the scripts import each other. Tests
that need to patch a helper *as the script sees it* must therefore reach it through
the script -- ``CONTROLLER.load_sibling_module("source_inventory")`` -- rather than
loading a second copy of it here.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"

#: Reentrant because executing a script can load further scripts on the same thread.
_LOAD_LOCK = threading.RLock()

#: Resolved script path -> the one module object this suite uses for it.
_MODULE_CACHE: dict[Path, ModuleType] = {}

#: Handed to the packaged loader, which keys it by asset root, stem and tree hash.
_ISOLATED_CACHE: dict[str, ModuleType] = {}

#: Resolved ``_workspace_module_loader.py`` path -> that loader module.
_LOADER_CACHE: dict[Path, ModuleType] = {}

#: The packaged loader's filename stem, and the name under which a workspace script
#: imports it as a plain sibling. Never the name it is *executed* under here --
#: :data:`_PACKAGED_LOADER_NAME` is, so this suite never shadows that import.
_WORKSPACE_LOADER_STEM = "_workspace_module_loader"
_PACKAGED_LOADER_NAME = f"evidence_wiki_test_{_WORKSPACE_LOADER_STEM}"

_MISSING = object()


def _execute(name: str, path: Path) -> ModuleType:
    """Execute ``path`` as a module named ``name`` and return it."""
    if not path.is_file():
        raise AssertionError(f"missing workspace script: {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load script from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _in_repository(path: Path) -> bool:
    """Is ``path`` a checked-in script rather than a per-test copy of one?"""
    return path.resolve().is_relative_to(REPO_ROOT)


def load_module(name: str, path: str | Path) -> ModuleType:
    """Return the single module object this suite uses for ``path``.

    The first caller executes the script; every later caller that asks *here* for
    the same path -- under any name -- gets that same object, so one patch covers
    all of them.
    """
    script = Path(path)
    if not _in_repository(script):
        return _execute(name, script)
    key = script.resolve()
    with _LOAD_LOCK:
        cached = _MODULE_CACHE.get(key)
        if cached is not None:
            return cached
        # Executed from ``key``, not from ``script``: the cached identity and the
        # module's own ``__file__`` then agree, so a script that derives paths from
        # ``__file__`` reads the same spelling this cache keyed it under.
        module = _execute(name, key)
        _MODULE_CACHE[key] = module
        return module


def load_module_uncached(name: str, path: str | Path) -> ModuleType:
    """Execute ``path`` again and return a module object nothing else holds.

    For tests whose subject is module-level state: re-executing is how they get a
    clean registry, cache or hook table per case, and sharing would quietly turn
    that into state carried between cases.
    """
    return _execute(name, Path(path))


def load_script(name: str, filename: str) -> ModuleType:
    """:func:`load_module` for a script named relative to ``workspace-template``."""
    return load_module(name, SCRIPTS / filename)


def load_script_uncached(name: str, filename: str) -> ModuleType:
    """:func:`load_module_uncached` for a ``workspace-template`` script."""
    return load_module_uncached(name, SCRIPTS / filename)


def _execute_unregistered(name: str, path: Path) -> ModuleType:
    """Execute ``path`` without leaving ``name`` bound in ``sys.modules``.

    The binding still exists while the module runs, for anything that looks itself
    up during execution, and ``sys.modules`` is restored to exactly what it held
    before -- including a name that was already bound to something else.
    """
    previous = sys.modules.get(name, _MISSING)
    try:
        return _execute(name, path)
    finally:
        if previous is _MISSING:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous


def _packaged_loader(loader_path: Path) -> ModuleType:
    """The ``_workspace_module_loader`` that ships beside a script directory.

    Executed under a private name and unregistered again. Binding the canonical
    ``_workspace_module_loader`` would shadow the plain sibling import that every
    directly loaded script makes for itself, and for a workspace under a temporary
    directory it would leave a module from a deleted tree bound -- with its own
    import lock -- for the rest of the process.
    """
    if not _in_repository(loader_path):
        return _execute_unregistered(_PACKAGED_LOADER_NAME, loader_path)
    key = loader_path.resolve()
    with _LOAD_LOCK:
        cached = _LOADER_CACHE.get(key)
        if cached is not None:
            return cached
        module = _execute_unregistered(_PACKAGED_LOADER_NAME, loader_path)
        _LOADER_CACHE[key] = module
        return module


def load_isolated_module(name: str, path: str | Path) -> ModuleType:
    """Load ``path`` the way the installed package loads a workspace script.

    A script directory that ships ``_workspace_module_loader.py`` is read through
    it, so the script gets the per-load sibling isolation the product gives it --
    including its own ``_script_errors``, and therefore its own refusal classes.
    Directories without one fall back to a direct load.
    """
    script = Path(path)
    loader_path = script.parent / f"{_WORKSPACE_LOADER_STEM}.py"
    if script == loader_path or not loader_path.is_file():
        return load_module(name, script)
    loader = _packaged_loader(loader_path)
    if not _in_repository(script):
        return loader.load_workspace_module(script.parent, script.stem)
    with _LOAD_LOCK:
        return loader.load_workspace_module(script.parent, script.stem, cache=_ISOLATED_CACHE)
