"""The supported write path for grounding (CR-7 T5/T7/T8).

Before this path existed, every host that wanted to record grounding hand-edited question
frontmatter: load the YAML, mutate a list, dump it back — reordering keys and retyping
dates, on a file this package also writes under its own lock. These tests hold the two
properties that make the supported path worth using instead.

**Byte-identity.** `grounding set` writes exactly the canonical block, so "byte-identical
to what a compliant hand edit would produce" is a statement a test can check rather than a
claim a reviewer has to take on faith. `CANONICAL_GROUNDING_BLOCK` below is the same
normative example `tests/test_grounding_render.py` pins for the serializer.

**Fail-closed.** Every refusal — bad file, bad claim, terminal status, unknown source,
failing anchor — must leave the page byte-identical to what it was. The assertions read the
page bytes before and after, because "the write did not happen" is the only useful meaning
of a refusal here.
"""

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from tests._script_loader import load_script as load_script_module

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"


RESOLVE = load_script_module("cr7_write_path_resolve", "question_resolve.py")
CLAIM = load_script_module("cr7_write_path_claim", "question_claim.py")
VERIFY = load_script_module("cr7_write_path_verify", "verify_quotes.py")
# The record-naming rule, taken from the code that reads the records rather than restated
# here, so the fixture cannot drift from where the verifier actually looks.
safe_source_id = VERIFY.load_sibling_module("normalize_sources").safe_source_id

SLUG = "supplier-price"
ANCHOR_SOURCE = "data--keepa--b0abc123"
QUOTE_SOURCE = "web:vendor-official-product-spec"
RECORD_BODY = "Vendor-controlled product specification."

# The normative bytes. A host that hand-edits `grounding` must produce exactly these, and
# `grounding set` is specified against them.
CANONICAL_GROUNDING_BLOCK = (
    "grounding:\n"
    '  - claim: "Current supplier price is 23.99 EUR"\n'
    f"    source_id: {ANCHOR_SOURCE}\n"
    "    anchor:\n"
    '      pointer: "supplier_quote/price"\n'
    '      expected: "23.99 EUR"\n'
    f'  - claim: "The product spec is vendor-controlled."\n'
    f'    source_id: "{QUOTE_SOURCE}"\n'
    '    quote: "Vendor-controlled product specification."\n'
    '    location_hint: "Official product spec"'
)

ANCHOR_ENTRY = {
    "claim": "Current supplier price is 23.99 EUR",
    "source_id": ANCHOR_SOURCE,
    "anchor": {"pointer": "supplier_quote/price", "expected": "23.99 EUR"},
}
QUOTE_ENTRY = {
    "claim": "The product spec is vendor-controlled.",
    "source_id": QUOTE_SOURCE,
    "quote": RECORD_BODY,
    "location_hint": "Official product spec",
}
SIDECAR = {"supplier_quote": {"price": "23.99 EUR", "currency": "EUR"}}


