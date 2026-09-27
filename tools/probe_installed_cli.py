"""Bind installed command observations to one environment and complete outcome sets."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import runpy
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

OUTCOME_CASES = {
    "cli_entrypoints": ("package_module", "cli_module", "console_script"),
    "generated_setup_actions": ("empty_inspection", "empty_replay", "local_source_inspection", "local_source_replay"),
    "generated_research_actions": ("start", "heartbeat"),
}

IDENTITY_PROGRAM = """
import importlib.metadata
import importlib.util
import json
import sys
from pathlib import Path
import evidence_wiki

def origin(name):
    spec = importlib.util.find_spec(name)
    return spec.origin if spec is not None else None

distribution = importlib.metadata.distribution('evidence-wiki')
print(json.dumps({
    'executable': sys.executable, 'prefix': sys.prefix, 'base_prefix': sys.base_prefix,
    'package_version': evidence_wiki.__version__, 'distribution_version': distribution.version,
    'distribution_root': str(Path(distribution.locate_file('')).resolve()),
    'modules': {name: origin(name) for name in ('evidence_wiki', 'evidence_wiki.cli', 'evidence_wiki.__main__')},
}))
"""


@lru_cache(maxsize=1)
def process_support():
    """Load the fixed, separately hashed qualification helper without importing the checkout."""
    return runpy.run_path(str(Path(__file__).with_name("_qualification_process.py")))


def checked_outcomes(group, outcomes):
    """Reject missing, extra, failed or unknown outcomes instead of synthesizing completion."""
    expected = OUTCOME_CASES.get(group)
    if (expected is None or not isinstance(outcomes, dict) or set(outcomes) != set(expected)
            or any(value != "passed" for value in outcomes.values())):
        raise ValueError(f"Incomplete installed outcomes: {group}")
    return {name: outcomes[name] for name in expected}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def workspace_files(root):
    """Compare file contents in the explicitly selected disposable workspace."""
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


def setup_next_actions(commands, receipt, *, target, kind):
    """Execute the public inspection/replay arrays and verify their workspace postconditions."""
    require(kind in {"empty", "local_source"}, "Unknown setup scenario")
    require(receipt["target"] == str(target), "Setup receipt selected a different target")
    inspections = [row for row in receipt["next_actions"] if row["action"] == "inspect_sources"]
    resumes = [row for row in receipt["next_actions"] if row["action"] == "resume_setup"]
    require(len(inspections) == len(resumes) == 1, "Setup action inventory is incomplete or duplicated")
    expected_paths = {path for row in receipt["sources"] for item in row["observations"] for path in item["raw_paths"]}
    require(bool(expected_paths) == (kind == "local_source"), "Setup source scenario differs")
    argv = inspections[0]["argv"]
    require(argv[argv.index("--target") + 1] == str(target), "Inspection command selected a different target")
    selectors = {argv[index + 1] for index, value in enumerate(argv) if value == "--source-path"}
    require(selectors == expected_paths, "Inspection command selected different sources")
    argv = resumes[0]["argv"]
    require(argv[argv.index("--from-file") + 1] == str(Path(receipt["checkpoint"]).with_name("plan.json")),
            "Replay command selected a different saved plan")
    before = workspace_files(target)
    inspection = commands.command(inspections[0]["argv"], label=kind + "-inspection", timeout=120)
    require(inspection.returncode == 0 and not inspection.stderr, "Setup inspection failed")
    observed = json.loads(inspection.stdout)
    require(observed["schema_version"] == "evidence-source-inspection/v1"
            and observed["target"]["selected"] and observed["target"]["state"] == "present"
            and observed["research_ready"] is False, "Setup inspection result differs")
    selected = {path for row in observed["sources"] for path in row["raw_paths"]}
    require(selected == expected_paths and all(row["usability"] == "usable" for row in observed["sources"]),
            "Setup inspection did not observe the selected usable sources")
    require(workspace_files(target) == before, "Setup inspection changed workspace content")
    replay = commands.command(resumes[0]["argv"], label=kind + "-replay", timeout=120)
    require(replay.returncode == 0 and not replay.stderr, "Setup replay failed")
    resumed = json.loads(replay.stdout)
    require(resumed["schema_version"] == "evidence-setup-result/v1" and resumed["status"] == "ready"
            and resumed["setup_ready"] and resumed["plan_id"] == receipt["plan_id"]
            and resumed["transaction_id"] == receipt["transaction_id"]
            and resumed["research_complete"] is False and resumed["claims_verified"] is False,
            "Setup replay changed identity or readiness semantics")
    require(workspace_files(target) == before, "Setup replay changed workspace content")
    return {kind + "_inspection": "passed", kind + "_replay": "passed"}


def research_next_action(commands, advice, *, operation, target, agent_id, run_id=None):
    """Execute only an explicitly selected, fresh caller action without granting authority to advice."""
    require(operation in {"start", "heartbeat"}, "Unknown research observation")
    require(advice["actions_executed"] is False and advice["research_complete"] is False,
            "Guidance claimed execution or completion")
    require(all(row["authorized"] is False and row["evidence_accepted"] is False for row in advice["actions"]),
            "Guidance changed authority semantics")
    selected = [row for row in advice["actions"] if row["operation"] == operation]
    require(len(selected) == 1, "Research action is missing or duplicated")
    action = selected[0]
    require(datetime.fromisoformat(action["expires_at"]) > datetime.now(timezone.utc), "Research action expired")
    argv = action["argv"]
    require(argv[4:6] == ["agent", operation] and argv[argv.index("--target") + 1] == str(target)
            and argv[argv.index("--agent-id") + 1] == agent_id, "Research command selected a different operation or caller")
    if run_id is not None:
        require(argv[argv.index("--run-id") + 1] == run_id, "Research command selected a different run")
    observed = commands.command(argv, label=operation, timeout=120)
    require(observed.returncode == 0 and not observed.stderr, "Research command failed")
    result = json.loads(observed.stdout)
    require(result["schema_version"] == "evidence-caller-run-result/v1" and result["operation"] == operation
            and result["research_complete"] is False and result["run"]["agent_id"] == agent_id
            and result["run"]["caller_context"]["context_id"] and result["run"]["run_id"]
            and (run_id is None or result["run"]["run_id"] == run_id), "Research result changed caller or run identity")
    return result


class InstalledCommands:
    """Observe one installed package, then execute explicit argv with the shared bounded runner."""

    def __init__(self, python, cli, *, cwd, checkout_root, expected_version=None, output=None, runner=None):
        self.python = Path(python).absolute()
        self.cli = Path(cli).absolute()
        self.prefix = self.python.parent.parent.resolve()
        self.cwd = Path(cwd).resolve()
        checkout = Path(checkout_root).resolve()
        if self.prefix.is_relative_to(checkout) or self.cwd.is_relative_to(checkout):
            raise ValueError("Installed commands require an environment and CWD outside the checkout")
        if (not self.cwd.is_dir() or not (self.prefix / "pyvenv.cfg").is_file()
                or not self.python.is_file() or not self.cli.is_file()
                or self.cli.parent.resolve() != self.python.parent.resolve()
                or not self.cli.resolve().is_relative_to(self.prefix)):
            raise ValueError("Selected installation launchers or working directory are unavailable")
        self.environment = {key: value for key, value in os.environ.items()
                            if key not in {"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "GIT_DIR", "GIT_WORK_TREE"}}
        self.environment.update(PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
        self.environment["PATH"] = os.pathsep.join(
            entry for entry in self.environment.get("PATH", "").split(os.pathsep)
            if entry and Path(entry).resolve() != self.python.parent.resolve()
        )
        self.runner = process_support()["CommandRunner"](output) if runner is None else runner
        # Match the import environment of emitted -m commands, including their CWD.
        observed = self._execute([str(self.python), "-B", "-c", IDENTITY_PROGRAM],
                                 label="installation-identity", expected=0, timeout=30)
        self.identity = json.loads(observed.stdout)
        self._validate_identity(expected_version)

    def _validate_identity(self, expected_version):
        """Require interpreter, distribution and all entry-point modules in the selected virtual environment."""
        data = self.identity
        fields = ("executable", "prefix", "base_prefix", "distribution_root", "package_version", "distribution_version")
        if not isinstance(data, dict) or any(not isinstance(data.get(key), str) or not data[key] for key in fields):
            raise ValueError("Installed identity is incomplete")
        if (Path(data["executable"]).absolute() != self.python or Path(data["prefix"]).resolve() != self.prefix
                or Path(data["base_prefix"]).resolve() == self.prefix):
            raise ValueError("Interpreter does not belong to the selected virtual environment")
        if (data["package_version"] != data["distribution_version"]
                or expected_version is not None and data["package_version"] != expected_version):
            raise ValueError("Installed package and distribution versions differ from the candidate")
        distribution = Path(data["distribution_root"]).resolve()
        if not distribution.is_relative_to(self.prefix) or "site-packages" not in distribution.parts:
            raise ValueError("Distribution metadata is outside the selected installation")
        origins = data.get("modules")
        names = {"evidence_wiki": "__init__.py", "evidence_wiki.cli": "cli.py", "evidence_wiki.__main__": "__main__.py"}
        if not isinstance(origins, dict) or set(origins) != set(names):
            raise ValueError("Installed module origins are incomplete")
        package = None
        for name, filename in names.items():
            value = origins[name]
            if not isinstance(value, str) or not Path(value).is_absolute():
                raise ValueError(f"Installed module origin is unavailable: {name}")
            path = Path(value).resolve()
            if (not path.is_file() or path.name != filename or not path.is_relative_to(distribution)
                    or package is not None and path.parent != package):
                raise ValueError(f"Module is outside the selected installed package: {name}")
            package = path.parent

    def _execute(self, argv, *, label, expected, timeout):
        """Preserve exit codes, diagnostics and the shared runner's output/time/process bounds."""
        result = self.runner.run(argv, label=label, cwd=self.cwd, env=self.environment,
                                 timeout=timeout, expected=(expected,))
        if result.returncode != expected:
            raise ValueError(f"{label} returned {result.returncode}; expected {expected}\n"
                             f"stdout (tail):\n{result.stdout[-8192:]}\nstderr (tail):\n{result.stderr[-8192:]}")
        return result

    def command(self, argv, *, label, expected=0, timeout=60):
        """Execute an explicit package/console argv unchanged; never repair its launcher."""
        if not isinstance(argv, list) or not argv or any(not isinstance(value, str) for value in argv):
            raise ValueError("Installed command argv must be a nonempty string array")
        module_command = (argv[:3] == [str(self.python), "-B", "-m"] and len(argv) >= 4
                          and argv[3] in {"evidence_wiki", "evidence_wiki.cli"})
        if not module_command and argv[0] != str(self.cli):
            raise ValueError("Command does not select the observed installation launcher")
        return self._execute(argv, label=label, expected=expected, timeout=timeout)


