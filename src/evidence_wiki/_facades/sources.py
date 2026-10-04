"""Canonical source inventory operations for an existing workspace."""

from __future__ import annotations

from typing import Any

from ._base import Namespace, call_seam


class SourcesNamespace(Namespace):
    """Inventory retained local inputs through the source manifest owner."""

    def capture_bytes_document(self, content: bytes, metadata: dict[str, Any]) -> dict[str, Any]:
        """Build a bounded inert capture delivery without publishing or approval."""
        self._workspace._check_open()
        from ..errors import UsageError
        from ..source_capture_contracts import from_bytes
        from ..source_capture_inputs import CaptureInputError

        try:
            return from_bytes(content, metadata)
        except CaptureInputError as error:
            raise UsageError("SOURCE_CAPTURE_REFUSED", "Capture input refused.",
                remediation="Use the published capture metadata schema and bounded original bytes.",
                details={"field": str(error)}) from None

    def capture_path_document(self, *, root, relative: str, metadata: dict[str, Any],
                              expected_content_sha256: str, expected_bytes: int) -> dict[str, Any]:
        """Observe an explicitly permitted original; its descriptor remains inert."""
        self._workspace._check_open()
        from ..errors import UsageError
        from ..host_context import current_context
        from ..source_capture_contracts import from_path
        from ..source_capture_inputs import CaptureInputError

        context = current_context()
        if context is None:
            raise UsageError("SOURCE_CAPTURE_REFUSED", "Capture path requires a selected host context.",
                details={"field": "approved_context_required"})
        context.require_grant("source_capture_contracts.from_path")
        try:
            return from_path(context, root=root, relative=relative, supplied=metadata,
                expected_content_sha256=expected_content_sha256, expected_bytes=expected_bytes)
        except CaptureInputError as error:
            raise UsageError("SOURCE_CAPTURE_REFUSED", "Capture input refused.",
                remediation="Use an approved native path and exact current content identity.",
                details={"field": str(error)}) from None

    def prepare_capture(self, *, request_id: str, path: str, delivery: dict[str, Any]) -> dict[str, Any]:
        """Freeze capture identity and scope through the existing mutation owner."""
        return self._workspace.mutations.prepare(request_id=request_id,
            operation="sources.capture_revision", arguments={"path": path, "delivery": delivery})

    def capture_revision(self, request: dict[str, Any]) -> dict[str, Any]:
        """Submit an exact retained capture request; replay never repeats publication."""
        self._workspace._check_open()
        from ..mutation_contracts import REQUEST, checked, refuse

        request = checked(REQUEST, request)
        if request["operation"] != "sources.capture_revision":
            refuse("OPERATION_UNSUPPORTED", "/operation")
        return self._workspace.mutations.submit(request)

    def resume_capture(self, request_id: str) -> dict[str, Any]:
        """Explicitly complete sealed pair outputs while preserving original uncertainty."""
        self._workspace._check_open()
        def load(_stem):
            from .. import mutation_capture_recovery
            return mutation_capture_recovery
        return call_seam(load, "mutation_capture_recovery", "resume", self._root, request_id)

    def inventory(
        self, *, dry_run: bool = False, append_log: bool = False,
        require_checksum: bool = False, reject_mismatch: bool = False,
        max_records: int = 1000, max_entries: int = 10_000,
        max_input_bytes: int = 16_777_216,
    ) -> dict[str, Any]:
        """Return the inventory result without terminal rendering.

        Checksum refusals may follow the admitted manifest write. Inspect the
        canonical state before retrying; refusal does not promise rollback.
        """
        return self._call(
            "source_inventory", "run_inventory_report", self._root,
            dry_run=dry_run, append_log=append_log, require_checksum=require_checksum,
            reject_mismatch=reject_mismatch, max_records=max_records,
            max_entries=max_entries, max_input_bytes=max_input_bytes,
        )
