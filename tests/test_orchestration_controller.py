import contextlib
import hashlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"


def load_script_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load script from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


INIT = load_script_module("orchestration_controller_init", SCRIPTS / "init_research_workspace.py")
INTAKE = load_script_module("orchestration_controller_intake", SCRIPTS / "intake_questions.py")
CONTROLLER = load_script_module("orchestration_controller_under_test", SCRIPTS / "orchestration_controller.py")
RUN_CONTROLLER = load_script_module("orchestration_child_run_controller", SCRIPTS / "run_controller.py")
STATUS = load_script_module("orchestration_workspace_status", SCRIPTS / "workspace_status.py")
SOURCE_REQUESTS = load_script_module("orchestration_source_requests", SCRIPTS / "source_requests.py")
CLAIM = load_script_module("orchestration_question_claim", SCRIPTS / "question_claim.py")
RESOLVE = load_script_module("orchestration_question_resolve", SCRIPTS / "question_resolve.py")
DISCOVER = load_script_module("orchestration_discover_sources", SCRIPTS / "discover_sources.py")
INVENTORY = load_script_module("orchestration_source_inventory", SCRIPTS / "source_inventory.py")
NORMALIZE = load_script_module("orchestration_normalize_sources", SCRIPTS / "normalize_sources.py")
COVERAGE = load_script_module("orchestration_coverage_manifest", SCRIPTS / "coverage_manifest.py")
VERIFY_QUOTES = load_script_module("orchestration_verify_quotes", SCRIPTS / "verify_quotes.py")
READINESS = load_script_module("orchestration_publication_readiness", SCRIPTS / "publication_readiness.py")

ACADEMIC_DOI = "10.5555/orchestration-solid-electrolyte"
ARXIV_PAYLOAD = f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>https://arxiv.org/abs/2601.12345v2</id>
    <published>2026-01-10T00:00:00Z</published>
    <updated>2026-01-12T00:00:00Z</updated>
    <title>Solid Electrolyte Conductivity Survey</title>
    <summary>Compares solid electrolyte families.</summary>
    <author><name>Ada Example</name></author>
    <arxiv:doi>{ACADEMIC_DOI}</arxiv:doi>
    <link rel="alternate" href="https://arxiv.org/abs/2601.12345v2" />
    <link title="pdf" href="https://arxiv.org/pdf/2601.12345v2" />
  </entry>
