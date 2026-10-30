"""Source request declarations and controlled fulfillment over canonical owners."""

from __future__ import annotations

from typing import Any

from ._base import Namespace, call_seam


class SourceRequestsNamespace(Namespace):
    """A request records an evidence gap; creating one acquires no source."""

    def current(self, *, request_ids=(), question_slugs=(), max_records=200):
        """Keep recorded fulfillment separate from current evidence readiness."""
        self._workspace._check_open()
        def load(_stem):
            from .. import request_lifecycle_operations
            return request_lifecycle_operations
        return call_seam(load, "request_lifecycle_operations", "current", self._root,
            request_ids=request_ids, question_slugs=question_slugs, max_records=max_records)

    def _acquisition(self, seam, **arguments):
        self._workspace._check_open()
        def load(_stem):
            from .. import acquisition_operations
            return acquisition_operations
        return call_seam(load, "acquisition_operations", seam, self._root, **arguments)

    def prepare_acquisition(self, *, request_id, alternative_ids=(), run_id=None):
        """Observe bounded native alternatives without acquiring rights or evidence."""
        return self._acquisition("prepare", request_id=request_id, alternative_ids=alternative_ids, run_id=run_id)

    def reconcile_acquisition(self, *, request_id, acquisition_ref):
        """Bind genuine retained capture custody to current request/source facts."""
        return self._acquisition("reconcile", request_id=request_id, acquisition_ref=acquisition_ref)

    def complete_acquisition(self, *, request_id, acquisition_ref, expected_request_basis,
                             expected_material_basis, mutation_request_id):
        """Apply exact current fulfillment through its existing serialized owner."""
        return self._acquisition("fulfill", request_id=request_id, acquisition_ref=acquisition_ref,
            expected_request_basis=expected_request_basis, expected_material_basis=expected_material_basis,
            mutation_request_id=mutation_request_id)

    def link_current(self, request):
        """Submit a frozen compatible-scope request link through its native owner."""
        self._workspace._check_open()
        from ..mutation_contracts import REQUEST, checked, refuse
        request = checked(REQUEST, request)
        if request["operation"] != "source_requests.link_current":
            refuse("OPERATION_UNSUPPORTED", "/operation")
        return self._workspace.mutations.submit(request)

    def fulfill_current(self, request):
        """Submit current source checks before native historical fulfillment."""
        self._workspace._check_open()
        from ..mutation_contracts import REQUEST, checked, refuse
        request = checked(REQUEST, request)
        if request["operation"] != "source_requests.fulfill_current":
            refuse("OPERATION_UNSUPPORTED", "/operation")
        return self._workspace.mutations.submit(request)

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
