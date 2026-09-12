"""Historical evaluation uses one frozen cutoff and the same typed refusals."""

import json
from pathlib import Path

import pytest

from tests._temporal_fixture import TemporalFixture
from tests.seam_cases import REFUSAL, SeamCase

SCRIPT = "evidence_temporal.py"


def cases(workspace: Path) -> tuple[SeamCase, ...]:
    directory = workspace.parent / "host-fixture"
    directory.mkdir()
    with pytest.MonkeyPatch.context() as patch:
        fixture = TemporalFixture(directory, patch)
        fixture.captured()
        request = fixture.request()
        root = fixture.root
        environment = {"EVIDENCE_WIKI_AUTHORITY_FILE": str(fixture.policy_path), "EVIDENCE_WIKI_STATE_DIR": str(fixture.host)}
        fixture.workspace.close()
    argv = ("evaluate", "--project-root", str(root))
    return (
        SeamCase("frozen_historical_evaluation", argv, lambda module: module.run_evaluate(root, request=request),
                 stdin=json.dumps(request), environment=environment, volatile=("retrieval_permission.evaluated_at",)),
        SeamCase("invalid_request", argv, lambda module: module.run_evaluate(root, request={}),
                 expect=REFUSAL, stdin="{}", environment=environment),
    )
