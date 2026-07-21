#!/usr/bin/env python3
"""Network-free Codex CLI stand-in for installed-wheel orchestration smoke tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

FAKE_CODEX_WORKSPACE_PYTHON = "EVIDENCE_WIKI_FAKE_CODEX_WORKSPACE_PYTHON"
MANAGED_WORKSPACE_PYTHON = "EVIDENCE_WIKI_PYTHON"


def workspace_python() -> str:
    return os.environ.get(FAKE_CODEX_WORKSPACE_PYTHON, sys.executable)


def option_value(arguments: list[str], name: str) -> str:
    try:
        return arguments[arguments.index(name) + 1]
    except (ValueError, IndexError) as exc:
        raise SystemExit(f"fake codex: missing {name}") from exc


def config_values(arguments: list[str]) -> list[str]:
    return [arguments[index + 1] for index, value in enumerate(arguments[:-1]) if value == "--config"]


def require_managed_python_config(arguments: list[str]) -> None:
    configs = config_values(arguments)
    if "allow_login_shell=false" not in configs:
        raise SystemExit("fake codex: managed login shells were not disabled")
    shell_configs = [value for value in configs if value.startswith("shell_environment_policy=")]
    if len(shell_configs) != 1 or MANAGED_WORKSPACE_PYTHON not in shell_configs[0]:
        raise SystemExit("fake codex: managed workspace Python was not pinned")


def capability_probe(arguments: list[str]) -> int:
    root = Path(option_value(arguments, "--cd"))
    if option_value(arguments, "--permission-profile") != "evidence_wiki_worker":
        raise SystemExit("fake codex: unexpected permission profile")
    if os.environ.get("EVIDENCE_WIKI_FAKE_CODEX_PROBE_FAIL"):
        print("fake codex: permission profile unavailable", file=sys.stderr)
        return 73
    if (root / "protected" / "sentinel.txt").is_file():
        (root / "allowed.txt").write_text("allowed", encoding="utf-8")
        return 0

    require_managed_python_config(arguments)
    command = arguments[-5:]
    if command[1:4] != ["-I", "-B", "-c"] or "yaml" not in command[4]:
        raise SystemExit("fake codex: unexpected managed Python probe")
    if Path(command[0]).resolve() != Path(workspace_python()).resolve():
        raise SystemExit("fake codex: managed Python probe used the wrong interpreter")
    environment = os.environ.copy()
    environment[MANAGED_WORKSPACE_PYTHON] = command[0]
    return subprocess.run(  # noqa: S603 - exact host-generated dependency probe.
        command,
        cwd=root,
        check=False,
        env=environment,
    ).returncode


def execute(arguments: list[str]) -> int:
    required_flags = {
        "--ignore-user-config",
        "--ignore-rules",
        "--strict-config",
        "--output-schema",
        "--output-last-message",
    }
    missing = sorted(required_flags - set(arguments))
    if missing:
        raise SystemExit(f"fake codex: missing managed flags: {', '.join(missing)}")
    if "--sandbox" in arguments or "workspace-write" in arguments:
        raise SystemExit("fake codex: legacy sandbox arguments are forbidden")
    configs = config_values(arguments)
    if 'default_permissions="evidence_wiki_worker"' not in configs:
        raise SystemExit("fake codex: named permission profile was not selected")
    require_managed_python_config(arguments)
    configured_python = os.environ.get(MANAGED_WORKSPACE_PYTHON)
    if configured_python is None or Path(configured_python).resolve() != Path(workspace_python()).resolve():
        raise SystemExit("fake codex: managed process environment used the wrong workspace Python")

    prompt = sys.stdin.read()
    marker = "WORK ORDER (trusted orchestration data):\n"
    if marker not in prompt:
        raise SystemExit("fake codex: work order missing from prompt")
    order = json.loads(prompt.split(marker, 1)[1])

    fail_once = os.environ.get("EVIDENCE_WIKI_FAKE_CODEX_FAIL_ONCE")
    if fail_once:
        failure_marker = Path(fail_once)
        if not failure_marker.exists():
            failure_marker.write_text(order["action_id"], encoding="utf-8")
            failure_bytes = int(os.environ.get("EVIDENCE_WIKI_FAKE_CODEX_FAILURE_BYTES", "0"))
            if failure_bytes:
                print("x" * failure_bytes, file=sys.stderr)
            print("fake codex: injected action failure", file=sys.stderr)
            return 17

    root = Path(option_value(arguments, "--cd"))
    control_path = root / "research.yml"
    if os.environ.get("EVIDENCE_WIKI_FAKE_CODEX_TOUCH_CONTROL"):
        control_path.touch()
    if os.environ.get("EVIDENCE_WIKI_FAKE_CODEX_TAMPER_CONTROL"):
        with control_path.open("a", encoding="utf-8") as handle:
            handle.write("\n# fake semantic tamper\n")

    artifacts: list[str] = []
    if order.get("phase") == "research" and order.get("scope", {}).get("question_slugs"):
        slug = order["scope"]["question_slugs"][0]
        agent_id = order.get("agent_id") or "installed-wheel-fake"

        def workspace_command(script: str, *command: str) -> dict:
            process = subprocess.run(  # noqa: S603 - fixed workspace script and bounded fixture arguments.
                [workspace_python(), "-B", str(root / "scripts" / script), *command, "--format", "json"],
                cwd=root,
                check=False,
                capture_output=True,
                text=True,
            )
            if process.returncode != 0:
                raise SystemExit(
                    f"fake codex: {script} failed with {process.returncode}: {process.stderr or process.stdout}"
                )
            return json.loads(process.stdout)

        workspace_command("question_claim.py", "claim", "--slug", slug, "--agent-id", agent_id)
        request = workspace_command(
            "source_requests.py",
            "add",
            "--kind",
            "paper",
            "--query-or-identifier",
            f"evidence required for {slug}",
            "--rationale",
            "Installed-wheel fake runner needs evidence to exercise blocked research progress.",
            "--question-slug",
            slug,
        )["request"]
        workspace_command(
            "question_resolve.py",
            "block",
            "--slug",
            slug,
            "--agent-id",
            agent_id,
            "--blocked-reason",
            "The installed-wheel smoke intentionally starts without evidence.",
            "--request-id",
            request["request_id"],
        )
        artifacts.extend([f"wiki/questions/{slug}.md", "sources/source-requests.jsonl"])

    result = {
        "schema_version": "1.0",
        "action_id": order["action_id"],
        "outcome": "completed",
        "summary": "Installed-wheel fake runner completed the persisted research action.",
        "artifacts": artifacts,
    }
    Path(option_value(arguments, "--output-last-message")).write_text(
        json.dumps(result),
        encoding="utf-8",
    )
    return 0


def main() -> int:
    arguments = sys.argv[1:]
    if arguments == ["--version"]:
        print("codex-cli 0.138.0")
        return 0
    if arguments and arguments[0] == "sandbox":
        return capability_probe(arguments)
    if "exec" in arguments:
        return execute(arguments)
    raise SystemExit("fake codex: unsupported invocation")


if __name__ == "__main__":
    raise SystemExit(main())
