"""Seam-conformance cases for ``question_claim.py``.

Every verb here writes, which makes this the first case module whose success
cases cannot simply be run twice against one workspace: claiming a question the
CLI just claimed is a different operation (an idempotent re-claim by the holder),
and so is releasing a claim the CLI just released. The applied cases therefore
hand the CLI and the seam their own copy of one prepared state — see
``tests/_seam_question_fixture.py`` — while the cases that do not write share a
single workspace.

The claim timestamp a run stamps is genuinely per-invocation and is declared
volatile. A timestamp that was already on the page before the copies were taken
is not: it is identical in both copies and is compared like any other field, so a
seam that dropped or reformatted ``previous_holder`` still fails.
"""

from __future__ import annotations

from pathlib import Path

from tests._seam_question_fixture import add_questions, claim, twin_copies
from tests.seam_cases import REFUSAL, SUCCESS, SeamCase

SCRIPT = "question_claim.py"

HOLDER = "agent-holder"
OTHER = "agent-other"


def cases(workspace: Path) -> tuple[SeamCase, ...]:
    add_questions(workspace, ("fresh", "held", "contended", "stale"))
    # Claimed before the copies are taken, so both copies carry the same claimed_at.
    claim(workspace, "held", HOLDER)
    claim(workspace, "contended", HOLDER)
    claim(workspace, "stale", HOLDER)
    shared = str(workspace)
    claim_cli, claim_seam = twin_copies(workspace, "claim")
    release_cli, release_seam = twin_copies(workspace, "release")
    steal_cli, steal_seam = twin_copies(workspace, "steal")

    return (
        SeamCase(
            name="claim_applies",
            argv=("--project-root", claim_cli, "claim", "--slug", "fresh", "--agent-id", OTHER, "--format", "json"),
            call=lambda module: module.run_claim(claim_seam, slug="fresh", agent_id=OTHER),
            expect=SUCCESS,
            volatile=("holder.claimed_at",),
            note="the applied transition, on twin copies of one open question",
        ),
        SeamCase(
            name="claim_is_idempotent_for_the_holder",
            argv=("--project-root", shared, "claim", "--slug", "held", "--agent-id", HOLDER, "--format", "json"),
            call=lambda module: module.run_claim(shared, slug="held", agent_id=HOLDER),
            expect=SUCCESS,
            note="a no-op writes nothing, so one workspace serves both runs and no field is volatile",
        ),
        SeamCase(
            name="steal_applies",
            argv=(
                "--project-root", steal_cli, "claim", "--slug", "stale", "--agent-id", OTHER,
                "--steal", "--if-older-than", "0", "--format", "json",
            ),
            call=lambda module: module.run_claim(
                steal_seam, slug="stale", agent_id=OTHER, steal=True, if_older_than=0.0
            ),
            expect=SUCCESS,
            volatile=("holder.claimed_at",),
            note="orchestrator-mediated recovery: previous_holder is compared, the new claim's clock is not",
        ),
        SeamCase(
            name="release_applies",
            argv=("--project-root", release_cli, "release", "--slug", "held", "--agent-id", HOLDER, "--format", "json"),
            call=lambda module: module.run_release(release_seam, slug="held", agent_id=HOLDER),
            expect=SUCCESS,
            note="release clears the claim; previous_holder came from before the copies and must match exactly",
        ),
        SeamCase(
            name="claim_held_by_another_agent",
            argv=("--project-root", shared, "claim", "--slug", "contended", "--agent-id", OTHER, "--format", "json"),
            call=lambda module: module.run_claim(shared, slug="contended", agent_id=OTHER),
            expect=REFUSAL,
            note="CLAIM_HELD, exit 3: the contention refusal, whose message names the holder and its clock",
        ),
        SeamCase(
            name="steal_without_a_threshold",
            argv=(
                "--project-root", shared, "claim", "--slug", "contended", "--agent-id", OTHER,
                "--steal", "--format", "json",
            ),
            call=lambda module: module.run_claim(shared, slug="contended", agent_id=OTHER, steal=True),
            expect=REFUSAL,
            note="STEAL_THRESHOLD_REQUIRED, exit 2: stealing is never automatic on either path",
        ),
        SeamCase(
            name="unknown_slug",
            argv=("--project-root", shared, "claim", "--slug", "no-such-question", "--agent-id", HOLDER, "--format", "json"),
            call=lambda module: module.run_claim(shared, slug="no-such-question", agent_id=HOLDER),
            expect=REFUSAL,
            note="SLUG_UNKNOWN, exit 2: refused before any lock is taken",
        ),
        SeamCase(
            name="empty_agent_id",
            argv=("--project-root", shared, "claim", "--slug", "held", "--agent-id", "  ", "--format", "json"),
            call=lambda module: module.run_claim(shared, slug="held", agent_id="  "),
            expect=REFUSAL,
            note="AGENT_ID_INVALID, exit 2: the seam strips its inputs the way the CLI always did",
        ),
    )
