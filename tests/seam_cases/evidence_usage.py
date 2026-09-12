"""Host usage status and typed refusal agree across both entry points."""

from pathlib import Path

import pytest

from tests._snapshot_fixture import SnapshotFixture
from tests.seam_cases import REFUSAL, SeamCase

SCRIPT = "evidence_usage.py"


def cases(workspace: Path) -> tuple[SeamCase, ...]:
    directory = workspace.parent / "host-fixture"
    directory.mkdir()
    with pytest.MonkeyPatch.context() as patch:
        fixture = SnapshotFixture(directory, patch)
        root = fixture.root
        environment = {"EVIDENCE_WIKI_AUTHORITY_FILE": str(fixture.policy_path), "EVIDENCE_WIKI_STATE_DIR": str(fixture.host)}
        fixture.workspace.close()
    return (
        SeamCase("host_status", ("status", "--project-root", str(root)),
                 lambda module: module.run_status(root), environment=environment),
        SeamCase("invalid_request_identity", ("status", "--project-root", str(root), "--request-id", "../outside"),
                 lambda module: module.run_status(root, request_id="../outside"), expect=REFUSAL, environment=environment),
    )
