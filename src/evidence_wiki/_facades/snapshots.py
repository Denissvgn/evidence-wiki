"""Immutable evidence export and separate historical/current authorization checks."""

from __future__ import annotations

from typing import Any

from .._script_host import load_packaged_script, shared_assets_root
from ._base import Namespace


def verify_snapshot(data: bytes, *, trust_policy_bytes: bytes) -> dict[str, Any]:
    """Verify historical bindings against explicit trust bytes without opening a workspace."""
    script = load_packaged_script(shared_assets_root(), "evidence_snapshots")
    return script.run_verify(data=data, trust_policy_bytes=trust_policy_bytes)


class SnapshotsNamespace(Namespace):
    def prepare(self, selection: dict[str, Any]) -> dict[str, Any]:
        """Freeze a coherent selection and return a registration payload for the host to sign."""
        return self._call("evidence_snapshots", "run_prepare", self._root, selection=selection)

    def export(self, selection: dict[str, Any], *, registration_request_id: str) -> dict[str, Any]:
        """Publish identical bundle bytes from a previously accepted host registration."""
        return self._call("evidence_snapshots", "run_export", self._root, selection=selection,
                          registration_request_id=registration_request_id)

    def check(self, data: bytes) -> dict[str, Any]:
        """Reconcile a frozen bundle with current host authority and revocations."""
        return self._call("evidence_snapshots", "run_check", self._root, data=data)
