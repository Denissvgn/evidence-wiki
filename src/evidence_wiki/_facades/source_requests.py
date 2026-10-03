"""Source request declarations and controlled fulfillment over canonical owners."""

from __future__ import annotations

from typing import Any

from ._base import Namespace


class SourceRequestsNamespace(Namespace):
    """A request records an evidence gap; creating one acquires no source."""

    def add(self, *, kind: str, query_or_identifier: str, rationale: str,
            priority: str = "medium", question_slugs: list[str] | None = None,
            scope: dict[str, str] | None = None) -> dict[str, Any]:
        return self._call("source_requests", "run_add_request", self._root, kind=kind,
            query_or_identifier=query_or_identifier, rationale=rationale, priority=priority,
            question_slugs=question_slugs, scope=scope)

    def list(self, *, statuses: list[str] | None = None, kinds: list[str] | None = None,
             scope: dict[str, str] | None = None) -> dict[str, Any]:
        return self._call("source_requests", "run_list_requests", self._root,
                          statuses=statuses, kinds=kinds, scope=scope)

    def fulfill(self, *, request_id: str, source_id: str, match_scope: dict[str, str] | None = None,
                require_scope: bool = False) -> dict[str, Any]:
        """Match a delivered manifest source; delegated claims remain contingent."""
        return self._call("source_requests", "run_fulfill_request", self._root,
            request_id=request_id, source_id=source_id, match_scope=match_scope, require_scope=require_scope)
