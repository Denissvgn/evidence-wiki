"""Vocabulary shared by seam-conformance case modules.

One module per wrapped script lives beside this file. The contract those modules
follow is documented in full in ``tests/test_seam_conformance.py``; this package
only holds the types they declare their cases with, so that adding a script means
adding one new file and touching nothing that another author owns.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

#: The CLI printed a document and the seam returned it.
SUCCESS = "success"
#: The CLI printed an error envelope and exited non-zero; the seam raised ScriptRefusal.
REFUSAL = "refusal"


@dataclass(frozen=True)
class SeamCase:
    """One operation, expressed twice: as CLI argv and as a call to the seam.

    ``argv`` is the complete argument list handed to the script as a subprocess,
    without the interpreter or the script path. It must name the workspace
    explicitly (the child does not run with the workspace as its cwd) and, for a
    ``SUCCESS`` case, must select ``--format json`` so stdout carries the document
    the seam returns.

    ``call`` receives the script module, loaded in-process, and must invoke the
    seam with inputs equivalent to ``argv`` -- returning the document, or letting
    the ``ScriptRefusal`` propagate.

    ``volatile`` names dotted paths of mapping keys whose values are wall-clock or
    otherwise per-invocation (``"generated_at"``, ``"run_controller.checked_at"``).
    Each path must be present on both sides and is then blanked before the two
    documents are compared, so a field the seam drops entirely is still caught.
    Declare a path only when it genuinely cannot agree between two invocations.
    """

    name: str
    argv: tuple[str, ...]
    call: Callable[[ModuleType], Any]
    expect: str = SUCCESS
    volatile: tuple[str, ...] = field(default=())
    note: str = ""
