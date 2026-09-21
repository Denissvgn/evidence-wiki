"""Selected publication remains coherent, scoped, and read-only."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from tests import test_publication_readiness as fixtures
from tests._script_loader import load_isolated_module

SCRIPTS = Path(__file__).resolve().parents[1] / "workspace-template" / "scripts"
SELECTED = load_isolated_module("selected_publication_tests", SCRIPTS / "_selected_publication.py")
REVISION = load_isolated_module("evidence_revision_tests", SCRIPTS / "_evidence_revision.py")
READINESS = load_isolated_module("selected_readiness_tests", SCRIPTS / "publication_readiness.py")


def tree_bytes(root):
    return {str(path.relative_to(root)): path.read_bytes() if path.is_file() else None
            for path in sorted(root.rglob("*")) if "__pycache__" not in path.parts}


@unittest.skipUnless(os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"), "requires no-follow capture support")
class SelectedPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        helper = fixtures.PublicationReadinessTests()
        self.root = helper.init_workspace(Path(self.temporary.name))
        helper.write_ship_ready_vendor_fixture(self.root)
        self.slug = "vendor-product-spec"

    def run_selected(self, slugs=None, **kwargs):
        return SELECTED.run_selected_publication(self.root, [self.slug] if slugs is None else slugs, **kwargs)

    def assert_refusal(self, code, action):
        with self.assertRaises(Exception) as caught:
            action()
        self.assertEqual(code, getattr(caught.exception, "error_code", None), str(caught.exception))

    def test_selection_excludes_unrelated_review_and_preserves_global_gates(self):
        question = self.root / "wiki/questions" / f"{self.slug}.md"
        text = question.read_text().replace("status: answered", "status: human_review")
        text = text.replace("source_ids:", "human_review_required: true\nhuman_review_status: pending\nsource_ids:", 1)
        (question.parent / "awaiting-review.md").write_text(text)
        before = tree_bytes(self.root)
        document = self.run_selected([self.slug, self.slug])
        self.assertEqual("ship", document["verdict"], document["readiness"]["reasons"])
        self.assertEqual([self.slug], document["question_slugs"])
        self.assertEqual([self.slug], [item["slug"] for item in document["export"]["questions"]])
        self.assertEqual("blocking", document["gate_scope"]["unrelated_source_defects"])
        self.assertEqual(before, tree_bytes(self.root))
        legacy = READINESS.build_readiness_document(self.root)
        self.assertNotEqual("ship", legacy["verdict"])

    def test_unknown_empty_and_unsafe_selection_refuse_without_writes(self):
        before = tree_bytes(self.root)
        for selection, code in (([], "PUBLICATION_SELECTION_INVALID"), (["absent"], "PUBLICATION_QUESTION_UNKNOWN"), (["../outside"], "PUBLICATION_SELECTION_INVALID")):
            with self.subTest(selection=selection):
                self.assert_refusal(code, lambda selection=selection: self.run_selected(selection))
                self.assertEqual(before, tree_bytes(self.root))

    def test_expected_revision_and_content_change(self):
        document = self.run_selected()
        identity = document["revision"]["revision_id"]
        self.assertEqual(identity, self.run_selected(expected_revision=identity)["revision"]["revision_id"])
        (self.root / "log.md").write_text((self.root / "log.md").read_text() + "\nA new observation.\n")
        self.assert_refusal("EVIDENCE_REVISION_CHANGED", lambda: self.run_selected(expected_revision=identity))

    def test_edit_between_readiness_and_export_retries_whole_capture(self):
        original_load = SELECTED.load_workspace_module
        exporter = original_load(SCRIPTS, "export_answers")
        original_export = exporter.build_export
        invocations = []
        def edit_then_export(root, *args, **kwargs):
            invocations.append(root)
            if len(invocations) == 1:
                question = self.root / "wiki/questions" / f"{self.slug}.md"
                question.write_text(question.read_text().replace("status: answered", "status: human_review"))
            return original_export(root, *args, **kwargs)
        def load_for_evaluation(root, name, **kwargs):
            return exporter if name == "export_answers" else original_load(root, name, **kwargs)
        with patch.object(SELECTED, "load_workspace_module", side_effect=load_for_evaluation), patch.object(exporter, "build_export", side_effect=edit_then_export):
            document = self.run_selected()
        self.assertEqual(2, len(invocations))
        self.assertNotEqual("ship", document["verdict"])
        self.assertEqual("human_review", document["export"]["questions"][0]["status"])
        self.assertEqual(SELECTED.capture_workspace(self.root).revision_id, document["revision"]["revision_id"])

    def test_cli_uses_same_revision_and_verdict(self):
        api = self.run_selected()
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = READINESS.main(["--project-root", str(self.root), "--format", "json", "--question", self.slug])
        self.assertEqual("", stderr.getvalue())
        cli = json.loads(stdout.getvalue())
        self.assertEqual(0, code)
        for key in ("question_slugs", "revision", "producer_id", "verdict", "gate_scope"):
            self.assertEqual(api[key], cli[key])
        self.assertEqual(api["export"]["questions"], cli["export"]["questions"])

    def test_public_api_and_real_cli_agree_on_state_and_refusal(self):
        from evidence_wiki import Workspace
        from evidence_wiki.errors import PublicationError

        environment = {**os.environ, "PYTHONPATH": str(SCRIPTS.parents[1] / "src")}
        command = [str(Path(sys.executable).with_name("evidence-wiki")), "publication", "--target", str(self.root), "--format", "json"]
        before = tree_bytes(self.root)
        with Workspace.open(self.root) as workspace:
            document = workspace.publish_selected([self.slug])
            result = subprocess.run([*command, "--question", self.slug], capture_output=True, text=True, env=environment, timeout=30)
            self.assertEqual(0, result.returncode, result.stderr)
            rendered = json.loads(result.stdout)
            for key in ("question_slugs", "revision", "producer_id", "gate_scope", "verdict"):
                self.assertEqual(document[key], rendered[key])
            self.assertEqual(document["export"]["questions"], rendered["export"]["questions"])
            with self.assertRaises(PublicationError) as caught:
                workspace.publish_selected(["absent"])
            result = subprocess.run([*command, "--question", "absent"], capture_output=True, text=True, env=environment, timeout=30)
            self.assertEqual(caught.exception.exit_code, result.returncode)
            envelope = json.loads(result.stderr)
            self.assertEqual(caught.exception.error_code, envelope["error_code"])
        self.assertEqual(before, tree_bytes(self.root))

    def test_output_inside_workspace_is_refused_without_changes(self):
        before = tree_bytes(self.root)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = READINESS.main(["--project-root", str(self.root), "--format", "json", "--question", self.slug, "--output", str(self.root / "report.json")])
        self.assertEqual(2, code)
        self.assertEqual("PUBLICATION_OUTPUT_INVALID", json.loads(stderr.getvalue())["error_code"])
        self.assertEqual(before, tree_bytes(self.root))

    def test_continuous_edits_refuse_instead_of_returning_a_mixed_report(self):
        original_capture = SELECTED.capture_workspace
        captures = []
        def capture_then_edit(root):
            revision = original_capture(root)
            captures.append(revision.revision_id)
            if len(captures) % 2:
                log = self.root / "log.md"
                log.write_text(log.read_text() + "\nAnother concurrent observation.\n")
            return revision
        with patch.object(SELECTED, "capture_workspace", side_effect=capture_then_edit):
            self.assert_refusal("EVIDENCE_REVISION_CHANGED", self.run_selected)
        self.assertEqual(6, len(captures))

    def test_selected_unreviewed_question_and_unrelated_source_defects_block(self):
        question = self.root / "wiki/questions" / f"{self.slug}.md"
        original = question.read_text()
        question.write_text(original.replace("status: answered", "status: human_review"))
        self.assertNotEqual("ship", self.run_selected()["verdict"])
        question.write_text(original)
        manifest = self.root / "sources/manifest.jsonl"
        manifest.write_text(manifest.read_text() + json.dumps({"id": "web:unrelated", "kind": "html", "raw_paths": ["raw/web/missing.html"], "status": "normalized"}) + "\n")
        document = self.run_selected()
        self.assertNotEqual("ship", document["verdict"])
        self.assertEqual("blocking", document["gate_scope"]["unrelated_source_defects"])

    def test_secret_refused_before_materialization(self):
        (self.root / "raw/web/secret.txt").write_text("token = sk-" + "x" * 48)
        before = tree_bytes(self.root)
        revision = SELECTED.capture_workspace(self.root)
        with patch.object(SELECTED, "capture_workspace", return_value=revision), patch.object(type(revision), "materialize") as materialize:
            self.assert_refusal("PUBLICATION_SAFETY_REFUSED", self.run_selected)
        materialize.assert_not_called()
        self.assertEqual(before, tree_bytes(self.root))

    def test_missing_raw_bytes_block_even_when_normalized_text_remains(self):
        (self.root / "raw/web/vendor-product.html").unlink()
        self.assertNotEqual("ship", self.run_selected()["verdict"])

    def test_platform_without_descriptor_support_refuses_without_writes(self):
        before = tree_bytes(self.root)
        with patch.object(REVISION.os, "supports_dir_fd", set()):
            self.assert_refusal("EVIDENCE_REVISION_UNSUPPORTED", lambda: REVISION.capture_workspace(self.root))
        self.assertEqual(before, tree_bytes(self.root))

    def test_captured_scripts_are_never_executed(self):
        marker = Path(self.temporary.name) / "executed.txt"
        (self.root / "scripts/export_answers.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\nraise RuntimeError('untrusted script')\n")
        self.assertEqual("ship", self.run_selected()["verdict"])
        self.assertFalse(marker.exists())

    def test_linked_input_and_outside_config_are_refused(self):
        target = self.root / "raw/web/vendor-product.html"
        link = self.root / "raw/web/linked.html"
        for create in (lambda: link.symlink_to(target), lambda: os.link(target, link)):
            create()
            try:
                self.assert_refusal("EVIDENCE_REVISION_UNSAFE", self.run_selected)
            finally:
                link.unlink()
        config_path = self.root / "research.yml"
        config = yaml.safe_load(config_path.read_text())
        config["outside_path"] = "../outside"
        config_path.write_text(yaml.safe_dump(config))
        self.assert_refusal("PUBLICATION_CONFIG_INVALID", self.run_selected)

    def test_content_identity_ignores_incidental_stat_times(self):
        before = REVISION.capture_workspace(self.root)
        path = self.root / "log.md"
        info = path.stat()
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000))
        self.assertEqual(before.revision_id, REVISION.capture_workspace(self.root).revision_id)

    def test_bounds_fail_closed(self):
        with patch.object(REVISION, "MAX_FILES", 1):
            self.assert_refusal("EVIDENCE_REVISION_LIMIT", lambda: REVISION.capture_workspace(self.root))
        with patch.object(REVISION, "MAX_ENTRIES", 1):
            self.assert_refusal("EVIDENCE_REVISION_LIMIT", lambda: REVISION.capture_workspace(self.root))


if __name__ == "__main__":
    unittest.main()