</feed>
""".encode()


def openalex_payload() -> bytes:
    return json.dumps(
        {
            "meta": {"count": 1},
            "results": [
                {
                    "id": "https://openalex.org/W12345",
                    "doi": f"https://doi.org/{ACADEMIC_DOI}",
                    "display_name": "Solid Electrolyte Conductivity Survey",
                    "publication_year": 2026,
                    "type": "article",
                    "cited_by_count": 12,
                    "authorships": [{"author": {"display_name": "Ada Example"}}],
                    "open_access": {"is_oa": True, "oa_status": "green"},
                    "best_oa_location": {
                        "landing_page_url": "https://arxiv.org/abs/2601.12345v2",
                        "pdf_url": "https://arxiv.org/pdf/2601.12345v2",
                        "license": "cc-by-4.0",
                    },
                }
            ],
        }
    ).encode()


class OrchestrationControllerTests(unittest.TestCase):
    def run_module(self, module, argv: list[str]) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = module.main(argv)
        return int(code or 0), stdout.getvalue(), stderr.getvalue()

    def init_workspace(self, root: Path, *, question: bool = False) -> Path:
        target = root / "workspace"
        code, _, stderr = self.run_module(
            INIT,
            [
                "--target",
                str(target),
                "--project-name",
                "orchestration-test",
                "--project-description",
                "Workspace for orchestration controller tests.",
            ],
        )
        self.assertEqual(0, code, stderr)
        if question:
            batch = root / "batch.json"
            batch.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "questions": [
                            {
                                "id": "test-question",
                                "question": "Which evidence answers this test question?",
                                "priority": "high",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            code, _, stderr = self.run_module(
                INTAKE,
                ["--project-root", str(target), "--from-file", str(batch), "--format", "json"],
            )
            self.assertEqual(0, code, stderr)
        return target

    def controller(self, target: Path, command: str, *args: str) -> tuple[int, dict, str]:
        code, stdout, stderr = self.run_module(
            CONTROLLER,
            ["--project-root", str(target), command, *args, "--format", "json"],
        )
        payload = json.loads(stdout or stderr)
        return code, payload, stderr

    def hydrated_order(self, target: Path, order: dict) -> dict:
        return CONTROLLER.hydrate_integrity_baselines(target, order)

    def json_script(self, module, argv: list[str]) -> tuple[int, dict, str]:
        code, stdout, stderr = self.run_module(module, argv)
        payload_text = stdout.strip() or stderr.strip()
        return code, json.loads(payload_text) if payload_text else {}, stderr

    def assert_json_script_ok(self, module, argv: list[str]) -> dict:
        code, payload, stderr = self.json_script(module, argv)
        self.assertEqual(0, code, stderr)
        return payload

    def build_verification_bundle(self, target: Path, run_id: str) -> dict:
        return self.assert_json_script_ok(
            READINESS,
            [
                "--project-root",
                str(target),
                "--format",
                "json",
                "bundle",
                "--run-id",
                run_id,
            ],
        )

    def enable_academic_providers(self, target: Path) -> None:
        config_path = target / "research.yml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        config.setdefault("integrations", {})["discovery"] = {
            "enabled": True,
            "providers": ["arxiv", "openalex"],
            "candidate_store_path": "sources/discovery/candidates.jsonl",
        }
        config["integrations"]["acquisition"] = {
            "enabled": True,
            "providers": ["arxiv", "openalex"],
            "target_root": "raw/papers",
            "max_downloads_per_run": 10,
            "require_license_check": True,
        }
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        (target / "sources" / "discovery").mkdir(parents=True, exist_ok=True)

    def manifest_records(self, target: Path) -> list[dict]:
        path = target / "sources" / "manifest.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def write_mock_acquired_paper(self, target: Path, *, request_id: str, candidate_id: str) -> str:
        relative = "raw/papers/solid-electrolyte.html"
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "<html><head><title>Solid Electrolyte Conductivity Survey</title></head>"
            "<body>Room-temperature ionic conductivity exceeds 1 mS/cm for the reported sulfide family.</body>"
            "</html>\n",
            encoding="utf-8",
        )
        sidecar = {
            "origin_url": "https://arxiv.org/abs/2601.12345v2",
            "retrieved_at": "2026-07-20T00:00:00Z",
            "retrieved_by": "fetch_sources.py/arxiv",
            "license": "CC-BY-4.0",
            "terms_url": "https://info.arxiv.org/help/license/index.html",
            "terms_note": "Mocked offline acquisition uses explicit arXiv provenance.",
            "notes": "Network-free acquisition fixture for the parent orchestrator.",
            "request_id": request_id,
            "candidate_id": candidate_id,
            "academic_provider": "arxiv",
            "academic_source_type": "preprint",
            "arxiv_id": "2601.12345v2",
            "doi": ACADEMIC_DOI,
            "title": "Solid Electrolyte Conductivity Survey",
            "authors": ["Ada Example"],
            "published": "2026-01-10T00:00:00Z",
            "checksum": f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}",
        }
        (target / f"{relative}.provenance.yml").write_text(
            yaml.safe_dump(sidecar, sort_keys=False),
            encoding="utf-8",
        )
        return relative

    def write_grounded_answer(self, target: Path, source_id: str) -> None:
        answer = target / "wiki" / "synthesis" / "test-answer.md"
        answer.write_text(
            "---\n"
            "type: synthesis\n"
            "created: 2026-07-20\n"
            "updated: 2026-07-20\n"
            "source_ids:\n"
            f"  - {source_id}\n"
            "summary: The acquired paper reports qualifying room-temperature conductivity.\n"
            "---\n\n"
            "# Solid Electrolyte Conductivity\n\n"
            "The cited paper reports a sulfide electrolyte family above the requested threshold.\n",
            encoding="utf-8",
        )
        source_note = target / "wiki" / "sources" / f"{NORMALIZE.safe_source_id(source_id)}-source.md"
        source_note.write_text(
            "---\n"
            "type: source\n"
            "created: 2026-07-20\n"
            "updated: 2026-07-20\n"
            "source_ids:\n"
            f"  - {source_id}\n"
            "---\n\n"
            "# Solid Electrolyte Conductivity Survey\n\n"
            "Source note for the acquired paper.\n",
            encoding="utf-8",
        )

        question_path = target / "wiki" / "questions" / "test-question.md"
        question_text = question_path.read_text(encoding="utf-8")
        parts = CLAIM.split_frontmatter_lines(question_text)
        self.assertIsNotNone(parts)
        frontmatter_lines, opening, rest = parts
        frontmatter_lines.extend(
            [
                "grounding:",
                "  - claim: A sulfide electrolyte family exceeds the requested conductivity threshold.",
                f"    source_id: {source_id}",
                "    quote: Room-temperature ionic conductivity exceeds 1 mS/cm for the reported sulfide family.",
                "    location_hint: Solid Electrolyte Conductivity Survey",
            ]
        )
        CLAIM.write_page_atomic(question_path, "\n".join([*opening, *frontmatter_lines, *rest]))

        coverage = {
            "schema_version": "1.0",
            "question_slug": "test-question",
            "created_at": "2026-07-20T00:00:00Z",
            "updated_at": "2026-07-20T00:00:00Z",
            "coverage_profile": "academic-paper-evidence",
            "coverage_verdict": "pending",
            "required_facets": [
                {
                    "facet_id": "conductivity-threshold",
                    "description": "An indexed academic source reports conductivity above the threshold.",
                    "required": True,
                    "evidence_path": "academic_method_existence",
                    "source_policy": "academic_indexed",
                    "freshness_policy": "publication_identity",
                    "identity_policy": "citation_id_resolves",
                    "min_sources": 1,
                    "accepted_source_ids": [source_id],
                    "blocking_request_ids": [],
                    "facet_verdict": "pending",
                }
            ],
            "optional_facets": [],
        }
        coverage_path = target / "sources" / "coverage" / "test-question.yml"
        coverage_path.parent.mkdir(parents=True, exist_ok=True)
        coverage_path.write_text(yaml.safe_dump(coverage, sort_keys=False), encoding="utf-8")

    def start(self, target: Path, orchestration_id: str = "orch-test", **limits: int) -> dict:
        args = ["--orchestration-id", orchestration_id, "--agent-id", "agent-test"]
        for key, value in limits.items():
            args.extend([f"--{key.replace('_', '-')}", str(value)])
        code, payload, stderr = self.controller(target, "start", *args)
        self.assertEqual(0, code, stderr)
        return payload

    def add_questions(self, root: Path, target: Path, questions: list[dict]) -> None:
        batch = root / f"batch-{len(questions)}-{questions[0]['id']}.json"
        batch.write_text(
            json.dumps({"schema_version": "1.0", "questions": questions}),
            encoding="utf-8",
        )
        code, _, stderr = self.run_module(
            INTAKE,
            ["--project-root", str(target), "--from-file", str(batch), "--format", "json"],
        )
        self.assertEqual(0, code, stderr)

    def block_question(self, target: Path, slug: str = "test-question") -> str:
        self.assert_json_script_ok(
            CLAIM,
            [
                "--project-root",
                str(target),
                "claim",
                "--slug",
                slug,
                "--agent-id",
                "agent-test",
                "--format",
                "json",
            ],
        )
        request = self.assert_json_script_ok(
            SOURCE_REQUESTS,
            [
                "--project-root",
                str(target),
                "add",
                "--kind",
                "paper",
                "--query-or-identifier",
                f"Evidence needed for {slug}",
                "--rationale",
                "The scoped question cannot be answered from delivered evidence.",
                "--priority",
                "high",
                "--question-slug",
                slug,
                "--format",
                "json",
            ],
        )["request"]
        self.assert_json_script_ok(
            RESOLVE,
            [
                "--project-root",
                str(target),
                "block",
                "--slug",
                slug,
                "--agent-id",
                "agent-test",
                "--blocked-reason",
                "The scoped question requires additional evidence.",
                "--request-id",
                request["request_id"],
                "--format",
                "json",
            ],
        )
        return request["request_id"]

    def submit(
        self,
        root: Path,
        target: Path,
        action_id: str,
        *,
        outcome: str = "completed",
        summary: str = "Action completed.",
        artifacts: list[str] | None = None,
    ) -> tuple[int, dict, str]:
        result_path = root / f"{action_id}-{outcome}-{abs(hash(summary))}.json"
        result_path.write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "action_id": action_id,
                    "outcome": outcome,
                    "summary": summary,
                    "artifacts": artifacts or [],
                }
            ),
            encoding="utf-8",
        )
        return self.controller(
            target,
            "submit",
            "--orchestration-id",
            "orch-test",
            "--action-id",
            action_id,
            "--result-file",
            str(result_path),
            "--agent-id",
            "agent-test",
        )

    def test_start_next_and_pending_replay_are_durable_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            session = self.start(target)

            code, first, stderr = self.controller(
                target,
                "next",
                "--orchestration-id",
                "orch-test",
                "--agent-id",
                "agent-test",
            )
            self.assertEqual(0, code, stderr)
            code, replay, stderr = self.controller(
                target,
                "next",
                "--orchestration-id",
                "orch-test",
                "--agent-id",
                "agent-test",
            )
            self.assertEqual(0, code, stderr)

            self.assertEqual("orchestration_session", session["artifact_type"])
            self.assertEqual(first, replay)
            self.assertEqual("orchestration_work_order", first["artifact_type"])
            self.assertEqual("research", first["phase"])
            self.assertEqual("research-run", first["skill"])
            self.assertEqual(["test-question"], first["scope"]["question_slugs"])
            retained = target / "runs" / "orchestrations" / "orch-test" / "work-orders" / "action-0001.json"
            self.assertEqual(first, json.loads(retained.read_text(encoding="utf-8")))
            pending_session = CONTROLLER.load_session(target, "orch-test")
            fingerprint_summary = pending_session["pending_trusted_static_inputs"]
            self.assertNotIn("entries", fingerprint_summary)
            fingerprint_path = CONTROLLER.trusted_static_input_path(target, "orch-test", first["action_id"])
            fingerprint = json.loads(fingerprint_path.read_text(encoding="utf-8"))
            self.assertTrue(CONTROLLER.valid_trusted_static_input_fingerprint(fingerprint))
            self.assertEqual(fingerprint["fingerprint"], fingerprint_summary["fingerprint"])
            child = RUN_CONTROLLER.load_run_state(target, first["run_id"])
            self.assertEqual("answering", child["state"]["current"])
            self.assertEqual(
                ["initialized", "planned", "answering"],
                [item["to_state"] for item in child["state_history"]],
            )

            expired = json.loads(retained.read_text(encoding="utf-8"))
            expired["lease"]["expires_at"] = (
                datetime.now(timezone.utc) - timedelta(seconds=1)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            retained.write_text(json.dumps(expired, indent=2) + "\n", encoding="utf-8")
            code, reissued, stderr = self.controller(
                target,
                "next",
                "--orchestration-id",
                "orch-test",
                "--agent-id",
                "agent-test",
                "--resume",
            )
            self.assertEqual(0, code, stderr)
            self.assertEqual(first["action_id"], reissued["action_id"])
            self.assertEqual(2, reissued["lease"]["attempt"])
            self.assertNotEqual(expired["lease"]["expires_at"], reissued["lease"]["expires_at"])

    def test_required_control_repair_marker_blocks_protocol_next_and_submit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.start(target)
            _, order, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
            marker_path = CONTROLLER.control_repair_path(target, "orch-test")
            marker = {
                "schema_version": "1.0",
                "artifact_type": "orchestration_control_repair",
                "orchestration_id": "orch-test",
                "status": "required",
                "reason_code": "CONTROL_ARTIFACT_TAMPERED",
                "detected_at": "2026-07-21T00:00:00Z",
                "acknowledged_at": None,
                "attempt_ids": ["attempt-test"],
                "expected_control_fingerprint": f"sha256:{'0' * 64}",
            }
            CONTROLLER.write_json_atomic(marker_path, marker)

            code, error, _ = self.controller(
                target,
                "next",
                "--orchestration-id",
                "orch-test",
                "--resume",
            )
            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("ORCHESTRATION_CONTROL_REPAIR_REQUIRED", error["error_code"])

            code, error, _ = self.submit(root, target, order["action_id"])
            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("ORCHESTRATION_CONTROL_REPAIR_REQUIRED", error["error_code"])
            self.assertFalse(CONTROLLER.work_result_path(target, "orch-test", order["action_id"]).exists())

            marker["status"] = "acknowledged"
            marker["acknowledged_at"] = "2026-07-21T00:05:00Z"
            CONTROLLER.write_json_atomic(marker_path, marker)
            code, replayed, stderr = self.controller(
                target,
                "next",
                "--orchestration-id",
                "orch-test",
                "--resume",
            )
            self.assertEqual(0, code, stderr)
            self.assertEqual(order["action_id"], replayed["action_id"])

    def test_trusted_static_fingerprint_tracks_semantics_and_excludes_generated_run_reports(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            baseline = CONTROLLER.trusted_static_input_fingerprint(target)
            self.assertTrue(CONTROLLER.valid_trusted_static_input_fingerprint(baseline))
            agents_entry = next(item for item in baseline["entries"] if item["path"] == "AGENTS.md")
            self.assertEqual({"path", "kind", "mode", "size", "sha256"}, set(agents_entry))
            self.assertEqual("file", agents_entry["kind"])

            agents = target / "AGENTS.md"
            agents_stat = agents.stat()
            os.utime(agents, ns=(agents_stat.st_atime_ns, agents_stat.st_mtime_ns + 1_000_000_000))
            self.assertEqual(baseline, CONTROLLER.trusted_static_input_fingerprint(target))

            report = target / "runs" / "run-reports" / "worker-output.md"
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text("# Writable run report\n", encoding="utf-8")
            self.assertEqual(baseline, CONTROLLER.trusted_static_input_fingerprint(target))

            original_agents = agents.read_bytes()
            original_agents_mode = stat.S_IMODE(agents.stat().st_mode)
            agents.write_bytes(original_agents + b"\nsemantic change\n")
            self.assertNotEqual(baseline["fingerprint"], CONTROLLER.trusted_static_input_fingerprint(target)["fingerprint"])
            agents.write_bytes(original_agents)
            agents.chmod(original_agents_mode)

            if os.name != "nt":
                agents.chmod(original_agents_mode ^ stat.S_IXUSR)
                self.assertNotEqual(
                    baseline["fingerprint"],
                    CONTROLLER.trusted_static_input_fingerprint(target)["fingerprint"],
                )
                agents.chmod(original_agents_mode)

            static_doc = target / "docs" / "acquisition.md"
            original_doc = static_doc.read_bytes()
            original_doc_mode = stat.S_IMODE(static_doc.stat().st_mode)
            static_doc.unlink()
            static_doc.mkdir()
            self.assertNotEqual(baseline["fingerprint"], CONTROLLER.trusted_static_input_fingerprint(target)["fingerprint"])
            static_doc.rmdir()
            static_doc.write_bytes(original_doc)
            static_doc.chmod(original_doc_mode)

            added = target / "docs" / "new-static-input.md"
            added.write_text("new\n", encoding="utf-8")
            self.assertNotEqual(baseline["fingerprint"], CONTROLLER.trusted_static_input_fingerprint(target)["fingerprint"])
            added.unlink()

            skill = target / "skills" / "research-run.md"
            original_skill = skill.read_bytes()
            original_skill_mode = stat.S_IMODE(skill.stat().st_mode)
            skill.unlink()
            self.assertNotEqual(baseline["fingerprint"], CONTROLLER.trusted_static_input_fingerprint(target)["fingerprint"])
            skill.write_bytes(original_skill)
            skill.chmod(original_skill_mode)
            self.assertEqual(baseline["fingerprint"], CONTROLLER.trusted_static_input_fingerprint(target)["fingerprint"])

    def test_raw_tree_snapshot_detects_same_size_content_change_with_restored_mtime(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            raw_path = target / "raw" / "papers" / "immutable.txt"
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text("first-value", encoding="utf-8")
            config = CONTROLLER.load_config(target)
            before = CONTROLLER.raw_tree_snapshot(target, config)
            timestamps = (raw_path.stat().st_atime_ns, raw_path.stat().st_mtime_ns)

            raw_path.write_text("other-value", encoding="utf-8")
            os.utime(raw_path, ns=timestamps)
            after = CONTROLLER.raw_tree_snapshot(target, config)

            self.assertEqual("sha256-content-v1", before["algorithm"])
            self.assertEqual(before["file_count"], after["file_count"])
            self.assertEqual(before["total_bytes"], after["total_bytes"])
            self.assertNotEqual(before["fingerprint"], after["fingerprint"])

    def test_manifest_digest_detects_same_count_same_size_rewrite(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            manifest = target / "sources" / "manifest.jsonl"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text('{"source_id":"source-one"}\n', encoding="utf-8")
            before = CONTROLLER.evidence_manifest_digest(target)

            manifest.write_text('{"source_id":"source-two"}\n', encoding="utf-8")
            after = CONTROLLER.evidence_manifest_digest(target)

            self.assertNotEqual(before, after)

    def test_work_order_externalizes_integrity_baselines_and_detects_sidecar_tampering(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.start(target)
            code, order, stderr = self.controller(target, "next", "--orchestration-id", "orch-test")
            self.assertEqual(0, code, stderr)
            self.assertLessEqual(
                CONTROLLER.work_order_path(target, "orch-test", order["action_id"]).stat().st_size,
                CONTROLLER.MAX_WORK_ORDER_BYTES,
            )
            guard = next(
                item
                for item in order["required_postconditions"]
                if item["check"] == "controller_integrity_baseline"
            )
            readiness = next(
                item
                for item in order["required_postconditions"]
                if item["check"] == "workspace_readiness_changed"
            )
            self.assertNotIn("scoped_questions_before", readiness)
            hydrated = self.hydrated_order(target, order)
            hydrated_readiness = next(
                item
                for item in hydrated["required_postconditions"]
                if item["check"] == "workspace_readiness_changed"
            )
            self.assertEqual(["test-question"], sorted(hydrated_readiness["scoped_questions_before"]))

            sidecar = target / guard["path"]
            document = json.loads(sidecar.read_text(encoding="utf-8"))
            document["phase"] = "discovery"
            CONTROLLER.write_json_atomic(sidecar, document)
            with self.assertRaisesRegex(
                CONTROLLER.OrchestrationControllerError,
                "missing or changed",
            ):
                self.hydrated_order(target, order)

    def test_legacy_discovery_replay_refuses_missing_content_immutability_guards(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self.init_workspace(Path(tmpdir))
            order = {
                "orchestration_id": "orch-legacy",
                "action_id": "action-0001",
                "phase": "discovery",
                "required_postconditions": [
                    {
                        "check": "discovery_never_fetches",
                        "manifest_records_before": 0,
                    },
                    {
                        "check": "raw_tree_unchanged",
                        "before": {"file_count": 0, "total_bytes": 0, "fingerprint": "sha256:legacy"},
                    },
                ],
            }
            with self.assertRaisesRegex(
                CONTROLLER.OrchestrationControllerError,
                "immutability baseline",
            ):
                CONTROLLER.require_action_baselines(order)

    def test_verification_artifact_reads_are_bounded_and_do_not_follow_links(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            evaluation = target / "runs" / "run-test" / "evaluation"
            evaluation.mkdir(parents=True)
            oversized = evaluation / "oversized.json"
            with oversized.open("wb") as handle:
                handle.seek(CONTROLLER.MAX_VERIFICATION_ARTIFACT_BYTES)
                handle.write(b"x")
            with self.assertRaisesRegex(
                CONTROLLER.OrchestrationControllerError,
                "exceeds|unsafe",
            ):
                CONTROLLER.file_digest(oversized, containment_root=target)

            outside = target / "outside.json"
            outside.write_text("{}\n", encoding="utf-8")
            linked = evaluation / "linked.json"
            try:
                linked.symlink_to(outside)
            except OSError:
                self.skipTest("symbolic links are unavailable on this platform")
            with self.assertRaisesRegex(
                CONTROLLER.OrchestrationControllerError,
                "unsafe|singly linked",
            ):
                CONTROLLER.file_digest(linked, containment_root=target)
            with self.assertRaisesRegex(
                CONTROLLER.OrchestrationControllerError,
                "singly linked",
            ):
                CONTROLLER.load_json_object(
                    linked,
                    error_code="ORCHESTRATION_POSTCONDITION_FAILED",
                    label="linked verification artifact",
                    max_bytes=CONTROLLER.MAX_VERIFICATION_ARTIFACT_BYTES,
                    containment_root=target,
                )

    def test_verification_artifact_reads_accept_an_aliased_containment_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            actual = root / "actual"
            nested = actual / "nested"
            nested.mkdir(parents=True)
            artifact = nested / "artifact.json"
            content = b'{"ok": true}\n'
            artifact.write_bytes(content)
            alias = root / "alias"
            try:
                alias.symlink_to(actual, target_is_directory=True)
            except OSError:
                self.skipTest("directory symbolic links are unavailable on this platform")

            aliased_artifact = alias / "nested" / "artifact.json"
            self.assertEqual(
                content,
                CONTROLLER.bounded_regular_bytes(
                    aliased_artifact,
                    max_bytes=1024,
                    error_code="ORCHESTRATION_POSTCONDITION_FAILED",
                    label="verification artifact",
                    containment_root=alias,
                ),
            )
            self.assertEqual(
                f"sha256:{hashlib.sha256(content).hexdigest()}",
                CONTROLLER.file_digest(aliased_artifact, containment_root=alias),
            )

    def test_trusted_static_fingerprint_rejects_hardlinks_and_invalid_persisted_shape(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            fingerprint = CONTROLLER.trusted_static_input_fingerprint(target)
            invalid = json.loads(json.dumps(fingerprint))
            invalid["entries"].append(dict(invalid["entries"][0]))
            invalid["entry_count"] += 1
            invalid["fingerprint"] = CONTROLLER.result_digest({"entries": invalid["entries"]})
            self.assertFalse(CONTROLLER.valid_trusted_static_input_fingerprint(invalid))

            source = target / "wiki" / "hardlink-source.txt"
            source.write_text("hardlinked\n", encoding="utf-8")
            linked = target / "docs" / "hardlink.txt"
            try:
                os.link(source, linked)
            except OSError as exc:
                self.skipTest(f"hardlinks unavailable: {exc}")
            with self.assertRaisesRegex(CONTROLLER.OrchestrationControllerError, "multiply linked"):
                CONTROLLER.trusted_static_input_fingerprint(target)

    def test_submit_rejects_static_input_drift_and_legacy_pending_action_requires_binding(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.start(target)
            _, order, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
            agents = target / "AGENTS.md"
            original = agents.read_text(encoding="utf-8")
            agents.write_text(original + "\nstatic drift\n", encoding="utf-8")

            with (
                mock.patch.object(
                    CONTROLLER,
                    "fresh_workspace_status",
                    side_effect=AssertionError("workspace status must not run after trusted-input drift"),
                ) as status_mock,
                mock.patch.object(
                    CONTROLLER,
                    "load_config",
                    side_effect=AssertionError("workspace config must not be read after trusted-input drift"),
                ) as config_mock,
            ):
                code, error, _ = self.submit(root, target, order["action_id"])
            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("ORCHESTRATION_TRUSTED_INPUT_CHANGED", error["error_code"])
            self.assertTrue(any(path.startswith("AGENTS.md ") for path in error["details"]["changed_paths"]))
            self.assertFalse(CONTROLLER.work_result_path(target, "orch-test", order["action_id"]).exists())
            status_mock.assert_not_called()
            config_mock.assert_not_called()

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.start(target)
            _, order, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
            session = CONTROLLER.load_session(target, "orch-test")
            session.pop("pending_trusted_static_inputs")
            CONTROLLER.write_json_atomic(CONTROLLER.session_path(target, "orch-test"), session)
            CONTROLLER.trusted_static_input_path(target, "orch-test", order["action_id"]).unlink()

            code, error, _ = self.submit(root, target, order["action_id"])
            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("ORCHESTRATION_LEGACY_ACTION_UNBOUND", error["error_code"])

            code, replayed, stderr = self.controller(
                target,
                "next",
                "--orchestration-id",
                "orch-test",
                "--resume",
            )
            self.assertEqual(0, code, stderr)
            self.assertEqual(order["action_id"], replayed["action_id"])
            migrated = CONTROLLER.load_session(target, "orch-test")
            self.assertTrue(CONTROLLER.valid_pending_trusted_static_inputs(migrated["pending_trusted_static_inputs"]))
            fingerprint_path = CONTROLLER.trusted_static_input_path(target, "orch-test", order["action_id"])
            self.assertTrue(fingerprint_path.is_file())

            agents = target / "AGENTS.md"
            agents.write_text(agents.read_text(encoding="utf-8") + "\npost-binding drift\n", encoding="utf-8")
            code, error, _ = self.submit(root, target, order["action_id"])
            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("ORCHESTRATION_TRUSTED_INPUT_CHANGED", error["error_code"])

    def test_legacy_trusted_input_binding_recovers_after_snapshot_precedes_session_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.start(target)
            _, order, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
            session = CONTROLLER.load_session(target, "orch-test")
            session.pop("pending_trusted_static_inputs")
            CONTROLLER.write_json_atomic(CONTROLLER.session_path(target, "orch-test"), session)
            fingerprint_path = CONTROLLER.trusted_static_input_path(target, "orch-test", order["action_id"])
            fingerprint_path.unlink()
            real_write = CONTROLLER.write_json_atomic
            crashed = False

            def crash_before_session_binding(path: Path, document: dict) -> None:
                nonlocal crashed
                if (
                    not crashed
                    and path == CONTROLLER.session_path(target.resolve(), "orch-test")
                    and "pending_trusted_static_inputs" in document
                ):
                    crashed = True
                    raise CONTROLLER.OrchestrationControllerError(
                        "INJECTED_CRASH",
                        "injected crash after legacy fingerprint persistence",
                    )
                real_write(path, document)

            with mock.patch.object(CONTROLLER, "write_json_atomic", side_effect=crash_before_session_binding):
                code, error, _ = self.controller(
                    target,
                    "next",
                    "--orchestration-id",
                    "orch-test",
                    "--resume",
                )
            self.assertTrue(crashed, "fault injection did not reach the session write")
            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("INJECTED_CRASH", error["error_code"])
            self.assertTrue(fingerprint_path.is_file())
            retained_fingerprint = fingerprint_path.read_bytes()
            self.assertNotIn("pending_trusted_static_inputs", CONTROLLER.load_session(target, "orch-test"))

            code, replayed, stderr = self.controller(
                target,
                "next",
                "--orchestration-id",
                "orch-test",
                "--resume",
            )

            self.assertEqual(0, code, stderr)
            self.assertEqual(order["action_id"], replayed["action_id"])
            self.assertEqual(retained_fingerprint, fingerprint_path.read_bytes())
            rebound = CONTROLLER.load_session(target, "orch-test")
            self.assertTrue(CONTROLLER.valid_pending_trusted_static_inputs(rebound["pending_trusted_static_inputs"]))

    def test_legacy_replay_binds_trusted_inputs_before_workspace_status_executes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.start(target)
            _, order, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
            session = CONTROLLER.load_session(target, "orch-test")
            session.pop("pending_trusted_static_inputs")
            CONTROLLER.write_json_atomic(CONTROLLER.session_path(target, "orch-test"), session)
            CONTROLLER.trusted_static_input_path(target, "orch-test", order["action_id"]).unlink()
            real_status = CONTROLLER.fresh_workspace_status

            def status_after_binding(project_root: Path) -> dict:
                retained = CONTROLLER.load_session(project_root, "orch-test")
                self.assertTrue(
                    CONTROLLER.valid_pending_trusted_static_inputs(
                        retained.get("pending_trusted_static_inputs")
                    )
                )
                return real_status(project_root)

            with mock.patch.object(
                CONTROLLER,
                "fresh_workspace_status",
                side_effect=status_after_binding,
            ):
                code, replayed, stderr = self.controller(
                    target,
                    "next",
                    "--orchestration-id",
                    "orch-test",
                    "--resume",
                )

            self.assertEqual(0, code, stderr)
            self.assertEqual(order["action_id"], replayed["action_id"])

    def test_materialized_effects_without_result_replay_same_action_then_submit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.start(target)
            _, order, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
            materialized = target / "runs" / "run-reports" / "interrupted-answer.md"
            materialized.parent.mkdir(parents=True, exist_ok=True)
            materialized.write_text("# Materialized answer from the interrupted attempt\n", encoding="utf-8")
            self.block_question(target)

            order_path = CONTROLLER.work_order_path(target, "orch-test", order["action_id"])
            expired = json.loads(order_path.read_text(encoding="utf-8"))
            expired["lease"]["expires_at"] = (
                datetime.now(timezone.utc) - timedelta(seconds=1)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            CONTROLLER.write_json_atomic(order_path, expired)

            code, replayed, stderr = self.controller(
                target,
                "next",
                "--orchestration-id",
                "orch-test",
                "--resume",
            )
            self.assertEqual(0, code, stderr)
            self.assertEqual(order["action_id"], replayed["action_id"])
            self.assertEqual(2, replayed["lease"]["attempt"])
            pending = CONTROLLER.load_session(target, "orch-test")
            self.assertEqual(order["action_id"], pending["pending_action_id"])
            self.assertEqual(0, pending["completed_action_count"])
            self.assertEqual(CONTROLLER.RECOVERY_RECONCILE, pending["recovery"]["state"])
            self.assertFalse(CONTROLLER.work_result_path(target, "orch-test", order["action_id"]).exists())
            self.assertEqual("answering", RUN_CONTROLLER.load_run_state(target, order["run_id"])["state"]["current"])

            recovered_status = {
                "workspace_health": {"materially_valid": True},
                "readiness": {"verdict": "complete", "reasons": []},
            }
            with mock.patch.object(CONTROLLER, "fresh_workspace_status", return_value=recovered_status):
                code, accepted, stderr = self.submit(
                    root,
                    target,
                    order["action_id"],
                    summary="Reconciled the already materialized answer after interruption.",
                    artifacts=["runs/run-reports/interrupted-answer.md"],
                )
            self.assertEqual(0, code, stderr)
            self.assertEqual("active", accepted["status"])
            self.assertEqual("verification", accepted["phase"])
            self.assertEqual(1, accepted["completed_action_count"])
            self.assertEqual(CONTROLLER.RECOVERY_NONE, accepted["recovery"]["state"])
            self.assertEqual("verifying", RUN_CONTROLLER.load_run_state(target, order["run_id"])["state"]["current"])

    def test_action_limit_pauses_and_resume_starts_a_fresh_window(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.add_questions(
                root,
                target,
                [
                    {
                        "id": "second-question",
                        "question": "Which evidence answers the second test question?",
                        "priority": "medium",
                    }
                ],
            )
            self.start(target, max_actions=1)
            _, work_order, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
            self.block_question(target)
            code, accepted, stderr = self.submit(root, target, work_order["action_id"])
            self.assertEqual(0, code, stderr)
            self.assertEqual("active", accepted["status"])

            code, paused, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
            self.assertEqual(CONTROLLER.EXIT_PAUSED, code)
            self.assertEqual("paused", paused["status"])
            self.assertIn("max_actions", paused["pause_reason"])

            code, resumed, stderr = self.controller(
                target,
                "next",
                "--orchestration-id",
                "orch-test",
                "--agent-id",
                "agent-test",
                "--resume",
            )
            self.assertEqual(0, code, stderr)
            self.assertEqual("action-0002", resumed["action_id"])
            self.assertEqual("research", resumed["phase"])

    def test_conflicting_result_is_rejected_after_idempotent_submit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.start(target)
            _, order, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
            self.block_question(target)
            first = self.submit(root, target, order["action_id"], summary="First accepted result.")
            self.assertEqual(0, first[0], first[2])
            duplicate = self.submit(root, target, order["action_id"], summary="First accepted result.")
            self.assertEqual(0, duplicate[0], duplicate[2])

            code, error, _ = self.submit(root, target, order["action_id"], summary="Conflicting result.")
            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("RESULT_CONFLICT", error["error_code"])

    def test_absolute_result_artifact_is_rejected_without_persistence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.start(target)
            _, order, _ = self.controller(target, "next", "--orchestration-id", "orch-test")

            code, error, _ = self.submit(
                root,
                target,
                order["action_id"],
                artifacts=["/tmp/not-a-workspace-artifact"],
            )
            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("RESULT_INVALID", error["error_code"])
            retained = target / "runs" / "orchestrations" / "orch-test" / "work-results" / "action-0001.json"
            self.assertFalse(retained.exists())

    def test_controller_owned_parent_artifact_is_rejected_without_persistence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            self.start(target)
            _, order, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
            controller_owned = "runs/orchestrations/orch-test/answers.json"
            (target / controller_owned).write_text("{}\n", encoding="utf-8")

            code, error, _ = self.submit(
                root,
                target,
                order["action_id"],
                artifacts=[controller_owned],
            )

            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("RESULT_INVALID", error["error_code"])
            self.assertFalse(CONTROLLER.work_result_path(target, "orch-test", order["action_id"]).exists())

    def test_failed_postcondition_retains_no_result_and_identical_retry_succeeds(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            self.start(target)
            _, order, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
            self.build_verification_bundle(target, order["run_id"])
            publication_path = target / "runs" / order["run_id"] / "evaluation" / "publication-readiness.json"
            publication = json.loads(publication_path.read_text(encoding="utf-8"))
            publication["verdict"] = "no_ship"
            publication_path.write_text(json.dumps(publication), encoding="utf-8")
            code, error, _ = self.submit(root, target, order["action_id"])
            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("ORCHESTRATION_POSTCONDITION_FAILED", error["error_code"])
            retained = target / "runs" / "orchestrations" / "orch-test" / "work-results" / "action-0001.json"
            self.assertFalse(retained.exists())

            self.build_verification_bundle(target, order["run_id"])
            code, completed, stderr = self.submit(root, target, order["action_id"])
            self.assertEqual(0, code, stderr)
            self.assertEqual("complete", completed["status"])
            self.assertTrue(retained.is_file())

    def test_blocked_result_preserves_immutable_terminal_child_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.start(target)
            _, order, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
            code, session, _ = self.submit(
                root,
                target,
                order["action_id"],
                outcome="blocked",
                summary="No permitted evidence route remains.",
            )
            self.assertEqual(CONTROLLER.EXIT_BLOCKED, code)
            self.assertEqual("blocked_on_sources", session["status"])
            child = RUN_CONTROLLER.load_run_state(target, order["run_id"])
            self.assertEqual("blocked_on_sources", child["state"]["current"])

            duplicate = self.submit(
                root,
                target,
                order["action_id"],
                outcome="blocked",
                summary="No permitted evidence route remains.",
            )
            self.assertEqual(CONTROLLER.EXIT_BLOCKED, duplicate[0])
            self.assertEqual("blocked_on_sources", duplicate[1]["status"])

            code, _, stderr = self.run_module(
                RUN_CONTROLLER,
                [
                    "--project-root",
                    str(target),
                    "transition",
                    "--run-id",
                    order["run_id"],
                    "--agent-id",
                    "agent-test",
                    "--to-state",
                    "verifying",
                    "--format",
                    "json",
                ],
            )
            self.assertEqual(RUN_CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("RUN_TERMINAL", json.loads(stderr)["error_code"])

    def test_failed_result_is_terminal_and_resume_does_not_reopen(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.start(target)
            _, order, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
            summary = "The work order could not execute its required Python tooling."

            code, failed, stderr = self.submit(
                root,
                target,
                order["action_id"],
                outcome="failed",
                summary=summary,
            )

            self.assertEqual(CONTROLLER.EXIT_INVALID, code, stderr)
            self.assertEqual("failed", failed["status"])
            self.assertIsNone(failed["pending_action_id"])
            self.assertIsNone(failed["pending_submission"])
            self.assertEqual(
                [
                    {
                        "recorded_at": failed["failure_records"][0]["recorded_at"],
                        "action_id": order["action_id"],
                        "summary": summary,
                    }
                ],
                failed["failure_records"],
            )
            work_orders_dir = CONTROLLER.work_order_path(target, "orch-test", order["action_id"]).parent
            work_orders = sorted(work_orders_dir.iterdir())

            code, resumed, stderr = self.controller(
                target,
                "next",
                "--orchestration-id",
                "orch-test",
                "--agent-id",
                "agent-test",
                "--resume",
            )

            self.assertEqual(CONTROLLER.EXIT_INVALID, code, stderr)
            self.assertEqual(failed, resumed)
            self.assertEqual(work_orders, sorted(work_orders_dir.iterdir()))
            self.assertEqual(1, resumed["action_count"])
            self.assertEqual(1, resumed["completed_action_count"])

    def test_retained_result_without_session_completion_proof_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.start(target)
            _, order, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
            result_path = root / "forged-retained.json"
            result = {
                "schema_version": "1.0",
                "action_id": order["action_id"],
                "outcome": "completed",
                "summary": "Forged retained result must not prove completion.",
                "artifacts": [],
            }
            result_path.write_text(json.dumps(result), encoding="utf-8")
            CONTROLLER.write_json_atomic(
                CONTROLLER.work_result_path(target, "orch-test", order["action_id"]),
                result,
            )
            session = CONTROLLER.load_session(target, "orch-test")
            session["pending_action_id"] = None
            CONTROLLER.write_json_atomic(CONTROLLER.session_path(target, "orch-test"), session)

            code, error, _ = self.controller(
                target,
                "submit",
                "--orchestration-id",
                "orch-test",
                "--action-id",
                order["action_id"],
                "--result-file",
                str(result_path),
            )
            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("ORCHESTRATION_STATE_INVALID", error["error_code"])

    def test_pending_submission_rejects_tampered_result_even_with_matching_digest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir), question=True)
            self.start(target)
            _, order, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
            session = CONTROLLER.load_session(target, "orch-test")
            result = {
                "schema_version": "1.0",
                "action_id": order["action_id"],
                "outcome": "completed",
                "summary": "Tampered accepted result.",
                "artifacts": [],
                "unsupported": True,
            }
            session["pending_submission"] = {
                "action_id": order["action_id"],
                "accepted_at": "2026-07-21T10:00:00Z",
                "result": result,
                "result_digest": CONTROLLER.result_digest(result),
                "next_phase": "verification",
                "completion_reason": None,
            }
            session["recovery"] = {
                "state": CONTROLLER.RECOVERY_FINALIZING,
                "action_id": order["action_id"],
                "attempt": 1,
                "reason_code": "accepted_result_pending_finalization",
                "recorded_at": "2026-07-21T10:00:00Z",
            }
            CONTROLLER.write_json_atomic(CONTROLLER.session_path(target, "orch-test"), session)

            with self.assertRaisesRegex(
                CONTROLLER.OrchestrationControllerError,
                "invalid orchestration session shape",
            ):
                CONTROLLER.load_session(target, "orch-test")

    def test_fresh_ship_verification_writes_answer_export_and_completes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            self.start(target)
            _, order, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
            self.assertEqual("verification", order["phase"])
            checks = {item["check"] for item in order["required_postconditions"]}
            self.assertNotIn("answer_export_written", checks)
            self.assertFalse(
                any(
                    path.startswith("runs/orchestrations/")
                    for item in order["required_postconditions"]
                    for path in item.get("paths", [])
                )
            )
            self.build_verification_bundle(target, order["run_id"])

            code, completed, stderr = self.submit(root, target, order["action_id"])
            self.assertEqual(0, code, stderr)
            self.assertEqual("complete", completed["status"])
            answers = target / "runs" / "orchestrations" / "orch-test" / "answers.json"
            self.assertTrue(answers.is_file())
            self.assertEqual(0, json.loads(answers.read_text(encoding="utf-8"))["counts"]["total"])
            child = RUN_CONTROLLER.load_run_state(target, order["run_id"])
            self.assertEqual("complete", child["state"]["current"])

            status = STATUS.build_status_document(target)
            self.assertEqual("orch-test", status["orchestration"]["orchestration_id"])
            self.assertTrue(status["orchestration"]["terminal"])

    def test_verification_preflight_is_read_only_and_controller_writes_derived_outputs_only_on_apply(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            self.start(target)
            _, order, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
            self.build_verification_bundle(target, order["run_id"])
            session = CONTROLLER.load_session(target, "orch-test")
            evaluation = target / "runs" / order["run_id"] / "evaluation"

            with mock.patch.object(CONTROLLER, "write_json_atomic") as write:
                next_phase, _ = CONTROLLER.verify_action_postconditions(
                    target,
                    session,
                    order,
                    apply_effects=False,
                )

            self.assertEqual("complete", next_phase)
            write.assert_not_called()
            self.assertFalse((evaluation / "quote-verification.json").exists())
            self.assertFalse((evaluation / "coverage-summary.json").exists())
            self.assertFalse(CONTROLLER.answers_path(target, "orch-test").exists())

    def test_invalid_workspace_health_refuses_before_session_creation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            (target / "research.yml").write_text("project: [unterminated\n", encoding="utf-8")
            code, error, _ = self.controller(
                target,
                "start",
                "--orchestration-id",
                "orch-test",
                "--agent-id",
                "agent-test",
            )
            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertIn(error["error_code"], {"WORKSPACE_UNREADABLE", "CONFIG_INVALID"})
            self.assertFalse((target / "runs" / "orchestrations" / "orch-test" / "session.json").exists())

    def test_lock_refusal_uses_stable_machine_error_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            unavailable = CONTROLLER.LockUnavailableError("orchestration writer is active")
            with mock.patch.object(CONTROLLER, "workspace_lock", side_effect=unavailable):
                code, error, _ = self.controller(
                    target,
                    "start",
                    "--orchestration-id",
                    "orch-test",
                    "--agent-id",
                    "agent-test",
                )
            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("LOCK_UNAVAILABLE", error["error_code"])
            self.assertFalse((target / "runs" / "orchestrations" / "orch-test" / "session.json").exists())

    def test_provider_policy_never_treats_legacy_strategies_as_network_authority(self):
        policy = CONTROLLER.provider_policy(
            {
                "integrations": {
                    "discovery": {"enabled": True, "providers": ["legal", "arxiv", "authors"]},
                    "acquisition": {"enabled": True, "providers": ["openalex"]},
                }
            }
        )
        self.assertEqual({"enabled": True, "providers": ["arxiv"]}, policy["discovery"])
        self.assertEqual({"enabled": True, "providers": ["openalex"]}, policy["acquisition"])
        alias_only = CONTROLLER.provider_policy(
            {
                "integrations": {
                    "discovery": {"enabled": True, "providers": ["legal", "companions"]},
                }
            }
        )
        self.assertEqual({"enabled": False, "providers": []}, alias_only["discovery"])

    def test_research_scope_uses_configured_max_questions_per_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.add_questions(
                root,
                target,
                [
                    {"id": "question-two", "question": "Second bounded question?", "priority": "high"},
                    {"id": "question-three", "question": "Third bounded question?", "priority": "low"},
                ],
            )
            config_path = target / "research.yml"
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config.setdefault("run", {})["max_questions_per_run"] = 2
            config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
            self.start(target)

            code, order, stderr = self.controller(target, "next", "--orchestration-id", "orch-test")

        self.assertEqual(0, code, stderr)
        self.assertEqual("research", order["phase"])
        self.assertEqual(2, order["budgets"]["max_questions_per_run"])
        self.assertEqual(["question-two", "test-question"], order["scope"]["question_slugs"])

    def test_research_scope_uses_active_child_remaining_budget_and_rolls_over_at_zero(self):
        base_status = {
            "workspace_health": {"materially_valid": True},
            "readiness": {
                "verdict": "in_progress",
                "budget_state": {"questions_remaining_this_run": 1},
            },
            "questions": {"actionable_slugs": ["question-one", "question-two", "question-three"]},
            "run": {"max_questions_per_run": 3},
            "run_controller": {"run_id": "run-active", "state": "answering", "terminal": False},
        }
        session = {"active_run_id": "run-active"}
        with (
            mock.patch.object(CONTROLLER, "load_config", return_value={}),
            mock.patch.object(CONTROLLER, "fresh_workspace_status", return_value=base_status),
        ):
            route, context = CONTROLLER.choose_route(Path("/unused"), session)

        self.assertEqual("research", route)
        self.assertEqual(["question-one"], context["scope"]["question_slugs"])

        exhausted = json.loads(json.dumps(base_status))
        exhausted["readiness"]["budget_state"]["questions_remaining_this_run"] = 0
        rollover_session = {"active_run_id": "run-active"}

        def close_active(_project_root: Path, retained_session: dict, verdict: str) -> None:
            self.assertEqual("no_ship", verdict)
            retained_session["active_run_id"] = None

        with (
            mock.patch.object(CONTROLLER, "load_config", return_value={}),
            mock.patch.object(CONTROLLER, "fresh_workspace_status", return_value=exhausted),
            mock.patch.object(CONTROLLER, "finish_active_child", side_effect=close_active) as finish_mock,
            mock.patch.object(CONTROLLER, "record_event") as event_mock,
        ):
            route, context = CONTROLLER.choose_route(Path("/unused"), rollover_session)

        self.assertEqual("research", route)
        self.assertEqual(
            ["question-one", "question-two", "question-three"],
            context["scope"]["question_slugs"],
        )
        self.assertIsNone(rollover_session["active_run_id"])
        finish_mock.assert_called_once()
        event_mock.assert_called_once()

    def test_source_request_budget_exhaustion_rolls_unanswered_questions_to_fresh_child(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.add_questions(
                root,
                target,
                [
                    {"id": "question-two", "question": "Second evidence gap?", "priority": "high"},
                    {"id": "question-three", "question": "Third unanswered question?", "priority": "high"},
                ],
            )
            config_path = target / "research.yml"
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config.setdefault("run", {})["max_source_requests_per_run"] = 2
            config["run"]["max_questions_per_run"] = 3
            config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
            self.start(target)
            _, first_order, _ = self.controller(target, "next", "--orchestration-id", "orch-test")

            for slug in ("test-question", "question-two"):
                request = self.assert_json_script_ok(
                    SOURCE_REQUESTS,
                    [
                        "--project-root",
                        str(target),
                        "add",
                        "--kind",
                        "paper",
                        "--query",
                        f"evidence for {slug}",
                        "--rationale",
                        f"The scoped question {slug} needs primary evidence.",
                        "--question-slug",
                        slug,
                        "--format",
                        "json",
                    ],
                )["request"]
                self.assert_json_script_ok(
                    CLAIM,
                    [
                        "--project-root",
                        str(target),
                        "claim",
                        "--slug",
                        slug,
                        "--agent-id",
                        "agent-test",
                        "--format",
                        "json",
                    ],
                )
                self.assert_json_script_ok(
                    RESOLVE,
                    [
                        "--project-root",
                        str(target),
                        "block",
                        "--slug",
                        slug,
                        "--agent-id",
                        "agent-test",
                        "--blocked-reason",
                        "The run reached its bounded source-request workflow.",
                        "--request-id",
                        request["request_id"],
                        "--format",
                        "json",
                    ],
                )

            code, _, stderr = self.submit(
                root,
                target,
                first_order["action_id"],
                summary="Opened the two permitted source requests while leaving one question actionable.",
            )
            self.assertEqual(0, code, stderr)
            first_child_path = target / "runs" / first_order["run_id"] / "run-state.json"
            self.assertEqual("answering", json.loads(first_child_path.read_text())["state"]["current"])

            code, second_order, stderr = self.controller(
                target,
                "next",
                "--orchestration-id",
                "orch-test",
            )
            self.assertEqual(0, code, stderr)
            self.assertEqual("research", second_order["phase"])
            self.assertNotEqual(first_order["run_id"], second_order["run_id"])
            self.assertEqual(["question-three"], second_order["scope"]["question_slugs"])
            self.assertEqual("no_ship", json.loads(first_child_path.read_text())["state"]["current"])

    def test_legacy_research_without_question_baseline_requires_fresh_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.start(target)
            _, order, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
            order_path = CONTROLLER.work_order_path(target, "orch-test", order["action_id"])
            retained_order = json.loads(order_path.read_text(encoding="utf-8"))
            retained_order["required_postconditions"] = [
                item
                for item in retained_order["required_postconditions"]
                if item["check"] != "controller_integrity_baseline"
            ]
            CONTROLLER.write_json_atomic(order_path, retained_order)

            code, error, _ = self.controller(
                target,
                "next",
                "--orchestration-id",
                "orch-test",
                "--resume",
            )
            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("ORCHESTRATION_RESEARCH_BASELINE_UNAVAILABLE", error["error_code"])
            self.assertIn("fresh orchestration session", error["remediation"])

            code, error, _ = self.submit(root, target, order["action_id"])

        self.assertEqual(CONTROLLER.EXIT_INVALID, code)
        self.assertEqual("ORCHESTRATION_RESEARCH_BASELINE_UNAVAILABLE", error["error_code"])
        self.assertFalse(CONTROLLER.work_result_path(target, "orch-test", order["action_id"]).exists())

    def test_selected_candidates_are_bounded_by_work_order_candidate_scope(self):
        candidates = [
            {
                "candidate_id": "cand-authorized",
                "source_request_id": "req-test",
                "provider": "arxiv",
                "lifecycle_state": "selected",
            },
            {
                "candidate_id": "cand-out-of-scope",
                "source_request_id": "req-test",
                "provider": "arxiv",
                "lifecycle_state": "selected",
            },
            {
                "candidate_id": "cand-other-request",
                "source_request_id": "req-other",
                "provider": "arxiv",
                "lifecycle_state": "selected",
            },
        ]
        with mock.patch.object(CONTROLLER, "load_candidates", return_value=candidates):
            selected = CONTROLLER.selected_candidates_for_scope(
                Path("/unused"),
                {},
                ["req-test"],
                ["cand-authorized"],
            )
            outside = CONTROLLER.selected_candidates_outside_scope(
                Path("/unused"),
                {},
                ["req-test"],
                ["cand-authorized"],
            )

        self.assertEqual(["cand-authorized"], [item["candidate_id"] for item in selected])
        self.assertEqual(["cand-out-of-scope"], [item["candidate_id"] for item in outside])

    def test_candidate_review_rejects_only_new_out_of_scope_selections(self):
        request_id = "req-review"
        historical = {
            "candidate_id": "cand-historical-unroutable",
            "source_request_id": request_id,
            "provider": "github",
            "lifecycle_state": "selected",
        }
        authorized = {
            "candidate_id": "cand-authorized",
            "source_request_id": request_id,
            "provider": "arxiv",
            "source_type": "paper",
            "paper": {"provider_ids": {"arxiv": "2601.12345v2"}},
            "lifecycle_state": "selected",
        }
        authorized_before = {**authorized, "lifecycle_state": "proposed"}
        injected = {
            "candidate_id": "cand-injected",
            "source_request_id": request_id,
            "provider": "arxiv",
            "source_type": "paper",
            "paper": {"provider_ids": {"arxiv": "2601.54321v1"}},
            "lifecycle_state": "selected",
        }
        raw_baseline = {
            "algorithm": "sha256-content-v1",
            "file_count": 0,
            "total_bytes": 0,
            "fingerprint": "sha256:" + "0" * 64,
        }
        work_order = {
            "phase": "candidate_review",
            "run_id": "run-review",
            "scope": {
                "question_slugs": [],
                "request_ids": [request_id],
                "candidate_ids": [authorized["candidate_id"]],
            },
            "required_postconditions": [
                {
                    "check": "selected_candidate_for_request",
                    "selected_before": 1,
                    "selected_candidate_ids_before": [historical["candidate_id"]],
                    "candidate_record_fingerprints_before": CONTROLLER.candidate_record_fingerprint_snapshot(
                        [historical, authorized_before]
                    ),
                },
                {
                    "check": "selection_does_not_fetch",
                    "manifest_records_before": 0,
                    "manifest_digest_before": None,
                },
                {"check": "raw_tree_unchanged", "before": raw_baseline},
            ],
        }
        status = {"readiness": {"verdict": "blocked_on_sources"}, "sources": {"manifest_records": 0}}
        config = {
            "integrations": {
                "discovery": {"enabled": True, "providers": ["arxiv"]},
                "acquisition": {"enabled": True, "providers": ["arxiv"]},
            }
        }
        run_controller = mock.Mock()
        run_controller.load_run_state.return_value = {"state": {"current": "candidates_ready"}}

        def verify(candidates: list[dict]) -> tuple[str | None, str | None]:
            def sibling(stem: str):
                return run_controller if stem == "run_controller" else SOURCE_REQUESTS

            with (
                mock.patch.object(CONTROLLER, "load_config", return_value=config),
                mock.patch.object(CONTROLLER, "fresh_workspace_status", return_value=status),
                mock.patch.object(CONTROLLER, "load_candidates", return_value=candidates),
                mock.patch.object(CONTROLLER, "raw_tree_snapshot", return_value=raw_baseline),
                mock.patch.object(CONTROLLER, "evidence_manifest_digest", return_value=None),
                mock.patch.object(CONTROLLER, "load_sibling_module", side_effect=sibling),
            ):
                return CONTROLLER.verify_action_postconditions(
                    Path("/unused"),
                    {"agent_id": "test-agent"},
                    work_order,
                )

        self.assertEqual(("acquisition", None), verify([historical, authorized]))
        with self.assertRaisesRegex(
            CONTROLLER.OrchestrationControllerError,
            "outside the persisted candidate scope",
        ):
            verify([historical, authorized, injected])

    def test_completed_research_requires_terminal_scoped_progress_and_accepts_linked_source_block(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.start(target)
            _, order, _ = self.controller(target, "next", "--orchestration-id", "orch-test")

            code, error, _ = self.submit(
                root,
                target,
                order["action_id"],
                summary="The worker returned without processing its scoped question.",
            )
            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("ORCHESTRATION_POSTCONDITION_FAILED", error["error_code"])
            self.assertIn("without terminally processing", error["message"])
            self.assertFalse(CONTROLLER.work_result_path(target, "orch-test", order["action_id"]).exists())

            request_id = self.block_question(target)
            code, accepted, stderr = self.submit(
                root,
                target,
                order["action_id"],
                summary="Created a scoped source request and durably blocked the question on it.",
                artifacts=["sources/source-requests.jsonl", "wiki/questions/test-question.md"],
            )
            retained_request = json.loads(
                (target / "sources" / "source-requests.jsonl").read_text(encoding="utf-8")
            )

        self.assertEqual(0, code, stderr)
        self.assertEqual("active", accepted["status"])
        self.assertEqual("planning", accepted["phase"])
        self.assertEqual(request_id, retained_request["request_id"])

    def test_research_validates_blocked_links_even_when_other_questions_keep_readiness_in_progress(self):
        work_order = {
            "phase": "research",
            "run_id": "run-mixed-research",
            "scope": {
                "question_slugs": ["blocked-question", "open-question"],
                "request_ids": [],
                "candidate_ids": [],
            },
            "required_postconditions": [
                {
                    "check": "workspace_readiness_changed",
                    "allowed_verdicts": ["in_progress", "blocked_on_sources", "complete"],
                    "scoped_questions_before": {
                        "blocked-question": {
                            "status": "open",
                            "blocking_request_ids": [],
                            "answer_page": "",
                        },
                        "open-question": {
                            "status": "open",
                            "blocking_request_ids": [],
                            "answer_page": "",
                        },
                    },
                    "question_file_fingerprints_before": {
                        "blocked-question.md": "sha256:" + "1" * 64,
                        "open-question.md": "sha256:" + "2" * 64,
                    },
                    "source_request_record_fingerprints_before": {},
                },
                {"check": "child_run_state", "expected": "answering"},
            ],
        }
        after_questions = {
            "blocked-question": {
                "status": "blocked",
                "blocking_request_ids": ["req-orphaned"],
                "answer_page": "",
            },
            "open-question": {
                "status": "open",
                "blocking_request_ids": [],
                "answer_page": "",
            },
        }
        run_controller = mock.Mock()
        run_controller.load_run_state.return_value = {"state": {"current": "answering"}}
        source_requests = mock.Mock()
        source_requests.requests_path.return_value = Path("/unused/source-requests.jsonl")
        source_requests.load_requests.return_value = []

        def sibling(stem: str):
            return {"run_controller": run_controller, "source_requests": source_requests}[stem]

        with (
            mock.patch.object(CONTROLLER, "load_config", return_value={}),
            mock.patch.object(
                CONTROLLER,
                "fresh_workspace_status",
                return_value={"readiness": {"verdict": "in_progress"}},
            ),
            mock.patch.object(CONTROLLER, "scoped_question_snapshot", return_value=after_questions),
            mock.patch.object(
                CONTROLLER,
                "question_file_fingerprint_snapshot",
                return_value={
                    "blocked-question.md": "sha256:" + "3" * 64,
                    "open-question.md": "sha256:" + "2" * 64,
                },
            ),
            mock.patch.object(CONTROLLER, "load_sibling_module", side_effect=sibling),
            self.assertRaisesRegex(
                CONTROLLER.OrchestrationControllerError,
                "blocked research questions lack open request artifacts",
            ),
        ):
            CONTROLLER.verify_action_postconditions(
                Path("/unused"),
                {"agent_id": "agent-test"},
                work_order,
            )

    def test_blocked_provider_scopes_are_bounded_before_work_order_issuance(self):
        config = {
            "integrations": {
                "discovery": {"enabled": True, "providers": ["openalex"]},
                "acquisition": {"enabled": True, "providers": ["openalex"]},
            }
        }
        request = {
            "request_id": "req-bounded",
            "status": "open",
            "priority": "high",
            "question_slugs": ["test-question"],
        }
        status = {
            "workspace_health": {"materially_valid": True},
            "readiness": {"verdict": "blocked_on_sources"},
        }
        reviewable = [
            {
                "candidate_id": f"cand-{index:03d}",
                "source_request_id": request["request_id"],
                "provider": "openalex",
                "source_type": "paper",
                "paper": {"provider_ids": {"openalex": f"W{index + 1}"}},
                "lifecycle_state": "proposed",
            }
            for index in range(CONTROLLER.MAX_SCOPE_IDS + 44)
        ]

        def choose(candidates: list[dict]) -> tuple[str | None, dict]:
            with (
                mock.patch.object(CONTROLLER, "load_config", return_value=config),
                mock.patch.object(CONTROLLER, "fresh_workspace_status", return_value=status),
                mock.patch.object(CONTROLLER, "open_requests", return_value=[request]),
                mock.patch.object(CONTROLLER, "load_candidates", return_value=candidates),
            ):
                return CONTROLLER.choose_route(Path("/unused"), {})

        route, context = choose(reviewable)
        self.assertEqual("candidate_review", route)
        self.assertEqual(CONTROLLER.MAX_SCOPE_IDS, len(context["scope"]["candidate_ids"]))
        self.assertEqual("cand-000", context["scope"]["candidate_ids"][0])
        self.assertEqual("cand-255", context["scope"]["candidate_ids"][-1])

        selected = [
            {**candidate, "lifecycle_state": "selected"}
            for candidate in reviewable[:2]
        ]
        route, context = choose(selected)
        self.assertEqual("acquisition", route)
        self.assertEqual(["cand-000"], context["scope"]["candidate_ids"])

    def test_exhausted_or_unroutable_existing_candidates_trigger_rediscovery(self):
        config = {
            "integrations": {
                "discovery": {"enabled": True, "providers": ["arxiv"]},
                "acquisition": {"enabled": True, "providers": ["openalex"]},
            }
        }
        request = {
            "request_id": "req-retry-route",
            "status": "open",
            "priority": "high",
            "question_slugs": ["test-question"],
        }
        candidates = [
            {
                "candidate_id": "cand-rejected",
                "source_request_id": request["request_id"],
                "provider": "openalex",
                "lifecycle_state": "rejected",
            },
            {
                "candidate_id": "cand-failed",
                "source_request_id": request["request_id"],
                "provider": "openalex",
                "lifecycle_state": "failed",
            },
            {
                "candidate_id": "cand-superseded",
                "source_request_id": request["request_id"],
                "provider": "openalex",
                "lifecycle_state": "superseded",
            },
            {
                "candidate_id": "cand-unroutable",
                "source_request_id": request["request_id"],
                "provider": "github",
                "lifecycle_state": "proposed",
            },
        ]
        status = {
            "workspace_health": {"materially_valid": True},
            "readiness": {"verdict": "blocked_on_sources"},
        }
        session: dict = {}
        with (
            mock.patch.object(CONTROLLER, "load_config", return_value=config),
            mock.patch.object(CONTROLLER, "fresh_workspace_status", return_value=status),
            mock.patch.object(CONTROLLER, "open_requests", return_value=[request]),
            mock.patch.object(CONTROLLER, "load_candidates", return_value=candidates),
        ):
            route, context = CONTROLLER.choose_route(Path("/unused"), session)

        self.assertEqual("discovery", route)
        self.assertEqual(4, context["candidate_count_before"])
        self.assertEqual(["arxiv"], context["discovery_providers"])
        self.assertEqual([request["request_id"]], context["scope"]["request_ids"])

    def test_discovery_completion_requires_new_reviewable_end_to_end_candidate(self):
        request_id = "req-retry-route"
        historical = {
            "candidate_id": "cand-historical-rejected",
            "source_request_id": request_id,
            "provider": "openalex",
            "source_type": "paper",
            "lifecycle_state": "rejected",
            "paper": {"provider_ids": {"openalex": "W12345"}},
        }
        baseline = CONTROLLER.request_candidate_state_snapshot([historical], [request_id])
        new_unroutable = {
            "candidate_id": "cand-new-unroutable",
            "source_request_id": request_id,
            "provider": "arxiv",
            "discovery_providers": ["arxiv"],
            "source_type": "paper",
            "lifecycle_state": "proposed",
            "paper": {"provider_ids": {}},
        }

        eligible = CONTROLLER.eligible_new_discovery_candidates(
            [historical, new_unroutable],
            [request_id],
            baseline,
            {"arxiv"},
            {"openalex"},
        )

        self.assertEqual([], eligible, "historical rejected routes must not make a new unroutable result pass")

        new_routable = {
            **new_unroutable,
            "candidate_id": "cand-new-routable",
            "paper": {"provider_ids": {"doi": "10.5555/routable"}},
        }
        eligible = CONTROLLER.eligible_new_discovery_candidates(
            [historical, new_routable],
            [request_id],
            baseline,
            {"arxiv"},
            {"openalex"},
        )

        self.assertEqual(["cand-new-routable"], [item["candidate_id"] for item in eligible])
        for non_append_state in ("reviewed", "deferred"):
            with self.subTest(non_append_state=non_append_state):
                injected = {**new_routable, "lifecycle_state": non_append_state}
                self.assertEqual(
                    [],
                    CONTROLLER.eligible_new_discovery_candidates(
                        [historical, injected],
                        [request_id],
                        baseline,
                        {"arxiv"},
                        {"openalex"},
                    ),
                )

        raw_baseline = {
            "algorithm": "sha256-content-v1",
            "file_count": 0,
            "total_bytes": 0,
            "fingerprint": "sha256:" + "0" * 64,
        }
        work_order = {
            "phase": "discovery",
            "run_id": "run-discovery",
            "scope": {"question_slugs": [], "request_ids": [request_id], "candidate_ids": []},
            "provider_policy": {
                "discovery": {"enabled": True, "providers": ["arxiv"]},
                "acquisition": {"enabled": True, "providers": ["openalex"]},
            },
            "required_postconditions": [
                {
                    "check": "request_scoped_candidates_increased",
                    "before": 1,
                    "candidate_states_before": baseline,
                    "candidate_record_fingerprints_before": CONTROLLER.candidate_record_fingerprint_snapshot(
                        [historical]
                    ),
                },
                {
                    "check": "discovery_never_fetches",
                    "manifest_records_before": 0,
                    "manifest_digest_before": None,
                },
                {"check": "raw_tree_unchanged", "before": raw_baseline},
            ],
        }
        status = {"readiness": {"verdict": "blocked_on_sources"}, "sources": {"manifest_records": 0}}
        run_controller = mock.Mock()
        run_controller.load_run_state.return_value = {"state": {"current": "discovering"}}

        def verify(candidates: list[dict]) -> tuple[str | None, str | None]:
            def sibling(stem: str):
                return run_controller if stem == "run_controller" else SOURCE_REQUESTS

            with (
                mock.patch.object(CONTROLLER, "load_config", return_value={}),
                mock.patch.object(CONTROLLER, "fresh_workspace_status", return_value=status),
                mock.patch.object(CONTROLLER, "load_candidates", return_value=candidates),
                mock.patch.object(CONTROLLER, "raw_tree_snapshot", return_value=raw_baseline),
                mock.patch.object(CONTROLLER, "evidence_manifest_digest", return_value=None),
                mock.patch.object(CONTROLLER, "load_sibling_module", side_effect=sibling),
            ):
                return CONTROLLER.verify_action_postconditions(
                    Path("/unused"),
                    {"agent_id": "test-agent"},
                    work_order,
                )

        with self.assertRaisesRegex(
            CONTROLLER.OrchestrationControllerError,
            "no newly added, reviewable candidate",
        ):
            verify([historical, new_unroutable])
        self.assertEqual(("candidate_review", None), verify([historical, new_routable]))
        disabled_provider = {
            **new_routable,
            "candidate_id": "cand-disabled-provider",
            "provider": "github",
            "discovery_providers": ["github"],
        }
        with self.assertRaisesRegex(
            CONTROLLER.OrchestrationControllerError,
            "enabled discovery-provider policy",
        ):
            verify([historical, new_routable, disabled_provider])

    def test_academic_candidate_routes_through_either_retained_provider_identity(self):
        merged_arxiv = {
            "provider": "arxiv",
            "source_type": "paper",
            "paper": {
                "provider_ids": {
                    "arxiv": "2601.12345v2",
                    "openalex": "W12345",
                    "doi": ACADEMIC_DOI,
                }
            },
        }
        merged_openalex = {
            "provider": "openalex",
            "source_type": "paper",
            "paper": {
                "provider_ids": {
                    "arxiv": "2601.12345v2",
                    "openalex": "W12345",
                    "doi": ACADEMIC_DOI,
                }
            },
        }
        self.assertEqual("openalex", CONTROLLER.acquisition_route(merged_arxiv, {"openalex"}))
        self.assertEqual("arxiv", CONTROLLER.acquisition_route(merged_openalex, {"arxiv"}))
        self.assertIsNone(CONTROLLER.acquisition_route(merged_arxiv, {"github"}))
        search_paper = {**merged_arxiv, "provider": "search"}
        search_repository = {
            "provider": "search",
            "source_type": "code_repository",
            "url": "https://github.com/example/electrolyte-data",
        }
        official_web = {
            "provider": "search",
            "source_type": "web_page",
            "url": "https://standards.example.test/electrolytes",
            "official_source": True,
        }
        unofficial_web = {
            **official_web,
            "official_source": False,
            "trust_tier": "secondary",
        }
        manual_dataset = {
            "provider": "search",
            "source_type": "dataset",
            "url": "https://data.example.test/electrolytes.csv",
            "official_source": True,
        }
        self.assertEqual("openalex", CONTROLLER.acquisition_route(search_paper, {"openalex"}))
        self.assertEqual("github", CONTROLLER.acquisition_route(search_repository, {"github"}))
        self.assertEqual("web", CONTROLLER.acquisition_route(official_web, {"web"}))
        self.assertIsNone(CONTROLLER.acquisition_route(unofficial_web, {"web"}))
        self.assertIsNone(CONTROLLER.acquisition_route(manual_dataset, {"web"}))

    def test_only_end_to_end_composable_provider_pairs_can_issue_discovery(self):
        self.assertEqual(
            ["search"],
            CONTROLLER.composable_discovery_providers(
                {
                    "discovery": {"enabled": True, "providers": ["arxiv", "search"]},
                    "acquisition": {"enabled": True, "providers": ["github"]},
                }
            ),
        )
        self.assertEqual(
            ["search"],
            CONTROLLER.composable_discovery_providers(
                {
                    "discovery": {"enabled": True, "providers": ["search"]},
                    "acquisition": {"enabled": True, "providers": ["openalex"]},
                }
            ),
        )
        self.assertEqual(
            ["arxiv", "search"],
            CONTROLLER.composable_discovery_providers(
                {
                    "discovery": {"enabled": True, "providers": ["arxiv", "search"]},
                    "acquisition": {"enabled": True, "providers": ["openalex", "web"]},
                }
            ),
        )

    def test_acquisition_reconciles_existing_matching_evidence_and_requires_exact_reopen(self):
        request_id = "req-existing-evidence"
        candidate_id = "cand-existing-evidence"
        source_id = "html:existing-evidence"
        work_order = {
            "phase": "acquisition",
            "run_id": "run-existing-evidence",
            "scope": {
                "question_slugs": ["test-question"],
                "request_ids": [request_id],
                "candidate_ids": [candidate_id],
            },
            "required_postconditions": [
                {"check": "request_fulfilled_with_normalized_source"},
                {
                    "check": "linked_blocked_questions_reopened",
                    "blocked_questions_before": {
                        "test-question": {
                            "status": "blocked",
                            "blocking_request_ids": [request_id],
                            "source_ids_before": [],
                        }
                    },
                },
                {
                    "check": "manifest_records_increased",
                    "before": 1,
                    "matching_source_ids_before": [source_id],
                },
            ],
        }
        fulfilled_request = {
            "request_id": request_id,
            "status": "fulfilled",
            "source_id": source_id,
            "question_slugs": ["test-question"],
        }
        manifest_record = {
            "id": source_id,
            "provenance": {"request_id": request_id, "candidate_id": candidate_id},
        }
        fetched_candidate = {
            "candidate_id": candidate_id,
            "source_request_id": request_id,
            "lifecycle_state": "fetched",
            "fetched_source_id": source_id,
        }
        status = {
            "readiness": {"verdict": "in_progress"},
            "sources": {"manifest_records": 1},
            "questions": {"blocked_slugs": []},
        }
        run_controller = mock.Mock()
        run_controller.load_run_state.return_value = {"state": {"current": "fetching"}}
        source_requests = mock.Mock()
        source_requests.requests_path.return_value = Path("/unused/source-requests.jsonl")
        source_requests.load_requests.return_value = [fulfilled_request]
        normalize_sources = mock.Mock()
        normalize_sources.source_paths.return_value = (
            "sources/manifest.jsonl",
            "sources/normalized",
        )
        normalize_sources.load_manifest.return_value = [manifest_record]
        normalize_sources.records_by_source_id.return_value = {source_id: manifest_record}

        with tempfile.TemporaryDirectory() as tmpdir:
            normalized_path = Path(tmpdir) / "sources" / "normalized" / "existing.md"
            normalized_path.parent.mkdir(parents=True)
            normalized_path.write_text("normalized evidence\n", encoding="utf-8")
            normalize_sources.normalized_output_path_for_record.return_value = normalized_path
            raw_baseline = {
                "algorithm": "sha256-content-v1",
                "file_count": 0,
                "total_bytes": 0,
                "fingerprint": "sha256:" + hashlib.sha256(b"").hexdigest(),
                "entries": {},
            }
            selected_candidate = {
                **fetched_candidate,
                "lifecycle_state": "selected",
            }
            selected_candidate.pop("fetched_source_id")
            open_request = {**fulfilled_request, "status": "open"}
            open_request.pop("source_id")
            manifest_guard = work_order["required_postconditions"][2]
            manifest_fingerprint = CONTROLLER.canonical_json_fingerprint(
                manifest_record,
                label="test manifest",
            )[0]
            normalized_fingerprint = CONTROLLER.file_digest(
                normalized_path,
                containment_root=Path(tmpdir),
            )
            manifest_guard.update(
                {
                    "matching_source_records_before": {
                        source_id: {
                            "record_fingerprint": manifest_fingerprint,
                            "normalized_fingerprint": normalized_fingerprint,
                        }
                    },
                    "manifest_record_fingerprints_before": {source_id: manifest_fingerprint},
                    "raw_tree_before": raw_baseline,
                    "candidate_record_fingerprints_before": (
                        CONTROLLER.candidate_record_fingerprint_snapshot([selected_candidate])
                    ),
                    "source_request_record_fingerprints_before": CONTROLLER.record_fingerprint_snapshot(
                        [open_request],
                        id_field="request_id",
                        label="test requests",
                    ),
                    "normalized_file_fingerprints_before": {
                        "sources/normalized/existing.md": normalized_fingerprint,
                    },
                    "question_file_fingerprints_before": {
                        "test-question.md": "sha256:" + "4" * 64,
                    },
                }
            )

            def sibling(stem: str):
                return {
                    "run_controller": run_controller,
                    "source_requests": source_requests,
                    "normalize_sources": normalize_sources,
                }[stem]

            def verify(question: dict) -> tuple[str | None, str | None]:
                with (
                    mock.patch.object(CONTROLLER, "load_config", return_value={}),
                    mock.patch.object(CONTROLLER, "fresh_workspace_status", return_value=status),
                    mock.patch.object(CONTROLLER, "open_requests", return_value=[]),
                    mock.patch.object(CONTROLLER, "load_candidates", return_value=[fetched_candidate]),
                    mock.patch.object(CONTROLLER, "raw_tree_snapshot", return_value=raw_baseline),
                    mock.patch.object(
                        CONTROLLER,
                        "normalized_file_fingerprint_snapshot",
                        return_value={"sources/normalized/existing.md": normalized_fingerprint},
                    ),
                    mock.patch.object(
                        CONTROLLER,
                        "question_file_fingerprint_snapshot",
                        return_value={"test-question.md": "sha256:" + "5" * 64},
                    ),
                    mock.patch.object(
                        CONTROLLER,
                        "scoped_question_evidence_snapshot",
                        return_value={"test-question": question},
                    ),
                    mock.patch.object(CONTROLLER, "load_sibling_module", side_effect=sibling),
                ):
                    return CONTROLLER.verify_action_postconditions(
                        Path(tmpdir),
                        {"agent_id": "agent-test"},
                        work_order,
                    )

            reopened = {
                "status": "open",
                "blocking_request_ids": [],
                "source_ids": [source_id],
            }
            self.assertEqual(("research", None), verify(reopened))
            for bypass_status in ("answered", "deferred", "rejected"):
                with self.subTest(bypass_status=bypass_status), self.assertRaisesRegex(
                    CONTROLLER.OrchestrationControllerError,
                    "did not reopen every scoped blocked question",
                ):
                    verify({**reopened, "status": bypass_status})

    def test_legacy_acquisition_without_reconciliation_baselines_requires_fresh_session(self):
        with self.assertRaisesRegex(
            CONTROLLER.OrchestrationControllerError,
            "question/evidence baseline",
        ):
            CONTROLLER.require_action_baselines(
                {
                    "phase": "acquisition",
                    "action_id": "action-legacy-acquisition",
                    "required_postconditions": [
                        {"check": "request_fulfilled_with_normalized_source"},
                        {"check": "linked_blocked_questions_reopened"},
                        {"check": "manifest_records_increased", "before": 0},
                    ],
                }
            )

    def test_child_creation_intent_survives_crash_before_work_order_persistence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.start(target)
            with mock.patch.object(CONTROLLER, "issue_work_order", side_effect=RuntimeError("injected crash")):
                with self.assertRaisesRegex(RuntimeError, "injected crash"):
                    self.run_module(
                        CONTROLLER,
                        [
                            "--project-root",
                            str(target),
                            "next",
                            "--orchestration-id",
                            "orch-test",
                            "--format",
                            "json",
                        ],
                    )

            persisted = json.loads(
                (target / "runs" / "orchestrations" / "orch-test" / "session.json").read_text(encoding="utf-8")
            )
            self.assertEqual(1, len(persisted["child_run_ids"]))
            child_run_id = persisted["child_run_ids"][0]
            self.assertEqual(child_run_id, persisted["active_run_id"])
            self.assertIsNone(persisted["pending_action_id"])

            code, order, stderr = self.controller(target, "next", "--orchestration-id", "orch-test")
            self.assertEqual(0, code, stderr)
            self.assertEqual(child_run_id, order["run_id"])
            session = CONTROLLER.load_session(target, "orch-test")
            self.assertEqual([child_run_id], session["child_run_ids"])

    def test_identical_submit_recovers_after_child_finalization_precedes_session_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            self.start(target)
            _, order, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
            self.build_verification_bundle(target, order["run_id"])
            result_path = root / "crash-result.json"
            result_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "action_id": order["action_id"],
                        "outcome": "completed",
                        "summary": "Verification completed before the injected parent-session crash.",
                        "artifacts": [],
                    }
                ),
                encoding="utf-8",
            )
            real_write = CONTROLLER.write_json_atomic
            expected_session_path = CONTROLLER.session_path(target.resolve(), "orch-test")
            crashed = False

            def crash_before_session_commit(path: Path, document: dict) -> None:
                nonlocal crashed
                if (
                    not crashed
                    and path == expected_session_path
                    and document.get("last_completed_action_id") == order["action_id"]
                ):
                    crashed = True
                    raise CONTROLLER.OrchestrationControllerError(
                        "INJECTED_CRASH",
                        "injected crash before parent session commit",
                    )
                real_write(path, document)

            with mock.patch.object(CONTROLLER, "write_json_atomic", side_effect=crash_before_session_commit):
                code, error, _ = self.controller(
                    target,
                    "submit",
                    "--orchestration-id",
                    "orch-test",
                    "--action-id",
                    order["action_id"],
                    "--result-file",
                    str(result_path),
                )
            self.assertTrue(crashed, "fault injection must match the controller's canonical session path")
            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("INJECTED_CRASH", error["error_code"])
            self.assertTrue(
                CONTROLLER.work_result_path(target, "orch-test", order["action_id"]).is_file(),
                "accepted result must precede child/session finalization",
            )
            self.assertEqual("complete", RUN_CONTROLLER.load_run_state(target, order["run_id"])["state"]["current"])

            code, completed, stderr = self.controller(
                target,
                "submit",
                "--orchestration-id",
                "orch-test",
                "--action-id",
                order["action_id"],
                "--result-file",
                str(result_path),
            )
            self.assertEqual(0, code, stderr)
            self.assertEqual("complete", completed["status"])
            self.assertEqual(1, completed["completed_action_count"])
            self.assertEqual([order["run_id"]], completed["child_run_ids"])

    def test_terminal_submit_recovers_after_child_finalization_precedes_session_write(self):
        cases = (
            ("blocked", "blocked_on_sources", CONTROLLER.EXIT_BLOCKED),
            ("failed", "failed", CONTROLLER.EXIT_INVALID),
        )
        for outcome, expected_status, expected_exit_code in cases:
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                target = self.init_workspace(root, question=True)
                self.start(target)
                _, order, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
                result_path = root / f"{outcome}-crash-result.json"
                result_path.write_text(
                    json.dumps(
                        {
                            "schema_version": "1.0",
                            "action_id": order["action_id"],
                            "outcome": outcome,
                            "summary": f"Worker ended the action as {outcome}.",
                            "artifacts": [],
                        }
                    ),
                    encoding="utf-8",
                )
                real_write = CONTROLLER.write_json_atomic
                expected_session_path = CONTROLLER.session_path(target.resolve(), "orch-test")
                expected_action_id = order["action_id"]
                crashed = False

                def crash_before_parent_commit(
                    path: Path,
                    document: dict,
                    expected_path: Path = expected_session_path,
                    action_id: str = expected_action_id,
                    write=real_write,
                ) -> None:
                    nonlocal crashed
                    if (
                        not crashed
                        and path == expected_path
                        and document.get("last_completed_action_id") == action_id
                    ):
                        crashed = True
                        raise CONTROLLER.OrchestrationControllerError(
                            "INJECTED_CRASH",
                            "injected crash after terminal child finalization",
                        )
                    write(path, document)

                with mock.patch.object(
                    CONTROLLER,
                    "write_json_atomic",
                    side_effect=crash_before_parent_commit,
                ):
                    code, error, _ = self.controller(
                        target,
                        "submit",
                        "--orchestration-id",
                        "orch-test",
                        "--action-id",
                        order["action_id"],
                        "--result-file",
                        str(result_path),
                    )
                self.assertTrue(crashed)
                self.assertEqual(CONTROLLER.EXIT_INVALID, code)
                self.assertEqual("INJECTED_CRASH", error["error_code"])
                self.assertEqual(
                    expected_status,
                    RUN_CONTROLLER.load_run_state(target, order["run_id"])["state"]["current"],
                )

                code, completed, stderr = self.controller(
                    target,
                    "submit",
                    "--orchestration-id",
                    "orch-test",
                    "--action-id",
                    order["action_id"],
                    "--result-file",
                    str(result_path),
                )
                self.assertEqual(expected_exit_code, code, stderr)
                self.assertEqual(expected_status, completed["status"])
                self.assertIsNone(completed["active_run_id"])
                self.assertIsNone(completed["pending_submission"])
                self.assertEqual(1, completed["completed_action_count"])

    def test_record_event_once_recognizes_legacy_equivalent_events(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            session = self.start(target)
            CONTROLLER.record_event(
                target,
                session,
                "action_completed",
                "Legacy action completion event.",
                action_id="action-0001",
            )
            CONTROLLER.record_event(
                target,
                session,
                "session_finished",
                "Legacy session completion event.",
            )

            CONTROLLER.record_event_once(
                target,
                session,
                "action_completed",
                "Replayed action completion event.",
                action_id="action-0001",
            )
            CONTROLLER.record_event_once(
                target,
                session,
                "session_finished",
                "Replayed session completion event.",
            )

            events = [
                json.loads(line)
                for line in CONTROLLER.events_path(target, "orch-test").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(
                1,
                sum(
                    event.get("event_type") == "action_completed"
                    and event.get("action_id") == "action-0001"
                    for event in events
                ),
            )
            self.assertEqual(1, sum(event.get("event_type") == "session_finished" for event in events))

    def test_next_finalizes_prepared_submission_after_result_persistence_crash(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            self.start(target)
            _, order, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
            self.build_verification_bundle(target, order["run_id"])
            result_path = root / "prepared-result.json"
            result_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "action_id": order["action_id"],
                        "outcome": "completed",
                        "summary": "Accepted before the injected result persistence crash.",
                        "artifacts": [],
                    }
                ),
                encoding="utf-8",
            )
            expected_result_path = CONTROLLER.work_result_path(target.resolve(), "orch-test", order["action_id"])
            real_write = CONTROLLER.write_json_atomic
            crashed = False

            def crash_before_result_persistence(path: Path, document: dict) -> None:
                nonlocal crashed
                if not crashed and path == expected_result_path:
                    crashed = True
                    raise CONTROLLER.OrchestrationControllerError(
                        "INJECTED_CRASH",
                        "injected crash before work-result persistence",
                    )
                real_write(path, document)

            with mock.patch.object(CONTROLLER, "write_json_atomic", side_effect=crash_before_result_persistence):
                code, error, _ = self.controller(
                    target,
                    "submit",
                    "--orchestration-id",
                    "orch-test",
                    "--action-id",
                    order["action_id"],
                    "--result-file",
                    str(result_path),
                )
            self.assertTrue(crashed)
            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("INJECTED_CRASH", error["error_code"])
            prepared = CONTROLLER.load_session(target, "orch-test")
            self.assertEqual(order["action_id"], prepared["pending_submission"]["action_id"])
            self.assertEqual(CONTROLLER.RECOVERY_FINALIZING, prepared["recovery"]["state"])
            self.assertFalse(expected_result_path.exists())

            code, completed, stderr = self.controller(
                target,
                "next",
                "--orchestration-id",
                "orch-test",
            )
            self.assertEqual(0, code, stderr)
            self.assertEqual("complete", completed["status"])
            self.assertIsNone(completed["pending_submission"])
            self.assertTrue(expected_result_path.is_file())
            self.assertEqual(1, completed["completed_action_count"])

    def test_next_repairs_missing_completion_events_without_recounting_action(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            self.start(target)
            _, order, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
            self.build_verification_bundle(target, order["run_id"])

            with mock.patch.object(
                CONTROLLER,
                "ensure_completion_events",
                side_effect=CONTROLLER.OrchestrationControllerError(
                    "INJECTED_CRASH",
                    "injected crash after final session commit",
                ),
            ):
                code, error, _ = self.submit(root, target, order["action_id"])
            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("INJECTED_CRASH", error["error_code"])
            committed = CONTROLLER.load_session(target, "orch-test")
            self.assertEqual("complete", committed["status"])
            self.assertEqual(1, committed["completed_action_count"])

            code, completed, stderr = self.controller(target, "next", "--orchestration-id", "orch-test")
            self.assertEqual(0, code, stderr)
            self.assertEqual("complete", completed["status"])
            self.assertEqual(1, completed["completed_action_count"])
            events = [
                json.loads(line)
                for line in CONTROLLER.events_path(target, "orch-test").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(
                1,
                sum(event.get("event_type") == "action_completed" for event in events),
            )
            self.assertEqual(
                1,
                sum(event.get("event_type") == "session_finished" for event in events),
            )

    def test_standalone_controller_does_not_mutate_protected_scripts_with_bytecode(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            controller_path = target / "scripts" / "orchestration_controller.py"
            environment = os.environ.copy()
            environment.pop("PYTHONDONTWRITEBYTECODE", None)
            environment.pop("PYTHONPYCACHEPREFIX", None)

            def run_controller(*args: str) -> dict:
                completed = subprocess.run(
                    [
                        sys.executable,
                        str(controller_path),
                        "--project-root",
                        str(target),
                        *args,
                        "--format",
                        "json",
                    ],
                    cwd=target,
                    env=environment,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(0, completed.returncode, completed.stderr or completed.stdout)
                return json.loads(completed.stdout)

            run_controller(
                "start",
                "--orchestration-id",
                "orch-bytecode",
                "--agent-id",
                "bytecode-agent",
            )
            first = run_controller("next", "--orchestration-id", "orch-bytecode")
            replayed = run_controller("next", "--orchestration-id", "orch-bytecode")

            self.assertEqual(first["action_id"], replayed["action_id"])
            self.assertEqual([], list((target / "scripts").rglob("__pycache__")))
            self.assertEqual([], list((target / "scripts").rglob("*.pyc")))

    def test_pending_work_checks_trusted_fingerprint_before_narrowed_provider_policy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.enable_academic_providers(target)
            self.start(target)
            _, order, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
            config_path = target / "research.yml"
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config["integrations"]["discovery"]["providers"] = ["arxiv"]
            config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

            code, error, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("ORCHESTRATION_TRUSTED_INPUT_CHANGED", error["error_code"])
            self.assertTrue(any(path.startswith("research.yml ") for path in error["details"]["changed_paths"]))
            self.assertEqual(order["action_id"], CONTROLLER.load_session(target, "orch-test")["pending_action_id"])

    def test_legacy_pending_work_refuses_narrowed_provider_policy_on_replay(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.enable_academic_providers(target)
            self.start(target)
            _, order, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
            session = CONTROLLER.load_session(target, "orch-test")
            session.pop("pending_trusted_static_inputs")
            CONTROLLER.write_json_atomic(CONTROLLER.session_path(target, "orch-test"), session)
            CONTROLLER.trusted_static_input_path(target, "orch-test", order["action_id"]).unlink()
            config_path = target / "research.yml"
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config["integrations"]["discovery"]["providers"] = ["arxiv"]
            config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

            code, error, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("ORCHESTRATION_PROVIDER_POLICY_CHANGED", error["error_code"])
            self.assertEqual(order["action_id"], CONTROLLER.load_session(target, "orch-test")["pending_action_id"])

    def test_empty_raw_end_to_end_research_discovers_acquires_reopens_and_exports(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.enable_academic_providers(target)
            self.assertEqual(
                [],
                [path for path in (target / "raw").rglob("*") if path.is_file() and path.name != ".gitkeep"],
            )

            self.start(target)
            preflight_status = STATUS.build_status_document(target)
            smoke_details = CONTROLLER.load_sibling_module("smoke_validate_workspace").run_checks(target)
            self.assertNotEqual(
                "attention_required",
                preflight_status["readiness"]["verdict"],
                smoke_details,
            )
            _, first_research, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
            self.assertEqual("research", first_research["phase"], first_research)

            request_report = self.assert_json_script_ok(
                SOURCE_REQUESTS,
                [
                    "--project-root",
                    str(target),
                    "add",
                    "--kind",
                    "paper",
                    "--query-or-identifier",
                    "solid electrolyte room-temperature ionic conductivity above 1 mS/cm",
                    "--rationale",
                    "The open question needs primary academic evidence for the conductivity threshold.",
                    "--priority",
                    "high",
                    "--question-slug",
                    "test-question",
                    "--format",
                    "json",
                ],
            )
            request_id = request_report["request"]["request_id"]
            self.assert_json_script_ok(
                CLAIM,
                [
                    "--project-root",
                    str(target),
                    "claim",
                    "--slug",
                    "test-question",
                    "--agent-id",
                    "answer-agent",
                    "--format",
                    "json",
                ],
            )
            self.assert_json_script_ok(
                RESOLVE,
                [
                    "--project-root",
                    str(target),
                    "block",
                    "--slug",
                    "test-question",
                    "--agent-id",
                    "answer-agent",
                    "--blocked-reason",
                    "No delivered academic evidence is available yet.",
                    "--request-id",
                    request_id,
                    "--format",
                    "json",
                ],
            )
            code, after_block, stderr = self.submit(
                root,
                target,
                first_research["action_id"],
                summary="Recorded the evidence gap and blocked the question on a source request.",
                artifacts=["sources/source-requests.jsonl", "wiki/questions/test-question.md"],
            )
            self.assertEqual(0, code, stderr)
            self.assertEqual("active", after_block["status"])
            first_child_path = target / "runs" / first_research["run_id"] / "run-state.json"
            first_child_terminal_bytes = first_child_path.read_bytes()
            self.assertEqual(
                "blocked_on_sources",
                json.loads(first_child_terminal_bytes)["state"]["current"],
            )

            _, discovery_order, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
            self.assertEqual("discovery", discovery_order["phase"])
            hydrated_discovery_order = self.hydrated_order(target, discovery_order)
            candidate_baseline = next(
                item
                for item in hydrated_discovery_order["required_postconditions"]
                if item["check"] == "request_scoped_candidates_increased"
            )
            self.assertEqual(0, candidate_baseline["before"])
            self.assertEqual({}, candidate_baseline["candidate_states_before"])
            provider_calls: list[tuple[str, str]] = []

            def arxiv_transport(url, _timeout, _headers):
                provider_calls.append(("arxiv", url))
                return ARXIV_PAYLOAD

            def openalex_transport(url, _timeout, _headers):
                provider_calls.append(("openalex", url))
                return openalex_payload()

            with (
                mock.patch.object(DISCOVER, "ARXIV_TRANSPORT", arxiv_transport),
                mock.patch.object(DISCOVER, "OPENALEX_TRANSPORT", openalex_transport),
                mock.patch.object(DISCOVER, "ARXIV_CLOCK", lambda: 0.0),
                mock.patch.object(DISCOVER, "OPENALEX_CLOCK", lambda: 0.0),
                mock.patch.object(DISCOVER, "ARXIV_SLEEP", lambda _seconds: None),
                mock.patch.object(DISCOVER, "OPENALEX_SLEEP", lambda _seconds: None),
                mock.patch.object(DISCOVER, "ARXIV_LAST_REQUEST_AT", None),
                mock.patch.object(DISCOVER, "OPENALEX_LAST_REQUEST_AT", None),
            ):
                discovery = self.assert_json_script_ok(
                    DISCOVER,
                    [
                        "--project-root",
                        str(target),
                        "--format",
                        "json",
                        "academic",
                        "--request-id",
                        request_id,
                        "--provider",
                        "arxiv",
                        "--provider",
                        "openalex",
                        "--max-results",
                        "15",
                    ],
                )
            self.assertEqual(1, discovery["count"])
            self.assertEqual({"arxiv", "openalex"}, {provider for provider, _url in provider_calls})
            candidate = discovery["candidates"][0]
            candidate_id = candidate["candidate_id"]
            self.assertEqual(["arxiv", "openalex"], candidate["discovery_providers"])
            self.assertEqual([], self.manifest_records(target), "discovery must not deliver evidence")
            code, _, stderr = self.submit(
                root,
                target,
                discovery_order["action_id"],
                summary="Mocked academic providers produced one deduplicated request-scoped candidate.",
                artifacts=["sources/discovery/candidates.jsonl"],
            )
            self.assertEqual(0, code, stderr)

            _, review_order, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
            self.assertEqual("candidate_review", review_order["phase"])
            self.assertEqual([candidate_id], review_order["scope"]["candidate_ids"])
            self.assert_json_script_ok(
                DISCOVER,
                [
                    "--project-root",
                    str(target),
                    "--format",
                    "json",
                    "candidates",
                    "select",
                    "--candidate-id",
                    candidate_id,
                    "--request-id",
                    request_id,
                    "--reason",
                    "Selected the academic-primary arXiv route for relevant threshold evidence.",
                    "--actor",
                    "review-agent",
                    "--run-id",
                    review_order["run_id"],
                ],
            )
            code, _, stderr = self.submit(
                root,
                target,
                review_order["action_id"],
                summary="Reviewed and selected the routable academic candidate.",
                artifacts=["sources/discovery/candidates.jsonl"],
            )
            self.assertEqual(0, code, stderr)

            _, acquisition_order, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
            self.assertEqual("acquisition", acquisition_order["phase"])
            hydrated_acquisition_order = self.hydrated_order(target, acquisition_order)
            acquisition_guards = {
                item["check"]: item for item in hydrated_acquisition_order["required_postconditions"]
            }
            self.assertEqual(
                {
                    "test-question": {
                        "status": "blocked",
                        "blocking_request_ids": [request_id],
                        "source_ids_before": [],
                    }
                },
                acquisition_guards["linked_blocked_questions_reopened"]["blocked_questions_before"],
            )
            self.assertEqual(
                [],
                acquisition_guards["manifest_records_increased"]["matching_source_ids_before"],
            )
            raw_relative = self.write_mock_acquired_paper(
                target,
                request_id=request_id,
                candidate_id=candidate_id,
            )
            inventory = self.assert_json_script_ok(
                INVENTORY,
                ["--project-root", str(target), "--report", "--format", "json"],
            )
            self.assertEqual("ready_for_normalization", inventory["readiness"])
            normalized = self.assert_json_script_ok(
                NORMALIZE,
                ["--project-root", str(target), "--all", "--format", "json"],
            )
            self.assertEqual(1, normalized["summary"]["created"])
            records = self.manifest_records(target)
            matches = [record for record in records if raw_relative in record.get("raw_paths", [])]
            self.assertEqual(1, len(matches))
            source_id = matches[0]["id"]
            self.assert_json_script_ok(
                SOURCE_REQUESTS,
                [
                    "--project-root",
                    str(target),
                    "fulfill",
                    "--request-id",
                    request_id,
                    "--source-id",
                    source_id,
                    "--format",
                    "json",
                ],
            )
            self.assert_json_script_ok(
                RESOLVE,
                [
                    "--project-root",
                    str(target),
                    "reopen",
                    "--slug",
                    "test-question",
                    "--agent-id",
                    "acquire-agent",
                    "--source-id",
                    source_id,
                    "--request-id",
                    request_id,
                    "--format",
                    "json",
                ],
            )

            code, error, _ = self.submit(
                root,
                target,
                acquisition_order["action_id"],
                summary="Fulfilled evidence before recording the candidate-to-source transition.",
            )
            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("ORCHESTRATION_POSTCONDITION_FAILED", error["error_code"])
            self.assertIn("candidate provenance", error["message"])

            self.assert_json_script_ok(
                DISCOVER,
                [
                    "--project-root",
                    str(target),
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
                    "Provenance-backed evidence was inventoried and normalized.",
                    "--source-id",
                    source_id,
                    "--actor",
                    "acquire-agent",
                    "--run-id",
                    acquisition_order["run_id"],
                ],
            )

            manifest_path = target / "sources" / "manifest.jsonl"
            original_manifest = manifest_path.read_text(encoding="utf-8")
            wrong_records = self.manifest_records(target)
            wrong_records[0]["provenance"]["candidate_id"] = "cand-out-of-scope"
            manifest_path.write_text(
                "".join(json.dumps(record, sort_keys=True) + "\n" for record in wrong_records),
                encoding="utf-8",
            )
            code, error, _ = self.submit(
                root,
                target,
                acquisition_order["action_id"],
                summary="Fulfilled evidence with unrelated candidate provenance.",
            )
            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("ORCHESTRATION_POSTCONDITION_FAILED", error["error_code"])
            self.assertIn("candidate provenance", error["message"])
            manifest_path.write_text(original_manifest, encoding="utf-8")

            # Resuming while the acquisition result is pending must replay the
            # same action and cannot authorize a second delivery.
            raw_digest = hashlib.sha256((target / raw_relative).read_bytes()).hexdigest()
            _, replayed, _ = self.controller(
                target,
                "next",
                "--orchestration-id",
                "orch-test",
                "--resume",
            )
            self.assertEqual(acquisition_order["action_id"], replayed["action_id"])
            self.assertEqual(raw_digest, hashlib.sha256((target / raw_relative).read_bytes()).hexdigest())
            self.assertEqual(1, len(self.manifest_records(target)))

            code, _, stderr = self.submit(
                root,
                target,
                acquisition_order["action_id"],
                summary="Delivered, inventoried, normalized, fulfilled, and reopened the linked question.",
                artifacts=[
                    raw_relative,
                    f"{raw_relative}.provenance.yml",
                    "sources/manifest.jsonl",
                    "sources/source-requests.jsonl",
                    "wiki/questions/test-question.md",
                ],
            )
            self.assertEqual(0, code, stderr)

            _, second_research, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
            self.assertEqual("research", second_research["phase"])
            self.assertNotEqual(first_research["run_id"], second_research["run_id"])
            self.write_grounded_answer(target, source_id)
            coverage = self.assert_json_script_ok(
                COVERAGE,
                [
                    "--project-root",
                    str(target),
                    "evaluate",
                    "--slug",
                    "test-question",
                    "--format",
                    "json",
                ],
            )
            self.assertEqual("pass", coverage["coverage_verdict"])
            self.assert_json_script_ok(
                CLAIM,
                [
                    "--project-root",
                    str(target),
                    "claim",
                    "--slug",
                    "test-question",
                    "--agent-id",
                    "answer-agent",
                    "--format",
                    "json",
                ],
            )
            self.assert_json_script_ok(
                RESOLVE,
                [
                    "--project-root",
                    str(target),
                    "answer",
                    "--slug",
                    "test-question",
                    "--agent-id",
                    "answer-agent",
                    "--answer-page",
                    "wiki/synthesis/test-answer.md",
                    "--source-id",
                    source_id,
                    "--confidence",
                    "medium",
                    "--evidence-strength",
                    "single_source",
                    "--require-coverage",
                    "--require-grounding",
                    "--coverage-manifest",
                    "sources/coverage/test-question.yml",
                    "--format",
                    "json",
                ],
            )
            code, _, stderr = self.submit(
                root,
                target,
                second_research["action_id"],
                summary="Produced a coverage-qualified answer with a normalized-source grounding quote.",
                artifacts=[
                    "wiki/questions/test-question.md",
                    "wiki/synthesis/test-answer.md",
                    "sources/coverage/test-question.yml",
                ],
            )
            self.assertEqual(0, code, stderr)

            _, verification_order, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
            self.assertEqual("verification", verification_order["phase"])
            quote_report = self.assert_json_script_ok(
                VERIFY_QUOTES,
                [
                    "--project-root",
                    str(target),
                    "--slug",
                    "test-question",
                    "--write",
                    "--verified-by",
                    "verification-agent",
                    "--format",
                    "json",
                ],
            )
            self.assertEqual("verified", quote_report["overall_result"])
            verification_bundle = self.build_verification_bundle(target, verification_order["run_id"])
            self.assertEqual("ship", verification_bundle["publication_readiness"]["verdict"])
            evaluation = target / "runs" / verification_order["run_id"] / "evaluation"

            fabricated_export_path = evaluation / "export.json"
            fabricated_export = json.loads(fabricated_export_path.read_text(encoding="utf-8"))
            fabricated_export["questions"] = []
            fabricated_export["counts"] = {"total": 0, "by_status": {}, "exported": 0}
            fabricated_export_path.write_text(json.dumps(fabricated_export), encoding="utf-8")
            code, rejected, _ = self.submit(
                root,
                target,
                verification_order["action_id"],
                summary="Fresh deterministic verification returned ship and exported the answer.",
                artifacts=["wiki/questions/test-question.md"],
            )
            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("ORCHESTRATION_POSTCONDITION_FAILED", rejected["error_code"])
            self.assertIn("export.json", rejected["message"])
            self.assertFalse(
                CONTROLLER.work_result_path(target, "orch-test", verification_order["action_id"]).exists()
            )

            self.build_verification_bundle(target, verification_order["run_id"])
            fabricated_citation_path = evaluation / "citation-verification.json"
            fabricated_citation = json.loads(fabricated_citation_path.read_text(encoding="utf-8"))
            fabricated_citation["results"] = []
            fabricated_citation["counts"] = {
                "verified": 0,
                "mismatch": 0,
                "not_found": 0,
                "skipped_no_live": 0,
                "insufficient_metadata": 0,
                "total": 0,
            }
            fabricated_citation["overall_result"] = "verified"
            fabricated_citation_path.write_text(json.dumps(fabricated_citation), encoding="utf-8")
            code, rejected, _ = self.submit(
                root,
                target,
                verification_order["action_id"],
                summary="Fresh deterministic verification returned ship and exported the answer.",
                artifacts=["wiki/questions/test-question.md"],
            )
            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("ORCHESTRATION_POSTCONDITION_FAILED", rejected["error_code"])
            self.assertIn("citation-verification.json", rejected["message"])
            self.assertFalse(
                CONTROLLER.work_result_path(target, "orch-test", verification_order["action_id"]).exists()
            )

            self.build_verification_bundle(target, verification_order["run_id"])
            fabricated_publication_path = evaluation / "publication-readiness.json"
            fabricated_publication_path.write_text(
                json.dumps({"schema_version": "1.0", "verdict": "ship"}),
                encoding="utf-8",
            )
            code, rejected, _ = self.submit(
                root,
                target,
                verification_order["action_id"],
                summary="Fresh deterministic verification returned ship and exported the answer.",
                artifacts=["wiki/questions/test-question.md"],
            )
            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("ORCHESTRATION_POSTCONDITION_FAILED", rejected["error_code"])
            self.assertIn("publication-readiness.json", rejected["message"])
            self.assertFalse(
                CONTROLLER.work_result_path(target, "orch-test", verification_order["action_id"]).exists()
            )

            self.build_verification_bundle(target, verification_order["run_id"])
            code, completed, stderr = self.submit(
                root,
                target,
                verification_order["action_id"],
                summary="Fresh deterministic verification returned ship and exported the answer.",
                artifacts=["wiki/questions/test-question.md"],
            )
            self.assertEqual(0, code, stderr)
            self.assertEqual("complete", completed["status"])

            # The bounded run that first reported the source gap remains a
            # terminal immutable record while a later run completes the work.
            self.assertEqual(first_child_terminal_bytes, first_child_path.read_bytes())
            first_child = RUN_CONTROLLER.load_run_state(target, first_research["run_id"])
            later_child = RUN_CONTROLLER.load_run_state(target, second_research["run_id"])
            self.assertEqual("blocked_on_sources", first_child["state"]["current"])
            self.assertEqual("complete", later_child["state"]["current"])
            answers_path = target / "runs" / "orchestrations" / "orch-test" / "answers.json"
            answers = json.loads(answers_path.read_text(encoding="utf-8"))
            self.assertEqual(1, answers["counts"]["total"])
            self.assertEqual("answered", answers["questions"][0]["status"])
            self.assertIn(source_id, answers["questions"][0]["source_ids"])
            self.assertEqual(1, len(self.manifest_records(target)))
            evaluation = target / "runs" / second_research["run_id"] / "evaluation"
            citation = json.loads((evaluation / "citation-verification.json").read_text(encoding="utf-8"))
            quotes = json.loads((evaluation / "quote-verification.json").read_text(encoding="utf-8"))
            coverage_summary = json.loads((evaluation / "coverage-summary.json").read_text(encoding="utf-8"))
            lint = json.loads((evaluation / "lint.json").read_text(encoding="utf-8"))
            publication = json.loads((evaluation / "publication-readiness.json").read_text(encoding="utf-8"))
            self.assertEqual("verified", citation["overall_result"])
            self.assertEqual("verified", quotes["overall_result"])
            self.assertEqual(1, coverage_summary["coverage"]["required_question_counts"]["passed"])
            self.assertEqual(0, lint["stats"]["issue_counts"].get("HIGH", 0))
            self.assertEqual("ship", publication["verdict"])


if __name__ == "__main__":
    unittest.main()
