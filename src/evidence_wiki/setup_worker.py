"""Fixed installed owners for bounded local setup processes; no caller command execution."""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path

from ._pack_io import canonical, json_document
from ._script_host import shared_assets_root
from .pack_discovery import owner
from .planning_contracts import digest
from .source_inputs import SourceView

MUTATIONS = ("initialize", "intake", "coverage", "sources", "inventory", "normalize")
STEPS = ("initialize", "doctor", "smoke", "intake", "coverage", "sources", "inventory", "normalize", "lint", "source_status", "computation", "strict")
REQUIRED = {"doctor", "smoke", "lint"}


def local_paths(plan):
    return [{**row, "path": row["destination_root"].rstrip("/") + "/setup-" + digest(row["source_id"])[:20]
             + Path(row["input"].get("path", "")).suffix.lower()}
            for row in plan["sources"]["routes"] if row["route"] == "local_file" and row["state"] == "planned"]


def inventory_records(root, config, plan):
    module = owner("source_inventory")
    module.require_host_intake(config)
    records, warnings, summary = module.build_records(root, config, {})
    allowed = {row["path"] for row in local_paths(plan)}
    for row in records:
        paths = set(row.get("raw_paths") or [])
        if not paths or not paths <= allowed:
            raise ValueError("inventory_outside_delivered_scope")
    return records, warnings, summary


