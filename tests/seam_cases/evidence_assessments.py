"""Authenticated assessment consumption and refusal agree across entry points."""

import contextlib
import io
import json
from pathlib import Path

import pytest

from evidence_wiki import Workspace
from evidence_wiki.cli import main
from tests._assessment_fixture import AssessmentFixture
from tests.seam_cases import REFUSAL, SeamCase

SCRIPT = "evidence_assessments.py"


def cases(workspace: Path) -> tuple[SeamCase, ...]:
    directory = workspace.parent / "host-fixture"
    directory.mkdir()

    def initialize(profile):
        with contextlib.redirect_stdout(io.StringIO()):
            assert main(["init", "--profile", str(profile)]) == 0

    with pytest.MonkeyPatch.context() as patch:
        fixture = AssessmentFixture(directory, patch, initialize)
        with Workspace.open(fixture.root) as client:
            fixture.checkpoint = client.usage.transact(fixture.command("initialize"))["checkpoint"]
            body, files = fixture.temporal_source()
            fixture.checkpoint = client.usage.transact(fixture.command("deposit", body), artifacts=files)["checkpoint"]
            envelope = fixture.sign(client.assessments.prepare(fixture.request())["registration"])
            client.assessments.issue(envelope)
        root = fixture.root
        environment = {"EVIDENCE_WIKI_AUTHORITY_FILE": str(fixture.policy_path), "EVIDENCE_WIKI_STATE_DIR": str(fixture.host)}
    argv = ("check", "--project-root", str(root))
    return (
        SeamCase("current_authenticated_evidence", argv,
                 lambda module: module.run_operation(root, operation="check", request=envelope),
                 stdin=json.dumps(envelope), environment=environment, volatile=("evaluated_at",)),
        SeamCase("invalid_envelope", argv, lambda module: module.run_operation(root, operation="check", request={}),
                 expect=REFUSAL, stdin="{}", environment=environment),
    )
