"""Replay retained installed command evidence under external producer custody."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from pathlib import Path

MAX_FILE_BYTES = 33_554_432
MAX_EVIDENCE_FILES = 32768
MAX_EVIDENCE_BYTES = 2_147_483_648
PARENT_STAGES = (
    "venv", "pip", "cli-entrypoints", "html-fresh", "html-upgrade", "html-revision", "agent", "pack", "native_initialization",
    "evidence-wiki contract", "evidence-wiki deploy --target", "evidence-wiki pack refresh",
    "evidence-wiki orchestrate start", "evidence-wiki orchestrate next", "evidence-wiki orchestrate status",
    "installed", "smoke_installed_orchestration.py", "publication", "packet", "execution", "usage", "snapshot", "temporal",
    "market", "historical_execution", "simulation", "assessment", "computation", "source", "planning", "research-actions",
    "revision", "setup-actions", "probe_installed_extensions.py", "pack_authoring", "research-journeys", "public-installed-journeys",
)
PUBLIC_STAGES = (
    "public-pip-check", "public-cli-contract", "public-cli-resources", "public-mcp-stdio", "public-direct-host",
    "public-cli-runtime-refusal", "public-current-caller", "public-mcp",
)
NATIVE_REQUIRED = {"selected_publication": "passed", "execution_evidence": "validated", "evidence_usage": "validated",
    "evidence_snapshots": "validated", "evidence_temporal": "validated", "historical_execution": "validated",
    "market_simulation": "independently_recalculated", "evidence_assessments": "authenticated_cli_api_parity",
    "declarative_computation": "passed", "copied_computation_scripts": "passed", "market_evidence": "passed",
    "installed_agent_bootstrap": "passed", "closed_resources": "passed", "source_inspection": "passed", "research_planning": "passed"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def bounded_file(root, relative):
    require(isinstance(relative, str) and relative == Path(relative).as_posix() and not Path(relative).is_absolute()
            and all(part not in {".", ".."} for part in relative.split("/")), "unsafe evidence locator")
    path = Path(root) / relative
    require(not any(item.is_symlink() for item in (path, *path.parents) if item.is_relative_to(Path(root))), "linked retained evidence")
    require(path.is_file() and path.stat().st_size <= MAX_FILE_BYTES, "missing or oversized retained evidence")
    return path


def evidence_inventory(root):
    result, total = {}, 0
    for path in sorted(Path(root).rglob("*")):
        require(not path.is_symlink(), "linked retained evidence")
        if path.is_file() and path != Path(root) / "summary.json":
            relative = path.relative_to(root).as_posix()
            bounded_file(root, relative)
            total += path.stat().st_size
            require(total <= MAX_EVIDENCE_BYTES and len(result) < MAX_EVIDENCE_FILES, "retained evidence exceeds bounds")
            result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def replay_commands(directory, records, expected_stages):
    require(isinstance(records, list) and [row.get("stage") for row in records] == list(expected_stages),
            "required command inventory missing, duplicated, skipped or extra")
    results = {}
    used = set()
    for record in records:
        stage = record["stage"]
        expected_exit = 2 if stage == "public-cli-runtime-refusal" or stage.endswith(("-unknown_command", "-invalid_plan")) else 0
        require(record.get("status") == "passed" and record.get("exit_code") == expected_exit
                and record.get("expected_exit_codes") == [expected_exit], "command exit observation differs: " + stage)
        require(record.get("stdin_mode") == ("explicit_payload" if stage == "public-mcp-stdio" else "devnull"),
                "command stdin observation differs from noninteractive obligation")
        require(type(record.get("pid")) is int and record["pid"] > 0, "command was never observed to start")
        require(isinstance(record.get("argv"), list) and record["argv"] and all(isinstance(value, str) for value in record["argv"]),
                "command argv observation missing")
        start, end = record.get("started_monotonic"), record.get("completed_monotonic")
        require(type(start) in {int, float} and type(end) in {int, float} and math.isfinite(start) and math.isfinite(end)
                and end >= start and 0 <= record.get("seconds", -1) <= record.get("timeout_seconds", 0) + 10,
                "command timing observation invalid")
        require(datetime.fromisoformat(record["completed_at_utc"]) >= datetime.fromisoformat(record["started_at_utc"]),
                "command UTC observations are reversed")
        receipt = bounded_file(directory, record["receipt"])
        require(json.loads(receipt.read_text(encoding="utf-8")) == record, "retained command receipt differs from producer")
        values = {}
        for channel in ("stdout", "stderr"):
            relative = record[channel]
            require(relative not in used, "command log reused by multiple obligations")
            used.add(relative)
            data = bounded_file(directory, relative).read_bytes()
            require(len(data) == record[channel + "_bytes"] and hashlib.sha256(data).hexdigest() == record[channel + "_sha256"],
                    "retained command log bytes differ")
            values[channel] = data.decode("utf-8")
        results[stage] = {**values, "record": record}
    actual_receipts = {path.name for path in Path(directory).glob("*.json")}
    require(actual_receipts == {record["receipt"] for record in records}, "retained command receipt inventory differs")
    return results


def parsed(text):
    return json.loads(text)


def replay_installation(report, root, expected):
    """Reconstruct success from original receipt/log bytes, then join native results."""
    from tools import probe_installed_public as public
    from tools import validate_installed_artifacts as owner

    kind = report["artifact"]
    require(kind in {"wheel", "sdist"}, "invalid installed artifact kind")
    stages = ["artifacts/selected-runtime-identity"] + (["sdist/build"] if kind == "sdist" else [])
    stages += [kind + "/" + value for value in PARENT_STAGES]
    top = replay_commands(Path(root) / "commands", report["commands"], stages)
    installed = report["checks"]["installed_" + kind]
    runtime = parsed(top["artifacts/selected-runtime-identity"]["stdout"])
    require(runtime["runtime"] == report["qualification_runtime"], "selected runtime stdout differs")
    require(runtime["markers"] == installed["public_installed"]["marker_environment"], "installed marker environment differs from selected interpreter")
    for name in ("agent", "pack", "native_initialization", "installed", "publication", "packet", "execution", "usage", "snapshot", "temporal",
                 "market", "historical_execution", "simulation", "assessment", "computation", "source", "planning", "research-actions", "revision",
                 "setup-actions", "probe_installed_extensions.py", "pack_authoring", "cli-entrypoints"):
        output = parsed(top[kind + "/" + name]["stdout"])
        require(isinstance(output, dict) and all(installed.get(key) == value for key, value in output.items()),
                "installed outcomes differ from actual command stdout: " + name)
        program = owner.__dict__.get(name.upper() + "_PROBE")
        if program is not None:
            argv = top[kind + "/" + name]["record"]["argv"]
            require("-c" in argv and argv[argv.index("-c") + 1] == program, "native probe code differs from frozen source")
    require(all(installed.get(key) == value for key, value in NATIVE_REQUIRED.items()), "required native observation was skipped or unsupported")
    require(parsed(top[kind + "/evidence-wiki contract"]["stdout"]) and
            public.digest(parsed(top[kind + "/evidence-wiki contract"]["stdout"])) == expected["contract"], "native CLI contract differs")
    for mode in ("fresh", "upgrade", "revision"):
        require(parsed(top[kind + "/html-" + mode]["stdout"]) == installed["html_usability"][mode], "HTML observations differ from actual stdout")
    observations = parsed(bounded_file(root, kind + "/journeys/observations.json").read_text(encoding="utf-8"))
    require(observations == installed["journeys"], "native journey observations differ from retained execution")
    public_report = parsed(top[kind + "/public-installed-journeys"]["stdout"])
    require(public_report == installed["public_installed"], "public outcomes differ from actual probe stdout")
    public.checked_supervision(public_report, parent_record=top[kind + "/public-installed-journeys"]["record"],
                               parent_stage=installed["public_stage"])
    require(public_report["prefix"] == installed["cli_entrypoint_identity"]["prefix"], "public dependency prefix differs from observed installed CLI")
    for name, origin in public_report["dependency_origins"].items():
        data = bounded_file(root, kind + "/public-commands/dependency-origins/" + name + ".py").read_bytes()
        require(len(data) == origin["bytes"] and hashlib.sha256(data).hexdigest() == origin["sha256"], "retained imported dependency bytes differ")
    children = replay_commands(Path(root) / kind / "public-commands", public_report["commands"], PUBLIC_STAGES)
    require(children["public-pip-check"]["stdout"] == "No broken requirements found.\n", "pip check did not complete")
    require(public.digest(parsed(children["public-cli-contract"]["stdout"])) == expected["contract"], "public CLI contract differs")
    require(parsed(children["public-cli-resources"]["stdout"])["payload"] == {key: value for key, value in expected["resource_index"].items() if key != "schema_version"}, "public CLI resources differ")
    public.checked_direct_result(parsed(children["public-direct-host"]["stdout"]))
    for route in ("current-caller", "mcp"):
        public.checked_adapter_result(parsed(children["public-" + route]["stdout"]), route)
    refusal = parsed(children["public-cli-runtime-refusal"]["stdout"])
    require(refusal["status"] == "refused" and refusal["terminal"] is True and refusal["error"]["error_code"] == "RESEARCH_EXECUTION_CONFIG_INVALID", "CLI runtime refusal differs")
    transport = [parsed(line) for line in children["public-mcp-stdio"]["stdout"].splitlines()]
    require([row.get("id") for row in transport] == [1, 2, 3] and transport[1]["result"]["tools"] == expected["mcp_tools"], "actual MCP discovery differs")
    entrypoint_stages = ["installation-identity"] + [name + "-" + case for name in ("cli_module", "package_module", "console_script")
        for case in ("help", "version", "schemas", "unknown_command", "invalid_plan")]
    entrypoints = replay_commands(Path(root) / kind / "cli-entrypoint-commands", installed["cli_entrypoint_commands"], entrypoint_stages)
    from types import SimpleNamespace

    from tools.probe_installed_cli import check_cli_case
    for stage, values in entrypoints.items():
        if stage != "installation-identity":
            case = next(case for case in ("help", "version", "schemas", "unknown_command", "invalid_plan") if stage.endswith("-" + case))
            check_cli_case(case, SimpleNamespace(returncode=values["record"]["exit_code"], stdout=values["stdout"], stderr=values["stderr"]), installed["version"])
    for family, stages in (("setup", ("installation-identity", "empty-inspection", "empty-replay", "local_source-inspection", "local_source-replay")),
                           ("research", ("installation-identity", "start", "heartbeat"))):
        replay_commands(Path(root) / kind / (family + "-action-commands"), installed[family + "_action_commands"], stages)
    return public.checked_public_outcomes(public_report, expected)
