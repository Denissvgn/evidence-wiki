"""Observe canonical native skill discovery and removal in explicitly selected runtimes."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from evidence_wiki._pack_io import canonical  # noqa: E402
from evidence_wiki.agent_resources import resource_document  # noqa: E402
from evidence_wiki.native_instructions import apply, plan  # noqa: E402
from tools.qualify_frameworks import environment, execute  # noqa: E402


def require(value, message):
    if not value:
        raise ValueError(message)


def qualify(output, tools):
    matrix = json.loads(resource_document("framework/compatibility/v1")["content"])
    results = []
    packages = {"pi": "@earendil-works/pi-coding-agent", "opencode": "opencode-ai", "gemini": "@google/gemini-cli"}
    for row in matrix["frameworks"]:
        name, version = row["id"], row["version"]
        metadata = json.loads((tools / packages[name] / "package.json").read_text(encoding="utf-8"))
        require(metadata["version"] == version, "Selected runtime version differs from the qualified matrix")
        for scope in ("project", "user"):
            state = output / (name + "-" + scope)
            work, home = state / "work", state / "home"
            work.mkdir(parents=True)
            home.mkdir()
            env = environment(state)
            env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"), GEMINI_CLI_HOME=str(home))
            if name == "pi":
                script = "import {loadSkills} from REPLACE; console.log(JSON.stringify(loadSkills({cwd:process.argv[1],agentDir:process.argv[2],skillPaths:[],includeDefaults:true})));"
                script = script.replace("REPLACE", json.dumps((tools / packages[name] / "dist/index.js").as_uri()))
                argv = ["node", "--input-type=module", "-e", script, str(work), str(home / ".pi/agent")]
            elif name == "opencode":
                env.update(OPENCODE_DISABLE_PROJECT_CONFIG="0", OPENCODE_PURE="0",
                    OPENCODE_CONFIG_CONTENT=json.dumps({"autoupdate": False, "snapshot": False, "share": "disabled", "enabled_providers": []}))
                argv = [str(tools / "opencode-darwin-arm64/bin/opencode"), "debug", "skill"]
            else:
                settings = home / ".gemini"
                settings.mkdir()
                (settings / "settings.json").write_text(json.dumps({"security": {"folderTrust": {"enabled": True}}}), encoding="utf-8", newline="\n")
                (settings / "trustedFolders.json").write_text(json.dumps({str(work): "TRUST_FOLDER"}), encoding="utf-8", newline="\n")
                argv = ["node", str(tools / packages[name] / "bundle/gemini.js"), "skills", "list"]
            prepared = plan(canonical({"schema_version": "evidence-native-instructions-request/v1", "root": str(work if scope == "project" else home),
                "scope": scope, "framework": name, "version": version}))
            before = execute(argv, work, env, state / "before.json")
            require(before.returncode == 0 and "evidence-wiki" not in before.stdout, "Unexpected preexisting native skill")
            installed = apply(canonical(prepared))
            observed = execute(argv, work, env, state / "installed.json")
            require(observed.returncode == 0 and "evidence-wiki" in observed.stdout, "Installed skill was not discovered: " + name + "/" + scope)
            if name == "pi":
                skills = json.loads(observed.stdout)["skills"]
                require(any(skill["filePath"] == str(Path(installed["path"]) / "SKILL.md") for skill in skills), "Pi selected another skill")
            apply(canonical(prepared), remove=True)
            removed = execute(argv, work, env, state / "removed.json")
            require(removed.returncode == 0 and "evidence-wiki" not in removed.stdout, "Archived skill remained discoverable")
            results.append({"framework": name, "version": version, "scope": scope, "discovery": "passed", "removal": "passed",
                "live_model": "not_invoked", "trust": "temporary fixture settings only; installer grants none", "plan_id": prepared["plan_id"]})
            (output / "observations.json").write_text(json.dumps({"cases": results}, indent=2) + "\n", encoding="utf-8", newline="\n")
    return {"status": "passed", "cases": results, "limits": ["Pinned local runtimes and platform only.", "Discovery is not model instruction adherence."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tools-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    subprocess.run(["git", "check-ignore", "-q", "--", str(output)], cwd=ROOT, check=True)  # noqa: S603,S607 -- local-only evidence guard.
    output.mkdir(parents=True, exist_ok=False)
    result = qualify(output, args.tools_root.resolve() / "node_modules")
    (output / "observations.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
