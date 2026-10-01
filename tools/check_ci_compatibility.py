#!/usr/bin/env python3
"""Check portable collection and HTML contracts without claiming complete-suite execution."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Direct script invocation puts tools, rather than the checkout, on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.run_test_groups import run, source_identity  # noqa: E402

TARGETS = (
    "tests/test_html_usability.py",
    "tests/test_strict_evidence.py::test_html_qualification_only_change_cannot_reuse_a_review_receipt",
    "tests/test_test_groups.py::test_collection_id_guard",
)


def require_complete(row: dict, expected: list[str], *, collect: bool = False) -> None:
    """Refuse missing records, failed processes, or altered execution inventories."""
    observed = row.get("result", {})
    if (row["exit_code"] != 0 or observed.get("exit_code") != 0
            or observed.get("collected") != expected
            or observed.get("executed") != ([] if collect else expected)
            or (not collect and observed.get("outcomes", {}).get("passed") != len(expected))):
        raise ValueError(f"{row['label']} failed or did not complete its exact selection")


def failure_details(row: dict) -> None:
    """Show bounded retained diagnostics without flooding the job log."""
    path = Path(row["log"])
    print(f"Diagnostics: {path}", flush=True)
    if path.is_file():
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - 4000))
            print(stream.read(4000).decode("utf-8", errors="replace"), flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("compatibility-evidence"))
    args = parser.parse_args(argv)
    root, output = Path.cwd().resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    identity = source_identity(root)
    report = {"kind": "compatibility", "status": "running", "python": sys.version,
              "python_version": ".".join(map(str, sys.version_info[:3])),
              "expected_python": os.environ.get("EVIDENCE_WIKI_EXPECTED_PYTHON"),
              "source_sha256": identity, "runs": [],
              "limits": ["Full collection, selected execution only; not a full-suite shard."]}
    row = None
    try:
        if report["expected_python"] and report["expected_python"] != report["python_version"]:
            raise ValueError("Resolved Python patch differs from the expected qualification runtime")
        row = run(root, output, "collection", ["tests"], collect=True, timeout=180)
        report["runs"].append(row)
        nodes = row.get("result", {}).get("collected", [])
        require_complete(row, nodes, collect=True)
        if not nodes or len(nodes) != len(set(nodes)):
            raise ValueError("Complete collection is empty or contains duplicate test IDs")
        selected = []
        for target in TARGETS:
            members = [node for node in nodes if node == target or node.startswith(target + "::")
                       or node.startswith(target + "[")]
            if not members:
                raise ValueError(f"Required compatibility target was not collected: {target}")
            selected.extend(members)
        row = run(root, output, "compatibility", selected, timeout=600)
        report["runs"].append(row)
        require_complete(row, selected)
        report.update(collected=len(nodes), selected=len(selected))
        report["sources_unchanged"] = source_identity(root) == identity
        if not report["sources_unchanged"]:
            raise ValueError("Sources changed during compatibility checks")
        report["status"] = "passed"
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        report.update(status="failed", error=str(error))
        print(f"Compatibility checks failed: {error}", flush=True)
        if row is not None:
            failure_details(row)
        return 1
    except KeyboardInterrupt:
        report.update(status="interrupted", error="Interrupted; no pass credit.")
        return 130
    finally:
        (output / "summary.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")
        print(f"Compatibility evidence: {output / 'summary.json'}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
