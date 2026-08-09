"""Seam-conformance cases for ``doctor.py``.

Success cases only, and deliberately so: this command has no refusal any input
reaches, which ``SEAM_WITHOUT_REFUSAL`` in ``tests/test_seam_conformance.py``
records and holds. The obligation that replaces the missing refusal case is
``broken_workspace_is_report_content``: the input another command would have
refused over is asserted to produce the *same document* on both sides, so the
doctor's "contract breaches are report content, not fatal errors" rule is pinned
across the seam rather than merely stated in a docstring.

``env`` has no counterpart in ``argv`` — it is an in-process injection point, so
every case here runs the real environment on both sides, which is what makes the
two comparable at all.
"""

from __future__ import annotations

from pathlib import Path

from tests.seam_cases import SUCCESS, SeamCase

SCRIPT = "doctor.py"


def cases(workspace: Path) -> tuple[SeamCase, ...]:
    root = str(workspace)

    # A directory that is not a workspace at all. The doctor diagnoses it rather than
    # refusing: verdict "missing", exit 1, and the full check list still on stdout.
    not_a_workspace = workspace.parent / "not-a-workspace"
    not_a_workspace.mkdir()
    broken_root = str(not_a_workspace)

    return (
        SeamCase(
            name="diagnosis",
            argv=("--project-root", root, "--format", "json"),
            call=lambda module: module.run_doctor(root),
            expect=SUCCESS,
            volatile=("generated_at",),
            note="env.now_utc() stamps each run; every check below it must still match",
        ),
        SeamCase(
            name="broken_workspace_is_report_content",
            argv=("--project-root", broken_root, "--format", "json"),
            call=lambda module: module.run_doctor(broken_root),
            expect=SUCCESS,
            volatile=("generated_at",),
            note=(
                "the case that stands in for this command's missing refusal: an unreadable "
                "workspace is a 'missing' verdict with a full report, identical on both sides"
            ),
        ),
    )
