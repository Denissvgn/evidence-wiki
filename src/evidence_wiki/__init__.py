"""Installable EvidenceWiki package.

The embeddable API is re-exported here (``from evidence_wiki import
Workspace``) but resolved *lazily*, via the PEP 562 module ``__getattr__``.
Importing the package therefore stays as cheap as it was before the API
existed, which matters because ``evidence_wiki.cli`` imports this package on
every command: an eager ``from .workspace import Workspace`` would pull the
facade tree into every CLI startup to serve callers that never touch it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__version__ = "0.2.4"

if TYPE_CHECKING:  # pragma: no cover - for type checkers only; never executed
    # Spelled as redundant aliases: ``__all__`` is derived from the table below
    # rather than written out, so this is what tells a static checker (and ruff)
    # that these names are deliberate re-exports and not dead imports.
    from . import errors as errors
    from ._contract import contract as contract
    from ._facades.diagnostics import fleet_status as fleet_status
    from .workspace import Workspace as Workspace

# Attribute name -> (submodule to import, attribute on it, or ``None`` for the
# module itself). Keeping this a table rather than a chain of ``if`` branches
# means ``__all__`` and ``__dir__`` stay derivable from one source.
#
# Register a name here only once its module exists, so ``__all__`` stays true
# and ``from evidence_wiki import *`` cannot raise.
#
# ``contract`` resolves to ``._contract``, never a submodule spelled
# ``contract``: the import system binds a submodule onto its package the first
# time anything imports it, and ``cli`` does -- which would shadow the callable
# below with a module and make ``evidence_wiki.contract()`` fail by import
# order. The leading underscore keeps one meaning for the public name.
_LAZY_ATTRS: dict[str, tuple[str, str | None]] = {
    "Workspace": (".workspace", "Workspace"),
    "contract": ("._contract", "contract"),
    "errors": (".errors", None),
    # Module-level rather than a handle method: it aggregates across many
    # workspaces, so no single handle owns it.
    "fleet_status": ("._facades.diagnostics", "fleet_status"),
}

__all__ = ["__version__", *sorted(_LAZY_ATTRS)]


def __getattr__(name: str) -> Any:
    """Resolve a lazily re-exported API name on first access (PEP 562)."""
    target = _LAZY_ATTRS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    from importlib import import_module

    module = import_module(target[0], __name__)
    value = module if target[1] is None else getattr(module, target[1])
    # Cache on the module so later lookups skip ``__getattr__`` entirely.
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY_ATTRS))
