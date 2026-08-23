import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import yaml

from tests._script_loader import load_script as load_script_module

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
PROFILE_FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "workspace-init-profile.yml"


RUN_REPORT = load_script_module("research_run_report", "run_report.py")
INIT = load_script_module("research_run_report_init", "init_research_workspace.py")
QUESTION_STATUS = load_script_module("research_run_report_status", "question_status.py")
REQUESTS = load_script_module("research_run_report_requests", "source_requests.py")
RUN_CONTROLLER = load_script_module("research_run_report_controller", "run_controller.py")

# CR-4: a domain pack may namespace its own request kinds. The run report carries the
# id through verbatim; it never interprets it.
PACK_REQUEST_KIND = "pack:market-data/supplier_quote"


class RunReportTests(unittest.TestCase):
    """E20-T04: per-run report artifact."""

    def init_workspace(self, root: Path) -> Path:
        target = root / "report-workspace"
        profile = yaml.safe_load(PROFILE_FIXTURE_PATH.read_text())
        profile["workspace_init"]["target_path"] = str(target)
        profile["workspace_init"]["questions"] = [
            {"id": "which-benchmarks", "question": "Which benchmarks matter?", "priority": "high"},
            {"id": "needs-evidence", "question": "Needs evidence?", "priority": "medium"},
        ]
        profile_path = root / "profile.yml"
        profile_path.write_text(yaml.safe_dump(profile, sort_keys=False))
        with contextlib.redirect_stdout(io.StringIO()):
            INIT.main(["--profile", str(profile_path)])
        return target

    def capture_baseline(self, target: Path, path: Path) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = QUESTION_STATUS.main(["--project-root", str(target), "--format", "json"])
        self.assertEqual(0, code)
        path.write_text(stdout.getvalue())

    def run_report(self, target: Path, baseline: Path | None, *extra: str) -> tuple[int, str, str]:
        args = ["--project-root", str(target)]
        if baseline is not None:
            args.extend(["--baseline", str(baseline)])
        args.extend(extra)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                code = RUN_REPORT.main(args)
            except SystemExit as exc:
                code = int(exc.code or 0) if isinstance(exc.code, int) else 1
        return code, stdout.getvalue(), stderr.getvalue()

    def run_controller(self, target: Path, *args: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = RUN_CONTROLLER.main(["--project-root", str(target), *args])
        return int(code or 0), stdout.getvalue(), stderr.getvalue()

    def start_run(self, target: Path, run_id: str) -> dict:
        code, stdout, stderr = self.run_controller(
            target,
            "start",
            "--run-id",
            run_id,
            "--agent-id",
            "agent-pm",
            "--format",
            "json",
        )
        self.assertEqual(0, code, stderr)
        return json.loads(stdout)

    def transition_run(self, target: Path, run_id: str, state: str) -> dict:
        code, stdout, stderr = self.run_controller(
            target,
            "transition",
            "--run-id",
            run_id,
            "--agent-id",
            "agent-pm",
            "--to-state",
            state,
            "--reason",
            f"Move to {state}.",
            "--format",
            "json",
        )
        self.assertEqual(0, code, stderr)
        return json.loads(stdout)

    def finish_run(self, target: Path, run_id: str, final_verdict: str) -> dict:
        code, stdout, stderr = self.run_controller(
            target,
            "finish",
            "--run-id",
            run_id,
            "--agent-id",
            "agent-pm",
            "--final-verdict",
            final_verdict,
            "--reason",
            f"Finished as {final_verdict}.",
            "--format",
            "json",
        )
        self.assertEqual(0, code, stderr)
        return json.loads(stdout)

    def capture_run_report_baseline(self, target: Path, path: Path, *extra: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = RUN_REPORT.main(
                ["baseline", "--project-root", str(target), "--output", str(path), *extra]
            )
        return code, stdout.getvalue(), stderr.getvalue()

    def answer_question(self, target: Path, slug: str) -> None:
        answer_dir = target / "wiki" / "synthesis"
        answer_dir.mkdir(parents=True, exist_ok=True)
        (answer_dir / "run-answer.md").write_text(
            "---\ntype: synthesis\ncreated: 2026-06-11\nupdated: 2026-06-11\n"
            "source_ids: []\nsummary: Answer summary.\n---\n\n# Answer\n\nBody.\n"
        )
        page = target / "wiki" / "questions" / f"{slug}.md"
        page.write_text(
            page.read_text().replace(
                "status: open", "status: answered\nanswer_page: ../synthesis/run-answer.md", 1
            )
        )

    def block_question_with_request(self, target: Path, slug: str) -> str:
        page = target / "wiki" / "questions" / f"{slug}.md"
        page.write_text(
            page.read_text().replace(
                "status: open",
                "status: blocked\nblocked_reason: Needs the benchmark report.",
                1,
            )
        )
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            code = REQUESTS.main(
                [
                    "--project-root", str(target),
                    "add", "--kind", "paper",
                    "--query-or-identifier", "arXiv:2601.00001",
                    "--rationale", "Blocks the question.",
                    "--question-slug", slug,
                    "--format", "json",
                ]
            )
        self.assertEqual(0, code)
        return json.loads(stdout.getvalue())["request"]["request_id"]

    def write_normalized_source(
        self,
        target: Path,
        filename: str,
        source_id: str,
        *,
        updated: str,
        normalized_at: str | None = None,
    ) -> None:
        normalized_dir = target / "sources" / "normalized"
        normalized_dir.mkdir(parents=True, exist_ok=True)
        normalized_at_line = f"normalized_at: {normalized_at}\n" if normalized_at is not None else ""
        (normalized_dir / filename).write_text(
            "---\n"
            "type: normalized_source\n"
            f"source_id: {source_id}\n"
            "title: Run report fixture\n"
            f"updated: {updated}\n"
            f"{normalized_at_line}"
            "---\n\n"
            "# Run report fixture\n\n"
            "Evidence text.\n"
        )

    def write_candidate_records(self, target: Path) -> None:
        candidate_store = target / "sources" / "discovery" / "candidates.jsonl"
        candidate_store.parent.mkdir(parents=True, exist_ok=True)
        candidate_store.write_text(
            "\n".join(
                [
                    json.dumps({"candidate_id": "cand-selected", "status": "selected"}),
                    json.dumps({"candidate_id": "cand-rejected", "status": "rejected"}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    def declare_pack_request_kind(self, target: Path) -> None:
        """Give the workspace a domain pack that declares one namespaced request kind."""
        config_path = target / "research.yml"
        config = yaml.safe_load(config_path.read_text())
        config["domain_pack"] = {
            "name": "market-data",
            "request_kinds": [
                {
                    "id": PACK_REQUEST_KIND,
                    "label": "Supplier quote",
                    "description": "Live SKU price from a named supplier.",
                }
            ],
        }
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    def write_extended_kind_requests(self, target: Path) -> list[dict]:
        """Write open requests covering a pack kind, ``structured_data``, and a scope map.

        Written as JSONL rather than through ``source_requests.py add`` so this unit
        does not depend on that command's in-flight ``--kind`` validation.
        """
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        records = [
            {
                "schema_version": "1.0",
                "request_id": "req-pack-quote",
                "kind": PACK_REQUEST_KIND,
                "query_or_identifier": "acme-widget list price",
                "rationale": "Blocks the pricing question.",
                "priority": "high",
                "question_slugs": ["needs-evidence"],
                "status": "open",
                "created_at": now,
                "updated_at": now,
                "source_id": None,
                "scope": {"facet_id": "supplier_quote", "candidate": "acme-widget"},
            },
            {
                "schema_version": "1.0",
                "request_id": "req-structured",
                "kind": "structured_data",
                "query_or_identifier": "regional price index series",
                "rationale": "Background series.",
                "priority": "medium",
                "question_slugs": [],
                "status": "open",
                "created_at": now,
                "updated_at": now,
                "source_id": None,
            },
        ]
        path = target / "sources" / "source-requests.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
            encoding="utf-8",
        )
        return records

    def test_baseline_carries_pack_and_structured_data_kinds_verbatim(self):
        """CR-4: the baseline snapshots whole request records, so kind and scope survive."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            self.declare_pack_request_kind(target)
            self.write_extended_kind_requests(target)

            baseline = root / "run-baseline.json"
            code, _, stderr = self.capture_run_report_baseline(target, baseline)
            self.assertEqual(0, code, stderr)

            raw = baseline.read_text(encoding="utf-8")
            document = json.loads(raw)

        # Verbatim: the exact id round-trips, unprefixed and unmangled.
        self.assertIn(PACK_REQUEST_KIND, raw)
        self.assertIn("structured_data", raw)

        snapshot = document["source_requests"]
        self.assertEqual(2, snapshot["open_total"])
        by_id = {record["request_id"]: record for record in snapshot["open"]}
        self.assertEqual(PACK_REQUEST_KIND, by_id["req-pack-quote"]["kind"])
        self.assertEqual("structured_data", by_id["req-structured"]["kind"])
        # A scope mapping is an extra record field; the snapshot must carry it untouched.
        self.assertEqual(
            {"facet_id": "supplier_quote", "candidate": "acme-widget"},
            by_id["req-pack-quote"]["scope"],
        )

    def test_report_counts_pack_and_structured_data_requests_as_opened(self):
        """A namespaced kind must not fall out of the run window or the open tally."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            self.declare_pack_request_kind(target)

            baseline = root / "run-baseline.json"
            code, _, stderr = self.capture_run_report_baseline(target, baseline)
            self.assertEqual(0, code, stderr)

            self.write_extended_kind_requests(target)

            code, stdout, stderr = self.run_report(target, baseline, "--format", "json")
            self.assertEqual(0, code, stderr)
            document = json.loads(stdout)
            report_text = (target / document["report_path"]).read_text(encoding="utf-8")

        requests = document["source_requests"]
        self.assertEqual(["req-pack-quote", "req-structured"], requests["opened"])
        self.assertEqual(2, requests["open_total"])

        by_id = {
            entry["request_id"]: entry
            for entry in document["official_source_evaluation"]["open_requests"]
        }
        self.assertEqual({"req-pack-quote", "req-structured"}, set(by_id))
        # CR-4 names run reports as a surface that must carry the kind through: a report
        # that projects the request without it renders every non-documentary request
        # indistinguishable, which is the "wall of other" the change request objected to.
        self.assertEqual(PACK_REQUEST_KIND, by_id["req-pack-quote"]["kind"])
        self.assertEqual("structured_data", by_id["req-structured"]["kind"])
        # Scope rides along when declared, and is absent when it is not.
        self.assertEqual(
            {"facet_id": "supplier_quote", "candidate": "acme-widget"},
            by_id["req-pack-quote"]["scope"],
        )
        self.assertNotIn("scope", by_id["req-structured"])

        self.assertIn("req-pack-quote", report_text)
        self.assertIn("req-structured", report_text)

    def test_report_diffs_backlog_and_names_touched_questions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            baseline = root / "baseline.json"
            self.capture_baseline(target, baseline)

            self.answer_question(target, "which-benchmarks")
            request_id = self.block_question_with_request(target, "needs-evidence")

            code, stdout, _ = self.run_report(target, baseline, "--agent-id", "agent-a", "--format", "json")
            self.assertEqual(0, code)
            document = json.loads(stdout)

            self.assertEqual("1.0", document["schema_version"])
            self.assertEqual("agent-a", document["agent_id"])
            self.assertIsNotNone(document["window"]["start"])
            self.assertEqual(2, document["questions"]["before"]["by_status"].get("open"))
            self.assertEqual(1, document["questions"]["after"]["by_status"].get("answered"))
            self.assertEqual(1, document["questions"]["after"]["by_status"].get("blocked"))

            touched = {entry["slug"]: entry for entry in document["questions"]["touched"]}
            self.assertEqual({"which-benchmarks", "needs-evidence"}, set(touched))
            self.assertEqual("open", touched["which-benchmarks"]["status_before"])
            self.assertEqual("answered", touched["which-benchmarks"]["status_after"])
            self.assertEqual("../synthesis/run-answer.md", touched["which-benchmarks"]["answer_page"])
            self.assertEqual("blocked", touched["needs-evidence"]["status_after"])
            self.assertIn(request_id, document["source_requests"]["opened"])
            self.assertIn("issue_counts", document["lint"])

            report_path = target / document["report_path"]
            self.assertTrue(report_path.is_file())
            report_text = report_path.read_text()
            self.assertIn("# Research Run Report", report_text)
            self.assertIn("`which-benchmarks`: open -> answered", report_text)
            self.assertIn(request_id, report_text)

    def test_empty_run_reports_no_touched_questions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            baseline = root / "baseline.json"
            self.capture_baseline(target, baseline)

            code, stdout, _ = self.run_report(target, baseline, "--format", "json")
            self.assertEqual(0, code)
            document = json.loads(stdout)

            self.assertEqual([], document["questions"]["touched"])
            self.assertEqual([], document["source_requests"]["opened"])
            self.assertEqual(
                document["questions"]["before"]["by_status"],
                document["questions"]["after"]["by_status"],
            )
            self.assertIn("- None.", (target / document["report_path"]).read_text())

    def test_added_question_appears_as_added(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            baseline = root / "baseline.json"
            self.capture_baseline(target, baseline)

            new_page = target / "wiki" / "questions" / "late-arrival.md"
            new_page.write_text(
                "---\ntype: question\ncreated: 2026-06-11\nupdated: 2026-06-11\n"
                "status: open\npriority: low\norigin: scout\nsource_ids: []\n"
                "summary: Late question.\nquestion: A late question?\n---\n\n# Late\n"
            )

            code, stdout, _ = self.run_report(target, baseline, "--format", "json")
            self.assertEqual(0, code)
            document = json.loads(stdout)

            touched = {entry["slug"]: entry for entry in document["questions"]["touched"]}
            self.assertEqual(["late-arrival"], list(touched))
            self.assertEqual("added", touched["late-arrival"]["change"])

    def test_baseline_without_generated_at_degrades_with_warning(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            baseline = root / "baseline.json"
            self.capture_baseline(target, baseline)
            stripped = json.loads(baseline.read_text())
            stripped.pop("generated_at", None)
            baseline.write_text(json.dumps(stripped))

            code, stdout, _ = self.run_report(target, baseline, "--format", "json")
            self.assertEqual(0, code)
            document = json.loads(stdout)

            self.assertIsNone(document["window"]["start"])
            self.assertTrue(any("generated_at" in warning for warning in document["warnings"]))

    def test_source_normalized_before_baseline_on_same_day_is_excluded(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            baseline = root / "baseline.json"
            self.capture_baseline(target, baseline)
            baseline_payload = json.loads(baseline.read_text())
            baseline_payload["generated_at"] = "2026-06-14T12:00:00Z"
            baseline.write_text(json.dumps(baseline_payload))
            self.write_normalized_source(
                target,
                "paper--before-baseline.md",
                "paper:before-baseline",
                updated="2026-06-14",
                normalized_at="2026-06-14T11:59:59Z",
            )

            code, stdout, _ = self.run_report(target, baseline, "--format", "json")
            self.assertEqual(0, code)
            document = json.loads(stdout)

            self.assertEqual([], document["sources_normalized"])
            self.assertEqual([], document["sources_normalized_legacy_date_match"])
            report_text = (target / document["report_path"]).read_text()
            self.assertIn("## Sources Normalized During Run", report_text)
            self.assertIn("- None.", report_text)

    def test_source_normalized_after_baseline_is_included(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            baseline = root / "baseline.json"
            self.capture_baseline(target, baseline)
            baseline_payload = json.loads(baseline.read_text())
            baseline_payload["generated_at"] = "2026-06-14T12:00:00Z"
            baseline.write_text(json.dumps(baseline_payload))
            self.write_normalized_source(
                target,
                "paper--after-baseline.md",
                "paper:after-baseline",
                updated="2026-06-14",
                normalized_at="2026-06-14T12:00:01Z",
            )

            code, stdout, _ = self.run_report(target, baseline, "--format", "json")
            self.assertEqual(0, code)
            document = json.loads(stdout)

            self.assertEqual(["paper:after-baseline"], document["sources_normalized"])
            self.assertEqual([], document["sources_normalized_legacy_date_match"])
            self.assertIn("`paper:after-baseline`", (target / document["report_path"]).read_text())

    def test_legacy_date_only_normalized_source_is_separate_with_warning(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            baseline = root / "baseline.json"
            self.capture_baseline(target, baseline)
            baseline_payload = json.loads(baseline.read_text())
            baseline_payload["generated_at"] = "2026-06-14T12:00:00Z"
            baseline.write_text(json.dumps(baseline_payload))
            self.write_normalized_source(
                target,
                "paper--legacy-date.md",
                "paper:legacy-date",
                updated="2026-06-14",
            )

            code, stdout, _ = self.run_report(target, baseline, "--format", "json")
            self.assertEqual(0, code)
            document = json.loads(stdout)

            self.assertEqual([], document["sources_normalized"])
            self.assertEqual(["paper:legacy-date"], document["sources_normalized_legacy_date_match"])
            self.assertTrue(
                any("normalized_at" in warning and "paper:legacy-date" in warning for warning in document["warnings"])
            )
            report_text = (target / document["report_path"]).read_text()
            self.assertIn("## Legacy Date-Matched Normalized Sources", report_text)
            self.assertIn("`paper:legacy-date`", report_text)

    def test_baseline_command_captures_questions_requests_and_normalized_sources(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            request_id = self.block_question_with_request(target, "needs-evidence")
            self.write_normalized_source(
                target,
                "paper--baseline-source.md",
                "paper:baseline-source",
                updated="2026-06-14",
                normalized_at="2026-06-14T12:00:01Z",
            )
            baseline = root / "run-baseline.json"

            code, stdout, stderr = self.capture_run_report_baseline(target, baseline, "--format", "json")
            self.assertEqual(0, code, stderr)
            self.assertTrue(baseline.is_file())
            command_report = json.loads(stdout)
            document = json.loads(baseline.read_text())

            self.assertEqual("run_report_baseline", document["document_type"])
            self.assertEqual("1.0", document["schema_version"])
            self.assertEqual(document["generated_at"], document["question_status"]["generated_at"])
            self.assertEqual(2, document["question_status"]["total"])
            open_requests = {record["request_id"]: record for record in document["source_requests"]["open"]}
            self.assertIn(request_id, open_requests)
            self.assertEqual(1, document["source_requests"]["open_total"])
            normalized = {record["source_id"]: record for record in document["normalized_sources"]}
            self.assertEqual("2026-06-14T12:00:01Z", normalized["paper:baseline-source"]["normalized_at"])
            self.assertEqual(str(baseline.resolve()), command_report["baseline_path"])

    def test_report_accepts_rich_baseline_artifact(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            baseline = root / "run-baseline.json"
            code, _, stderr = self.capture_run_report_baseline(target, baseline)
            self.assertEqual(0, code, stderr)
            baseline_payload = json.loads(baseline.read_text())

            self.answer_question(target, "which-benchmarks")
            self.write_normalized_source(
                target,
                "paper--after-rich-baseline.md",
                "paper:after-rich-baseline",
                updated="2026-06-14",
                normalized_at=baseline_payload["generated_at"],
            )

            code, stdout, stderr = self.run_report(target, baseline, "--format", "json")
            self.assertEqual(0, code, stderr)
            document = json.loads(stdout)

            touched = {entry["slug"]: entry for entry in document["questions"]["touched"]}
            self.assertEqual("answered", touched["which-benchmarks"]["status_after"])
            self.assertEqual(["paper:after-rich-baseline"], document["sources_normalized"])

    def test_report_uses_run_controller_baseline_and_includes_run_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            self.write_candidate_records(target)
            self.write_normalized_source(
                target,
                "paper--before-run.md",
                "paper:before-run",
                updated="2000-01-01",
                normalized_at="2000-01-01T00:00:00Z",
            )
            run_id = "run-2026-06-29T090000Z-report"
            run_state = self.start_run(target, run_id)
            baseline_path = target / run_state["workspace_baseline"]["run_report_baseline_path"]
            baseline_payload = json.loads(baseline_path.read_text(encoding="utf-8"))
            self.write_normalized_source(
                target,
                "paper--inside-run.md",
                "paper:inside-run",
                updated="2026-06-29",
                normalized_at=baseline_payload["generated_at"],
            )
            self.answer_question(target, "which-benchmarks")
            self.transition_run(target, run_id, "planned")
            self.transition_run(target, run_id, "discovering")
            self.finish_run(target, run_id, "blocked_on_sources")

            code, stdout, stderr = self.run_report(target, None, "--run-id", run_id, "--format", "json")
            self.assertEqual(0, code, stderr)
            document = json.loads(stdout)

            self.assertEqual(["paper:inside-run"], document["sources_normalized"])
            self.assertNotIn("paper:before-run", document["sources_normalized"])
            self.assertEqual(
                {
                    "present": True,
                    "run_id": run_id,
                    "state": "blocked_on_sources",
                    "state_history": document["run_controller"]["state_history"],
                    "candidate_counts": {"total": 2, "new": 0, "selected": 1, "rejected": 1, "fetched": 0},
                    "coverage_counts": {"required": 0, "satisfied": 0, "missing": 0, "unknown": 2},
                    "budget_state": document["run_controller"]["budget_state"],
                    "budget_overrides": {},
                    "final_verdict": "blocked_on_sources",
                },
                document["run_controller"],
            )
            self.assertEqual(
                ["initialized", "planned", "discovering", "blocked_on_sources"],
                [entry["to_state"] for entry in document["run_controller"]["state_history"]],
            )

            report_text = (target / document["report_path"]).read_text(encoding="utf-8")
            self.assertIn("## Run Controller", report_text)
            self.assertIn(f"- Run id: `{run_id}`", report_text)
            self.assertIn("- Final verdict: `blocked_on_sources`", report_text)
            self.assertIn("`initialized` -> `planned`", report_text)

    def test_report_without_run_id_keeps_legacy_no_run_shape(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            baseline = root / "run-baseline.json"
            code, _, stderr = self.capture_run_report_baseline(target, baseline)
            self.assertEqual(0, code, stderr)

            code, stdout, stderr = self.run_report(target, baseline, "--format", "json")

            self.assertEqual(0, code, stderr)
            self.assertEqual({"present": False}, json.loads(stdout)["run_controller"])

    def test_report_fails_when_run_state_is_malformed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            run_id = "run-2026-06-29T100000Z-bad"
            run_dir = target / "runs" / run_id
            run_dir.mkdir(parents=True)
            (run_dir / "run-state.json").write_text("{not json", encoding="utf-8")

            code, stdout, stderr = self.run_report(target, None, "--run-id", run_id, "--format", "json")

            self.assertEqual(2, code)
            self.assertEqual("", stdout)
            self.assertEqual("RUN_STATE_INVALID", json.loads(stderr)["error_code"])

    def test_invalid_baseline_exits_2(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            missing = root / "missing.json"

            code, _, stderr = self.run_report(target, missing)
            self.assertEqual(2, code)
            self.assertIn("Missing baseline", stderr)

            not_baseline = root / "not-baseline.json"
            not_baseline.write_text(json.dumps({"foo": "bar"}))
            code, _, stderr = self.run_report(target, not_baseline)
            self.assertEqual(2, code)
            self.assertIn("question_status.py", stderr)


if __name__ == "__main__":
    unittest.main()