class GroundingWriteFixture(unittest.TestCase):
    """A workspace with one claimed question, two manifest sources, and both evidence forms."""

    def make_workspace(self, root: Path, *, status: str = "in_progress", claimed_by: str | None = "agent-a") -> Path:
        target = root / "grounding-write-workspace"
        (target / "wiki" / "questions").mkdir(parents=True)
        normalized = target / "sources" / "normalized"
        normalized.mkdir(parents=True)
        (target / "research.yml").write_text("project:\n  name: Grounding Write Fixture\n", encoding="utf-8")

        (target / "sources" / "manifest.jsonl").write_text(
            "".join(
                json.dumps(
                    {
                        "id": source_id,
                        "kind": "markdown",
                        "raw_paths": [f"raw/{index}.md"],
                        "status": "normalized",
                        "detected_at": "2026-08-01T00:00:00Z",
                    }
                )
                + "\n"
                for index, source_id in enumerate((ANCHOR_SOURCE, QUOTE_SOURCE))
            ),
            encoding="utf-8",
        )

        payload = json.dumps(SIDECAR, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        safe_anchor = safe_source_id(ANCHOR_SOURCE)
        (normalized / f"{safe_anchor}.structured.json").write_bytes(payload)
        anchor_record = {
            "type": "normalized_source",
            "source_id": ANCHOR_SOURCE,
            "title": "Keepa supplier quote",
            "structured_view": {
                "path": f"sources/normalized/{safe_anchor}.structured.json",
                "content_hash": f"sha256:{hashlib.sha256(payload).hexdigest()}",
            },
        }
        (normalized / f"{safe_anchor}.md").write_text(
            "---\n" + yaml.safe_dump(anchor_record, sort_keys=False) + "---\n\n# Keepa supplier quote\n\nRetained.\n",
            encoding="utf-8",
        )

        safe_quote = safe_source_id(QUOTE_SOURCE)
        (normalized / f"{safe_quote}.md").write_text(
            "---\ntype: normalized_source\n"
            f"source_id: {QUOTE_SOURCE}\n"
            "title: Official product spec\n---\n\n"
            f"# Official product spec\n\n{RECORD_BODY}\n",
            encoding="utf-8",
        )

        frontmatter = [
            "type: question",
            f"slug: {SLUG}",
            "question: What is the current supplier price?",
            f"status: {status}",
            "created: 2026-08-01",
            "updated: 2026-08-01",
        ]
        if claimed_by is not None:
            frontmatter += [f"claimed_by: {claimed_by}", 'claimed_at: "2026-08-01T00:00:00Z"']
        (target / "wiki" / "questions" / f"{SLUG}.md").write_text(
            "---\n" + "\n".join(frontmatter) + "\n---\n\n# Supplier price\n\nBody.\n",
            encoding="utf-8",
        )
        return target

    # -- helpers -----------------------------------------------------------------

    def page(self, target: Path) -> Path:
        return target / "wiki" / "questions" / f"{SLUG}.md"

    def page_bytes(self, target: Path) -> bytes:
        return self.page(target).read_bytes()

    def frontmatter(self, target: Path) -> dict:
        return yaml.safe_load(self.page(target).read_text(encoding="utf-8").split("---\n", 2)[1])

    def grounding_file(self, root: Path, document, name: str = "grounding.yml") -> Path:
        path = root / name
        if isinstance(document, str):
            path.write_text(document, encoding="utf-8")
        else:
            path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        return path

    def run_resolve(self, target: Path, *args: str) -> tuple[int, dict, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = RESOLVE.main(["--project-root", str(target), *args, "--format", "json"])
        payload = json.loads(stdout.getvalue() or stderr.getvalue() or "{}")
        return int(code or 0), payload, stderr.getvalue()

    def run_grounding_set(self, target: Path, source: Path, *extra: str, agent_id: str = "agent-a"):
        return self.run_resolve(
            target,
            "grounding", "set",
            "--slug", SLUG,
            "--agent-id", agent_id,
            "--from-file", str(source),
            *extra,
        )

    def write_answer_page(self, target: Path) -> str:
        answer_dir = target / "wiki" / "synthesis"
        answer_dir.mkdir(parents=True, exist_ok=True)
        (answer_dir / "supplier-price.md").write_text(
            "---\ntype: synthesis\nsummary: Supplier price.\n---\n\n# Supplier price\n\nBody.\n",
            encoding="utf-8",
        )
        return "wiki/synthesis/supplier-price.md"

    def run_answer(self, target: Path, *extra: str, agent_id: str = "agent-a"):
        return self.run_resolve(
            target,
            "answer",
            "--slug", SLUG,
            "--agent-id", agent_id,
            "--answer-page", self.write_answer_page(target),
            "--source-id", ANCHOR_SOURCE,
            *extra,
        )


class GroundingSetWriteTests(GroundingWriteFixture):
    def test_written_block_is_byte_identical_to_the_canonical_form(self):
        """The acceptance criterion: what this writes is what a compliant hand edit writes."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.make_workspace(root)
            source = self.grounding_file(root, {"grounding": [ANCHOR_ENTRY, QUOTE_ENTRY]})

            code, payload, stderr = self.run_grounding_set(target, source)
            page_text = self.page(target).read_text(encoding="utf-8")

        self.assertEqual(0, code, stderr)
        self.assertIn(CANONICAL_GROUNDING_BLOCK, page_text, stderr)
        self.assertEqual([ANCHOR_ENTRY, QUOTE_ENTRY], yaml.safe_load(page_text.split("---\n", 2)[1])["grounding"])
        self.assertEqual("grounding set", payload["action"])
        self.assertEqual(2, payload["grounding_count"])
        self.assertEqual({"quote": 1, "anchor": 1}, payload["by_form"])
        self.assertEqual(f"wiki/questions/{SLUG}.md", payload["question_page"])
        self.assertEqual([ANCHOR_SOURCE, QUOTE_SOURCE], payload["source_ids"])

    def test_host_formatting_never_reaches_the_page(self):
        """Two files that differ only in quoting and key order write the same bytes.

        This is why the *validated* entries are rendered rather than the file's raw
        mappings: `expected: 23.99` is a float to YAML and `expected: "23.99"` is a string,
        and a page that recorded whichever one the author happened to type would make the
        canonical form unenforceable.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.make_workspace(root)
            scrambled = (
                "grounding:\n"
                f"  - source_id: {ANCHOR_SOURCE}\n"
                "    anchor:\n"
                "      expected: 23.99 EUR\n"
                "      pointer: supplier_quote/price\n"
                "    claim: '  Current supplier price is 23.99 EUR  '\n"
                f"  - source_id: {QUOTE_SOURCE}\n"
                "    location_hint: Official product spec\n"
                f"    quote: {RECORD_BODY}\n"
                "    claim: The product spec is vendor-controlled.\n"
            )
            source = self.grounding_file(root, scrambled)

            code, _, stderr = self.run_grounding_set(target, source)
            page_text = self.page(target).read_text(encoding="utf-8")

        self.assertEqual(0, code, stderr)
        self.assertIn(CANONICAL_GROUNDING_BLOCK, page_text, stderr)

    def test_a_bare_list_file_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.make_workspace(root)
            source = self.grounding_file(root, [ANCHOR_ENTRY, QUOTE_ENTRY])

            code, _, stderr = self.run_grounding_set(target, source)
            page_text = self.page(target).read_text(encoding="utf-8")

        self.assertEqual(0, code, stderr)
        self.assertIn(CANONICAL_GROUNDING_BLOCK, page_text, stderr)

    def test_a_json_file_is_accepted_because_json_is_yaml(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.make_workspace(root)
            source = root / "grounding.json"
            source.write_text(json.dumps({"grounding": [ANCHOR_ENTRY, QUOTE_ENTRY]}, indent=2), encoding="utf-8")

            code, _, stderr = self.run_grounding_set(target, source)
            page_text = self.page(target).read_text(encoding="utf-8")

        self.assertEqual(0, code, stderr)
        self.assertIn(CANONICAL_GROUNDING_BLOCK, page_text, stderr)

    def test_the_block_is_replaced_not_merged(self):
        """Grounding is authored as a set for one answer; a merge would duplicate claims."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.make_workspace(root)
            first = self.grounding_file(root, {"grounding": [ANCHOR_ENTRY, QUOTE_ENTRY]}, "first.yml")
            second = self.grounding_file(root, {"grounding": [QUOTE_ENTRY]}, "second.yml")

            self.run_grounding_set(target, first)
            code, payload, stderr = self.run_grounding_set(target, second)
            grounding = self.frontmatter(target)["grounding"]

        self.assertEqual(0, code, stderr)
        self.assertEqual([QUOTE_ENTRY], grounding)
        self.assertEqual(1, payload["grounding_count"])

    def test_an_explicit_empty_list_clears_the_block(self):
        """The supported path has to be able to clear grounding, or hosts hand-edit to do it."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.make_workspace(root)
            self.run_grounding_set(target, self.grounding_file(root, {"grounding": [QUOTE_ENTRY]}, "full.yml"))

            code, payload, stderr = self.run_grounding_set(
                target, self.grounding_file(root, {"grounding": []}, "empty.yml")
            )
            frontmatter = self.frontmatter(target)

        self.assertEqual(0, code, stderr)
        self.assertEqual([], frontmatter["grounding"])
        self.assertEqual(0, payload["grounding_count"])
        self.assertEqual({"quote": 0, "anchor": 0}, payload["by_form"])

    def test_stale_verifier_stamps_are_removed_with_the_block(self):
        """The stamps attested entries that no longer exist; leaving them fakes verified state."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.make_workspace(root)
            page = self.page(target)
            page.write_text(
                page.read_text(encoding="utf-8").replace(
                    "updated: 2026-08-01",
                    'updated: 2026-08-01\nverified_by: "verifier-agent"\n'
                    'grounding_verified_at: "2026-08-02T00:00:00Z"',
                ),
                encoding="utf-8",
            )
            self.assertIn("verified_by", self.frontmatter(target))
            source = self.grounding_file(root, {"grounding": [QUOTE_ENTRY]})

            code, _, stderr = self.run_grounding_set(target, source)
            frontmatter = self.frontmatter(target)

        self.assertEqual(0, code, stderr)
        self.assertNotIn("verified_by", frontmatter)
        self.assertNotIn("grounding_verified_at", frontmatter)

    def test_the_envelope_names_verification_as_not_performed_and_what_performs_it(self):
        """No `--verify` flag by design: `verify_quotes.py` already is that seam (CR-7 §7.2)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.make_workspace(root)
            source = self.grounding_file(root, {"grounding": [ANCHOR_ENTRY, QUOTE_ENTRY]})

            code, payload, stderr = self.run_grounding_set(target, source)
            # No `--verify` flag ships, so the remediation is the only route to the check
            # step — and there is exactly one spelling of that step to keep in sync.
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as rejected:
                self.run_grounding_set(target, source, "--verify")

        self.assertEqual(0, code, stderr)
        self.assertEqual("not_performed", payload["verification"])
        self.assertIn("verify_quotes.py", payload["remediation"])
        self.assertIn(f"--slug {SLUG}", payload["remediation"])
        self.assertEqual(2, rejected.exception.code, "--verify must not be a recognized option")

    def test_verification_is_not_required_to_write(self):
        """The two-step flow records grounding while cited evidence may still be normalizing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.make_workspace(root)
            mismatched = {**ANCHOR_ENTRY, "anchor": {"pointer": "supplier_quote/price", "expected": "11.00 EUR"}}
            source = self.grounding_file(root, {"grounding": [mismatched]})

            code, payload, stderr = self.run_grounding_set(target, source)
            grounding = self.frontmatter(target)["grounding"]

        self.assertEqual(0, code, stderr)
        self.assertEqual([mismatched], grounding)
        self.assertEqual("not_performed", payload["verification"])

    def test_a_successful_write_is_what_verify_quotes_then_reads(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.make_workspace(root)
            source = self.grounding_file(root, {"grounding": [ANCHOR_ENTRY, QUOTE_ENTRY]})
            self.run_grounding_set(target, source)

            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                verify_code = VERIFY.main(["--project-root", str(target), "--slug", SLUG, "--format", "json"])
            report = json.loads(stdout.getvalue())

        self.assertEqual(0, verify_code, stderr.getvalue())
        self.assertEqual("verified", report["overall_result"])
        self.assertEqual({"quote": 1, "anchor": 1}, report["counts"]["by_form"])

    def test_the_write_appends_one_log_entry_naming_the_action(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.make_workspace(root)
            source = self.grounding_file(root, {"grounding": [ANCHOR_ENTRY, QUOTE_ENTRY]})

            code, _, stderr = self.run_grounding_set(target, source)
            log = (target / "log.md").read_text(encoding="utf-8")

        self.assertEqual(0, code, stderr)
        self.assertIn("Question grounding recorded", log)
        self.assertIn(f"`{SLUG}` (grounding set)", log)
        self.assertIn("Grounding entries: 2 (quote: 1, anchor: 1).", log)
        self.assertIn("verify_quotes.py", log)

    def test_an_unclaimed_question_needs_the_explicit_flag(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.make_workspace(root, status="open", claimed_by=None)
            source = self.grounding_file(root, {"grounding": [QUOTE_ENTRY]})

            refused_code, refused, _ = self.run_grounding_set(target, source)
            after_refusal = self.page_bytes(target)
            allowed_code, _, stderr = self.run_grounding_set(target, source, "--allow-unclaimed")
            grounding = self.frontmatter(target)["grounding"]

        self.assertEqual(RESOLVE.EXIT_INVALID, refused_code)
        self.assertEqual("QUESTION_NOT_CLAIMED", refused["error_code"])
        self.assertNotIn(b"grounding:", after_refusal)
        self.assertEqual(0, allowed_code, stderr)
        self.assertEqual([QUOTE_ENTRY], grounding)


class GroundingSetRefusalTests(GroundingWriteFixture):
    """Every refusal leaves the page byte-identical. That is what fail-closed means here."""

    def assert_refused(self, unchanged: tuple[bytes, bytes], code: int, payload: dict, expected_code: str) -> None:
        before, after = unchanged
        self.assertEqual(expected_code, payload.get("error_code"), payload)
        self.assertIn(code, {RESOLVE.EXIT_INVALID, RESOLVE.EXIT_CONFLICT})
        self.assertEqual(before, after, "a refused write must leave the page untouched")

    def test_another_agents_claim_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.make_workspace(root, claimed_by="agent-b")
            source = self.grounding_file(root, {"grounding": [QUOTE_ENTRY]})
            before = self.page_bytes(target)

            code, payload, _ = self.run_grounding_set(target, source, agent_id="agent-a")
            after = self.page_bytes(target)

        self.assert_refused((before, after), code, payload, "CLAIM_HELD")
        self.assertEqual(RESOLVE.EXIT_CONFLICT, code)

    def test_allow_unclaimed_does_not_override_another_agents_claim(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.make_workspace(root, claimed_by="agent-b")
            source = self.grounding_file(root, {"grounding": [QUOTE_ENTRY]})
            before = self.page_bytes(target)

            code, payload, _ = self.run_grounding_set(target, source, "--allow-unclaimed", agent_id="agent-a")
            after = self.page_bytes(target)

        self.assert_refused((before, after), code, payload, "CLAIM_HELD")

    def test_terminal_statuses_are_not_rewritten(self):
        for status in RESOLVE.TERMINAL_STATUSES:
            with self.subTest(status=status):
                with tempfile.TemporaryDirectory() as tmpdir:
                    root = Path(tmpdir)
                    target = self.make_workspace(root, status=status, claimed_by=None)
                    source = self.grounding_file(root, {"grounding": [QUOTE_ENTRY]})
                    before = self.page_bytes(target)

                    code, payload, _ = self.run_grounding_set(target, source, "--allow-unclaimed")
                    after = self.page_bytes(target)

                self.assert_refused((before, after), code, payload, "STATUS_NOT_RESOLVABLE")

    def test_an_entry_citing_an_unknown_source_is_refused(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.make_workspace(root)
            source = self.grounding_file(
                root, {"grounding": [QUOTE_ENTRY, {**ANCHOR_ENTRY, "source_id": "web:never-inventoried"}]}
            )
            before = self.page_bytes(target)

            code, payload, _ = self.run_grounding_set(target, source)
            after = self.page_bytes(target)

        self.assert_refused((before, after), code, payload, "SOURCE_UNKNOWN")

    def test_each_malformed_file_class_refuses_with_the_file_code(self):
        cases = {
            "unreadable": None,
            "not_yaml": "grounding:\n  - claim: 'unterminated\n",
            "scalar_document": "just a string\n",
            "mapping_without_grounding": "groundings:\n  - claim: typo\n",
            "grounding_is_not_a_list": "grounding:\n  claim: a mapping, not a list\n",
            "grounding_is_null": "grounding:\n",
        }
        for name, text in cases.items():
            with self.subTest(case=name):
                with tempfile.TemporaryDirectory() as tmpdir:
                    root = Path(tmpdir)
                    target = self.make_workspace(root)
                    before = self.page_bytes(target)
                    source = root / "missing.yml" if text is None else self.grounding_file(root, text)

                    code, payload, _ = self.run_grounding_set(target, source)
                    after = self.page_bytes(target)

                self.assert_refused((before, after), code, payload, "GROUNDING_FILE_INVALID")
                self.assertIn("grounding_file", payload["details"])

    def test_entry_shape_violations_keep_the_verifier_s_own_code(self):
        """Shape rules live in `grounding_entries`; both readers report them identically."""
        cases = {
            "neither_form": {"claim": "c", "source_id": QUOTE_SOURCE},
            "both_forms": {**QUOTE_ENTRY, "anchor": {"pointer": "a/b", "expected": "x"}},
            "missing_claim": {"source_id": QUOTE_SOURCE, "quote": RECORD_BODY},
            "not_a_mapping": "a bare string entry",
            "anchor_without_pointer": {"claim": "c", "source_id": ANCHOR_SOURCE, "anchor": {"expected": "x"}},
            "location_hint_beside_anchor": {**ANCHOR_ENTRY, "location_hint": "nope"},
        }
        for name, entry in cases.items():
            with self.subTest(case=name):
                with tempfile.TemporaryDirectory() as tmpdir:
                    root = Path(tmpdir)
                    target = self.make_workspace(root)
                    before = self.page_bytes(target)
                    source = self.grounding_file(root, {"grounding": [entry]})

                    code, payload, _ = self.run_grounding_set(target, source)
                    after = self.page_bytes(target)

                self.assert_refused((before, after), code, payload, "GROUNDING_INVALID")

    def test_serializer_misuse_becomes_a_refusal_not_a_traceback(self):
        """The renderer's `ValueError` is a signal, not an escape hatch to a stack trace."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(Path(tmpdir))
            text = self.page(target).read_text(encoding="utf-8")

            with self.assertRaises(RESOLVE.ResolveError) as caught:
                RESOLVE.apply_resolution_edits(text, {"grounding": [{"claim": "c"}]}, ())

        self.assertEqual("GROUNDING_INVALID", caught.exception.error_code)
        self.assertEqual(RESOLVE.EXIT_INVALID, caught.exception.exit_code)

    def test_a_held_question_lock_refuses_the_write_without_touching_the_page(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.make_workspace(root)
            source = self.grounding_file(root, {"grounding": [QUOTE_ENTRY]})
            before = self.page_bytes(target)
            question_claim = RESOLVE.load_sibling_module("question_claim")
            unavailable = RESOLVE.LockUnavailableError("question mutation lock is held")

            with mock.patch.object(question_claim, "question_lock", side_effect=unavailable):
                code, payload, _ = self.run_grounding_set(target, source)
            after = self.page_bytes(target)

        self.assertEqual(RESOLVE.EXIT_INVALID, code)
        self.assertEqual("LOCK_UNAVAILABLE", payload["error_code"])
        self.assertEqual(before, after)

    def test_the_page_lock_is_the_only_write_discipline(self):
        """The write happens inside `question_lock` and through `write_page_atomic`, always."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.make_workspace(root)
            source = self.grounding_file(root, {"grounding": [QUOTE_ENTRY]})
            question_claim = RESOLVE.load_sibling_module("question_claim")
            held: list[bool] = []
            real_lock = question_claim.question_lock
            real_write = question_claim.write_page_atomic

            @contextlib.contextmanager
            def tracking_lock(page_path):
                with real_lock(page_path):
                    held.append(True)
                    try:
                        yield
                    finally:
                        held.pop()

            def tracking_write(path, content):
                self.assertTrue(held, "write_page_atomic ran outside the question lock")
                return real_write(path, content)

            with (
                mock.patch.object(question_claim, "question_lock", side_effect=tracking_lock),
                mock.patch.object(question_claim, "write_page_atomic", side_effect=tracking_write) as writes,
            ):
                code, _, stderr = self.run_grounding_set(target, source)
            write_count = writes.call_count

        self.assertEqual(0, code, stderr)
        self.assertEqual(1, write_count, "the block must land in exactly one atomic write")


class AnswerGroundingFileTests(GroundingWriteFixture):
    def test_grounding_and_status_land_in_one_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.make_workspace(root)
            source = self.grounding_file(root, {"grounding": [ANCHOR_ENTRY, QUOTE_ENTRY]})
            question_claim = RESOLVE.load_sibling_module("question_claim")
            snapshots: list[str] = []
            real_write = question_claim.write_page_atomic

            def capture(path, content):
                snapshots.append(content)
                return real_write(path, content)

            with mock.patch.object(question_claim, "write_page_atomic", side_effect=capture):
                code, payload, stderr = self.run_answer(
                    target, "--require-grounding", "--grounding-file", str(source)
                )
            page_text = self.page(target).read_text(encoding="utf-8")
            frontmatter = self.frontmatter(target)

        self.assertEqual(0, code, stderr)
        self.assertEqual(1, len(snapshots), "grounding and status must not be two writes")
        # Together or not at all: the one written document already carries both.
        self.assertIn("status: answered", snapshots[0])
        self.assertIn(CANONICAL_GROUNDING_BLOCK, snapshots[0])
        self.assertIn(CANONICAL_GROUNDING_BLOCK, page_text)
        self.assertEqual("answered", payload["status"])
        self.assertEqual(2, payload["grounding_count"])
        self.assertEqual({"quote": 1, "anchor": 1}, payload["by_form"])
        self.assertTrue(frontmatter["grounding_required"])

    def test_require_grounding_verifies_the_file_not_the_page(self):
        """The page's stale entries must not be what gates an answer carrying new ones."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.make_workspace(root)
            stale = self.grounding_file(
                root,
                {"grounding": [{**QUOTE_ENTRY, "quote": "A sentence no record contains."}]},
                "stale.yml",
            )
            self.run_grounding_set(target, stale)
            self.assertIn("grounding:", self.page(target).read_text(encoding="utf-8"))
            good = self.grounding_file(root, {"grounding": [ANCHOR_ENTRY, QUOTE_ENTRY]}, "good.yml")

            code, payload, stderr = self.run_answer(target, "--require-grounding", "--grounding-file", str(good))
            grounding = self.frontmatter(target)["grounding"]

        self.assertEqual(0, code, stderr)
        self.assertEqual([ANCHOR_ENTRY, QUOTE_ENTRY], grounding)
        self.assertEqual(2, payload["grounding_count"])

    def test_a_failing_anchor_refuses_with_the_anchor_code_and_writes_nothing(self):
        """CR-7's fail-closed acceptance criterion, on the new code path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.make_workspace(root)
            mismatched = {**ANCHOR_ENTRY, "anchor": {"pointer": "supplier_quote/price", "expected": "11.00 EUR"}}
            source = self.grounding_file(root, {"grounding": [mismatched]})
            self.write_answer_page(target)
            before = self.page_bytes(target)

            code, payload, _ = self.run_answer(target, "--require-grounding", "--grounding-file", str(source))
            after = self.page_bytes(target)

        self.assertEqual(RESOLVE.EXIT_INVALID, code)
        self.assertEqual("GROUNDING_ANCHOR_INVALID", payload["error_code"])
        self.assertEqual(1, len(payload["details"]["failures"]))
        self.assertEqual("anchor_value_mismatch", payload["details"]["failures"][0]["result"])
        self.assertEqual(before, after, "a refused answer must leave the page untouched")

    def test_a_failing_quote_keeps_the_quote_code_verbatim(self):
        """Hosts switch on `GROUNDING_QUOTE_INVALID`; an all-quote failure set never renames it."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.make_workspace(root)
            source = self.grounding_file(
                root, {"grounding": [{**QUOTE_ENTRY, "quote": "A sentence no record contains."}]}
            )
            self.write_answer_page(target)
            before = self.page_bytes(target)

            code, payload, _ = self.run_answer(target, "--require-grounding", "--grounding-file", str(source))
            after = self.page_bytes(target)

        self.assertEqual(RESOLVE.EXIT_INVALID, code)
        self.assertEqual("GROUNDING_QUOTE_INVALID", payload["error_code"])
        self.assertEqual(before, after)

    def test_a_mixed_failure_set_reports_the_anchor_code_and_enumerates_everything(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.make_workspace(root)
            source = self.grounding_file(
                root,
                {
                    "grounding": [
                        {**QUOTE_ENTRY, "quote": "A sentence no record contains."},
                        {**ANCHOR_ENTRY, "anchor": {"pointer": "supplier_quote/price", "expected": "11.00 EUR"}},
                    ]
                },
            )
            self.write_answer_page(target)

            code, payload, _ = self.run_answer(target, "--require-grounding", "--grounding-file", str(source))

        self.assertEqual(RESOLVE.EXIT_INVALID, code)
        self.assertEqual("GROUNDING_ANCHOR_INVALID", payload["error_code"])
        self.assertEqual(2, len(payload["details"]["failures"]))

    def test_the_file_writes_without_require_grounding_but_unverified(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.make_workspace(root)
            mismatched = {**ANCHOR_ENTRY, "anchor": {"pointer": "supplier_quote/price", "expected": "11.00 EUR"}}
            source = self.grounding_file(root, {"grounding": [mismatched]})

            code, payload, stderr = self.run_answer(target, "--grounding-file", str(source))
            frontmatter = self.frontmatter(target)

        self.assertEqual(0, code, stderr)
        self.assertEqual([mismatched], frontmatter["grounding"])
        self.assertNotIn("grounding_required", frontmatter)
        self.assertEqual(1, payload["grounding_count"])

    def test_a_malformed_file_refuses_before_the_answer_is_written(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.make_workspace(root)
            source = self.grounding_file(root, "just a string\n")
            self.write_answer_page(target)
            before = self.page_bytes(target)

            code, payload, _ = self.run_answer(target, "--grounding-file", str(source))
            after = self.page_bytes(target)

        self.assertEqual(RESOLVE.EXIT_INVALID, code)
        self.assertEqual("GROUNDING_FILE_INVALID", payload["error_code"])
        self.assertEqual(before, after)

    def test_an_unknown_grounding_source_refuses_before_the_answer_is_written(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.make_workspace(root)
            source = self.grounding_file(
                root, {"grounding": [{**QUOTE_ENTRY, "source_id": "web:never-inventoried"}]}
            )
            self.write_answer_page(target)
            before = self.page_bytes(target)

            code, payload, _ = self.run_answer(target, "--grounding-file", str(source))
            after = self.page_bytes(target)

        self.assertEqual(RESOLVE.EXIT_INVALID, code)
        self.assertEqual("SOURCE_UNKNOWN", payload["error_code"])
        self.assertEqual(before, after)

    def test_answering_with_a_file_drops_stale_verifier_stamps(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.make_workspace(root)
            page = self.page(target)
            page.write_text(
                page.read_text(encoding="utf-8").replace(
                    "updated: 2026-08-01",
                    'updated: 2026-08-01\nverified_by: "verifier-agent"\n'
                    'grounding_verified_at: "2026-08-02T00:00:00Z"',
                ),
                encoding="utf-8",
            )
            source = self.grounding_file(root, {"grounding": [ANCHOR_ENTRY, QUOTE_ENTRY]})

            code, _, stderr = self.run_answer(target, "--require-grounding", "--grounding-file", str(source))
            frontmatter = self.frontmatter(target)

        self.assertEqual(0, code, stderr)
        self.assertNotIn("verified_by", frontmatter)
        self.assertNotIn("grounding_verified_at", frontmatter)

    def test_an_answer_without_the_flag_still_verifies_the_pages_own_grounding(self):
        """The existing behavior is untouched when no file is supplied."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.make_workspace(root)
            self.run_grounding_set(target, self.grounding_file(root, {"grounding": [ANCHOR_ENTRY, QUOTE_ENTRY]}))

            code, payload, stderr = self.run_answer(target, "--require-grounding")
            frontmatter = self.frontmatter(target)

        self.assertEqual(0, code, stderr)
        self.assertEqual("answered", payload["status"])
        self.assertNotIn("grounding_count", payload)
        self.assertEqual([ANCHOR_ENTRY, QUOTE_ENTRY], frontmatter["grounding"])

    def test_a_written_answer_verifies_against_what_it_wrote(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.make_workspace(root)
            source = self.grounding_file(root, {"grounding": [ANCHOR_ENTRY, QUOTE_ENTRY]})
            self.run_answer(target, "--require-grounding", "--grounding-file", str(source))

            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                verify_code = VERIFY.main(["--project-root", str(target), "--slug", SLUG, "--format", "json"])
            report = json.loads(stdout.getvalue())

        self.assertEqual(0, verify_code, stderr.getvalue())
        self.assertEqual("verified", report["overall_result"])


if __name__ == "__main__":
    unittest.main()
