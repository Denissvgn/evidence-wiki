"""Frontmatter scalars these scripts write must reload as the strings they wrote.

`question_claim.py` and `question_resolve.py` render frontmatter by hand rather than
dumping YAML, so quoting is their own decision. Three pre-existing defects lived in it:

- `quote_scalar` chose bare output by character membership, which cannot express YAML's
  type inference. `007` reloaded as an int, `true`/`yes`/`on` as bools, `null` as None,
  `2026-08-09` as a date, a padded value lost its spaces, and a leading `-` made the page
  unparseable. It reached live data through `human_reviews`, whose `--note` and
  `--review-ref` are free-text host prose.
- The sequence branch of `render_frontmatter_value` applied no quoting at all, so a list
  item could reload as a mapping or a nested list — structural corruption of `source_ids`,
  `blocking_request_ids` and `human_review_policies`, not merely a retyped scalar.
- `question_claim.render_scalar` emitted values raw, and its "quoted" branch interpolated
  into `"..."` without escaping. `claimed_by` carries `--agent-id` verbatim on the first
  write of a question's lifecycle, so an id like `- dash` or `a: b` left a page that no
  longer parsed — stranding the question, because the release path must read the page it
  can no longer read.

These tests pin the property rather than the implementation: whatever a renderer emits
must survive `yaml.safe_load` unchanged, in the syntactic position it was written to.
"""

import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
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


SCALAR_SAFETY_RESOLVE = load_script_module("cr7_scalar_safety_resolve", "question_resolve.py")
SCALAR_SAFETY_CLAIM = load_script_module("cr7_scalar_safety_claim", "question_claim.py")


# Values chosen because each one broke, or plausibly could break, the character-class rule:
# YAML 1.1 booleans and null, leading-zero and hex and float shapes, dates, padding, block
# indicators, YAML metacharacters, and ordinary prose a reviewer might actually type.
HOSTILE_SCALARS = (
    "007",
    "0x1f",
    "0o17",
    "0b1010",
    "1e5",
    "23.99",
    "+5",
    "-5",
    "1_000",
    "12:30",
    ".inf",
    ".nan",
    "true",
    "false",
    "yes",
    "no",
    "on",
    "off",
    "null",
    "~",
    "2026-08-09",
    "2026-08-09T10:11:12Z",
    "  padded  ",
    " leading",
    "trailing ",
    "- dash leading",
    "-",
    "? question",
    ": colon",
    "has: colon space",
    "#hash",
    "trailing #comment",
    "*alias",
    "&anchor",
    "!!str tagged",
    "%directive",
    "@at",
    "`backtick",
    '"quoted"',
    "'single'",
    "{brace}",
    "[bracket]",
    "a, b",
    "|pipe",
    ">fold",
    "multi\nline",
    "tab\there",
    "café",
    "…ellipsis",
    "plain value",
    "web:vendor-official-product-spec",
    "pack:market-data/quote-48h",
)

# Ids the workspace really writes into list fields. These reload identically bare, so the
# renderer must leave them alone: quoting them would rewrite the `source_ids` of every
# existing workspace on its next resolution.
MANIFEST_IDS = (
    "web:vendor-official-product-spec",
    "pack:market-data/quote-48h",
    "raw:bench-survey-2026",
    "req-20260704-current-official-figure",
    "data--keepa--b0abc123",
    "arxiv:2604.13018v1",
)


