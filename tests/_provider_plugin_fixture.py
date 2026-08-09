"""Install and uninstall the CR-5 fixture provider distributions by ``sys.path`` alone.

Registration is packaging metadata, so the only honest way to test it is to let real
``importlib.metadata`` discovery find a real ``.dist-info`` directory. This helper does
that without pip and without mutating the interpreter's site-packages: the fixture
distributions live under ``tests/fixtures/provider-plugins`` and are made discoverable by
putting their parent directory on ``sys.path``.

The design constraint is *restoration*, not installation. This helper runs inside a suite
of ~2400 tests that share one interpreter, so a leaked ``sys.path`` entry, a leaked
``sys.modules`` entry, or a stale ``importlib.metadata`` cache would surface as an
unrelated failure somewhere far away. Every install therefore records exactly what it
changed and the matching uninstall undoes exactly that — which also makes double-install
and nested (re-entrant) use safe, because an install that changed nothing removes nothing.

Usage::

    from tests._provider_plugin_fixture import installed_provider_plugins

    with installed_provider_plugins():
        ...                                   # keepa-fixture / keepa-search-fixture live

    with installed_provider_plugins("duplicate-id"):
        ...                                   # plus a rival distribution claiming the id

    with installed_provider_plugins("import-error", base=False):
        ...                                   # only the failing distribution

The loader's per-process cache (``_provider_plugins.clear_cache()``) is cleared on both
install and uninstall. The loader is a workspace script, not an importable package, and
each test loads it under its own module name, so it is found by scanning ``sys.modules``
for an already-imported copy rather than by guessing that name. A loader that was never
imported has no cache to clear.
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.metadata
import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parent
FIXTURE_ROOT = TESTS_ROOT / "fixtures" / "provider-plugins"
VARIANTS_ROOT = FIXTURE_ROOT / "variants"

ACQUISITION_ENTRY_POINT_GROUP = "evidence_wiki.acquisition_providers"
DISCOVERY_ENTRY_POINT_GROUP = "evidence_wiki.discovery_providers"

BASE_DISTRIBUTION_NAME = "keepa-fixture"
BASE_DISTRIBUTION_VERSION = "0.1.0"
BASE_MODULE_NAME = "keepa_fixture"
ACQUISITION_PROVIDER_ID = "keepa-fixture"
DISCOVERY_PROVIDER_ID = "keepa-search-fixture"

#: Variant name -> the directory that goes on ``sys.path`` to install it.
VARIANT_ROOTS: dict[str, Path] = {
    "duplicate-id": VARIANTS_ROOT / "duplicate-id",
    "reserved-id": VARIANTS_ROOT / "reserved-id",
    "invalid-declaration": VARIANTS_ROOT / "invalid-declaration",
    "import-error": VARIANTS_ROOT / "import-error",
}
VARIANT_NAMES = tuple(sorted(VARIANT_ROOTS))

_PROVIDER_PLUGINS_FILENAME = "_provider_plugins.py"


@dataclass(frozen=True)
class ProviderPluginInstall:
    """What one :func:`install_provider_plugins` call changed, and nothing more."""

    roots: tuple[Path, ...]
    added_paths: tuple[str, ...]
    preexisting_modules: frozenset[str]


def _resolve_roots(variants: tuple[str, ...], *, base: bool) -> tuple[Path, ...]:
    unknown = sorted({name for name in variants if name not in VARIANT_ROOTS})
    if unknown:
        raise ValueError(
            f"unknown provider plugin fixture variant(s): {', '.join(unknown)}. "
            f"Known variants: {', '.join(VARIANT_NAMES)}"
        )
    roots: list[Path] = [FIXTURE_ROOT] if base else []
    for name in variants:
        root = VARIANT_ROOTS[name]
        if root not in roots:
            roots.append(root)
    missing = [str(root) for root in roots if not root.is_dir()]
    if missing:
        raise FileNotFoundError(f"provider plugin fixture directory is missing: {', '.join(missing)}")
    return tuple(roots)


def _module_locations(module: object) -> Iterator[str]:
    origin = getattr(module, "__file__", None)
    if isinstance(origin, str):
        yield origin
    for entry in getattr(module, "__path__", ()) or ():
        if isinstance(entry, str):
            yield entry


def _fixture_module_names() -> frozenset[str]:
    """Return every ``sys.modules`` name currently backed by a file under the fixture root."""

    names: set[str] = set()
    for name, module in list(sys.modules.items()):
        if module is None:
            continue
        for location in _module_locations(module):
            try:
                resolved = Path(location).resolve()
            except (OSError, ValueError):  # pragma: no cover - defensive on exotic paths
                continue
            if resolved == FIXTURE_ROOT or FIXTURE_ROOT in resolved.parents:
                names.add(name)
                break
    return frozenset(names)


def refresh_provider_plugin_caches() -> None:
    """Drop every cache that could still remember the previous ``sys.path``."""

    importlib.invalidate_caches()
    # Python 3.12+ routes this through PathFinder.invalidate_caches(); 3.10/3.11 do not,
    # and the repository floor is 3.10, so call it directly as well.
    invalidate_metadata = getattr(importlib.metadata.MetadataPathFinder, "invalidate_caches", None)
    if callable(invalidate_metadata):
        with contextlib.suppress(Exception):
            invalidate_metadata()
    _clear_loader_cache()


def _clear_loader_cache() -> None:
    """Clear ``_provider_plugins``' registration cache in every already-imported copy."""

    for module in list(sys.modules.values()):
        if module is None:
            continue
        origin = getattr(module, "__file__", None)
        if not isinstance(origin, str) or os.path.basename(origin) != _PROVIDER_PLUGINS_FILENAME:
            continue
        clear_cache = getattr(module, "clear_cache", None)
        if callable(clear_cache):
            # Unit 2 is still in flight: a loader without the seam must not break callers.
            with contextlib.suppress(Exception):
                clear_cache()


