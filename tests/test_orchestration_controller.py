import ast
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
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
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
LINT = load_script_module("orchestration_lint", SCRIPTS / "lint.py")
DOCTOR = load_script_module("orchestration_doctor", SCRIPTS / "doctor.py")
SOURCE_REQUESTS = load_script_module("orchestration_source_requests", SCRIPTS / "source_requests.py")
CLAIM = load_script_module("orchestration_question_claim", SCRIPTS / "question_claim.py")
RESOLVE = load_script_module("orchestration_question_resolve", SCRIPTS / "question_resolve.py")
DISCOVER = load_script_module("orchestration_discover_sources", SCRIPTS / "discover_sources.py")
INVENTORY = load_script_module("orchestration_source_inventory", SCRIPTS / "source_inventory.py")
NORMALIZE = load_script_module("orchestration_normalize_sources", SCRIPTS / "normalize_sources.py")
COVERAGE = load_script_module("orchestration_coverage_manifest", SCRIPTS / "coverage_manifest.py")
VERIFY_QUOTES = load_script_module("orchestration_verify_quotes", SCRIPTS / "verify_quotes.py")
READINESS = load_script_module("orchestration_publication_readiness", SCRIPTS / "publication_readiness.py")
LOCKS = load_script_module("orchestration_workspace_locks", SCRIPTS / "_workspace_locks.py")

