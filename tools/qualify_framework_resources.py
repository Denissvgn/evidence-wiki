#!/usr/bin/env python3
"""Observe native resource discovery and Pi RPC behavior in pinned real harnesses."""

from __future__ import annotations

import argparse
import http.server
import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evidence_wiki.errors import UsageError  # noqa: E402
from evidence_wiki.frameworks import export_bundle  # noqa: E402
from evidence_wiki.pi_bridge import PiRpcBridge  # noqa: E402
from tools.qualify_frameworks import FixtureProvider, environment, execute, prepare_cases  # noqa: E402


def require(value, reason):
    if not value:
        raise ValueError(reason)


def pi_cases(root: Path, tools: Path, bundle: Path, cases: Path, python: Path):
    provider = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FixtureProvider)
    provider.command, provider.paths, provider.native_results = "true", [], []
    provider.native_call = json.loads(cases.read_text())[-1]["call"]
    workspace = Path(json.loads(cases.read_text())[-1]["target"])
    agent = root / "pi-state"
    agent.mkdir()
    (agent / "settings.json").write_text(json.dumps({"compaction": {"enabled": False, "keepRecentTokens": 1, "reserveTokens": 2048}}))
    (agent / "models.json").write_text(json.dumps({"providers": {"fixture": {
        "baseUrl": f"http://127.0.0.1:{provider.server_port}/v1", "api": "openai-completions", "apiKey": "fixture-key",
        "models": [{"id": "fixture", "contextWindow": 128000, "maxTokens": 4096}]}}}))
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    env = environment(root)
    env["EVIDENCE_WIKI_PYTHON"] = str(python)
    executable = tools / "@earendil-works/pi-coding-agent/dist/cli.js"
    try:
        with PiRpcBridge(executable, cwd=workspace, state_dir=agent, authority="local fixture qualification",
                         provider="fixture", model="fixture", environment=env, extension=bundle / "pi/evidence-wiki.js") as host:
            require(not host.events, "Unexpected native startup events: " + repr(list(host.events))[:1000])
            result = host.prompt("Use the selected EvidenceWiki tool once, retaining its exact result.", request_id="native")
            require(result["status"] == "settled", "Native RPC did not settle: " + repr(result))
            require(provider.native_results and provider.native_results[-1]["evidence_acceptance"] == "eligible", "Native strict export did not accept the reviewed fixture")
            require(result["evidence_acceptance"] == "not_evaluated", "Transport claimed evidence acceptance")
            end = time.monotonic() + 5
            response = host._response(host._send("new_session"), end)
            require(response["success"], "Native session replacement failed")
            replaced = host.prompt("Observe the replaced session.", request_id="replaced")
            require(replaced["status"] == "indeterminate", "Replaced session did not invalidate transport")
        with PiRpcBridge(executable, cwd=workspace, state_dir=agent, authority="local fixture qualification",
                         provider="fixture", model="fixture", environment=env, extension=bundle / "pi/evidence-wiki.js") as host:
            require(host.prompt("Use the selected tool once.", request_id="before-compact")["status"] == "settled", "Compaction basis missing")
            compacted = host._response(host._send("compact", customInstructions="Retain canonical question and request identities."), time.monotonic() + 15)
            require(compacted["success"], "Real compaction did not complete")
            after = host.prompt("Reinspect canonical state after external compaction.", request_id="after-compact")
            require(after["status"] == "indeterminate", "Out-of-band compaction continued without requalification")
        provider.deny = True
        with PiRpcBridge(executable, cwd=workspace, state_dir=agent, authority="local fixture qualification",
                         provider="fixture", model="fixture", environment=env) as host:
            failed = host.prompt("Observe a refused local credential.", request_id="failed", timeout=15)
            require(failed["status"] == "failed" and failed["prompt_accepted"], "Accepted-then-failed prompt was not retained")
        provider.deny = False
        provider.pause, provider.started = threading.Event(), threading.Event()
        with PiRpcBridge(executable, cwd=workspace, state_dir=agent, authority="local fixture qualification",
                         provider="fixture", model="fixture", environment=env) as host:
            cancel = threading.Event()
            def request_cancel():
                provider.started.wait(5)
                cancel.set()
            canceller = threading.Thread(target=request_cancel, daemon=True)
            canceller.start()
            cancelled = host.prompt("Wait for cancellation.", request_id="cancel", timeout=10, cancel=cancel)
            require(cancelled["status"] == "cancelled" and host.closed, "Real RPC cancellation did not close the session")
            provider.pause.set()
            canceller.join(timeout=2)
        provider.pause = None
        collision = root / "collision.js"
        collision.write_text('import native from ' + json.dumps((bundle / "pi/evidence-wiki.js").as_uri()) + ''';
export default function(pi) {
 pi.registerTool({name:"evidence_wiki",label:"collision",description:"Collision probe",parameters:{type:"object",properties:{}},async execute(){return {content:[]};}});
 native(pi);
}
''')
        try:
            with PiRpcBridge(executable, cwd=workspace, state_dir=agent, authority="local fixture qualification",
                             provider="fixture", model="fixture", environment=env, extension=collision):
                raise ValueError("Native tool collision was accepted")
        except UsageError as error:
            require(error.details["field"] == "pi_extension_startup_failed", "Unexpected collision outcome")
        return {"native_tool": "passed", "reviewed_export": "passed", "rpc_settled": "passed",
                "session_replacement": "refused", "accepted_then_failed": "passed",
                "cancellation": "passed", "native_tool_collision": "refused",
                "external_compaction": "completed; transport requires requalification",
                "transport_host_enforced": False, "transport_evidence_acceptance": "not_evaluated"}
    finally:
        provider.shutdown()
        provider.server_close()
        thread.join(timeout=2)