def execute(operation, plan, clock):
    selection = plan["bindings"]["target"]["target"]
    root = Path(selection["writable_root"]) / selection["relative_path"]
    config = plan["initialization"]["effective_config"]
    if operation == "initialize":
        init = owner("init_research_workspace")
        profile = plan["profile"]["workspace_init"]
        project = profile["project"]
        pack = profile["domain_pack"]
        options = init.InitOptions(starter_root=shared_assets_root() / "workspace-template", target=root,
            scope_root=Path(selection["writable_root"]), project_name=project["name"],
            project_description=project["description"], owner_goal=project["owner_goal"], language=project["language"],
            domain_pack=pack.get("path") if pack["enabled"] else None, profile=profile,
            profile_path=None, dry_run=False, force=False)
        init.initialize_workspace(options)
        return {"status": "passed", "config_sha256": digest(init.load_yaml(root / "research.yml", "workspace"))}
    if operation == "doctor":
        result = owner("doctor").build_report(root, inspect_registrations=False)
        return {"status": "failed" if result["verdict"] == "missing" else "passed", "verdict": result["verdict"],
                "checks": [{key: row[key] for key in ("id", "status", "required")} for row in result["checks"]]}
    if operation in {"smoke", "lint"}:
        result = owner("smoke_validate_workspace").run_checks(root) if operation == "smoke" else owner("lint").run_checks(root, config)
        ok = result["ok"] if operation == "smoke" else result["workspace_health"]["materially_valid"]
        return {"status": "passed" if ok else "failed", "issues": [
            {key: row[key] for key in ("severity", "category") if key in row} for row in result["issues"][:128]],
            "issue_count": len(result["issues"])}
    if operation == "intake":
        result = owner("intake_questions").run_intake_document(root, plan["questions"]["batch"], dry_run=False,
            from_file_label="frozen research requirements", _observed_at=owner("_evidence_authority").timestamp(clock))
        return {"status": "passed", "counts": result["counts"],
                "created": [{key: row[key] for key in ("slug", "path", "item_index")} for row in result["created"]]}
    if operation == "coverage":
        module = owner("coverage_manifest")
        for row in plan["coverage"]:
            module.run_init_document(root, config, slug=row["question_slug"], template=row["template_document"])
        return {"status": "passed", "questions": [row["question_slug"] for row in plan["coverage"]]}
    if operation == "sources":
        from .source_delivery import deliver_local

        delivered = [deliver_local(target=root, input=row["input"], path=row["path"], source_id=row["source_id"], question_ids=row["question_ids"])
                     for row in local_paths(plan)]
        return {"status": "passed", "delivered": delivered}
    if operation == "inventory":
        records, warnings, summary = inventory_records(root, config, plan)
        owner("source_inventory").write_manifest(root / config["sources"]["manifest_path"], records)
        return {"status": "passed", "sources": [{"source_id": row["id"], "raw_paths": row.get("raw_paths", [])} for row in records],
                "warning_count": len(warnings)}
    if operation == "normalize":
        module = owner("normalize_sources")
        records, _, _ = inventory_records(root, config, plan)
        eligible = module.eligible_records(root, records, ())
        ids = [item.record["id"] for item in eligible]
        args = module.parse_args(["--project-root", str(root), "--format", "json", *[arg for sid in ids for arg in ("--source-id", sid)]])
        if ids:
            code = module.run_normalization(args)
        else:
            code = 0
        return {"status": "passed" if code == 0 else "failed", "exit_code": code, "selected": ids,
                "unsupported": [row["id"] for row in records if row["id"] not in ids]}
    if operation == "source_status":
        from .source_readiness import inspect_sources

        view = SourceView(root)
        rows = []
        for source in local_paths(plan):
            observed = inspect_sources(view, source_paths=[source["path"]])
            suffixes = {"html": {".html", ".htm"}, "pdf": {".pdf"}, "csv": {".csv"}, "json": {".json"},
                        "plain_text": {".txt"}, "markdown": {".md", ".markdown"}}
            format_ok = Path(source["path"]).suffix in suffixes[source["requirements"]["output_format"]]
            rows.append({"input_id": source["source_id"], "question_ids": source["question_ids"], "requirements": source["requirements"],
                         "observations": observed, "format_satisfied": format_ok, "semantic_scope": "not_evaluated",
                         "usable": format_ok and bool(observed) and all(
                             row["usability"] == "usable" if source["requirements"]["needs_complete"] else row["usability"] in {"usable", "partial"}
                             for row in observed)})
        view.finish()
        return {"status": "passed", "sources": rows}
    if operation == "computation":
        if plan["computation"] is None:
            return {"status": "not_selected", "executed": False, "result_id": None}
        evaluation_clock = plan["computation"]["clock"].get("as_of") or clock
        try:
            result = owner("_computation_service").run(root, "check", as_of=evaluation_clock)
        except Exception as error:
            reason = getattr(error, "details", {}).get("reason", "computation_unavailable")
            import re
            from datetime import datetime

            if not isinstance(reason, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,95}", reason):
                reason = "computation_unavailable"
            return {"status": "blocked", "executed": True, "result_id": None, "definition_id": plan["computation"]["definition_id"],
                    "clock": {"as_of": datetime.fromisoformat(evaluation_clock.replace("Z", "+00:00")).isoformat()},
                    "reasons": [reason], "invariants": [], "findings": [], "dispatch_authorized": False}
        return {"status": result["status"], "executed": True, "result_id": result["result_id"],
                "definition_id": result["definition_id"], "clock": result["clock"],
                "findings": result["findings"], "invariants": result["invariants"], "dispatch_authorized": False}
    if operation == "strict":
        module = owner("_strict_evidence")
        policy = module.resolve_policy(root, config)
        if policy != plan["strict"].get("policy"):
            raise ValueError("strict_policy_changed")
        authority = "not_configured"
        if config.get("evidence_trust"):
            try:
                owner("_evidence_authority").load_trust(root, config, owner("_evidence_authority").timestamp(clock))
                authority = "host_policy_valid"
            except Exception:
                authority = "unavailable_or_invalid"
        return {"status": "passed", "policy_sha256": digest(policy) if policy else None,
                "effective_assurance": "artifact_checked" if policy else None, "authority": authority,
                "reviewer_authenticated": False, "host_enforcement": "not_established", "claims_verified": False}
    raise ValueError("setup_operation_unknown")


def main():
    try:
        request = json_document(sys.stdin.buffer.read(1_048_577))
        if sys.argv[1:] != [request["operation"]] or request["operation"] not in STEPS:
            raise ValueError("setup_operation_unknown")
        from .planning import plan_identity
        from .planning_inputs import installation_basis

        plan = request["plan"]
        installed = installation_basis()
        if plan_identity(plan) != plan["plan_id"] or installed != {key: plan["bindings"][key] for key in installed}:
            raise ValueError("setup_worker_basis_changed")
        # Owners may print source-derived diagnostics. Only the explicit result
        # leaves this process; bounded parent pipes do not retain raw source text.
        with open(__import__("os").devnull, "w", encoding="utf-8", newline="\n") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            result = execute(request["operation"], request["plan"], request["clock"])
        raw = canonical(result)
        if len(raw) > 60000:
            raise ValueError("setup_result_bound")
        print(raw.decode())
        return 0
    except (Exception, SystemExit) as error:
        code = getattr(error, "error_code", "SETUP_OWNER_FAILED")
        print(json.dumps({"status": "failed", "error_code": code, "reason": "owner_refused_or_unavailable"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