#: A *real* second driver, in its own OS process, holding the session lock the way
#: the controller holds it.
#:
#: Deliberately not "the test process takes the lock": that would exercise the
#: refusal but not the thing the refusal reports. Here the holder block in the
#: sidecar is published by the controller's own ``driver_session_lock``, so the
#: pid a refused peer reads back is a pid that really owns the lock, written by
#: the code under test rather than by the test.
HOLDING_DRIVER = """
import importlib.util, os, sys, time
from pathlib import Path

scripts, project_root, orchestration_id, ready, release, agent_id = sys.argv[1:7]
spec = importlib.util.spec_from_file_location(
    "held_driver_controller", str(Path(scripts) / "orchestration_controller.py")
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

with module.driver_session_lock(
    Path(project_root),
    orchestration_id,
    command="next",
    agent_id=(agent_id or None),
    wait_seconds=0.0,
):
    Path(ready).write_text(str(os.getpid()), encoding="utf-8")
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline and not Path(release).exists():
        time.sleep(0.02)
"""

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
    def test_answered_slug_filter_accepts_anchor_only_grounding(self):
        """A question grounded only by anchors reaches the verifier like any other.

        The entries are parsed by the verifier rather than hand-written, so the filter is
        tested against the entry shape grounding actually has, not a guess at it.
        """
        anchor_only = VERIFY_QUOTES.grounding_entries(
            {
                "grounding": [
                    {
                        "claim": "The current supplier price is 23.99 EUR.",
                        "source_id": "data:keepa-b0abc123",
                        "anchor": {"pointer": "supplier_quote/price", "expected": "23.99 EUR"},
                    }
                ]
            },
            "anchor-only",
        )
        quote_only = VERIFY_QUOTES.grounding_entries(
            {
                "grounding": [
                    {
                        "claim": "The survey is retained evidence.",
                        "source_id": "raw:bench-survey-2026",
                        "quote": "Benchmark Survey 2026",
                    }
                ]
            },
            "quote-only",
        )
        self.assertEqual(["anchor"], [entry["form"] for entry in anchor_only])

        selected = CONTROLLER.answered_grounded_slugs(
            {
                "questions": [
                    {"slug": "anchor-only", "status": "answered", "grounding": anchor_only},
                    {"slug": "quote-only", "status": "answered", "grounding": quote_only},
                    {"slug": "mixed", "status": "answered", "grounding": anchor_only + quote_only},
                    {"slug": "ungrounded", "status": "answered", "grounding": []},
                    {"slug": "not-answered", "status": "open", "grounding": anchor_only},
                    {"slug": "grounding-not-a-list", "status": "answered", "grounding": {}},
                ]
            }
        )

        self.assertEqual(["anchor-only", "quote-only", "mixed"], selected)

    def test_empty_quote_verification_report_matches_the_real_report_shape(self):
        """The persisted artifact has one schema whether or not a run had grounding.

        Compared against `verify_quotes.build_report`'s own output rather than a second
        hardcoded literal: a literal here would only prove this file agrees with itself.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "quote-shape-workspace"
            (target / "wiki" / "questions").mkdir(parents=True)
            (target / "research.yml").write_text("project:\n  name: Quote Shape\n", encoding="utf-8")
            (target / "wiki" / "questions" / "shape.md").write_text(
                "---\nstatus: answered\n---\n\n# Shape\n",
                encoding="utf-8",
            )

            real = VERIFY_QUOTES.build_report(target, SimpleNamespace(slug=["shape"]))

        empty = CONTROLLER.empty_quote_verification_report()

        self.assertEqual(list(real), list(empty))
        self.assertEqual(list(real["counts"]), list(empty["counts"]))
        self.assertEqual(list(real["counts"]["by_form"]), list(empty["counts"]["by_form"]))
        self.assertEqual({"quote": 0, "anchor": 0}, empty["counts"]["by_form"])
        self.assertEqual(0, sum(empty["counts"][key] for key in empty["counts"] if key != "by_form"))
        self.assertEqual([], empty["questions"])
        self.assertEqual("verified", empty["overall_result"])
        self.assertFalse(empty["network_io_executed"])

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

    def events(self, target: Path, orchestration_id: str) -> list[dict]:
        text = CONTROLLER.events_path(target, orchestration_id).read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def set_review_scope(self, target: Path, scope: str) -> None:
        config_path = target / "research.yml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        config.setdefault("review", {})["escalation_scope"] = scope
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    def park_question_for_review(self, target: Path, slug: str) -> None:
        """Record the frontmatter `question_resolve.py answer --require-coverage` writes when parking."""
        page = target / "wiki" / "questions" / f"{slug}.md"
        text = page.read_text(encoding="utf-8")
        self.assertIn("status: open", text)
        page.write_text(
            text.replace(
                "status: open",
                "status: human_review\n"
                "human_review_required: true\n"
                "human_review_status: pending\n"
                "human_review_policies:\n"
                "  - pack:fixture-pack/manual-check",
                1,
            ),
            encoding="utf-8",
        )

    def block_question(self, target: Path, slug: str = "test-question", priority: str = "high") -> str:
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
                priority,
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

    def append_selected_acquisition_candidates(
        self,
        target: Path,
        request_id: str,
        candidate_ids: list[str],
    ) -> None:
        config = DISCOVER.load_config(target)
        candidates = [
            {
                "schema_version": "1.0",
                "candidate_id": candidate_id,
                "request_id": request_id,
                "source_request_id": request_id,
                "selected_for_request_id": request_id,
                "selected_request_id": request_id,
                "provider": "arxiv",
                "discovery_providers": ["arxiv"],
                "source_type": "paper",
                "paper": {"provider_ids": {"arxiv": f"2601.{12345 + index:05d}v2"}},
                "lifecycle_schema_version": "2.0",
                "lifecycle_state": "selected",
                "status": "selected",
                "selection_status": "selected",
                "fetch_status": "planned",
                "selected_at": "2026-07-21T00:00:00Z",
                "selected_by": "agent-test",
                "selection_reason": "Selected as a test acquisition route.",
                "lifecycle_updated_at": "2026-07-21T00:00:00Z",
                "lifecycle_updated_by": "agent-test",
                "lifecycle_reason": "Selected as a test acquisition route.",
            }
            for index, candidate_id in enumerate(candidate_ids)
        ]
        written = DISCOVER.append_candidates(DISCOVER.candidate_store_path(target, config), candidates)
        self.assertEqual(candidate_ids, written)

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

    def test_network_free_external_protocol_replays_validates_and_advances(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.start(target)

            code, first, stderr = self.controller(
                target,
                "next",
                "--orchestration-id",
                "orch-test",
                "--agent-id",
                "agent-test",
            )
            self.assertEqual(0, code, stderr)
            code, replayed, stderr = self.controller(
                target,
                "next",
                "--orchestration-id",
                "orch-test",
                "--agent-id",
                "agent-test",
            )
            self.assertEqual(0, code, stderr)
            self.assertEqual(first, replayed)

            malformed_path = root / "malformed-result.json"
            malformed_path.write_text('{"schema_version": "1.0"}\n', encoding="utf-8")
            code, malformed, _ = self.controller(
                target,
                "submit",
                "--orchestration-id",
                "orch-test",
                "--action-id",
                first["action_id"],
                "--result-file",
                str(malformed_path),
                "--agent-id",
                "agent-test",
            )
            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("RESULT_INVALID", malformed["error_code"])

            code, unsafe, _ = self.submit(
                root,
                target,
                first["action_id"],
                artifacts=["../outside-workspace"],
            )
            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("RESULT_INVALID", unsafe["error_code"])
            self.assertFalse(
                CONTROLLER.work_result_path(target, "orch-test", first["action_id"]).exists()
            )

            self.block_question(target)
            code, accepted, stderr = self.submit(
                root,
                target,
                first["action_id"],
                summary="External harness completed the deterministic work order.",
            )
            self.assertEqual(0, code, stderr)
            self.assertEqual(1, accepted["completed_action_count"])
            self.assertEqual(first["action_id"], accepted["last_completed_action_id"])

            code, status, stderr = self.controller(
                target,
                "status",
                "--orchestration-id",
                "orch-test",
            )
            self.assertEqual(0, code, stderr)
            self.assertEqual(1, status["completed_action_count"])
            self.assertIsNone(status["pending_action_id"])
            self.assertNotEqual("research", status["phase"])

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

    def test_blocked_result_pauses_without_completing_and_resume_replays_same_action(self):
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
            self.assertEqual(CONTROLLER.EXIT_PAUSED, code)
            self.assertEqual("paused", session["status"])
            self.assertEqual(order["action_id"], session["pending_action_id"])
            self.assertEqual(0, session["completed_action_count"])
            self.assertIsNone(session["last_completed_action_id"])
            self.assertFalse(
                CONTROLLER.work_result_path(target, "orch-test", order["action_id"]).exists()
            )
            child = RUN_CONTROLLER.load_run_state(target, order["run_id"])
            self.assertEqual("answering", child["state"]["current"])

            code, still_paused, stderr = self.controller(
                target,
                "next",
                "--orchestration-id",
                "orch-test",
            )
            self.assertEqual(CONTROLLER.EXIT_PAUSED, code, stderr)
            self.assertEqual("paused", still_paused["status"])

            code, replayed, stderr = self.controller(
                target,
                "next",
                "--orchestration-id",
                "orch-test",
                "--resume",
            )
            self.assertEqual(0, code, stderr)
            self.assertEqual(order["action_id"], replayed["action_id"])
            self.assertEqual(order["run_id"], replayed["run_id"])

            duplicate = self.submit(
                root,
                target,
                order["action_id"],
                outcome="blocked",
                summary="No permitted evidence route remains.",
            )
            self.assertEqual(CONTROLLER.EXIT_PAUSED, duplicate[0])
            self.assertEqual("paused", duplicate[1]["status"])
            self.assertEqual(0, duplicate[1]["completed_action_count"])
            self.assertFalse(
                CONTROLLER.work_result_path(target, "orch-test", order["action_id"]).exists()
            )

    def test_blocked_acquisition_preserves_correlated_partial_delivery_for_same_action_replay(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.enable_academic_providers(target)
            request_id = self.block_question(target)
            candidate_id = "cand-retryable-pdf"
            self.append_selected_acquisition_candidates(target, request_id, [candidate_id])
            self.start(target)

            code, order, stderr = self.controller(
                target,
                "next",
                "--orchestration-id",
                "orch-test",
            )
            self.assertEqual(0, code, stderr)
            self.assertEqual("acquisition", order["phase"])
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
            self.assertEqual(1, len(self.manifest_records(target)))

            summary = "The configured PDF extractor dependency is temporarily unavailable."
            code, paused, stderr = self.submit(
                root,
                target,
                order["action_id"],
                outcome="blocked",
                summary=summary,
                artifacts=[
                    raw_relative,
                    f"{raw_relative}.provenance.yml",
                    "sources/manifest.jsonl",
                ],
            )
            self.assertEqual(CONTROLLER.EXIT_PAUSED, code, stderr)
            self.assertEqual("paused", paused["status"])
            self.assertEqual(summary, paused["pause_reason"])
            self.assertEqual(order["action_id"], paused["pending_action_id"])
            self.assertEqual(0, paused["completed_action_count"])
            self.assertIsNone(paused["last_completed_action_id"])
            self.assertFalse(
                CONTROLLER.work_result_path(target, "orch-test", order["action_id"]).exists()
            )
            self.assertEqual(
                "selected",
                CONTROLLER.candidate_state(CONTROLLER.load_candidates(target, CONTROLLER.load_config(target))[0]),
            )
            self.assertEqual("fetching", RUN_CONTROLLER.load_run_state(target, order["run_id"])["state"]["current"])

            code, replayed, stderr = self.controller(
                target,
                "next",
                "--orchestration-id",
                "orch-test",
                "--resume",
            )
            self.assertEqual(0, code, stderr)
            self.assertEqual(order["action_id"], replayed["action_id"])
            self.assertEqual([candidate_id], replayed["scope"]["candidate_ids"])
            self.assertEqual(1, len(self.manifest_records(target)))

    def test_blocked_failed_acquisition_route_completes_attempt_and_selects_next_candidate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.enable_academic_providers(target)
            request_id = self.block_question(target)
            candidate_ids = ["cand-first-route", "cand-second-route"]
            self.append_selected_acquisition_candidates(target, request_id, candidate_ids)
            self.start(target)
            _, first_order, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
            self.assertEqual("acquisition", first_order["phase"])
            self.assertEqual([candidate_ids[0]], first_order["scope"]["candidate_ids"])

            transition = self.assert_json_script_ok(
                DISCOVER,
                [
                    "--project-root",
                    str(target),
                    "--format",
                    "json",
                    "candidates",
                    "transition",
                    "--candidate-id",
                    candidate_ids[0],
                    "--expected-state",
                    "selected",
                    "--to-state",
                    "failed",
                    "--reason",
                    "The first provider route returned an unusable source artifact.",
                    "--actor",
                    "agent-test",
                    "--run-id",
                    first_order["run_id"],
                ],
            )
            self.assertTrue(transition["updated"])

            code, continued, stderr = self.submit(
                root,
                target,
                first_order["action_id"],
                outcome="blocked",
                summary="The first candidate-specific route failed.",
                artifacts=[
                    "sources/discovery/candidates.jsonl",
                    "sources/discovery/audit.jsonl",
                ],
            )
            self.assertEqual(0, code, stderr)
            self.assertEqual("active", continued["status"])
            self.assertEqual("planning", continued["phase"])
            self.assertEqual(1, continued["completed_action_count"])
            self.assertEqual(first_order["action_id"], continued["last_completed_action_id"])
            self.assertTrue(
                CONTROLLER.work_result_path(target, "orch-test", first_order["action_id"]).is_file()
            )
            self.assertEqual(
                "fetching",
                RUN_CONTROLLER.load_run_state(target, first_order["run_id"])["state"]["current"],
            )

            code, second_order, stderr = self.controller(
                target,
                "next",
                "--orchestration-id",
                "orch-test",
            )
            self.assertEqual(0, code, stderr)
            self.assertEqual("acquisition", second_order["phase"])
            self.assertEqual([candidate_ids[1]], second_order["scope"]["candidate_ids"])
            self.assertNotEqual(first_order["action_id"], second_order["action_id"])

    def test_blocked_acquisition_cannot_reuse_historical_candidate_failure_audit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.enable_academic_providers(target)
            request_id = self.block_question(target)
            candidate_id = "cand-historical-failure"
            self.append_selected_acquisition_candidates(target, request_id, [candidate_id])
            predicted_run_id = "run-orch-test-001"

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
                    "failed",
                    "--reason",
                    "Historical route failure.",
                    "--actor",
                    "agent-test",
                    "--run-id",
                    predicted_run_id,
                ],
            )
            failed_record = DISCOVER.load_all_candidates(
                DISCOVER.candidate_store_path(target, DISCOVER.load_config(target))
            )[0]
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
                    "failed",
                    "--to-state",
                    "selected",
                    "--request-id",
                    request_id,
                    "--reason",
                    "Retry the candidate in a later bounded action.",
                    "--actor",
                    "agent-test",
                    "--run-id",
                    predicted_run_id,
                ],
            )

            self.start(target)
            _, order, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
            self.assertEqual(predicted_run_id, order["run_id"])
            DISCOVER.rewrite_candidates(
                DISCOVER.candidate_store_path(target, DISCOVER.load_config(target)),
                [failed_record],
            )

            code, error, _ = self.submit(
                root,
                target,
                order["action_id"],
                outcome="blocked",
                summary="Attempted to reuse a historical candidate failure.",
                artifacts=["sources/discovery/candidates.jsonl"],
            )
            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("ORCHESTRATION_POSTCONDITION_FAILED", error["error_code"])
            self.assertIn("exactly one audit event", error["message"])
            self.assertFalse(
                CONTROLLER.work_result_path(target, "orch-test", order["action_id"]).exists()
            )

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

    def test_contention_is_read_off_the_error_by_shape_not_by_class(self):
        """The one check that decides between the two refusals reads an attribute.

        ``contended`` is a flag on the existing class rather than a subclass
        because workspace scripts load siblings by file path: several copies of
        ``_workspace_locks``, and therefore several distinct
        ``LockUnavailableError`` classes, coexist in one interpreter, and a
        subclass check across copies compiles, reads naturally, and never fires.

        The consequence worth pinning is the degradation: an error object that
        predates the flag -- what an older vendored copy of the lock module
        raises -- must fall back to "not contended" and take the pre-existing
        ``LOCK_UNAVAILABLE`` path, never crash on a missing attribute and never
        be guessed into the new one. Both directions are asserted here, so a
        refactor to ``exc.contended`` or to ``isinstance`` fails loudly.
        """
        legacy = CONTROLLER.LockUnavailableError("no backend is available")
        del legacy.contended
        self.assertFalse(hasattr(legacy, "contended"))
        contended = CONTROLLER.LockUnavailableError("a peer holds it", contended=True)

        cases = (
            (legacy, CONTROLLER.EXIT_INVALID, "LOCK_UNAVAILABLE"),
            (contended, CONTROLLER.EXIT_DRIVER_BUSY, "ORCHESTRATION_DRIVER_BUSY"),
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.start(target)
            for raised, expected_code, expected_error in cases:
                with self.subTest(error=expected_error):
                    with mock.patch.object(CONTROLLER, "workspace_lock", side_effect=raised):
                        code, error, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
                    self.assertEqual(expected_code, code)
                    self.assertEqual(expected_error, error["error_code"])

    def test_driver_busy_refusal_renders_without_any_readable_holder(self):
        """The refusal must survive knowing nothing about who it is refusing for.

        ``read_lock_holder`` reports ``None`` for a sidecar that is missing,
        truncated, or written by a crashed peer -- and the winner publishes its
        sidecar just *after* acquiring, so a loser refused inside that window
        legitimately sees nothing. A message that indexed the holder block would
        raise while explaining a failure, replacing a clean refusal with a
        traceback at precisely the moment a host needs to parse an envelope.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.start(target)
            contended = CONTROLLER.LockUnavailableError("a peer holds it", contended=True)
            with mock.patch.object(CONTROLLER, "workspace_lock", side_effect=contended), mock.patch.object(
                CONTROLLER, "read_lock_holder", return_value=None
            ):
                code, error, _ = self.controller(target, "next", "--orchestration-id", "orch-test")

            self.assertEqual(CONTROLLER.EXIT_DRIVER_BUSY, code)
            self.assertEqual("ORCHESTRATION_DRIVER_BUSY", error["error_code"])
            self.assertIsNone(error["details"]["holder"])
            self.assertIn("<unrecorded agent>", error["message"])
            self.assertIn("orch-test", error["message"])

            # A sidecar that parses but carries nothing usable is the same story:
            # the fields fall back individually rather than all-or-nothing.
            for hostile in ({}, {"agent_id": None, "pid": "", "acquired_at": {}}):
                with self.subTest(holder=hostile):
                    with mock.patch.object(
                        CONTROLLER, "workspace_lock", side_effect=contended
                    ), mock.patch.object(CONTROLLER, "read_lock_holder", return_value=hostile):
                        code, error, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
                    self.assertEqual(CONTROLLER.EXIT_DRIVER_BUSY, code)
                    self.assertIn("<unrecorded agent>", error["message"])

            # And a hostile peer cannot turn a one-line refusal into a wall of text.
            shouting = {"agent_id": "A" * 5000, "pid": 7, "acquired_at": "2026-08-10T00:00:00Z"}
            with mock.patch.object(CONTROLLER, "workspace_lock", side_effect=contended), mock.patch.object(
                CONTROLLER, "read_lock_holder", return_value=shouting
            ):
                code, error, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
            self.assertEqual(CONTROLLER.EXIT_DRIVER_BUSY, code)
            self.assertLess(len(error["message"]), 400)

    def test_driver_busy_refusal_is_immediate_and_writes_nothing(self):
        """The default refuses now, and refusing leaves the session byte-identical.

        Both halves matter. The bounded wait this replaces is what made
        interleaved drivers silent, so "immediate" is the behaviour under test,
        not an implementation detail -- and it is measured in-process, where no
        interpreter start-up can hide a ten-second sleep. "Writes nothing" is the
        fail-closed rule the rest of this controller keeps: a refusal that had
        already appended an event would have made the session's own history lie
        about what happened.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.start(target)
            session_file = CONTROLLER.session_path(target, "orch-test")
            events_file = CONTROLLER.events_path(target, "orch-test")
            before_session = session_file.read_bytes()
            before_events = events_file.read_bytes()

            with LOCKS.workspace_lock(
                CONTROLLER.session_lock_path(target, "orch-test"),
                purpose="competing driver",
                holder={"agent_id": "competing-driver", "pid": 4242, "acquired_at": "2026-08-10T00:00:00Z"},
            ):
                started = time.monotonic()
                code, error, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
                elapsed = time.monotonic() - started

            self.assertEqual(CONTROLLER.EXIT_DRIVER_BUSY, code)
            self.assertEqual("ORCHESTRATION_DRIVER_BUSY", error["error_code"])
            self.assertTrue(error["recoverable"])
            self.assertIn("status polling never requires this lock", error["remediation"])
            self.assertEqual("competing-driver", error["details"]["holder"]["agent_id"])
            self.assertEqual(4242, error["details"]["holder"]["pid"])
            self.assertEqual("orch-test", error["details"]["orchestration_id"])
            self.assertLess(elapsed, 1.0, "the default must refuse immediately, not wait")
            self.assertEqual(before_session, session_file.read_bytes())
            self.assertEqual(before_events, events_file.read_bytes())

    def test_driver_wait_seconds_restores_queueing_for_hosts_that_ask(self):
        """The opt-in wait is the compatibility lever for hosts that liked queueing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir), question=True)
            self.start(target)
            lock_path = CONTROLLER.session_lock_path(target, "orch-test")
            acquired, released = threading.Event(), threading.Event()
            hold_seconds = 0.5

            def hold_briefly():
                with LOCKS.workspace_lock(lock_path, purpose="competing driver", holder={"pid": 1}):
                    acquired.set()
                    time.sleep(hold_seconds)
                released.set()

            holder = threading.Thread(target=hold_briefly, daemon=True)
            holder.start()
            self.addCleanup(holder.join, 30)
            # Waiting for the acquisition, rather than sleeping and hoping, is what
            # keeps the elapsed-time assertion below meaningful instead of flaky.
            self.assertTrue(acquired.wait(30), "the competing driver never acquired the session lock")

            started = time.monotonic()
            code, order, stderr = self.controller(
                target,
                "next",
                "--orchestration-id",
                "orch-test",
                "--driver-wait-seconds",
                "30",
            )
            elapsed = time.monotonic() - started

        self.assertEqual(0, code, stderr)
        self.assertIn("action_id", order)
        self.assertTrue(released.wait(30), "the holder never released")
        # It queued rather than refusing (the lower bound) and it did not sit out
        # the whole budget (the upper bound): the wait ends when the lock frees.
        self.assertGreater(elapsed, hold_seconds / 2)
        self.assertLess(elapsed, 30.0)

    def test_a_negative_driver_wait_is_refused_at_the_cli_boundary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            for bad in ("-1", "-0.5", "nan", "inf", "soon"):
                with self.subTest(value=bad):
                    with self.assertRaises(SystemExit) as caught:
                        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                            CONTROLLER.main(
                                [
                                    "--project-root",
                                    str(target),
                                    "next",
                                    "--orchestration-id",
                                    "orch-test",
                                    "--driver-wait-seconds",
                                    bad,
                                ]
                            )
                    self.assertEqual(2, caught.exception.code)
            self.assertEqual(0.0, CONTROLLER.parse_wait_seconds("0"))
            self.assertEqual(30.0, CONTROLLER.parse_wait_seconds("30"))

    def test_driver_identity_is_recorded_in_issuance_and_completion_events(self):
        """Post-hoc visibility: the events say which process wrote them.

        Not enforcement. Nothing reads this block back to decide whether a write
        is allowed -- span-level leases are deliberately future work. What it buys
        is that an interleaving which slipped past the per-invocation lock (two
        hosts taking turns under a long ``--driver-wait-seconds``) is legible in
        ``events.jsonl`` afterwards instead of reconstructed from timestamps.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.start(target)
            _, order, stderr = self.controller(
                target,
                "next",
                "--orchestration-id",
                "orch-test",
                "--agent-id",
                "agent-test",
            )
            self.assertIn("action_id", order, stderr)
            self.block_question(target)
            code, _, stderr = self.submit(root, target, order["action_id"])
            self.assertEqual(0, code, stderr)

            events = self.events(target, "orch-test")
            issued = next(event for event in events if event["event_type"] == "action_issued")
            completed = next(event for event in events if event["event_type"] == "action_completed")
            for event in (issued, completed):
                with self.subTest(event=event["event_type"]):
                    driver = event["data"]["driver"]
                    self.assertEqual(os.getpid(), driver["pid"])
                    self.assertTrue(driver["hostname"])
                    self.assertEqual("agent-test", driver["agent_id"])
            # The completion event keeps the payload it always carried.
            self.assertEqual("completed", completed["data"]["outcome"])

            # `next` replays `repair_last_completion_events`; a repair must not
            # rewrite an event that already exists, driver block included. The
            # whole existing prefix is compared, not just the one event, because
            # a repair that appended a duplicate would still leave the original
            # intact and pass a narrower check.
            self.controller(target, "next", "--orchestration-id", "orch-test", "--agent-id", "agent-test")
            after_events = self.events(target, "orch-test")
            self.assertEqual(events, after_events[: len(events)])
            self.assertEqual(
                1,
                sum(event["event_type"] == "action_completed" for event in after_events),
            )

    def test_events_written_outside_a_driver_lock_are_unchanged(self):
        """A direct `record_event` still produces exactly the event it always did."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            session = self.start(target)
            self.assertIsNone(CONTROLLER.event_driver_block())
            CONTROLLER.record_event(target, session, "action_issued", "Direct call.", action_id="action-0001")
            direct = self.events(target, "orch-test")[-1]
            self.assertEqual({}, direct["data"])

    # -- real-process driver contention ------------------------------------------------
    #
    # The refusal exists for two *hosts*, so the proof has to be two processes. An
    # in-process test shares one interpreter, one lock table, and one pid, which is
    # exactly the situation the lock is not needed for.

    def spawn_holding_driver(
        self,
        target: Path,
        scratch: Path,
        *,
        orchestration_id: str = "orch-test",
        agent_id: str = "holding-driver",
    ) -> tuple[subprocess.Popen, int, Path]:
        """Start a second driver process and block until it truly holds the lock."""
        ready = scratch / f"{orchestration_id}-ready"
        release = scratch / f"{orchestration_id}-release"
        process = subprocess.Popen(  # noqa: S603
            [
                sys.executable,
                "-B",
                "-c",
                HOLDING_DRIVER,
                str(target / "scripts"),
                str(target),
                orchestration_id,
                str(ready),
                str(release),
                agent_id,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        def stop() -> None:
            """Let the holder go, tolerating a workspace that is already gone.

            Registered as a cleanup so no test can leak a process that holds a
            lock for two minutes, and written to survive running *after* the
            temporary directory it signals through has been removed.
            """
            with contextlib.suppress(OSError):
                release.touch()
            try:
                process.wait(30)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                process.kill()
                process.wait(30)

        self.addCleanup(stop)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if ready.is_file():
                return process, int(ready.read_text(encoding="utf-8")), release
            if process.poll() is not None:
                self.fail(f"the holding driver exited early: {process.communicate()[1]}")
            time.sleep(0.02)
        self.fail("the holding driver never acquired the session lock")

    def run_controller_process(self, target: Path, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
        return subprocess.run(  # noqa: S603
            [
                sys.executable,
                "-B",
                str(target / "scripts" / "orchestration_controller.py"),
                "--project-root",
                str(target),
                *args,
                "--format",
                "json",
            ],
            cwd=str(target),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )

    def test_a_second_driver_process_is_refused_and_names_the_winner(self):
        """Two OS processes, one session: the loser exits 6 and says whose pid won.

        This is CR-8's whole point stated as an experiment. Before it, the second
        process waited ten seconds and then proceeded, interleaving its writes
        with the first driver's; the corruption surfaced later, somewhere else,
        with nothing tying it back to the moment two hosts overlapped.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.start(target)
            session_file = CONTROLLER.session_path(target, "orch-test")
            events_file = CONTROLLER.events_path(target, "orch-test")

            holder, holder_pid, release = self.spawn_holding_driver(target, root)
            before_session = session_file.read_bytes()
            before_events = events_file.read_bytes()

            refused = self.run_controller_process(target, "next", "--orchestration-id", "orch-test")

            self.assertEqual(CONTROLLER.EXIT_DRIVER_BUSY, refused.returncode, refused.stderr)
            # Stdout purity: the envelope belongs on stderr, and stdout stays empty
            # so a host parsing reports never sees a refusal interleaved with one.
            self.assertEqual("", refused.stdout.strip())
            envelope = json.loads(refused.stderr)
            self.assertEqual("ORCHESTRATION_DRIVER_BUSY", envelope["error_code"])
            self.assertTrue(envelope["recoverable"])
            self.assertEqual(holder_pid, envelope["details"]["holder"]["pid"])
            self.assertEqual("holding-driver", envelope["details"]["holder"]["agent_id"])
            self.assertEqual("next", envelope["details"]["holder"]["command"])
            self.assertIn(str(holder_pid), envelope["message"])
            self.assertEqual(before_session, session_file.read_bytes())
            self.assertEqual(before_events, events_file.read_bytes())

            # The winner is unobstructed, and once it lets go a successor proceeds.
            release.touch()
            self.assertEqual(0, holder.wait(60), holder.communicate()[1])
            proceeded = self.run_controller_process(target, "next", "--orchestration-id", "orch-test")
            self.assertEqual(0, proceeded.returncode, proceeded.stderr)
            self.assertIn("action_id", json.loads(proceeded.stdout))

    def test_status_is_never_blocked_by_a_held_driver_lock(self):
        """Polling has to stay free, or a busy session becomes an unobservable one."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            self.start(target)
            holder, _, release = self.spawn_holding_driver(target, root)

            started = time.monotonic()
            polled = self.run_controller_process(target, "status", "--orchestration-id", "orch-test")
            elapsed = time.monotonic() - started

            # Released inside the temporary directory rather than by the
            # registered cleanup, which runs after it: Windows cannot delete a
            # file another process still holds open, so a holder outliving the
            # tree fails the teardown rather than the assertion.
            release.touch()
            holder.wait(60)

        self.assertEqual(0, polled.returncode, polled.stderr)
        self.assertEqual("orch-test", json.loads(polled.stdout)["orchestration_id"])
        self.assertLess(elapsed, 30.0)

    def test_a_driver_killed_mid_call_leaves_no_stale_refusal(self):
        """SIGKILL is the crash the OS can tell us about; the successor must proceed.

        Scoped to the native advisory backends on purpose. Only they learn of a
        holder's death from the kernel. The exclusive-create fallback has no such
        notification and recovers on a timer instead -- shortened to
        ``DRIVER_LOCK_STALE_FALLBACK_SECONDS`` for this lock, but still a wait,
        and covered by the lock module's own tests rather than by a two-minute
        sleep here.
        """
        if not LOCKS.multiprocess_lock_supported():
            self.skipTest("no native advisory lock backend on this platform")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.start(target)
            holder, _, _ = self.spawn_holding_driver(target, root)

            # ``Popen.kill`` rather than ``os.kill(pid, SIGKILL)``: Windows has no
            # SIGKILL, and this is the portable spelling of the same
            # uncatchable-termination the test is about (TerminateProcess there).
            holder.kill()
            holder.wait(60)
            # The crashed driver's sidecar is still on disk; a successor must not
            # mistake that leftover for a live owner.
            self.assertTrue(LOCKS.lock_holder_path(CONTROLLER.session_lock_path(target, "orch-test")).exists())

            proceeded = self.run_controller_process(target, "next", "--orchestration-id", "orch-test")

        self.assertEqual(0, proceeded.returncode, proceeded.stderr)
        self.assertIn("action_id", json.loads(proceeded.stdout))

    def test_racing_starts_on_one_id_leave_exactly_one_session(self):
        """Either refusal is truthful; what must never happen is two sessions.

        A loser that arrived after the winner committed sees
        ``ORCHESTRATION_EXISTS``; one that arrived during sees
        ``ORCHESTRATION_DRIVER_BUSY``. Both are final and carry the same
        remediation, so this asserts membership in that pair rather than a single
        code -- pinning one would make the test flaky by construction.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            racers = [
                subprocess.Popen(  # noqa: S603
                    [
                        sys.executable,
                        "-B",
                        str(target / "scripts" / "orchestration_controller.py"),
                        "--project-root",
                        str(target),
                        "start",
                        "--orchestration-id",
                        "orch-race",
                        "--agent-id",
                        f"racer-{index}",
                        "--format",
                        "json",
                    ],
                    cwd=str(target),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for index in range(2)
            ]
            # Drained before the return code is read: a racer that filled a pipe
            # while the test waited on it would deadlock rather than fail.
            outcomes = []
            for process in racers:
                stdout, stderr = process.communicate(timeout=120)
                outcomes.append((process.returncode, stdout, stderr))

            winners = [outcome for outcome in outcomes if outcome[0] == 0]
            losers = [outcome for outcome in outcomes if outcome[0] != 0]
            self.assertEqual(1, len(winners), outcomes)
            for code, _, stderr in losers:
                self.assertTrue(stderr.strip().startswith("{"), f"the loser wrote no envelope: {stderr}")
                self.assertIn(
                    json.loads(stderr)["error_code"],
                    {"ORCHESTRATION_EXISTS", "ORCHESTRATION_DRIVER_BUSY"},
                    stderr,
                )
                self.assertIn(code, {CONTROLLER.EXIT_INVALID, CONTROLLER.EXIT_DRIVER_BUSY})
            self.assertEqual(
                ["orch-race"],
                sorted(path.name for path in (target / "runs" / "orchestrations").iterdir()),
            )

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

    def parked_review_workspace(self, root: Path, scope: str) -> Path:
        """Two questions, one parked in human_review, under the requested escalation scope."""
        target = self.init_workspace(root, question=True)
        self.add_questions(
            root,
            target,
            [
                {
                    "id": "parked-question",
                    "question": "Which recorded review clears this parked question?",
                    "priority": "high",
                }
            ],
        )
        self.set_review_scope(target, scope)
        self.park_question_for_review(target, "parked-question")
        return target

    def test_question_scope_issues_work_for_the_unparked_question(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.parked_review_workspace(root, "question")
            self.start(target)

            code, order, stderr = self.controller(target, "next", "--orchestration-id", "orch-test")

            self.assertEqual(0, code, stderr)
            self.assertEqual("orchestration_work_order", order["artifact_type"])
            self.assertEqual("research", order["phase"])
            self.assertEqual(["test-question"], order["scope"]["question_slugs"])

            self.block_question(target, "test-question")
            code, accepted, stderr = self.submit(
                root,
                target,
                order["action_id"],
                summary="Created a scoped source request and durably blocked the question on it.",
                artifacts=["sources/source-requests.jsonl", "wiki/questions/test-question.md"],
            )

            self.assertEqual(0, code, stderr)
            self.assertEqual("active", accepted["status"])

    def test_question_scope_runtime_guards_accept_a_review_parked_mid_action(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.parked_review_workspace(root, "question")
            self.start(target)
            _, order, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
            session = CONTROLLER.load_session(target, "orch-test")
            retained = json.loads(
                CONTROLLER.work_order_path(target, "orch-test", order["action_id"]).read_text(encoding="utf-8")
            )

            status = CONTROLLER.verify_runtime_guards(target, session, retained)

            self.assertEqual("in_progress", status["readiness"]["verdict"])
            self.assertEqual(1, status["readiness"]["questions_awaiting_review"])

            # The guard passed, so submission proceeds to result validation instead of refusing
            # the workspace outright.
            code, error, _ = self.submit(
                root,
                target,
                order["action_id"],
                summary="The worker returned without processing its scoped question.",
            )
            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("ORCHESTRATION_POSTCONDITION_FAILED", error["error_code"])

    def test_question_scope_terminates_no_ship_when_every_question_awaits_review(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.parked_review_workspace(root, "question")
            self.park_question_for_review(target, "test-question")
            self.start(target)

            code, finished, stderr = self.controller(target, "next", "--orchestration-id", "orch-test")

            # A terminal no_ship session is reported, not raised: the document is the payload.
            self.assertEqual(CONTROLLER.EXIT_INVALID, code, stderr)
            self.assertEqual("no_ship", finished["status"])
            self.assertEqual("no_ship", finished["verdict"])
            self.assertTrue(finished["pause_reason"].startswith(CONTROLLER.AWAITING_REVIEW_TERMINAL_REASON))
            self.assertIn("parked-question", finished["pause_reason"])

            events = self.events(target, "orch-test")
            finished_event = [event for event in events if event["event_type"] == "session_finished"][-1]
            self.assertEqual(2, finished_event["data"]["questions_awaiting_review"])
            self.assertEqual(
                ["parked-question", "test-question"],
                sorted(finished_event["data"]["question_slugs"]),
            )

    def test_workspace_scope_keeps_the_0_2_4_freeze(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.parked_review_workspace(root, "workspace")
            self.start(target)

            code, finished, stderr = self.controller(target, "next", "--orchestration-id", "orch-test")

            self.assertEqual(CONTROLLER.EXIT_INVALID, code, stderr)
            self.assertEqual("no_ship", finished["status"])
            self.assertEqual(
                "Workspace health or HIGH validation findings require operator attention.",
                finished["pause_reason"],
            )
            self.assertFalse(finished["pause_reason"].startswith(CONTROLLER.AWAITING_REVIEW_TERMINAL_REASON))

    def test_workspace_scope_refuses_submit_after_a_review_is_parked_mid_action(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.add_questions(
                root,
                target,
                [{"id": "parked-question", "question": "Which review clears this?", "priority": "high"}],
            )
            self.start(target)
            _, order, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
            self.park_question_for_review(target, "parked-question")

            code, error, _ = self.submit(root, target, order["action_id"])

            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("ORCHESTRATION_WORKSPACE_HEALTH_CHANGED", error["error_code"])
            self.assertEqual("attention_required", error["details"]["readiness_verdict"])
            self.assertFalse(CONTROLLER.work_result_path(target, "orch-test", order["action_id"]).exists())

    def test_review_scope_flip_during_a_pending_action_is_refused_as_static_input_drift(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.parked_review_workspace(root, "question")
            self.start(target)
            _, order, _ = self.controller(target, "next", "--orchestration-id", "orch-test")

            self.set_review_scope(target, "workspace")

            code, error, _ = self.submit(root, target, order["action_id"])

            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("ORCHESTRATION_TRUSTED_INPUT_CHANGED", error["error_code"])
            self.assertTrue(
                any(path.startswith("research.yml ") for path in error["details"]["changed_paths"]),
                error["details"]["changed_paths"],
            )
            self.assertFalse(CONTROLLER.work_result_path(target, "orch-test", order["action_id"]).exists())

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

    def test_normalized_acquisition_quality_rejects_unusable_or_empty_pdf_records(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir)
            path = target / "sources" / "normalized" / "paper.md"
            path.parent.mkdir(parents=True)
            record = {"id": "paper:test", "kind": "pdf", "raw_pdf": "raw/papers/test.pdf"}

            def write_record(*, status: str, evidence_usable: str, extracted_text: str) -> None:
                path.write_text(
                    "---\n"
                    "type: normalized_source\n"
                    "source_id: paper:test\n"
                    "source_kind: pdf\n"
                    f"status: {status}\n"
                    f"evidence_usable: {evidence_usable}\n"
                    "extraction_method: pdf_text\n"
                    "---\n\n"
                    "# Test paper\n\n"
                    "## Extracted Text\n\n"
                    f"{extracted_text}\n",
                    encoding="utf-8",
                )

            for unusable_status in ("failed", "stubbed"):
                with self.subTest(status=unusable_status):
                    write_record(
                        status=unusable_status,
                        evidence_usable="true",
                        extracted_text="Extracted research content.",
                    )
                    failure = CONTROLLER.normalized_source_quality_failure(target, path, record)
                    self.assertIn("unusable extraction status", failure["reason"])

            write_record(
                status="content_extracted",
                evidence_usable="false",
                extracted_text="An official error page was extracted.",
            )
            failure = CONTROLLER.normalized_source_quality_failure(target, path, record)
            self.assertIn("not explicitly marked usable", failure["reason"])

            write_record(
                status="partial",
                evidence_usable="true",
                extracted_text="None extracted.",
            )
            failure = CONTROLLER.normalized_source_quality_failure(target, path, record)
            self.assertIn("contains no extracted text", failure["reason"])

            write_record(
                status="content_extracted",
                evidence_usable="true",
                extracted_text="Extracted research content.",
            )
            self.assertIsNone(CONTROLLER.normalized_source_quality_failure(target, path, record))

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
            normalized_path.write_text(
                "---\n"
                "type: normalized_source\n"
                f"source_id: {source_id}\n"
                "source_kind: html\n"
                "status: content_extracted\n"
                "evidence_usable: true\n"
                "---\n\n"
                "# Existing evidence\n\n"
                "Normalized evidence.\n",
                encoding="utf-8",
            )
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
                    "candidate_audit_record_fingerprints_before": {},
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

            # Wiring site three. The provider arm validates the reuse baseline it consumes
            # on the same terms as the delegated arm: an id the manifest baseline does not
            # name is checked by nothing downstream, and an id already in the scoped-match
            # map would leave which reuse terms apply to lookup order. The replay guard
            # answers first in production, so it is stood down here: each verifier
            # validates every baseline it consumes, and a check that only ever runs
            # behind another one is a check nobody would notice losing.
            for tampered, why in (
                (["raw:never-in-this-manifest"], "names a record outside the manifest baseline"),
                ([source_id], "is already a scoped match"),
            ):
                manifest_guard["reusable_source_ids_before"] = tampered
                with (
                    self.subTest(why=why),
                    mock.patch.object(
                        CONTROLLER, "require_action_baselines", side_effect=lambda order, _root: order
                    ),
                    self.assertRaisesRegex(
                        CONTROLLER.OrchestrationControllerError,
                        "bounded evidence integrity baseline",
                    ),
                ):
                    verify(reopened)
            manifest_guard["reusable_source_ids_before"] = []

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

    # -- delegated acquisition: declaration captured at start, drift refused after ----

    def declare_delegation(self, target: Path, **section) -> None:
        """Write a research.yml orchestration: section, defaulting to a valid delegation."""
        declaration = {"acquisition": "delegated", "acquirer_agent_id": "acquirer-1"}
        declaration.update(section)
        config_path = target / "research.yml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if declaration:
            config["orchestration"] = declaration
        else:
            config.pop("orchestration", None)
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    def test_start_captures_the_declared_acquisition_posture(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.declare_delegation(target, max_attempts_per_request=4)

            session = self.start(target)

            self.assertEqual("delegated", session["acquisition_mode"])
            self.assertEqual("acquirer-1", session["acquirer_agent_id"])
            self.assertEqual(4, session["max_attempts_per_request"])
            # Delegation is not a provider grant: the session's authorization is unchanged.
            self.assertEqual(
                {"enabled": False, "providers": []},
                session["provider_policy"]["acquisition"],
            )
            self.assertEqual(session, CONTROLLER.load_session(target, "orch-test"))

    def test_start_records_providers_mode_explicitly(self):
        # Written rather than left absent, so a session created now is distinguishable
        # from one created before delegation existed.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)

            session = self.start(target)

            self.assertEqual("providers", session["acquisition_mode"])
            self.assertIsNone(session["acquirer_agent_id"])
            self.assertEqual(
                CONTROLLER.DEFAULT_MAX_ATTEMPTS_PER_REQUEST,
                session["max_attempts_per_request"],
            )

    def test_start_refuses_delegation_alongside_enabled_acquisition_providers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.enable_academic_providers(target)
            self.declare_delegation(target)

            code, error, _ = self.controller(
                target, "start", "--orchestration-id", "orch-test", "--agent-id", "agent-test"
            )

            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("CONFIG_INVALID", error["error_code"])
            self.assertIn("exactly one of them acquires evidence", error["message"])
            # No session document: the contradiction is caught before durable state exists.
            self.assertFalse(CONTROLLER.session_path(target, "orch-test").exists())

    def test_start_refuses_a_malformed_declaration_through_the_config_error_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.declare_delegation(target, acquirer_agent_id=None)

            code, error, _ = self.controller(
                target, "start", "--orchestration-id", "orch-test", "--agent-id", "agent-test"
            )

            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("CONFIG_INVALID", error["error_code"])
            self.assertIn("acquirer_agent_id is required", error["message"])
            self.assertFalse(CONTROLLER.session_path(target, "orch-test").exists())

    def test_changing_the_acquirer_between_actions_is_refused(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.declare_delegation(target)
            self.start(target)
            self.declare_delegation(target, acquirer_agent_id="someone-else")

            code, error, _ = self.controller(target, "next", "--orchestration-id", "orch-test")

            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("ORCHESTRATION_DELEGATION_CHANGED", error["error_code"])
            self.assertEqual(
                {"expected": "acquirer-1", "current": "someone-else"},
                error["details"]["changed"]["acquirer_agent_id"],
            )
            self.assertFalse(error["recoverable"])

    def test_turning_delegation_on_under_a_running_session_is_refused(self):
        # The session was planned in providers mode; a mid-flight switch would change
        # which work orders it may issue and who may execute them.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.start(target)
            self.declare_delegation(target)

            code, error, _ = self.controller(target, "next", "--orchestration-id", "orch-test")

            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("ORCHESTRATION_DELEGATION_CHANGED", error["error_code"])
            self.assertEqual(
                {"expected": "providers", "current": "delegated"},
                error["details"]["changed"]["acquisition_mode"],
            )

    def test_turning_delegation_off_under_a_running_session_is_refused(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.declare_delegation(target)
            self.start(target)
            config_path = target / "research.yml"
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config.pop("orchestration")
            config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

            code, error, _ = self.controller(target, "next", "--orchestration-id", "orch-test")

            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("ORCHESTRATION_DELEGATION_CHANGED", error["error_code"])

    def test_an_unchanged_declaration_does_not_refuse(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.declare_delegation(target)
            self.start(target)

            code, payload, stderr = self.controller(target, "next", "--orchestration-id", "orch-test")

            self.assertEqual(0, code, stderr)
            # A2 does not route delegated acquisition yet (that is C1); what matters here
            # is that the guard let the session proceed on its own declaration.
            self.assertNotEqual("ORCHESTRATION_DELEGATION_CHANGED", payload.get("error_code"))

    def test_changing_the_attempts_budget_between_actions_is_allowed(self):
        # The budget bounds retries; unlike who acquires, changing it cannot make a
        # pending order unexecutable, and the router reads the session's frozen value.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.declare_delegation(target, max_attempts_per_request=2)
            self.start(target)
            self.declare_delegation(target, max_attempts_per_request=5)

            code, payload, stderr = self.controller(target, "next", "--orchestration-id", "orch-test")

            self.assertEqual(0, code, stderr)
            self.assertNotEqual("ORCHESTRATION_DELEGATION_CHANGED", payload.get("error_code"))
            self.assertEqual(2, CONTROLLER.load_session(target, "orch-test")["max_attempts_per_request"])

    def test_a_session_predating_delegation_still_loads_and_runs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.start(target)
            session_path = CONTROLLER.session_path(target, "orch-test")
            session = json.loads(session_path.read_text(encoding="utf-8"))
            for field in ("acquisition_mode", "acquirer_agent_id", "max_attempts_per_request"):
                session.pop(field)
            session_path.write_text(json.dumps(session, indent=2) + "\n", encoding="utf-8")

            loaded = CONTROLLER.load_session(target, "orch-test")
            self.assertNotIn("acquisition_mode", loaded)
            self.assertEqual(
                "providers",
                CONTROLLER.session_acquisition_policy(loaded)["acquisition_mode"],
            )

            code, payload, stderr = self.controller(target, "next", "--orchestration-id", "orch-test")
            self.assertEqual(0, code, stderr)
            self.assertNotEqual("ORCHESTRATION_DELEGATION_CHANGED", payload.get("error_code"))

    def test_a_delegated_session_without_an_acquirer_is_an_invalid_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.declare_delegation(target)
            self.start(target)
            session_path = CONTROLLER.session_path(target, "orch-test")
            session = json.loads(session_path.read_text(encoding="utf-8"))
            session["acquirer_agent_id"] = None
            session_path.write_text(json.dumps(session, indent=2) + "\n", encoding="utf-8")

            with self.assertRaises(CONTROLLER.OrchestrationControllerError) as caught:
                CONTROLLER.load_session(target, "orch-test")
            self.assertEqual("ORCHESTRATION_STATE_INVALID", caught.exception.error_code)

    def test_delegation_drift_under_a_pending_action_is_refused_as_trusted_input_drift(self):
        # research.yml is a trusted static input, so under a pending action the earlier,
        # stricter guard answers first. Pinned so the layering stays visible: the
        # delegation guard is for the planning gaps, not a replacement for this.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.declare_delegation(target)
            self.start(target)
            _, order, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
            self.declare_delegation(target, acquirer_agent_id="someone-else")

            code, error, _ = self.controller(target, "next", "--orchestration-id", "orch-test")

            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("ORCHESTRATION_TRUSTED_INPUT_CHANGED", error["error_code"])
            self.assertEqual(order["action_id"], CONTROLLER.load_session(target, "orch-test")["pending_action_id"])

    def test_legacy_pending_work_refuses_a_changed_acquirer_on_replay(self):
        # A session predating trusted-input binding has no fingerprint to compare, so the
        # replay path is where the delegation guard actually earns its call site.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.declare_delegation(target)
            self.start(target)
            _, order, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
            session = CONTROLLER.load_session(target, "orch-test")
            session.pop("pending_trusted_static_inputs")
            CONTROLLER.write_json_atomic(CONTROLLER.session_path(target, "orch-test"), session)
            CONTROLLER.trusted_static_input_path(target, "orch-test", order["action_id"]).unlink()
            self.declare_delegation(target, acquirer_agent_id="someone-else")

            code, error, _ = self.controller(target, "next", "--orchestration-id", "orch-test")

            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("ORCHESTRATION_DELEGATION_CHANGED", error["error_code"])
            self.assertEqual(order["action_id"], CONTROLLER.load_session(target, "orch-test")["pending_action_id"])

    # -- delegated acquisition: routing under blocked_on_sources ----------------------

    def delegated_session(self, target: Path, orchestration_id: str = "orch-test", **section) -> dict:
        self.declare_delegation(target, **section)
        return self.start(target, orchestration_id)

    def record_attempt(self, target: Path, request_id: str, *, code: str, session: str, action: str) -> None:
        self.assert_json_script_ok(
            SOURCE_REQUESTS,
            [
                "--project-root", str(target),
                "record-attempt-failure",
                "--request-id", request_id,
                "--failure-code", code,
                "--orchestration-id", session,
                "--action-id", action,
                "--format", "json",
            ],
        )

    def route_for(self, target: Path, orchestration_id: str = "orch-test") -> tuple:
        session = CONTROLLER.load_session(target, orchestration_id)
        return CONTROLLER.choose_route(target, session)

    def test_delegated_routing_issues_an_acquisition_order_without_candidates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            request_id = self.block_question(target)
            self.delegated_session(target)

            route, context = self.route_for(target)

            self.assertEqual("acquisition", route)
            self.assertTrue(context["delegated"])
            self.assertEqual("acquirer-1", context["acquirer_agent_id"])
            self.assertEqual([request_id], context["scope"]["request_ids"])
            self.assertEqual(["test-question"], context["scope"]["question_slugs"])
            self.assertEqual([], context["scope"]["candidate_ids"])

    def test_one_order_carries_every_routable_request(self):
        # Batched on purpose: a host with parallel connectors should not be forced through
        # one protocol round trip per request.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.add_questions(
                root,
                target,
                [{"id": "second-question", "question": "What else is missing?", "priority": "high"}],
            )
            first = self.block_question(target)
            second = self.block_question(target, slug="second-question")
            self.delegated_session(target)

            _, context = self.route_for(target)

            self.assertEqual({first, second}, set(context["scope"]["request_ids"]))
            self.assertEqual({"test-question", "second-question"}, set(context["scope"]["question_slugs"]))

    def test_a_request_at_its_attempt_budget_is_not_rerouted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            request_id = self.block_question(target)
            # Recorded before the session starts: once one is live, the delegation gate
            # refuses an attempt no pending work order scopes — which is D3's whole point.
            for index in range(2):
                self.record_attempt(
                    target, request_id, code="provider_throttled", session="orch-test", action=f"a{index}"
                )
            self.delegated_session(target)

            route, context = self.route_for(target)

            self.assertIsNone(route)
            self.assertEqual("blocked_on_sources", context["terminal_status"])
            # Spelled out rather than compared against the constant: the prefix is a
            # stable string hosts branch on, so asserting `startswith(CONTROLLER.CONST)`
            # would only prove the code uses its own value, whatever that value became.
            self.assertTrue(
                context["reason"].startswith(
                    "Delegated acquisition exhausted its attempts for every open source request"
                ),
                context["reason"],
            )
            self.assertEqual(
                "Delegated acquisition exhausted its attempts for every open source request",
                CONTROLLER.DELEGATED_EXHAUSTED_TERMINAL_REASON,
            )
            self.assertEqual(
                {"exhausted_requests": {request_id: "provider_throttled"}},
                context["event_data"],
            )

    def test_one_attempt_below_the_budget_still_routes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            request_id = self.block_question(target)
            self.record_attempt(target, request_id, code="provider_throttled", session="orch-test", action="a0")
            self.delegated_session(target)

            route, context = self.route_for(target)

            self.assertEqual("acquisition", route)
            self.assertEqual([request_id], context["scope"]["request_ids"])

    def test_a_standing_refusal_retires_a_request_on_its_first_attempt(self):
        # not_authorized will answer the same way next time, so spending the rest of the
        # budget proving that is waste the router can see coming.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            request_id = self.block_question(target)
            self.record_attempt(target, request_id, code="not_authorized", session="orch-test", action="a0")
            self.delegated_session(target)

            route, context = self.route_for(target)

            self.assertIsNone(route)
            self.assertEqual({"exhausted_requests": {request_id: "not_authorized"}}, context["event_data"])

    def test_a_configured_budget_replaces_the_default(self):
        # Two workspaces rather than one with a mid-test recording: with a session live the
        # delegation gate refuses an attempt no pending order scopes, so the history has to
        # exist before the session starts.
        for recorded, expected_route in ((2, "acquisition"), (3, None)):
            with self.subTest(recorded_attempts=recorded), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                target = self.init_workspace(root, question=True)
                request_id = self.block_question(target)
                for index in range(recorded):
                    self.record_attempt(
                        target, request_id, code="provider_throttled", session="orch-test", action=f"a{index}"
                    )
                self.delegated_session(target, max_attempts_per_request=3)

                route, _ = self.route_for(target)

                self.assertEqual(expected_route, route)

    def test_only_this_sessions_attempts_count(self):
        # The per-session rule: a new session gets a fresh look at every request, which is
        # the supported way to retry after fixing a host-side cause. The audit keeps the
        # earlier session's events; only the routing read is scoped.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            request_id = self.block_question(target)
            for index in range(2):
                self.record_attempt(
                    target, request_id, code="provider_throttled", session="orch-first", action=f"a{index}"
                )
            self.delegated_session(target, orchestration_id="orch-first")
            self.assertIsNone(self.route_for(target, "orch-first")[0])

            self.start(target, "orch-second")
            route, context = self.route_for(target, "orch-second")

            self.assertEqual("acquisition", route)
            self.assertEqual([request_id], context["scope"]["request_ids"])
            audit = target / "sources" / "source-request-attempts.jsonl"
            self.assertEqual(2, len(audit.read_text(encoding="utf-8").strip().splitlines()))

    def test_a_partially_exhausted_backlog_routes_only_what_remains(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.add_questions(
                root,
                target,
                [{"id": "second-question", "question": "What else is missing?", "priority": "high"}],
            )
            retired = self.block_question(target)
            live = self.block_question(target, slug="second-question")
            self.record_attempt(target, retired, code="not_authorized", session="orch-test", action="a0")
            self.delegated_session(target)

            route, context = self.route_for(target)

            self.assertEqual("acquisition", route)
            self.assertEqual([live], context["scope"]["request_ids"])
            self.assertEqual(["second-question"], context["scope"]["question_slugs"])

    def test_question_scope_is_built_from_the_scoped_requests_only(self):
        # Above the scope cap the order carries only the requests that fit, and its
        # question scope must be derived from those. The distinguishing case is a scoped
        # request with no linked question: deriving slugs from every open request instead
        # would put a dropped request's question in scope, authorizing the delegate to
        # mutate a question this order never scoped. (With one slug per request the two
        # spellings truncate identically, so a count-based assertion proves nothing.)
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            unlinked = self.assert_json_script_ok(
                SOURCE_REQUESTS,
                [
                    "--project-root", str(target),
                    "add", "--kind", "other",
                    "--query-or-identifier", "standing background evidence",
                    "--rationale", "Not linked to any question.",
                    "--priority", "high",
                    "--format", "json",
                ],
            )["request"]["request_id"]
            # Lower priority so the unlinked request sorts first deterministically.
            # Open requests tie-break on created_at then request_id, and a request id is a
            # hash over its creation timestamp — so two same-priority requests created in
            # the same second order unpredictably from run to run.
            self.block_question(target, priority="medium")
            self.delegated_session(target)

            # Patching the cap keeps the case deterministic; building 257 blocked
            # questions would test the same branch far more slowly.
            original_cap = CONTROLLER.MAX_SCOPE_IDS
            CONTROLLER.MAX_SCOPE_IDS = 1
            self.addCleanup(setattr, CONTROLLER, "MAX_SCOPE_IDS", original_cap)

            _, context = self.route_for(target)

            self.assertEqual([unlinked], context["scope"]["request_ids"])
            self.assertEqual(
                [],
                context["scope"]["question_slugs"],
                "the dropped request's question must not ride along in scope",
            )

    def test_delegated_orders_carry_no_provider_authority(self):
        # Delegation is not a provider grant. The order's policy must stay the workspace's
        # real (empty) one so verify_provider_policy_unchanged has nothing to widen.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.block_question(target)
            session = self.delegated_session(target)

            self.route_for(target)

            self.assertEqual(
                {"enabled": False, "providers": []},
                session["provider_policy"]["acquisition"],
            )

    def test_providers_mode_still_walks_candidates_and_discovery(self):
        # The delegated arm returns before the provider walk; this pins that the walk is
        # still reached when the session is not delegated.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.enable_academic_providers(target)
            self.block_question(target)
            self.start(target)

            route, context = self.route_for(target)

            self.assertEqual("discovery", route)
            self.assertNotIn("delegated", context)

    def test_a_corrupt_attempt_audit_refuses_rather_than_routing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.block_question(target)
            self.delegated_session(target)
            audit = target / "sources" / "source-request-attempts.jsonl"
            audit.write_text("{not json\n", encoding="utf-8")

            with self.assertRaises(CONTROLLER.OrchestrationControllerError) as caught:
                self.route_for(target)
            self.assertEqual("SOURCE_REQUESTS_INVALID", caught.exception.error_code)

    # -- delegated acquisition: the issued work order --------------------------------

    def issue_delegated_order(self, target: Path, orchestration_id: str = "orch-test") -> dict:
        code, order, stderr = self.controller(target, "next", "--orchestration-id", orchestration_id)
        self.assertEqual(0, code, stderr)
        return order

    def test_a_delegated_order_names_its_acquirer_and_skill(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            request_id = self.block_question(target)
            session = self.delegated_session(target)

            order = self.issue_delegated_order(target)

            self.assertEqual("acquisition", order["phase"])
            self.assertEqual("delegated", order["acquisition_mode"])
            self.assertEqual("acquirer-1", order["assigned_agent_id"])
            self.assertEqual("research-acquire-delegated", order["skill"])
            self.assertEqual([request_id], order["scope"]["request_ids"])
            self.assertEqual([], order["scope"]["candidate_ids"])
            # Addressed to the acquirer, still owned by the session driver: being the
            # addressee does not grant the right to drive the protocol.
            self.assertEqual(session["agent_id"], order["agent_id"])
            self.assertNotEqual(order["assigned_agent_id"], order["agent_id"])

    def test_a_delegated_order_carries_no_provider_authority(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.block_question(target)
            self.delegated_session(target)

            order = self.issue_delegated_order(target)

            self.assertEqual(
                {"discovery": {"enabled": False, "providers": []},
                 "acquisition": {"enabled": False, "providers": []}},
                order["provider_policy"],
            )

    def test_a_delegated_order_names_the_attempt_audit_and_not_the_candidate_store(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.block_question(target)
            self.delegated_session(target)

            order = self.issue_delegated_order(target)

            self.assertIn("sources/source-request-attempts.jsonl", order["inputs"])
            self.assertNotIn("sources/discovery/candidates.jsonl", order["inputs"])

    def test_a_delegated_order_keeps_the_provider_postcondition_check_names(self):
        # The verifier branches on acquisition_mode; the check names stay stable so the
        # published per-check schemas need no new shapes.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.block_question(target)
            self.delegated_session(target)

            order = self.issue_delegated_order(target)

            self.assertEqual(
                [
                    "request_fulfilled_with_normalized_source",
                    "linked_blocked_questions_reopened",
                    "manifest_records_increased",
                    "controller_integrity_baseline",
                ],
                [check["check"] for check in order["required_postconditions"]],
            )

    def test_a_delegated_order_baselines_the_attempt_audit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            request_id = self.block_question(target)
            self.record_attempt(
                target, request_id, code="provider_throttled", session="orch-test", action="pre-order"
            )
            self.delegated_session(target)

            order = self.issue_delegated_order(target)
            hydrated = self.hydrated_order(target, order)
            manifest_guard = next(
                check for check in hydrated["required_postconditions"]
                if check["check"] == "manifest_records_increased"
            )

            baseline = manifest_guard["request_attempt_audit_record_fingerprints_before"]
            self.assertEqual(1, len(baseline), "the attempt recorded before issue is in the baseline")
            # It travels in the protected sidecar, not the published order.
            published = next(
                check for check in order["required_postconditions"]
                if check["check"] == "manifest_records_increased"
            )
            self.assertNotIn("request_attempt_audit_record_fingerprints_before", published)

    def test_a_delegated_order_replays_without_losing_its_baseline(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.block_question(target)
            self.delegated_session(target)
            first = self.issue_delegated_order(target)

            second = self.issue_delegated_order(target)

            self.assertEqual(first["action_id"], second["action_id"])
            self.assertEqual(first, second)
            # The guard that refuses replaying an order without a trustworthy baseline.
            CONTROLLER.require_acquisition_evidence_baselines(self.hydrated_order(target, second))

    def test_a_pre_delegation_provider_order_still_replays(self):
        # Orders issued before delegated acquisition existed carry neither new field.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.enable_academic_providers(target)
            self.start(target)
            _, order, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
            order_path = CONTROLLER.work_order_path(target, "orch-test", order["action_id"])
            stored = json.loads(order_path.read_text(encoding="utf-8"))
            self.assertNotIn("acquisition_mode", stored)
            self.assertNotIn("assigned_agent_id", stored)

            code, replayed, stderr = self.controller(target, "next", "--orchestration-id", "orch-test")

            self.assertEqual(0, code, stderr)
            self.assertEqual(order["action_id"], replayed["action_id"])

    def test_a_delegated_order_stays_small_with_a_large_request_scope(self):
        # The baseline maps grow with the workspace; they belong in the protected sidecar,
        # not the 256 KiB published order.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.block_question(target)
            for index in range(60):
                self.assert_json_script_ok(
                    SOURCE_REQUESTS,
                    [
                        "--project-root", str(target),
                        "add", "--kind", "other",
                        "--query-or-identifier", f"bulk evidence request {index}",
                        "--rationale", "Bulk scope for the work-order size guard.",
                        "--format", "json",
                    ],
                )
            self.delegated_session(target)

            order = self.issue_delegated_order(target)

            self.assertEqual(61, len(order["scope"]["request_ids"]))
            encoded = len(json.dumps(order).encode("utf-8"))
            self.assertLess(encoded, CONTROLLER.MAX_WORK_ORDER_BYTES)
            self.assertLess(encoded, 32 * 1024, f"published order grew to {encoded} bytes")

    def test_a_delegated_order_can_reuse_evidence_delivered_before_it_was_issued(self):
        # Provider orders may reconcile an unchanged pre-existing scoped source rather than
        # re-fetching it. Delegated orders correlate by request alone, because there is no
        # candidate id to match; without that the baseline would always be empty and the
        # reuse path would silently disappear.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            request_id = self.block_question(target)
            links = target / "raw" / "links"
            links.mkdir(parents=True, exist_ok=True)
            (links / "quote.txt").write_text("https://example.org/quote\n", encoding="utf-8")
            (links / "quote.txt.provenance.yml").write_text(
                "origin_url: https://example.org/quote\n"
                "retrieved_at: 2026-08-08T00:00:00Z\n"
                "retrieved_by: acquirer-1\n"
                f"request_id: {request_id}\n",
                encoding="utf-8",
            )
            self.assert_json_script_ok(
                INVENTORY, ["--project-root", str(target), "--report", "--format", "json"]
            )
            self.assert_json_script_ok(
                NORMALIZE, ["--project-root", str(target), "--all", "--format", "json"]
            )
            self.delegated_session(target)

            order = self.issue_delegated_order(target)
            hydrated = self.hydrated_order(target, order)
            manifest_guard = next(
                check for check in hydrated["required_postconditions"]
                if check["check"] == "manifest_records_increased"
            )

            self.assertEqual(
                1,
                len(manifest_guard["matching_source_ids_before"]),
                "a source already delivered for the scoped request must be reconcilable",
            )

    def test_candidate_correlation_is_required_in_provider_mode_and_skipped_when_delegated(self):
        # `acquisition_reuse_baselines` takes an optional candidate scope for
        # delegated orders. Provider orders must keep correlating on the candidate: a
        # source belonging to a different candidate on the same request is not evidence
        # this order may reconcile against.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            request_id = self.block_question(target)
            links = target / "raw" / "links"
            links.mkdir(parents=True, exist_ok=True)
            (links / "quote.txt").write_text("https://example.org/quote\n", encoding="utf-8")
            (links / "quote.txt.provenance.yml").write_text(
                "origin_url: https://example.org/quote\n"
                "retrieved_at: 2026-08-08T00:00:00Z\n"
                "retrieved_by: fetcher\n"
                f"request_id: {request_id}\n"
                "candidate_id: cand-scoped\n",
                encoding="utf-8",
            )
            self.assert_json_script_ok(
                INVENTORY, ["--project-root", str(target), "--report", "--format", "json"]
            )
            self.assert_json_script_ok(
                NORMALIZE, ["--project-root", str(target), "--all", "--format", "json"]
            )
            config = CONTROLLER.load_config(target)

            matched, _, _ = CONTROLLER.acquisition_reuse_baselines(
                target, config, [request_id], ["cand-scoped"]
            )
            self.assertEqual(1, len(matched), "the scoped candidate's source is reconcilable")

            other, _, _ = CONTROLLER.acquisition_reuse_baselines(
                target, config, [request_id], ["cand-different"]
            )
            self.assertEqual({}, other, "another candidate's source is not in this order's baseline")

            delegated, _, _ = CONTROLLER.acquisition_reuse_baselines(target, config, [request_id], None)
            self.assertEqual(
                set(matched),
                set(delegated),
                "delegated orders correlate by request alone, since there is no candidate to name",
            )

    def test_a_provider_order_baselines_only_its_own_candidates_evidence(self):
        # The call-site half of the correlation rule. A provider acquisition order is
        # scoped to one selected candidate; a source delivered for a *different* candidate
        # on the same request must not be reconcilable against this order, or the
        # one-candidate-at-a-time boundary stops meaning anything.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.enable_academic_providers(target)
            request_id = self.block_question(target)
            self.append_selected_acquisition_candidates(target, request_id, ["cand-scoped"])
            links = target / "raw" / "links"
            links.mkdir(parents=True, exist_ok=True)
            (links / "other.txt").write_text("https://example.org/other\n", encoding="utf-8")
            (links / "other.txt.provenance.yml").write_text(
                "origin_url: https://example.org/other\n"
                "retrieved_at: 2026-08-08T00:00:00Z\n"
                "retrieved_by: fetcher\n"
                f"request_id: {request_id}\n"
                "candidate_id: cand-someone-else\n",
                encoding="utf-8",
            )
            self.assert_json_script_ok(
                INVENTORY, ["--project-root", str(target), "--report", "--format", "json"]
            )
            self.assert_json_script_ok(
                NORMALIZE, ["--project-root", str(target), "--all", "--format", "json"]
            )
            self.start(target)

            code, order, stderr = self.controller(target, "next", "--orchestration-id", "orch-test")
            self.assertEqual(0, code, stderr)
            self.assertEqual("acquisition", order["phase"])
            manifest_guard = next(
                check for check in self.hydrated_order(target, order)["required_postconditions"]
                if check["check"] == "manifest_records_increased"
            )

            self.assertEqual(
                [],
                manifest_guard["matching_source_ids_before"],
                "another candidate's delivered source is not this order's reconcilable evidence",
            )

    # -- delegated acquisition: completed-path verification --------------------------

    def deliver_for_request(
        self,
        target: Path,
        request_id: str,
        *,
        name: str = "supplier-quote",
        sidecar_request_id: str | None = "",
        with_sidecar: bool = True,
        normalize: bool = True,
    ) -> str:
        """Deliver one acquirer-style artifact with a provenance sidecar, no candidate.

        `normalize=False` stops after the inventory, which is where a workspace sits when a
        prior order delivered evidence and never got as far as normalizing it.
        """
        relative = f"raw/papers/{name}.html"
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"<html><head><title>{name} evidence</title></head>"
            "<body>The named supplier quotes 23.99 EUR per unit with a 50 unit minimum order.</body>"
            "</html>\n",
            encoding="utf-8",
        )
        if with_sidecar:
            sidecar = {
                "origin_url": f"https://supplier.example/{name}",
                "retrieved_at": "2026-08-08T00:00:00Z",
                "retrieved_by": "acquirer-1",
                "license": "CC-BY-4.0",
                "checksum": f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}",
            }
            stamped = request_id if sidecar_request_id == "" else sidecar_request_id
            if stamped is not None:
                sidecar["request_id"] = stamped
            (target / f"{relative}.provenance.yml").write_text(
                yaml.safe_dump(sidecar, sort_keys=False), encoding="utf-8"
            )
        self.assert_json_script_ok(
            INVENTORY, ["--project-root", str(target), "--report", "--format", "json"]
        )
        if normalize:
            self.assert_json_script_ok(
                NORMALIZE, ["--project-root", str(target), "--all", "--format", "json"]
            )
        manifest = (target / "sources" / "manifest.jsonl").read_text(encoding="utf-8")
        for line in manifest.splitlines():
            record = json.loads(line)
            if relative in record.get("raw_paths", []):
                return str(record["id"])
        raise AssertionError(f"no manifest record for {relative}")

    def fulfil_and_reopen(self, target: Path, request_id: str, source_id: str, slug: str | None) -> None:
        self.assert_json_script_ok(
            SOURCE_REQUESTS,
            [
                "--project-root", str(target), "fulfill",
                "--request-id", request_id, "--source-id", source_id, "--format", "json",
            ],
        )
        if slug is not None:
            self.assert_json_script_ok(
                RESOLVE,
                [
                    "--project-root", str(target), "reopen", "--slug", slug,
                    "--agent-id", "acquirer-1", "--source-id", source_id,
                    "--request-id", request_id, "--format", "json",
                ],
            )

    def submit_delegated(self, target: Path, action_id: str = "action-0001", **overrides) -> tuple:
        result = {
            "schema_version": "1.0",
            "action_id": action_id,
            "outcome": "completed",
            "summary": "Delegated acquisition finished its scoped requests.",
            "artifacts": [],
        }
        result.update(overrides)
        result_path = target / "delegated-result.json"
        result_path.write_text(json.dumps(result), encoding="utf-8")
        return self.controller(
            target,
            "submit",
            "--orchestration-id", "orch-test",
            "--action-id", action_id,
            "--result-file", str(result_path),
        )

    def delegated_action(self, root: Path) -> tuple[Path, str]:
        """A workspace with one blocked question and a pending delegated order."""
        target = self.init_workspace(root, question=True)
        request_id = self.block_question(target)
        self.delegated_session(target)
        code, order, stderr = self.controller(target, "next", "--orchestration-id", "orch-test")
        self.assertEqual(0, code, stderr)
        self.assertEqual("delegated", order["acquisition_mode"])
        return target, request_id

    def test_a_delegated_fulfilment_passes_and_returns_to_research(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target, request_id = self.delegated_action(Path(tmpdir))
            source_id = self.deliver_for_request(target, request_id)
            self.fulfil_and_reopen(target, request_id, source_id, "test-question")

            code, session, stderr = self.submit_delegated(target)

            self.assertEqual(0, code, stderr)
            self.assertEqual("research", session["phase"])
            self.assertEqual("action-0001", session["last_completed_action_id"])

    def test_a_fulfilment_without_a_provenance_sidecar_is_refused(self):
        # CR AC2: the refusal must name the missing artifact.
        with tempfile.TemporaryDirectory() as tmpdir:
            target, request_id = self.delegated_action(Path(tmpdir))
            source_id = self.deliver_for_request(target, request_id, with_sidecar=False)
            self.fulfil_and_reopen(target, request_id, source_id, "test-question")

            code, error, _ = self.submit_delegated(target)

            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("ORCHESTRATION_POSTCONDITION_FAILED", error["error_code"])
            self.assertIn("provenance sidecar", error["message"])
            self.assertEqual(
                [request_id],
                [item["request_id"] for item in error["details"]["correlation_failures"]],
            )

    def test_a_sidecar_naming_another_request_is_refused(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target, request_id = self.delegated_action(Path(tmpdir))
            source_id = self.deliver_for_request(target, request_id, sidecar_request_id="req-somewhere-else")
            self.fulfil_and_reopen(target, request_id, source_id, "test-question")

            code, error, _ = self.submit_delegated(target)

            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            failure = error["details"]["correlation_failures"][0]
            self.assertTrue(failure["has_provenance"])
            self.assertEqual("req-somewhere-else", failure["provenance_request_id"])

    def test_a_fulfilment_without_a_normalized_record_is_refused_by_source_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target, request_id = self.delegated_action(Path(tmpdir))
            source_id = self.deliver_for_request(target, request_id)
            self.fulfil_and_reopen(target, request_id, source_id, "test-question")
            for path in (target / "sources" / "normalized").glob("*.md"):
                path.unlink()

            code, error, _ = self.submit_delegated(target)

            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertIn("do not have normalized evidence", error["message"])
            self.assertEqual([source_id], error["details"]["source_ids"])

    def test_a_recorded_attempt_failure_completes_the_action_and_returns_to_planning(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target, request_id = self.delegated_action(Path(tmpdir))
            self.record_attempt(
                target, request_id, code="provider_throttled", session="orch-test", action="action-0001"
            )

            code, session, stderr = self.submit_delegated(target)

            self.assertEqual(0, code, stderr)
            self.assertEqual("planning", session["phase"])
            # The question stays blocked and the request stays open; nothing was invented.
            question = (target / "wiki" / "questions" / "test-question.md").read_text(encoding="utf-8")
            self.assertIn("status: blocked", question)

    def test_a_scoped_request_with_no_outcome_is_refused(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target, request_id = self.delegated_action(Path(tmpdir))

            code, error, _ = self.submit_delegated(target)

            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertIn("neither a fulfilment nor a recorded attempt failure", error["message"])
            self.assertEqual([request_id], error["details"]["request_ids"])

    def test_an_attempt_failure_for_another_action_does_not_account_for_a_request(self):
        # The event must name *this* action; a failure recorded under a different action id
        # is someone else's evidence.
        with tempfile.TemporaryDirectory() as tmpdir:
            target, request_id = self.delegated_action(Path(tmpdir))
            self.record_attempt(
                target, request_id, code="no_result", session="orch-test", action="action-9999"
            )

            code, error, _ = self.submit_delegated(target)

            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertIn("outside this action's request scope", error["message"])

    def test_rewriting_an_attempt_recorded_before_this_action_is_refused(self):
        # The append-only guarantee is about history. An event this action created and
        # then corrected is indistinguishable from one written correctly — both leave the
        # same durable artifact — so the protected set is what existed at issue time.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            request_id = self.block_question(target)
            self.record_attempt(
                target, request_id, code="provider_throttled", session="orch-test", action="earlier-action"
            )
            self.delegated_session(target)
            code, order, stderr = self.controller(target, "next", "--orchestration-id", "orch-test")
            self.assertEqual(0, code, stderr)

            audit = target / "sources" / "source-request-attempts.jsonl"
            event = json.loads(audit.read_text(encoding="utf-8").splitlines()[0])
            event["failure_code"] = "not_authorized"
            audit.write_text(json.dumps(event) + "\n", encoding="utf-8")
            self.record_attempt(
                target, request_id, code="no_result", session="orch-test", action=order["action_id"]
            )

            code, error, _ = self.submit_delegated(target, action_id=order["action_id"])

            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertIn("rewrote or removed recorded acquisition attempts", error["message"])

    def test_a_partial_batch_reopens_only_the_fully_unblocked_question(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.add_questions(
                root,
                target,
                [{"id": "second-question", "question": "What else is missing?", "priority": "high"}],
            )
            fulfilled_request = self.block_question(target)
            failed_request = self.block_question(target, slug="second-question")
            self.delegated_session(target)
            code, order, stderr = self.controller(target, "next", "--orchestration-id", "orch-test")
            self.assertEqual(0, code, stderr)
            self.assertEqual({fulfilled_request, failed_request}, set(order["scope"]["request_ids"]))

            source_id = self.deliver_for_request(target, fulfilled_request)
            self.fulfil_and_reopen(target, fulfilled_request, source_id, "test-question")
            self.record_attempt(
                target, failed_request, code="no_result", session="orch-test", action="action-0001"
            )

            code, session, stderr = self.submit_delegated(target)

            self.assertEqual(0, code, stderr)
            self.assertEqual("research", session["phase"], "a partial batch still made progress")
            self.assertIn(
                "status: open",
                (target / "wiki" / "questions" / "test-question.md").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "status: blocked",
                (target / "wiki" / "questions" / "second-question.md").read_text(encoding="utf-8"),
            )

    def test_reopening_a_question_whose_other_blocker_failed_is_refused(self):
        # The question is blocked by two requests; only one was fulfilled, so reopening it
        # claims evidence the action did not produce.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            # One question blocked by two requests: `block` links both at once, since a
            # question already blocked cannot be blocked again.
            self.assert_json_script_ok(
                CLAIM,
                [
                    "--project-root", str(target), "claim", "--slug", "test-question",
                    "--agent-id", "agent-test", "--format", "json",
                ],
            )
            request_ids = [
                self.assert_json_script_ok(
                    SOURCE_REQUESTS,
                    [
                        "--project-root", str(target), "add", "--kind", "other",
                        "--query-or-identifier", f"gap {index} for the same question",
                        "--rationale", "Blocks the question.", "--priority", "high",
                        "--question-slug", "test-question", "--format", "json",
                    ],
                )["request"]["request_id"]
                for index in range(2)
            ]
            first, second = request_ids
            self.assert_json_script_ok(
                RESOLVE,
                [
                    "--project-root", str(target), "block", "--slug", "test-question",
                    "--agent-id", "agent-test", "--blocked-reason", "Two gaps remain.",
                    "--request-id", first, "--request-id", second, "--format", "json",
                ],
            )
            self.delegated_session(target)
            self.controller(target, "next", "--orchestration-id", "orch-test")

            source_id = self.deliver_for_request(target, first)
            self.fulfil_and_reopen(target, first, source_id, "test-question")
            self.record_attempt(target, second, code="no_result", session="orch-test", action="action-0001")

            code, error, _ = self.submit_delegated(target)

            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertIn("not fully unblocked", error["message"])

    def test_an_attempt_failure_for_an_unscoped_request_is_refused(self):
        # An order scopes only the requests routing judged retryable. Recording a failure
        # against one it excluded is evidence about work this action was not authorized to
        # do, and would let an exhausted request accumulate attempts it never received.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.add_questions(
                root,
                target,
                [{"id": "second-question", "question": "What else is missing?", "priority": "high"}],
            )
            scoped = self.block_question(target)
            exhausted = self.block_question(target, slug="second-question")
            for index in range(2):
                self.record_attempt(
                    target, exhausted, code="provider_throttled", session="orch-test", action=f"earlier-{index}"
                )
            self.delegated_session(target)
            code, order, stderr = self.controller(target, "next", "--orchestration-id", "orch-test")
            self.assertEqual(0, code, stderr)
            self.assertEqual([scoped], order["scope"]["request_ids"], "the exhausted request is out of scope")

            self.record_attempt(
                target, scoped, code="no_result", session="orch-test", action=order["action_id"]
            )
            # Written straight to the audit: the delegation gate refuses this through the
            # CLI, which is the first line of defence. The postcondition is the second, and
            # it must hold for an audit file however its content arrived.
            audit = target / "sources" / "source-request-attempts.jsonl"
            with audit.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "schema_version": "1.0",
                            "event_type": "source_request_attempt_failed",
                            "event_id": "attempt-outofscope0000000000000000",
                            "request_id": exhausted,
                            "orchestration_id": "orch-test",
                            "action_id": order["action_id"],
                            "failure_code": "no_result",
                            "detail": None,
                            "recorded_at": "2026-08-08T00:00:00Z",
                        }
                    )
                    + "\n"
                )

            code, error, _ = self.submit_delegated(target, action_id=order["action_id"])

            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertIn("outside this action's request scope", error["message"])
            self.assertEqual(
                [exhausted],
                [item["request_id"] for item in error["details"]["unattributable_events"]],
            )

    def test_an_attempt_event_with_an_undocumented_failure_code_is_refused(self):
        # The controller does not trust the audit's content just because it is on disk: a
        # code outside the taxonomy explains nothing a router could act on.
        with tempfile.TemporaryDirectory() as tmpdir:
            target, request_id = self.delegated_action(Path(tmpdir))
            audit = target / "sources" / "source-request-attempts.jsonl"
            audit.parent.mkdir(parents=True, exist_ok=True)
            audit.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "event_type": "source_request_attempt_failed",
                        "event_id": "attempt-handwritten00000000000000",
                        "request_id": request_id,
                        "orchestration_id": "orch-test",
                        "action_id": "action-0001",
                        "failure_code": "the-connector-was-sad",
                        "detail": None,
                        "recorded_at": "2026-08-08T00:00:00Z",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            code, error, _ = self.submit_delegated(target)

            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertIn("outside this action's request scope", error["message"])
            self.assertEqual(
                "the-connector-was-sad",
                error["details"]["unattributable_events"][0]["failure_code"],
            )

    def test_a_fully_unblocked_question_left_blocked_is_refused(self):
        # Fulfilment alone is not the outcome: the question the evidence was for must
        # actually reopen, or research never picks it up again. Through `submit` the
        # earlier runtime guard answers first — a blocked question whose only request is
        # fulfilled is a HIGH lint finding, which flips the readiness verdict — so this
        # asserts the refusal happens and the next case pins the postcondition itself.
        with tempfile.TemporaryDirectory() as tmpdir:
            target, request_id = self.delegated_action(Path(tmpdir))
            source_id = self.deliver_for_request(target, request_id)
            self.fulfil_and_reopen(target, request_id, source_id, slug=None)

            code, error, _ = self.submit_delegated(target)

            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("ORCHESTRATION_WORKSPACE_HEALTH_CHANGED", error["error_code"])

    def test_the_question_transition_postcondition_names_the_unreopened_question(self):
        # The guard behind the runtime refusal above, exercised directly. It is the check
        # that would answer if lint ever stopped flagging that state, and it names the
        # question rather than the workspace.
        with tempfile.TemporaryDirectory() as tmpdir:
            target, request_id = self.delegated_action(Path(tmpdir))
            source_id = self.deliver_for_request(target, request_id)
            self.fulfil_and_reopen(target, request_id, source_id, slug=None)
            order = self.hydrated_order(
                target,
                CONTROLLER.load_json_object(
                    CONTROLLER.work_order_path(target, "orch-test", "action-0001"),
                    error_code="WORK_ORDER_INVALID",
                    label="work order",
                ),
            )

            with self.assertRaises(CONTROLLER.OrchestrationControllerError) as caught:
                CONTROLLER.verify_delegated_acquisition_postconditions(
                    target,
                    CONTROLLER.load_session(target, "orch-test"),
                    order,
                    config=CONTROLLER.load_config(target),
                    status=CONTROLLER.fresh_workspace_status(target),
                    run_id=order.get("run_id"),
                    current="fetching",
                    controller=CONTROLLER.load_sibling_module("run_controller"),
                    apply_effects=False,
                )

            self.assertIn("did not reopen every fully unblocked question", str(caught.exception))
            self.assertEqual(
                ["test-question"],
                [item["question_slug"] for item in caught.exception.details["question_transition_failures"]],
            )

    def test_changing_a_pre_existing_candidate_record_is_refused(self):
        # The candidate store is out of bounds for a delegated action in both directions:
        # nothing may be added, and nothing already there may be altered.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            request_id = self.block_question(target)
            self.append_selected_acquisition_candidates(target, request_id, ["cand-preexisting"])
            self.delegated_session(target)
            code, order, stderr = self.controller(target, "next", "--orchestration-id", "orch-test")
            self.assertEqual(0, code, stderr)

            source_id = self.deliver_for_request(target, request_id)
            self.fulfil_and_reopen(target, request_id, source_id, "test-question")
            store = target / "sources" / "discovery" / "candidates.jsonl"
            record = json.loads(store.read_text(encoding="utf-8").splitlines()[0])
            # A benign field: changing the lifecycle state instead would trip the candidate
            # store's own consistency guard before this check is reached.
            record["selected_by"] = "acquirer-1"
            store.write_text(json.dumps(record) + "\n", encoding="utf-8")

            code, error, _ = self.submit_delegated(target, action_id=order["action_id"])

            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertIn("changed candidate records", error["message"])
            self.assertEqual(
                ["cand-preexisting"],
                error["details"]["candidate_scope_violations"]["changed_outside_scope"],
            )

    def test_editing_a_failed_requests_record_is_refused(self):
        # A failed attempt lives in the audit; the request record itself must be untouched.
        with tempfile.TemporaryDirectory() as tmpdir:
            target, request_id = self.delegated_action(Path(tmpdir))
            self.record_attempt(
                target, request_id, code="no_result", session="orch-test", action="action-0001"
            )
            store = target / "sources" / "source-requests.jsonl"
            record = json.loads(store.read_text(encoding="utf-8").splitlines()[0])
            record["rationale"] = "edited out of band"
            store.write_text(json.dumps(record) + "\n", encoding="utf-8")

            code, error, _ = self.submit_delegated(target)

            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertIn("outside the fulfilled request scope", error["message"])

    def test_touching_the_candidate_store_is_refused(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target, request_id = self.delegated_action(Path(tmpdir))
            source_id = self.deliver_for_request(target, request_id)
            self.fulfil_and_reopen(target, request_id, source_id, "test-question")
            self.append_selected_acquisition_candidates(target, request_id, ["cand-invented"])

            code, error, _ = self.submit_delegated(target)

            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertIn("changed candidate records", error["message"])

    def test_the_delegated_arm_requires_its_attempt_audit_baseline(self):
        # C2 captures the baseline; a work order without it cannot tell a new event from a
        # rewritten one, so verification refuses rather than guessing.
        with tempfile.TemporaryDirectory() as tmpdir:
            target, request_id = self.delegated_action(Path(tmpdir))
            order = CONTROLLER.hydrate_integrity_baselines(
                target,
                CONTROLLER.load_json_object(
                    CONTROLLER.work_order_path(target, "orch-test", "action-0001"),
                    error_code="WORK_ORDER_INVALID",
                    label="work order",
                ),
            )
            for check in order["required_postconditions"]:
                if check["check"] == "manifest_records_increased":
                    check.pop("request_attempt_audit_record_fingerprints_before")

            with self.assertRaises(CONTROLLER.OrchestrationControllerError) as caught:
                CONTROLLER.verify_delegated_acquisition_postconditions(
                    target,
                    CONTROLLER.load_session(target, "orch-test"),
                    order,
                    config=CONTROLLER.load_config(target),
                    status=CONTROLLER.fresh_workspace_status(target),
                    run_id=order.get("run_id"),
                    current="fetching",
                    controller=CONTROLLER.load_sibling_module("run_controller"),
                    apply_effects=False,
                )
            self.assertIn("bounded evidence integrity baseline", str(caught.exception))

    # -- reuse of correlated evidence nothing normalized yet ---------------------------

    def reuse_selector(self, tmpdir: str, records: list[dict], normalized_ids: set[str]):
        """Both issuance-time baselines over one hand-built manifest.

        Returns `(matching, reusable)` so the pair can be read together: they answer the
        same correlation question and split on one clause, and a test that saw only one of
        them could not tell a source that moved between them from one that fell out of both.
        """
        root = Path(tmpdir)
        normalized_root = root / "sources" / "normalized"
        normalized_root.mkdir(parents=True, exist_ok=True)
        for source_id in normalized_ids:
            (normalized_root / f"{source_id.replace(':', '--')}.md").write_text(
                f"---\ntype: normalized_source\nsource_id: {source_id}\n---\n\nbody\n",
                encoding="utf-8",
            )
        normalize_sources = mock.Mock()
        normalize_sources.source_paths.return_value = ("sources/manifest.jsonl", "sources/normalized")
        normalize_sources.load_manifest.return_value = records
        normalize_sources.normalized_output_path_for_record.side_effect = (
            lambda record, output_root: output_root / f"{str(record['id']).replace(':', '--')}.md"
        )
        normalize_sources.is_codebase_record.return_value = False
        # A record declaring no input paths cannot be refused for naming an unpinned one,
        # which keeps this fixture about the one clause it exists to isolate.
        normalize_sources.raw_paths.return_value = []
        with mock.patch.object(CONTROLLER, "load_sibling_module", return_value=normalize_sources):
            matching, reusable, _ = CONTROLLER.acquisition_reuse_baselines(
                root, {}, ["req-scoped"], None
            )
            return matching, reusable

    def test_the_two_reuse_baselines_split_on_the_is_file_clause_and_nothing_else(self):
        """The whole widening, pinned to the one clause it is.

        Two records with identical correlation, differing only in whether anything has
        normalized them. One lands in the map that fingerprints both digests; the other in
        the list that authorizes the record it still owes. Neither lands in both, which is
        what lets the verifier read the reuse terms off the baseline rather than off the
        state the action left behind.
        """
        records = [
            {"id": "raw:already-normalized", "provenance": {"request_id": "req-scoped"}},
            {"id": "raw:not-normalized-yet", "provenance": {"request_id": "req-scoped"}},
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            matching, reusable = self.reuse_selector(tmpdir, records, {"raw:already-normalized"})

        self.assertEqual(["raw:already-normalized"], sorted(matching))
        self.assertEqual(["raw:not-normalized-yet"], reusable)
        self.assertEqual(set(), set(matching) & set(reusable))

    def test_evidence_correlated_to_another_request_or_to_nothing_is_not_reusable(self):
        """The predicate is correlation, and correlation only.

        Every other field a reuse rule could read -- the delivery's scope, its timing, its
        retriever -- is written by the same untrusted party as `request_id` itself, so a
        rule that read one of them would authorize whatever that party decided to write.
        These three are the shapes that rule would have admitted.
        """
        records = [
            {"id": "raw:another-request", "provenance": {"request_id": "req-elsewhere"}},
            {"id": "raw:no-request", "provenance": {"retrieved_by": "acquirer-1"}},
            {"id": "raw:no-provenance", "provenance": None},
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            matching, reusable = self.reuse_selector(tmpdir, records, set())

        self.assertEqual({}, matching)
        self.assertEqual([], reusable)

    def test_a_reuse_baseline_beyond_the_bounded_contract_is_refused_at_issuance(self):
        records = [
            {"id": f"raw:pending-{index:04d}", "provenance": {"request_id": "req-scoped"}}
            for index in range(CONTROLLER.MAX_SCOPE_IDS + 1)
        ]
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            self.assertRaises(CONTROLLER.OrchestrationControllerError) as caught,
        ):
            self.reuse_selector(tmpdir, records, set())

        self.assertEqual("ORCHESTRATION_SCOPE_EXCEEDED", caught.exception.error_code)

    def test_the_reuse_baseline_validator_bounds_names_and_disambiguates(self):
        """Three properties, because a verifier acting on any of it needs all three."""
        manifest = {"raw:a": "sha256:" + "1" * 64, "raw:b": "sha256:" + "2" * 64}
        valid = CONTROLLER.valid_unnormalized_reuse_baseline

        self.assertTrue(valid([], [], manifest))
        self.assertTrue(valid(["raw:a"], ["raw:b"], manifest))
        # Not a bounded list of scope ids.
        self.assertFalse(valid({"raw:a": {}}, [], manifest))
        self.assertFalse(valid(["raw:a", ""], [], manifest))
        self.assertFalse(
            valid([f"raw:pad-{index}" for index in range(CONTROLLER.MAX_SCOPE_IDS + 1)], [], manifest)
        )
        # Names a record the manifest baseline does not, so nothing would check it.
        self.assertFalse(valid(["raw:invented"], [], manifest))
        # In both maps at once, so which reuse terms apply would depend on lookup order.
        self.assertFalse(valid(["raw:a"], ["raw:a"], manifest))

        # The bound and the id shape have to be asserted against ids the manifest baseline
        # *does* name, or the containment clause alone rejects every case above and the
        # first two properties are never exercised: deleting them from the validator would
        # leave the suite green. Each of these is well within the manifest baseline and
        # refused on its own terms.
        digest = "sha256:" + "3" * 64
        oversized = "raw:" + "x" * CONTROLLER.MAX_SCOPE_ID_LENGTH
        embedded_nul = "raw:a\x00b"
        malformed_manifest = {**manifest, oversized: digest, embedded_nul: digest}
        self.assertFalse(
            valid([oversized], [], malformed_manifest),
            "an over-length id the manifest baseline holds is still not a scope id",
        )
        self.assertFalse(
            valid([embedded_nul], [], malformed_manifest),
            "an id carrying a NUL is still not a scope id",
        )
        self.assertFalse(
            valid(["raw:a", "raw:a"], [], manifest),
            "a repeated id makes the baseline's own length no answer about what it names",
        )
        over_bound = [f"raw:pad-{index}" for index in range(CONTROLLER.MAX_SCOPE_IDS + 1)]
        self.assertFalse(
            valid(over_bound, [], {source_id: digest for source_id in over_bound}),
            "the bound holds even when every id is named by the manifest baseline",
        )

    def test_a_source_whose_normalization_reads_unpinned_inputs_is_not_reusable(self):
        """Re-derivation is only as strong as what the order pinned, and says so.

        A `codebase:` record normalizes from an artifact bundle under the configured
        codebase output directory, which neither the raw-tree baseline nor the normalized
        one fingerprints. Re-deriving such a record would confirm its body against an input
        the acquirer can write during the order, which is the one thing this check exists
        to prevent, so the reuse is withheld instead.
        """
        normalize_sources = CONTROLLER.load_sibling_module("normalize_sources")
        record = {
            "id": "codebase:example",
            "kind": normalize_sources.CODEBASE_KIND,
            "raw_paths": ["raw/code/example"],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            normalized_path = root / "sources" / "normalized" / "codebase--example.md"
            normalized_path.parent.mkdir(parents=True)
            normalized_path.write_text(
                "---\ntype: normalized_source\nsource_id: codebase:example\n"
                "updated: 2026-08-18\nnormalized_at: 2026-08-18T00:00:00Z\n---\n\nbody\n",
                encoding="utf-8",
            )

            failure = CONTROLLER.normalized_output_derivation_failure(
                root, {}, record, normalized_path, [record], normalize_sources
            )

        self.assertEqual(
            "the reused source normalizes from artifacts this order does not fingerprint",
            failure["reason"],
        )

    def test_a_record_naming_a_path_outside_the_fingerprinted_raw_roots_is_not_reusable(self):
        """The general form of the kind-specific early-out above.

        `raw_tree_snapshot` walks `raw.source_roots` and nothing else, while every record
        path field resolves through `safe_workspace_path`, which accepts any
        workspace-relative path. So a record may name raw evidence no baseline fingerprints
        — exactly the set a *failed* prior order leaves behind — and re-deriving from it
        would confirm the reused body against bytes the acquirer can still write.
        """
        normalize_sources = CONTROLLER.load_sibling_module("normalize_sources")
        config = {"raw": {"source_roots": ["raw/papers", "raw/data"]}}
        unpinned = CONTROLLER.unpinned_record_input_paths

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self.assertEqual(
                [],
                unpinned(root, config, {"raw_paths": ["raw/papers/a.pdf"]}, normalize_sources),
                "a path under a configured root is pinned",
            )
            self.assertEqual(
                ["sources/scratch/a.pdf"],
                unpinned(root, config, {"raw_paths": ["sources/scratch/a.pdf"]}, normalize_sources),
            )
            self.assertEqual(
                ["raw/leftovers/a.pdf"],
                unpinned(root, config, {"raw_pdf": "raw/leftovers/a.pdf"}, normalize_sources),
                "raw/ is not the fingerprinted set; the configured roots are",
            )
            self.assertEqual(
                ["raw/tex-elsewhere"],
                unpinned(root, config, {"latex_root": "raw/tex-elsewhere"}, normalize_sources),
            )
            self.assertEqual(
                ["../escape.pdf"],
                unpinned(root, config, {"raw_paths": ["../escape.pdf"]}, normalize_sources),
                "a path safe_workspace_path refuses is unpinned rather than an exception",
            )
            self.assertEqual(
                ["raw/papers/a.pdf"],
                unpinned(root, {}, {"raw_paths": ["raw/papers/a.pdf"]}, normalize_sources),
                "a workspace configuring no raw roots fingerprints nothing, so nothing is pinned",
            )

    def test_the_re_derivation_sandbox_carries_the_adapter_it_has_to_run(self):
        """The confinement must not turn a working adapter into unverifiable evidence.

        The sandbox holds the trusted static inputs and the record's own raw evidence, and
        the adapter runs with it as the working directory. An adapter argv naming a
        workspace path outside those trees — `tools/adapter.py` is as valid as one under
        `scripts/` — would be absent there, so a workspace where `normalize_sources.py`
        succeeds would fail only under verification, refused as evidence that could not be
        re-derived. Whatever of the argv lives in the workspace comes along, with its mode
        bits, because an adapter that is an executable script must still be executable.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "tools").mkdir()
            outside = root / "tools" / "structured_adapter.py"
            outside.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            outside.chmod(0o755)
            (root / "scripts").mkdir()
            (root / "scripts" / "normalize_sources.py").write_text("x = 1\n", encoding="utf-8")

            config = {
                "normalization": {
                    "adapters": [
                        {
                            "kinds": ["structured_data"],
                            "provider": "command",
                            "command": ["python3", "tools/structured_adapter.py", "--json"],
                            "name": "stub",
                            "version": "1.0.0",
                        }
                    ]
                }
            }
            record = {"id": "raw:example", "kind": "structured_data"}
            self.assertEqual(
                ["tools/structured_adapter.py"],
                CONTROLLER.adapter_workspace_command_paths(root, config, record),
                "an interpreter name and a flag are not workspace paths; the script is",
            )
            self.assertEqual(
                [],
                CONTROLLER.adapter_workspace_command_paths(root, config, {"kind": "table"}),
                "a kind no adapter is configured for names nothing",
            )

            module = CONTROLLER.load_sibling_module("normalize_sources")
            item = SimpleNamespace(method=module.ADAPTER_METHOD)
            with CONTROLLER.rederivation_root(
                root, item, module, ["tools/structured_adapter.py"]
            ) as sandbox:
                copied = sandbox / "tools" / "structured_adapter.py"
                self.assertTrue(copied.is_file(), "the sandbox does not carry the adapter it must run")
                self.assertTrue(os.access(copied, os.X_OK), "the copied adapter lost its mode bits")
                self.assertTrue((sandbox / "scripts" / "normalize_sources.py").is_file())
                self.assertNotEqual(root.resolve(), sandbox.resolve())
            self.assertFalse(sandbox.exists(), "the sandbox outlived the re-derivation")

    def test_the_re_derivation_sandbox_refuses_a_multiply_linked_input(self):
        """Singly linked, not merely regular -- the constraint every sibling guard applies.

        `file_tree_fingerprint_snapshot` and `file_digest` both refuse a file whose
        `st_nlink` exceeds one, because a second name for the same inode makes "which path
        was verified" unanswerable. The sandbox copies inputs by path, so admitting one
        would re-derive evidence from bytes no baseline fingerprinted under that name --
        the confinement checking a weaker property than the guards it stands beside.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "scripts").mkdir()
            (root / "scripts" / "normalize_sources.py").write_text("x = 1\n", encoding="utf-8")
            target = root / "scripts" / "shared.py"
            target.write_text("y = 2\n", encoding="utf-8")
            os.link(target, root / "scripts" / "alias.py")

            module = CONTROLLER.load_sibling_module("normalize_sources")
            item = SimpleNamespace(method=module.ADAPTER_METHOD)
            with self.assertRaises(CONTROLLER.RederivationSandboxError) as caught:
                with CONTROLLER.rederivation_root(root, item, module, []):
                    pass
            self.assertIn("singly linked", str(caught.exception))

    def test_a_stamped_pdf_extractor_is_resolved_through_the_allowlist_not_executed(self):
        """The record is the acquirer's file, so the extractor it names is untrusted text.

        `normalize_selected_record` takes `pdf_extractor or resolve_pdf_extractor(...)`, and
        a bare `str` there means *a resolved pdftotext executable path* that becomes
        `argv[0]` of a subprocess. Forwarding the stamped name let the record choose the
        program postcondition verification runs. Resolving through the allowlist refuses an
        unknown name with a reason instead, and never reaches an executable at all.
        """
        normalize_sources = CONTROLLER.load_sibling_module("normalize_sources")

        with mock.patch.object(normalize_sources.subprocess, "run") as never_run:
            for stamped in (
                {"name": "/tmp/attacker-script.sh", "version": "1"},
                {"name": "pdftotext"},
                {"name": ""},
                {"version": "1"},
                "poppler",
                None,
            ):
                with self.subTest(stamped=stamped):
                    failure, extractor = CONTROLLER.stamped_pdf_extractor(
                        {"pdf_extractor": stamped}, normalize_sources
                    )
                    self.assertIsNone(extractor)
                    self.assertEqual(
                        "normalized evidence names a PDF extractor this package does not implement",
                        failure["reason"],
                    )
            never_run.assert_not_called()

        # And the one name this package does implement resolves to a real extractor whose
        # identity comes from the allowlist rather than from the record.
        failure, extractor = CONTROLLER.stamped_pdf_extractor(
            {"pdf_extractor": {"name": "pypdf", "version": "claimed-by-the-record"}}, normalize_sources
        )
        self.assertIsNone(failure)
        self.assertEqual("pypdf", extractor.name)
        self.assertNotEqual("claimed-by-the-record", extractor.version)

    def test_version_stamps_are_read_back_so_a_host_upgrade_is_not_a_forgery(self):
        """Provenance about the tools, not a statement about the evidence.

        Both stamps are taken from whatever is installed at the moment of the run and land
        in the compared bytes. Comparing them would make an upgrade between issuance and
        submission, a different virtualenv, or a replay across a version bump read as
        "normalized evidence is not what normalizing the raw evidence produces" — with a
        remediation that blames hand-editing. `normalize_sources.is_stale` already calls
        extractor versions provenance rather than a rewrite trigger; this keeps the two
        halves of the package saying one thing.
        """
        expected = {
            "normalizer": {"name": "normalize_sources.py", "version": 9},
            "pdf_extractor": {"name": "pypdf", "version": "6.0.0"},
            "content_hash": "sha256:derived",
        }
        CONTROLLER.carry_version_stamps(
            expected,
            {
                "normalizer": {"name": "something-else", "version": 8},
                "pdf_extractor": {"name": "poppler", "version": "24.02.0"},
                "content_hash": "sha256:claimed",
            },
        )
        self.assertEqual(8, expected["normalizer"]["version"])
        self.assertEqual("24.02.0", expected["pdf_extractor"]["version"])
        # Only the versions travel: the producer names and everything else stay derived, so
        # a record still cannot claim a producer that did not produce it.
        self.assertEqual("normalize_sources.py", expected["normalizer"]["name"])
        self.assertEqual("pypdf", expected["pdf_extractor"]["name"])
        self.assertEqual("sha256:derived", expected["content_hash"])

    def test_the_re_derivation_verdict_is_reached_once_per_submit_not_once_per_pass(self):
        """Three verification passes, one re-derivation.

        `submit_result` verifies to prepare the submission, again to confirm the prepared
        phase still holds, and again to apply effects. Re-derivation is the most expensive
        check either arm has — an external adapter run, or two `pdftotext` passes, per
        reused source, bounded only by `MAX_SCOPE_IDS` — and all of it is held inside the
        driver session lock that peers acquire with `wait_seconds=0`.
        """
        calls: list[tuple[str, str]] = []

        def record(project_root, config, record_value, normalized_path, manifest_records, module):
            calls.append((record_value["id"], str(normalized_path)))
            return None

        with (
            mock.patch.object(CONTROLLER, "normalized_output_derivation_failure", record),
            CONTROLLER.derivation_verdict_memo(),
        ):
            for _ in range(3):
                CONTROLLER.memoised_derivation_failure(
                    Path("/workspace"), {}, "raw:a", {"id": "raw:a"}, Path("/n/a.md"),
                    "sha256:record", "sha256:normalized", [], object(),
                )
            self.assertEqual(1, len(calls))

            # A digest that moved is a different question, so it is asked again.
            CONTROLLER.memoised_derivation_failure(
                Path("/workspace"), {}, "raw:a", {"id": "raw:a"}, Path("/n/a.md"),
                "sha256:record", "sha256:rewritten", [], object(),
            )
            self.assertEqual(2, len(calls))

        # Outside a submit there is no memo at all, so nothing is carried between them.
        with mock.patch.object(CONTROLLER, "normalized_output_derivation_failure", record):
            for _ in range(2):
                CONTROLLER.memoised_derivation_failure(
                    Path("/workspace"), {}, "raw:a", {"id": "raw:a"}, Path("/n/a.md"),
                    "sha256:record", "sha256:normalized", [], object(),
                )
        self.assertEqual(4, len(calls))

    def test_submit_enters_the_memo_around_every_verification_pass(self):
        """The memo is only worth anything where all three passes are inside it."""
        tree = ast.parse(Path(CONTROLLER.__file__).read_text(encoding="utf-8"))
        function = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "submit_result"
        )
        guarded = {
            getattr(call.func, "id", None)
            for statement in ast.walk(function)
            if isinstance(statement, ast.With)
            for item in statement.items
            for call in [item.context_expr]
            if isinstance(call, ast.Call)
        }
        self.assertIn("derivation_verdict_memo", guarded)
        finalizers = [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "finalize_pending_submission"
        ]
        self.assertTrue(finalizers, "submit_result no longer finalizes a submission")
        memo_lines = [
            statement.lineno
            for statement in ast.walk(function)
            if isinstance(statement, ast.With)
            and any(
                isinstance(item.context_expr, ast.Call)
                and getattr(item.context_expr.func, "id", None) == "derivation_verdict_memo"
                for item in statement.items
            )
        ]
        for call in finalizers:
            self.assertTrue(
                any(line < call.lineno for line in memo_lines),
                "a verification pass runs outside the per-submit memo",
            )

    def test_neither_acquisition_arm_offers_a_recourse_a_fulfilled_request_refuses(self):
        """One set of reuse terms, and no second route from either arm, because there is none.

        Both remediations used to end in an "otherwise" — record the attempt failure for the
        delegated arm, acquire through another candidate for the provider one. Both are
        printed only from `preexisting_reuse_scope_failures`, which is computed over the
        acquirer's *fulfilled* list, so the request they speak about is already fulfilled by
        the source being refused. `record-attempt-failure` refuses a fulfilled request
        outright and `fulfill` refuses to relink one, which is where the second delivery and
        the second candidate both end. `following_the_reuse_refusals_escapes_is_refused`
        below walks all of that against the real scripts.

        So the split between the arms is no longer which recourse each offers — neither has
        one — but which dead end each arm's reader would otherwise have reached for, named
        so it is not tried.
        """
        terms = CONTROLLER.REUSE_SCOPE_TERMS
        self.assertTrue(CONTROLLER.REUSE_SCOPE_REMEDIATION.startswith(terms))
        self.assertTrue(CONTROLLER.PROVIDER_REUSE_SCOPE_REMEDIATION.startswith(terms))
        self.assertNotIn("record-attempt-failure", CONTROLLER.REUSE_SCOPE_REMEDIATION)
        self.assertNotIn("record-attempt-failure", CONTROLLER.PROVIDER_REUSE_SCOPE_REMEDIATION)
        self.assertIn("another selected candidate", CONTROLLER.PROVIDER_REUSE_SCOPE_REMEDIATION)
        self.assertNotIn("another selected candidate", CONTROLLER.REUSE_SCOPE_REMEDIATION)
        for constant in (
            CONTROLLER.REUSE_SCOPE_REMEDIATION,
            CONTROLLER.PROVIDER_REUSE_SCOPE_REMEDIATION,
        ):
            with self.subTest(advice=constant[-60:]):
                self.assertIn(CONTROLLER.NO_SECOND_ROUTE_FOR_A_FULFILLED_REQUEST, constant)

    def tampered_reuse_baseline(self, order: dict, value: list[str]) -> dict:
        for check in order["required_postconditions"]:
            if check["check"] == "manifest_records_increased":
                check["reusable_source_ids_before"] = value
        return order

    def hydrated_pending_order(self, target: Path, action_id: str = "action-0001") -> dict:
        return CONTROLLER.hydrate_integrity_baselines(
            target,
            CONTROLLER.load_json_object(
                CONTROLLER.work_order_path(target, "orch-test", action_id),
                error_code="WORK_ORDER_INVALID",
                label="work order",
            ),
        )

    def verify_delegated(self, target: Path, order: dict):
        return CONTROLLER.verify_delegated_acquisition_postconditions(
            target,
            CONTROLLER.load_session(target, "orch-test"),
            order,
            config=CONTROLLER.load_config(target),
            status=CONTROLLER.fresh_workspace_status(target),
            run_id=order.get("run_id"),
            current="fetching",
            controller=CONTROLLER.load_sibling_module("run_controller"),
            apply_effects=False,
        )

    def test_a_reuse_baseline_naming_an_unknown_record_is_refused_at_replay(self):
        """Wiring site one: the guard that decides a pending order is replayable at all."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target, _ = self.delegated_action(Path(tmpdir))
            order = self.tampered_reuse_baseline(
                self.hydrated_pending_order(target), ["raw:never-in-this-manifest"]
            )

            with self.assertRaises(CONTROLLER.OrchestrationControllerError) as caught:
                CONTROLLER.require_acquisition_evidence_baselines(order)

            self.assertEqual(
                "ORCHESTRATION_ACQUISITION_BASELINE_UNAVAILABLE", caught.exception.error_code
            )

    def test_a_reuse_baseline_naming_an_unknown_record_is_refused_by_the_delegated_arm(self):
        """Wiring site two. An id outside the manifest baseline is checked by nothing.

        Reconciliation only walks sources the manifest snapshot holds, so an allowlist that
        reaches past it would silently authorize an id no guard ever looks at. The two maps
        are written together from one manifest read, so disagreeing means tampering.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            target, request_id = self.delegated_action(Path(tmpdir))
            source_id = self.deliver_for_request(target, request_id)
            self.fulfil_and_reopen(target, request_id, source_id, "test-question")
            order = self.tampered_reuse_baseline(
                self.hydrated_pending_order(target), ["raw:never-in-this-manifest"]
            )

            with self.assertRaises(CONTROLLER.OrchestrationControllerError) as caught:
                self.verify_delegated(target, order)

            self.assertIn("bounded evidence integrity baseline", str(caught.exception))

    def test_an_order_issued_before_reuse_existed_replays_unchanged(self):
        """The field is additive: a pending order without it verifies exactly as it did."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target, request_id = self.delegated_action(Path(tmpdir))
            source_id = self.deliver_for_request(target, request_id)
            self.fulfil_and_reopen(target, request_id, source_id, "test-question")
            order = self.hydrated_pending_order(target)
            manifest_guard = next(
                check
                for check in order["required_postconditions"]
                if check["check"] == "manifest_records_increased"
            )
            self.assertEqual([], manifest_guard.pop("reusable_source_ids_before"))

            CONTROLLER.require_acquisition_evidence_baselines(order)

            self.assertEqual(("research", None), self.verify_delegated(target, order))

    def test_an_order_issued_before_reuse_existed_replays_with_reuse_unavailable(self):
        """And it is additive in the other direction too: the affordance is simply absent.

        The same workspace, the same fulfilment, one field removed. With the field the
        reuse is authorized and the action verifies; without it the action is refused, and
        refused for the reason that is actually true of a pre-reuse order -- this order
        authorized none -- rather than for a mislabelled one.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            request_id = self.block_question(target)
            source_id = self.deliver_for_request(target, request_id, normalize=False)
            self.delegated_session(target)
            code, _, stderr = self.controller(target, "next", "--orchestration-id", "orch-test")
            self.assertEqual(0, code, stderr)
            self.assert_json_script_ok(
                NORMALIZE, ["--project-root", str(target), "--all", "--format", "json"]
            )
            self.fulfil_and_reopen(target, request_id, source_id, "test-question")

            self.assertEqual(("research", None), self.verify_delegated(target, self.hydrated_pending_order(target)))

            legacy = self.tampered_reuse_baseline(self.hydrated_pending_order(target), [])
            with self.assertRaises(CONTROLLER.OrchestrationControllerError) as caught:
                self.verify_delegated(target, legacy)

            self.assertIn("reuses pre-existing evidence", str(caught.exception))
            self.assertEqual(
                [
                    {
                        "source_id": source_id,
                        "cause": "no_reuse_authorization_at_issuance",
                        # Each cause carries its own repair, because they do not share one.
                        # This source satisfies every clause of the shared reuse terms, so
                        # repeating those terms at it would tell it to do what it has done.
                        "repair": CONTROLLER.REUSE_SCOPE_CAUSE_REPAIRS[
                            "no_reuse_authorization_at_issuance"
                        ],
                        "provenance_request_id": request_id,
                        "record_unchanged": True,
                    }
                ],
                caught.exception.details["reuse_scope_failures"],
            )

    def order_over_a_source_delivered_first(
        self,
        root: Path,
        *,
        sidecar_request_id: str | None = "",
    ) -> tuple[Path, str, str]:
        """A live order whose scoped request is fulfilled by evidence delivered before it.

        The only shape in which the reuse refusals can fire: the source is in
        `manifest_record_fingerprints_before`, so it is pre-existing, and the request that
        names it is `fulfilled`, so every escape those refusals could print is being printed
        at a request `source_requests.py` will no longer let anyone touch.
        """
        target = self.init_workspace(root, question=True)
        request_id = self.block_question(target)
        source_id = self.deliver_for_request(target, request_id, sidecar_request_id=sidecar_request_id)
        self.delegated_session(target)
        code, order, stderr = self.controller(target, "next", "--orchestration-id", "orch-test")
        self.assertEqual(0, code, stderr)
        self.assertEqual("delegated", order["acquisition_mode"])
        self.fulfil_and_reopen(target, request_id, source_id, "test-question")
        return target, request_id, source_id

    def stored_request(self, target: Path, request_id: str) -> dict:
        path = target / "sources" / "source-requests.jsonl"
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = json.loads(line)
                if record.get("request_id") == request_id:
                    return record
        raise AssertionError(f"no stored request {request_id}")

    def test_following_the_reuse_refusals_escapes_is_refused(self):
        """The printed advice, walked — and refused in the one state that can print it.

        `preexisting_reuse_scope_failures` takes the acquirer's *fulfilled* list, so by the
        time this refusal exists the scoped request is already fulfilled by the source being
        refused. Both escapes the advice used to name are closed in exactly that state, and
        both are performed here rather than reasoned about:

        - `source_requests.py record-attempt-failure` refuses a fulfilled request outright;
        - "deliver that evidence again as a new source under its own raw path" gets as far
          as a clean second inventory — which is what made it look like a route — and then
          `fulfill` refuses to relink the request to it.

        Two commands, one refusal each, and the operator ends exactly where they started.
        That is why neither is named any more, and why what is named instead is the state:
        this request is fulfilled and takes neither.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            target, request_id, source_id = self.order_over_a_source_delivered_first(
                Path(tmpdir), sidecar_request_id=None
            )

            code, error, _ = self.submit_delegated(target)

            self.assertEqual(CONTROLLER.EXIT_INVALID, code, error)
            self.assertEqual("ORCHESTRATION_POSTCONDITION_FAILED", error["error_code"])
            self.assertIn("reuses pre-existing evidence", error["message"], error)
            failure = error["details"]["reuse_scope_failures"][0]
            self.assertEqual(source_id, failure["source_id"])
            self.assertEqual("provenance_names_no_scoped_request", failure["cause"], failure)
            self.assertEqual(CONTROLLER.REUSE_SCOPE_REMEDIATION, error["remediation"])

            # Escape one, exactly as it used to be printed.
            attempt_code, attempt_error, _ = self.json_script(
                SOURCE_REQUESTS,
                [
                    "--project-root", str(target), "record-attempt-failure",
                    "--request-id", request_id, "--failure-code", "no_result",
                    "--orchestration-id", "orch-test", "--action-id", "action-0001",
                    "--format", "json",
                ],
            )
            self.assertEqual(CONTROLLER.EXIT_INVALID, attempt_code, attempt_error)
            self.assertEqual("REQUEST_ALREADY_FULFILLED", attempt_error["error_code"], attempt_error)
            self.assertTrue(attempt_error["recoverable"], attempt_error)

            # Escape two. The delivery and the inventory both succeed, which is the whole
            # trap: nothing refuses until the request is asked to take the new source.
            second_source_id = self.deliver_for_request(target, request_id, name="supplier-quote-again")
            self.assertNotEqual(source_id, second_source_id)

            relink_code, relink_error, _ = self.try_fulfil(target, request_id, second_source_id)

            self.assertEqual(CONTROLLER.EXIT_INVALID, relink_code, relink_error)
            self.assertEqual("REQUEST_ALREADY_FULFILLED", relink_error["error_code"], relink_error)
            self.assertTrue(relink_error["recoverable"], relink_error)
            self.assertEqual(source_id, self.stored_request(target, request_id)["source_id"])

            # And the advice the operator actually reads names neither of them.
            for text in (error["remediation"], failure["repair"]):
                with self.subTest(text=text[:60]):
                    self.assertNotIn("record-attempt-failure", text)
                    self.assertIn("already fulfilled", text)

    def test_restoring_the_rewritten_record_only_renames_the_refusal(self):
        """The third cause's repair, walked: it is required, and it is not a way through.

        "Restore it exactly as the order recorded it" reads as the repair for
        `manifest_record_changed_after_issuance`, and the restore genuinely is required —
        the manifest-scope guard downstream refuses any rewritten pre-existing record. What
        it is not is a route to an accepted action, because membership of this failure set
        never depended on the rewrite: a source lands here by being pre-existing and in
        neither reuse baseline, both settled at issuance, and only the *cause* is decided by
        comparing today's record against the fingerprint. Restore it and the same guard
        refuses the same source for the reason that was true all along.

        So the repair is performed literally and the workspace resubmitted, exactly as the
        sidecar-remediation precedent does it, and the second refusal is asserted rather
        than an acceptance.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            request_id = self.block_question(target)
            source_id = self.deliver_for_request(target, request_id, sidecar_request_id=None)
            self.delegated_session(target)
            code, _, stderr = self.controller(target, "next", "--orchestration-id", "orch-test")
            self.assertEqual(0, code, stderr)
            manifest = target / "sources" / "manifest.jsonl"
            at_issuance = manifest.read_bytes()

            # Stamp the sidecar and re-inventory, which is what puts the record out of step
            # with the fingerprint the order took.
            self.assertEqual(source_id, self.deliver_for_request(target, request_id))
            self.assertNotEqual(at_issuance, manifest.read_bytes(), "the re-inventory rewrote nothing")
            self.fulfil_and_reopen(target, request_id, source_id, "test-question")

            code, error, _ = self.submit_delegated(target)

            self.assertEqual(CONTROLLER.EXIT_INVALID, code, error)
            failure = error["details"]["reuse_scope_failures"][0]
            self.assertEqual("manifest_record_changed_after_issuance", failure["cause"], failure)
            self.assertFalse(failure["record_unchanged"], failure)
            self.assertEqual(
                CONTROLLER.REUSE_SCOPE_CAUSE_REPAIRS["manifest_record_changed_after_issuance"],
                failure["repair"],
            )

            # The repair, performed exactly as it is printed.
            manifest.write_bytes(at_issuance)

            code, again, _ = self.submit_delegated(target)

            self.assertEqual(CONTROLLER.EXIT_INVALID, code, again)
            self.assertIn("reuses pre-existing evidence", again["message"], again)
            restored = again["details"]["reuse_scope_failures"][0]
            self.assertEqual(source_id, restored["source_id"])
            self.assertTrue(restored["record_unchanged"], restored)
            self.assertEqual(
                "provenance_names_no_scoped_request",
                restored["cause"],
                "restoring the record cleared the refusal instead of renaming its cause",
            )

    def reconciliation_refusal_over_a_rewritten_record(self, target: Path, source_id: str) -> dict:
        """Rebuild the fingerprinted normalized record, then submit, and return the failure.

        `normalized_at` is second-resolution, so a rebuild in the same wall-clock second as
        the original renders identical bytes and there is no refusal to reach. The stamp is
        pinned rather than left to how fast the suite runs.
        """
        with mock.patch.object(NORMALIZE, "timestamp_utc", lambda: "2026-08-20T09:00:00Z"):
            self.assert_json_script_ok(
                NORMALIZE,
                ["--project-root", str(target), "--source-id", source_id, "--force", "--format", "json"],
            )
        code, error, _ = self.submit_delegated(target)
        self.assertEqual(CONTROLLER.EXIT_INVALID, code, error)
        self.assertEqual(
            "pre-existing fulfilled evidence is not an unchanged exact scoped reconciliation match",
            error["message"],
            error,
        )
        failure = next(
            item for item in error["details"]["reconciliation_failures"] if item["source_id"] == source_id
        )
        self.assertTrue(failure["was_scoped_match"], failure)
        self.assertEqual(CONTROLLER.RECONCILIATION_ARM_REPAIRS["scoped_match"], failure["repair"])
        return error

    def test_the_failed_outcome_costs_exactly_what_the_repair_says(self):
        """The one escape nothing refuses, and the reason it is no longer offered as one.

        `scoped_match` used to end "end the action with a failed outcome and start a new
        session". It is not refused — and that is worse than being refused, because what it
        does instead is accept. `prepare_submission` answers a `failed` outcome without
        calling `verify_action_postconditions`, so the reconciliation guard that had just
        refused this evidence never runs again; `run_fulfill` has already written
        `fulfilled` and the source id into the request store and nothing rolls that back;
        and routing scopes `open_requests`, so no later order ever sees this request.

        All three are observed here on the workspace the advice was followed on. The
        refusal is real first, then the advice is performed, and what is left behind is a
        session that ended, a fulfilment the controller declined to verify still recorded
        against the request, and no request left for the "new session" the advice promised
        to route.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            target, request_id, source_id = self.order_over_a_source_delivered_first(Path(tmpdir))
            error = self.reconciliation_refusal_over_a_rewritten_record(target, source_id)
            self.assertEqual(CONTROLLER.RECONCILIATION_REMEDIATION, error["remediation"])
            rewritten = sorted((target / "sources" / "normalized").glob("*.md"))
            unverified = {path: path.read_bytes() for path in rewritten}

            code, session, stderr = self.submit_delegated(
                target,
                outcome="failed",
                summary="Reconciliation refused the reused source and the rewrite is being kept.",
            )

            # `EXIT_INVALID` here is the session's terminal verdict, not a refusal: the
            # submission was accepted, the action is recorded as completed, and no
            # postcondition ran. A host reading the exit code alone cannot tell this apart
            # from the refusal above, which is part of why the advice read as harmless.
            self.assertEqual(CONTROLLER.EXIT_INVALID, code, stderr)
            self.assertEqual("failed", session["phase"], session)
            self.assertEqual("failed", session["status"], session)
            self.assertEqual("action-0001", session["last_completed_action_id"], session)
            self.assertEqual(
                ["action-0001"], [record["action_id"] for record in session["failure_records"]], session
            )

            # Cost one: the fulfilment the controller refused to verify is still recorded.
            stored = self.stored_request(target, request_id)
            self.assertEqual("fulfilled", stored["status"], stored)
            self.assertEqual(source_id, stored["source_id"], stored)

            # Cost two: the request never reopens, so the promised new session has nothing
            # to route and this evidence is never verified by anything.
            self.assertEqual(
                [],
                [
                    record["request_id"]
                    for record in CONTROLLER.open_requests(target, CONTROLLER.load_config(target))
                ],
                "a fulfilled request is invisible to routing, so no later order revisits it",
            )

            # Cost three: the unverified bytes are simply still there.
            for path, content in unverified.items():
                with self.subTest(record=path.name):
                    self.assertEqual(content, path.read_bytes())

    def test_the_reuse_baseline_authorises_exactly_one_normalized_record(self):
        """The unit-level view of what the arm widens: `allowed_new_normalized_paths`.

        Asserted on the returned set rather than through a refusal, because the boundary
        that matters is what the set *contains* -- an arm the order recorded as already
        normalized must contribute nothing to it, or its byte-identity guarantee quietly
        becomes optional and no single refusal would show that.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            request_id = self.block_question(target)
            reused = self.deliver_for_request(target, request_id, normalize=False)
            self.delegated_session(target)
            code, _, stderr = self.controller(target, "next", "--orchestration-id", "orch-test")
            self.assertEqual(0, code, stderr)

            order = self.hydrated_pending_order(target)
            manifest_guard = next(
                check
                for check in order["required_postconditions"]
                if check["check"] == "manifest_records_increased"
            )
            self.assertEqual([reused], manifest_guard["reusable_source_ids_before"])
            self.assertEqual({}, manifest_guard["matching_source_records_before"])

            normalize_sources = CONTROLLER.load_sibling_module("normalize_sources")
            config = CONTROLLER.load_config(target)
            manifest_relative, normalized_relative = normalize_sources.source_paths(config)
            by_source_id = normalize_sources.records_by_source_id(
                normalize_sources.load_manifest(target / manifest_relative)
            )

            allowed, required = CONTROLLER.normalized_output_scope(
                target, target / normalized_relative, {reused}, by_source_id, normalize_sources
            )
            self.assertEqual({f"sources/normalized/{reused.replace(':', '--')}.md"}, allowed)
            self.assertEqual(allowed, required)

            # The arm that was normalized at issuance contributes nothing at all.
            empty_allowed, empty_required = CONTROLLER.normalized_output_scope(
                target, target / normalized_relative, set(), by_source_id, normalize_sources
            )
            self.assertEqual(set(), empty_allowed)
            self.assertEqual(set(), empty_required)

    def test_an_authored_normalized_body_is_refused_for_a_reused_source(self):
        """The reuse authorizes a path; re-derivation is what binds the bytes to the raw.

        Every other artifact a reuse touches is pinned by a digest the order took before the
        acquirer started. The normalized record is the one it may create, and it is the one
        the wiki quotes, so the verifier normalizes the unchanged raw evidence itself and
        compares rather than trusting that a new file is a derived one.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            request_id = self.block_question(target)
            source_id = self.deliver_for_request(target, request_id, normalize=False)
            self.delegated_session(target)
            code, _, stderr = self.controller(target, "next", "--orchestration-id", "orch-test")
            self.assertEqual(0, code, stderr)
            self.assert_json_script_ok(
                NORMALIZE, ["--project-root", str(target), "--all", "--format", "json"]
            )
            self.fulfil_and_reopen(target, request_id, source_id, "test-question")
            record = target / "sources" / "normalized" / f"{source_id.replace(':', '--')}.md"
            record.write_text(
                record.read_text(encoding="utf-8").replace("23.99 EUR", "1.00 EUR"),
                encoding="utf-8",
            )

            with self.assertRaises(CONTROLLER.OrchestrationControllerError) as caught:
                self.verify_delegated(target, self.hydrated_pending_order(target))

            failure = caught.exception.details["reconciliation_failures"][0]
            self.assertEqual(source_id, failure["source_id"])
            self.assertTrue(failure["was_authorized_unnormalized"])
            self.assertTrue(failure["record_unchanged"])
            self.assertEqual(
                "normalized evidence is not what normalizing the raw evidence produces",
                failure["derivation_failure"]["reason"],
            )

    def test_a_crashing_re_derivation_is_not_sent_to_the_rewrite_that_crashed(self):
        """The same defect one layer inside the constant written to stop it.

        `normalized_output_derivation_failure` ends in `except (Exception, SystemExit)`, so a
        re-derivation that raises anything unclassified reports "normalized evidence could not
        be re-derived from the raw evidence". That reason was the one crash verdict missing
        from `UNVERIFIABLE_DERIVATION_REASONS`, which meant the arm-(b) rewrite claimed it:
        `normalize_sources.py --source-id <id> --force` re-enters the normalizer that had just
        raised, so the operator who followed the advice reached the same wall from the other
        side.

        One broken normalizer, observed through both doors. The verifier's re-derivation is
        refused, and the rewrite it used to advise is run and refused too — which is the whole
        argument for routing this verdict to the repair that tells the operator to fix the
        host instead.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            request_id = self.block_question(target)
            source_id = self.deliver_for_request(target, request_id, normalize=False)
            self.delegated_session(target)
            code, _, stderr = self.controller(target, "next", "--orchestration-id", "orch-test")
            self.assertEqual(0, code, stderr)
            self.assert_json_script_ok(
                NORMALIZE, ["--project-root", str(target), "--all", "--format", "json"]
            )
            self.fulfil_and_reopen(target, request_id, source_id, "test-question")

            def broken_normalizer(*_args, **_kwargs):
                raise SystemExit("the normalizer stopped partway through this source")

            # Workspace scripts load siblings by path, so the controller holds its own copy
            # of the module. Both doors are patched because both are the same broken
            # normalizer, reached from the verifier and from the CLI in turn.
            controller_copy = CONTROLLER.load_sibling_module("normalize_sources")
            with mock.patch.object(controller_copy, "normalize_selected_record", broken_normalizer):
                with self.assertRaises(CONTROLLER.OrchestrationControllerError) as caught:
                    self.verify_delegated(target, self.hydrated_pending_order(target))

            failure = caught.exception.details["reconciliation_failures"][0]
            self.assertEqual(source_id, failure["source_id"])
            self.assertTrue(failure["was_authorized_unnormalized"], failure)
            self.assertEqual(
                "normalized evidence could not be re-derived from the raw evidence",
                failure["derivation_failure"]["reason"],
                failure,
            )
            self.assertEqual(
                CONTROLLER.RECONCILIATION_ARM_REPAIRS["authorized_unnormalized_unverifiable"],
                failure["repair"],
                failure,
            )
            self.assertNotIn("--force", failure["repair"], failure)

            # The advice this verdict used to carry, run against the same broken normalizer.
            with mock.patch.object(NORMALIZE, "normalize_selected_record", broken_normalizer):
                rewrite_code, rewrite_error, _ = self.json_script(
                    NORMALIZE,
                    [
                        "--project-root", str(target), "--source-id", source_id,
                        "--force", "--format", "json",
                    ],
                )

            self.assertNotEqual(0, rewrite_code, rewrite_error)
            self.assertIn("stopped partway through", json.dumps(rewrite_error), rewrite_error)

    # -- delegated acquisition: blocked-path verification -----------------------------

    def test_a_blocked_delegated_action_pauses_and_replays_the_same_action(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target, _ = self.delegated_action(Path(tmpdir))

            code, session, stderr = self.submit_delegated(
                target, outcome="blocked", summary="The acquirer's connector was unavailable."
            )

            self.assertEqual(CONTROLLER.EXIT_PAUSED, code, stderr)
            self.assertEqual(CONTROLLER.PAUSED_STATUS, session["status"])
            self.assertEqual("action-0001", session["pending_action_id"])

            code, replayed, stderr = self.controller(
                target, "next", "--orchestration-id", "orch-test", "--resume"
            )
            self.assertEqual(0, code, stderr)
            self.assertEqual("action-0001", replayed["action_id"])
            self.assertEqual("delegated", replayed["acquisition_mode"])

    def test_a_blocked_delegated_action_that_fulfilled_a_request_is_refused(self):
        # CR-3's own acceptance case, in the delegated shape: a blocked attempt cannot
        # fulfil a request. The work actually done must be reported as completed.
        with tempfile.TemporaryDirectory() as tmpdir:
            target, request_id = self.delegated_action(Path(tmpdir))
            source_id = self.deliver_for_request(target, request_id)
            self.fulfil_and_reopen(target, request_id, source_id, "test-question")

            code, error, _ = self.submit_delegated(target, outcome="blocked", summary="Aborted.")

            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("ORCHESTRATION_POSTCONDITION_FAILED", error["error_code"])
            self.assertIn("changed the source-request store", error["message"])
            self.assertIn("cannot fulfill a request", error["remediation"])

    def test_a_blocked_delegated_action_that_recorded_an_attempt_is_refused(self):
        # Recording a failure is the delegated way of saying "this request produced
        # nothing" — durable evidence that the action ran, which is `completed`.
        with tempfile.TemporaryDirectory() as tmpdir:
            target, request_id = self.delegated_action(Path(tmpdir))
            self.record_attempt(
                target, request_id, code="provider_throttled", session="orch-test", action="action-0001"
            )

            code, error, _ = self.submit_delegated(target, outcome="blocked", summary="Aborted.")

            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertIn("recorded an acquisition attempt", error["message"])
            self.assertIn("report it as completed", error["remediation"])

    def test_a_blocked_delegated_action_that_inventoried_a_delivery_is_refused(self):
        # Strict no-change, unlike the provider path's correlated partial-delivery
        # allowance: a delegate holding delivered evidence can fulfil it instead.
        with tempfile.TemporaryDirectory() as tmpdir:
            target, request_id = self.delegated_action(Path(tmpdir))
            self.deliver_for_request(target, request_id)

            code, error, _ = self.submit_delegated(target, outcome="blocked", summary="Aborted.")

            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertIn("changed the evidence manifest", error["message"])

    def test_a_blocked_delegated_action_that_only_wrote_raw_files_is_refused(self):
        # Delivered but not inventoried: the manifest and normalized trees are untouched,
        # so this is the case that exercises the raw-tree check on its own.
        with tempfile.TemporaryDirectory() as tmpdir:
            target, request_id = self.delegated_action(Path(tmpdir))
            path = target / "raw" / "papers" / "half-delivered.html"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("<html><body>A partial delivery.</body></html>\n", encoding="utf-8")

            code, error, _ = self.submit_delegated(target, outcome="blocked", summary="Aborted.")

            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertIn("changed raw evidence", error["message"])
            self.assertIn(
                "raw/papers/half-delivered.html",
                error["details"]["raw_scope_violations"]["added_outside_scope"],
            )

    def test_a_delegated_blocked_order_carrying_a_candidate_scope_is_refused(self):
        # Defensive: C2 never emits one, so this is reachable only through a tampered or
        # hand-built order — which is exactly when a candidate-shaped check must not run.
        with tempfile.TemporaryDirectory() as tmpdir:
            target, _ = self.delegated_action(Path(tmpdir))
            order = self.hydrated_order(
                target,
                CONTROLLER.load_json_object(
                    CONTROLLER.work_order_path(target, "orch-test", "action-0001"),
                    error_code="WORK_ORDER_INVALID",
                    label="work order",
                ),
            )
            order["scope"]["candidate_ids"] = ["cand-smuggled"]

            with self.assertRaises(CONTROLLER.OrchestrationControllerError) as caught:
                CONTROLLER.verify_blocked_delegated_acquisition_postconditions(target, order)

            self.assertIn("carries a candidate scope", str(caught.exception))

    def test_a_blocked_delegated_action_that_touched_a_question_is_refused(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target, _ = self.delegated_action(Path(tmpdir))
            question = target / "wiki" / "questions" / "test-question.md"
            question.write_text(
                question.read_text(encoding="utf-8") + "\nAn out-of-band note.\n", encoding="utf-8"
            )

            code, error, _ = self.submit_delegated(target, outcome="blocked", summary="Aborted.")

            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertIn("changed question files", error["message"])

    def test_a_blocked_delegated_action_that_touched_the_candidate_store_is_refused(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target, request_id = self.delegated_action(Path(tmpdir))
            self.append_selected_acquisition_candidates(target, request_id, ["cand-invented"])

            code, error, _ = self.submit_delegated(target, outcome="blocked", summary="Aborted.")

            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertIn("changed candidate records", error["message"])

    def test_the_provider_blocked_path_still_allows_its_candidate_route_failure(self):
        # The delegated branch returns before the provider classifier; this pins that a
        # provider order still reaches it and still refuses a candidate-free scope.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.enable_academic_providers(target)
            request_id = self.block_question(target)
            self.append_selected_acquisition_candidates(target, request_id, ["cand-provider"])
            self.start(target)
            code, order, stderr = self.controller(target, "next", "--orchestration-id", "orch-test")
            self.assertEqual(0, code, stderr)
            self.assertEqual("acquisition", order["phase"])
            self.assertNotIn("acquisition_mode", order)

            result_path = target / "provider-result.json"
            result_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "action_id": order["action_id"],
                        "outcome": "blocked",
                        "summary": "The provider was unavailable.",
                        "artifacts": [],
                    }
                ),
                encoding="utf-8",
            )
            code, session, stderr = self.controller(
                target,
                "submit",
                "--orchestration-id", "orch-test",
                "--action-id", order["action_id"],
                "--result-file", str(result_path),
            )

            self.assertEqual(CONTROLLER.EXIT_PAUSED, code, stderr)
            self.assertEqual(CONTROLLER.PAUSED_STATUS, session["status"])

    # -- delegated acquisition: the out-of-band gate (CR AC3) -------------------------

    def try_fulfil(self, target: Path, request_id: str, source_id: str) -> tuple:
        return self.json_script(
            SOURCE_REQUESTS,
            [
                "--project-root", str(target), "fulfill",
                "--request-id", request_id, "--source-id", source_id, "--format", "json",
            ],
        )

    def try_reopen(self, target: Path, slug: str, request_id: str, source_id: str) -> tuple:
        return self.json_script(
            RESOLVE,
            [
                "--project-root", str(target), "reopen", "--slug", slug,
                "--agent-id", "acquirer-1", "--source-id", source_id,
                "--request-id", request_id, "--format", "json",
            ],
        )

    def test_fulfilling_out_of_band_during_a_live_session_is_refused(self):
        # CR AC3: with delegation on, a direct fulfil against a request scoped to an active
        # session is refused. Here the session's pending order is a *research* order, which
        # sanctions questions but never a fulfilment.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            request_id = self.block_question(target)
            self.add_questions(
                root,
                target,
                [{"id": "second-question", "question": "Something answerable.", "priority": "high"}],
            )
            self.delegated_session(target)
            code, order, stderr = self.controller(target, "next", "--orchestration-id", "orch-test")
            self.assertEqual(0, code, stderr)
            self.assertEqual("research", order["phase"], "an actionable question routes to research first")
            source_id = self.deliver_for_request(target, request_id)

            code, error, _ = self.try_fulfil(target, request_id, source_id)

            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("SOURCE_REQUEST_FULFILL_DELEGATED", error["error_code"])
            self.assertEqual(
                [{"orchestration_id": "orch-test", "pending_action_id": order["action_id"]}],
                error["details"]["live_sessions"],
            )
            self.assertIn("while executing the delegated acquisition", error["remediation"])

    def test_fulfilling_inside_the_delegated_order_that_scopes_it_succeeds(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target, request_id = self.delegated_action(Path(tmpdir))
            source_id = self.deliver_for_request(target, request_id)

            code, report, stderr = self.try_fulfil(target, request_id, source_id)

            self.assertEqual(0, code, stderr)
            self.assertTrue(report["updated"])

    def test_fulfilling_with_every_session_terminal_succeeds(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            request_id = self.block_question(target)
            self.delegated_session(target)
            session_path = CONTROLLER.session_path(target, "orch-test")
            session = json.loads(session_path.read_text(encoding="utf-8"))
            session["status"] = "no_ship"
            session["pending_action_id"] = None
            session_path.write_text(json.dumps(session, indent=2) + "\n", encoding="utf-8")
            source_id = self.deliver_for_request(target, request_id)

            code, report, stderr = self.try_fulfil(target, request_id, source_id)

            self.assertEqual(0, code, stderr)
            self.assertTrue(report["updated"])

    def test_a_providers_workspace_is_never_gated(self):
        # Backward compatibility: without the orchestration: section nothing changes, even
        # with a live session holding a pending order.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            request_id = self.block_question(target)
            self.enable_academic_providers(target)
            self.append_selected_acquisition_candidates(target, request_id, ["cand-provider"])
            self.start(target)
            self.controller(target, "next", "--orchestration-id", "orch-test")
            source_id = self.deliver_for_request(target, request_id)

            code, report, stderr = self.try_fulfil(target, request_id, source_id)

            self.assertEqual(0, code, stderr)
            self.assertTrue(report["updated"])

    def test_an_idempotent_refulfil_is_never_gated(self):
        # A repeat with the same source id changes nothing, so refusing it would break a
        # delegate replaying its own action after an interruption.
        with tempfile.TemporaryDirectory() as tmpdir:
            target, request_id = self.delegated_action(Path(tmpdir))
            source_id = self.deliver_for_request(target, request_id)
            self.try_fulfil(target, request_id, source_id)
            self.controller(target, "submit", "--orchestration-id", "orch-test", "--action-id", "action-0001",
                            "--result-file", self.blocked_result_file(target, "action-0001"))

            code, report, stderr = self.try_fulfil(target, request_id, source_id)

            self.assertEqual(0, code, stderr)
            self.assertFalse(report["updated"], "a same-source refulfil is a no-op")

    def test_recording_an_attempt_out_of_band_is_refused(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            request_id = self.block_question(target)
            self.delegated_session(target)

            code, error, _ = self.json_script(
                SOURCE_REQUESTS,
                [
                    "--project-root", str(target), "record-attempt-failure",
                    "--request-id", request_id, "--failure-code", "no_result",
                    "--orchestration-id", "orch-test", "--action-id", "action-0001", "--format", "json",
                ],
            )

            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("SOURCE_REQUEST_FULFILL_DELEGATED", error["error_code"])

    def test_reopening_out_of_band_during_a_live_session_is_refused(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target, request_id = self.delegated_action(Path(tmpdir))
            source_id = self.deliver_for_request(target, request_id)
            self.try_fulfil(target, request_id, source_id)
            # End the action, leaving the session live with no pending order.
            self.controller(target, "submit", "--orchestration-id", "orch-test", "--action-id", "action-0001",
                            "--result-file", self.blocked_result_file(target, "action-0001"))
            session_path = CONTROLLER.session_path(target, "orch-test")
            session = json.loads(session_path.read_text(encoding="utf-8"))
            session["pending_action_id"] = None
            session_path.write_text(json.dumps(session, indent=2) + "\n", encoding="utf-8")

            code, error, _ = self.try_reopen(target, "test-question", request_id, source_id)

            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("QUESTION_REOPEN_DELEGATED", error["error_code"])
            self.assertIn("while executing the work order that scopes it", error["remediation"])

    def test_reopening_inside_an_order_that_scopes_the_question_succeeds(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target, request_id = self.delegated_action(Path(tmpdir))
            source_id = self.deliver_for_request(target, request_id)
            self.try_fulfil(target, request_id, source_id)

            code, report, stderr = self.try_reopen(target, "test-question", request_id, source_id)

            self.assertEqual(0, code, stderr)
            self.assertEqual("open", report["status"])

    def test_a_delegated_order_sanctions_only_the_requests_it_scopes(self):
        # A pending delegated acquisition order is not blanket permission: fulfilling a
        # request it did not scope is still a mutation nothing accounts for.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.add_questions(
                root,
                target,
                [{"id": "second-question", "question": "What else is missing?", "priority": "high"}],
            )
            scoped = self.block_question(target)
            unscoped = self.block_question(target, slug="second-question")
            # Exhausting one request keeps it out of the order's scope while leaving it open.
            for index in range(2):
                self.record_attempt(
                    target, unscoped, code="provider_throttled", session="orch-test", action=f"earlier-{index}"
                )
            self.delegated_session(target)
            code, order, stderr = self.controller(target, "next", "--orchestration-id", "orch-test")
            self.assertEqual(0, code, stderr)
            self.assertEqual([scoped], order["scope"]["request_ids"])
            source_id = self.deliver_for_request(target, unscoped, name="unscoped-evidence")

            code, error, _ = self.try_fulfil(target, unscoped, source_id)

            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("SOURCE_REQUEST_FULFILL_DELEGATED", error["error_code"])

    def test_a_malformed_delegation_section_closes_the_gate(self):
        # Reading a broken declaration as "not delegated" would silently reopen the
        # out-of-band path this gate exists to close.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            request_id = self.block_question(target)
            self.delegated_session(target)
            source_id = self.deliver_for_request(target, request_id)
            config_path = target / "research.yml"
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config["orchestration"] = {"acquisition": "delegated", "acquirer_agent_id": ""}
            config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

            code, error, _ = self.try_fulfil(target, request_id, source_id)

            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("CONFIG_INVALID", error["error_code"])
            self.assertIn("acquirer_agent_id", error["message"])

    def test_an_unreadable_session_closes_the_gate_rather_than_opening_it(self):
        # Corruption is not evidence that a mutation is sanctioned.
        with tempfile.TemporaryDirectory() as tmpdir:
            target, request_id = self.delegated_action(Path(tmpdir))
            source_id = self.deliver_for_request(target, request_id)
            CONTROLLER.session_path(target, "orch-test").write_text("{not json\n", encoding="utf-8")

            code, error, _ = self.try_fulfil(target, request_id, source_id)

            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("ORCHESTRATION_STATE_UNREADABLE", error["error_code"])

    def blocked_result_file(self, target: Path, action_id: str) -> str:
        path = target / f"blocked-{action_id}.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "action_id": action_id,
                    "outcome": "completed",
                    "summary": "Delivered the scoped evidence.",
                    "artifacts": [],
                }
            ),
            encoding="utf-8",
        )
        return str(path)

    # -- delegated acquisition: surfacing (status, lint, doctor) ----------------------

    def lint_report(self, target: Path) -> dict:
        return LINT.run_checks(target, LINT.load_config(target))

    def lint_categories(self, target: Path, prefix: str) -> list[str]:
        return sorted(
            issue["category"]
            for issue in self.lint_report(target)["issues"]
            if issue["category"].startswith(prefix)
        )

    def test_status_surfaces_the_sessions_acquisition_posture(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.delegated_session(target)

            summary = self.assert_json_script_ok(
                STATUS, ["--project-root", str(target), "--format", "json"]
            )["orchestration"]

            self.assertEqual("delegated", summary["acquisition_mode"])
            self.assertEqual("acquirer-1", summary["acquirer_agent_id"])

            # The MCP read surface returns the same document, so every other service sees
            # the posture too. Asserted rather than assumed, since a future filter there
            # would silently drop it.
            mcp = load_script_module("orchestration_serve_mcp", SCRIPTS / "serve_mcp.py")
            payload = mcp.ResearchWikiMcpServer(target).call_tool_payload("workspace_status", {})
            self.assertEqual("delegated", payload["orchestration"]["acquisition_mode"])
            self.assertEqual("acquirer-1", payload["orchestration"]["acquirer_agent_id"])

    def test_status_reports_a_pre_delegation_session_as_providers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.start(target)
            session_path = CONTROLLER.session_path(target, "orch-test")
            session = json.loads(session_path.read_text(encoding="utf-8"))
            for field in ("acquisition_mode", "acquirer_agent_id"):
                session.pop(field)
            session_path.write_text(json.dumps(session, indent=2) + "\n", encoding="utf-8")

            summary = self.assert_json_script_ok(
                STATUS, ["--project-root", str(target), "--format", "json"]
            )["orchestration"]

            self.assertEqual("providers", summary["acquisition_mode"])
            self.assertIsNone(summary["acquirer_agent_id"])

    def test_lint_reports_a_fulfilment_no_work_order_accounts_for(self):
        # The residue the D3 gate cannot close: the gate only refuses while a session is
        # live, so a fulfilment recorded with none running leaves exactly this trace.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            request_id = self.block_question(target)
            self.declare_delegation(target)
            source_id = self.deliver_for_request(target, request_id)
            self.assert_json_script_ok(
                SOURCE_REQUESTS,
                [
                    "--project-root", str(target), "fulfill",
                    "--request-id", request_id, "--source-id", source_id, "--format", "json",
                ],
            )

            report = self.lint_report(target)

            categories = [issue["category"] for issue in report["issues"]]
            self.assertIn("delegated_fulfilment_unattributed", categories)
            self.assertEqual(1, report["stats"]["delegated_unattributed_fulfilments"])
            offender = next(
                issue for issue in report["issues"]
                if issue["category"] == "delegated_fulfilment_unattributed"
            )
            self.assertEqual("LOW", offender["severity"])
            self.assertIn(request_id, offender["message"])

    def test_lint_stays_quiet_when_the_work_order_accounts_for_the_fulfilment(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target, request_id = self.delegated_action(Path(tmpdir))
            source_id = self.deliver_for_request(target, request_id)
            self.fulfil_and_reopen(target, request_id, source_id, "test-question")

            report = self.lint_report(target)

            self.assertEqual([], self.lint_categories(target, "delegated_"))
            self.assertEqual(0, report["stats"]["delegated_unattributed_fulfilments"])

    def test_lint_does_not_gate_a_providers_workspace(self):
        # The checks are delegation-specific; a workspace acquiring through its own
        # providers must not grow findings about work orders it never issues.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            request_id = self.block_question(target)
            source_id = self.deliver_for_request(target, request_id)
            self.assert_json_script_ok(
                SOURCE_REQUESTS,
                [
                    "--project-root", str(target), "fulfill",
                    "--request-id", request_id, "--source-id", source_id, "--format", "json",
                ],
            )

            report = self.lint_report(target)

            self.assertEqual([], self.lint_categories(target, "delegated_"))
            self.assertNotIn("delegated_unattributed_fulfilments", report["stats"])

    def test_lint_reports_an_attempt_event_for_a_request_the_store_lost(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            request_id = self.block_question(target)
            self.declare_delegation(target)
            self.record_attempt(
                target, request_id, code="no_result", session="orch-gone", action="action-0001"
            )
            store = target / "sources" / "source-requests.jsonl"
            store.write_text("", encoding="utf-8")

            report = self.lint_report(target)

            offender = next(
                issue for issue in report["issues"]
                if issue["category"] == "source_request_attempt_orphaned"
            )
            self.assertEqual("LOW", offender["severity"])
            self.assertIn(request_id, offender["message"])
            self.assertEqual(1, report["stats"]["source_request_attempt_orphans"])

    def test_lint_warns_before_the_attempt_audit_outgrows_the_read_guard(self):
        # The warning has to arrive with room to act: past the controller's bounded read,
        # delegated acquisition stops verifying at all.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            request_id = self.block_question(target)
            self.declare_delegation(target)
            self.record_attempt(
                target, request_id, code="no_result", session="orch-1", action="action-0001"
            )
            audit = target / "sources" / "source-request-attempts.jsonl"
            line = audit.read_text(encoding="utf-8").splitlines()[0]
            with audit.open("a", encoding="utf-8") as handle:
                # Distinct event ids so the padding is a plausible audit, not one line
                # repeated; size is what the check reads, but a valid file keeps the other
                # checks meaningful.
                for index in range(LINT.ATTEMPT_AUDIT_WARNING_BYTES // len(line) + 8):
                    event = json.loads(line)
                    event["event_id"] = f"attempt-{index:032d}"
                    handle.write(json.dumps(event) + "\n")

            report = self.lint_report(target)

            offender = next(
                issue for issue in report["issues"]
                if issue["category"] == "source_request_attempt_audit_large"
            )
            self.assertEqual("LOW", offender["severity"])
            self.assertGreater(
                report["stats"]["source_request_attempt_audit_bytes"],
                LINT.ATTEMPT_AUDIT_WARNING_BYTES,
            )

    def test_the_audit_warning_threshold_leaves_room_below_the_controller_guard(self):
        # The two constants have to stay related: a warning at or above the guard would
        # arrive only once verification had already stopped working.
        self.assertLess(LINT.ATTEMPT_AUDIT_WARNING_BYTES, CONTROLLER.MAX_SCOPE_GUARD_BYTES)

    def test_doctor_reports_the_delegation_posture(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.declare_delegation(target, max_attempts_per_request=4)

            report = self.assert_json_script_ok(
                DOCTOR, ["--project-root", str(target), "--format", "json"]
            )
            check = next(item for item in report["checks"] if item["id"] == "acquisition_mode")

            self.assertEqual("ok", check["status"])
            self.assertIn("acquirer-1", check["message"])
            self.assertEqual(
                {
                    "acquisition_mode": "delegated",
                    "acquirer_agent_id": "acquirer-1",
                    "max_attempts_per_request": 4,
                },
                check["details"],
            )
            self.assertIn("Managed runners refuse this workspace", check["implication"])

    def test_doctor_reports_a_providers_workspace_as_self_acquiring(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)

            report = self.assert_json_script_ok(
                DOCTOR, ["--project-root", str(target), "--format", "json"]
            )
            check = next(item for item in report["checks"] if item["id"] == "acquisition_mode")

            self.assertEqual("ok", check["status"])
            self.assertEqual("providers", check["details"]["acquisition_mode"])
            self.assertIsNone(check["details"]["acquirer_agent_id"])

    def test_doctor_degrades_on_a_malformed_orchestration_section(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            config_path = target / "research.yml"
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config["orchestration"] = {"acquisition": "sideways"}
            config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

            report = self.assert_json_script_ok(
                DOCTOR, ["--project-root", str(target), "--format", "json"]
            )
            check = next(item for item in report["checks"] if item["id"] == "acquisition_mode")

            self.assertEqual("degraded", check["status"])
            self.assertEqual("CONFIG_INVALID", check["details"]["error_code"])

    # -- delegated acquisition: providers-mode backward compatibility ----------------

    VOLATILE_KEYS = frozenset(
        {
            "issued_at", "expires_at", "started_at", "updated_at", "completed_at",
            "window_started_at", "occurred_at", "recorded_at",
            "fingerprint", "total_bytes", "entry_count",
            "run_id", "active_run_id", "child_run_ids",
        }
    )

    def stable(self, value, *, aliases: dict[str, str] | None = None):
        """Normalize what cannot be equal across two runs, so the rest can be compared.

        Two kinds of noise. Timestamps and content fingerprints are dropped outright.
        Request ids are *substituted* rather than dropped, because where an id appears is
        exactly what the differential is checking — but a request id is a hash over its
        creation timestamp, so the literal value differs between runs that straddle a
        second boundary. Dropping them instead would have hidden a scope change; leaving
        them made the comparison flaky.
        """
        aliases = aliases or {}
        if isinstance(value, dict):
            # Keys are aliased too: baseline maps are keyed *by* request id, so
            # substituting only values would leave the two runs trivially unequal.
            return {
                self.stable(key, aliases=aliases): self.stable(item, aliases=aliases)
                for key, item in value.items()
                if key not in self.VOLATILE_KEYS
            }
        if isinstance(value, list):
            return [self.stable(item, aliases=aliases) for item in value]
        if isinstance(value, str):
            for actual, placeholder in aliases.items():
                value = value.replace(actual, placeholder)
            # A content hash over workspace bytes cannot match when those bytes
            # legitimately differ — the question file and candidate record both embed the
            # run's request id. The *key set* of a fingerprint map is what this
            # differential checks (which artifacts are baselined); the digests are not
            # comparable and are normalized rather than dropped, so a map that lost an
            # entry still fails.
            if value.startswith("sha256:"):
                return "<sha256>"
        return value

    def provider_scenario(self, root: Path, *, inert_section: bool) -> dict:
        """Drive the provider acquisition path, returning what a differential compares.

        The scenario is identical in both runs except for the presence of an inert
        `orchestration: {acquisition: providers}` section, which is the whole variable
        under test: declaring the default explicitly must change nothing.
        """
        target = self.init_workspace(root, question=True)
        self.enable_academic_providers(target)
        if inert_section:
            config_path = target / "research.yml"
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config["orchestration"] = {"acquisition": "providers"}
            config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        request_id = self.block_question(target)
        self.append_selected_acquisition_candidates(target, request_id, ["cand-differential"])

        session = self.start(target)
        code, order, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
        self.assertEqual(0, code)
        self.assertEqual("acquisition", order["phase"])

        # A completed claim with no work done, and a blocked claim with nothing changed:
        # the two submission arms this CR touched, exercised without the full acquisition.
        refused_code, refused, _ = self.submit_delegated(target, outcome="completed")
        blocked_code, blocked, _ = self.submit_delegated(
            target, outcome="blocked", summary="Nothing to do."
        )
        aliases = {request_id: "<request>"}
        return {
            "session": self.stable(session, aliases=aliases),
            "order": self.stable(order, aliases=aliases),
            "hydrated_order": self.stable(self.hydrated_order(target, order), aliases=aliases),
            "refused": (refused_code, self.stable(refused, aliases=aliases)),
            "blocked": (blocked_code, self.stable(blocked, aliases=aliases)),
            "final_session": self.stable(
                CONTROLLER.load_session(target, "orch-test"), aliases=aliases
            ),
        }

    def test_declaring_the_default_acquisition_mode_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            without = self.provider_scenario(Path(tmpdir) / "without", inert_section=False)
            with_section = self.provider_scenario(Path(tmpdir) / "with", inert_section=True)

        # Compared whole, not field by field: a differential that lists what to check
        # cannot notice something new appearing.
        self.assertEqual(without, with_section)

        # And the run is worth comparing. Two equal empty structures would satisfy the
        # assertion above, so what survived stabilization is checked explicitly.
        self.assertEqual("acquisition", without["order"]["phase"])
        self.assertEqual("research-acquire", without["order"]["skill"])
        self.assertTrue(without["order"]["scope"]["request_ids"])
        self.assertTrue(without["order"]["scope"]["candidate_ids"])
        self.assertTrue(without["hydrated_order"]["required_postconditions"])
        self.assertEqual("active", without["session"]["status"])
        self.assertNotIn("acquisition_mode", without["order"])
        self.assertNotIn("assigned_agent_id", without["order"])
        self.assertEqual(CONTROLLER.EXIT_INVALID, without["refused"][0])
        self.assertEqual(CONTROLLER.EXIT_PAUSED, without["blocked"][0])

    def test_the_volatile_key_filter_actually_drops_something(self):
        # Guards the failure mode CR-2's differential hit: a renamed key silently stops
        # being filtered, the comparison starts passing for the wrong reason, and the
        # suite goes green while comparing nothing.
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir), question=True)
            session = self.start(target)

        self.assertNotEqual(
            session, self.stable(session), "no volatile key was dropped from a session"
        )
        for key in ("started_at", "updated_at"):
            self.assertIn(key, session)
            self.assertNotIn(key, self.stable(session))

    def test_a_pre_delegation_session_and_order_still_replay_and_submit(self):
        # Artifacts written before delegated acquisition existed carry none of the new
        # fields. They must keep working, including through submission — the controller
        # reads a missing mode as `providers` and never writes the fields back.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            self.enable_academic_providers(target)
            request_id = self.block_question(target)
            self.append_selected_acquisition_candidates(target, request_id, ["cand-legacy"])
            self.start(target)
            code, order, stderr = self.controller(target, "next", "--orchestration-id", "orch-test")
            self.assertEqual(0, code, stderr)

            session_path = CONTROLLER.session_path(target, "orch-test")
            session = json.loads(session_path.read_text(encoding="utf-8"))
            for field in ("acquisition_mode", "acquirer_agent_id", "max_attempts_per_request"):
                session.pop(field)
            session_path.write_text(json.dumps(session, indent=2) + "\n", encoding="utf-8")
            order_path = CONTROLLER.work_order_path(target, "orch-test", order["action_id"])
            stored = json.loads(order_path.read_text(encoding="utf-8"))
            self.assertNotIn("acquisition_mode", stored, "a provider order never had the field")

            # Replay returns the same order.
            code, replayed, stderr = self.controller(target, "next", "--orchestration-id", "orch-test")
            self.assertEqual(0, code, stderr)
            self.assertEqual(order["action_id"], replayed["action_id"])

            # And submission works: the blocked arm accepts an untouched workspace.
            code, paused, stderr = self.submit_delegated(
                target, action_id=order["action_id"], outcome="blocked", summary="Provider offline."
            )

            self.assertEqual(CONTROLLER.EXIT_PAUSED, code, stderr)
            self.assertEqual(CONTROLLER.PAUSED_STATUS, paused["status"])
            # The controller read the session but did not backfill the new fields.
            reloaded = json.loads(session_path.read_text(encoding="utf-8"))
            self.assertNotIn("acquisition_mode", reloaded)
            self.assertEqual(
                "providers",
                CONTROLLER.session_acquisition_policy(reloaded)["acquisition_mode"],
            )

    def test_invalid_session_acquisition_fields_are_refused_field_by_field(self):
        cases = {
            "unknown mode": {"acquisition_mode": "external"},
            "control character in acquirer": {
                "acquisition_mode": "delegated",
                "acquirer_agent_id": "acq\nuirer",
            },
            "attempts below range": {"max_attempts_per_request": 0},
            "attempts above range": {
                "max_attempts_per_request": CONTROLLER.MAX_MAX_ATTEMPTS_PER_REQUEST + 1
            },
            "boolean attempts": {"max_attempts_per_request": True},
            "non-integer attempts": {"max_attempts_per_request": "2"},
        }
        for label, overrides in cases.items():
            with self.subTest(case=label):
                document = {
                    "acquisition_mode": "providers",
                    "acquirer_agent_id": None,
                    "max_attempts_per_request": 2,
                }
                document.update(overrides)
                self.assertFalse(CONTROLLER.valid_session_acquisition(document))

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


    # -- generated bytecode is not a trusted input ------------------------
    #
    # A work order tells an agent to run ``scripts/*.py`` directly -- the workflow
    # ``skills/research-run.md`` and its siblings document. Doing so makes CPython
    # write ``scripts/__pycache__/*.pyc``. Every one of those files used to land in
    # the trusted-input fingerprint, so the following ``submit`` reported the
    # workspace as tampered and listed the bytecode as the evidence. Only
    # ``orchestration_controller`` passed ``-B``, so only it escaped a trap the
    # documented workflow walked straight into.
    #
    # Both halves are pinned below. Tolerating bytecode is worth nothing if it also
    # blinded the check to a real edit, and that check exists to catch tampering.

    def issue_an_action(self, root: Path) -> tuple[Path, dict]:
        target = self.init_workspace(root, question=True)
        self.start(target)
        _, order, stderr = self.controller(target, "next", "--orchestration-id", "orch-test")
        self.assertIn("action_id", order, stderr)
        return target, order

    def write_bytecode_the_way_an_agent_does(self, target: Path) -> list[str]:
        """Import a workspace script in a child process with bytecode writing ON.

        Deliberately a subprocess without ``-B`` and without
        ``PYTHONDONTWRITEBYTECODE``: this test is about what the documented
        workflow does to the workspace, and the parent test process runs under
        pytest's own settings.
        """
        completed = subprocess.run(  # noqa: S603
            [
                sys.executable,
                "-c",
                "import sys; sys.path.insert(0, sys.argv[1]); import _script_errors, question_claim  # noqa: F401",
                str(target / "scripts"),
            ],
            capture_output=True,
            text=True,
            check=False,
            env={k: v for k, v in os.environ.items() if k != "PYTHONDONTWRITEBYTECODE"},
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        cache = target / "scripts" / "__pycache__"
        self.assertTrue(cache.is_dir(), "the documented workflow should have produced bytecode")
        written = sorted(path.name for path in cache.glob("*.pyc"))
        self.assertTrue(written, "expected at least one .pyc")
        return written

    def test_bytecode_written_by_the_documented_workflow_does_not_block_submit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target, order = self.issue_an_action(root)
            self.block_question(target)

            written = self.write_bytecode_the_way_an_agent_does(target)

            code, payload, stderr = self.submit(root, target, order["action_id"])
            self.assertEqual(
                0,
                code,
                f"submit refused after {len(written)} .pyc file(s) appeared: {payload}\n{stderr}",
            )
            self.assertEqual(1, payload["completed_action_count"])
            self.assertEqual(order["action_id"], payload["last_completed_action_id"])

    def test_a_real_script_edit_is_still_detected_alongside_the_bytecode(self):
        """The exclusion must not have blinded the check it lives inside."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target, order = self.issue_an_action(root)
            self.block_question(target)

            self.write_bytecode_the_way_an_agent_does(target)
            tampered = target / "scripts" / "lint.py"
            tampered.write_text(tampered.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")

            code, error, _ = self.submit(root, target, order["action_id"])
            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("ORCHESTRATION_TRUSTED_INPUT_CHANGED", error["error_code"])
            changed = error["details"]["changed_paths"]
            self.assertIn("scripts/lint.py [content]", changed)
            # The report names the edit and nothing else: bytecode present in the
            # same tree stays out of it, so the finding is readable.
            self.assertEqual(
                [],
                [path for path in changed if "__pycache__" in path],
                f"generated bytecode should not appear in the drift report: {changed}",
            )

    def test_the_excluded_subtree_is_still_inspected_for_unsafe_entries(self):
        """Excluded from the fingerprint is not excluded from the safety checks."""
        self.assertIn("scripts/__pycache__", CONTROLLER.TRUSTED_STATIC_EXCLUDED_SUBTREES)
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, question=True)
            cache = target / "scripts" / "__pycache__"
            cache.mkdir()
            outside = root / "outside.txt"
            outside.write_text("payload", encoding="utf-8")
            try:
                (cache / "escape.pyc").symlink_to(outside)
            except (OSError, NotImplementedError):
                self.skipTest("filesystem does not support symlinks")
            with self.assertRaises(CONTROLLER.OrchestrationControllerError) as caught:
                CONTROLLER.trusted_static_input_fingerprint(target)
            self.assertIn("symbolic link", str(caught.exception))


class NormalizedOutputScopeTests(unittest.TestCase):
    """The declared-only gate on structured-view sidecars (EW-BUG-004).

    Normalization writes the sidecar beside the record, so an acquisition that fulfils a
    structured source adds two files under the normalized root and the scope guards must
    allow both. Allowing every `.structured.json` unconditionally would be the easy fix and
    the wrong one: the controller runs no record-contract validation, so an undeclared
    sidecar would pass unremarked. These pin the gate itself, independent of any harness.
    """

    def record(self, root: Path, *, declares: bool) -> Path:
        path = root / "raw--sample-0123456789.md"
        block = (
            "structured_view:\n  path: sources/normalized/raw--sample-0123456789.structured.json\n"
            "  content_hash: sha256:0\n"
            if declares
            else ""
        )
        path.write_text(f"---\nsource_id: raw:sample\n{block}---\n\nbody\n", encoding="utf-8")
        return path

    def allowed(self, root: Path, record_path: Path) -> set[str]:
        normalize_sources = CONTROLLER.load_sibling_module("normalize_sources")
        return CONTROLLER.allowed_normalized_paths_for_record(root, record_path, normalize_sources)

    def test_a_declared_sidecar_is_allowed_beside_its_record(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            allowed = self.allowed(root, self.record(root, declares=True))
            self.assertEqual(
                {"raw--sample-0123456789.md", "raw--sample-0123456789.structured.json"},
                allowed,
            )

    def test_an_undeclared_sidecar_is_not_allowed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            allowed = self.allowed(root, self.record(root, declares=False))
            self.assertEqual({"raw--sample-0123456789.md"}, allowed)

    def test_an_unreadable_record_authorizes_nothing_beside_itself(self):
        """Fails closed: a record the guard cannot parse cannot widen the guard."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            missing = root / "raw--absent-0000000000.md"
            self.assertEqual({"raw--absent-0000000000.md"}, self.allowed(root, missing))

            malformed = root / "raw--broken-0000000000.md"
            malformed.write_text("no frontmatter here\n", encoding="utf-8")
            self.assertEqual({"raw--broken-0000000000.md"}, self.allowed(root, malformed))

    def test_every_normalized_scope_site_shares_the_one_allowance(self):
        """Three postcondition paths answer this question; none may answer it alone.

        `verify_delegated_acquisition_postconditions`, `verify_action_postconditions` and
        `verify_blocked_action_postconditions` each bound what an action may add under the
        normalized root. EW-BUG-004 was one rule written three times and updated in none of
        them, so the rule now lives in one helper and this fails if a site stops using it.
        """
        tree = ast.parse(Path(CONTROLLER.__file__).read_text(encoding="utf-8"))
        wanted = {
            "verify_delegated_acquisition_postconditions",
            "verify_action_postconditions",
            "verify_blocked_action_postconditions",
        }
        shared = {"allowed_normalized_paths_for_record", "normalized_output_scope"}
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or node.name not in wanted:
                continue
            called = {
                getattr(call.func, "id", None) or getattr(call.func, "attr", None)
                for call in ast.walk(node)
                if isinstance(call, ast.Call)
            }
            with self.subTest(function=node.name):
                self.assertTrue(
                    called & shared,
                    f"{node.name} bounds normalized outputs without the shared allowance",
                )
            wanted = wanted - {node.name}
        self.assertEqual(set(), wanted, f"postcondition functions not found: {sorted(wanted)}")

    def test_both_acquisition_arms_hold_reuse_to_the_same_shared_terms(self):
        """The symmetry the two arms promise, asserted where it can actually be broken.

        `workspace-template/docs/orchestration.md` states that the two arms admit reuse on
        the same terms, and the delegated verifier's own docstring counts the differences
        between them. Both claims stay true only while the reuse decision lives in shared
        helpers, so this fails if either arm grows its own copy.
        """
        tree = ast.parse(Path(CONTROLLER.__file__).read_text(encoding="utf-8"))
        wanted = {"verify_delegated_acquisition_postconditions", "verify_action_postconditions"}
        shared = {
            "preexisting_reuse_scope_failures",
            "reused_source_reconciliation_failure",
            "valid_unnormalized_reuse_baseline",
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or node.name not in wanted:
                continue
            called = {
                getattr(call.func, "id", None) or getattr(call.func, "attr", None)
                for call in ast.walk(node)
                if isinstance(call, ast.Call)
            }
            with self.subTest(function=node.name):
                self.assertEqual(
                    shared,
                    shared & called,
                    f"{node.name} decides reuse without one of the shared helpers",
                )
            wanted = wanted - {node.name}
        self.assertEqual(set(), wanted, f"postcondition functions not found: {sorted(wanted)}")

    def test_the_reuse_refusal_speaks_before_the_guards_that_would_mask_it(self):
        """Call-presence is not parity; order is, and only one arm had it.

        Two of the three causes the reuse refusal reports are also visible to later guards:
        a sidecar naming no scoped request is refused by candidate correlation, and a record
        rewritten since issuance is refused by the manifest-scope guard. Whichever guard
        speaks first is the one the acquirer acts on, and only the reuse refusal names reuse,
        says which cause applies to which source, and carries that cause's repair. Ordered
        after them, its remediation is dead text for two of the three states it describes.

        The sibling parity test walks calls and cannot see this: both arms called the helper,
        and the provider arm called it roughly 190 lines too late.
        """
        tree = ast.parse(Path(CONTROLLER.__file__).read_text(encoding="utf-8"))
        expected_order = ("reuse_scope_failures", "correlation_failures", "manifest_scope_violations")
        for name in ("verify_delegated_acquisition_postconditions", "verify_action_postconditions"):
            function = next(
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef) and node.name == name
            )
            first_line: dict[str, int] = {}
            for statement in ast.walk(function):
                # Annotated assignments count too: one arm seeds its correlation list with
                # `name: list[...] = []`, and a walk that saw only plain assignments would
                # report that guard as absent rather than as ordered.
                if isinstance(statement, ast.AnnAssign):
                    targets = [statement.target]
                elif isinstance(statement, ast.Assign):
                    targets = list(statement.targets)
                else:
                    continue
                for target in targets:
                    if isinstance(target, ast.Name) and target.id in expected_order:
                        first_line.setdefault(target.id, statement.lineno)
            with self.subTest(function=name):
                self.assertEqual(
                    set(expected_order),
                    set(first_line),
                    f"{name} no longer computes one of the guards whose order is asserted here",
                )
                self.assertEqual(
                    list(expected_order),
                    sorted(first_line, key=lambda key: first_line[key]),
                    f"{name} orders the reuse refusal behind a guard that masks two of its causes",
                )

    def test_the_reconciliation_and_missing_record_advice_names_only_selectors_that_work(self):
        """A remediation that is refused for being followed is the defect, not the fix.

        `normalize_sources.py` with neither selector normalizes the pending set, which for a
        hand-edited record is empty; `--all` considers every eligible record in the workspace
        and an acquisition order authorizes output for the sources it scopes and nothing else,
        so following it lands on `unexpected_new_normalized` or on a scope guard whose
        `mutable_ids` is empty. `--source-id` is the selector these refusals can honestly name.

        The reconciliation pair no longer names a selector itself. The rewrite it used to
        advise is the recourse for one of its two arms only, so it moved to that arm's entry
        in `RECONCILIATION_ARM_REPAIRS` and the shared terms point at it. What is asserted
        here is unchanged in substance: wherever the rewrite *is* named it still carries the
        selector that works, and `--all` is named nowhere.
        """
        arm_repairs = CONTROLLER.RECONCILIATION_ARM_REPAIRS
        for constant in (
            CONTROLLER.MISSING_NORMALIZED_REMEDIATION,
            arm_repairs["authorized_unnormalized"],
        ):
            with self.subTest(advice=constant[:60]):
                self.assertIn("--source-id", constant)
        for constant in (
            CONTROLLER.RECONCILIATION_REMEDIATION,
            CONTROLLER.PROVIDER_RECONCILIATION_REMEDIATION,
            CONTROLLER.MISSING_NORMALIZED_REMEDIATION,
            *arm_repairs.values(),
        ):
            with self.subTest(advice=constant[:60]):
                self.assertNotIn("--all", constant)

        # Same split as the reuse terms, and now for the same reason: this refusal is also
        # computed over the acquirer's fulfilled list, so neither arm has a second route and
        # each names only the dead end its own reader would otherwise reach for.
        self.assertTrue(CONTROLLER.RECONCILIATION_REMEDIATION.startswith(CONTROLLER.RECONCILIATION_TERMS))
        self.assertTrue(
            CONTROLLER.PROVIDER_RECONCILIATION_REMEDIATION.startswith(CONTROLLER.RECONCILIATION_TERMS)
        )
        self.assertNotIn("record-attempt-failure", CONTROLLER.RECONCILIATION_REMEDIATION)
        self.assertNotIn("record-attempt-failure", CONTROLLER.PROVIDER_RECONCILIATION_REMEDIATION)
        self.assertIn("another selected candidate", CONTROLLER.PROVIDER_RECONCILIATION_REMEDIATION)
        self.assertNotIn("another selected candidate", CONTROLLER.RECONCILIATION_REMEDIATION)
        for constant in (
            CONTROLLER.RECONCILIATION_REMEDIATION,
            CONTROLLER.PROVIDER_RECONCILIATION_REMEDIATION,
        ):
            with self.subTest(advice=constant[-60:]):
                self.assertIn(CONTROLLER.NO_SECOND_ROUTE_FOR_A_FULFILLED_REQUEST, constant)

    def test_every_reuse_scope_cause_carries_a_repair_that_is_not_the_shared_terms(self):
        """Each cause has its own repair, because they do not share one.

        The single sentence that used to serve all three told the acquirer to reuse only a
        source correlated to this request and unchanged since — which for
        `no_reuse_authorization_at_issuance` describes exactly what the refused source
        already is. Advice that restates the state being refused is advice with no next step
        in it.
        """
        repairs = CONTROLLER.REUSE_SCOPE_CAUSE_REPAIRS
        self.assertEqual(
            {
                "provenance_names_no_scoped_request",
                "manifest_record_changed_after_issuance",
                "no_reuse_authorization_at_issuance",
            },
            set(repairs),
        )
        self.assertEqual(len(repairs), len(set(repairs.values())), "two causes share one repair")
        for cause, repair in repairs.items():
            with self.subTest(cause=cause):
                self.assertNotIn(CONTROLLER.REUSE_SCOPE_TERMS, repair)
        self.assertIn("nothing done inside the order can add one", repairs["no_reuse_authorization_at_issuance"])

    def test_only_the_reconciliation_repair_that_can_be_followed_names_the_rewrite(self):
        """The same defect as the reuse causes above, one guard over.

        Reconciliation holds a reused source to one of two arms and used to attach one shared
        remediation to both. That remediation told the acquirer to rewrite the record with
        `normalize_sources.py --source-id <id> --force`, which is the recourse for the arm
        where the order recorded no normalized output — and is unfollowable for the arm where
        it recorded one, because every run restamps the second-resolution `normalized_at` that
        the fingerprint covers. Advice that cannot be followed is worse than no advice: it
        reads as a repair and returns the identical refusal.

        The rewrite is not even the recourse for every failure of the arm it belongs to. A
        re-derivation that could not be *performed* — the adapter would not run, the sandbox
        would not build, the source normalizes from inputs no baseline pins — is refused
        before the record's bytes are compared, so rewriting them changes nothing and lands
        on the same refusal. That state gets its own repair rather than the rewrite.
        """
        repairs = CONTROLLER.RECONCILIATION_ARM_REPAIRS
        self.assertEqual(
            {"scoped_match", "authorized_unnormalized", "authorized_unnormalized_unverifiable"},
            set(repairs),
        )
        self.assertEqual(len(repairs), len(set(repairs.values())), "two states share one repair")
        for arm, repair in repairs.items():
            with self.subTest(arm=arm):
                self.assertNotIn(CONTROLLER.RECONCILIATION_TERMS, repair)

        # The rewrite belongs to exactly one of the three, and the terms must send the reader
        # to the per-failure repair rather than carrying any of their answers themselves.
        self.assertIn("--force", repairs["authorized_unnormalized"])
        for arm in ("scoped_match", "authorized_unnormalized_unverifiable"):
            with self.subTest(arm=arm):
                self.assertNotIn("--force", repairs[arm])
                self.assertNotIn("normalize_sources.py", repairs[arm])
        self.assertIn("normalized_at", repairs["scoped_match"])
        self.assertIn("repair", CONTROLLER.RECONCILIATION_TERMS)
        self.assertNotIn("--force", CONTROLLER.RECONCILIATION_TERMS)

    #: Every string a reused source's refusal can print at an operator: the four
    #: remediations the two guards attach, plus the per-source repair each carries.
    def reuse_path_advice(self) -> dict[str, str]:
        return {
            "RECONCILIATION_REMEDIATION": CONTROLLER.RECONCILIATION_REMEDIATION,
            "PROVIDER_RECONCILIATION_REMEDIATION": CONTROLLER.PROVIDER_RECONCILIATION_REMEDIATION,
            "REUSE_SCOPE_REMEDIATION": CONTROLLER.REUSE_SCOPE_REMEDIATION,
            "PROVIDER_REUSE_SCOPE_REMEDIATION": CONTROLLER.PROVIDER_REUSE_SCOPE_REMEDIATION,
            **{f"REUSE_SCOPE_CAUSE_REPAIRS[{key!r}]": value for key, value in CONTROLLER.REUSE_SCOPE_CAUSE_REPAIRS.items()},
            **{f"RECONCILIATION_ARM_REPAIRS[{key!r}]": value for key, value in CONTROLLER.RECONCILIATION_ARM_REPAIRS.items()},
        }

    def test_no_reuse_advice_names_a_command_a_fulfilled_request_refuses(self):
        """The defect one level up from "the selector does not work": the *state* forbids it.

        Every string swept here is printed from a guard whose input is the acquirer's
        `fulfilled` list, so the request it advises about is already fulfilled by the source
        being refused, and `source_requests.py` closes both doors out of that state:
        `record-attempt-failure` refuses a fulfilled request, and `fulfill` refuses to
        relink one to a second delivery. Four of these strings used to send the operator
        through one of those doors anyway — two at `record-attempt-failure`, two at
        "deliver that evidence again as a new source under its own raw path".

        Naming a second delivery is not itself the defect; presenting it as a way out of
        *this* order is. So the sweep is not "never say raw path" — that would push the
        useful pointer at a later order out of the text too — but "wherever a second
        delivery or a second candidate is named, say in the same breath that this request is
        already fulfilled and cannot be relinked to it".

        `following_the_reuse_refusals_escapes_is_refused` walks the two doors against the
        real scripts; this pins the text so it cannot drift back on its own.
        """
        for name, advice in self.reuse_path_advice().items():
            with self.subTest(constant=name):
                self.assertNotIn(
                    "record-attempt-failure",
                    advice,
                    "this advice is printed only for a request that is already fulfilled",
                )
                if "raw path" in advice or "another selected candidate" in advice:
                    self.assertTrue(
                        "already fulfilled" in advice or "cannot be relinked" in advice,
                        "a second delivery is named without saying this request cannot take one",
                    )

        # The same sentence lived inline in the delegated correlation refusal, which reads
        # the fulfilled list too. It is not a constant, so it is pinned by absence.
        self.assertNotIn(
            "deliver that evidence as a new source under its own raw path instead",
            Path(CONTROLLER.__file__).read_text(encoding="utf-8"),
            "a guard over fulfilled requests still advises a delivery that cannot be linked to one",
        )

    def test_the_scoped_match_repair_names_what_the_failed_outcome_costs(self):
        """The advice that was not refused, and was worse than the advice that was.

        This repair used to end "end the action with a failed outcome and start a new
        session". Nothing refuses that — which is the whole problem. `prepare_submission`
        answers a `failed` outcome without calling `verify_action_postconditions` at all, so
        every evidence and scope guard is skipped; `run_fulfill` has already written
        `fulfilled` and the source id into the request store and nothing rolls that back;
        and routing scopes `open_requests`, so the request is invisible to every later
        order. Following it converted a refusal into a permanent, silent acceptance of
        evidence the controller had just declined to verify.

        `the_failed_outcome_costs_exactly_what_the_repair_says` performs it and observes the
        wreckage. Here the text is pinned: the outcome may be named, but never without its
        price beside it.
        """
        repair = CONTROLLER.RECONCILIATION_ARM_REPAIRS["scoped_match"]
        self.assertIn("failed outcome", repair, "the state this repair is about is unnamed")
        for cost in (
            "without any evidence or scope check running",
            "leaves the fulfilment",
            "no later order can see",
        ):
            with self.subTest(cost=cost):
                self.assertIn(cost, repair, "the failed outcome is named without its cost")
        self.assertNotIn(
            "start a new session",
            repair,
            "a fresh session is not reachable from here without paying that price first",
        )

    def test_every_unverifiable_derivation_reason_is_one_the_verifier_actually_reports(self):
        """A reason set matched by string equality is a set that can silently stop matching.

        `UNVERIFIABLE_DERIVATION_REASONS` decides which arm-(b) failures are told the rewrite
        cannot help them, and it decides it by comparing `derivation_failure["reason"]` to
        literals. Reasons are prose rather than an enumerated contract — no constant, no doc
        row — so rewording one in `normalized_output_derivation_failure` would leave this set
        matching nothing and quietly restore the unfollowable advice for every state in it.
        Each member is therefore pinned to a literal the module still emits.
        """
        source = Path(CONTROLLER.__file__).read_text(encoding="utf-8")
        emitted = {
            node.value
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        # Guard the guard: `UNVERIFIABLE_DERIVATION_REASONS` is itself built from literals, so
        # membership alone would be satisfied by the set's own definition. Every member has to
        # appear on a `reason` key of a verdict this module returns.
        reported = {
            value.value
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Dict)
            for key, value in zip(node.keys, node.values, strict=True)
            if isinstance(key, ast.Constant)
            and key.value == "reason"
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        }
        self.assertGreater(len(reported), 10, "the reason sweep found suspiciously few verdicts")
        self.assertLessEqual(
            CONTROLLER.UNVERIFIABLE_DERIVATION_REASONS,
            reported,
            "these reasons are matched by equality but no verdict in this module reports them",
        )
        self.assertLessEqual(CONTROLLER.UNVERIFIABLE_DERIVATION_REASONS, emitted)

    def test_a_reconciliation_failure_carries_the_repair_for_the_arm_it_was_held_to(self):
        """The arm the failure reports and the repair it attaches must be the same arm.

        `was_scoped_match` had no test reference anywhere before this: the distinction the
        whole refusal turns on was asserted by nothing, so attaching the wrong arm's repair —
        or attaching one arm's to both, which is the defect being repaired — was invisible.
        Both arms are reached here through their own baseline, exactly as the postcondition
        does it: an entry in `matching_source_records_before` for the arm that was already
        normalized at issuance, membership of `reusable_source_ids_before` for the arm that
        was not. Each is failed on its manifest record alone, so no re-derivation runs and the
        arm selection is the only thing under test.
        """
        normalize_sources = CONTROLLER.load_sibling_module("normalize_sources")
        record = {"id": "raw:quote", "kind": "structured_data", "raw_paths": ["raw/data/quote.json"]}
        issued = "sha256:" + "1" * 64
        rewritten = "sha256:" + "2" * 64

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            def failure_for(*, scoped_match: bool) -> dict:
                return CONTROLLER.reused_source_reconciliation_failure(
                    root,
                    {},
                    "raw:quote",
                    record=record,
                    manifest_records=[record],
                    normalized_root=root / "sources" / "normalized",
                    matching_source_records_before=(
                        {"raw:quote": {"record_fingerprint": issued, "normalized_fingerprint": issued}}
                        if scoped_match
                        else {}
                    ),
                    reusable_source_ids_before=set() if scoped_match else {"raw:quote"},
                    manifest_records_before={"raw:quote": issued},
                    current_record_fingerprint=rewritten,
                    normalized_files_before={},
                    normalize_sources=normalize_sources,
                )

            scoped = failure_for(scoped_match=True)
            self.assertTrue(scoped["was_scoped_match"], scoped)
            self.assertFalse(scoped["was_authorized_unnormalized"], scoped)
            self.assertFalse(scoped["record_unchanged"], scoped)
            self.assertEqual(CONTROLLER.RECONCILIATION_ARM_REPAIRS["scoped_match"], scoped["repair"])

            authorized = failure_for(scoped_match=False)
            self.assertFalse(authorized["was_scoped_match"], authorized)
            self.assertTrue(authorized["was_authorized_unnormalized"], authorized)
            self.assertFalse(authorized["record_unchanged"], authorized)
            self.assertEqual(
                CONTROLLER.RECONCILIATION_ARM_REPAIRS["authorized_unnormalized"],
                authorized["repair"],
            )
            self.assertNotEqual(
                scoped["repair"],
                authorized["repair"],
                "both arms were handed the same repair, which is the defect this reports",
            )

    def test_a_record_naming_another_normalizer_is_refused_by_name_not_by_bytes(self):
        """The producer name is compared as an identity, not left to fall out of the bytes.

        `carry_version_stamps` carries `normalizer.version` from the file and deliberately
        leaves `normalizer.name` derived, so a record whose stamped name is not the one
        configured for its kind renders different bytes — and used to be reported as
        "normalized evidence is not what normalizing the raw evidence produces", whose
        remediation is about a hand-edited body. The two lines that actually disagree were
        never quoted back, so the operator had nothing to compare.

        Absent on both sides is not a disagreement: a record for a kind that stamps no
        producer block has nothing to be wrong about, and the byte comparison keeps the last
        word over everything this does not answer.
        """
        identity = CONTROLLER.normalizer_identity_failure

        self.assertIsNone(identity({"normalizer": {"name": "stub"}}, {"normalizer": {"name": "stub"}}))
        self.assertIsNone(identity({}, {}), "neither side names a producer, so neither is wrong")

        failure = identity(
            {"normalizer": {"name": "evidence-wiki-normalizer", "version": "2"}},
            {"normalizer": {"name": "retired-adapter", "version": "2"}},
        )
        self.assertEqual(
            "normalized evidence does not name the normalizer configured for its kind",
            failure["reason"],
        )
        self.assertEqual("retired-adapter", failure["normalizer"], failure)
        self.assertEqual("evidence-wiki-normalizer", failure["configured_normalizer"], failure)

        # A record whose producer block was deleted or corrupted into something that is not a
        # producer object reaches here too, and the reason has to stay true of it: it names no
        # normalizer rather than naming a different one, and `None` says exactly that instead
        # of echoing a non-name back as though the record had claimed it.
        for stamped in ({"normalizer": "stub"}, {"normalizer": None}, {}):
            with self.subTest(stamped=stamped):
                malformed = identity({"normalizer": {"name": "stub"}}, stamped)
                self.assertEqual(
                    "normalized evidence does not name the normalizer configured for its kind",
                    malformed["reason"],
                    "the reason asserts the record named something it did not name",
                )
                self.assertIsNone(malformed["normalizer"], malformed)
                self.assertEqual("stub", malformed["configured_normalizer"], malformed)

        # Versions are the half `carry_version_stamps` carries; comparing them here would
        # undo the one thing that helper exists to allow.
        self.assertIsNone(
            identity(
                {"normalizer": {"name": "stub", "version": "1.0.0"}},
                {"normalizer": {"name": "stub", "version": "9.9.9"}},
            ),
            "a host upgrade is not a forged identity",
        )


if __name__ == "__main__":
    unittest.main()
