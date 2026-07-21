"""Network-free integration of selected-candidate planning and acquisition."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"


def load_script_module(name: str, filename: str):
    path = SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load script from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


INIT = load_script_module("plan_fetch_acquisition_init", "init_research_workspace.py")
RUN_CONTROLLER = load_script_module("plan_fetch_acquisition_run_controller", "run_controller.py")
SOURCE_REQUESTS = load_script_module("plan_fetch_acquisition_requests", "source_requests.py")
DISCOVER = load_script_module("plan_fetch_acquisition_discover", "discover_sources.py")
FETCH = load_script_module("plan_fetch_acquisition_fetch", "fetch_sources.py")
INVENTORY = load_script_module("plan_fetch_acquisition_inventory", "source_inventory.py")
NORMALIZE = load_script_module("plan_fetch_acquisition_normalize", "normalize_sources.py")

ARXIV_ID = "2601.12345v2"
DOI = "10.5555/selected-candidate-integration"
ARXIV_PAYLOAD = f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>https://arxiv.org/abs/{ARXIV_ID}</id>
    <published>2026-01-10T00:00:00Z</published>
    <updated>2026-01-12T00:00:00Z</updated>
    <title>Selected Candidate Acquisition Integration</title>
    <summary>Reports evidence delivered through the exact selected candidate plan.</summary>
    <author><name>Ada Example</name></author>
    <arxiv:doi>{DOI}</arxiv:doi>
    <link rel="alternate" href="https://arxiv.org/abs/{ARXIV_ID}" />
    <link title="pdf" href="https://arxiv.org/pdf/{ARXIV_ID}" />
  </entry>
</feed>
""".encode()


def source_archive() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        content = (
            b"\\documentclass{article}\n"
            b"\\begin{document}\n"
            b"The selected candidate preserves request and candidate provenance.\n"
            b"\\end{document}\n"
        )
        info = tarfile.TarInfo("main.tex")
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


