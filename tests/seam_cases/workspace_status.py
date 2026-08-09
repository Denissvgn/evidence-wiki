"""Seam-conformance cases for ``workspace_status.py``.

Both refusal funnels the command has are covered, because they reach the shared
``ScriptRefusal`` by different routes: an unknown ``--run-id`` surfaces a coded
run-controller error, while a rejected ``review:`` section surfaces a
``SystemExit`` whose message the shared helper classifies.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from tests.seam_cases import REFUSAL, SUCCESS, SeamCase

SCRIPT = "workspace_status.py"


def cases(workspace: Path) -> tuple[SeamCase, ...]:
    root = str(workspace)

    # A second, deliberately misconfigured copy: research.yml stays valid YAML and the
    # workspace stays structurally complete, so workspace health passes it through and
    # the rejected review setting is what actually refuses. Copied before any case runs,
    # so it is a copy of the pristine workspace.
    invalid_review = workspace.parent / "invalid-review"
    shutil.copytree(workspace, invalid_review)
    with (invalid_review / "research.yml").open("a", encoding="utf-8") as handle:
        handle.write("\nreview:\n  escalation_scope: nonsense\n")
    invalid_root = str(invalid_review)

    return (
        SeamCase(
            name="status_document",
            argv=("--project-root", root, "--format", "json"),
            call=lambda module: module.run_status_report(root),
            expect=SUCCESS,
            note="the cached path, where generated_at is stable and the whole document must match",
        ),
        SeamCase(
            name="status_document_uncached",
            argv=("--project-root", root, "--format", "json", "--no-cache"),
            call=lambda module: module.run_status_report(root, no_cache=True),
            expect=SUCCESS,
            volatile=("generated_at",),
            note="two independent builds stamp their own generated_at; everything else must still match",
        ),
        SeamCase(
            name="counters_change_the_document",
            argv=(
                "--project-root", root, "--format", "json",
                "--questions-processed-this-run", "3",
                "--source-requests-opened-this-run", "1",
            ),
            call=lambda module: module.run_status_report(
                root,
                questions_processed_this_run=3,
                source_requests_opened_this_run=1,
            ),
            expect=SUCCESS,
            note="per-run counters add readiness.budget_state, so the kwargs must map to the flags 1:1",
        ),
        SeamCase(
            name="unknown_run_id",
            argv=("--project-root", root, "--format", "json", "--run-id", "no-such-run"),
            call=lambda module: module.run_status_report(root, run_id="no-such-run"),
            expect=REFUSAL,
            note="coded run-controller error funnel",
        ),
        SeamCase(
            name="rejected_review_scope",
            argv=("--project-root", invalid_root, "--format", "json"),
            call=lambda module: module.run_status_report(invalid_root),
            expect=REFUSAL,
            note="SystemExit funnel, whose error code the shared helper classifies from the message",
        ),
    )
