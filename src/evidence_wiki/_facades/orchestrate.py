"""``ws.orchestrate`` -- orchestration sessions and their work orders.

The hot path this whole library API was filed for: a long-lived host drives
``next``/``submit`` once per work order, and paying process-spawn cost per call
is what it wants to stop doing.

**It still spawns one subprocess per call, and that is deliberate.** Every
operation here runs the *deployed* controller at
``<workspace>/scripts/orchestration_controller.py`` through
:mod:`evidence_wiki.orchestration`'s protocol seams. The deployed controller is
authoritative for run-state mutation because it is version-matched to the
session state it owns; loading it in-process from this distribution's packaged
assets would let an installed library version mutate run state owned by a
different workspace version. That is the same reason managed orchestration is
absent from the MCP server. What this facade removes is the *CLI* process --
argument parsing, config discovery, and the Python interpreter start for
``evidence_wiki.cli`` -- not the controller boundary. Do not "optimize" the
subprocess away.

Errors arrive as the controller's own schema-1.0 envelopes and leave as the
typed exceptions in :mod:`evidence_wiki.errors`, so a host catches
``OrchestrationError`` or dispatches on ``error_code`` instead of scraping
stderr. Nothing untyped escapes: not
:class:`~evidence_wiki.orchestration.OrchestrationHostError`, not a workspace
script's refusal, and above all not ``SystemExit`` -- in an ASGI worker that
would terminate the process.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .. import orchestration
from ..errors import EvidenceWikiError, OrchestrationError, UsageError, error_from_envelope
from ._base import Namespace

# Two host-side codes with no counterpart in the workspace's own vocabulary,
# because the conditions they name happen on this side of the process boundary:
# the controller never got to emit an envelope, or it exited rather than
# returned. Both keep the ``ORCHESTRATION_`` prefix so
# :func:`~evidence_wiki.errors.error_class_for` places them in the same family a
# host is already catching, and both are reported with the child's own exit
# status where there was one.

#: ``error_code`` for a controller failure that carried no usable envelope.
HOST_ERROR_CODE = "ORCHESTRATION_HOST_FAILED"

#: ``error_code`` for a workspace script that exited instead of returning.
EXITED_ERROR_CODE = "ORCHESTRATION_HOST_EXITED"


def _is_refusal(exc: BaseException) -> bool:
    """Recognize a workspace script's refusal by *shape*, never by class.

    The workspace module loader isolates every sibling stem on each load,
    ``_script_errors`` included, so each loaded script gets its own
    ``ScriptRefusal`` class object. ``except ScriptRefusal`` on an imported one
    is therefore an arm that silently catches nothing while reading as though
    it handled the case. This mirrors ``_script_errors.is_refusal``, which is an
    asset rather than an importable module.
    """
    return callable(getattr(exc, "to_envelope", None)) and isinstance(getattr(exc, "error_code", None), str)


def _typed_error(operation: str, exc: BaseException) -> EvidenceWikiError:
    """Translate anything the controller path can raise into a typed error.

    Order matters. A host error that captured the controller's envelope keeps
    the controller's own stable code; only a failure with no envelope -- a
    controller that died before its error handler ran, or a host-side check
    that never reached the child -- falls back to
    :class:`~evidence_wiki.errors.OrchestrationError`.

    ``_redact`` is reapplied on the way out. Messages composed on the host side
    interpolate values that came from the child, and redaction is idempotent,
    so paying for it twice is cheaper than reasoning about which arm already
    did it.
    """
    if isinstance(exc, orchestration.OrchestrationHostError):
        if exc.envelope is not None:
            return error_from_envelope(exc.envelope)
        return OrchestrationError(
            HOST_ERROR_CODE,
            orchestration._redact(str(exc)),
            details={"operation": operation},
            exit_code=exc.exit_code,
        )
    if _is_refusal(exc):
        return error_from_envelope(exc.to_envelope())  # type: ignore[attr-defined]
    if isinstance(exc, SystemExit):
        return OrchestrationError(
            EXITED_ERROR_CODE,
            orchestration._redact(f"Workspace orchestration {operation} exited instead of returning: {exc.code!r}."),
            details={"operation": operation},
        )
    return OrchestrationError(
        HOST_ERROR_CODE,
        orchestration._redact(f"Workspace orchestration {operation} failed: {exc}"),
        details={"operation": operation},
    )


@contextlib.contextmanager
def _typed_errors(operation: str) -> Iterator[None]:
    """Run one controller operation, letting only typed errors out.

    ``SystemExit`` is named explicitly rather than reached through a blanket
    ``BaseException``: a library that also swallowed ``KeyboardInterrupt``
    would make a host unkillable mid-session, which is a worse failure than the
    one this guard exists to prevent.
    """
    try:
        yield
    except EvidenceWikiError:
        raise
    except (SystemExit, Exception) as exc:
        raise _typed_error(operation, exc) from exc


def _checked_wait_seconds(value: float | None) -> float | None:
    """Reject a wait the controller would reject, before spawning it to find out.

    Mirrors ``parse_wait_seconds`` in the deployed controller: zero is admitted
    and *is* the default meaning "do not wait", negatives are refused rather
    than clamped, and non-finite values are refused because ``inf`` turns a
    bounded wait into a hang while ``nan`` makes every deadline comparison
    false. The controller stays authoritative -- it re-validates whatever argv
    it receives, including from a caller that bypassed this facade -- so this is
    a faster, clearer refusal rather than the only one.

    ``nan`` is the case that earns the local check: ``_identifier_arguments``
    would render it as the literal string ``nan``, so without this the failure
    arrives as an argparse usage error from a subprocess rather than as a typed
    ``UsageError`` naming the argument.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UsageError(
            "VALUE_INVALID",
            f"driver_wait_seconds must be a number of seconds, got {type(value).__name__}.",
        )
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise UsageError(
            "VALUE_INVALID",
            f"driver_wait_seconds must be a non-negative, finite number of seconds, got {value!r}.",
            details={"driver_wait_seconds": repr(value)},
        )
    return number


