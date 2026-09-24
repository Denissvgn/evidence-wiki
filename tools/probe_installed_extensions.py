"""Exercise scoped lifecycle owners and DOCX retrieval from a selected installation."""

from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import io
import json
import runpy
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from evidence_wiki import Onboarding, Workspace
from evidence_wiki._pack_io import canonical, capture_pack
from evidence_wiki._script_host import shared_assets_root
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.pack_discovery import owner


def require(condition, message):
    if not condition:
        raise ValueError(message)


def command(*args):
    result = subprocess.run([sys.executable, "-I", "-m", "evidence_wiki.cli", *map(str, args)],  # noqa: S603 -- fixed installed CLI.
        capture_output=True, text=True, timeout=60, check=False, encoding="utf-8")
    require(result.returncode == 0, result.stdout + result.stderr)
    return result.stdout


def initialize(root):
    command("init", "--target", root, "--project-name", "retained-observations", "--project-description", "Observe exact input text",
            "--owner-goal", "Preserve evidence and uncertainty")
    request = root.parent / "questions.json"
    request.write_bytes(canonical({"schema_version": "1.0", "questions": [
        {"id": "q1", "question": "What is supported?", "priority": "high", "origin": "caller"}]}))
    command("questions", "add", "--target", root, "--from-file", request, "--format", "json")


