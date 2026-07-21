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


def tiny_pdf_bytes() -> bytes:
    """Return a dependency-free one-page PDF with extractable text."""
    stream = (
        "BT\n"
        "/F1 18 Tf\n"
        "72 720 Td\n"
        "(Installed Wheel PDF Smoke) Tj\n"
        "0 -36 Td\n"
        "/F1 12 Tf\n"
        "(Abstract) Tj\n"
        "0 -18 Td\n"
        "(The installed pypdf backend extracts this portable PDF without Poppler.) Tj\n"
        "0 -30 Td\n"
        "(1 Verification) Tj\n"
        "0 -18 Td\n"
        "(This body is long enough to be retained as usable normalized evidence.) Tj\n"
        "ET\n"
    ).encode("ascii")
    objects = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        (
            b"3 0 obj\n"
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>\n"
            b"endobj\n"
        ),
        b"4 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n",
        (
            b"5 0 obj\n<< /Length "
            + str(len(stream)).encode("ascii")
            + b" >>\nstream\n"
            + stream
            + b"endstream\nendobj\n"
        ),
    ]
    pdf = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
    offsets: list[int] = []
    for obj in objects:
        offsets.append(len(pdf))
        pdf += obj
    xref_offset = len(pdf)
    pdf += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    pdf += b"0000000000 65535 f \n"
    for offset in offsets:
        pdf += f"{offset:010d} 00000 n \n".encode("ascii")
    pdf += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n"
    ).encode("ascii")
    return pdf


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


def verify_installed_pdf_backend(cli: str, workspace: Path) -> None:
    run(
        [
            cli,
            "deploy",
            "--target",
            str(workspace),
            "--project-name",
            "installed-wheel-pdf-smoke",
            "--project-description",
            "Installed-wheel portable PDF normalization smoke",
        ]
    )
    raw_pdf = workspace / "raw" / "pdf" / "installed-wheel-smoke.pdf"
    raw_pdf.parent.mkdir(parents=True, exist_ok=True)
    raw_pdf.write_bytes(tiny_pdf_bytes())
    source_id = "raw:installed-wheel-pdf-smoke"
    manifest = workspace / "sources" / "manifest.jsonl"
    manifest.write_text(
        json.dumps(
            {
                "id": source_id,
                "kind": "pdf",
                "status": "discovered",
                "pairing_status": "pdf_only",
                "raw_paths": ["raw/pdf/installed-wheel-smoke.pdf"],
                "raw_pdf": "raw/pdf/installed-wheel-smoke.pdf",
            },
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    normalized = run(
        [
            sys.executable,
            str(workspace / "scripts" / "normalize_sources.py"),
            "--project-root",
            str(workspace),
            "--source-id",
            source_id,
            "--format",
            "json",
        ]
    )
    report = json.loads(normalized.stdout)
    actions = report.get("actions")
    if not isinstance(actions, list) or len(actions) != 1:
        raise SystemExit(f"installed wheel returned an invalid PDF normalization report: {report}")
    action = actions[0]
    if not isinstance(action, dict):
        raise SystemExit(f"installed wheel returned an invalid PDF normalization action: {report}")
    extractor = action.get("pdf_extractor")
    if (
        action.get("status") != "content_extracted"
        or not isinstance(extractor, dict)
        or extractor.get("name") != "pypdf"
    ):
        raise SystemExit(f"installed wheel did not use its portable PDF backend: {report}")
    output = workspace / str(action.get("output", ""))
    if not output.is_file() or "Installed Wheel PDF Smoke" not in output.read_text(encoding="utf-8"):
        raise SystemExit("installed wheel did not retain extracted PDF text")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cli", required=True, type=Path)
    args = parser.parse_args()
    cli = str(args.cli.resolve())

    contract = json.loads(run([cli, "contract", "--format", "json"]).stdout)
    if contract.get("starter_version") != "0.5.4":
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
        verify_installed_pdf_backend(cli, temporary_root / "portable PDF workspace")
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
