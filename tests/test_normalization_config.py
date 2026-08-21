"""The optional `research.yml` `normalization:` section.

Configuring an adapter authorizes this package to execute a command, so the reader is
strict and every rejection is covered here. A malformed section must fail loudly rather
than degrade to "no adapter", which would let a workspace believe its structured
evidence had been normalized when nothing ran.
"""

import re
import tempfile
import unittest
from pathlib import Path

import yaml

from tests._script_loader import load_script as load_script_module

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"


CONFIG = load_script_module("research_normalization_config", "_normalization_config.py")
NORMALIZE = load_script_module("normalization_config_tests_normalize", "normalize_sources.py")


def adapter(**overrides):
    declaration = {
        "kinds": ["structured_data"],
        "provider": "command",
        "command": ["autoseller-normalize", "--format", "json"],
        "name": "autoseller-normalize",
        "version": "1.4.0",
    }
    declaration.update(overrides)
    return declaration


def config(*declarations):
    return {"normalization": {"adapters": list(declarations)}}


class NormalizationConfigTests(unittest.TestCase):
    def assertRejected(self, document, *, contains: str):
        with self.assertRaises(CONFIG.NormalizationConfigError) as caught:
            CONFIG.normalization_config(document)
        self.assertEqual("CONFIG_INVALID", caught.exception.error_code)
        self.assertIn(contains, caught.exception.message)
        self.assertTrue(caught.exception.remediation)
        return caught.exception

    # -- absence and defaults ----------------------------------------------------

    def test_absent_section_configures_no_adapter(self):
        self.assertEqual({"adapters": ()}, CONFIG.normalization_config({}))
        self.assertEqual({"adapters": ()}, CONFIG.normalization_config({"normalization": None}))

    def test_absent_and_empty_adapters_are_equivalent(self):
        self.assertEqual((), CONFIG.normalization_config({"normalization": {}})["adapters"])
        self.assertEqual((), CONFIG.normalization_config(config())["adapters"])

    def test_valid_declaration_round_trips(self):
        configured = CONFIG.normalization_config(config(adapter()))["adapters"]

        self.assertEqual(1, len(configured))
        entry = configured[0]
        self.assertEqual(("structured_data",), entry.kinds)
        self.assertEqual("command", entry.provider)
        self.assertEqual(("autoseller-normalize", "--format", "json"), entry.command)
        self.assertEqual("autoseller-normalize", entry.name)
        self.assertEqual("1.4.0", entry.version)
        self.assertEqual(CONFIG.DEFAULT_TIMEOUT_SECONDS, entry.timeout_seconds)

    def test_explicit_timeout_is_kept(self):
        configured = CONFIG.normalization_config(config(adapter(timeout_seconds=45)))["adapters"]
        self.assertEqual(45, configured[0].timeout_seconds)

    def test_x_prefixed_keys_are_tolerated(self):
        document = {"normalization": {"x-note": "local", "adapters": [adapter(**{"x-owner": "team"})]}}
        self.assertEqual(1, len(CONFIG.normalization_config(document)["adapters"]))

    # -- section shape -----------------------------------------------------------

    def test_non_mapping_section_is_rejected(self):
        self.assertRejected({"normalization": ["adapters"]}, contains="must be a mapping")

    def test_unknown_section_key_is_rejected(self):
        self.assertRejected({"normalization": {"adapter": []}}, contains="unknown keys: adapter")

    def test_non_list_adapters_is_rejected(self):
        self.assertRejected({"normalization": {"adapters": {}}}, contains="must be a list")

    def test_non_mapping_adapter_is_rejected(self):
        self.assertRejected({"normalization": {"adapters": ["autoseller"]}}, contains="[0] must be a mapping")

    def test_unknown_adapter_key_is_rejected(self):
        self.assertRejected(config(adapter(timeout=30)), contains="unknown keys: timeout")

    # -- kinds -------------------------------------------------------------------

    def test_missing_or_empty_kinds_is_rejected(self):
        self.assertRejected(config(adapter(kinds=[])), contains="non-empty list")
        declaration = adapter()
        del declaration["kinds"]
        self.assertRejected(config(declaration), contains="non-empty list")

    def test_non_string_kind_is_rejected(self):
        self.assertRejected(config(adapter(kinds=[7])), contains="must be non-empty strings")

    def test_kind_with_whitespace_is_rejected(self):
        self.assertRejected(config(adapter(kinds=["structured data"])), contains="must not contain whitespace")

    def test_duplicate_kind_within_one_adapter_is_rejected(self):
        self.assertRejected(
            config(adapter(kinds=["structured_data", "structured_data"])),
            contains="more than once",
        )

    def test_one_kind_may_not_be_claimed_by_two_adapters(self):
        self.assertRejected(
            config(adapter(), adapter(name="other", kinds=["structured_data", "sensor_series"])),
            contains="One kind resolves to one adapter",
        )

    def test_namespaced_pack_kinds_are_accepted(self):
        # Membership is open by design: a domain pack may declare its own kinds, so the
        # reader validates shape rather than a closed vocabulary.
        configured = CONFIG.normalization_config(
            config(adapter(kinds=["market-data/supplier_quote", "market-data/price_history"]))
        )["adapters"]
        self.assertEqual(("market-data/supplier_quote", "market-data/price_history"), configured[0].kinds)

    def test_kinds_the_package_normalizes_itself_are_refused(self):
        for kind in sorted(CONFIG.NATIVE_SOURCE_KINDS):
            with self.subTest(kind=kind):
                self.assertRejected(config(adapter(kinds=[kind])), contains="extracts that kind")

    # -- provider ----------------------------------------------------------------

    def test_provider_is_required(self):
        declaration = adapter()
        del declaration["provider"]
        self.assertRejected(config(declaration), contains="provider is required")

    def test_unknown_provider_is_rejected(self):
        self.assertRejected(config(adapter(provider="http")), contains="rejected value 'http'")

    # -- command -----------------------------------------------------------------

    def test_command_string_is_rejected_in_favour_of_argv(self):
        # Splitting a string guesses at the operator's quoting; argv says exactly what runs.
        exception = self.assertRejected(
            config(adapter(command="autoseller-normalize --format json")),
            contains="must be a list of arguments, not a string",
        )
        self.assertIn('["tool", "--flag", "value"]', exception.message)

    def test_empty_command_is_rejected(self):
        self.assertRejected(config(adapter(command=[])), contains="non-empty list")

    def test_non_string_command_entry_is_rejected(self):
        self.assertRejected(config(adapter(command=["tool", 3])), contains="must be non-empty strings")

    def test_command_arguments_keep_their_exact_form(self):
        configured = CONFIG.normalization_config(
            config(adapter(command=["tool", "--path", " spaced value "]))
        )["adapters"]
        self.assertEqual(("tool", "--path", " spaced value "), configured[0].command)

    # -- identity ----------------------------------------------------------------

    def test_name_and_version_are_required(self):
        for field in ("name", "version"):
            with self.subTest(field=field):
                declaration = adapter()
                del declaration[field]
                self.assertRejected(config(declaration), contains=f"{field} must be a non-empty string")

    def test_integer_version_is_accepted_as_a_string(self):
        configured = CONFIG.normalization_config(config(adapter(version=2)))["adapters"]
        self.assertEqual("2", configured[0].version)

    def test_float_version_is_rejected_because_yaml_loses_its_form(self):
        exception = self.assertRejected(config(adapter(version=1.40)), contains="lose its exact form")
        self.assertIn("quote it", exception.message)

    def test_boolean_identity_is_rejected(self):
        self.assertRejected(config(adapter(name=True)), contains="rejected value True")

    # -- timeout -----------------------------------------------------------------

    def test_non_integer_timeout_is_rejected(self):
        for value in ("30", 30.5, True):
            with self.subTest(value=value):
                self.assertRejected(config(adapter(timeout_seconds=value)), contains="timeout_seconds")

    def test_out_of_range_timeout_is_rejected(self):
        self.assertRejected(config(adapter(timeout_seconds=0)), contains="must be between 1")
        self.assertRejected(
            config(adapter(timeout_seconds=CONFIG.MAX_TIMEOUT_SECONDS + 1)),
            contains="must be between 1",
        )

    def test_boundary_timeouts_are_accepted(self):
        for value in (1, CONFIG.MAX_TIMEOUT_SECONDS):
            with self.subTest(value=value):
                configured = CONFIG.normalization_config(config(adapter(timeout_seconds=value)))["adapters"]
                self.assertEqual(value, configured[0].timeout_seconds)

    # -- lookup and reporting ----------------------------------------------------

    def test_adapter_for_kind_resolves_only_declared_kinds(self):
        configured = CONFIG.normalization_config(
            config(adapter(kinds=["structured_data", "sensor_series"]))
        )["adapters"]

        self.assertEqual("autoseller-normalize", CONFIG.adapter_for_kind(configured, "structured_data").name)
        self.assertEqual("autoseller-normalize", CONFIG.adapter_for_kind(configured, "sensor_series").name)
        self.assertIsNone(CONFIG.adapter_for_kind(configured, "paper"))
        self.assertIsNone(CONFIG.adapter_for_kind(configured, None))
        self.assertIsNone(CONFIG.adapter_for_kind((), "structured_data"))

    def test_summary_describes_what_the_workspace_may_execute(self):
        configured = CONFIG.normalization_config(config(adapter()))["adapters"]
        self.assertEqual(
            {
                "kinds": ["structured_data"],
                "provider": "command",
                "command": ["autoseller-normalize", "--format", "json"],
                "name": "autoseller-normalize",
                "version": "1.4.0",
                "timeout_seconds": 120,
            },
            CONFIG.adapter_summaries(configured)[0],
        )


