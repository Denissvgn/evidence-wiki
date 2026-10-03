"""Inventory report and refusal parity over local source ownership."""

from pathlib import Path

from tests.seam_cases import REFUSAL, SeamCase
from tests.test_source_owner_api import workspace as local_workspace

SCRIPT = "source_inventory.py"


def cases(workspace: Path):
    root = local_workspace(workspace.parent / "inventory-local")
    missing = workspace.parent / "inventory-missing"
    missing.mkdir()
    strict = local_workspace(workspace.parent / "inventory-strict")
    return (
        SeamCase(name="inventory_report", argv=("--project-root", str(root), "--report", "--format", "json"),
                 call=lambda module: module.run_inventory_report(root), volatile=("timestamp",)),
        SeamCase(name="inventory_dry_run", argv=("--project-root", str(root), "--report", "--dry-run", "--format", "json"),
                 call=lambda module: module.run_inventory_report(root, dry_run=True), volatile=("timestamp",)),
        SeamCase(name="missing_config", argv=("--project-root", str(missing), "--report", "--format", "json"),
                 call=lambda module: module.run_inventory_report(missing), expect=REFUSAL, error_code="CONFIG_MISSING"),
        SeamCase(name="strict_checksum", argv=("--project-root", str(strict), "--report", "--format", "json", "--require-checksum"),
                 call=lambda module: module.run_inventory_report(strict, require_checksum=True),
                 expect=REFUSAL, error_code="INVENTORY_CHECKSUM_REQUIRED"),
    )
