"""Seam-conformance cases for ``fleet_status.py``.

Success cases only, and deliberately so: this command has no refusal, which
``SEAM_WITHOUT_REFUSAL`` in ``tests/test_seam_conformance.py`` records and holds.

``degraded_target_is_a_report_entry`` is what stands in for the missing refusal
case, and it is the one case here worth reading. It runs the command over a good
workspace and a path that does not exist, and requires the CLI and the seam to
produce the same document — one ``ok: True`` entry, one ``ok: False`` entry
carrying the failing target's error code. That is the command's whole guarantee:
a host that hands this seam ten workspaces and one bad path gets eleven answers,
not one exception. Nothing else in the suite would notice if a future change
turned that unreadable target into a raised refusal.
"""

from __future__ import annotations

from pathlib import Path

from tests.seam_cases import SUCCESS, SeamCase

SCRIPT = "fleet_status.py"


def cases(workspace: Path) -> tuple[SeamCase, ...]:
    root = str(workspace)
    # Absolute and never created. The child process runs with its cwd set to the scratch
    # directory rather than the workspace, so a relative path would resolve to two
    # different absolute paths and the two documents would differ for the wrong reason.
    missing = str(workspace.parent / "no-such-workspace")

    return (
        SeamCase(
            name="one_healthy_target",
            argv=("--target", root, "--format", "json"),
            call=lambda module: module.run_fleet_status([root]),
            expect=SUCCESS,
            note="the ordinary path: one readable workspace summarized from its status document",
        ),
        SeamCase(
            name="degraded_target_is_a_report_entry",
            argv=("--target", root, "--target", missing, "--format", "json"),
            call=lambda module: module.run_fleet_status([root, missing]),
            expect=SUCCESS,
            note=(
                "the degradation guarantee: an unreadable target is an ok: False entry with its "
                "error code, on both sides, and the command still exits 0"
            ),
        ),
        SeamCase(
            name="no_cache_still_agrees",
            argv=("--target", root, "--format", "json", "--no-cache"),
            call=lambda module: module.run_fleet_status([root], no_cache=True),
            expect=SUCCESS,
            note=(
                "two independent status builds rather than one cache read, so a per-invocation "
                "field leaking into the fleet summary would surface here"
            ),
        ),
    )
