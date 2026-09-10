"""``ws.normalize`` -- source normalization and intake shaping."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ._base import Namespace


class NormalizeNamespace(Namespace):
    """Normalization operations for the owning workspace."""

    def profiles(self) -> dict[str, Any]:
        """Discover supported inert intake profiles and their validation limits."""
        return self._call("qualified_packet", "profiles")

    def validate_packet(self, source_id: str) -> dict[str, Any]:
        """Validate original delivery bytes; invalid packets are report verdicts."""
        return self._call("qualified_packet", "validate_source", self._root, source_id)

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
