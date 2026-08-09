"""Seam-conformance cases for ``export_answers.py``.

The export is read-only, so unlike intake both sides of every case can run
against the same workspace without either disturbing the other -- the only copy
here exists because the refusal needs its own ``research.yml``.

Note there is no ``--format text`` to cover: this command renders ``json`` or
``jsonl``, and ``jsonl`` is the same document reshaped by ``render_output``
rather than a second document. The seam returns the document, so ``json`` is the
form the contract compares.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from tests.seam_cases import REFUSAL, SUCCESS, SeamCase

SCRIPT = "export_answers.py"

#: Inserted into ``project:`` so the workspace declares a handoff block. With a
#: secret configured and no ``project.handoff_signature`` beside it, the export
#: refuses on the unsigned branch of HANDOFF_SIGNATURE_INVALID.
PROJECT_HANDOFF = "  handoff:\n    task_id: chain-task-0099\n    requested_by: planner-agent\n"
LANGUAGE_LINE = "  language: en\n"


def declare_unsigned_handoff(root: Path) -> None:
    config_path = root / "research.yml"
    text = config_path.read_text(encoding="utf-8")
    if LANGUAGE_LINE not in text:
        raise AssertionError("research.yml no longer carries project.language, so the handoff anchor is gone")
    config_path.write_text(text.replace(LANGUAGE_LINE, LANGUAGE_LINE + PROJECT_HANDOFF, 1), encoding="utf-8")
    (root / ".research-handoff-secret").write_text("seam-conformance-secret\n", encoding="utf-8")


def cases(workspace: Path) -> tuple[SeamCase, ...]:
    root = str(workspace)

    unsigned = workspace.parent / "unsigned-handoff"
    shutil.copytree(workspace, unsigned)
    declare_unsigned_handoff(unsigned)
    unsigned_root = str(unsigned)

    return (
        SeamCase(
            name="export_document",
            argv=("--project-root", root, "--format", "json"),
            call=lambda module: module.run_export(root),
            expect=SUCCESS,
            volatile=("generated_at",),
            note="the whole export, unfiltered",
        ),
        SeamCase(
            name="status_filtered_export",
            argv=("--project-root", root, "--format", "json", "--status", "open", "--status", "answered"),
            call=lambda module: module.run_export(root, status=["open", "answered"]),
            expect=SUCCESS,
            volatile=("generated_at",),
            note="the repeatable --status filter must reach the seam as one list",
        ),
        SeamCase(
            name="handoff_signature_invalid",
            argv=("--project-root", unsigned_root, "--format", "json"),
            call=lambda module: module.run_export(unsigned_root),
            expect=REFUSAL,
            note="the ad-hoc SystemExit this file used to raise, now a named ExportRefusal",
        ),
    )
