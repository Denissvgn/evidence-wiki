"""Seam-conformance cases for ``question_resolve.py``.

Resolution is the widest refusal surface in the package: four exception families
reach the CLI's error envelope by four different routes, and two of them carry
their own exit code. The refusal cases below pick one route each, and deliberately
include both exit codes a resolution can end on — a claim conflict is exit 3, and
everything else is exit 2 — because collapsing them is exactly the mistake a
rewrite of these handlers makes.

The applied cases run on twin copies of one prepared workspace, for the reason
``tests/_seam_question_fixture.py`` explains: a resolution is not idempotent, and
a terminal question refuses the second attempt outright.
"""

from __future__ import annotations

from pathlib import Path

from tests._seam_question_fixture import add_questions, claim, twin_copies
from tests.seam_cases import REFUSAL, SUCCESS, SeamCase

SCRIPT = "question_resolve.py"

HOLDER = "agent-holder"
OTHER = "agent-other"
ANSWER_PAGE = "wiki/synthesis/seam-answer.md"


def cases(workspace: Path) -> tuple[SeamCase, ...]:
    add_questions(workspace, ("defer-me", "block-me", "answer-me", "held", "unclaimed"))
    # Claimed before the copies are taken, so previous_holder is identical in both.
    for slug in ("defer-me", "block-me", "answer-me", "held"):
        claim(workspace, slug, HOLDER)
    (workspace / ANSWER_PAGE).write_text(
        "---\ntype: synthesis\ntitle: Seam answer\n---\n\nAn answer page the resolution can point at.\n",
        encoding="utf-8",
    )
    shared = str(workspace)
    defer_cli, defer_seam = twin_copies(workspace, "defer")
    block_cli, block_seam = twin_copies(workspace, "block")
    answer_cli, answer_seam = twin_copies(workspace, "answer")

    return (
        SeamCase(
            name="defer_applies",
            argv=(
                "--project-root", defer_cli, "defer", "--slug", "defer-me", "--agent-id", HOLDER,
                "--reason", "Out of scope for this run.", "--format", "json",
            ),
            call=lambda module: module.run_defer(
                defer_seam, slug="defer-me", agent_id=HOLDER, reason="Out of scope for this run."
            ),
            expect=SUCCESS,
            note="the simplest applied resolution: status, previous_holder, and the page label must all match",
        ),
        SeamCase(
            name="block_applies",
            argv=(
                "--project-root", block_cli, "block", "--slug", "block-me", "--agent-id", HOLDER,
                "--blocked-reason", "No admissible evidence yet.", "--format", "json",
            ),
            call=lambda module: module.run_block(
                block_seam, slug="block-me", agent_id=HOLDER, blocked_reason="No admissible evidence yet."
            ),
            expect=SUCCESS,
            note="the verb the audit-trail proof drives, so its two paths are held together here too",
        ),
        SeamCase(
            name="answer_applies_uncited",
            argv=(
                "--project-root", answer_cli, "answer", "--slug", "answer-me", "--agent-id", HOLDER,
                "--answer-page", ANSWER_PAGE, "--allow-uncited", "--confidence", "medium", "--format", "json",
            ),
            call=lambda module: module.run_answer(
                answer_seam,
                slug="answer-me",
                agent_id=HOLDER,
                answer_page=ANSWER_PAGE,
                allow_uncited=True,
                confidence="medium",
            ),
            expect=SUCCESS,
            note="the widest verb: an optional flag that reaches the written frontmatter must reach it both ways",
        ),
        SeamCase(
            name="unknown_slug",
            argv=(
                "--project-root", shared, "defer", "--slug", "no-such-question", "--agent-id", HOLDER,
                "--reason", "irrelevant", "--format", "json",
            ),
            call=lambda module: module.run_defer(
                shared, slug="no-such-question", agent_id=HOLDER, reason="irrelevant"
            ),
            expect=REFUSAL,
            note="the sibling ClaimError funnel: SLUG_UNKNOWN, exit 2",
        ),
        SeamCase(
            name="question_not_claimed",
            argv=(
                "--project-root", shared, "defer", "--slug", "unclaimed", "--agent-id", HOLDER,
                "--reason", "irrelevant", "--format", "json",
            ),
            call=lambda module: module.run_defer(shared, slug="unclaimed", agent_id=HOLDER, reason="irrelevant"),
            expect=REFUSAL,
            note="ResolveError, exit 2: an unheld question is not resolved without --allow-unclaimed",
        ),
        SeamCase(
            name="claim_held_by_another_agent",
            argv=(
                "--project-root", shared, "defer", "--slug", "held", "--agent-id", OTHER,
                "--reason", "irrelevant", "--format", "json",
            ),
            call=lambda module: module.run_defer(shared, slug="held", agent_id=OTHER, reason="irrelevant"),
            expect=REFUSAL,
            note="ResolveError, exit 3: the one refusal here that is not exit 2, and must stay that way",
        ),
        SeamCase(
            name="answer_source_required",
            argv=(
                "--project-root", shared, "answer", "--slug", "held", "--agent-id", HOLDER,
                "--answer-page", ANSWER_PAGE, "--format", "json",
            ),
            call=lambda module: module.run_answer(
                shared, slug="held", agent_id=HOLDER, answer_page=ANSWER_PAGE
            ),
            expect=REFUSAL,
            note="ANSWER_SOURCE_REQUIRED, exit 2: an uncited answer is refused unless it is explicit",
        ),
        SeamCase(
            name="empty_reviewer",
            argv=("--project-root", shared, "approve", "--slug", "held", "--reviewer", "  ", "--format", "json"),
            call=lambda module: module.run_approve(shared, slug="held", reviewer="  "),
            expect=REFUSAL,
            note="REVIEWER_INVALID, exit 2: approve records --reviewer, not --agent-id, on both paths",
        ),
    )