def install_provider_plugins(*variants: str, base: bool = True) -> ProviderPluginInstall:
    """Put the requested fixture distributions on ``sys.path`` and return an undo handle.

    ``base`` installs the primary ``keepa-fixture`` distribution; each variant name adds a
    second distribution beside it. Installing a root that is already on ``sys.path`` is a
    no-op for that root, so repeated and nested installs stay balanced.
    """

    roots = _resolve_roots(variants, base=base)
    preexisting_modules = _fixture_module_names()
    added: list[str] = []
    for root in roots:
        entry = str(root)
        if entry not in sys.path:
            sys.path.insert(0, entry)
            added.append(entry)
    refresh_provider_plugin_caches()
    return ProviderPluginInstall(
        roots=roots,
        added_paths=tuple(added),
        preexisting_modules=preexisting_modules,
    )


def uninstall_provider_plugins(handle: ProviderPluginInstall) -> None:
    """Undo exactly what ``handle`` changed. Safe to call twice."""

    for name in sorted(_fixture_module_names() - handle.preexisting_modules):
        sys.modules.pop(name, None)
    for entry in handle.added_paths:
        # Exactly one occurrence per added entry: install skips roots already present, so
        # removing more would take away an entry somebody else owns.
        if entry in sys.path:
            sys.path.remove(entry)
        sys.path_importer_cache.pop(entry, None)
    refresh_provider_plugin_caches()


@contextlib.contextmanager
def installed_provider_plugins(*variants: str, base: bool = True) -> Iterator[ProviderPluginInstall]:
    """Install the fixture distributions for the duration of the block, then restore."""

    handle = install_provider_plugins(*variants, base=base)
    try:
        yield handle
    finally:
        uninstall_provider_plugins(handle)


def fixture_entry_points(group: str) -> tuple[importlib.metadata.EntryPoint, ...]:
    """Return the entry points currently visible in ``group``, in discovery order."""

    return tuple(importlib.metadata.entry_points(group=group))
