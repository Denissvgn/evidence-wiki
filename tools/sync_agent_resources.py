#!/usr/bin/env python3
"""Export installed resource schemas/examples from their canonical owners."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import yaml  # noqa: E402

from evidence_wiki import __version__  # noqa: E402
from evidence_wiki._agent_catalog import CATALOG_PATH, resource_paths  # noqa: E402
from evidence_wiki._contract import LIBRARY_API_VERSION  # noqa: E402
from evidence_wiki._script_host import load_packaged_script  # noqa: E402
from evidence_wiki.frameworks import portable_bundle, tool_schemas, validate_bundle  # noqa: E402
from evidence_wiki.onboarding_schemas import schema_document, schema_ids  # noqa: E402


def encoded(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def exports() -> dict[str, bytes]:
    """Build exact exports; reading this tool never changes generated files."""
    paths = resource_paths()
    strict = load_packaged_script(ROOT, "_strict_contract")
    computation = load_packaged_script(ROOT, "_computation_contract")
    initializer = load_packaged_script(ROOT, "init_research_workspace")
    schemas = {key: schema_document(key) for key in schema_ids()}
    schemas.update(strict.schema_documents())
    schemas.update(computation.schemas())
    schemas.update(tool_schemas())
    output = {paths[key]: encoded(value) for key, value in schemas.items()}
    guide_bytes = (ROOT / paths["guide/bootstrap/v1"]).read_bytes()
    bundle = portable_bundle({"content": guide_bytes.decode(), "sha256": hashlib.sha256(guide_bytes).hexdigest()},
                             (ROOT / "workspace-template/docs/frameworks/pi.js").read_text(), (ROOT / "LICENSE").read_text())
    validate_bundle(bundle)
    output[paths["framework/bundle/v1"]] = encoded(bundle)
    policy = {
        "schema_version": strict.POLICY_SCHEMA, "policy_id": "reviewed-evidence", "revision": "1",
        "assurance": "artifact_checked", "claims_path": "claims.json",
        "instructions": {"docs/installed-agent.md": "sha256:" + hashlib.sha256(
            (ROOT / paths["guide/bootstrap/v1"]).read_bytes()).hexdigest()},
        "rubric": {"id": "evidence-review", "revision": "1", "criteria": {
            "support": "Confirm the exact evidence supports the claim and any derivation.",
            "source_suitability": "Confirm source authority and fitness for this question.",
            "scope": "Confirm population, jurisdiction and other scope limits.",
            "time": "Confirm capture and claim dates are suitable for the question.",
            "units": "Confirm quantities, units and rounding have the intended meaning.",
            "counterevidence": "Consider contrary evidence and retain material uncertainty.",
        }},
        "human_review": False, "max_source_age_seconds": 2_592_000, "max_review_age_seconds": 604_800,
    }
    strict.policy_document(policy)
    output[paths["example/strict-policy/v1"]] = encoded(policy)
    profile = {"workspace_init": {
        "schema_version": initializer.PROFILE_SCHEMA_VERSION, "target_path": "workspace",
        "project": {"name": "research", "description": "Investigate the original questions using retained evidence.",
                    "owner_goal": "Return supported answers and explicit evidence gaps.", "language": "en"},
        "domain_guidance": {"mode": "none", "rationale": "Start generic until evidence justifies reusable guidance."},
        "domain_pack": {"enabled": False},
        "raw": {"immutable": True, "source_roots": ["raw/papers", "raw/links"]},
        "claim_strictness": "structured_claims", "ingest": {"claim_extraction": True},
        "outputs": {"supported_formats": ["markdown", "json"]},
        "integrations": {"git": {"snapshot_user_edits": "explicit"}},
        "assumptions": ["Review scope and original questions before initialization."],
        "skipped_decisions": ["No network research during initialization."],
    }}
    initializer.validate_profile(profile["workspace_init"])
    output[paths["example/init-profile/v1"]] = encoded(profile)
    for name, directory in [("pack", ROOT / "domain-packs/general-science"), *(
            (name, ROOT / "workspace-template/docs/computation-examples" / name)
            for name in ("sample-benchmark", "sample-portfolio", "sample-filing"))]:
        files = {p.relative_to(directory).as_posix(): p.read_text(encoding="utf-8")
                 for p in sorted(directory.rglob("*")) if p.is_file()}
        output[paths[f"example/{name}/v1"]] = encoded({"files": files})
    metadata = yaml.safe_load((ROOT / "workspace-template/workspace-system.yml").read_text())["workspace_system"]
    entries = {}
    for key, relative in paths.items():
        data = output.get(relative)
        if data is None:
            data = (ROOT / relative).read_bytes()
        if len(data.decode("utf-8")) > 65_536:
            raise ValueError(f"Resource exceeds content bound: {key}")
        entries[key] = {
            "id": key, "version": key.rsplit("/v", 1)[1] + ".0", "path": relative,
            "sha256": hashlib.sha256(data).hexdigest(),
            "media_type": ("application/schema+json" if key in schemas else "text/markdown"
                           if key.startswith("guide/") else "text/x-python" if key.startswith("script/")
                           else "application/json"),
        }
    output[CATALOG_PATH] = encoded({
        "schema_version": "evidence-agent-catalog/v1", "resources": entries,
        "runtime_files": sorted(path.name for path in (ROOT / "workspace-template/scripts").glob("*.py")),
        "installation": {"package_version": __version__, "starter_version": metadata["starter_version"],
                         "library_api_version": LIBRARY_API_VERSION,
                         "profile_schema_version": initializer.PROFILE_SCHEMA_VERSION,
                         "research_contract_version": metadata["compatible_research_yml_contract"]},
    })
    return output


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    changed = []
    for relative, content in exports().items():
        path = ROOT / relative
        if path.is_file() and path.read_bytes() == content:
            continue
        changed.append(relative)
        if not args.check:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
    print(json.dumps({"current": not changed, "changed": changed}))
    return int(args.check and bool(changed))


if __name__ == "__main__":
    raise SystemExit(main())
