"""The shared base every facade namespace builds on, and the seam call it routes through.

Kept deliberately thin: it holds the owning handle and offers the three things
every namespace needs -- a way to load a packaged script, the workspace root to
pass that script as ``--project-root``, and :func:`call_seam`, the single place a
script's refusal becomes a typed :mod:`evidence_wiki.errors` exception.
Everything else belongs in the namespace modules themselves.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

from .._script_host import load_packaged_script, shared_assets_root
from ..errors import EvidenceWikiError, error_from_envelope

if TYPE_CHECKING:  # pragma: no cover - import cycle exists only for type checkers
    from ..workspace import Workspace

#: Stem of the packaged module that owns the error vocabulary.
_SCRIPT_ERRORS = "_script_errors"

#: Code for a ``SystemExit`` message the packaged classifier could not be asked about.
#: Matches ``_script_errors.classify_error_code``'s own fallback, so an
#: unreachable classifier degrades to the answer it would most likely have given.
UNCLASSIFIED_EXIT_CODE = "WORKSPACE_UNREADABLE"


def refusal_envelope(exc: BaseException) -> dict[str, Any] | None:
    """Return the error envelope of a script refusal, or ``None`` if ``exc`` is not one.

    Refusals are recognized **structurally**, never by ``isinstance``. The
    workspace module loader isolates every sibling stem on each load,
    ``_script_errors`` included, so each loaded script carries its *own*
    ``ScriptRefusal`` class object::

        cov = load_workspace_module(d, "coverage_manifest")
        exp = load_workspace_module(d, "export_answers")
        cov._script_errors.ScriptRefusal is exp.ScriptRefusal   # False

    A package-side ``except SomeImportedScriptRefusal`` would therefore compile,
    read as though it handled the case, and catch nothing at runtime. The shape
    tested here -- a callable ``to_envelope`` beside a string ``error_code`` --
    is the same pair ``_script_errors.is_refusal`` tests. That helper is a
    packaged *asset* rather than importable package code, so this mirrors its
    logic instead of importing it.
    """
    to_envelope = getattr(exc, "to_envelope", None)
    if not callable(to_envelope) or not isinstance(getattr(exc, "error_code", None), str):
        return None
    try:
        envelope = to_envelope()
    except Exception:  # noqa: BLE001 - a refusal whose rendering fails is still a refusal
        envelope = None
    if isinstance(envelope, dict):
        return envelope
    # The shape test above already identified this as a refusal; only its
    # rendering failed. Falling through to ``None`` here would hand the host an
    # opaque third-party exception for a condition the package *does* have a
    # code for, so the envelope is rebuilt from the attributes instead. The
    # error code survives, which is the part a caller branches on.
    return {
        "error_code": exc.error_code,
        "message": str(exc),
        "recoverable": getattr(exc, "recoverable", None),
        "remediation": getattr(exc, "remediation", None),
        "details": getattr(exc, "details", None),
    }


def _classified_exit(message: str) -> EvidenceWikiError:
    """Type a bare ``SystemExit(str)`` the way the CLI would have printed it.

    Several workspace scripts still funnel a fatal condition through
    ``SystemExit(str)``, and a few raise one *outside* the ``try`` their seam
    wraps -- ``question_resolve._run_command`` loads ``question_claim`` before it
    -- so such a message can reach here. Classification is delegated to the
    packaged ``_script_errors`` rather than reimplemented, which is what makes
    the code a host reads off the exception the same code the CLI writes into
    its stderr envelope for the identical failure.

    The classifier is loaded straight from the packaged assets rather than
    through the caller's loader. One of the conditions reaching here is *"that
    loader could not load a script"*, and classifying that failure by asking the
    same loader for another script would fail for the same reason -- turning a
    precise ``TOOLING_MISSING`` into an unclassified exit exactly when a host
    most needs to be told its installation is broken.
    """
    try:
        script_errors = load_packaged_script(shared_assets_root(), _SCRIPT_ERRORS)
        envelope = script_errors.error_envelope(script_errors.classify_error_code(message), message)
    except (Exception, SystemExit):  # noqa: BLE001 - the classifier is a convenience, not a precondition
        envelope = {"error_code": UNCLASSIFIED_EXIT_CODE, "message": message}
    return error_from_envelope(envelope)


def _translated(exc: BaseException) -> EvidenceWikiError | None:
    """Return the typed error for ``exc``, or ``None`` to let it propagate unchanged."""
    envelope = refusal_envelope(exc)
    if envelope is not None:
        return error_from_envelope(envelope)
    if isinstance(exc, SystemExit):
        # A library must never let SystemExit reach its caller: inside an ASGI
        # worker it terminates the process. A string code is a refusal funnel and
        # is typed above. A non-string code -- ``sys.exit(0)``, ``sys.exit(3)``,
        # ``sys.exit()`` -- is process control rather than a refusal about the
        # workspace, and is re-raised untouched. That is the same split
        # ``ScriptRefusal.from_system_exit`` makes, kept identical on purpose.
        return _classified_exit(exc.code) if isinstance(exc.code, str) else None
    return None


def call_seam(
    load_script: Callable[[str], ModuleType],
    stem: str,
    seam: str,
    /,
    *args: Any,
    **kwargs: Any,
) -> dict[str, Any]:
    """Load ``stem``, call its ``seam``, and translate whatever it refuses with.

    The one path every documented API operation takes. Loading is *inside* the
    guarded region deliberately: a missing or unloadable packaged script is
    itself reported as ``SystemExit(str)``, and a host must get that as a typed
    ``ConfigError`` too rather than as a process exit.

    Anything that is neither a refusal nor a ``SystemExit`` propagates unchanged
    -- a ``TypeError`` from a bad argument is a bug in the caller, not a
    workspace condition, and dressing it as an :class:`EvidenceWikiError` would
    hide it.
    """
    try:
        return getattr(load_script(stem), seam)(*args, **kwargs)
    except (Exception, SystemExit) as exc:
        translated = _translated(exc)
        if translated is None:
            raise
        raise translated from exc


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

    def _call(self, stem: str, seam: str, /, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Call one packaged seam through :func:`call_seam`.

        Every namespace method is one line of this. Routing through the owning
        handle's loader is what makes a closed handle's namespaces refuse, and
        routing through ``call_seam`` is what makes a refusal arrive typed.
        """
        return call_seam(self._script, stem, seam, *args, **kwargs)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self._workspace.root}>"
