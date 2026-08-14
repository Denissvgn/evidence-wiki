"""Every text read, write, and subprocess decode names its encoding explicitly.

Python's text mode defaults to `locale.getpreferredencoding(False)`, which is UTF-8 on
macOS and Linux but the ANSI code page -- typically cp1252 -- on Windows outside UTF-8
mode. This package writes and reads its own workspace artifacts, so a default-encoded
writer and a UTF-8 reader agree on every developer machine and disagree only on a user's
Windows box: normalizing a source whose text leaves cp1252 raises `UnicodeEncodeError`
mid-write, and text cp1252 *can* encode is written as cp1252 bytes that the UTF-8 readers
then refuse. `.github/workflows/ci.yml` sets `PYTHONUTF8: "1"`, so CI cannot catch this
class on its own -- which is exactly why it is pinned here instead.

`write_text` additionally defaults to `newline=None`, rewriting "\\n" to `os.linesep`.
Generated artifacts are hashed, diffed, and line-compared, so writes pin `newline="\\n"`
to stay byte-identical across platforms.

The scan is deliberately syntactic: it asks whether the argument is present, not whether
the value is reachable, so it cannot drift from the source the way a prose convention
does.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCANNED_ROOTS = (
    REPO_ROOT / "workspace-template" / "scripts",
    REPO_ROOT / "src" / "evidence_wiki",
    REPO_ROOT / "tools",
)

TEXT_IO_METHODS = {"read_text", "write_text"}
SUBPROCESS_CALLS = {"run", "Popen", "check_output"}


def python_sources() -> list[Path]:
    found: list[Path] = []
    for root in SCANNED_ROOTS:
        if root.exists():
            found.extend(sorted(root.rglob("*.py")))
    return found


def call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return getattr(func, "id", None)


def is_os_or_archive_open(node: ast.Call) -> bool:
    """`os.open` takes a descriptor and `tarfile.open` reads bytes: neither decodes text."""
    func = node.func
    if not isinstance(func, ast.Attribute):
        return False
    module = getattr(func.value, "id", None)
    return func.attr == "open" and module in {"os", "tarfile", "zipfile", "gzip", "tempfile"}


def keyword_names(node: ast.Call) -> set[str]:
    return {keyword.arg for keyword in node.keywords if keyword.arg}


def uses_text_mode(node: ast.Call) -> bool:
    for keyword in node.keywords:
        if keyword.arg in {"text", "universal_newlines"}:
            if isinstance(keyword.value, ast.Constant) and keyword.value.value is True:
                return True
    return False


class TextEncodingContractTests(unittest.TestCase):
    def scan(self, predicate) -> list[str]:
        offenders: list[str] = []
        for path in python_sources():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and predicate(node):
                    relative = path.relative_to(REPO_ROOT)
                    offenders.append(f"{relative}:{node.lineno}: {call_name(node)}()")
        return offenders

    def test_text_reads_and_writes_declare_an_encoding(self):
        def offends(node: ast.Call) -> bool:
            if call_name(node) not in TEXT_IO_METHODS:
                return False
            if not isinstance(node.func, ast.Attribute):
                # A module-level helper of the same name, not Path.read_text.
                return False
            return "encoding" not in keyword_names(node)

        offenders = self.scan(offends)
        self.assertEqual(
            [],
            offenders,
            "These calls decode or encode with the process locale, which is cp1252 on "
            'Windows outside UTF-8 mode. Pass encoding="utf-8":\n' + "\n".join(offenders),
        )

    def test_text_writes_pin_the_line_ending(self):
        def offends(node: ast.Call) -> bool:
            if call_name(node) != "write_text" or not isinstance(node.func, ast.Attribute):
                return False
            return "newline" not in keyword_names(node)

        offenders = self.scan(offends)
        self.assertEqual(
            [],
            offenders,
            'These writes let Python rewrite "\\n" to os.linesep, so the same artifact '
            "differs byte-for-byte on Windows. Pass newline=\"\\n\":\n" + "\n".join(offenders),
        )

    def test_text_mode_subprocesses_declare_an_encoding(self):
        def offends(node: ast.Call) -> bool:
            if call_name(node) not in SUBPROCESS_CALLS:
                return False
            return uses_text_mode(node) and "encoding" not in keyword_names(node)

        offenders = self.scan(offends)
        self.assertEqual(
            [],
            offenders,
            "These calls decode child-process output with the process locale. The tools "
            'this package invokes emit UTF-8; pass encoding="utf-8":\n' + "\n".join(offenders),
        )

    def test_file_open_declares_an_encoding_in_text_mode(self):
        def offends(node: ast.Call) -> bool:
            if call_name(node) != "open" or is_os_or_archive_open(node):
                return False
            if "encoding" in keyword_names(node):
                return False

            mode: str | None = None
            for keyword in node.keywords:
                if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
                    mode = keyword.value.value

            if isinstance(node.func, ast.Attribute):
                # `something.open(...)`. Only a file open takes a string mode first;
                # `opener.open(request, timeout=...)` and friends are not file I/O, and
                # nothing static distinguishes them beyond that argument shape.
                leading = node.args[0] if node.args else None
                if isinstance(leading, ast.Constant) and isinstance(leading.value, str):
                    mode = leading.value
                elif mode is None:
                    return False
            else:
                positional = node.args[1] if len(node.args) > 1 else None
                if isinstance(positional, ast.Constant):
                    mode = positional.value

            return not (isinstance(mode, str) and "b" in mode)

        offenders = self.scan(offends)
        self.assertEqual(
            [],
            offenders,
            'These text-mode opens rely on the process locale. Pass encoding="utf-8":\n'
            + "\n".join(offenders),
        )

    def test_the_scan_actually_reaches_the_shipped_scripts(self):
        """A guard that silently scanned nothing would pass forever."""
        scanned = python_sources()
        self.assertGreater(len(scanned), 40, "encoding scan found suspiciously few sources")
        names = {path.name for path in scanned}
        for expected in ("normalize_sources.py", "intake_questions.py", "orchestration.py"):
            self.assertIn(expected, names)


if __name__ == "__main__":
    unittest.main()