class SelectedCandidateAcquisitionIntegrationTests(unittest.TestCase):
    def run_module(self, module, argv: list[str]) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = module.main(argv)
        return int(code or 0), stdout.getvalue(), stderr.getvalue()

    def run_json(self, module, argv: list[str]) -> dict:
        code, stdout, stderr = self.run_module(module, argv)
        self.assertEqual(0, code, stderr)
        return json.loads(stdout)

    def transition_run(self, workspace: Path, run_id: str, state: str) -> None:
        self.run_json(
            RUN_CONTROLLER,
            [
                "--project-root",
                str(workspace),
                "transition",
                "--run-id",
                run_id,
                "--agent-id",
                "integration-agent",
                "--to-state",
                state,
                "--reason",
                f"Enter {state} for the selected-candidate integration test.",
                "--format",
                "json",
            ],
        )

    def test_selected_plan_executes_provider_argv_then_inventories_and_normalizes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "deployed workspace"
            code, _stdout, stderr = self.run_module(
                INIT,
                [
                    "--target",
                    str(workspace),
                    "--project-name",
                    "selected-candidate-integration",
                    "--project-description",
                    "Exercise the exact selected candidate acquisition plan.",
                ],
            )
            self.assertEqual(0, code, stderr)

            config_path = workspace / "research.yml"
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config.setdefault("integrations", {})["discovery"] = {
                "enabled": True,
                "providers": ["arxiv"],
                "candidate_store_path": "sources/discovery/candidates.jsonl",
            }
            config["integrations"]["acquisition"] = {
                "enabled": True,
                "providers": ["arxiv"],
                "target_root": "raw/papers",
                "max_downloads_per_run": 10,
                "require_license_check": True,
            }
            config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

            request = self.run_json(
                SOURCE_REQUESTS,
                [
                    "--project-root",
                    str(workspace),
                    "add",
                    "--kind",
                    "paper",
                    "--query-or-identifier",
                    "selected candidate acquisition integration",
                    "--rationale",
                    "Prove the planner and provider preserve correlation metadata.",
                    "--priority",
                    "high",
                    "--format",
                    "json",
                ],
            )["request"]
            request_id = request["request_id"]

            run_id = "run-selected-candidate-integration"
            self.run_json(
                RUN_CONTROLLER,
                [
                    "--project-root",
                    str(workspace),
                    "start",
                    "--run-id",
                    run_id,
                    "--agent-id",
                    "integration-agent",
                    "--format",
                    "json",
                ],
            )
            self.transition_run(workspace, run_id, "planned")
            self.transition_run(workspace, run_id, "discovering")

            discovery_calls: list[str] = []

            def discovery_transport(url: str, _timeout: float, _headers: dict[str, str]) -> bytes:
                discovery_calls.append(url)
                return ARXIV_PAYLOAD

            with (
                mock.patch.object(DISCOVER, "ARXIV_TRANSPORT", discovery_transport),
                mock.patch.object(DISCOVER, "ARXIV_CLOCK", lambda: 0.0),
                mock.patch.object(DISCOVER, "ARXIV_SLEEP", lambda _seconds: None),
                mock.patch.object(DISCOVER, "ARXIV_LAST_REQUEST_AT", None),
            ):
                discovery = self.run_json(
                    DISCOVER,
                    [
                        "--project-root",
                        str(workspace),
                        "--format",
                        "json",
                        "academic",
                        "--request-id",
                        request_id,
                        "--provider",
                        "arxiv",
                        "--run-id",
                        run_id,
                        "--max-results",
                        "5",
                    ],
                )
            self.assertEqual(1, discovery["count"])
            self.assertEqual(1, len(discovery_calls))
            candidate_id = discovery["candidates"][0]["candidate_id"]

            self.run_json(
                DISCOVER,
                [
                    "--project-root",
                    str(workspace),
                    "--format",
                    "json",
                    "candidates",
                    "select",
                    "--candidate-id",
                    candidate_id,
                    "--request-id",
                    request_id,
                    "--reason",
                    "Selected the academic-primary arXiv candidate for exact-route acquisition.",
                    "--actor",
                    "integration-reviewer",
                    "--run-id",
                    run_id,
                ],
            )

            plan = self.run_json(
                SOURCE_REQUESTS,
                [
                    "--project-root",
                    str(workspace),
                    "plan-fetch",
                    "--request-id",
                    request_id,
                    "--candidate-id",
                    candidate_id,
                    "--format",
                    "json",
                ],
            )
            self.assertEqual("selected_candidates", plan["routing_basis"])
            self.assertEqual([], plan["routes"])
            self.assertEqual([candidate_id], [route["candidate_id"] for route in plan["candidate_routes"]])
            route = plan["candidate_routes"][0]
            self.assertEqual("download-source", route["route"])
            provider_argvs = [
                route["command_argv"],
                *(companion["command_argv"] for companion in route["companion_commands"]),
            ]

            fetch_calls: list[str] = []
            archive = source_archive()

            def fetch_transport(url: str, _timeout: float) -> bytes:
                fetch_calls.append(url)
                if "export.arxiv.org/api/query" in url:
                    return ARXIV_PAYLOAD
                if "/pdf/" in url:
                    return b"%PDF-1.4\nNetwork-free selected-candidate fixture.\n"
                return archive

            self.assertEqual(
                [],
                [
                    path
                    for path in (workspace / "raw").rglob("*")
                    if path.is_file() and path.name != ".gitkeep"
                ],
            )
            with (
                mock.patch.object(FETCH, "ARXIV_TRANSPORT", fetch_transport),
                mock.patch.object(FETCH, "ARXIV_CLOCK", lambda: 0.0),
                mock.patch.object(FETCH, "ARXIV_SLEEP", lambda _seconds: None),
                mock.patch.object(FETCH, "ARXIV_LAST_REQUEST_AT", None),
            ):
                for command_argv in provider_argvs:
                    self.assertEqual(["python3", "scripts/fetch_sources.py"], command_argv[:2])
                    report = self.run_json(
                        FETCH,
                        ["--project-root", str(workspace), *command_argv[2:]],
                    )
                    self.assertEqual(request_id, yaml.safe_load(
                        (workspace / report["sidecar_path"]).read_text(encoding="utf-8")
                    )["request_id"])

            self.assertEqual(4, len(fetch_calls))
            inventory = self.run_json(
                INVENTORY,
                ["--project-root", str(workspace), "--report", "--format", "json"],
            )
            self.assertEqual("ready_for_normalization", inventory["readiness"])
            normalization = self.run_json(
                NORMALIZE,
                ["--project-root", str(workspace), "--all", "--format", "json"],
            )
            self.assertEqual(1, normalization["summary"]["created"])

            manifest_records = [
                json.loads(line)
                for line in (workspace / "sources" / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(1, len(manifest_records))
            source = manifest_records[0]
            source_id = source["id"]
            self.assertEqual(request_id, source["provenance"]["request_id"])
            self.assertEqual(candidate_id, source["provenance"]["candidate_id"])
            self.assertEqual(run_id, source["provenance"]["acquisition_run_id"])
            normalized_path = NORMALIZE.normalized_output_path_for_record(
                source,
                workspace / "sources" / "normalized",
            )
            self.assertTrue(normalized_path.is_file())
            normalized_text = normalized_path.read_text(encoding="utf-8")
            self.assertIn(request_id, normalized_text)
            self.assertIn(candidate_id, normalized_text)

            transition = self.run_json(
                DISCOVER,
                [
                    "--project-root",
                    str(workspace),
                    "--format",
                    "json",
                    "candidates",
                    "transition",
                    "--candidate-id",
                    candidate_id,
                    "--expected-state",
                    "selected",
                    "--to-state",
                    "fetched",
                    "--reason",
                    "Exact planned acquisition was inventoried and normalized.",
                    "--source-id",
                    source_id,
                    "--actor",
                    "integration-acquirer",
                    "--run-id",
                    run_id,
                ],
            )
            self.assertEqual("fetched", transition["candidate"]["status"])
            self.assertEqual(source_id, transition["candidate"]["fetched_source_id"])

            fulfilled = self.run_json(
                SOURCE_REQUESTS,
                [
                    "--project-root",
                    str(workspace),
                    "fulfill",
                    "--request-id",
                    request_id,
                    "--source-id",
                    source_id,
                    "--format",
                    "json",
                ],
            )
            self.assertEqual("fulfilled", fulfilled["request"]["status"])
            self.assertEqual(source_id, fulfilled["request"]["source_id"])


if __name__ == "__main__":
    unittest.main()
