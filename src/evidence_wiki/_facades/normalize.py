"""``ws.normalize`` -- source normalization and intake shaping.

Add methods here rather than in a shared module: parallel work units fill
sibling namespaces, and one shared module would put them all in each other's
diff.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ._base import Namespace


class NormalizeNamespace(Namespace):
    """Normalization operations for the owning workspace."""

    def verify(self, source_ids: Sequence[str] | None = None) -> dict[str, Any]:
        """Check normalized records against the published record contract.

        Returns exactly the report ``evidence-wiki normalize verify --format json``
        prints. ``source_ids`` mirrors the repeatable ``--source-id`` option;
        ``None`` (the default) is ``--all``.

        **A failed verification is a return value, not an exception.** A record
        that breaches the contract comes back as ``overall_result:
        "not_verified"`` with the per-record violation list that is the entire
        point of asking -- the CLI prints that same report and exits non-zero,
        which is a verdict rather than an error. Only a workspace the verifier
        cannot read at all refuses.

        Raises:
            EvidenceWikiError: an unknown ``source_id``, or a manifest or
                ``research.yml`` that cannot be loaded. ``SourceError`` and
                ``ConfigError`` are the families to expect.
        """
        return self._call("normalize_verify", "run_verify", self._root, source_ids=source_ids)
