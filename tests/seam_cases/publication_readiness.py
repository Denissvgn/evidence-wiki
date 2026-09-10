"""Selected publication includes the same captured answers and global gates."""

from pathlib import Path

from tests._publication_fixture import write_ship_ready_vendor_fixture
from tests.seam_cases import REFUSAL, SeamCase
from tests.test_publication_readiness import PublicationReadinessTests

SCRIPT = "publication_readiness.py"


def cases(workspace: Path) -> tuple[SeamCase, ...]:
    root = PublicationReadinessTests().init_workspace(workspace.parent, name="selected-workspace")
    write_ship_ready_vendor_fixture(root)
    argv = ("--project-root", str(root), "--format", "json", "--question")
    return (
        SeamCase("selected_publication", (*argv, "vendor-product-spec"),
                 lambda module: module.run_selected_publication(root, ["vendor-product-spec"]),
                 volatile=("readiness.generated_at", "readiness.workspace_status.generated_at", "export.generated_at")),
        SeamCase("unknown_selection", (*argv, "absent"),
                 lambda module: module.run_selected_publication(root, ["absent"]), expect=REFUSAL),
    )
