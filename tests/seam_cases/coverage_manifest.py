"""Seam-conformance cases for ``coverage_manifest.py``.

``evaluate`` is the subcommand with a seam, and it is the one that writes: it
recomputes every facet verdict and the coverage verdict and persists them, so the
document the CLI prints is also the manifest's new state. Both invocations of a
case therefore evaluate the same manifest in turn, which is exactly the property
worth pinning -- the second evaluation must reproduce the first.

All three refusal funnels ``evaluate`` has are covered, because they reach the
shared ``ScriptRefusal`` by different routes: a rejected slug and a manifest that
does not match the schema raise coded refusals directly, while an unreadable
workspace arrives as a ``SystemExit`` whose message the shared helper classifies.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml

from tests.seam_cases import REFUSAL, SUCCESS, SeamCase

SCRIPT = "coverage_manifest.py"

SCRIPTS = Path(__file__).resolve().parents[2] / "workspace-template" / "scripts"

SLUG = "coverage-seam"
BROKEN_SLUG = "coverage-seam-broken"
FACET_ID = "paper-identity"

#: Only the file's existence matters to ``init``; the frontmatter is here so the
#: fixture is a plausible question page rather than an empty file.
QUESTION_PAGE = (
    "---\n"
    "type: question\n"
    "slug: {slug}\n"
    "status: open\n"
    "created: 2026-08-08\n"
    "updated: 2026-08-08\n"
    "---\n"
    "\n"
    "# {slug}\n"
)

#: An arXiv abstract link, inventoried so the accepted source below is a real manifest
#: record. The policy primitives read its local metadata and nothing off the network.
PROBE_LINK = "https://arxiv.org/abs/2601.00001v1\n"

#: One required facet with ``min_sources: 1``. The success case accepts the inventoried
#: source into it, so the evaluated document carries real per-policy results rather than
#: the empty list an untouched facet would produce.
TEMPLATE = {
    "coverage_profile": "academic-method-existence",
    "required_facets": [
        {
            "facet_id": "paper-identity",
            "description": "Confirm the method exists in a real scholarly index.",
            "required": True,
            "evidence_path": "academic_method_existence",
            "source_policy": "academic_indexed",
            "freshness_policy": "publication_identity",
            "identity_policy": "citation_id_resolves",
            "min_sources": 1,
        }
    ],
    "optional_facets": [],
}


def _run(workspace: Path, script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), "--project-root", str(workspace), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _inventoried_source_id(workspace: Path) -> str:
    """Inventory one raw link and return the manifest source id it was given."""
    links = workspace / "raw" / "links"
    links.mkdir(parents=True, exist_ok=True)
    (links / "probe.txt").write_text(PROBE_LINK, encoding="utf-8")
    result = _run(workspace, "source_inventory.py", "--report", "--format", "json")
    manifest = workspace / "sources" / "manifest.jsonl"
    if not manifest.is_file():
        raise AssertionError(f"could not inventory a source to accept: {result.stderr}")
    return json.loads(manifest.read_text(encoding="utf-8").splitlines()[0])["id"]


def _seed_manifest(workspace: Path, slug: str, template: Path) -> Path:
    """Create the question page and the coverage manifest ``evaluate`` will read."""
    questions = workspace / "wiki" / "questions"
    questions.mkdir(parents=True, exist_ok=True)
    (questions / f"{slug}.md").write_text(QUESTION_PAGE.format(slug=slug), encoding="utf-8")
    result = _run(workspace, SCRIPT, "init", "--slug", slug, "--template", str(template), "--format", "json")
    path = workspace / "sources" / "coverage" / f"{slug}.yml"
    if not path.is_file():
        raise AssertionError(f"could not seed a coverage manifest for {slug}: {result.stderr}")
    return path


def cases(workspace: Path) -> tuple[SeamCase, ...]:
    root = str(workspace)
    scratch = workspace.parent

    template = scratch / "coverage-template.yml"
    template.write_text(yaml.safe_dump(TEMPLATE, sort_keys=False), encoding="utf-8")

    _seed_manifest(workspace, SLUG, template)
    source_id = _inventoried_source_id(workspace)
    accepted = _run(
        workspace, SCRIPT,
        "set-facet", "--slug", SLUG, "--facet-id", FACET_ID,
        "--accepted-source-id", source_id, "--format", "json",
    )
    if accepted.returncode != 0:
        raise AssertionError(f"could not accept a source into {FACET_ID}: {accepted.stderr}")

    # A manifest that is valid YAML and a mapping, so it loads and then fails the
    # schema check -- the refusal the command exists to make.
    broken = _seed_manifest(workspace, BROKEN_SLUG, template)
    broken.write_text(yaml.safe_dump({"question_slug": BROKEN_SLUG}, sort_keys=False), encoding="utf-8")

    # A directory that was never initialized, reached explicitly rather than by cwd.
    not_a_workspace = scratch / "not-a-workspace"
    not_a_workspace.mkdir(exist_ok=True)
    outside = str(not_a_workspace)

    return (
        SeamCase(
            name="evaluate_manifest",
            argv=("--project-root", root, "evaluate", "--slug", SLUG, "--format", "json"),
            call=lambda module: module.run_evaluate(root, slug=SLUG),
            expect=SUCCESS,
            volatile=("manifest.updated_at",),
            note=(
                "the accepted source puts real per-policy results in the document; evaluate also "
                "rewrites the manifest it read, so each run stamps its own updated_at"
            ),
        ),
        SeamCase(
            name="manifest_fails_the_schema",
            argv=("--project-root", root, "evaluate", "--slug", BROKEN_SLUG, "--format", "json"),
            call=lambda module: module.run_evaluate(root, slug=BROKEN_SLUG),
            expect=REFUSAL,
            note="COVERAGE_MANIFEST_INVALID, raised as a coded refusal before anything is written",
        ),
        SeamCase(
            name="rejected_slug",
            argv=("--project-root", root, "evaluate", "--slug", "../escape", "--format", "json"),
            call=lambda module: module.run_evaluate(root, slug="../escape"),
            expect=REFUSAL,
            note="SLUG_INVALID: the slug is refused before it can name a path",
        ),
        SeamCase(
            name="uninitialized_workspace",
            argv=("--project-root", outside, "evaluate", "--slug", SLUG, "--format", "json"),
            call=lambda module: module.run_evaluate(outside, slug=SLUG),
            expect=REFUSAL,
            note="SystemExit funnel, whose error code the shared helper classifies from the message",
        ),
    )
