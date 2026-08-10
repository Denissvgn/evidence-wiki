"""The shared base every facade namespace builds on.

Kept deliberately thin: it holds the owning handle and offers the three things
every namespace needs -- a way to load a packaged script, the workspace root to
pass that script as ``--project-root``, and :func:`translated_refusals`, which
turns a workspace script's refusal into a typed
:class:`~evidence_wiki.errors.EvidenceWikiError`. Everything else belongs in the
namespace modules themselves.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

from .._script_host import load_packaged_script, shared_assets_root
from ..errors import EvidenceWikiError, error_from_envelope

if TYPE_CHECKING:  # pragma: no cover - import cycle exists only for type checkers
    from ..workspace import Workspace


class Namespace:
    """Base class for a group of workspace operations hung off a handle."""

    __slots__ = ("_workspace",)

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace

    @property
    def _root(self) -> Path:
        """The owning handle's workspace root."""
        return self._workspace.root

    def _script(self, stem: str) -> ModuleType:
        """Load a packaged workspace script through the owning handle.

        Routing through the handle rather than
        :mod:`evidence_wiki._script_host` directly is what makes a closed
        handle's namespaces refuse too: the handle checks its own validity here.
        """
        return self._workspace._script(stem)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self._workspace.root}>"


# -- refusal translation --------------------------------------------------
#
# Every library-API method funnels its seam call through
# ``with translated_refusals():``. Two things must happen there and nowhere else,
# so that no facade can get either of them subtly wrong on its own:
#
# 1. A workspace script's refusal becomes a typed ``EvidenceWikiError``.
# 2. No ``SystemExit`` escapes into the host. A library that lets one through
#    terminates an ASGI worker's process; only a ``SystemExit`` that is genuine
#    process control (a non-string ``code``) is allowed past, untouched.


@contextmanager
def translated_refusals() -> Iterator[None]:
    """Convert whatever a workspace script raises into the library's error type.

    Wrap the script *load* as well as the seam call: ``_script_host`` reports a
    missing or unloadable packaged script by raising ``SystemExit(str)``, which
    is exactly the thing a host must never receive.

    Anything that is neither a refusal nor a message-carrying ``SystemExit``
    propagates unchanged -- a ``KeyError`` from a script is a bug, and dressing
    it up as a workspace error would hide it.
    """
    try:
        yield
    except SystemExit as exc:
        # ``ExportRefusal(ScriptRefusal, SystemExit)`` is dual-inherited and so
        # lands in *this* arm rather than the one below. Test the refusal shape
        # first, or its code and details would be thrown away and reconstructed
        # from the message.
        error = _refusal_error(exc)
        if error is None:
            # Re-raises ``exc`` untouched when its code is not a string.
            error = _system_exit_error(exc)
        raise error from exc
    except Exception as exc:
        error = _refusal_error(exc)
        if error is None:
            raise
        raise error from exc


def _refusal_error(exc: BaseException) -> EvidenceWikiError | None:
    """Return the typed error for a script refusal, or ``None`` if this is not one.

    **Refusals are recognized by shape, never by class identity.**
    ``_workspace_module_loader`` isolates every sibling stem on each load,
    ``_script_errors`` included, so each loaded workspace script gets its *own*
    ``ScriptRefusal`` class object::

        cov = load_workspace_module(d, "coverage_manifest")
        exp = load_workspace_module(d, "export_answers")
        cov._script_errors.ScriptRefusal is exp.ScriptRefusal   # False

    Package code therefore cannot write ``except ScriptRefusal``: whichever class
    object it imported is not the one the script raised, so the catch would
    compile, read as though it handled the case, and catch nothing at runtime.
    The shape tested here is the same one ``_script_errors.is_refusal`` tests and
    ``emit_refusal`` reads -- a callable ``to_envelope()`` and a string
    ``error_code`` -- so it holds for refusals from any script, including ones
    this version has never seen.
    """
    to_envelope = getattr(exc, "to_envelope", None)
    error_code = getattr(exc, "error_code", None)
    if not callable(to_envelope) or not isinstance(error_code, str):
        return None

    try:
        envelope = to_envelope()
    except Exception:  # noqa: BLE001 - a refusal that cannot render is still a refusal
        envelope = None
    if not isinstance(envelope, dict):
        # Degrade to the two fields the shape check already proved are there,
        # rather than losing the diagnosis to a second failure.
        envelope = {"error_code": error_code, "message": str(exc)}

    # ``error_envelope`` carries no exit status, but the refusal does, and it is
    # the status the CLI would have exited with for this same call. Preserve it
    # so an in-process caller and a subprocess caller agree.
    exit_code = getattr(exc, "exit_code", None)
    if isinstance(exit_code, int) and not isinstance(exit_code, bool) and "exit_code" not in envelope:
        envelope = {**envelope, "exit_code": exit_code}
    return error_from_envelope(envelope)


def _system_exit_error(exc: SystemExit) -> EvidenceWikiError:
    """Convert a message-carrying ``SystemExit`` into a typed error.

    A ``SystemExit`` whose ``code`` is not a string is process control rather
    than a refusal -- ``sys.exit(0)``, ``sys.exit(2)`` -- and is re-raised
    untouched, the same judgement ``_script_errors.handle_system_exit`` makes.
    """
    if not isinstance(exc.code, str):
        raise exc
    return error_from_envelope(_system_exit_envelope(exc.code))


def _system_exit_envelope(message: str) -> dict[str, Any]:
    """Build the envelope the CLI would have printed for this ``SystemExit`` message.

    Classification lives in the packaged ``_script_errors`` asset, so it is asked
    rather than reimplemented: an in-process caller then reads the same
    ``error_code`` and remediation a subprocess caller would have read off stderr.
    The fallback covers the case where the assets are what is broken -- the
    classifier is unreachable exactly when a ``TOOLING_MISSING``-shaped
    ``SystemExit`` is most likely, and a second failure there would bury the
    first. ``SystemExit`` is named alongside ``Exception`` because
    ``load_packaged_script`` reports a missing script by raising one.
    """
    try:
        script_errors = load_packaged_script(shared_assets_root(), "_script_errors")
        return script_errors.error_envelope(script_errors.classify_error_code(message), message)
    except (Exception, SystemExit):  # noqa: BLE001 - see the docstring
        return {"error_code": "WORKSPACE_UNREADABLE", "message": message}
