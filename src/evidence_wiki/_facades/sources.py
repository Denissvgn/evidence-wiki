"""Canonical source inventory operations for an existing workspace."""

from __future__ import annotations

from typing import Any

from ._base import Namespace


class SourcesNamespace(Namespace):
    """Inventory retained local inputs through the source manifest owner."""

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
