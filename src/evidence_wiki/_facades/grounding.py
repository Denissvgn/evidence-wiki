"""``ws.grounding`` -- quote containment and claim grounding.

One operation, ``verify``, with the same containment semantics the CLI path uses.
Add operations here rather than in a shared module: sibling units own sibling
namespaces.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ._base import Namespace


class GroundingNamespace(Namespace):
    """Grounding operations for the owning workspace."""

    def verify(
        self,
        slugs: Sequence[str],
        *,
        write: bool = False,
        verified_by: str | None = None,
    ) -> dict[str, Any]:
        """Verify that each claim's quote is contained in an accepted source record.

        Returns exactly the document ``verify_quotes.py --format json`` prints.
        ``write`` and ``verified_by`` mirror ``--write`` and ``--verified-by``, and
        the stamping they request happens here -- a host that verifies-and-stamps
        leaves the same audit trail on the question pages the CLI does.

        **A failed verification is not a refusal.** When the run completes and some
        claim does not verify, the report saying so is *returned*, exactly as the
        CLI prints it before exiting non-zero. Which claim failed, against which
        record, and what would fix it are the whole point of that document, and a
        host cannot read any of it off an exception. Only a genuine refusal raises.

        ``slugs`` is positional and a ``Sequence``, mirroring the seam. It must
        name at least one question: an empty sequence refuses with
        ``SLUG_INVALID``, exactly as omitting ``--slug`` does.

        Raises:
            GroundingError: a grounding block is malformed, or ``write`` was asked
                for on a report the verifier will not stamp.
            QuestionError: a named slug does not exist, or none was given.
            ConfigError: the workspace cannot be read, or the handle is closed.
        """
        return self._call("verify_quotes", "run_verify", self._root, slugs, write=write, verified_by=verified_by)