def check_cli_case(case, result, version):
    """Validate stable success and refusal behavior independently of launcher parity."""
    if case == "unknown_command":
        valid = (result.stdout == "" and result.stderr.startswith("usage: evidence-wiki")
                 and "unknown command: unknown-command" in result.stderr)
    elif result.stderr:
        valid = False
    elif case == "help":
        valid = (result.stdout.startswith("evidence-wiki: ") and "Usage:" in result.stdout
                 and "evidence-wiki agent apply" in result.stdout)
    elif case == "version":
        valid = result.stdout == f"evidence-wiki {version}\n"
    elif case in {"schemas", "invalid_plan"}:
        payload = json.loads(result.stdout)
        if case == "schemas":
            valid = (isinstance(payload, dict) and isinstance(payload.get("schema_ids"), list)
                     and "evidence-source-inspection/v1" in payload["schema_ids"])
        else:
            valid = (isinstance(payload, dict) and payload.get("schema_version") == "1.0"
                     and payload.get("error_code") == "ONBOARDING_INVALID" and payload.get("recoverable") is False
                     and isinstance(payload.get("details"), dict) and bool(payload.get("message"))
                     and bool(payload.get("remediation")))
    else:
        valid = False
    if not valid:
        raise ValueError(f"Installed CLI behavior differs for {case}")


