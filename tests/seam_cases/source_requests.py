"""Source request reader and refusal parity against the command owner."""

from pathlib import Path

from tests.seam_cases import REFUSAL, SeamCase

SCRIPT = "source_requests.py"


def cases(workspace: Path):
    root = str(workspace)
    return (
        SeamCase(
            name="request_list",
            argv=("--project-root", root, "list", "--format", "json"),
            call=lambda module: module.run_list_requests(root),
            volatile=("generated_at",),
        ),
        SeamCase(
            name="unknown_kind",
            argv=("--project-root", root, "add", "--kind", "unlisted-evidence-kind",
                  "--query-or-identifier", "bounded source query", "--rationale", "Retain a declared gap.",
                  "--format", "json"),
            call=lambda module: module.run_add_request(
                root, kind="unlisted-evidence-kind", query_or_identifier="bounded source query",
                rationale="Retain a declared gap.",
            ),
            expect=REFUSAL,
        ),
    )
