import contextlib
import hashlib
import hmac
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import yaml

from tests._script_loader import load_module as load_script_module

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
INTAKE_SCRIPT_PATH = SCRIPTS / "intake_questions.py"
INIT_SCRIPT_PATH = SCRIPTS / "init_research_workspace.py"
LINT_SCRIPT_PATH = SCRIPTS / "lint.py"
STATUS_SCRIPT_PATH = SCRIPTS / "question_status.py"
CLAIM_SCRIPT_PATH = SCRIPTS / "question_claim.py"
RESOLVE_SCRIPT_PATH = SCRIPTS / "question_resolve.py"
PROFILE_FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "workspace-init-profile.yml"


INTAKE = load_script_module("research_intake_questions", INTAKE_SCRIPT_PATH)
INIT = load_script_module("research_intake_questions_init", INIT_SCRIPT_PATH)
LINT = load_script_module("research_intake_questions_lint", LINT_SCRIPT_PATH)
QUESTION_STATUS = load_script_module("research_intake_questions_status", STATUS_SCRIPT_PATH)
QUESTION_CLAIM = load_script_module("research_intake_questions_claim", CLAIM_SCRIPT_PATH)
QUESTION_RESOLVE = load_script_module("research_intake_questions_resolve", RESOLVE_SCRIPT_PATH)


VALID_BATCH = {
    "schema_version": "1.0",
    "handoff": {"task_id": "chain-task-0042"},
    "questions": [
        {
            "question": "What evaluation benchmarks matter for reasoning?",
            "priority": "high",
            "origin": "planner_agent",
            "summary": "Benchmarks relevant to reasoning evaluation.",
            "context": "Focus on benchmarks used in 2025-2026 papers.",
        },
        {"question": "Which datasets are contaminated?"},
    ],
}