def entrypoint_report(commands):
    """Credit each launch form only after all success/refusal cases and exact parity pass."""
    launchers = {
        "cli_module": [str(commands.python), "-B", "-m", "evidence_wiki.cli"],
        "package_module": [str(commands.python), "-B", "-m", "evidence_wiki"],
        "console_script": [str(commands.cli)],
    }
    outcomes, reference = {}, {}
    with tempfile.TemporaryDirectory(prefix="entrypoint inputs ", dir=commands.cwd) as directory:
        plan = Path(directory) / "invalid plan.json"
        plan.write_text("{}", encoding="utf-8", newline="\n")
        cases = {
            "help": ["--help"], "version": ["--version"],
            "schemas": ["agent", "source-schemas", "--format", "json"],
            "unknown_command": ["unknown-command"],
            "invalid_plan": ["agent", "apply", "--from-file", str(plan), "--format", "json"],
        }
        for name, launcher in launchers.items():
            for case, arguments in cases.items():
                expected = 2 if case in {"unknown_command", "invalid_plan"} else 0
                result = commands.command([*launcher, *arguments], label=name + "-" + case, expected=expected)
                check_cli_case(case, result, commands.identity["package_version"])
                signature = result.returncode, result.stdout, result.stderr
                if name == "cli_module":
                    reference[case] = signature
                elif signature != reference[case]:
                    raise ValueError(f"Installed launcher {name} differs from CLI module for {case}")
            outcomes[name] = "passed"
    output = getattr(commands.runner, "output", None)
    return {"cli_entrypoints": checked_outcomes("cli_entrypoints", outcomes),
            "cli_entrypoint_identity": {**commands.identity, "console_script": str(commands.cli)},
            "cli_entrypoint_log_directory": str(output) if output is not None else None,
            "cli_entrypoint_commands": commands.runner.records}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--cwd", type=Path, required=True)
    parser.add_argument("--checkout-root", type=Path, required=True)
    parser.add_argument("--expected-version")
    parser.add_argument("--output", type=Path, help="Retain bounded command logs and outcomes in this directory.")
    args = parser.parse_args(argv)
    try:
        commands = InstalledCommands(sys.executable, args.cli, cwd=args.cwd, checkout_root=args.checkout_root,
                                     expected_version=args.expected_version, output=args.output)
        report = entrypoint_report(commands)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"Installed CLI qualification failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    with process_support()["interruptible"]():
        raise SystemExit(main())