class DocumentedExampleTests(unittest.TestCase):
    """The examples an operator copies must validate.

    The template ships the section commented out, so nothing exercises it at runtime;
    without this, a broken example could sit there indefinitely and fail only for the
    first person who uncommented it.
    """

    TEMPLATE = REPO_ROOT / "workspace-template" / "research.yml"
    DOCS = REPO_ROOT / "workspace-template" / "docs" / "research-yml.md"

    def test_template_section_is_shipped_commented_out(self):
        document = yaml.safe_load(self.TEMPLATE.read_text(encoding="utf-8"))
        self.assertNotIn(
            "normalization",
            document,
            "the template must not configure an adapter; executing a command is opt-in",
        )

    def test_commented_template_example_validates_when_uncommented(self):
        lines: list[str] = []
        capturing = False
        for line in self.TEMPLATE.read_text(encoding="utf-8").splitlines():
            if line.startswith("# normalization:"):
                capturing = True
            if capturing:
                if not line.startswith("#"):
                    break
                lines.append(re.sub(r"^# ?", "", line))
        self.assertTrue(lines, "template lost its commented normalization example")

        configured = CONFIG.normalization_config(yaml.safe_load("\n".join(lines)))["adapters"]
        self.assertEqual(1, len(configured))
        self.assertEqual(("structured_data",), configured[0].kinds)

    def test_documented_example_validates(self):
        match = re.search(r"```yaml\n(normalization:\n.*?)```", self.DOCS.read_text(encoding="utf-8"), re.DOTALL)
        self.assertIsNotNone(match, "docs lost the normalization example")

        configured = CONFIG.normalization_config(yaml.safe_load(match.group(1)))["adapters"]
        self.assertEqual(1, len(configured))

    def test_docs_state_the_execution_stance(self):
        # Collapse wrapping: the prose is line-wrapped, and where a sentence breaks is
        # not something a test should pin.
        docs = re.sub(r"\s+", " ", self.DOCS.read_text(encoding="utf-8"))

        self.assertIn("authorizes this package to execute that command", docs)
        self.assertIn("no network, no credentials", docs)
        # The contrast that keeps the codebase-analysis boundary legible.
        self.assertIn("unlike `integrations.codebase_analysis`", docs)