class MappingValueScalarTests(unittest.TestCase):
    """`quote_scalar` renders a mapping value that reloads as the string it was given."""

    def test_every_hostile_scalar_reloads_unchanged(self):
        for value in HOSTILE_SCALARS:
            with self.subTest(value=value):
                rendered = SCALAR_SAFETY_RESOLVE.quote_scalar(value)
                loaded = yaml.safe_load(f"probe: {rendered}")
                self.assertEqual({"probe": value}, loaded)

    def test_the_empty_string_survives(self):
        rendered = SCALAR_SAFETY_RESOLVE.quote_scalar("")
        self.assertEqual({"probe": ""}, yaml.safe_load(f"probe: {rendered}"))

    def test_a_leading_dash_no_longer_makes_the_page_unparseable(self):
        """The sharpest form of the defect: the page stopped being YAML at all.

        `human_reviews` carries `--note` and `--review-ref` verbatim from the host, so a
        reviewer note beginning with a dash wrote a question page nothing could read back.
        """
        rendered = SCALAR_SAFETY_RESOLVE.quote_scalar("- dash leading")
        self.assertEqual({"probe": "- dash leading"}, yaml.safe_load(f"probe: {rendered}"))

    def test_ordinary_identifiers_are_still_emitted_bare(self):
        for value in ("plain", "a b c", "answer-agent", "wiki/synthesis/example.md", "1e5"):
            with self.subTest(value=value):
                self.assertEqual(value, SCALAR_SAFETY_RESOLVE.quote_scalar(value))


class SequenceItemScalarTests(unittest.TestCase):
    """The list branch of `render_frontmatter_value` quotes on the same round-trip rule."""

    def render_list(self, value: str) -> list[str]:
        return SCALAR_SAFETY_RESOLVE.render_frontmatter_value("source_ids", [value], False)

    def test_every_hostile_scalar_reloads_unchanged_as_a_list_item(self):
        for value in HOSTILE_SCALARS:
            with self.subTest(value=value):
                block = "\n".join(self.render_list(value))
                self.assertEqual({"source_ids": [value]}, yaml.safe_load(block))

    def test_a_colon_space_item_no_longer_reloads_as_a_mapping(self):
        """Structural corruption, not retyping: the item became a dict inside the list."""
        block = "\n".join(self.render_list("has: colon space"))
        self.assertEqual({"source_ids": ["has: colon space"]}, yaml.safe_load(block))

    def test_a_dash_item_no_longer_reloads_as_a_nested_list(self):
        block = "\n".join(self.render_list("- leading dash"))
        self.assertEqual({"source_ids": ["- leading dash"]}, yaml.safe_load(block))

    def test_manifest_identifiers_stay_bare(self):
        """Correct output is not enough — it must also not churn existing workspaces.

        Every id here reloads identically bare, so quoting it would rewrite files for no
        gain. The round-trip rule is what keeps the renderer minimal here; the character
        class `quote_scalar` uses would have quoted every colon-bearing id.
        """
        for value in MANIFEST_IDS:
            with self.subTest(value=value):
                lines = self.render_list(value)
                self.assertEqual(f"  - {value}", lines[1])
                self.assertEqual({"source_ids": [value]}, yaml.safe_load("\n".join(lines)))

    def test_a_multi_item_list_reloads_in_order(self):
        lines = SCALAR_SAFETY_RESOLVE.render_frontmatter_value(
            "source_ids", ["web:vendor-spec", "007", "- dash", "raw:bench"], False
        )
        loaded = yaml.safe_load("\n".join(lines))
        self.assertEqual({"source_ids": ["web:vendor-spec", "007", "- dash", "raw:bench"]}, loaded)

    def test_an_empty_list_is_still_the_empty_flow_sequence(self):
        self.assertEqual(
            ["source_ids: []"],
            SCALAR_SAFETY_RESOLVE.render_frontmatter_value("source_ids", [], False),
        )


class HumanReviewsScalarTests(unittest.TestCase):
    """`human_reviews` is the live path the mapping-value defect reached production through."""

    def test_free_text_review_fields_reload_unchanged(self):
        entries = [
            {
                "policy": "pack:market-data/quote-48h",
                "verdict": "accepted",
                "reviewed_by": "counsel",
                "review_ref": "- TICKET-14",
                "note": "  padded note  ",
                "reviewed_at": "2026-08-09T10:11:12Z",
            }
        ]
        block = "\n".join(SCALAR_SAFETY_RESOLVE.render_mapping_sequence("human_reviews", entries))
        self.assertEqual({"human_reviews": entries}, yaml.safe_load(block))

    def test_a_note_of_every_hostile_shape_reloads_unchanged(self):
        for value in HOSTILE_SCALARS:
            with self.subTest(value=value):
                entries = [{"policy": "p", "note": value}]
                block = "\n".join(
                    SCALAR_SAFETY_RESOLVE.render_mapping_sequence("human_reviews", entries)
                )
                self.assertEqual({"human_reviews": entries}, yaml.safe_load(block))


