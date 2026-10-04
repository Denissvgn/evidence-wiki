"""Strict evidence checks and host-signed review registration."""

from __future__ import annotations

from typing import Any

from ._base import Namespace


class StrictNamespace(Namespace):
    """The canonical strict owner authenticates evidence and decides acceptance."""

    def admit(self, proposal: dict[str, Any], *, request_id: str, expected_revision: str) -> dict[str, Any]:
        """Submit unaccepted draft data through explicit serialized admission.

        The supplied revision remains a precondition. This operation supplies
        neither a review nor an answered status; its receipt is historical.
        """
        self._workspace._check_open()
        import re

        from ..draft_contracts import PROPOSAL, validate_document
        from ..mutation_contracts import REQUEST, refuse, schema_document

        if type(expected_revision) is not str or re.fullmatch(r"sha256:[a-f0-9]{64}", expected_revision) is None:
            refuse("INVALID", "/expected_revision")
        proposal = validate_document(PROPOSAL, proposal)
        recorded = self._workspace.mutations.inspect(request_id)["record"]
        if recorded is not None:
            intent = recorded["intent"]
            if (intent["operation"] != "strict.admit" or intent["arguments"] != {"proposal": proposal}
                    or intent["expected_revision"] != expected_revision):
                refuse("REQUEST_CONFLICT", "/request_id")
            request = {key: intent[key] for key in schema_document(REQUEST)["required"]
                       if key != "schema_version"}
            request["schema_version"] = REQUEST
            return self._workspace.mutations.submit(request)
        request = self._workspace.mutations.prepare(
            request_id=request_id, operation="strict.admit", arguments={"proposal": proposal})
        if request["expected_revision"] != expected_revision:
            refuse("REVISION_CONFLICT", "/expected_revision")
        return self._workspace.mutations.submit(request)

    def diagnose(self, proposal):
        """Return per-claim native mechanical diagnostics without accepting drafts."""
        self._workspace._check_open()
        from .._facades._base import call_seam
        def load(_stem):
            from .. import claim_diagnostics
            return claim_diagnostics
        return call_seam(load,"claim_diagnostics","current",self._root,proposal)

    def check(self) -> dict[str, Any]:
        """Recompute strict checks without registering a review or publishing files."""
        return self._call("strict_evidence", "run_operation", self._root, operation="check")

    def prepare_review(self, claim_id: str) -> dict[str, Any]:
        """Freeze review inputs for a declared claim; this supplies no signature."""
        return self._call("strict_evidence", "run_operation", self._root,
                          operation="prepare-review", claim_id=claim_id)

    def register_review(self, envelope: dict[str, Any]) -> dict[str, Any]:
        """Register an existing host-signed envelope through the authenticated owner.

        Principal/controller strings confer no authority. The owner verifies the
        whole signature, selected role, current policy and independent authorship.
        """
        return self._call("strict_evidence", "run_operation", self._root,
                          operation="review", envelope=envelope)

    def export(self) -> dict[str, Any]:
        """Return the owner's checked claim release and its unresolved limitations."""
        return self._call("strict_evidence", "run_operation", self._root, operation="export")
