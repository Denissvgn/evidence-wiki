#!/usr/bin/env python3
"""Qualify pinned real harnesses using local provider fixtures and canonical owners."""

from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import os
import shlex
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class FixtureProvider(http.server.BaseHTTPRequestHandler):
    """A local provider oracle; it is not an RPC mock or a live model."""

    def log_message(self, *_args):
        pass

    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 <= size <= 4_194_304:
            self.send_error(413)
            return
        body = json.loads(self.rfile.read(size))
        self.server.paths.append(self.path)
        if getattr(self.server, "pause", None) is not None:
            self.server.started.set()
            self.server.pause.wait(5)
            return
        if getattr(self.server, "deny", False):
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"Fixture credential refusal","type":"authentication_error"}}')
            return
        google = "contents" in body
        messages = body.get("contents", body.get("messages", []))
        for message in messages:
            if message.get("role") == "tool" and getattr(self.server, "native_call", None):
                self.server.native_results.append(json.loads(message["content"]))
        tool_done = any(message.get("role") == "tool" or any("functionResponse" in part for part in message.get("parts", []))
                        for message in messages)
        tool_done |= not bool(body.get("tools"))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        if google:
            part = {"text": "Canonical operations completed."} if tool_done else {
                "functionCall": {"name": "run_shell_command", "args": {
                    "command": self.server.command, "description": "Run the selected local EvidenceWiki cases"}}}
            data = {"candidates": [{"content": {"role": "model", "parts": [part]}, "finishReason": "STOP", "index": 0}],
                    "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1, "totalTokenCount": 2}}
            self.wfile.write(("data: " + json.dumps(data) + "\n\n").encode())
        else:
            native = getattr(self.server, "native_call", None)
            arguments = native or {"command": self.server.command, "description": "Run the selected local EvidenceWiki cases"}
            if not native and getattr(self.server, "bash_timeout", None) is not None:
                arguments["timeout"] = self.server.bash_timeout
            delta = {"content": "Canonical operations completed."} if tool_done else {"tool_calls": [{
                "index": 0, "id": "call_fixture", "type": "function", "function": {
                    "name": "evidence_wiki" if native else "bash", "arguments": json.dumps(arguments)}}]}
            for change, reason in (({"role": "assistant", **delta}, None), ({}, "stop" if tool_done else "tool_calls")):
                data = {"id": "chatcmpl_fixture", "object": "chat.completion.chunk", "created": 1,
                        "model": "fixture", "choices": [{"index": 0, "delta": change, "finish_reason": reason}]}
                self.wfile.write(("data: " + json.dumps(data) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
        self.close_connection = True


def environment(root: Path) -> dict[str, str]:
    return {"PATH": os.environ["PATH"], "LANG": "en_US.UTF-8", "TERM": "dumb", "NO_COLOR": "1",
            "PI_CODING_AGENT_DIR": str(root / "pi"), "PI_OFFLINE": "1", "PI_TELEMETRY": "0",
            "GEMINI_CLI_HOME": str(root / "gemini"), "GEMINI_CLI_NO_RELAUNCH": "1",
            "GEMINI_CLI_SYSTEM_SETTINGS_PATH": str(root / "absent-system-settings.json"),
            "XDG_CONFIG_HOME": str(root / "config"), "XDG_DATA_HOME": str(root / "data"),
            "XDG_CACHE_HOME": str(root / "cache"), "XDG_STATE_HOME": str(root / "state"),
            "OPENCODE_DISABLE_AUTOUPDATE": "1", "OPENCODE_DISABLE_MODELS_FETCH": "1",
            "OPENCODE_DISABLE_PROJECT_CONFIG": "1", "OPENCODE_PURE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            **{key: value for key, value in os.environ.items() if key.startswith("EVIDENCE_WIKI_")}}


def execute(argv, cwd, env, output: Path, *, timeout=90):
    from evidence_wiki.orchestration import _execute_bounded

    result = _execute_bounded(argv, cwd=cwd, stdin_text="", timeout_seconds=timeout, capture_limit=1_048_576,
                              environment=env, inherit_environment=False)
    output.write_text(json.dumps({"exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr,
                                 "timed_out": result.timed_out}, indent=2) + "\n", encoding="utf-8", newline="\n")
    return result


def prepare_cases(root: Path, python: Path, *, reviewed: bool):
    import pytest
    import yaml

    from evidence_wiki.agent_resources import resource_document
    from tests._computation_fixture import definition
    from tests.test_strict_evidence import host, review, save_claims

    patch = pytest.MonkeyPatch()
    evidence = root / "evidence"
    evidence.mkdir()
    fixture = host.__wrapped__(evidence, patch, SimpleNamespace(param={}))
    computation = definition()
    computation["graphs"] = {"decimal_example": {"description": "Exact decimal identity", "constants": {}, "inputs": {},
        "nodes": {"sum": {"expr": "0.1 + 0.2", "unit": "units"}}, "output_mapping": {"total": "sum"}, "output_page": None}}
    fixture.config["computation"] = computation
    (fixture.root / "research.yml").write_text(yaml.safe_dump(fixture.config, sort_keys=False), encoding="utf-8", newline="\n")
    fixture.claims["schema_version"] = "evidence-strict-claims/v2"
    for claim in fixture.claims["claims"]:
        claim["calculations"] = []
    save_claims(fixture)
    if reviewed:
        review(fixture)
    empty = root / "empty"
    empty.mkdir()
    guide = resource_document("guide/bootstrap/v1")
    cases = []
    for key, operation, target, parameters in (
        ("discovery", "bootstrap", empty, {}),
        ("resource", "resource", empty, {"resource_id": "onboarding/research_request/v2"}),
        ("decimal", "computation_check", fixture.root, {}),
        ("evidence", "strict_export", fixture.root, {}),
    ):
        cases.append({"id": key, "target": str(target), "call": {
            "schema_version": "evidence-framework-call/v1", "request_id": key,
            "instruction_sha256": guide["sha256"], "operation": operation, "parameters": parameters}})
    path = root / "cases.json"
    path.write_text(json.dumps(cases) + "\n", encoding="utf-8", newline="\n")
    return path, patch


def validate_results(path: Path, reviewed: bool):
    def require(condition):
        if not condition:
            raise ValueError("Canonical conformance expectation failed")
    rows = json.loads(path.read_text(encoding="utf-8"))
    require([row["id"] for row in rows] == ["discovery", "resource", "decimal", "evidence"])
    for row in rows:
        require(row["identity"] == row["value"]["result_sha256"] == hashlib.sha256(row["value"]["result_json"].encode()).hexdigest())
    require(json.loads(rows[0]["value"]["result_json"])["payload"]["workspace"] == "absent")
    computed = json.loads(rows[2]["value"]["result_json"])
    require(computed["status"] == "passed")
    require(computed["graphs"]["decimal_example"]["outputs"]["total"]["value"] == "0.3")
    require(rows[3]["value"]["evidence_acceptance"] == ("eligible" if reviewed else "ineligible"))
    require(not any(row["value"]["host_enforced"] for row in rows))
    return {row["id"]: row["identity"] for row in rows}


def qualify(name: str, root: Path, tools: Path, python: Path, cases: Path, reviewed: bool, *, journeys=False):
    from evidence_wiki.frameworks import compatibility

    pinned = next(row for row in compatibility()["frameworks"] if row["id"] == name)
    version = json.loads((tools / pinned["package"] / "package.json").read_text(encoding="utf-8"))["version"]
    if version != pinned["version"]:
        raise ValueError("unqualified_framework_version:" + name)
    work = root / "work"
    work.mkdir()
    env = environment(root)
    result_path = root / "result.json"
    command = shlex.join([str(python), str(ROOT / "tools/_framework_probe.py"), str(cases), str(result_path)])
    if journeys:
        command = shlex.join([str(python), "-B", str(ROOT / "tools/qualify_journeys.py"), "--cases", str(cases), "--output", str(root / "journeys")])
        result_path = root / "journeys/observations.json"
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FixtureProvider)
    server.command, server.paths = command, []
    if journeys and name == "opencode":
        server.bash_timeout = 3_600_000
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        if name == "pi":
            agent = root / "pi"
            agent.mkdir()
            (agent / "models.json").write_text(json.dumps({"providers": {"fixture": {
                "baseUrl": base_url + "/v1", "api": "openai-completions", "apiKey": "fixture-key",
                "models": [{"id": "fixture", "contextWindow": 128000, "maxTokens": 4096}]}}}), encoding="utf-8", newline="\n")
            argv = [str(tools / "@earendil-works/pi-coding-agent/dist/cli.js"), "--offline", "--no-approve", "--no-session",
                    "--no-extensions", "--no-skills", "--no-prompt-templates", "--no-themes", "--no-context-files",
                    "--provider", "fixture", "--model", "fixture", "--tools", "bash", "--mode", "json", "--print",
                    "Execute the selected local conformance command once and report its observed outcome."]
        elif name == "opencode":
            env["OPENCODE_CONFIG_CONTENT"] = json.dumps({"$schema": "https://opencode.ai/config.json", "autoupdate": False,
                "snapshot": False, "share": "disabled", "enabled_providers": ["fixture"],
                "provider": {"fixture": {"npm": "@ai-sdk/openai-compatible", "name": "Fixture",
                    "options": {"baseURL": base_url + "/v1", "apiKey": "fixture-key"},
                    "models": {"fixture": {"name": "Fixture", "limit": {"context": 128000, "output": 4096}}}}},
                "model": "fixture/fixture", "permission": {"*": "deny", "bash": {command: "allow"}}})
            argv = [str(tools / "opencode-darwin-arm64/bin/opencode"), "run", "--format", "json", "--model", "fixture/fixture",
                    "Execute the selected local conformance command once and report its observed outcome."]
        else:
            agent = root / "gemini" / ".gemini"
            agent.mkdir(parents=True)
            (agent / "settings.json").write_text(json.dumps({"general": {"disableAutoUpdate": True},
                "security": {"folderTrust": {"enabled": True}, "auth": {"selectedType": "gemini-api-key"}},
                "telemetry": {"enabled": False}}), encoding="utf-8", newline="\n")
            (agent / "trustedFolders.json").write_text(json.dumps({str(work): "TRUST_FOLDER"}), encoding="utf-8", newline="\n")
            env.update(GEMINI_API_KEY="fixture-key", GOOGLE_GEMINI_BASE_URL=base_url)
            argv = ["node", str(tools / "@google/gemini-cli/bundle/gemini.js"), "--model", "gemini-3.5-flash", "--output-format", "stream-json",
                    "--allowed-tools", f"run_shell_command({str(python)} {str(ROOT / 'tools/_framework_probe.py')})", "--prompt",
                    "Execute the selected local conformance command once and report its observed outcome."]
        result = execute(argv, work, env, root / "process.json", timeout=3600 if journeys else 90)
        outcome = {"framework": name, "provider": "deterministic-local-fixture", "process_exit": result.returncode,
                   "version": version, "timed_out": result.timed_out, "requests": len(server.paths), "reviewed": reviewed,
                   "mode": "scripted_journeys" if journeys else "canonical_fixture", "live_model": False}
        if result.returncode == 0 and result_path.is_file():
            if journeys:
                observed = json.loads(result_path.read_text(encoding="utf-8"))
                outcome.update(status=observed["status"], trials=observed["trials"],
                    observation_sha256=hashlib.sha256(result_path.read_bytes()).hexdigest())
            else:
                outcome.update(status="passed", cases=validate_results(result_path, reviewed))
        else:
            outcome.update(status="inconclusive", reason="harness_did_not_complete_canonical_cases")
        return outcome
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tools-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--framework", choices=("pi", "opencode", "gemini"), action="append")
    parser.add_argument("--journeys", action="store_true", help="Execute full scripted public-command journeys; does not qualify a live model.")
    args = parser.parse_args()
    output = args.output.resolve()
    subprocess.run(["git", "check-ignore", "-q", "--", str(output)], cwd=ROOT, check=True)  # noqa: S603,S607 -- enforce local-only qualification output.
    output.mkdir(exist_ok=False, parents=True)
    observations = []
    for reviewed in ((True,) if args.journeys else (False, True)):
        batch = output / ("reviewed" if reviewed else "unreviewed")
        batch.mkdir()
        cases, patch = ((ROOT / "tests/fixtures/onboarding-journeys/cases.json", None) if args.journeys
                        else prepare_cases(batch, args.python, reviewed=reviewed))
        try:
            for name in args.framework or ("pi", "opencode", "gemini"):
                directory = batch / name
                directory.mkdir()
                row = qualify(name, directory, args.tools_root.resolve() / "node_modules", args.python.absolute(), cases, reviewed, journeys=args.journeys)
                observations.append(row)
                print(json.dumps(row), flush=True)
                (output / "observations.json").write_text(json.dumps(observations, indent=2) + "\n", encoding="utf-8", newline="\n")
        finally:
            if patch is not None:
                patch.undo()
    return int(any(row["status"] != "passed" for row in observations))


if __name__ == "__main__":
    raise SystemExit(main())
