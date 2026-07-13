from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = REPO_ROOT / "workspace-template"
EXAMPLE_ROOT = REPO_ROOT / "examples" / "urban-heat-resilience-workspace"

DOC_PAIRS = {
    "obsidian-dataview.md": "## Comparative Usability Checklist",
    "obsidian-templates.md": "## Plugin-Free Copy Workflow",
}
EXPECTED_TEMPLATES = {
    "claim.md": "claim",
    "concept.md": "concept",
    "decision.md": "decision",
    "source-note.md": "source",
    "synthesis.md": "synthesis",
}
MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")


def frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    parts = text.split("---", 2)
    if len(parts) != 3 or parts[0].strip():
        raise AssertionError(f"missing leading frontmatter: {path}")
    payload = yaml.safe_load(parts[1])
    if not isinstance(payload, dict):
        raise AssertionError(f"frontmatter is not a mapping: {path}")
    return payload


def local_markdown_target(raw_target: str) -> PurePosixPath | None:
    target = raw_target.strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1].strip()
    target = unquote(target.split("#", 1)[0])
    parsed = urlparse(target)
    if not target or parsed.scheme or target.startswith("#"):
        return None
    if "\\" in target:
        raise AssertionError(f"Markdown link must use forward slashes: {raw_target}")
    return PurePosixPath(target)


def resolve_with_exact_case(source: Path, relative: PurePosixPath) -> Path | None:
    current = source.parent
    for component in relative.parts:
        if component in {"", "."}:
            continue
        if component == "..":
            current = current.parent
            continue
        if not current.is_dir():
            return None
        names = {entry.name for entry in current.iterdir()}
        if component not in names:
            return None
        current = current / component
    return current if current.is_file() else None


class MarkdownAndTemplateDiagnosticTests(unittest.TestCase):
    def test_local_source_tree_initializes_and_lints_spaced_unicode_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "Research Workspace – Café Heat"
            init = subprocess.run(
                [
                    sys.executable,
                    str(TEMPLATE_ROOT / "scripts" / "init_research_workspace.py"),
                    "--target",
                    str(target),
                    "--project-name",
                    "cafe-heat-diagnostic",
                    "--project-description",
                    "Local source-tree path diagnostic.",
                    "--owner-goal",
                    "Verify deterministic Unicode and spaced-path setup.",
                ],
                cwd=REPO_ROOT,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(0, init.returncode, init.stderr)

            for script in ("smoke_validate_workspace.py", "lint.py"):
                result = subprocess.run(
                    [
                        sys.executable,
                        str(target / "scripts" / script),
                        "--project-root",
                        str(target),
                        "--format",
                        "json",
                    ],
                    cwd=target,
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                )
                self.assertEqual(0, result.returncode, f"{script}: {result.stdout}\n{result.stderr}")

            copied_links = 0
            for filename in DOC_PAIRS:
                document = target / "docs" / filename
                for raw_target in MARKDOWN_LINK_RE.findall(document.read_text(encoding="utf-8")):
                    local_target = local_markdown_target(raw_target)
                    if local_target is None:
                        continue
                    copied_links += 1
                    self.assertIsNotNone(
                        resolve_with_exact_case(document, local_target),
                        f"initialized workspace link: {filename} -> {raw_target}",
                    )
            self.assertGreater(copied_links, 0)

    def test_all_templates_and_example_pages_have_parseable_frontmatter(self) -> None:
        required = {"type", "created", "updated", "source_ids", "summary"}
        for root in (TEMPLATE_ROOT, EXAMPLE_ROOT):
            template_dir = root / ".obsidian" / "templates"
            found = {path.name for path in template_dir.glob("*.md")}
            self.assertEqual(set(EXPECTED_TEMPLATES), found)
            for name, page_type in EXPECTED_TEMPLATES.items():
                metadata = frontmatter(template_dir / name)
                self.assertEqual(page_type, metadata["type"])
                self.assertTrue(required <= set(metadata), name)

        wiki_pages = sorted((EXAMPLE_ROOT / "wiki").glob("*/*.md"))
        self.assertGreaterEqual(len(wiki_pages), 12)
        for page in wiki_pages:
            metadata = frontmatter(page)
            self.assertTrue(required <= set(metadata), page)
            body = page.read_text(encoding="utf-8").split("---", 2)[2]
            self.assertIn("# ", body, page)

    def test_unicode_spaced_path_and_wrong_case_probe_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "Research Workspace – Café"
            concepts = workspace / "wiki" / "concepts"
            concepts.mkdir(parents=True)
            destination = concepts / "café-cooling.md"
            shutil.copy2(TEMPLATE_ROOT / ".obsidian" / "templates" / "concept.md", destination)
            destination.write_text(
                destination.read_text(encoding="utf-8")
                .replace("YYYY-MM-DD", "2026-07-11")
                .replace("Short concept summary.", "Cooling concept used by the Unicode path diagnostic.")
                .replace("# Concept Name", "# Café Cooling"),
                encoding="utf-8",
            )
            index = workspace / "index.md"
            index.write_text("# Index\n\n[Café Cooling](wiki/concepts/café-cooling.md)\n", encoding="utf-8")

            target = local_markdown_target("wiki/concepts/café-cooling.md")
            self.assertIsNotNone(target)
            self.assertTrue((workspace / Path(*target.parts)).is_file())
            self.assertEqual(destination, resolve_with_exact_case(index, target))
            self.assertTrue(unicodedata.is_normalized("NFC", destination.name))
            self.assertEqual("café-cooling.md", unicodedata.normalize("NFC", "cafe\u0301-cooling.md"))
            self.assertIsNone(
                resolve_with_exact_case(index, PurePosixPath("wiki/concepts/Café-cooling.md")),
                "wrong-case links must not pass through host case folding",
            )
            self.assertEqual("concept", frontmatter(destination)["type"])

    def test_plain_markdown_example_is_complete_with_plugins_disabled(self) -> None:
        config = yaml.safe_load((EXAMPLE_ROOT / "research.yml").read_text(encoding="utf-8"))
        obsidian = config["integrations"]["obsidian"]
        self.assertIs(obsidian["enabled"], False)
        self.assertEqual("optional", obsidian["dataview"])

        index = EXAMPLE_ROOT / "index.md"
        text = index.read_text(encoding="utf-8")
        self.assertNotIn("```dataview", text)
        self.assertNotIn("[[", text)
        linked_pages = set()
        for raw_target in MARKDOWN_LINK_RE.findall(text):
            target = local_markdown_target(raw_target)
            if target is None or not target.as_posix().startswith("wiki/"):
                continue
            resolved = resolve_with_exact_case(index, target)
            self.assertIsNotNone(resolved, raw_target)
            linked_pages.add(resolved.relative_to(EXAMPLE_ROOT).as_posix())
        maintained_pages = {
            path.relative_to(EXAMPLE_ROOT).as_posix() for path in (EXAMPLE_ROOT / "wiki").glob("*/*.md")
        }
        self.assertEqual(maintained_pages, linked_pages)


if __name__ == "__main__":
    unittest.main()
