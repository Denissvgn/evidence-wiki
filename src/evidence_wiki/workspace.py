"""The embeddable handle on an existing EvidenceWiki workspace.

A host process opens a workspace once and calls operations on the resulting
handle, instead of shelling out to ``evidence-wiki`` per operation. The handle
itself owns almost nothing: it validates a path, remembers it, and hands the
facade namespaces (``ws.coverage``, ``ws.grounding``, ...) a way to reach the
packaged workspace scripts. The operations themselves live in
:mod:`evidence_wiki._facades`.
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType, TracebackType
from typing import Any

from . import _script_host
from ._facades._base import call_seam
from ._facades.coverage import CoverageNamespace
from ._facades.diagnostics import DiagnosticsNamespace
from ._facades.grounding import GroundingNamespace
from ._facades.normalize import NormalizeNamespace
from ._facades.orchestrate import OrchestrateNamespace
from ._facades.questions import QuestionsNamespace
from .errors import ConfigError

#: Marker file that makes a directory a workspace rather than an ordinary directory.
WORKSPACE_MARKER = "research.yml"

#: Starter/system metadata file :meth:`Workspace.versions` reads.
WORKSPACE_SYSTEM_FILE = "workspace-system.yml"

#: Keys :meth:`Workspace.versions` reports out of ``workspace-system.yml``.
_WORKSPACE_VERSION_KEYS = (
    "starter_version",
    "schema_version",
    "compatible_research_yml_contract",
)


class Workspace:
    """An open handle on a workspace directory.

    Obtain one with :meth:`open`; the constructor is not part of the public
    surface. ``open`` validates and never creates, so a host that points at the
    wrong path gets a typed refusal rather than a stray directory tree.

    **No version gate.** ``open`` refuses only *structural* absence -- a path
    that is not a usable directory, or one without ``research.yml``. It
    deliberately does not compare the installed package's version against the
    workspace's deployed scripts. The CLI has no such gate, so adding one here
    would make the API refuse workspaces the CLI happily serves, and the
    workspace scripts already own their own compatibility checks. Version skew
    surfaces where it already surfaces: as a script's own typed refusal, at the
    call that actually depends on the incompatible piece. :meth:`versions` is
    the visibility counterpart -- it reports what is installed and what is
    deployed so a host can decide for itself, without the handle deciding for it.

    **Assets lifetime.** The handle borrows the process-wide assets root from
    :func:`evidence_wiki._script_host.shared_assets_root` on first use and owns
    no ``ExitStack`` of its own. For a zip or otherwise non-extracted install
    that root is a temporary directory materialized once and kept for the
    lifetime of the process. That is the point rather than a leak: per-handle
    ownership was rejected because N open handles would mean N full asset
    extractions and N disjoint families of cached script modules. Accordingly
    :meth:`close` invalidates *this* handle only -- later calls on it raise
    :class:`~evidence_wiki.errors.ConfigError` -- and never tears the shared
    root down, because sibling handles in the same process are still using it.
    """

    __slots__ = (
        "_root",
        "_closed",
        "coverage",
        "grounding",
        "questions",
        "normalize",
        "orchestrate",
        "diagnostics",
    )

    def __init__(self, root: Path) -> None:
        self._root = root
        self._closed = False
        self.coverage = CoverageNamespace(self)
        self.grounding = GroundingNamespace(self)
        self.questions = QuestionsNamespace(self)
        self.normalize = NormalizeNamespace(self)
        self.orchestrate = OrchestrateNamespace(self)
        self.diagnostics = DiagnosticsNamespace(self)

    # -- construction ---------------------------------------------------

    @classmethod
    def open(cls, path: str | Path) -> Workspace:
        """Return a handle on an existing workspace at ``path``.

        The path is user-expanded and resolved. It must be a directory
        containing ``research.yml``. Nothing is created, written, or repaired:
        a path that does not already hold a workspace is a refusal, not an
        invitation to initialize one. Use ``evidence-wiki init`` for that.

        Raises:
            ConfigError: ``WORKSPACE_UNREADABLE`` when ``path`` is not a usable
                directory, ``CONFIG_MISSING`` when it holds no ``research.yml``.
        """
        try:
            root = Path(path).expanduser().resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            # A resolvable-looking path can still explode: a symlink loop, a
            # path longer than the platform allows, an unset home directory for
            # ``~``. Those are all "this path is not usable", so report them as
            # such instead of letting an untyped OSError escape the API.
            raise ConfigError(
                "WORKSPACE_UNREADABLE",
                f"Workspace path cannot be resolved: {path} ({exc}).",
                details={"path": str(path)},
            ) from exc

        if not root.is_dir():
            raise ConfigError(
                "WORKSPACE_UNREADABLE",
                f"Workspace target is not a directory: {root}",
                details={"path": str(root)},
            )
        if not (root / WORKSPACE_MARKER).is_file():
            raise ConfigError(
                "CONFIG_MISSING",
                f"Workspace target does not contain {WORKSPACE_MARKER}: {root}",
                details={"path": str(root), "expected": WORKSPACE_MARKER},
            )
        return cls(root)

    # -- identity -------------------------------------------------------

    @property
    def root(self) -> Path:
        """The resolved workspace directory."""
        return self._root

    @property
    def closed(self) -> bool:
        """Whether :meth:`close` has been called on this handle."""
        return self._closed

    def __repr__(self) -> str:
        state = " (closed)" if self._closed else ""
        return f"<Workspace {self._root}{state}>"

    # -- versions -------------------------------------------------------

    def versions(self) -> dict[str, Any]:
        """Report the installed package version beside the workspace's own.

        Returns ``{"package": ..., "workspace": {"starter_version": ...,
        "schema_version": ..., "compatible_research_yml_contract": ...}}``.

        This is a pure read and never refuses *on the basis of what it reads*.
        Every workspace key degrades to ``None`` when ``workspace-system.yml``
        is absent, unreadable, not valid YAML, or shaped unexpectedly --
        reporting skew is the whole job here, so raising on the very file that
        would reveal it would defeat the purpose. A host that wants a hard
        failure compares the values itself.

        Handle lifetime is the one exception, and it is not about content: like
        every operation on the handle, this refuses once :meth:`close` has been
        called, because calling it then is a use-after-close bug in the host
        rather than a question about the workspace.

        Raises:
            ConfigError: ``WORKSPACE_UNREADABLE`` if the handle is closed.
        """
        self._check_open()
        return {
            "package": _package_version(),
            "workspace": self._workspace_versions(),
        }

    def _workspace_versions(self) -> dict[str, Any]:
        metadata = _read_workspace_system(self._root / WORKSPACE_SYSTEM_FILE)
        return {key: metadata.get(key) for key in _WORKSPACE_VERSION_KEYS}

    # -- workspace-scoped operations ------------------------------------
    #
    # These two are methods on the handle rather than namespace operations
    # because the published capability contract declares them as
    # ``workspace.export_answers`` and ``workspace.doctor``. That contract is
    # output a host negotiates against, so a declared name has to be a real one.

    def export_answers(self, status: list[str] | None = None) -> dict[str, Any]:
        """Export this workspace's answered questions.

        Returns exactly the document ``evidence-wiki export --format json``
        prints. ``status`` mirrors the repeatable ``--status`` filter; ``None``
        (the default) exports every status. The export is a pure read -- nothing
        under this call writes to the workspace.

        ``--format`` and ``--output`` have no counterpart: both choose how the
        document is rendered and where it is delivered, not what it says. A host
        that wants the ``jsonl`` bytes reshapes this document itself.

        Raises:
            EvidenceWikiError: whatever the export refuses on -- an unreadable
                workspace or a malformed ``research.yml`` arrive as
                :class:`~evidence_wiki.errors.ConfigError`.
        """
        return call_seam(self._script, "export_answers", "run_export", self._root, status=status)

    def doctor(self) -> dict[str, Any]:
        """Diagnose this workspace's runtime, tooling, and configuration.

        Returns exactly the report ``evidence-wiki doctor --format json`` prints.
        A broken workspace is diagnosed rather than refused: read
        ``report["verdict"]``. See
        :meth:`evidence_wiki._facades.diagnostics.DiagnosticsNamespace.doctor`,
        which this delegates to, for why the script's ``env`` injection point is
        not part of the public signature.
        """
        return self.diagnostics.doctor()

    # -- status ---------------------------------------------------------

    def status(
        self,
        *,
        no_cache: bool = False,
        questions_processed_this_run: int | None = None,
        source_requests_opened_this_run: int | None = None,
        releases_this_run: int | None = None,
        discovery_results_this_run: int | None = None,
        acquisition_downloads_this_run: int | None = None,
        github_archive_bytes_this_run: int | None = None,
        academic_provider_requests_this_run: int | None = None,
        web_downloads_this_run: int | None = None,
        manual_url_deliveries_this_run: int | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Return the workspace status document.

        Exactly what ``evidence-wiki status --format json`` prints, as a dict.
        Every keyword mirrors the CLI flag of the same name, one for one.

        ``--append-log`` has no counterpart, matching the seam: appending to
        ``log.md`` is something the CLI does *after* the document is produced,
        not part of producing it, so a host that wants a log entry writes one.

        Raises:
            ConfigError: the workspace cannot be read, or the handle is closed.
            RunError: ``run_id`` names a run this workspace does not have.
        """
        return call_seam(
            self._script,
            "workspace_status",
            "run_status_report",
            self._root,
            no_cache=no_cache,
            questions_processed_this_run=questions_processed_this_run,
            source_requests_opened_this_run=source_requests_opened_this_run,
            releases_this_run=releases_this_run,
            discovery_results_this_run=discovery_results_this_run,
            acquisition_downloads_this_run=acquisition_downloads_this_run,
            github_archive_bytes_this_run=github_archive_bytes_this_run,
            academic_provider_requests_this_run=academic_provider_requests_this_run,
            web_downloads_this_run=web_downloads_this_run,
            manual_url_deliveries_this_run=manual_url_deliveries_this_run,
            run_id=run_id,
        )

    # -- script access --------------------------------------------------

    def _script(self, stem: str) -> ModuleType:
        """Return the packaged workspace script module named ``stem``.

        The single accessor every facade namespace calls seam functions
        through. The module comes from the packaged assets that ship with the
        installed package -- deliberately not from ``<root>/scripts``, so that
        one installed version drives every open workspace and a host cannot be
        made to execute code out of a workspace directory.
        """
        self._check_open()
        return _script_host.load_packaged_script(_script_host.shared_assets_root(), stem)

    # -- lifetime -------------------------------------------------------

    def _check_open(self) -> None:
        """Raise if this handle has been closed."""
        if self._closed:
            raise ConfigError(
                "WORKSPACE_UNREADABLE",
                f"Workspace handle is closed: {self._root}",
                details={"path": str(self._root)},
            )

    def close(self) -> None:
        """Invalidate this handle. Idempotent.

        Every later *operation* on it raises
        :class:`~evidence_wiki.errors.ConfigError`. The inert identity
        accessors -- :attr:`root`, :attr:`closed`, ``repr()`` -- keep working,
        so a host can still log which handle it over-released.

        The shared assets root is left entered on purpose: it is process-wide,
        sibling handles may still be using it, and it is released at
        interpreter exit by :mod:`evidence_wiki._script_host`.
        """
        self._closed = True

    def __enter__(self) -> Workspace:
        self._check_open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def _package_version() -> str | None:
    """Return the installed package version, or ``None`` if it cannot be read."""
    from . import __version__

    return __version__ if isinstance(__version__, str) else None


def _read_workspace_system(path: Path) -> dict[str, Any]:
    """Return the ``workspace_system`` mapping from a metadata file, tolerantly.

    Returns an empty mapping for every failure mode -- missing file, unreadable
    file, invalid YAML, a document that is not a mapping, a ``workspace_system``
    key that is not a mapping. Callers then read ``None`` per key.

    The parse is guarded by a bare ``except Exception`` on purpose. This is a
    diagnostic read of a file that may have been hand-edited or truncated, and
    :meth:`Workspace.versions` promises never to refuse; a narrower guard would
    honour that promise for the common ``YAMLError`` and quietly break it for
    the rarer ones a malformed document can still provoke (``RecursionError``
    on pathological nesting, ``MemoryError`` on alias expansion). Reporting
    ``None`` is always a better answer here than raising.
    """
    import yaml

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    try:
        document = yaml.safe_load(text)
    except Exception:  # noqa: BLE001 - see the docstring; this read must never refuse
        return {}
    if not isinstance(document, dict):
        return {}
    workspace_system = document.get("workspace_system")
    if not isinstance(workspace_system, dict):
        return {}
    return workspace_system