class NativeKindDriftTests(unittest.TestCase):
    """`NATIVE_SOURCE_KINDS` must match what `normalize_sources.py` actually handles.

    The list is duplicated rather than imported, because `normalize_sources` imports
    this module. A stale list would either refuse a kind the package cannot normalize
    (blocking a legitimate adapter) or accept one it can (silently diverting built-in
    extraction), so the duplication is guarded here instead of by an import.
    """

    def method_for(self, workspace: Path, record: dict) -> str | None:
        return NORMALIZE.normalization_method(workspace, record)

    def records_for_native_kinds(self, workspace: Path) -> dict[str, dict]:
        (workspace / "raw" / "papers" / "bundle").mkdir(parents=True)
        (workspace / "raw" / "papers" / "bundle" / "main.tex").write_text(
            "\\documentclass{article}\\begin{document}x\\end{document}\n", encoding="utf-8"
        )
        (workspace / "raw" / "pdf").mkdir(parents=True)
        (workspace / "raw" / "pdf" / "doc.pdf").write_bytes(b"%PDF-1.4\n")
        (workspace / "raw" / "web").mkdir(parents=True)
        (workspace / "raw" / "web" / "page.html").write_text("<html></html>", encoding="utf-8")
        (workspace / "raw" / "data").mkdir(parents=True)
        (workspace / "raw" / "data" / "rows.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        return {
            "paper": {
                "id": "paper:x",
                "kind": "paper",
                "latex_root": "raw/papers/bundle",
                "entrypoint": "main.tex",
            },
            "pdf": {"id": "raw:pdf", "kind": "pdf", "raw_pdf": "raw/pdf/doc.pdf"},
            "repo_link": {"id": "link:r", "kind": "repo_link", "url": "https://github.com/o/r"},
            "web_link": {"id": "link:w", "kind": "web_link", "url": "https://example.org/a"},
            "html": {"id": "raw:h", "kind": "html", "raw_paths": ["raw/web/page.html"]},
            "table": {"id": "raw:t", "kind": "table", "raw_paths": ["raw/data/rows.csv"]},
            "codebase_architecture": {"id": "code:c", "kind": "codebase_architecture"},
        }

    def test_every_declared_native_kind_is_actually_normalized(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            records = self.records_for_native_kinds(workspace)

            self.assertEqual(
                CONFIG.NATIVE_SOURCE_KINDS,
                frozenset(records),
                "update this fixture when NATIVE_SOURCE_KINDS changes",
            )
            for kind, record in sorted(records.items()):
                with self.subTest(kind=kind):
                    self.assertIsNotNone(
                        self.method_for(workspace, record),
                        f"{kind} is declared native but normalize_sources.py does not handle it",
                    )

    def test_an_adapter_only_kind_is_not_normalized_natively(self):
        # The converse guard: the kind adapters exist for must have no built-in method,
        # or B3 would be overriding an extractor rather than filling a gap.
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            record = {"id": "raw:s", "kind": "structured_data", "raw_paths": ["raw/data/payload.json"]}
            self.assertIsNone(self.method_for(workspace, record))
            self.assertNotIn("structured_data", CONFIG.NATIVE_SOURCE_KINDS)


if __name__ == "__main__":
    unittest.main()
