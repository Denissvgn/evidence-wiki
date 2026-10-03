"""Explicit mutation coordination over existing canonical workspace owners."""

from __future__ import annotations

from typing import Any

from ..operation_control import OperationControl
from ._base import Namespace, call_seam


class MutationsNamespace(Namespace):
    """Host-selected control storage records effects; it grants no authority."""

    def _invoke(self, seam: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self._workspace._check_open()
        def load(stem):
            from .. import mutation_operations

            return mutation_operations

        return call_seam(load, "mutation_operations", seam,
                         self._root, *args, **kwargs)

    def revision(self) -> dict[str, Any]:
        """Observe the bounded canonical revision without starting a mutation."""
        return self._invoke("revision")

    def prepare(self, *, request_id: str, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Bind captured inputs and current owner generation without executing."""
        return self._invoke("prepare_request", request_id=request_id, operation=operation, arguments=arguments)

    def submit(self, request: dict[str, Any], *, operation_control: OperationControl | None = None) -> dict[str, Any]:
        """Submit once with optional host control; unknown work is not redispatched."""
        options = {} if operation_control is None else {"operation_control": operation_control}
        return self._invoke("submit", request, **options)

    def inspect(self, request_id: str) -> dict[str, Any]:
        """Read a historical receipt, independently of current acceptance."""
        return self._invoke("inspect", request_id)

    def reconcile(self, request_id: str, *, operation_control: OperationControl | None = None) -> dict[str, Any]:
        """Inspect retained effects within optional cleanup control, without rollback."""
        options = {} if operation_control is None else {"operation_control": operation_control}
        return self._invoke("reconcile", request_id, **options)
