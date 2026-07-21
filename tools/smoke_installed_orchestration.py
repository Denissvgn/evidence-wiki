#!/usr/bin/env python3
"""Exercise managed failure/resume using only an installed EvidenceWiki CLI."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_CODEX = REPO_ROOT / "tests" / "fixtures" / "fake_codex_cli.py"
FAKE_CODEX_WORKSPACE_PYTHON = "EVIDENCE_WIKI_FAKE_CODEX_WORKSPACE_PYTHON"


def run(
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
    expected: int = 0,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.run(  # noqa: S603 - argv is fixed by this repository-owned smoke harness.
        argv,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    if process.returncode != expected:
        raise SystemExit(
            f"command returned {process.returncode}, expected {expected}: {argv!r}\n"
            f"stdout:\n{process.stdout}\nstderr:\n{process.stderr}"
        )
    return process


def fake_codex_environment() -> dict[str, str]:
    environment = os.environ.copy()
    # The copied fixture starts through /usr/bin/env and may therefore run under a
    # different Python. Keep deployed workspace scripts inside the wheel venv.
    environment[FAKE_CODEX_WORKSPACE_PYTHON] = sys.executable
    return environment


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cli", required=True, type=Path)
    args = parser.parse_args()
    cli = str(args.cli.resolve())

    contract = json.loads(run([cli, "contract", "--format", "json"]).stdout)
    if contract.get("starter_version") != "0.5.2":
        raise SystemExit(f"installed CLI reported an unexpected starter version: {contract.get('starter_version')}")
    schema_documents = contract.get("artifact_schema_documents")
    expected_schemas = {
        "orchestration_session",
        "orchestration_work_order",
        "orchestration_result",
        "orchestration_attempt",
    }
    if not isinstance(schema_documents, dict) or set(schema_documents) != expected_schemas:
        raise SystemExit("installed CLI did not publish the complete orchestration schema document set")
    starter_assets = set(contract.get("required_asset_manifest", {}).get("starter", []))
    required_provider_assets = {
        "workspace-template/scripts/discover_sources.py",
        "workspace-template/scripts/fetch_sources.py",
        "workspace-template/scripts/run_controller.py",
        "workspace-template/scripts/source_requests.py",
    }
    if not required_provider_assets <= starter_assets:
        raise SystemExit("installed CLI contract omitted required provider workflow assets")

    with tempfile.TemporaryDirectory(prefix="evidence-wiki-wheel-smoke-") as tmpdir:
        temporary_root = Path(tmpdir)
        workspace = temporary_root / "workspace with spaces"
        fake_bin = temporary_root / "bin"
        fake_bin.mkdir()
        fake_codex = fake_bin / "codex"
        shutil.copyfile(FAKE_CODEX, fake_codex)
        fake_codex.chmod(0o755)

        run(
            [
                cli,
                "deploy",
                "--target",
                str(workspace),
                "--project-name",
                "managed-wheel-smoke",
                "--project-description",
                "Installed-wheel managed orchestration smoke",
            ]
        )
        batch = workspace / "batch.yaml"
        batch.write_text(
            'schema_version: "1.0"\n'
            "questions:\n"
            '  - question: "Can the installed managed host resume its persisted action?"\n'
            "    id: managed-resume-smoke\n"
            "    priority: high\n",
            encoding="utf-8",
        )
        run([cli, "questions", "add", "--target", str(workspace), "--from-file", str(batch)])

        failure_marker = temporary_root / "fail-once.marker"
        environment = fake_codex_environment()
        environment["PATH"] = os.pathsep.join((str(fake_bin), environment.get("PATH", "")))
        environment["EVIDENCE_WIKI_FAKE_CODEX_FAIL_ONCE"] = str(failure_marker)
        environment["EVIDENCE_WIKI_FAKE_CODEX_FAILURE_BYTES"] = str(256 * 1024)
        common = [
            "--target",
            str(workspace),
            "--runner",
            "codex",
            "--agent-id",
            "wheel-smoke-agent",
            "--action-timeout-seconds",
            "30",
            "--format",
            "json",
        ]
        failed = run(
            [
                cli,
                "orchestrate",
                "run",
                *common,
                "--orchestration-id",
                "wheel-managed-smoke",
                "--max-actions",
                "1",
                "--total-timeout-seconds",
                "120",
            ],
            env=environment,
            expected=5,
        )
        if "action remains resumable" not in failed.stderr:
            raise SystemExit(f"managed failure did not advertise resumability:\n{failed.stderr}")
        if "<output truncated>" not in failed.stderr or len(failed.stderr.encode("utf-8")) > 140 * 1024:
            raise SystemExit("managed runner diagnostics were not bounded")
        if failure_marker.read_text(encoding="utf-8") != "action-0001":
            raise SystemExit("fake runner did not fail the first persisted action")

        environment.pop("EVIDENCE_WIKI_FAKE_CODEX_FAILURE_BYTES")
        environment["EVIDENCE_WIKI_FAKE_CODEX_TOUCH_CONTROL"] = "1"
        run(
            [
                cli,
                "orchestrate",
                "resume",
                *common,
                "--orchestration-id",
                "wheel-managed-smoke",
            ],
            env=environment,
            expected=4,
        )
        status = json.loads(
            run(
                [
                    cli,
                    "orchestrate",
                    "status",
                    "--target",
                    str(workspace),
                    "--orchestration-id",
                    "wheel-managed-smoke",
                    "--format",
                    "json",
                ],
                expected=4,
            ).stdout
        )
        session = status.get("session", status)
        if session.get("status") != "paused" or session.get("verdict") != "paused":
            raise SystemExit(f"managed smoke did not pause at its action bound: {session}")
        if session.get("action_count") != 1 or session.get("completed_action_count") != 1:
            raise SystemExit(f"managed resume did not reconcile exactly one action: {session}")
        if session.get("last_completed_action_id") != "action-0001":
            raise SystemExit(f"managed resume completed the wrong action: {session}")

        orchestration_root = workspace / "runs" / "orchestrations" / "wheel-managed-smoke"
        retained_result = orchestration_root / "work-results" / "action-0001.json"
        if not retained_result.is_file():
            raise SystemExit("managed resume did not retain the submitted result")
        if (orchestration_root / ".host-results" / "action-0001.json").exists():
            raise SystemExit("managed resume left its host-staged result behind")
        attempts = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((orchestration_root / "attempts").glob("*.json"))
        ]
        if sorted(document.get("status") for document in attempts) != ["runner_failed", "submitted"]:
            raise SystemExit(f"managed resume did not retain its bounded attempt history: {attempts}")
        if any("prompt" in document or "diagnostic" in document for document in attempts):
            raise SystemExit("managed attempt history retained forbidden runner content")

        preflight_environment = environment.copy()
        preflight_environment.pop("EVIDENCE_WIKI_FAKE_CODEX_FAIL_ONCE")
        preflight_environment.pop("EVIDENCE_WIKI_FAKE_CODEX_TOUCH_CONTROL")
        preflight_environment["EVIDENCE_WIKI_FAKE_CODEX_PROBE_FAIL"] = "1"
        preflight_id = "wheel-preflight-smoke"
        refused = run(
            [
                cli,
                "orchestrate",
                "run",
                "--target",
                str(workspace),
                "--runner",
                "codex",
                "--agent-id",
                "wheel-smoke-agent",
                "--orchestration-id",
                preflight_id,
            ],
            env=preflight_environment,
            expected=5,
        )
        if "RUNNER_ISOLATION_UNAVAILABLE" not in refused.stderr:
            raise SystemExit(f"failed isolation preflight used the wrong diagnostic:\n{refused.stderr}")
        if (workspace / "runs" / "orchestrations" / preflight_id).exists():
            raise SystemExit("failed isolation preflight created an orchestration session")

        tampered_workspace = temporary_root / "tampered workspace"
        run(
            [
                cli,
                "deploy",
                "--target",
                str(tampered_workspace),
                "--project-name",
                "managed-wheel-tamper-smoke",
                "--project-description",
                "Installed-wheel control-tripwire smoke",
            ]
        )
        tampered_batch = tampered_workspace / "batch.yaml"
        tampered_batch.write_text(batch.read_text(encoding="utf-8"), encoding="utf-8")
        run(
            [
                cli,
                "questions",
                "add",
                "--target",
                str(tampered_workspace),
                "--from-file",
                str(tampered_batch),
            ]
        )
        tamper_environment = environment.copy()
        tamper_environment.pop("EVIDENCE_WIKI_FAKE_CODEX_FAIL_ONCE")
        tamper_environment.pop("EVIDENCE_WIKI_FAKE_CODEX_TOUCH_CONTROL")
        tamper_environment["EVIDENCE_WIKI_FAKE_CODEX_TAMPER_CONTROL"] = "1"
        tampered = run(
            [
                cli,
                "orchestrate",
                "run",
                "--target",
                str(tampered_workspace),
                "--runner",
                "codex",
                "--agent-id",
                "wheel-smoke-agent",
                "--orchestration-id",
                "wheel-tamper-smoke",
                "--max-actions",
                "1",
            ],
            env=tamper_environment,
            expected=5,
        )
        if (
            "CONTROL_ARTIFACT_TAMPERED" not in tampered.stderr
            or "research.yml [content_changed]" not in tampered.stderr
        ):
            raise SystemExit(f"semantic control tamper was not diagnosed exactly:\n{tampered.stderr}")
        if "did not roll back" not in tampered.stderr:
            raise SystemExit("semantic control tamper did not preserve operator-inspection semantics")
        if "# fake semantic tamper" not in (tampered_workspace / "research.yml").read_text(encoding="utf-8"):
            raise SystemExit("semantic control tamper was unexpectedly restored")
        tampered_orchestration = tampered_workspace / "runs" / "orchestrations" / "wheel-tamper-smoke"
        if list((tampered_orchestration / "work-results").glob("*.json")):
            raise SystemExit("semantic control tamper submitted a worker result")
        tamper_attempts = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((tampered_orchestration / "attempts").glob("*.json"))
        ]
        if len(tamper_attempts) != 1 or tamper_attempts[0].get("status") != "control_tampered":
            raise SystemExit(f"semantic control tamper did not retain its attempt record: {tamper_attempts}")
        quarantine = list((tampered_orchestration / "quarantine").glob("*.json"))
        if len(quarantine) != 1:
            raise SystemExit("semantic control tamper did not quarantine exactly one validated result")
        quarantined = json.loads(quarantine[0].read_text(encoding="utf-8"))
        if quarantined.get("reason_code") != "CONTROL_ARTIFACT_TAMPERED":
            raise SystemExit(f"semantic control quarantine is invalid: {quarantined}")

        repair_gate = run(
            [
                cli,
                "orchestrate",
                "resume",
                "--target",
                str(tampered_workspace),
                "--runner",
                "codex",
                "--agent-id",
                "wheel-smoke-agent",
                "--orchestration-id",
                "wheel-tamper-smoke",
            ],
            env=environment,
            expected=5,
        )
        if "CONTROL_REPAIR_REQUIRED" not in repair_gate.stderr:
            raise SystemExit(f"semantic control tamper did not gate resume:\n{repair_gate.stderr}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
