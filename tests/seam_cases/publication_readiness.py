"""Publication readiness and capture refusals agree across both interfaces."""

from pathlib import Path

from evidence_wiki._filesystem import os
from tests._publication_fixture import write_ship_ready_vendor_fixture
from tests.seam_cases import REFUSAL, SUCCESS, SeamCase
from tests.test_publication_readiness import PublicationReadinessTests

SCRIPT = "publication_readiness.py"


def cases(workspace: Path) -> tuple[SeamCase, ...]:
    root = PublicationReadinessTests().init_workspace(workspace.parent, name="selected-workspace").resolve()
    write_ship_ready_vendor_fixture(root)
    argv = ("--project-root", str(root), "--format", "json")
    capture_supported = os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW")
    unsupported_code = None if capture_supported else "EVIDENCE_REVISION_UNSUPPORTED"
    return (
        SeamCase("readiness_document", argv, lambda module: module.build_readiness_document(root),
                 volatile=("generated_at", "workspace_status.generated_at")),
        SeamCase("selected_publication", (*argv, "--question", "vendor-product-spec"),
                 lambda module: module.run_selected_publication(root, ["vendor-product-spec"]),
                 expect=SUCCESS if capture_supported else REFUSAL, error_code=unsupported_code,
                 volatile=("readiness.generated_at", "readiness.workspace_status.generated_at", "export.generated_at")),
        SeamCase("unknown_selection", (*argv, "--question", "absent"),
                 lambda module: module.run_selected_publication(root, ["absent"]), expect=REFUSAL,
                 error_code=unsupported_code or "PUBLICATION_QUESTION_UNKNOWN"),
    )
