"""The shared base every facade namespace builds on.

Kept deliberately thin: it holds the owning handle and offers the two things
every namespace needs -- a way to load a packaged script, and the workspace
root to pass that script as ``--project-root``. Everything else belongs in the
namespace modules themselves.
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

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
