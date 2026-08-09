"""Workspace preparation shared by the two question-lifecycle case modules.

``question_claim.py`` and ``question_resolve.py`` need the same two things a
freshly initialized workspace does not have: question pages to operate on, and —
for the cases that genuinely mutate — a second workspace holding exactly the same
state, so the CLI and the seam each get an untouched copy of it.

That second copy is what makes a mutating operation expressible as a
``SeamCase`` at all. The harness runs ``argv`` and then ``call``, and a claim, a
release, or a resolution is not idempotent: run twice against one workspace the
second run sees the first run's writes and reports something else entirely. Two
copies of one prepared state make the two runs the same operation again, which is
the only way the contract's question — *does the seam do what the CLI does?* — can
be asked about a verb that writes.

Preparation happens through the scripts' own CLIs rather than by hand-writing
frontmatter, so the state these cases start from is state the package actually
produces.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "workspace-template" / "scripts"


def _run_script(script: str, *args: str) -> subprocess.CompletedProcess:
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / script), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"seam-case setup failed: {script} {' '.join(args)}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result


def add_questions(workspace: Path, slugs: tuple[str, ...]) -> None:
    """Create one open question page per slug through the real intake path."""
    batch = {
        "schema_version": "1.0",
        "questions": [
            {"question": f"Seam conformance question {slug}?", "id": slug, "priority": "high"}
            for slug in slugs
        ],
    }
    batch_path = workspace.parent / "questions.json"
    batch_path.write_text(json.dumps(batch), encoding="utf-8")
    _run_script(
        "intake_questions.py",
        "--project-root", str(workspace),
        "--from-file", str(batch_path),
        "--format", "json",
    )


def claim(workspace: Path, slug: str, agent_id: str) -> None:
    """Claim a question through the CLI, so the page carries a real claim block."""
    _run_script(
        "question_claim.py",
        "--project-root", str(workspace),
        "claim",
        "--slug", slug,
        "--agent-id", agent_id,
        "--format", "json",
    )


def twin_copies(workspace: Path, name: str) -> tuple[str, str]:
    """Copy the prepared workspace twice and return the two roots as strings.

    The copies are taken *after* every claim this module makes, so a claim
    timestamp already on a page is identical in both — only a timestamp the case
    itself writes is genuinely per-invocation, and only that one is declared
    volatile.
    """
    scratch = workspace.parent
    for_cli = scratch / f"{name}-cli"
    for_seam = scratch / f"{name}-seam"
    shutil.copytree(workspace, for_cli)
    shutil.copytree(workspace, for_seam)
    return str(for_cli), str(for_seam)