def run(root, fixture):
    root.mkdir(parents=True, exist_ok=False)
    target = root / "workspace"
    initialize(target)
    original = runpy.run_path(str(fixture))["document"]()
    source = target / "raw/papers/observations.docx"
    source.write_bytes(original)
    source.with_name(source.name + ".provenance.yml").write_text(yaml.safe_dump({
        "checksum": "sha256:" + hashlib.sha256(original).hexdigest(), "retrieved_by": "local_setup",
        "retrieved_at": datetime.now(timezone.utc).isoformat(), "source_type": "local_file", "license": "MIT"}), encoding="utf-8", newline="\n")
    with contextlib.redirect_stdout(io.StringIO()):
        require(owner("source_inventory").main(["--project-root", str(target), "--format", "json", "--report"]) == 0, "DOCX inventory")
        records = [json.loads(row) for row in (target / "sources/manifest.jsonl").read_text(encoding="utf-8").splitlines()]
        record = next(row for row in records if row["kind"] == "docx")
        require(owner("normalize_sources").main(["--project-root", str(target), "--source-id", record["id"], "--format", "json"]) == 0, "DOCX normalization")
    with Workspace.open(target) as workspace:
        require(workspace.normalize.verify([record["id"]])["overall_result"] == "verified", "DOCX verification")
    retrieved = owner("serve_mcp").ResearchWikiMcpServer(target).call_tool_payload("query_index", {"query": "Northern observations", "scope": "normalized", "limit": 5})
    require("Northern observations" in json.dumps(retrieved) and source.read_bytes() == original, "DOCX retrieval/original bytes")
    require(owner("_docx_capture").extract(original)["structured"]["tables"][0]["rows"][1] == ["007", "0.10"], "DOCX exact cells")
    with Onboarding.open(allowed_roots=[root], allow=["migration_apply", "composition_apply", "fleet_apply", "transition_apply", "instructions_apply", "instructions_remove"]) as host:
        require(host.bootstrap()["payload"]["installation"]["library_api_version"] == "13", "API generation")
        require(host.contracts()["mcp"]["protocol_version"] == "2024-11-05", "MCP discovery")
        candidate = shared_assets_root() / "domain-packs/general-science"
        request = {"schema_version": "evidence-pack-migration-request/v1", "target": str(target), "path": str(candidate),
            "catalog": None, "revision": None, "rationale": "Attach reviewed guidance", "keep_local": [], "accept_pack": [],
            "mappings": {"policies": {}, "request_kinds": {}, "templates": {}}}
        try:
            prepared = host.migration_plan(request)
        except EvidenceWikiError as error:
            require("conflicts" in error.details, str(error))
            request["accept_pack"] = [row["target"] for row in error.details["conflicts"]]
            prepared = host.migration_plan(request)
        require(host.migration_apply(prepared)["status"] == "applied", "First attachment")
        require(host.migration_apply(prepared)["status"] == "already_applied", "Migration replay")
        require(source.read_bytes() == original, "Migration retained DOCX")
        changed_candidate = root / "replay-candidate/general-science"
        shutil.copytree(candidate, changed_candidate)
        changed_guidance = changed_candidate / "claims.md"
        changed_guidance.write_text(changed_guidance.read_text(encoding="utf-8") + "\nDifferent candidate input.\n", encoding="utf-8", newline="\n")
        for changed in ("candidate", "rationale"):
            altered = copy.deepcopy(prepared)
            if changed == "candidate":
                altered["request"]["path"] = str(changed_candidate)
                altered["candidate_sha256"] = capture_pack(changed_candidate).tree_sha256
            else:
                altered["request"]["rationale"] = "Different migration intent"
            altered["plan_id"] = owner("_pack_revision_impact").digest({k: v for k, v in altered.items() if k != "plan_id"})
            try:
                host.migration_apply(altered)
            except EvidenceWikiError as error:
                require(error.error_code == "ONBOARDING_PLAN_STALE", "Migration replay refusal type")
            else:
                raise ValueError("A changed request was represented as already applied")
        members = []
        for alias in ("north", "south"):
            member = root / alias / "general-science"
            shutil.copytree(candidate, member)
            members.append({"path": str(member), "alias": alias, "applicability": "Questions explicitly assigned to " + alias})
        composition = host.composition_plan({"schema_version": "evidence-pack-composition-request/v1", "name": "related-observations",
            "version": "1", "scope": "Explicit related observations", "members": members})
        compiled = host.composition_apply(composition, output=root / "composed")
        owner("_domain_pack_lifecycle").file_inventory(Path(compiled["candidate"]))
        revised = root / "candidate/general-science"
        shutil.copytree(candidate, revised)
        path = revised / "claims.md"
        path.write_text(path.read_text(encoding="utf-8") + "\nKeep limits explicit.\n", encoding="utf-8", newline="\n")
        fleet = host.fleet_plan({"schema_version": "evidence-fleet-revision-request/v1", "candidate": {"path": str(revised), "catalog": None, "revision": None},
            "rationale": "Review one explicitly selected workspace", "workspaces": [{"target": str(target), "keep_local": [], "accept_pack": []}]})
        require(host.fleet_apply({"schema_version": "evidence-fleet-revision-apply/v1", "plan": fleet, "targets": [str(target)]})["status"] == "complete", "Fleet application")
        matrix = json.loads(host.resource("framework/compatibility/v1")["content"])
        for row in matrix["frameworks"]:
            native_root = root / row["id"]
            native_root.mkdir()
            native = host.instructions_plan({"schema_version": "evidence-native-instructions-request/v1", "framework": row["id"],
                "version": row["version"], "scope": "project", "root": str(native_root)})
            require(host.instructions_apply(native)["status"] == "installed", "Native installation")
            require(host.instructions_remove(native)["status"] == "removed", "Native removal")
        control = root / "control"
        control.mkdir()
        transition = host.transition_plan({"schema_version": "evidence-host-transition-request/v1", "transition_id": "subsequent-research",
            "control_root": str(control), "target": str(target), "previous_session": None,
            "next_session": {"agent_id": "host", "orchestration_id": "next-session"}, "setup_plan": None, "revision_plan": None, "reevaluations": []})
        require(host.transition_apply(transition)["status"] == "complete", "Host transition")
        require(host.transition_apply(transition)["status"] == "already_complete", "Host transition replay")
        command("orchestrate", "next", "--target", target, "--orchestration-id", "next-session", "--agent-id", "host", "--format", "json")
        session = json.loads((target / "runs/orchestrations/next-session/session.json").read_text(encoding="utf-8"))
        require(session["schema_version"] == "1.1" and session["host_transition_id"] == transition["plan_id"][7:], "Correlated controller continuation")
    messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "probe", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "onboarding_bootstrap", "arguments": {}}}]
    transport = subprocess.run([sys.executable, "-I", "-m", "evidence_wiki.cli", "serve-onboarding-mcp"],
        input="\n".join(json.dumps(row) for row in messages) + "\n", text=True, capture_output=True, timeout=30, check=False, encoding="utf-8")  # noqa: S603 -- fixed installed module.
    require(transport.returncode == 0 and not transport.stderr, "Installed MCP process")
    responses = [json.loads(row) for row in transport.stdout.splitlines()]
    require(len(responses) == 2 and not responses[1]["result"]["isError"], "Installed MCP bootstrap")
    return {"scoped_onboarding": "passed", "onboarding_stdio": "passed", "docx_capture_to_retrieval": "passed",
            "identity_migration": "passed", "replay_input_binding": "passed", "pinned_composition": "passed", "fleet_revision": "passed",
            "host_transition": "passed", "native_instruction_lifecycle": "passed", "live_models": "not_invoked"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--docx-fixture", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.root.resolve(), args.docx_fixture.resolve()), sort_keys=True))


if __name__ == "__main__":
    main()
