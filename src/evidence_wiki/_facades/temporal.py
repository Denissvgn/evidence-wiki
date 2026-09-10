"""Read-only evaluation over revision-bound evidence at an explicit decision instant."""

from __future__ import annotations

from typing import Any

from ._base import Namespace


class TemporalNamespace(Namespace):
    def evaluate(self, request: dict[str, Any]) -> dict[str, Any]:
        """Select revisions and evaluate retrieval, policies and grounding at one cutoff."""
        return self._call("evidence_temporal", "run_evaluate", self._root, request=request)
