import contextlib
import hashlib
import importlib.util
import io
import json
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

    def json_script(self, module, argv: list[str]) -> tuple[int, dict, str]:
        code, stdout, stderr = self.run_module(module, argv)
        payload_text = stdout.strip() or stderr.strip()
        return code, json.loads(payload_text) if payload_text else {}, stderr

    def assert_json_script_ok(self, module, argv: list[str]) -> dict:
        code, payload, stderr = self.json_script(module, argv)
        self.assertEqual(0, code, stderr)
        return payload

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

    def test_action_limit_pauses_and_resume_starts_a_fresh_window(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.start(target, max_actions=1)
            _, work_order, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
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

    def test_failed_postcondition_retains_no_result_and_identical_retry_succeeds(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            self.start(target)
            _, order, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
            readiness = CONTROLLER.load_sibling_module("publication_readiness")
            with mock.patch.object(readiness, "build_readiness_document", return_value={"verdict": "no_ship"}):
                code, error, _ = self.submit(root, target, order["action_id"])
            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("ORCHESTRATION_POSTCONDITION_FAILED", error["error_code"])
            retained = target / "runs" / "orchestrations" / "orch-test" / "work-results" / "action-0001.json"
            self.assertFalse(retained.exists())

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

    def test_fresh_ship_verification_writes_answer_export_and_completes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            self.start(target)
            _, order, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
            self.assertEqual("verification", order["phase"])

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
        self.assertEqual("openalex", CONTROLLER.acquisition_route(search_paper, {"openalex"}))
        self.assertEqual("github", CONTROLLER.acquisition_route(search_repository, {"github"}))

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
            crashed = False

            def crash_before_session_commit(path: Path, document: dict) -> None:
                nonlocal crashed
                if (
                    not crashed
                    and path == CONTROLLER.session_path(target, "orch-test")
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

    def test_pending_work_refuses_narrowed_provider_policy_on_replay(self):
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
            readiness = self.assert_json_script_ok(
                READINESS,
                ["--project-root", str(target), "--format", "json"],
            )
            self.assertEqual("ship", readiness["verdict"])
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