class ClaimScalarTests(unittest.TestCase):
    """`question_claim.render_scalar` writes `claimed_by` straight from `--agent-id`."""

    def test_every_hostile_agent_id_reloads_unchanged(self):
        for value in HOSTILE_SCALARS:
            with self.subTest(value=value):
                rendered = SCALAR_SAFETY_CLAIM.render_scalar(value, False)
                self.assertEqual({"probe": value}, yaml.safe_load(f"probe: {rendered}"))

    def test_an_agent_id_that_would_have_stranded_the_question_now_reloads(self):
        """`- dash` and `a: b` used to leave a page nothing could parse.

        That is worse than a retyped value: `release` and every other verb have to read
        the page before they can rewrite it, so the question could not even be unclaimed.
        """
        for value in ("- dash", "a: b"):
            with self.subTest(value=value):
                rendered = SCALAR_SAFETY_CLAIM.render_scalar(value, False)
                self.assertEqual({"probe": value}, yaml.safe_load(f"probe: {rendered}"))

    def test_the_quoted_branch_escapes_instead_of_interpolating(self):
        """The old branch was `f'\"{value}\"'`, which escapes nothing."""
        for value in ('ag"ent', "back\\slash", 'both"\\kinds'):
            with self.subTest(value=value):
                rendered = SCALAR_SAFETY_CLAIM.render_scalar(value, True)
                self.assertEqual({"probe": value}, yaml.safe_load(f"probe: {rendered}"))

    def test_ordinary_agent_ids_and_timestamps_are_unchanged(self):
        for value in ("agent-a", "answer-agent", "verifier-agent"):
            with self.subTest(value=value):
                self.assertEqual(value, SCALAR_SAFETY_CLAIM.render_scalar(value, False))
        self.assertEqual(
            '"2026-08-09T10:11:12Z"',
            SCALAR_SAFETY_CLAIM.render_scalar("2026-08-09T10:11:12Z", True),
        )

    def test_the_empty_string_survives(self):
        for quote in (False, True):
            with self.subTest(quote=quote):
                rendered = SCALAR_SAFETY_CLAIM.render_scalar("", quote)
                self.assertEqual({"probe": ""}, yaml.safe_load(f"probe: {rendered}"))


class ClaimRoundTripThroughDiskTests(unittest.TestCase):
    """The property that matters end to end: claim, then read the page back."""

    def make_workspace(self, root: Path) -> Path:
        target = root / "claim-scalar-workspace"
        (target / "wiki" / "questions").mkdir(parents=True)
        (target / "research.yml").write_text("project:\n  name: Fixture\n", encoding="utf-8")
        (target / "wiki" / "questions" / "q1.md").write_text(
            "---\ntype: question\nslug: q1\nstatus: open\n---\n\n# Q\n", encoding="utf-8"
        )
        return target

    def test_a_claimed_page_still_parses_for_every_hostile_agent_id(self):
        results = {}
        for value in ("- dash", "a: b", "007", "true", "null", 'ag"ent'):
            with tempfile.TemporaryDirectory() as tmpdir:
                target = self.make_workspace(Path(tmpdir))
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    SCALAR_SAFETY_CLAIM.main(
                        ["--project-root", str(target), "claim", "--slug", "q1",
                         "--agent-id", value, "--format", "json"]
                    )
                page = (target / "wiki" / "questions" / "q1.md").read_text(encoding="utf-8")
                frontmatter = page.split("---")[1]
                try:
                    results[value] = yaml.safe_load(frontmatter).get("claimed_by")
                except yaml.YAMLError as exc:
                    results[value] = f"<unparseable: {type(exc).__name__}>"

        for value, claimed_by in results.items():
            with self.subTest(value=value):
                self.assertEqual(value, claimed_by)


if __name__ == "__main__":
    unittest.main()
