import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml

from tests._mutation_fault_harness import (
    FAULT_POINTS,
    InjectedMutationFault,
    generated_temp,
    inject_once,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
MATRIX_PATH = REPO_ROOT / "tests" / "fixtures" / "mutation-fault-recovery" / "matrix.yml"
ATOMIC_BOUNDARIES = ("before_temp_write", "after_temp_write", "before_replace", "after_replace")


def load_script_module(name: str, filename: str):
    path = SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def path_matches_name(fragment: str):
    def matches(path: Any, *_args: Any, **_kwargs: Any) -> bool:
        return generated_temp(path) and fragment in getattr(path, "name", "")

    return matches


class MutationFaultRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inventory = load_script_module("fault_inventory", "source_inventory.py")
        cls.normalize = load_script_module("fault_normalize", "normalize_sources.py")
        cls.questions = load_script_module("fault_questions", "question_claim.py")
        cls.candidates = load_script_module("fault_candidates", "discover_sources.py")
        cls.query = load_script_module("fault_query", "query_index.py")
        cls.upgrade = load_script_module("fault_upgrade", "init_research_workspace.py")

    def protected_files(self, root: Path) -> tuple[Path, Path, bytes, bytes]:
        raw = root / "sources" / "raw" / "evidence.txt"
        user = root / "wiki" / "user-edit.md"
        raw.parent.mkdir(parents=True, exist_ok=True)
        user.parent.mkdir(parents=True, exist_ok=True)
        raw.write_text("immutable raw evidence\n", encoding="utf-8")
        user.write_text("unrelated user edit\n", encoding="utf-8")
        return raw, user, raw.read_bytes(), user.read_bytes()

    def assert_protected_files(
        self,
        protected: tuple[Path, Path, bytes, bytes],
    ) -> None:
        raw, user, raw_before, user_before = protected
        self.assertEqual(raw_before, raw.read_bytes())
        self.assertEqual(user_before, user.read_bytes())

    def injected_path_call(
        self,
        boundary: str,
        fragment: str,
        invoke,
        *,
        fault_kind: str = "transient_io",
    ) -> None:
        owner = Path
        attribute = "write_text" if boundary in {"before_temp_write", "after_temp_write"} else "replace"
        timing = "after" if boundary in {"after_temp_write", "after_replace"} else "before"
        with inject_once(
            owner,
            attribute,
            fault_point=boundary,
            timing=timing,
            matches=path_matches_name(fragment),
            fault_kind=fault_kind,
        ):
            with self.assertRaises(InjectedMutationFault) as raised:
                invoke()
        self.assertEqual("MUTATION_FAULT_INJECTED", raised.exception.error_code)
        self.assertEqual(boundary, raised.exception.fault_point)

    def test_fault_matrix_is_complete(self):
        matrix = yaml.safe_load(MATRIX_PATH.read_text(encoding="utf-8"))

        self.assertEqual(FAULT_POINTS, frozenset(matrix["fault_taxonomy"]))
        self.assertEqual(
            {
                "inventory",
                "normalization",
                "question_mutation",
                "candidate_mutation",
                "indexing",
                "run_finalization",
                "upgrade",
            },
            set(matrix["mutation_classes"]),
        )
        for name, mutation in matrix["mutation_classes"].items():
            with self.subTest(mutation=name):
                self.assertTrue(set(ATOMIC_BOUNDARIES).issubset(mutation["boundaries"]))
        self.assertEqual(
            {
                "read_only",
                "missing_directory",
                "corrupt_jsonl",
                "corrupt_yaml",
                "corrupt_index",
                "transient_lock",
                "quota_exhausted",
            },
            set(matrix["environment_faults"]),
        )
        self.assertEqual({"sleep_resume", "crash", "adopt", "abandon"}, set(matrix["lifecycle_recovery"]))

    def test_inventory_boundaries_preserve_prior_manifest_and_retry_without_duplicates(self):
        records = [{"id": "source:new", "kind": "web_link", "raw_paths": []}]
        expected = json.dumps(records[0], sort_keys=True, separators=(",", ":")) + "\n"
        for boundary in ATOMIC_BOUNDARIES:
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                protected = self.protected_files(root)
                manifest = root / "sources" / "manifest.jsonl"
                manifest.parent.mkdir(parents=True, exist_ok=True)
                manifest.write_text('{"id":"source:prior"}\n', encoding="utf-8")
                prior = manifest.read_bytes()

                self.injected_path_call(
                    boundary,
                    "manifest.jsonl",
                    lambda manifest=manifest: self.inventory.write_manifest(manifest, records),
                    fault_kind="quota_exhausted" if boundary == "before_temp_write" else "transient_io",
                )

                if boundary == "after_replace":
                    self.assertEqual(expected, manifest.read_text(encoding="utf-8"))
                else:
                    self.assertEqual(prior, manifest.read_bytes())
                self.inventory.write_manifest(manifest, records)
                self.assertEqual([records[0]], [json.loads(line) for line in manifest.read_text().splitlines()])
                self.assert_protected_files(protected)

    def test_normalization_boundaries_never_accept_partial_markdown(self):
        record = {
            "id": "link:fixture",
            "kind": "web_link",
            "url": "https://example.invalid/evidence",
            "raw_paths": ["raw/links/links.txt"],
            "metadata": {"host": "example.invalid"},
        }
        source = self.normalize.normalize_link_record(record)
        for boundary in ATOMIC_BOUNDARIES:
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                protected = self.protected_files(root)
                normalized_root = root / "sources" / "normalized"
                output = self.normalize.normalized_output_path(source, normalized_root)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text("prior complete markdown\n", encoding="utf-8")
                prior = output.read_bytes()

                invoke = lambda normalized_root=normalized_root: self.normalize.write_normalized_source(
                    source,
                    normalized_root,
                    "sources/manifest.jsonl",
                    "2026-07-11",
                    force=True,
                    normalized_at="2026-07-11T00:00:00Z",
                )
                self.injected_path_call(boundary, output.name, invoke)

                if boundary == "after_replace":
                    self.assertIn("type: normalized_source", output.read_text(encoding="utf-8"))
                else:
                    self.assertEqual(prior, output.read_bytes())
                invoke()
                text = output.read_text(encoding="utf-8")
                self.assertEqual(1, text.count("type: normalized_source"))
                self.assert_protected_files(protected)

    def test_question_boundaries_preserve_complete_page_and_clean_generated_temp(self):
        for boundary in ATOMIC_BOUNDARIES:
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                protected = self.protected_files(root)
                page = root / "wiki" / "questions" / "question.md"
                page.parent.mkdir(parents=True, exist_ok=True)
                page.write_text("prior complete page\n", encoding="utf-8")
                prior = page.read_bytes()
                replacement = "replacement complete page\n"
                attribute = "open" if boundary == "before_temp_write" else "replace"
                timing = "after" if boundary == "after_replace" else "before"

                with inject_once(
                    Path,
                    attribute,
                    fault_point=boundary,
                    timing=timing,
                    matches=path_matches_name(page.name),
                ):
                    with self.assertRaises(InjectedMutationFault):
                        self.questions.write_page_atomic(page, replacement)

                if boundary == "after_replace":
                    self.assertEqual(replacement, page.read_text(encoding="utf-8"))
                else:
                    self.assertEqual(prior, page.read_bytes())
                self.assertEqual([], list(page.parent.glob(f".{page.name}.*.tmp")))
                self.questions.write_page_atomic(page, replacement)
                self.assertEqual(replacement, page.read_text(encoding="utf-8"))
                self.assert_protected_files(protected)

    def test_candidate_boundaries_preserve_valid_jsonl_and_retry_without_duplicates(self):
        records = [{"candidate_id": "candidate-new", "title": "New candidate", "url": "https://example.invalid"}]
        for boundary in ATOMIC_BOUNDARIES:
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                protected = self.protected_files(root)
                store = root / "sources" / "discovery" / "candidates.jsonl"
                store.parent.mkdir(parents=True, exist_ok=True)
                store.write_text('{"candidate_id":"candidate-prior"}\n', encoding="utf-8")
                prior = store.read_bytes()
                invoke = lambda store=store: self.candidates.rewrite_candidates(store, records)

                self.injected_path_call(boundary, store.name, invoke)

                if boundary != "after_replace":
                    self.assertEqual(prior, store.read_bytes())
                else:
                    self.assertEqual("candidate-new", json.loads(store.read_text().splitlines()[0])["candidate_id"])
                invoke()
                parsed = [json.loads(line) for line in store.read_text().splitlines()]
                self.assertEqual(1, len(parsed))
                self.assertEqual("candidate-new", parsed[0]["candidate_id"])
                self.assert_protected_files(protected)

    def test_index_boundaries_keep_a_complete_sqlite_database_and_retry(self):
        if not self.query.sqlite_fts5_available():
            self.skipTest("SQLite FTS5 is unavailable on this interpreter")
        config = {"wiki": {"root": "wiki"}, "sources": {"normalized_dir": "sources/normalized"}}
        for boundary in ATOMIC_BOUNDARIES:
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                protected = self.protected_files(root)
                page = root / "wiki" / "answer.md"
                page.write_text("# Prior\n\nprior searchable corpus\n", encoding="utf-8")
                index = root / ".research-cache" / "query-index.sqlite3"
                self.query.write_fts_index(root, config, "all", index)
                page.write_text("# Replacement\n\nreplacement searchable corpus\n", encoding="utf-8")
                temp_prefix = f".{index.name}."

                if boundary == "before_temp_write":
                    matcher = lambda database, *_args, temp_prefix=temp_prefix, **_kwargs: (
                        getattr(database, "name", "").startswith(temp_prefix)
                        and getattr(database, "name", "").endswith(".tmp")
                    )
                    owner, attribute, timing = self.query.sqlite3, "connect", "before"
                else:
                    matcher = path_matches_name(index.name)
                    owner, attribute = Path, "replace"
                    timing = "after" if boundary == "after_replace" else "before"
                with inject_once(
                    owner,
                    attribute,
                    fault_point=boundary,
                    timing=timing,
                    matches=matcher,
                ):
                    with self.assertRaises(InjectedMutationFault):
                        self.query.write_fts_index(root, config, "all", index)

                metadata = self.query.index_metadata(index)
                self.assertEqual(self.query.INDEX_SCHEMA_VERSION, metadata["schema_version"])
                self.assertEqual([], list(index.parent.glob(f".{index.name}.*.tmp*")))
                self.query.write_fts_index(root, config, "all", index)
                usable, note = self.query.evaluate_index(root, config, index, "all")
                self.assertTrue(usable, note)
                self.assert_protected_files(protected)

    def test_upgrade_boundaries_preserve_managed_file_raw_and_user_edits(self):
        boundaries = (*ATOMIC_BOUNDARIES, "during_upgrade")
        for boundary in boundaries:
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                starter = root / "starter"
                target = root / "workspace"
                source = starter / "scripts" / "managed.py"
                destination = target / "scripts" / "managed.py"
                source.parent.mkdir(parents=True)
                destination.parent.mkdir(parents=True)
                source.write_text("replacement managed tool\n", encoding="utf-8")
                destination.write_text("prior managed tool\n", encoding="utf-8")
                protected = self.protected_files(target)
                prior = destination.read_bytes()
                attribute = "write_bytes" if boundary in {"before_temp_write", "after_temp_write"} else "replace"
                timing = "after" if boundary in {"after_temp_write", "after_replace"} else "before"

                invoke = lambda starter=starter, target=target: self.upgrade.refresh_managed_path(
                    starter,
                    target,
                    "scripts",
                    dry_run=False,
                )
                with inject_once(
                    Path,
                    attribute,
                    fault_point=boundary,
                    timing=timing,
                    matches=path_matches_name(destination.name),
                    fault_kind="read_only" if boundary == "during_upgrade" else "transient_io",
                ):
                    with self.assertRaises(self.upgrade.UpgradeWriteError) as caught:
                        invoke()

                self.assertEqual("UPGRADE_WRITE_FAILED", caught.exception.error_code)
                self.assertEqual("scripts/managed.py", caught.exception.details["path"])
                self.assertIn("prior complete file or the complete replacement", caught.exception.details["preserved"])

                if boundary == "after_replace":
                    self.assertEqual(source.read_bytes(), destination.read_bytes())
                else:
                    self.assertEqual(prior, destination.read_bytes())
                invoke()
                self.assertEqual(source.read_bytes(), destination.read_bytes())
                self.assert_protected_files(protected)

    def test_run_finalization_boundaries_are_recoverable_and_event_ids_remain_unique(self):
        controller = load_script_module("fault_finalization_controller", "run_controller.py")
        boundaries = (*ATOMIC_BOUNDARIES, "during_event_append")
        for boundary in boundaries:
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                protected = self.protected_files(root)
                run_id = f"run-{boundary}"
                run_root = root / "runs" / run_id
                run_root.mkdir(parents=True)
                prior = {
                    "schema_version": controller.SCHEMA_VERSION,
                    "run_id": run_id,
                    "agent_id": "agent-before-crash",
                    "updated_at": "2026-07-11T00:00:00Z",
                    "state": {"current": "planned"},
                    "state_history": [],
                    "recovery_history": [],
                }
                state_path = run_root / controller.RUN_STATE_FILENAME
                event_path = run_root / controller.EVENTS_FILENAME
                state_path.write_text(json.dumps(prior), encoding="utf-8")
                initial_event = {
                    "schema_version": controller.SCHEMA_VERSION,
                    "event_id": "evt-0001",
                    "run_id": run_id,
                    "occurred_at": "2026-07-11T00:00:00Z",
                    "agent_id": "agent-before-crash",
                    "event_type": "state_transition",
                    "from_state": "initialized",
                    "to_state": "planned",
                    "message": "Prior good state.",
                    "data": {},
                }
                event_path.write_text(controller.compact_json(initial_event) + "\n", encoding="utf-8")
                intended = json.loads(json.dumps(prior))
                intended["state"] = {"current": "no_ship"}
                intended_event = {
                    **initial_event,
                    "event_id": "evt-0002",
                    "from_state": "planned",
                    "to_state": "no_ship",
                    "message": "Honest terminal verdict.",
                }

                if boundary in {"before_temp_write", "after_temp_write"}:
                    owner, attribute = Path, "write_text"
                    timing = "after" if boundary == "after_temp_write" else "before"
                    matcher = path_matches_name(controller.RUN_STATE_FILENAME)
                elif boundary == "during_event_append":
                    owner, attribute, timing = Path, "replace", "before"
                    matcher = path_matches_name(controller.EVENTS_FILENAME)
                else:
                    owner, attribute = Path, "replace"
                    timing = "after" if boundary == "after_replace" else "before"
                    matcher = path_matches_name(controller.RUN_STATE_FILENAME)
                with inject_once(
                    owner,
                    attribute,
                    fault_point=boundary,
                    timing=timing,
                    matches=matcher,
                    fault_kind="quota_exhausted" if boundary == "during_event_append" else "transient_io",
                ):
                    with self.assertRaises(controller.RunControllerError) as error:
                        controller.commit_run_mutation(root, run_id, intended, intended_event)
                self.assertEqual("RUN_MUTATION_WRITE_FAILED", error.exception.error_code)

                interrupted = json.loads(state_path.read_text(encoding="utf-8"))
                has_pending = controller.PENDING_EVENT_FIELD in interrupted
                generated_temps = [path for path in run_root.iterdir() if generated_temp(path)]
                if has_pending or generated_temps:
                    recovered = controller.run_recover(
                        root,
                        SimpleNamespace(run_id=run_id, agent_id="recovery-agent"),
                    )
                    self.assertNotIn(controller.PENDING_EVENT_FIELD, recovered)
                    if generated_temps:
                        quarantined = run_root / controller.MUTATION_RECOVERY_DIR / "quarantine"
                        self.assertTrue(any(quarantined.iterdir()))

                events = controller.load_events(event_path)
                if has_pending:
                    self.assertEqual("no_ship", json.loads(state_path.read_text())["state"]["current"])
                    self.assertFalse(controller.append_event(event_path, intended_event))
                else:
                    retry_document = controller.load_run_state(root, run_id)
                    retry_document["state"] = {"current": "no_ship"}
                    retry_event = {**intended_event, "event_id": controller.next_event_id(event_path)}
                    controller.commit_run_mutation(root, run_id, retry_document, retry_event)
                    events = controller.load_events(event_path)
                event_ids = [event["event_id"] for event in events]
                self.assertEqual(len(event_ids), len(set(event_ids)))
                self.assertEqual(1, sum(event.get("to_state") == "no_ship" for event in events))
                self.assert_protected_files(protected)

    def test_corrupt_yaml_jsonl_and_index_fail_closed_without_touching_evidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            protected = self.protected_files(root)
            config_path = root / "research.yml"
            config_path.write_text("project: [unterminated\n", encoding="utf-8")
            with self.assertRaises(SystemExit) as yaml_error:
                self.candidates.load_config(root)
            self.assertIn("Invalid YAML", str(yaml_error.exception))

            event_log = root / "runs" / "run-corrupt" / "events.jsonl"
            event_log.parent.mkdir(parents=True)
            event_log.write_text('{"event_id":"evt-0001"}\nnot-json\n', encoding="utf-8")
            controller = load_script_module("fault_controller", "run_controller.py")
            with self.assertRaises(controller.RunControllerError) as jsonl_error:
                controller.load_events(event_log)
            self.assertEqual("RUN_EVENTS_INVALID", jsonl_error.exception.error_code)
            self.assertIn("Preserve", jsonl_error.exception.remediation)

            corrupt_index = root / ".research-cache" / "query-index.sqlite3"
            corrupt_index.parent.mkdir(parents=True)
            corrupt_index.write_bytes(b"not a sqlite database")
            usable, note = self.query.evaluate_index(root, {}, corrupt_index, "all")
            self.assertFalse(usable)
            self.assertIn("unreadable", note or "")
            self.assertEqual(b"not a sqlite database", corrupt_index.read_bytes())
            self.assert_protected_files(protected)

    def test_missing_generated_directories_are_recreated_without_raw_mutation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            protected = self.protected_files(root)
            manifest = root / "missing" / "nested" / "manifest.jsonl"

            self.inventory.write_manifest(manifest, [{"id": "source:one"}])

            self.assertTrue(manifest.is_file())
            self.assertEqual("source:one", json.loads(manifest.read_text())["id"])
            self.assert_protected_files(protected)


if __name__ == "__main__":
    unittest.main()