class IntakeQuestionsTests(unittest.TestCase):
    def signed_handoff(self, handoff: dict[str, str], secret: str) -> str:
        payload = (
            '{{"task_id":"{}","requested_by":"{}","chain_run_id":"{}"}}'.format(
                handoff.get("task_id", ""),
                handoff.get("requested_by", ""),
                handoff.get("chain_run_id", ""),
            )
        ).encode("utf-8")
        digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
        return f"hmac-sha256:{digest}"

    def init_workspace(self, root: Path, name: str = "intake-workspace", questions: list[dict] | None = None) -> Path:
        target = root / name
        profile = yaml.safe_load(PROFILE_FIXTURE_PATH.read_text())
        profile["workspace_init"]["target_path"] = str(target)
        if questions is not None:
            profile["workspace_init"]["questions"] = questions
        profile_path = root / f"{name}-profile.yml"
        profile_path.write_text(yaml.safe_dump(profile, sort_keys=False))
        with contextlib.redirect_stdout(io.StringIO()):
            INIT.main(["--profile", str(profile_path)])
        return target

    def write_batch(self, root: Path, batch: dict, name: str = "batch.yaml") -> Path:
        path = root / name
        if name.endswith(".json"):
            path.write_text(json.dumps(batch))
        else:
            path.write_text(yaml.safe_dump(batch, sort_keys=False))
        return path

    def update_run_config(self, target: Path, **values: int) -> None:
        config_path = target / "research.yml"
        config = yaml.safe_load(config_path.read_text())
        run = config.setdefault("run", {})
        run.update(values)
        config_path.write_text(yaml.safe_dump(config, sort_keys=False))

    def run_intake(self, *args: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = INTAKE.main(list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def intake_json(self, target: Path, batch_path: Path, *extra: str) -> tuple[int, dict, str]:
        code, stdout, stderr = self.run_intake(
            "--project-root", str(target), "--from-file", str(batch_path), "--format", "json", *extra
        )
        payload = json.loads(stdout) if stdout.strip() else {}
        return code, payload, stderr

    def assert_intake_field_cap_rejects_atomically(
        self,
        *,
        root: Path,
        target: Path,
        item: dict,
        expected_field: str,
        expected_actual_bytes: int,
        expected_max_bytes: int,
        name: str,
    ) -> None:
        batch_path = self.write_batch(root, {"schema_version": "1.0", "questions": [item]}, name=name)
        index_before = (target / "index.md").read_text()
        log_before = (target / "log.md").read_text()
        question_pages_before = sorted((target / "wiki" / "questions").glob("*.md"))

        code, stdout, stderr = self.run_intake(
            "--project-root", str(target), "--from-file", str(batch_path), "--format", "json"
        )

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        envelope = json.loads(stderr)
        self.assertEqual("INTAKE_FIELD_TOO_LONG", envelope["error_code"])
        self.assertEqual(
            {
                "violations": [
                    {
                        "item_index": 0,
                        "field": expected_field,
                        "actual_bytes": expected_actual_bytes,
                        "max_bytes": expected_max_bytes,
                    }
                ],
                "max_question_bytes": 1024,
                "max_summary_bytes": 1024,
                "max_context_bytes": 8192,
                "max_metadata_bytes": 8192,
            },
            envelope["details"],
        )
        self.assertEqual(question_pages_before, sorted((target / "wiki" / "questions").glob("*.md")))
        self.assertEqual(index_before, (target / "index.md").read_text())
        self.assertEqual(log_before, (target / "log.md").read_text())

    def test_happy_path_creates_pages_index_and_log(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            batch_path = self.write_batch(root, VALID_BATCH)

            code, report, _ = self.intake_json(target, batch_path)

            self.assertEqual(0, code)
            self.assertEqual("1.0", report["schema_version"])
            self.assertEqual({"task_id": "chain-task-0042"}, report["handoff"])
            self.assertEqual(2, report["counts"]["created"])
            self.assertEqual(0, report["counts"]["skipped_duplicates"])
            self.assertTrue(report["index_updated"])
            self.assertTrue(report["log_appended"])

            page = target / "wiki" / "questions" / "what-evaluation-benchmarks-matter-for-reasoning.md"
            self.assertTrue(page.is_file())
            frontmatter = QUESTION_STATUS.load_frontmatter(page)
            self.assertEqual("question", frontmatter["type"])
            self.assertEqual("open", frontmatter["status"])
            self.assertEqual("high", frontmatter["priority"])
            self.assertEqual("planner_agent", frontmatter["origin"])
            self.assertEqual("Benchmarks relevant to reasoning evaluation.", frontmatter["summary"])
            self.assertIn("Focus on benchmarks used in 2025-2026 papers.", page.read_text())

            defaulted = QUESTION_STATUS.load_frontmatter(
                target / "wiki" / "questions" / "which-datasets-are-contaminated.md"
            )
            self.assertEqual("medium", defaulted["priority"])
            self.assertEqual("parent_agent", defaulted["origin"])

            index_text = (target / "index.md").read_text()
            self.assertIn("wiki/questions/what-evaluation-benchmarks-matter-for-reasoning.md", index_text)
            self.assertNotIn("| (none yet) | | | |", index_text.split("## Questions", 1)[1].split("## ", 1)[0])

            log_text = (target / "log.md").read_text()
            self.assertIn("intake | Injected question batch", log_text)
            self.assertRegex(log_text, r"- Created at: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\.")
            self.assertIn("task_id: chain-task-0042", log_text)

    def test_metadata_round_trips_namespaced_with_item_correlation_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            metadata = {
                "candidate_sku": "ACME-1",
                "candidate": {
                    "version": "2026-08",
                    "score": 3.5,
                    "available": True,
                    "note": None,
                    "status": "nested-only",
                },
                "a/b": "slash",
                "a~b": "tilde",
                "type": "cannot-replace-question",
            }
            batch_path = self.write_batch(
                root,
                {
                    "schema_version": "1.0",
                    "questions": [{"question": "Does this candidate match?", "metadata": metadata}],
                },
            )

            dry_code, dry_report, dry_stderr = self.intake_json(target, batch_path, "--dry-run")
            self.assertEqual(0, dry_code, dry_stderr)
            self.assertEqual(0, dry_report["created"][0]["item_index"])
            self.assertNotIn("metadata", dry_report["created"][0])
            dry_content = dry_report["created"][0]["content"]

            code, report, stderr = self.intake_json(target, batch_path)
            self.assertEqual(0, code, stderr)
            self.assertEqual(0, report["created"][0]["item_index"])
            self.assertNotIn("metadata", report["created"][0])
            page = target / "wiki" / "questions" / "does-this-candidate-match.md"
            self.assertEqual(dry_content, page.read_text(encoding="utf-8"))
            frontmatter = QUESTION_STATUS.load_frontmatter(page)
            self.assertEqual(metadata, frontmatter["metadata"])
            self.assertEqual("question", frontmatter["type"])
            self.assertEqual("open", frontmatter["status"])

    def test_metadata_survives_claim_and_release_lifecycle_mutations(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            metadata = {"candidate": {"sku": "ACME-1", "active": True}, "rank": 2}
            batch_path = self.write_batch(
                root,
                {
                    "schema_version": "1.0",
                    "questions": [{"id": "candidate", "question": "Candidate?", "metadata": metadata}],
                },
            )
            code, _, stderr = self.intake_json(target, batch_path)
            self.assertEqual(0, code, stderr)
            page = target / "wiki" / "questions" / "candidate.md"

            QUESTION_CLAIM.run_claim(target, slug="candidate", agent_id="agent-a")
            self.assertEqual(metadata, QUESTION_STATUS.load_frontmatter(page)["metadata"])
            QUESTION_CLAIM.run_release(target, slug="candidate", agent_id="agent-a")
            self.assertEqual(metadata, QUESTION_STATUS.load_frontmatter(page)["metadata"])
            QUESTION_RESOLVE.run_block(
                target,
                slug="candidate",
                agent_id="agent-a",
                blocked_reason="Optional evidence is unavailable.",
                allow_unclaimed=True,
            )
            self.assertEqual(metadata, QUESTION_STATUS.load_frontmatter(page)["metadata"])

    def test_metadata_shape_depth_nodes_and_size_are_refused_atomically(self):
        too_deep: dict = {"leaf": "ok"}
        for level in range(INTAKE.MAX_INTAKE_METADATA_DEPTH):
            too_deep = {f"level_{level}": too_deep}
        invalid_metadata = [
            {},
            [],
            {"items": ["array"]},
            {"native_date": date(2026, 8, 14)},
            {"native_set": {"not-json"}},
            {"native_binary": b"not-json"},
            {"nan": float("nan")},
            {"infinite": float("inf")},
            {"negative_infinite": float("-inf")},
            {"nested": {}},
            {"": "empty key"},
            {1: "non-string key"},
            {"bad_utf8": "\ud800"},
            {f"key_{index}": index for index in range(65)},
            too_deep,
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            before = {
                path.relative_to(target).as_posix(): path.read_bytes()
                for path in target.rglob("*")
                if path.is_file()
            }
            for index, metadata in enumerate(invalid_metadata):
                with self.subTest(case=index):
                    batch = {
                        "schema_version": "1.0",
                        "questions": [{"question": f"Invalid metadata {index}?", "metadata": metadata}],
                    }
                    with self.assertRaises(SystemExit):
                        INTAKE.run_intake_document(target, batch, dry_run=False, from_file_label="memory")
                    self.assertEqual(
                        before,
                        {
                            path.relative_to(target).as_posix(): path.read_bytes()
                            for path in target.rglob("*")
                            if path.is_file()
                        },
                    )

            mixed_batch = {
                "schema_version": "1.0",
                "questions": [
                    {"question": "Would otherwise be valid?", "metadata": {"candidate": "A"}},
                    {"question": "Invalid second item?", "metadata": {"items": ["array"]}},
                ],
            }
            with self.assertRaises(SystemExit):
                INTAKE.run_intake_document(
                    target, mixed_batch, dry_run=False, from_file_label="memory"
                )
            self.assertEqual(
                before,
                {
                    path.relative_to(target).as_posix(): path.read_bytes()
                    for path in target.rglob("*")
                    if path.is_file()
                },
            )

            oversized = {
                "schema_version": "1.0",
                "questions": [{"question": "Too much metadata?", "metadata": {"value": "x" * 8192}}],
            }
            result = self.write_batch(root, oversized, name="oversized-metadata.json")
            code, _, stderr = self.run_intake(
                "--project-root", str(target), "--from-file", str(result), "--format", "json"
            )
            self.assertEqual(2, code)
            envelope = json.loads(stderr)
            self.assertEqual("INTAKE_FIELD_TOO_LONG", envelope["error_code"])
            self.assertEqual("metadata", envelope["details"]["violations"][0]["field"])
            self.assertEqual(8192, envelope["details"]["max_metadata_bytes"])

    def test_exact_metadata_bounds_are_accepted(self):
        canonical_overhead = len(b'{"value":""}')
        value = "x" * (INTAKE.MAX_INTAKE_METADATA_BYTES - canonical_overhead)
        metadata = {"value": value}
        self.assertEqual(
            INTAKE.MAX_INTAKE_METADATA_BYTES,
            len(INTAKE.canonical_intake_metadata(metadata).encode("utf-8")),
        )
        normalized = INTAKE.normalize_intake_metadata(metadata, label="metadata", item_index=0)
        self.assertEqual(metadata, normalized)

        raw = {"nested": {"value": "original"}}
        copied = INTAKE.normalize_intake_metadata(raw, label="metadata", item_index=0)
        raw["nested"]["value"] = "mutated"
        self.assertEqual({"nested": {"value": "original"}}, copied)

        exact_nodes = {f"key_{index}": index for index in range(64)}
        self.assertEqual(
            exact_nodes,
            INTAKE.normalize_intake_metadata(exact_nodes, label="metadata", item_index=0),
        )

        depth_limited: dict = {"leaf": "ok"}
        for level in range(INTAKE.MAX_INTAKE_METADATA_DEPTH - 1):
            depth_limited = {f"level_{level}": depth_limited}
        normalized_depth = INTAKE.normalize_intake_metadata(
            depth_limited, label="metadata", item_index=0
        )
        self.assertEqual(depth_limited, normalized_depth)

    def test_metadata_byte_and_node_limits_refuse_exactly_one_over(self):
        canonical_overhead = len(b'{"value":""}')
        one_over_bytes = {
            "value": "x" * (INTAKE.MAX_INTAKE_METADATA_BYTES - canonical_overhead + 1)
        }
        self.assertEqual(
            INTAKE.MAX_INTAKE_METADATA_BYTES + 1,
            len(INTAKE.canonical_intake_metadata(one_over_bytes).encode("utf-8")),
        )
        with self.assertRaises(INTAKE.IntakeFieldTooLong) as oversized:
            INTAKE.normalize_intake_metadata(one_over_bytes, label="metadata", item_index=3)
        self.assertEqual(
            {
                "item_index": 3,
                "field": "metadata",
                "actual_bytes": INTAKE.MAX_INTAKE_METADATA_BYTES + 1,
                "max_bytes": INTAKE.MAX_INTAKE_METADATA_BYTES,
            },
            oversized.exception.details["violations"][0],
        )

        # Sixty-three scalar pairs are 126 nodes. The nested mapping key plus
        # its key/scalar pair brings the total to exactly 129.
        one_over_nodes = {f"key_{index}": index for index in range(63)}
        one_over_nodes["nested"] = {"leaf": "value"}
        with self.assertRaisesRegex(SystemExit, "maximum node count 128"):
            INTAKE.normalize_intake_metadata(one_over_nodes, label="metadata", item_index=0)

        with self.assertRaisesRegex(SystemExit, "bounded canonical JSON"):
            INTAKE.normalize_intake_metadata(
                {"huge_integer": 10**5000}, label="metadata", item_index=0
            )

    def test_metadata_cycles_and_shared_mappings_refuse_but_scalar_aliases_and_duplicate_keys_remain(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            refused_documents = {
                "cycle.yaml": (
                    'schema_version: "1.0"\nquestions:\n  - question: Cycle?\n'
                    "    metadata: &metadata\n      self: *metadata\n"
                ),
                "shared.yaml": (
                    'schema_version: "1.0"\nquestions:\n  - question: Shared?\n'
                    "    metadata:\n      one: &shared\n        value: x\n      two: *shared\n"
                ),
                "timestamp.yaml": (
                    'schema_version: "1.0"\nquestions:\n  - question: Timestamp?\n'
                    "    metadata:\n      observed_at: 2026-08-14T12:34:56Z\n"
                ),
                "binary.yaml": (
                    'schema_version: "1.0"\nquestions:\n  - question: Binary?\n'
                    "    metadata:\n      payload: !!binary eA==\n"
                ),
                "tagged.yaml": (
                    'schema_version: "1.0"\nquestions:\n  - question: Tagged?\n'
                    "    metadata:\n      candidate: !candidate ACME-1\n"
                ),
            }
            for name, document in refused_documents.items():
                with self.subTest(name=name):
                    path = root / name
                    path.write_text(document, encoding="utf-8")
                    code, _, stderr = self.run_intake(
                        "--project-root", str(target), "--from-file", str(path), "--format", "json"
                    )
                    self.assertEqual(2, code)
                    self.assertNotIn("Traceback", stderr)

            accepted = root / "aliases.yaml"
            accepted.write_text(
                'schema_version: "1.0"\nquestions:\n  - question: Alias?\n'
                "    metadata:\n      first: &value ACME-1\n      second: *value\n"
                "      duplicate: old\n      duplicate: new\n",
                encoding="utf-8",
            )
            code, report, stderr = self.intake_json(target, accepted)
            self.assertEqual(0, code, stderr)
            page = target / "wiki" / "questions" / f"{report['created'][0]['slug']}.md"
            self.assertEqual(
                {"first": "ACME-1", "second": "ACME-1", "duplicate": "new"},
                QUESTION_STATUS.load_frontmatter(page)["metadata"],
            )

            duplicate_json = root / "duplicate.json"
            duplicate_json.write_text(
                '{"schema_version":"1.0","questions":[{"question":"JSON duplicate?",'
                '"metadata":{"candidate":"old","candidate":"new"}}]}',
                encoding="utf-8",
            )
            code, report, stderr = self.intake_json(target, duplicate_json)
            self.assertEqual(0, code, stderr)
            page = target / "wiki" / "questions" / f"{report['created'][0]['slug']}.md"
            self.assertEqual(
                {"candidate": "new"},
                QUESTION_STATUS.load_frontmatter(page)["metadata"],
            )

    def test_metadata_dedupe_is_canonical_asymmetric_and_reports_item_indexes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            metadata_first = self.init_workspace(root, name="metadata-first")
            first_batch = self.write_batch(
                root,
                {
                    "schema_version": "1.0",
                    "questions": [
                        {"question": "Same candidate?", "metadata": {"sku": "A", "rank": 1}},
                        {"question": "Same candidate?"},
                    ],
                },
                name="metadata-first.yaml",
            )
            code, first, stderr = self.intake_json(metadata_first, first_batch)
            self.assertEqual(0, code, stderr)
            self.assertEqual([0], [item["item_index"] for item in first["created"]])
            self.assertEqual([1], [item["item_index"] for item in first["skipped_duplicates"]])

            omitted_first = self.init_workspace(root, name="omitted-first")
            second_batch = self.write_batch(
                root,
                {
                    "schema_version": "1.0",
                    "questions": [
                        {"question": "Same candidate?"},
                        {"question": "Same candidate?", "metadata": {"sku": "A", "rank": 1}},
                        {"question": "Same candidate?", "metadata": {"rank": 1, "sku": "A"}},
                        {"question": "Same candidate?", "metadata": {"sku": "B", "rank": 1}},
                    ],
                },
                name="omitted-first.yaml",
            )
            code, second, stderr = self.intake_json(omitted_first, second_batch)
            self.assertEqual(0, code, stderr)
            self.assertEqual([0, 1, 3], [item["item_index"] for item in second["created"]])
            self.assertEqual([2], [item["item_index"] for item in second["skipped_duplicates"]])
            self.assertEqual(
                ["same-candidate", "same-candidate-2", "same-candidate-4"],
                [item["slug"] for item in second["created"]],
            )

            replay = self.write_batch(
                root,
                {
                    "schema_version": "1.0",
                    "questions": [{"question": "Same candidate?", "metadata": {"rank": 1, "sku": "A"}}],
                },
                name="replay.yaml",
            )
            code, replay_report, stderr = self.intake_json(omitted_first, replay)
            self.assertEqual(0, code, stderr)
            self.assertEqual(0, replay_report["counts"]["created"])
            self.assertEqual("same-candidate-2", replay_report["skipped_duplicates"][0]["duplicate_of"])

    def test_metadata_bearing_item_does_not_match_legacy_or_malformed_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(
                root,
                questions=[{"id": "legacy", "question": "Which candidate?"}],
            )
            legacy = target / "wiki" / "questions" / "legacy.md"
            text = legacy.read_text(encoding="utf-8")
            legacy.write_text(text.replace("question: Which candidate?", "question: Which candidate?\nmetadata:\n  - invalid"))
            batch = self.write_batch(
                root,
                {
                    "schema_version": "1.0",
                    "questions": [{"question": "Which candidate?", "metadata": {"sku": "A"}}],
                },
            )

            code, report, stderr = self.intake_json(target, batch)
            self.assertEqual(0, code, stderr)
            self.assertEqual(1, report["counts"]["created"])
            self.assertEqual("which-candidate", report["created"][0]["slug"])

            wildcard = self.write_batch(
                root,
                {"schema_version": "1.0", "questions": [{"question": "Which candidate?"}]},
                name="wildcard.yaml",
            )
            code, report, stderr = self.intake_json(target, wildcard)
            self.assertEqual(0, code, stderr)
            self.assertEqual(0, report["counts"]["created"])
            self.assertEqual("legacy", report["skipped_duplicates"][0]["duplicate_of"])

    def test_question_type_required_metadata_requires_every_item(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            config_path = target / "research.yml"
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config["wiki"]["frontmatter_type_rules"]["question"]["required_fields"].append("metadata")
            config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
            batch = self.write_batch(
                root,
                {
                    "schema_version": "1.0",
                    "questions": [
                        {"question": "Has metadata?", "metadata": {"candidate": "A"}},
                        {"question": "Missing metadata?"},
                    ],
                },
            )
            before = (target / "log.md").read_bytes()

            code, _, stderr = self.run_intake(
                "--project-root", str(target), "--from-file", str(batch), "--format", "json"
            )
            self.assertEqual(2, code)
            self.assertIn("requires 'metadata' for every item", json.loads(stderr)["message"])
            self.assertEqual(before, (target / "log.md").read_bytes())
            self.assertEqual([], list((target / "wiki" / "questions").glob("*.md")))

            # The global list applies to every maintained page type; metadata
            # support is conditional and must not be used to satisfy it.
            config["wiki"]["frontmatter_type_rules"]["question"]["required_fields"].remove("metadata")
            config["wiki"]["frontmatter_required"].append("metadata")
            config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
            all_metadata = self.write_batch(
                root,
                {
                    "schema_version": "1.0",
                    "questions": [{"question": "Still conditional?", "metadata": {"candidate": "A"}}],
                },
                name="global-required.yaml",
            )
            code, _, stderr = self.run_intake(
                "--project-root", str(target), "--from-file", str(all_metadata), "--format", "json"
            )
            self.assertEqual(2, code)
            self.assertIn("wiki.frontmatter_required", json.loads(stderr)["message"])
            self.assertEqual([], list((target / "wiki" / "questions").glob("*.md")))

    def test_question_type_required_fields_unrelated_to_metadata_do_not_block_intake(self):
        # research.yml may legitimately require lifecycle-only fields (populated after
        # intake, e.g. by claim/resolution) in the question type's required_fields.
        # Intake must not police fields it never generates and does not own; only the
        # conditional `metadata` key gets a strict per-item presence check.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            config_path = target / "research.yml"
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config["wiki"]["frontmatter_type_rules"]["question"]["required_fields"].append(
                "human_review_status"
            )
            config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
            batch = self.write_batch(
                root,
                {"schema_version": "1.0", "questions": [{"question": "No metadata involved?"}]},
            )

            code, stdout, stderr = self.run_intake(
                "--project-root", str(target), "--from-file", str(batch), "--format", "json"
            )
            self.assertEqual(0, code, stderr)
            self.assertEqual(1, json.loads(stdout)["counts"]["created"])
            self.assertEqual(1, len(list((target / "wiki" / "questions").glob("*.md"))))

    def test_parser_recursion_is_a_bounded_invalid_batch_refusal(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            json_batch = root / "recursive.json"
            json_batch.write_text("{}", encoding="utf-8")
            with mock.patch.object(INTAKE.json, "loads", side_effect=RecursionError("too deep")):
                with self.assertRaises(INTAKE.ScriptRefusal) as raised:
                    INTAKE.run_intake(target, from_file=str(json_batch))
            self.assertEqual("WORKSPACE_UNREADABLE", raised.exception.error_code)
            self.assertIn("Invalid JSON batch", raised.exception.message)

            yaml_batch = root / "recursive.yaml"
            yaml_batch.write_text("questions: []", encoding="utf-8")
            with mock.patch.object(INTAKE.yaml, "safe_load", side_effect=RecursionError("too deep")):
                with self.assertRaises(INTAKE.ScriptRefusal) as raised:
                    INTAKE.run_intake(target, from_file=str(yaml_batch))
            self.assertEqual("WORKSPACE_UNREADABLE", raised.exception.error_code)
            self.assertIn("Invalid batch document", raised.exception.message)

            for batch_path, parser in (
                (json_batch, INTAKE.json),
                (yaml_batch, INTAKE.yaml),
            ):
                parser_method = "loads" if parser is INTAKE.json else "safe_load"
                with self.subTest(parser=parser_method):
                    with mock.patch.object(
                        parser,
                        parser_method,
                        side_effect=ValueError("integer token exceeds parser resource limit"),
                    ):
                        with self.assertRaises(INTAKE.ScriptRefusal) as raised:
                            INTAKE.run_intake(target, from_file=str(batch_path))
                    self.assertEqual("WORKSPACE_UNREADABLE", raised.exception.error_code)
                    self.assertIn("Invalid", raised.exception.message)

    def test_deep_or_huge_numeric_legacy_frontmatter_does_not_crash_intake_scan(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            questions_dir = target / "wiki" / "questions"
            deep_mapping = "{a:" * 1500 + "1" + "}" * 1500
            huge_integer = "9" * 5000
            malformed_pages = {
                "deep-legacy.md": deep_mapping,
                "huge-integer-legacy.md": f"{{candidate: {huge_integer}}}",
            }
            for name, metadata_yaml in malformed_pages.items():
                (questions_dir / name).write_text(
                    "---\n"
                    "type: question\n"
                    f"question: {name}?\n"
                    f"metadata: {metadata_yaml}\n"
                    "---\n\n# Legacy question\n",
                    encoding="utf-8",
                )

            batch = self.write_batch(
                root,
                {"schema_version": "1.0", "questions": [{"question": "Fresh question?"}]},
            )
            code, report, stderr = self.intake_json(target, batch)

            self.assertEqual(0, code, stderr)
            self.assertEqual(1, report["counts"]["created"])
            self.assertTrue((questions_dir / "fresh-question.md").is_file())

    def test_secret_configured_rejects_unsigned_handoff_batch_atomically(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            batch_path = self.write_batch(root, VALID_BATCH)
            index_before = (target / "index.md").read_text(encoding="utf-8")
            log_before = (target / "log.md").read_text(encoding="utf-8")

            with mock.patch.dict(os.environ, {"EVIDENCE_WIKI_HANDOFF_SECRET": "workspace-secret"}):
                code, stdout, stderr = self.run_intake(
                    "--project-root", str(target), "--from-file", str(batch_path), "--format", "json"
                )

            self.assertEqual(2, code)
            self.assertEqual("", stdout)
            envelope = json.loads(stderr)
            self.assertEqual("HANDOFF_SIGNATURE_INVALID", envelope["error_code"])
            self.assertEqual("unsigned", envelope["details"]["handoff_signature_status"])
            self.assertEqual(index_before, (target / "index.md").read_text(encoding="utf-8"))
            self.assertEqual(log_before, (target / "log.md").read_text(encoding="utf-8"))
            self.assertFalse(
                (target / "wiki" / "questions" / "what-evaluation-benchmarks-matter-for-reasoning.md").exists()
            )

    def test_secret_configured_accepts_signed_handoff_batch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            batch = dict(VALID_BATCH)
            batch["handoff_signature"] = self.signed_handoff(batch["handoff"], "workspace-secret")
            batch_path = self.write_batch(root, batch)

            with mock.patch.dict(os.environ, {"EVIDENCE_WIKI_HANDOFF_SECRET": "workspace-secret"}):
                code, report, _ = self.intake_json(target, batch_path)

            self.assertEqual(0, code)
            self.assertEqual({"task_id": "chain-task-0042"}, report["handoff"])
            self.assertEqual("verified", report["handoff_signature_status"])

    def test_created_pages_pass_lint_and_appear_in_question_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            batch_path = self.write_batch(root, VALID_BATCH)

            code, _, _ = self.intake_json(target, batch_path)
            self.assertEqual(0, code)

            config = yaml.safe_load((target / "research.yml").read_text())
            lint_results = LINT.run_checks(target, config)
            high = [issue for issue in lint_results["issues"] if issue.get("severity") == "HIGH"]
            self.assertEqual([], high)

            records = QUESTION_STATUS.collect_questions(target / "wiki" / "questions")
            slugs = {record["slug"] for record in records}
            self.assertIn("what-evaluation-benchmarks-matter-for-reasoning", slugs)
            self.assertIn("which-datasets-are-contaminated", slugs)

    def test_rerunning_same_batch_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            batch_path = self.write_batch(root, VALID_BATCH)

            first_code, first_report, _ = self.intake_json(target, batch_path)
            self.assertEqual(0, first_code)
            self.assertEqual(2, first_report["counts"]["created"])
            index_before = (target / "index.md").read_text()
            log_before = (target / "log.md").read_text()

            second_code, second_report, _ = self.intake_json(target, batch_path)

            self.assertEqual(0, second_code)
            self.assertEqual(0, second_report["counts"]["created"])
            self.assertEqual(2, second_report["counts"]["skipped_duplicates"])
            duplicate_of = {item["duplicate_of"] for item in second_report["skipped_duplicates"]}
            self.assertEqual(
                {"what-evaluation-benchmarks-matter-for-reasoning", "which-datasets-are-contaminated"},
                duplicate_of,
            )
            self.assertFalse(second_report["log_appended"])
            self.assertEqual(index_before, (target / "index.md").read_text())
            self.assertEqual(log_before, (target / "log.md").read_text())
            self.assertEqual(1, (target / "log.md").read_text().count("intake | Injected question batch"))

    def test_dedup_matches_seeded_questions_by_normalized_text(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(
                root,
                questions=[{"id": "seeded", "question": "What benchmarks matter?"}],
            )
            batch = {
                "schema_version": "1.0",
                "questions": [{"question": "  what   BENCHMARKS matter?  "}],
            }
            batch_path = self.write_batch(root, batch)

            code, report, _ = self.intake_json(target, batch_path)

            self.assertEqual(0, code)
            self.assertEqual(0, report["counts"]["created"])
            self.assertEqual("seeded", report["skipped_duplicates"][0]["duplicate_of"])

    def test_within_batch_duplicates_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            batch = {
                "schema_version": "1.0",
                "questions": [
                    {"question": "Is attention all you need?"},
                    {"question": "Is attention ALL you need?"},
                ],
            }
            batch_path = self.write_batch(root, batch)

            code, report, _ = self.intake_json(target, batch_path)

            self.assertEqual(0, code)
            self.assertEqual(1, report["counts"]["created"])
            self.assertEqual(1, report["counts"]["skipped_duplicates"])
            self.assertEqual(
                "is-attention-all-you-need",
                report["skipped_duplicates"][0]["duplicate_of"],
            )

    def test_invalid_batches_are_rejected_whole_with_exit_2(self):
        invalid_batches = [
            {"questions": [{"question": "No schema version?"}]},
            {"schema_version": "9.9", "questions": [{"question": "Wrong version?"}]},
            {"schema_version": "1.0", "questions": []},
            {"schema_version": "1.0", "questions": [{"question": "Ok?"}, {"text": ""}]},
            {"schema_version": "1.0", "questions": [{"question": "Bad key?", "answer": "nope"}]},
            {"schema_version": "1.0", "questions": [{"question": "Bad priority?", "priority": "urgent"}]},
            {"schema_version": "1.0", "handoff": {"unknown_key": "x"}, "questions": [{"question": "Q?"}]},
            {"schema_version": "1.0", "extra_top": True, "questions": [{"question": "Q?"}]},
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            log_before = (target / "log.md").read_text()
            for index, batch in enumerate(invalid_batches):
                with self.subTest(batch=index):
                    batch_path = self.write_batch(root, batch, name=f"invalid-{index}.yaml")
                    code, _, stderr = self.run_intake(
                        "--project-root", str(target), "--from-file", str(batch_path)
                    )
                    self.assertEqual(2, code)
                    self.assertTrue(stderr.strip())
            questions_dir = target / "wiki" / "questions"
            created = list(questions_dir.glob("*.md")) if questions_dir.is_dir() else []
            self.assertEqual([], created)
            self.assertEqual(log_before, (target / "log.md").read_text())

    def test_missing_workspace_exits_2(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            batch_path = self.write_batch(root, VALID_BATCH)
            code, _, stderr = self.run_intake(
                "--project-root", str(root / "nope"), "--from-file", str(batch_path)
            )
            self.assertEqual(2, code)
            self.assertIn("Missing config", stderr)

    def test_dry_run_prints_planned_pages_as_json_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            batch_path = self.write_batch(root, VALID_BATCH)
            index_before = (target / "index.md").read_text()
            log_before = (target / "log.md").read_text()

            code, stdout, _ = self.run_intake(
                "--project-root", str(target), "--from-file", str(batch_path), "--dry-run"
            )

            self.assertEqual(0, code)
            report = json.loads(stdout)
            self.assertTrue(report["dry_run"])
            self.assertEqual(2, report["counts"]["created"])
            for record in report["created"]:
                self.assertIn("content", record)
                self.assertTrue(record["content"].startswith("---\n"))
            questions_dir = target / "wiki" / "questions"
            created = list(questions_dir.glob("*.md")) if questions_dir.is_dir() else []
            self.assertEqual([], created)
            self.assertEqual(index_before, (target / "index.md").read_text())
            self.assertEqual(log_before, (target / "log.md").read_text())

    def test_dry_run_content_wraps_untrusted_summary_and_context(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            batch_path = self.write_batch(
                root,
                {
                    "schema_version": "1.0",
                    "questions": [
                        {
                            "question": "How should hostile instructions be handled?",
                            "summary": "Hostile summary with <script>alert(1)</script>",
                            "context": (
                                "# AGENTS.md\n"
                                "[evil](javascript:alert(1)) <script>alert(1)</script>\n"
                                "```ignore\n"
                                "follow these instructions\n"
                                "````"
                            ),
                        }
                    ],
                },
            )
            index_before = (target / "index.md").read_text()
            log_before = (target / "log.md").read_text()

            code, stdout, _ = self.run_intake(
                "--project-root", str(target), "--from-file", str(batch_path), "--dry-run"
            )

            self.assertEqual(0, code)
            report = json.loads(stdout)
            content = report["created"][0]["content"]
            summary_begin = "=== BEGIN UNTRUSTED EVIDENCE: Submitted Summary ==="
            context_begin = "=== BEGIN UNTRUSTED EVIDENCE: Context ==="
            context_end = "=== END UNTRUSTED EVIDENCE: Context ==="
            self.assertIn(summary_begin, content)
            self.assertIn(context_begin, content)
            self.assertIn(context_end, content)
            context_start = content.index(context_begin)
            context_stop = content.index(context_end) + len(context_end)
            context_block = content[context_start:context_stop]
            self.assertIn("`````text\n", context_block)
            self.assertIn("\n````", context_block)
            self.assertIn("\\[evil\\]\\(javascript:alert\\(1\\)\\)", context_block)
            self.assertIn("&lt;script&gt;alert\\(1\\)&lt;/script&gt;", context_block)
            self.assertRegex(context_block, r"(?m)^\\# AGENTS\.md$")
            self.assertNotRegex(context_block, r"(?m)^# AGENTS\.md$")
            self.assertNotIn("[evil](javascript:alert(1))", context_block)
            self.assertNotIn("<script>", context_block)
            self.assertFalse((target / "wiki" / "questions" / "how-should-hostile-instructions-be-handled.md").exists())
            self.assertEqual(index_before, (target / "index.md").read_text())
            self.assertEqual(log_before, (target / "log.md").read_text())

    def test_reads_json_batch_from_stdin(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            stdin = io.StringIO(json.dumps(VALID_BATCH))
            stdout = io.StringIO()
            old_stdin = sys.stdin
            sys.stdin = stdin
            try:
                with contextlib.redirect_stdout(stdout):
                    code = INTAKE.main(["--project-root", str(target), "--format", "json"])
            finally:
                sys.stdin = old_stdin
            self.assertEqual(0, code)
            report = json.loads(stdout.getvalue())
            self.assertEqual(2, report["counts"]["created"])
            self.assertIn("Batch source: stdin", (target / "log.md").read_text())

    def test_slug_collision_with_existing_page_gets_suffixed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(
                root,
                questions=[{"id": "benchmarks", "question": "What benchmarks matter?"}],
            )
            batch = {
                "schema_version": "1.0",
                "questions": [{"id": "benchmarks", "question": "Which benchmark suites are saturated?"}],
            }
            batch_path = self.write_batch(root, batch)

            code, report, _ = self.intake_json(target, batch_path)

            self.assertEqual(0, code)
            self.assertEqual(1, report["counts"]["created"])
            self.assertEqual("benchmarks-2", report["created"][0]["slug"])
            self.assertTrue((target / "wiki" / "questions" / "benchmarks-2.md").is_file())

    def test_overlong_question_text_summary_and_context_reject_atomically(self):
        cases = [
            (
                {"question": "Q" * 1025},
                "question",
                1025,
                1024,
                "overlong-question.yaml",
            ),
            (
                {"text": "T" * 1025},
                "text",
                1025,
                1024,
                "overlong-text.yaml",
            ),
            (
                {"question": "Valid question?", "summary": "S" * 1025},
                "summary",
                1025,
                1024,
                "overlong-summary.yaml",
            ),
            (
                {"question": "Valid question?", "context": "C" * 8193},
                "context",
                8193,
                8192,
                "overlong-context.yaml",
            ),
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            for item, expected_field, actual_bytes, max_bytes, name in cases:
                with self.subTest(field=expected_field):
                    self.assert_intake_field_cap_rejects_atomically(
                        root=root,
                        target=target,
                        item=item,
                        expected_field=expected_field,
                        expected_actual_bytes=actual_bytes,
                        expected_max_bytes=max_bytes,
                        name=name,
                    )

    def test_exact_intake_field_byte_limits_are_accepted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            batch_path = self.write_batch(
                root,
                {
                    "schema_version": "1.0",
                    "questions": [
                        {
                            "question": "Q" * 1024,
                            "summary": "S" * 1024,
                            "context": "C" * 8192,
                        }
                    ],
                },
            )

            code, report, stderr = self.intake_json(target, batch_path)

            self.assertEqual(0, code, stderr)
            self.assertEqual(1, report["counts"]["created"])
            self.assertTrue((target / "wiki" / "questions" / f"{'q' * 60}.md").is_file())

    def test_intake_field_limits_count_utf8_bytes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            overlong = "é" * 513

            self.assert_intake_field_cap_rejects_atomically(
                root=root,
                target=target,
                item={"question": overlong},
                expected_field="question",
                expected_actual_bytes=len(overlong.encode("utf-8")),
                expected_max_bytes=1024,
                name="overlong-multibyte.yaml",
            )

    def test_total_cap_rejects_batch_atomically_with_json_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(
                root,
                questions=[
                    {"id": "existing-one", "question": "Existing one?"},
                    {"id": "existing-two", "question": "Existing two?"},
                ],
            )
            self.update_run_config(target, max_open_questions_total=2)
            batch_path = self.write_batch(
                root,
                {"schema_version": "1.0", "questions": [{"question": "New external question?"}]},
            )
            index_before = (target / "index.md").read_text()
            log_before = (target / "log.md").read_text()

            code, stdout, stderr = self.run_intake(
                "--project-root", str(target), "--from-file", str(batch_path), "--format", "json"
            )

            self.assertEqual(2, code)
            self.assertEqual("", stdout)
            envelope = json.loads(stderr)
            self.assertEqual("INTAKE_TOTAL_CAP_EXCEEDED", envelope["error_code"])
            self.assertEqual(
                {
                    "open_questions_total": 2,
                    "new_questions": 1,
                    "max_open_questions_total": 2,
                },
                envelope["details"],
            )
            self.assertFalse((target / "wiki" / "questions" / "new-external-question.md").exists())
            self.assertEqual(index_before, (target / "index.md").read_text())
            self.assertEqual(log_before, (target / "log.md").read_text())

    def test_duplicate_only_batch_is_allowed_when_total_cap_is_full(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(
                root,
                questions=[{"id": "existing-one", "question": "Existing one?"}],
            )
            self.update_run_config(target, max_open_questions_total=1)
            batch_path = self.write_batch(
                root,
                {"schema_version": "1.0", "questions": [{"question": "  existing   ONE?  "}]},
            )

            code, report, _ = self.intake_json(target, batch_path)

            self.assertEqual(0, code)
            self.assertEqual(0, report["counts"]["created"])
            self.assertEqual(1, report["counts"]["skipped_duplicates"])

    def test_hourly_rate_limit_rejects_later_batch_atomically(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            self.update_run_config(target, max_intake_per_hour=1)
            first_batch = self.write_batch(
                root,
                {"schema_version": "1.0", "questions": [{"question": "First external question?"}]},
                name="first.yaml",
            )
            second_batch = self.write_batch(
                root,
                {"schema_version": "1.0", "questions": [{"question": "Second external question?"}]},
                name="second.yaml",
            )
            first_code, first_report, _ = self.intake_json(target, first_batch)
            self.assertEqual(0, first_code)
            self.assertEqual(1, first_report["counts"]["created"])
            index_before = (target / "index.md").read_text()
            log_before = (target / "log.md").read_text()

            code, stdout, stderr = self.run_intake(
                "--project-root", str(target), "--from-file", str(second_batch), "--format", "json"
            )

            self.assertEqual(2, code)
            self.assertEqual("", stdout)
            envelope = json.loads(stderr)
            self.assertEqual("INTAKE_RATE_LIMITED", envelope["error_code"])
            self.assertEqual(1, envelope["details"]["questions_created_last_hour"])
            self.assertEqual(1, envelope["details"]["new_questions"])
            self.assertEqual(1, envelope["details"]["max_intake_per_hour"])
            self.assertFalse((target / "wiki" / "questions" / "second-external-question.md").exists())
            self.assertEqual(index_before, (target / "index.md").read_text())
            self.assertEqual(log_before, (target / "log.md").read_text())

    def test_dry_run_enforces_limits_without_consuming_rate_budget(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(
                root,
                questions=[{"id": "existing-one", "question": "Existing one?"}],
            )
            self.update_run_config(target, max_open_questions_total=1)
            batch_path = self.write_batch(
                root,
                {"schema_version": "1.0", "questions": [{"question": "Dry run rejected?"}]},
            )
            log_before = (target / "log.md").read_text()

            code, stdout, stderr = self.run_intake(
                "--project-root", str(target), "--from-file", str(batch_path), "--dry-run"
            )

            self.assertEqual(2, code)
            self.assertEqual("", stdout)
            self.assertEqual("INTAKE_TOTAL_CAP_EXCEEDED", json.loads(stderr)["error_code"])
            self.assertFalse((target / "wiki" / "questions" / "dry-run-rejected.md").exists())
            self.assertEqual(log_before, (target / "log.md").read_text())


if __name__ == "__main__":
    unittest.main()
