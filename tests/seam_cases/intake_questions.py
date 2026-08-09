"""Seam-conformance cases for ``intake_questions.py``.

This is the first enrolled script whose seam *writes*. That changes how the
cases have to be built, because the harness runs each case twice -- once as a
subprocess and once in-process -- and intake is idempotent by design: the second
run of a batch sees its own questions already on disk and reports them as
skipped duplicates instead of created pages. Run twice against one workspace, a
mutating case would compare a "created 2" document against a "created 0" one and
fail for a reason that has nothing to do with the seam.

So the mutating case is given two pristine copies of the workspace, one for each
side. Both copies start identical and the report names only workspace-relative
paths, so the two documents still have to match field for field -- which is the
property under test -- while each side gets a workspace in the state the
operation expects.

The refusal cases write nothing (both limits and the handoff signature are
checked before any page is written), but they still get their own copies because
each needs its own ``research.yml``.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from tests.seam_cases import REFUSAL, SUCCESS, SeamCase

SCRIPT = "intake_questions.py"

#: A dry run reports the pages it *would* write, so the same batch can be
#: submitted from both sides without either side changing the workspace.
DRY_RUN_BATCH = {
    "schema_version": "1.0",
    "questions": [
        {"question": "What benchmarks matter for this domain?", "id": "benchmarks", "priority": "high"},
        {"question": "Which datasets are load-bearing?", "id": "datasets", "priority": "medium"},
    ],
}

#: Written for real, once per side, against that side's own copy.
INTAKE_BATCH = {
    "schema_version": "1.0",
    "handoff": {"task_id": "chain-task-0042", "requested_by": "planner-agent"},
    "questions": [
        {
            "question": "What evaluation protocol should the answer follow?",
            "id": "evaluation-protocol",
            "priority": "high",
            "summary": "Pin down the protocol before answering.",
            "context": "Submitted by the seam-conformance suite.",
        },
    ],
}

#: Two questions against a workspace whose total cap is one.
CAP_BATCH = {
    "schema_version": "1.0",
    "questions": [
        {"question": "First question over the cap?", "id": "over-cap-one", "priority": "low"},
        {"question": "Second question over the cap?", "id": "over-cap-two", "priority": "low"},
    ],
}

#: Carries a handoff block but no signature, against a workspace that configures
#: a secret -- the unsigned branch of HANDOFF_SIGNATURE_INVALID.
UNSIGNED_BATCH = {
    "schema_version": "1.0",
    "handoff": {"task_id": "chain-task-0043", "requested_by": "planner-agent"},
    "questions": [
        {"question": "Does an unsigned handoff refuse?", "id": "unsigned-handoff", "priority": "low"},
    ],
}


def write_batch(scratch: Path, name: str, batch: dict) -> str:
    path = scratch / f"{name}.json"
    path.write_text(json.dumps(batch, indent=2) + "\n", encoding="utf-8")
    return str(path)


def cap_total_open_questions(root: Path, limit: int) -> None:
    """Lower ``run.max_open_questions_total`` in place, leaving the rest of the file alone."""
    config_path = root / "research.yml"
    text = config_path.read_text(encoding="utf-8")
    original = "  max_open_questions_total: 250\n"
    if original not in text:
        raise AssertionError("research.yml no longer carries the run.max_open_questions_total default")
    config_path.write_text(text.replace(original, f"  max_open_questions_total: {limit}\n"), encoding="utf-8")


def cases(workspace: Path) -> tuple[SeamCase, ...]:
    scratch = workspace.parent
    root = str(workspace)

    # Every copy is taken here, before any case has run, so each is a copy of the
    # pristine workspace rather than of whatever an earlier case left behind.
    cli_target = scratch / "intake-cli"
    seam_target = scratch / "intake-seam"
    shutil.copytree(workspace, cli_target)
    shutil.copytree(workspace, seam_target)

    capped = scratch / "capped"
    shutil.copytree(workspace, capped)
    cap_total_open_questions(capped, 1)

    signed = scratch / "signed"
    shutil.copytree(workspace, signed)
    (signed / ".research-handoff-secret").write_text("seam-conformance-secret\n", encoding="utf-8")

    dry_run_batch = write_batch(scratch, "dry-run-batch", DRY_RUN_BATCH)
    intake_batch = write_batch(scratch, "intake-batch", INTAKE_BATCH)
    cap_batch = write_batch(scratch, "cap-batch", CAP_BATCH)
    unsigned_batch = write_batch(scratch, "unsigned-batch", UNSIGNED_BATCH)

    return (
        SeamCase(
            name="dry_run_plan",
            argv=("--project-root", root, "--from-file", dry_run_batch, "--dry-run"),
            call=lambda module: module.run_intake(root, from_file=dry_run_batch, dry_run=True),
            expect=SUCCESS,
            volatile=("generated_at",),
            note="writes nothing, so both sides can run it against the one pristine workspace",
        ),
        SeamCase(
            name="batch_creates_pages",
            argv=("--project-root", str(cli_target), "--from-file", intake_batch, "--format", "json"),
            call=lambda module: module.run_intake(str(seam_target), from_file=intake_batch),
            expect=SUCCESS,
            volatile=("generated_at",),
            note=(
                "the mutating path: page writes, the index.md update and the log.md append all "
                "sit inside the seam, and index_updated/log_appended/created report on them"
            ),
        ),
        SeamCase(
            name="total_cap_exceeded",
            argv=("--project-root", str(capped), "--from-file", cap_batch, "--format", "json"),
            call=lambda module: module.run_intake(str(capped), from_file=cap_batch),
            expect=REFUSAL,
            note="INTAKE_TOTAL_CAP_EXCEEDED, a coded IntakeValidationError with details",
        ),
        SeamCase(
            name="handoff_signature_invalid",
            argv=("--project-root", str(signed), "--from-file", unsigned_batch, "--format", "json"),
            call=lambda module: module.run_intake(str(signed), from_file=unsigned_batch),
            expect=REFUSAL,
            note="HANDOFF_SIGNATURE_INVALID, refused before anything is read or written",
        ),
    )
