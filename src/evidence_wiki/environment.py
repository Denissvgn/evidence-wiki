"""Credential presence checks and explicit, ephemeral command environments."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import subprocess
import sys
import warnings
from collections.abc import Mapping, Sequence

SERVICE_VARIABLES = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "github": "GITHUB_TOKEN",
    "openalex": "OPENALEX_API_KEY",
}


def variable_name(value: str) -> str:
    """Accept a variable name without reflecting a mistaken secret assignment."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", value):
        raise argparse.ArgumentTypeError(
            "Use an environment variable name only; supply its value through the environment or a hidden prompt."
        )
    return value.upper() if os.name == "nt" else value


def environment_report(
    services: Sequence[str] = (),
    required_names: Sequence[str] = (),
    *,
    environ: Mapping[str, str] | None = None,
) -> dict:
    """Report presence only; selected requirements never imply authenticated access."""
    if any(service not in SERVICE_VARIABLES for service in services):
        raise ValueError("Unknown environment service.")
    required = {variable_name(name) for name in required_names}
    required.update(SERVICE_VARIABLES[service] for service in services)
    environment = os.environ if environ is None else environ
    checks = [
        {
            "name": name,
            "required": name in required,
            "present": bool(environment.get(name, "").strip()),
        }
        for name in sorted(required or set(SERVICE_VARIABLES.values()))
    ]
    missing = [row["name"] for row in checks if row["required"] and not row["present"]]
    return {
        "schema_version": "evidence-environment/v1",
        "checks": checks,
        "missing_required": missing,
        "ready": not missing,
        "validation": "presence_only",
        "credentials_persisted": False,
    }


def render_report(report: dict) -> str:
    lines = ["Environment variables (presence only; API access has not been verified):"]
    for row in report["checks"]:
        state = "present" if row["present"] else "missing"
        requirement = "required" if row["required"] else "optional"
        lines.append(f"  {row['name']}: {state} ({requirement})")
    if report["missing_required"]:
        lines.append("Supply missing variables through your secret manager or use `env run --prompt`.")
    elif any(row["required"] for row in report["checks"]):
        lines.append("All selected variables are present.")
    else:
        lines.append("No requirements selected. Use --service or --require-env to check a particular workflow.")
    return "\n".join(lines)


def prompt_missing(names: Sequence[str], environment: dict[str, str]) -> None:
    """Fill a private environment, refusing input if echo suppression is unavailable."""
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise OSError("A terminal is required.")
    print("Enter missing values with input hidden. Values last only for the launched command.", file=sys.stderr)
    for name in names:
        with warnings.catch_warnings():
            # getpass otherwise falls back to echoed input when terminal control fails.
            warnings.simplefilter("error", getpass.GetPassWarning)
            value = getpass.getpass(f"{name}: ", stream=sys.stderr)
        if not value.strip() or any(character in value for character in "\0\r\n"):
            raise ValueError("A non-empty, single-line value is required.")
        environment[name] = value


def run_command(args: argparse.Namespace) -> int:
    """Launch the operator's argv with a private environment and no shell expansion."""
    environment = dict(os.environ)
    report = environment_report(args.service, args.require_env, environ=environment)
    missing = report["missing_required"]
    if missing:
        if not args.prompt:
            print(render_report(report), file=sys.stderr)
            return 1
        try:
            prompt_missing(missing, environment)
        except (EOFError, KeyboardInterrupt):
            print("Environment input cancelled; command was not started.", file=sys.stderr)
            return 130
        except (OSError, getpass.GetPassWarning):
            print(
                "Protected input requires an interactive terminal with echo suppression. "
                "Inject variables through your secret manager for unattended runs; command was not started.",
                file=sys.stderr,
            )
            return 2
        except ValueError:
            print("A non-empty, single-line value is required; command was not started.", file=sys.stderr)
            return 2
    try:
        result = subprocess.run(args.command, env=environment, check=False)  # noqa: S603 -- explicit operator argv, shell=False.
    except FileNotFoundError:
        print("Selected command was not found.", file=sys.stderr)
        return 127
    except OSError:
        print("Selected command could not be started.", file=sys.stderr)
        return 126
    except KeyboardInterrupt:
        return 130
    return result.returncode if result.returncode >= 0 else 128 - result.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="evidence-wiki env",
        description="Check credential variables or supply them privately to one command. No secrets are saved.",
        epilog="Workspace initialization and package imports do not require API credentials. "
        "Select only services needed by your command; runner login may supply its own authentication.",
        allow_abbrev=False,
    )
    subparsers = parser.add_subparsers(dest="operation", required=True)
    for operation in ("check", "run"):
        command = subparsers.add_parser(operation, allow_abbrev=False)
        command.add_argument("--service", action="append", choices=sorted(SERVICE_VARIABLES), default=[],
                             help="Require this service's API credential; repeat for multiple services.")
        command.add_argument("--require-env", action="append", type=variable_name, default=[], metavar="NAME",
                             help="Require an additional variable by name only; repeat as needed.")
        if operation == "check":
            command.add_argument("--format", choices=("text", "json"), default="text")
        else:
            command.add_argument("--prompt", action="store_true", help="Read missing values with terminal echo disabled.")
            command.add_argument("command", nargs=argparse.REMAINDER, metavar="-- COMMAND [ARG ...]")
    args = parser.parse_args(argv)
    if args.operation == "check":
        report = environment_report(args.service, args.require_env)
        print(json.dumps(report, indent=2) if args.format == "json" else render_report(report))
        return 0 if report["ready"] else 1
    if not args.service and not args.require_env:
        parser.error("env run requires at least one --service or --require-env selection")
    if not args.command or args.command[0] != "--" or len(args.command) < 2:
        parser.error("separate the selected command with --, followed by COMMAND [ARG ...]")
    args.command = args.command[1:]
    return run_command(args)