@contextlib.contextmanager
def _result_file(result: dict[str, Any]) -> Iterator[Path]:
    """Materialize a result document for the controller to read, then remove it.

    Three properties are load-bearing:

    * **The descriptor is closed before the controller is spawned.** On Windows
      a file the parent still holds open cannot be reopened by the child, so the
      controller would fail to read the very result it was handed. That is why
      this is ``mkstemp`` plus an explicit close rather than
      ``NamedTemporaryFile(delete=True)``, whose whole design is to keep the
      handle open for the lifetime of the object.
    * **The file lives outside the workspace tree.** ``mkstemp`` defaults to the
      platform temporary directory; a scratch file under the workspace would be
      picked up by the controller's own integrity guards as workspace drift.
    * **It is unlinked on every path**, including the one where the controller
      refuses.
    """
    try:
        payload = json.dumps(result, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise UsageError(
            "VALUE_INVALID",
            f"Orchestration result is not JSON-serializable: {exc}.",
        ) from exc
    descriptor, name = tempfile.mkstemp(prefix="evidence-wiki-result-", suffix=".json")
    path = Path(name)
    try:
        # Closing the wrapper closes ``descriptor`` -- before anything below
        # this block can spawn the controller.
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
        yield path
    finally:
        with contextlib.suppress(OSError):
            path.unlink()


class OrchestrationSession:
    """One parent orchestration session, driven a work order at a time.

    Obtain one from :meth:`OrchestrateNamespace.start` or
    :meth:`OrchestrateNamespace.session`. The object is a name plus the handle
    it came from -- it holds no session state of its own, because the durable
    state lives in the workspace and a cached copy here would go stale the
    moment another driver touched the same session.
    """

    __slots__ = ("_namespace", "_orchestration_id")

    def __init__(self, namespace: OrchestrateNamespace, orchestration_id: str) -> None:
        self._namespace = namespace
        self._orchestration_id = orchestration_id

    @property
    def orchestration_id(self) -> str:
        """The durable id of the session this object drives."""
        return self._orchestration_id

    def __repr__(self) -> str:
        return f"<OrchestrationSession {self._orchestration_id} at {self._namespace._workspace.root}>"

    def next(
        self,
        *,
        agent_id: str | None = None,
        resume: bool = False,
        driver_wait_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Issue or replay the next work order.

        Returns the work order when the session can still make progress, and
        the session document when it cannot. **A session that has ended is an
        answer, not an error**: a terminal or paused session comes back as a
        result even though the controller signals it with a non-zero exit, so a
        host loops on the returned ``artifact_type`` rather than on an
        exception.

        A returned work order is validated by the same
        ``_validate_work_order`` the managed runner uses, so an embedding host
        gets exactly the guarantees the managed path gets -- bounded size, safe
        relative paths, no absolute paths, no environment credential values.

        ``driver_wait_seconds`` waits that long for a competing driver to
        release the session before refusing with ``ORCHESTRATION_DRIVER_BUSY``.
        Omitted, the controller's own default applies: refuse immediately. Waiting
        in the controller costs a blocked subprocess, so a host that already
        serializes its own callers should leave this alone and keep queueing in
        its own process, where it can apply its own fairness policy.

        Raises:
            UsageError: ``driver_wait_seconds`` is negative or not finite.
            OrchestrationError: the controller refused, or issued a work order
                for a different session.
            EvidenceWikiError: any other typed refusal the controller reported.
        """
        root = self._root()
        wait_seconds = _checked_wait_seconds(driver_wait_seconds)
        with _typed_errors("next"):
            payload = orchestration.protocol_next(
                root,
                self._orchestration_id,
                agent_id=agent_id,
                resume=resume,
                driver_wait_seconds=wait_seconds,
            )
            work_order = orchestration._work_order_from_next(payload)
            if work_order is None:
                return payload
            work_order = orchestration._validate_work_order(work_order)
            if work_order["orchestration_id"] != self._orchestration_id:
                # Raised as the managed runner raises it, wording included, and
                # translated on the way out by the surrounding guard.
                raise orchestration.OrchestrationHostError(
                    "Controller work order does not belong to the active orchestration."
                )
            return work_order

    def submit(
        self,
        action_id: str,
        result: dict[str, Any] | str | os.PathLike[str],
        *,
        agent_id: str | None = None,
        driver_wait_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Submit one structured agent result and return the updated session.

        ``result`` is either the result document itself -- written to a
        temporary file outside the workspace and removed again -- or a path to
        a file that already holds one.

        The document is a *claim*. The controller re-verifies the workspace
        artifacts it names and refuses a result whose postconditions the
        workspace does not actually satisfy, so passing a well-formed document
        is not a way to make a session progress.

        ``driver_wait_seconds`` behaves as it does on :meth:`next`, and is worth
        more here: ``next`` is idempotent, so a refused caller can just ask
        again, while a refused ``submit`` leaves the caller holding a result the
        session has not accepted.

        Raises:
            UsageError: ``result`` is neither a mapping nor an existing file, or
                ``driver_wait_seconds`` is negative or not finite.
            OrchestrationError: the controller refused the submission.
        """
        root = self._root()
        wait_seconds = _checked_wait_seconds(driver_wait_seconds)
        if isinstance(result, dict):
            # The guard is entered first so a temporary file that cannot even be
            # created -- a full or missing temp directory -- still leaves a typed
            # error rather than a bare ``OSError``.
            with _typed_errors("submit"), _result_file(result) as path:
                return orchestration.protocol_submit(
                    root,
                    self._orchestration_id,
                    action_id,
                    path,
                    agent_id=agent_id,
                    driver_wait_seconds=wait_seconds,
                )
        if isinstance(result, (str, os.PathLike)):
            path = Path(os.fspath(result))
            if not path.is_file():
                raise UsageError(
                    "VALUE_INVALID",
                    f"Orchestration result file does not exist: {path}",
                    details={"result_file": str(path)},
                )
            with _typed_errors("submit"):
                return orchestration.protocol_submit(
                    root,
                    self._orchestration_id,
                    action_id,
                    path,
                    agent_id=agent_id,
                    driver_wait_seconds=wait_seconds,
                )
        raise UsageError(
            "VALUE_INVALID",
            f"Orchestration result must be a mapping or a path to one, got {type(result).__name__}.",
        )

    def status(self) -> dict[str, Any]:
        """Read this session's current document.

        A pure read of durable state, so a terminal or paused session is
        reported here the same way an active one is.
        """
        root = self._root()
        with _typed_errors("status"):
            return orchestration.protocol_status(root, orchestration_id=self._orchestration_id)

    def _root(self) -> Path:
        """The workspace root, refusing once the owning handle is closed."""
        return self._namespace._workspace_root()


class OrchestrateNamespace(Namespace):
    """Orchestration operations for the owning workspace."""

    __slots__ = ()

    def start(
        self,
        agent_id: str,
        *,
        orchestration_id: str | None = None,
        max_actions: int | None = None,
        action_timeout_seconds: int | None = None,
        total_timeout_seconds: int | None = None,
        driver_wait_seconds: float | None = None,
    ) -> OrchestrationSession:
        """Create a parent orchestration session and return a driver for it.

        Every limit left as ``None`` is omitted, so the workspace's deployed
        controller applies its own default rather than this package pinning one
        that a newer workspace has moved on from. ``driver_wait_seconds``
        follows that rule too; it matters least here, since two ``start`` calls
        racing on one id have a durable refusal waiting for the loser either way
        (``ORCHESTRATION_EXISTS``).

        Raises:
            ConfigError: the handle is closed.
            UsageError: ``driver_wait_seconds`` is negative or not finite.
            OrchestrationError: the controller refused to create the session --
                for example ``ORCHESTRATION_EXISTS`` for a reused id.
        """
        root = self._workspace_root()
        wait_seconds = _checked_wait_seconds(driver_wait_seconds)
        with _typed_errors("start"):
            started = orchestration.protocol_start(
                root,
                agent_id,
                orchestration_id=orchestration_id,
                max_actions=max_actions,
                action_timeout_seconds=action_timeout_seconds,
                total_timeout_seconds=total_timeout_seconds,
                driver_wait_seconds=wait_seconds,
            )
            return OrchestrationSession(self, orchestration._session_id(started))

    def session(self, orchestration_id: str) -> OrchestrationSession:
        """Return a driver for an existing session, without touching it.

        Deliberately no I/O: naming a session is not reading one. A host that
        restarts and wants to resume should be able to reconstruct its drivers
        without a controller spawn per session, and the first real call reports
        an unknown id as ``ORCHESTRATION_UNKNOWN`` anyway.

        Raises:
            UsageError: ``orchestration_id`` is empty or not a string.
        """
        if not isinstance(orchestration_id, str) or not orchestration_id.strip():
            raise UsageError("VALUE_INVALID", "Orchestration id must be a non-empty string.")
        return OrchestrationSession(self, orchestration_id)

    def _workspace_root(self) -> Path:
        """The workspace root, refusing once the owning handle is closed."""
        self._workspace._check_open()
        return self._root
