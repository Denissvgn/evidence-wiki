"""Host-owned permissions, exact sanitized revisions, and lineage."""

from __future__ import annotations

from typing import Any

from ._base import Namespace


class UsageNamespace(Namespace):
    def status(self, *, request_id: str | None = None) -> dict[str, Any]:
        """Read the current checkpoint or reconcile a prior transaction receipt."""
        return self._call("evidence_usage", "run_status", self._root, request_id=request_id)

    def transact(self, command: dict[str, Any], *, artifacts: dict[str, bytes] | None = None) -> dict[str, Any]:
        """Apply a host-signed command under a private, external state lock."""
        return self._call("evidence_usage", "run_transact", self._root, command=command, artifacts=artifacts)

    def check(self, revision: str, *, uses: list[str], purpose: str, consumer: str) -> dict[str, Any]:
        """Check every ancestor at the current clock; ineligibility is a report verdict."""
        return self._call("evidence_usage", "run_check", self._root, revision=revision,
                          uses=uses, purpose=purpose, consumer=consumer)

    def lineage(self, revision: str, *, limit: int = 4096) -> dict[str, Any]:
        """Trace bounded downstream references, explicitly reporting incomplete results."""
        return self._call("evidence_usage", "run_lineage", self._root, revision=revision, limit=limit)

    def materialize(self, revision: str, *, expected_content_hash: str | None = None) -> dict[str, Any]:
        """Publish exact approved normalized bytes, requiring a hash to replace different content."""
        return self._call("evidence_usage", "run_materialize", self._root, revision=revision,
                          expected_content_hash=expected_content_hash)
