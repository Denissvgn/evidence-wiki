"""Input limits bound file reads while preserving extraction and sampling semantics."""

import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._script_loader import load_script

INVENTORY = load_script("bounded_inventory", "source_inventory.py")
NORMALIZE = load_script("bounded_normalize", "normalize_sources.py")


class BudgetedReader:
    def __init__(self, handle, remaining):
        self.handle = handle
        self.remaining = remaining

    def __enter__(self):
        self.handle.__enter__()
        return self

    def __exit__(self, *args):
        return self.handle.__exit__(*args)

    def read(self, size=-1):
        if not 0 <= size <= self.remaining[0]:
            raise AssertionError(f"read({size}) exceeds the remaining input budget {self.remaining[0]}")
        data = self.handle.read(size)
        self.remaining[0] -= len(data)
        return data


class BoundedSourceReadTests(unittest.TestCase):
    @contextlib.contextmanager
    def assert_read_budget(self, path, limit):
        original = path.read_bytes()
        original_open = Path.open
        remaining = [limit]
        handles = []

        def bounded_open(opened_path, *args, **kwargs):
            handle = original_open(opened_path, *args, **kwargs)
            if opened_path == path:
                handles.append(handle)
                return BudgetedReader(handle, remaining)
            return handle

        with mock.patch.object(Path, "open", bounded_open):
            yield

        self.assertTrue(handles, "the source file must have been read")
        self.assertTrue(all(handle.closed for handle in handles))
        self.assertEqual(original, path.read_bytes(), "reading must preserve the raw artifact")

    def test_html_reads_are_bounded_below_at_and_above_the_limit(self):
        limit = NORMALIZE.HTML_MAX_BYTES
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "page.html"
            for size in (limit - 1, limit, limit + 1, limit * 2):
                with self.subTest(size=size):
                    path.write_bytes(b"a" * size)
                    with self.assert_read_budget(path, limit + 1):
                        text, warnings = NORMALIZE.read_html_text(path, path.name)

                    self.assertEqual("a" * min(size, limit), text)
                    self.assertEqual(
                        [f"page.html: HTML file exceeds {limit} bytes; extraction truncated"] if size > limit else [],
                        warnings,
                    )

    def test_html_preserves_utf8_replacement_at_the_byte_boundary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "page.html"
            path.write_bytes(b"abc\xe2\x82\xac\xfftail")
            with mock.patch.object(NORMALIZE, "HTML_MAX_BYTES", 5), self.assert_read_budget(path, 6):
                text, warnings = NORMALIZE.read_html_text(path, path.name)

            self.assertEqual("abc\ufffd", text)
            self.assertEqual(["page.html: HTML file exceeds 5 bytes; extraction truncated"], warnings)

    def test_table_reads_are_bounded_below_at_and_above_the_limit(self):
        limit = NORMALIZE.TABLE_MAX_BYTES
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "rows.csv"
            for size in (limit - 1, limit, limit + 1, limit * 2):
                with self.subTest(size=size):
                    payload = (b"a,b\n" * (size // 4 + 1))[:size]
                    path.write_bytes(payload)
                    with self.assert_read_budget(path, limit + 1):
                        text, truncated, warnings = NORMALIZE.read_table_text(path, path.name)

                    expected = payload[:limit].decode("utf-8")
                    if size > limit:
                        expected = expected[: expected.rfind("\n") + 1]
                    self.assertEqual(expected, text)
                    self.assertEqual(size > limit, truncated)
                    self.assertEqual(
                        [f"rows.csv: table file exceeds {limit} bytes; row scan truncated"] if size > limit else [],
                        warnings,
                    )

    def test_table_preserves_bom_decoding_crlf_and_complete_rows(self):
        prefix = b"\xef\xbb\xbfa,b\r\n1,\xc3\xa9\r\n"
        limit = len(prefix) + 2
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "rows.csv"
            path.write_bytes(prefix + b"2,partial\r\n")
            with mock.patch.object(NORMALIZE, "TABLE_MAX_BYTES", limit), self.assert_read_budget(path, limit + 1):
                text, truncated, warnings = NORMALIZE.read_table_text(path, path.name)

            self.assertEqual("a,b\r\n1,é\r\n", text)
            self.assertTrue(truncated)
            self.assertEqual(1, len(warnings))

    def test_table_drops_a_truncated_first_line_but_keeps_an_uncut_final_line(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "rows.tsv"
            for payload, expected, truncated in ((b"abcdef", "", True), (b"a\tb", "a\tb", False)):
                with self.subTest(payload=payload):
                    path.write_bytes(payload)
                    with mock.patch.object(NORMALIZE, "TABLE_MAX_BYTES", 4), self.assert_read_budget(path, 5):
                        text, actual_truncated, warnings = NORMALIZE.read_table_text(path, path.name)

                    self.assertEqual(expected, text)
                    self.assertEqual(truncated, actual_truncated)
                    self.assertEqual(int(truncated), len(warnings))

    def test_missing_inputs_keep_the_existing_warning_results(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "missing.html"
            text, warnings = NORMALIZE.read_html_text(path, path.name)
            self.assertEqual("", text)
            self.assertIn("missing.html: cannot read HTML file:", warnings[0])

            path = Path(tmpdir) / "missing.csv"
            text, truncated, warnings = NORMALIZE.read_table_text(path, path.name)
            self.assertEqual("", text)
            self.assertFalse(truncated)
            self.assertIn("missing.csv: cannot read table file:", warnings[0])

    def test_link_sampling_reads_only_the_existing_character_window(self):
        limit = 8192
        prefix = "https://example.test/"
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "links.txt"
            for separator, expected in (("", True), (" ", False)):
                with self.subTest(separator=separator):
                    sample = prefix + "é" * (limit - len(prefix) - len(separator) - 1) + separator + "é"
                    path.write_bytes((sample + "\r\nnot a link\r\n").encode("utf-8"))
                    with self.assert_read_budget(path, limit):
                        actual = INVENTORY.looks_like_link_file(path, path.parent)

                    self.assertEqual(expected, actual)

    def test_latex_sampling_reads_only_the_existing_character_window(self):
        limit = 20000
        directive = "\\documentclass{article}"
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "candidate.tex"
            for inside in (True, False):
                with self.subTest(directive_inside_window=inside):
                    padding = limit - len(directive) if inside else limit
                    path.write_bytes(("é" * padding + directive + "\r\nignored tail").encode("utf-8"))
                    with self.assert_read_budget(path, limit):
                        selected = INVENTORY.fallback_entrypoint(path.parent)

                    expected = ("candidate.tex", "fallback_documentclass") if inside else (None, None)
                    self.assertEqual(expected, selected)