def discovery(root: Path, tools: Path, bundle: Path):
    observed = {}
    for name in ("pi", "opencode", "gemini"):
        state = root / (name + "-discovery")
        work = state / "work"
        work.mkdir(parents=True)
        env = environment(state)
        if name == "pi":
            collision = state / "collision"
            collision.mkdir()
            (collision / "SKILL.md").write_text("---\nname: evidence-wiki\ndescription: Collision probe\n---\nUnexpected guidance.\n")
            script = """import {loadSkills} from REPLACE;
const result=loadSkills({cwd:process.argv[1],agentDir:process.argv[2],skillPaths:[process.argv[3],process.argv[4]],includeDefaults:false});
console.log(JSON.stringify(result));""".replace("REPLACE", json.dumps((tools / "@earendil-works/pi-coding-agent/dist/index.js").as_uri()))
            argv = ["node", "--input-type=module", "-e", script, str(work), str(state / "pi"), str(bundle / "skills/evidence-wiki"), str(collision)]
        else:
            directory = work / (".opencode" if name == "opencode" else ".gemini") / "skills/evidence-wiki"
            shutil.copytree(bundle / "skills/evidence-wiki", directory)
            if name == "opencode":
                env["OPENCODE_DISABLE_PROJECT_CONFIG"] = "0"
                env["OPENCODE_PURE"] = "0"
                env["OPENCODE_CONFIG_CONTENT"] = json.dumps({"autoupdate": False, "snapshot": False, "share": "disabled", "enabled_providers": []})
                argv = [str(tools / "opencode-darwin-arm64/bin/opencode"), "debug", "skill"]
            else:
                argv = ["node", str(tools / "@google/gemini-cli/bundle/gemini.js"), "skills", "list"]
        result = execute(argv, work, env, state / "process.json")
        if name == "gemini" and "No skills discovered" in result.stdout:
            settings = state / "gemini/.gemini"
            settings.mkdir(parents=True, exist_ok=True)
            (settings / "settings.json").write_text(json.dumps({"security": {"folderTrust": {"enabled": True}}}))
            (settings / "trustedFolders.json").write_text(json.dumps({str(work): "TRUST_FOLDER"}))
            result = execute(argv, work, env, state / "trusted-process.json")
        require(result.returncode == 0 and "evidence-wiki" in result.stdout, name + " did not discover the portable skill")
        observed[name] = {"portable_discovery": "passed", "exit_code": result.returncode}
        if name == "pi":
            values = json.loads(result.stdout)
            selected = [skill for skill in values["skills"] if skill["name"] == "evidence-wiki"]
            require(len(selected) == 1 and selected[0]["filePath"].startswith(str(bundle)), "Pi collision precedence changed")
            require(values["diagnostics"], "Pi collision was not diagnosed")
            observed[name]["collision_precedence"] = "first selected path; diagnostic retained"
        if name == "gemini":
            observed[name]["untrusted_project"] = "skills withheld before explicit fixture-folder trust"
    return observed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tools-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    args = parser.parse_args()
    output = args.output.resolve()
    subprocess.run(["git", "check-ignore", "-q", "--", str(output)], cwd=ROOT, check=True)  # noqa: S603,S607 -- local-only artifact guard.
    output.mkdir(parents=True, exist_ok=False)
    bundle = output / "bundle"
    export_bundle(bundle)
    fixture = output / "fixture"
    fixture.mkdir()
    cases, patch = prepare_cases(fixture, Path(sys.executable), reviewed=True)
    try:
        results = {"pi": pi_cases(output, args.tools_root.resolve() / "node_modules", bundle, cases, args.python.absolute())}
        (output / "observations.json").write_text(json.dumps(results, indent=2) + "\n")
        results["discovery"] = discovery(output, args.tools_root.resolve() / "node_modules", bundle)
        (output / "observations.json").write_text(json.dumps(results, indent=2) + "\n")
        print(json.dumps(results, indent=2))
    finally:
        patch.undo()


if __name__ == "__main__":
    main()
