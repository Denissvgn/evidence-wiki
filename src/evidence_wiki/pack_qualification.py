"""Content-bound canonical validation observations, separate from domain judgments."""

from __future__ import annotations

import re
from pathlib import Path

from . import __version__
from ._pack_io import capture_pack, yaml_document
from .agent_resources import installation_metadata
from .pack_authoring_contracts import VALIDATION, checked, digest
from .pack_authoring_store import load_draft, record
from .pack_discovery import owner, validation_observation


def qualify(candidate):
    from .pack_authoring import guidance_files

    path = Path(candidate).expanduser().absolute()
    before = capture_pack(path)
    guidance_files(before.files)
    snapshot, report, checker = validation_observation(path, before.tree_sha256)
    try:
        overlay = owner("_domain_pack_lifecycle").overlay_sha256(yaml_document(snapshot.files["research.overlay.yml"]))
    except Exception:
        overlay = None
    checks = []
    for row in report["checks"]:
        # Check IDs and statuses preserve the canonical categories. Free-form
        # diagnostics can contain source strings or private temporary paths.
        checks.append({"id": row["id"], "status": row["status"],
            "files": [name for name in row.get("files", []) if re.fullmatch(r"[a-zA-Z0-9_./-]{1,512}", name) and not name.startswith("/")],
            "details": "canonical check passed" if row["status"] == "pass" else "canonical check failed; use pack validate for local details"})
    value = {"schema_version": VALIDATION, "candidate": str(path),
        "identity": {"tree_sha256": snapshot.tree_sha256, "overlay_sha256": overlay},
        "checker_sha256": checker, "package_version": __version__,
        "research_contract_version": installation_metadata()["research_contract_version"],
        "ok": report["ok"], "checks": checks,
        "smoke": {"ok": report.get("smoke_validation", {}).get("ok", False),
            "summary": report.get("smoke_validation", {}).get("summary", {}),
            "issue_count": len(report.get("smoke_validation", {}).get("issues", []))},
        "authority": "caller_local_structural_observation", "semantic_adequacy": "not_evaluated"}
    return checked(value, VALIDATION)


def qualify_draft(root):
    root, candidate, _draft = load_draft(root)
    result = qualify(candidate)
    saved = record(root, "validation-" + digest(result) + ".json", result)
    return {"validation": result, "record": saved}
