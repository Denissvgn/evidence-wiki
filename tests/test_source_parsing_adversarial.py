import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures"
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


INVENTORY = load_script_module("adversarial_inventory", "source_inventory.py")
LINT = load_script_module("adversarial_lint", "lint.py")


class LinkParsingAdversarialTests(unittest.TestCase):
    def test_text_link_files_accept_comments_bare_urls_markdown_links_and_warn_for_unsupported_schemes(self):
        occurrences, warnings = INVENTORY.parse_text_link_file(
            [
                "# source list",
                "  https://Example.org/Paper?ref=One#Section  ",
                "[Fixture repo](https://github.com/Example/Repo?tab=readme)",
                "https://example.org/Paper?ref=One#Section",
                "ftp://example.org/not-supported",
            ],
            "raw/links/adversarial.txt",
        )

        self.assertEqual(
            [
                "https://Example.org/Paper?ref=One#Section",
                "https://github.com/Example/Repo?tab=readme",
                "https://example.org/Paper?ref=One#Section",
            ],
            [item["url"] for item in occurrences],
        )
        self.assertEqual([2, 3, 4], [item["raw_line"] for item in occurrences])
        self.assertEqual(
            ["raw/links/adversarial.txt:5: expected HTTP(S) URL"],
            warnings,
        )

    def test_inventory_merges_duplicate_urls_and_preserves_fragment_and_query_ids(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            (workspace / "raw" / "links").mkdir(parents=True)
            (workspace / "research.yml").write_text(
                "raw:\n"
                "  source_roots:\n"
                "    - raw/links\n"
                "sources:\n"
                "  manifest_path: sources/manifest.jsonl\n"
                "  normalized_dir: sources/normalized\n"
                "  default_status: discovered\n"
            )
            (workspace / "raw" / "links" / "list.txt").write_text(
                "# links\n"
                "https://example.org/paper?version=1#summary\n"
                "[same url](https://example.org/paper?version=1#summary)\n"
                "https://example.org/paper?version=2#summary\n"
                "mailto:team@example.org\n"
            )

            records, warnings, _ = INVENTORY.build_records(
                workspace,
                INVENTORY.load_config(workspace),
                previous_detected_at={},
            )

        urls = sorted(record["url"] for record in records if record.get("kind") == "web_link")
        self.assertEqual(
            [
                "https://example.org/paper?version=1#summary",
                "https://example.org/paper?version=2#summary",
            ],
            urls,
        )
        self.assertEqual(2, len({record["id"] for record in records}))
        duplicate_record = next(record for record in records if record["url"].endswith("version=1#summary"))
        self.assertEqual(["raw/links/list.txt"], duplicate_record["raw_paths"])
        self.assertTrue(any("expected HTTP(S) URL" in warning for warning in warnings))

    def test_stable_link_ids_are_repeatable_for_cleaned_equivalent_urls(self):
        url = INVENTORY.clean_url(" <https://www.example.org/paper?q=one#frag>. ")
        self.assertEqual("https://www.example.org/paper?q=one#frag", url)
        ids = [INVENTORY.stable_link_id(url) for _ in range(20)]
        self.assertEqual({ids[0]}, set(ids))


class ManifestAdversarialTests(unittest.TestCase):
    def copy_fixture(self, fixture_name: str, workspace: Path) -> Path:
        source = FIXTURES / fixture_name
        target = workspace / fixture_name
        shutil.copytree(source, target)
        return target

    def run_lint(self, project_root: Path) -> dict:
        return LINT.run_checks(project_root, LINT.load_config(project_root))

    def source_manifest_issues(self, results: dict) -> list[dict]:
        return [issue for issue in results["issues"] if issue["category"] == "source_manifest"]

    def test_manifest_reports_missing_required_fields_non_string_ids_and_conflicting_kinds(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            project = self.copy_fixture("minimal-project", Path(tmpdir))
            manifest = project / "sources" / "manifest.jsonl"
            manifest.write_text(
                "\n".join(
                    [
                        json.dumps({"id": 42, "kind": "paper", "raw_paths": ["raw/papers/a.txt"]}),
                        json.dumps({"id": "paper:missing-kind", "raw_paths": ["raw/papers/b.txt"]}),
                        json.dumps({"id": "paper:conflict", "kind": "paper", "raw_paths": ["raw/papers/c.txt"]}),
                        json.dumps({"id": "paper:conflict", "kind": "web_link", "raw_paths": ["raw/links/c.txt"]}),
                    ]
                )
                + "\n"
            )

            results = self.run_lint(project)

        messages = "\n".join(issue["message"] for issue in self.source_manifest_issues(results))
        self.assertIn("line 1", messages)
        self.assertIn("id", messages)
        self.assertIn("line 2", messages)
        self.assertIn("kind", messages)
        self.assertIn("conflicting kind", messages)
        self.assertTrue(all(issue["severity"] == "HIGH" for issue in self.source_manifest_issues(results)))

    def test_manifest_large_valid_line_and_unexpected_nested_metadata_do_not_crash(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            project = self.copy_fixture("minimal-project", Path(tmpdir))
            manifest = project / "sources" / "manifest.jsonl"
            record = {
                "id": "paper:huge-valid",
                "kind": "paper",
                "raw_paths": ["raw/papers/huge.txt"],
                "status": "rejected",
                "metadata": {
                    "nested": [{"value": "x" * 20000, "children": [{"shape": ["unexpected", {"but": "valid"}]}]}],
                },
            }
            manifest.write_text(json.dumps(record) + "\n")

            results = self.run_lint(project)

        self.assertEqual([], self.source_manifest_issues(results))
        self.assertEqual(1, results["stats"]["manifest_records"])
        self.assertEqual("rejected", results["source_coverage"][0]["effective_status"])


if __name__ == "__main__":
    unittest.main()
