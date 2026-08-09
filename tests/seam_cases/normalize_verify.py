"""Seam-conformance cases for ``normalize_verify.py``.

The interesting case here is the one in the middle. This command has three
outcomes, not two: it verifies, it refuses, and it *reports a failed
verification* — and only the second of those is a refusal. A record that breaches
the contract makes the CLI exit ``EXIT_NOT_VERIFIED`` while still printing the
whole report, violation list and all, so the seam must return that report rather
than raise. ``not_verified_is_not_a_refusal`` is a SUCCESS case for exactly that
reason, and it is the case that fails if a later change ever mistakes a verdict
for an error.

Both refusal funnels are covered too, because they reach ``ScriptRefusal`` by
different routes: an unknown ``--source-id`` raises the command's own coded
``NormalizeVerifyError``, while a missing manifest surfaces a ``SystemExit`` whose
message the shared helper classifies.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from tests.seam_cases import REFUSAL, SUCCESS, SeamCase

SCRIPT = "normalize_verify.py"

#: A record with no frontmatter at all: the shortest way to a real contract
#: violation, and one the verifier reports rather than refuses over.
BROKEN_RECORD = "this file is not a normalized record\n"


def cases(workspace: Path) -> tuple[SeamCase, ...]:
    root = str(workspace)

    # A copy carrying one unreadable record. Copied from the pristine workspace before
    # any case runs, so the verified case above never sees it.
    not_verified = workspace.parent / "not-verified"
    shutil.copytree(workspace, not_verified)
    normalized = not_verified / "sources" / "normalized"
    normalized.mkdir(parents=True, exist_ok=True)
    (normalized / "broken-record.md").write_text(BROKEN_RECORD, encoding="utf-8")
    not_verified_root = str(not_verified)

    # A copy with no manifest at all, which is the workspace-unreadable funnel.
    no_manifest = workspace.parent / "no-manifest"
    shutil.copytree(workspace, no_manifest)
    (no_manifest / "sources" / "manifest.jsonl").unlink()
    no_manifest_root = str(no_manifest)

    return (
        SeamCase(
            name="verify_all",
            argv=("--project-root", root, "--format", "json"),
            call=lambda module: module.run_verify(root),
            expect=SUCCESS,
            volatile=("generated_at",),
            note="the default selection: every record under the normalized directory",
        ),
        SeamCase(
            name="not_verified_is_not_a_refusal",
            argv=("--project-root", not_verified_root, "--format", "json"),
            call=lambda module: module.run_verify(not_verified_root),
            expect=SUCCESS,
            volatile=("generated_at",),
            note=(
                "the CLI exits EXIT_NOT_VERIFIED here and still prints the report; the seam "
                "must return that report, because the violation list is what the caller asked for"
            ),
        ),
        SeamCase(
            name="unknown_source_id",
            argv=("--project-root", root, "--format", "json", "--source-id", "no-such-source"),
            call=lambda module: module.run_verify(root, source_ids=["no-such-source"]),
            expect=REFUSAL,
            note="the command's own coded NormalizeVerifyError funnel (SOURCE_UNKNOWN)",
        ),
        SeamCase(
            name="missing_manifest",
            argv=("--project-root", no_manifest_root, "--format", "json"),
            call=lambda module: module.run_verify(no_manifest_root),
            expect=REFUSAL,
            note="SystemExit funnel, whose error code the shared helper classifies from the message",
        ),
    )
