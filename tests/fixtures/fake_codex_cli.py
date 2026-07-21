#!/usr/bin/env python3
"""Network-free Codex CLI stand-in for installed-wheel orchestration smoke tests."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def option_value(arguments: list[str], name: str) -> str:
    try:
        return arguments[arguments.index(name) + 1]
    except (ValueError, IndexError) as exc:
        raise SystemExit(f"fake codex: missing {name}") from exc


def capability_probe(arguments: list[str]) -> int:
    root = Path(option_value(arguments, "--cd"))
    if option_value(arguments, "--permission-profile") != "evidence_wiki_worker":
        raise SystemExit("fake codex: unexpected permission profile")
    if os.environ.get("EVIDENCE_WIKI_FAKE_CODEX_PROBE_FAIL"):
        print("fake codex: permission profile unavailable", file=sys.stderr)
        return 73
    (root / "allowed.txt").write_text("allowed", encoding="utf-8")
    return 0


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
    configs = [arguments[index + 1] for index, value in enumerate(arguments[:-1]) if value == "--config"]
    if 'default_permissions="evidence_wiki_worker"' not in configs:
        raise SystemExit("fake codex: named permission profile was not selected")

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

    result = {
        "schema_version": "1.0",
        "action_id": order["action_id"],
        "outcome": "completed",
        "summary": "Installed-wheel fake runner completed the persisted research action.",
        "artifacts": [],
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
