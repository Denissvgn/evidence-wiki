"""Normalization reports remain verdicts while unreadable scope refuses."""

import json
import shutil
from pathlib import Path

from tests.seam_cases import REFUSAL, SeamCase
from tests.test_source_owner_api import workspace as local_workspace

SCRIPT = "normalize_sources.py"


def cases(workspace: Path):
    root = local_workspace(workspace.parent / "normalization-local")
    record = {"id": "local-html", "kind": "html", "raw_paths": ["raw/web/source-0.html"], "status": "discovered"}
    (root / "sources" / "manifest.jsonl").write_text(json.dumps(record) + "\n")
    for_api = workspace.parent / "normalization-api"
    shutil.copytree(root, for_api)
    missing = local_workspace(workspace.parent / "normalization-missing")
    return (
        SeamCase(name="normalize_dry_run", argv=("--project-root", str(root), "--dry-run", "--all", "--format", "json"),
                 call=lambda module: module.run_normalization_report(root, all_sources=True, dry_run=True), volatile=("timestamp",)),
        SeamCase(name="normalize_write", argv=("--project-root", str(root), "--source-id", "local-html", "--format", "json"),
                 call=lambda module: module.run_normalization_report(for_api, source_ids=["local-html"]), volatile=("timestamp",)),
        SeamCase(name="unknown_source", argv=("--project-root", str(root), "--source-id", "unknown", "--format", "json"),
                 call=lambda module: module.run_normalization_report(root, source_ids=["unknown"]),
                 expect=REFUSAL, error_code="SOURCE_UNKNOWN"),
        SeamCase(name="missing_manifest", argv=("--project-root", str(missing), "--format", "json"),
                 call=lambda module: module.run_normalization_report(missing), expect=REFUSAL, error_code="MANIFEST_MISSING"),
    )
