"""Snapshot preparation transports the complete selection and refusal unchanged."""

import json
from pathlib import Path

import pytest

from tests._snapshot_fixture import SnapshotFixture
from tests.seam_cases import REFUSAL, SeamCase

SCRIPT = "evidence_snapshots.py"


def cases(workspace: Path) -> tuple[SeamCase, ...]:
    directory = workspace.parent / "host-fixture"
    directory.mkdir()
    with pytest.MonkeyPatch.context() as patch:
        fixture = SnapshotFixture(directory, patch)
        body, _files = fixture.add_execution()
        selection = fixture.selection(body)
        root = fixture.root
        environment = {"EVIDENCE_WIKI_AUTHORITY_FILE": str(fixture.policy_path), "EVIDENCE_WIKI_STATE_DIR": str(fixture.host)}
        fixture.workspace.close()
    argv = ("prepare", "--project-root", str(root))
    return (
        SeamCase("prepare_selection", argv, lambda module: module.run_prepare(root, selection=selection),
                 stdin=json.dumps(selection), environment=environment),
        SeamCase("invalid_selection", argv, lambda module: module.run_prepare(root, selection={}),
                 expect=REFUSAL, stdin="{}", environment=environment),
    )
