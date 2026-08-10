"""Cover the extended library surface: export, normalize verify, doctor, fleet status.

The claim each of these methods makes is that an in-process caller gets *the same
document* a subprocess caller would have parsed off stdout, so nearly every case
here drives the CLI's own ``main`` beside the API call and compares the parsed
documents (never bytes -- rendering is the CLI's job, not the API's). Wall-clock
fields are masked, because two calls a millisecond apart legitimately disagree
about ``generated_at`` and nothing else.

The other half of the file is about failure. Refusals must arrive as typed
exceptions carrying the script's own ``error_code``; ``fleet_status`` must
*degrade* instead, because one bad path among ten must not cost the caller the
other nine answers; and no ``SystemExit`` may ever reach a host, since a library
that lets one through kills its embedding process.
"""

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import evidence_wiki
from evidence_wiki import cli, errors
from evidence_wiki._contract import LIBRARY_API_SURFACE
from evidence_wiki._facades import _base
from evidence_wiki._facades.diagnostics import fleet_status
from evidence_wiki.workspace import Workspace

SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
INIT_SCRIPT_PATH = SCRIPTS / "init_research_workspace.py"

#: Fields that carry the moment of the call rather than a finding.
WALL_CLOCK_KEYS = frozenset({"generated_at"})

QUESTION_BATCH = """\
schema_version: "1.0"
questions:
  - question: What benchmarks matter?
    id: benchmarks
    priority: high
"""


def load_script_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - fixture guard
        raise RuntimeError(f"Cannot load script from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def mask(value):
    """Blank wall-clock fields anywhere in a document, at any depth."""
    if isinstance(value, dict):
        return {key: "<masked>" if key in WALL_CLOCK_KEYS else mask(item) for key, item in value.items()}
    if isinstance(value, list):
        return [mask(item) for item in value]
    return value


def cli_document(argv: list[str]) -> dict:
    """Return the JSON document ``evidence-wiki <argv>`` prints on stdout."""
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        cli.main(argv)
    return json.loads(stdout.getvalue())


class WorkspaceFixture(unittest.TestCase):
    """One initialized workspace per class: ``init`` is far too slow per test."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.root = cls.tmp / "ws"
        initializer = load_script_module(f"extended_surface_init_{cls.__name__}", INIT_SCRIPT_PATH)
        with contextlib.redirect_stdout(io.StringIO()):
            code = initializer.main(
                [
                    "--target",
                    str(cls.root),
                    "--project-name",
                    "ew-extended-surface",
                    "--project-description",
                    "Extended library surface fixture.",
                ]
            )
        assert int(code or 0) == 0, "fixture workspace failed to initialize"
        batch = cls.tmp / "batch.yaml"
        batch.write_text(QUESTION_BATCH, encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            cli.main(["questions", "add", "--target", str(cls.root), "--from-file", str(batch)])

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def open_workspace(self, root: Path | None = None) -> Workspace:
        handle = Workspace.open(self.root if root is None else root)
        self.addCleanup(handle.close)
        return handle


class ExportAnswersTests(WorkspaceFixture):
    def test_returns_the_document_the_cli_prints(self):
        api = self.open_workspace().export_answers()
        expected = cli_document(["export", "--target", str(self.root), "--format", "json"])
        self.assertEqual(mask(expected), mask(api))

    def test_status_filter_mirrors_the_repeatable_cli_option(self):
        api = self.open_workspace().export_answers(status=["answered"])
        expected = cli_document(
            ["export", "--target", str(self.root), "--format", "json", "--status", "answered"]
        )
        self.assertEqual(mask(expected), mask(api))
        # The filter has to actually filter, or the comparison above would pass
        # for the trivial reason that both sides ignored it.
        unfiltered = self.open_workspace().export_answers()
        self.assertNotEqual(len(unfiltered["questions"]), len(api["questions"]))

    def test_lives_on_the_handle_under_its_declared_contract_name(self):
        self.assertIn("workspace.export_answers", LIBRARY_API_SURFACE)
        self.assertTrue(callable(Workspace.export_answers))

    def test_a_malformed_research_yml_is_a_typed_config_refusal(self):
        broken = self.tmp / "broken"
        broken.mkdir(exist_ok=True)
        (broken / "research.yml").write_text("project: [unclosed\n", encoding="utf-8")
        with self.assertRaises(errors.ConfigError) as caught:
            self.open_workspace(broken).export_answers()
        self.assertEqual("CONFIG_INVALID", caught.exception.error_code)

    def test_the_real_dual_inherited_export_refusal_arrives_typed(self):
        # ``ExportRefusal(ScriptRefusal, SystemExit)`` is the one refusal in the
        # package that is *also* a ``SystemExit``, so it is the one most likely to
        # be mistaken for process control and either killed the host's process or
        # had its code reconstructed from its message. Driven here through the
        # real class rather than a stand-in: an unsigned handoff with a configured
        # secret is the cheapest way to provoke one.
        target = self.tmp / "unsigned-handoff"
        target.mkdir(exist_ok=True)
        (target / "research.yml").write_text(
            "project:\n"
            "  name: Handoff Fixture\n"
            "  handoff:\n"
            "    task_id: t-1\n"
            "    requested_by: operator\n"
            "    chain_run_id: chain-1\n",
            encoding="utf-8",
        )
        (target / ".research-handoff-secret").write_text("s3cret\n", encoding="utf-8")

        try:
            self.open_workspace(target).export_answers()
        except SystemExit as exc:  # pragma: no cover - the failure this guards
            self.fail(f"a real ExportRefusal reached the caller as SystemExit({exc.code!r})")
        except errors.IntakeError as caught:
            self.assertEqual("HANDOFF_SIGNATURE_INVALID", caught.error_code)
            self.assertEqual("unsigned", caught.details.get("handoff_signature_status"))
        else:  # pragma: no cover - the fixture stopped provoking a refusal
            self.fail("expected the unsigned handoff to refuse")

    def test_a_closed_handle_refuses(self):
        handle = Workspace.open(self.root)
        handle.close()
        with self.assertRaises(errors.ConfigError) as caught:
            handle.export_answers()
        self.assertEqual("WORKSPACE_UNREADABLE", caught.exception.error_code)


class NormalizeVerifyTests(WorkspaceFixture):
    def test_returns_the_document_the_cli_prints(self):
        api = self.open_workspace().normalize.verify()
        expected = cli_document(["normalize", "verify", "--target", str(self.root), "--format", "json"])
        self.assertEqual(mask(expected), mask(api))

    def test_an_unknown_source_id_is_a_typed_refusal_with_the_scripts_own_code(self):
        with self.assertRaises(errors.SourceError) as caught:
            self.open_workspace().normalize.verify(["no-such-source"])
        self.assertEqual("SOURCE_UNKNOWN", caught.exception.error_code)
        self.assertIn("no-such-source", str(caught.exception))

    def test_a_failed_verification_is_a_return_value_not_an_exception(self):
        # The per-record violation list is the entire point of asking. Raising
        # here would throw it away and leave the caller with only "it failed".
        target = self.tmp / "breached"
        (target / "sources" / "normalized").mkdir(parents=True, exist_ok=True)
        (target / "research.yml").write_text(
            "project:\n  name: Breach Fixture\n"
            "sources:\n  manifest_path: sources/manifest.jsonl\n  normalized_dir: sources/normalized\n",
            encoding="utf-8",
        )
        (target / "sources" / "manifest.jsonl").write_text("", encoding="utf-8")
        (target / "sources" / "normalized" / "orphan.md").write_text("junk\n", encoding="utf-8")

        report = self.open_workspace(target).normalize.verify()
        self.assertEqual("not_verified", report["overall_result"])
        self.assertEqual(
            mask(cli_document(["normalize", "verify", "--target", str(target), "--format", "json"])),
            mask(report),
        )

    def test_a_closed_handle_refuses(self):
        handle = Workspace.open(self.root)
        handle.close()
        with self.assertRaises(errors.ConfigError) as caught:
            handle.normalize.verify()
        self.assertEqual("WORKSPACE_UNREADABLE", caught.exception.error_code)


class DoctorTests(WorkspaceFixture):
    def test_returns_the_document_the_cli_prints(self):
        api = self.open_workspace().doctor()
        expected = cli_document(["doctor", "--target", str(self.root), "--format", "json"])
        self.assertEqual(mask(expected), mask(api))

    def test_the_handle_method_and_the_namespace_agree(self):
        # ``workspace.doctor`` is the declared contract name; the namespace is an
        # implementation detail it delegates to. They must not be able to drift.
        handle = self.open_workspace()
        self.assertEqual(mask(handle.doctor()), mask(handle.diagnostics.doctor()))
        self.assertIn("workspace.doctor", LIBRARY_API_SURFACE)
        self.assertTrue(callable(Workspace.doctor))

    def test_a_workspace_it_cannot_read_is_diagnosed_not_refused(self):
        # Diagnosing a broken workspace is the reason to call this, so a broken
        # workspace must not be a reason to withhold the diagnosis.
        bare = self.tmp / "bare"
        bare.mkdir(exist_ok=True)
        (bare / "research.yml").write_text("project: [unclosed\n", encoding="utf-8")
        report = self.open_workspace(bare).doctor()
        self.assertIn("verdict", report)
        self.assertIn("checks", report)

    def test_the_public_signature_takes_no_environment_injection(self):
        # ``DoctorEnvironment`` is defined inside a packaged script asset, so a
        # host has no supported way to name the type it would have to construct.
        # Publishing the parameter would publish one no caller could legally use.
        import inspect

        self.assertEqual(["self"], list(inspect.signature(Workspace.doctor).parameters))

    def test_a_closed_handle_refuses(self):
        handle = Workspace.open(self.root)
        handle.close()
        for label, call in (("ws.doctor", handle.doctor), ("ws.diagnostics.doctor", handle.diagnostics.doctor)):
            with self.subTest(label=label), self.assertRaises(errors.ConfigError) as caught:
                call()
            self.assertEqual("WORKSPACE_UNREADABLE", caught.exception.error_code)


class FleetStatusTests(WorkspaceFixture):
    def test_returns_the_document_the_cli_prints(self):
        api = fleet_status([str(self.root)])
        expected = cli_document(["fleet-status", "--target", str(self.root), "--format", "json"])
        self.assertEqual(mask(expected), mask(api))

    def test_no_cache_mirrors_the_cli_option(self):
        api = fleet_status([str(self.root)], no_cache=True)
        expected = cli_document(
            ["fleet-status", "--target", str(self.root), "--format", "json", "--no-cache"]
        )
        self.assertEqual(mask(expected), mask(api))

    def test_an_unreadable_target_is_reported_rather_than_raised(self):
        # The degradation guarantee: one bad path among many must not cost the
        # caller every other answer. This is the whole reason the seam declares
        # no refusal, and the property most likely to be lost to a stray wrapper.
        missing = self.tmp / "definitely" / "not" / "here"
        report = fleet_status([str(self.root), str(missing)])

        self.assertEqual(2, report["counts"]["targets"])
        self.assertEqual(1, report["counts"]["ok"])
        self.assertEqual(1, report["counts"]["errors"])
        # The report keys each entry by the *resolved* target path.
        by_path = {entry["path"]: entry for entry in report["targets"]}
        good = str(self.root.resolve())
        bad = str(missing.expanduser().resolve())
        self.assertTrue(by_path[good]["ok"])
        self.assertFalse(by_path[bad]["ok"])
        self.assertIsInstance(by_path[bad]["error_code"], str)

    def test_every_target_unreadable_still_returns_a_report(self):
        report = fleet_status([str(self.tmp / "nope-one"), str(self.tmp / "nope-two")])
        self.assertEqual(2, report["counts"]["errors"])
        self.assertEqual([False, False], [entry["ok"] for entry in report["targets"]])

    def test_no_targets_is_an_empty_report_rather_than_a_refusal(self):
        report = fleet_status([])
        self.assertEqual(0, report["counts"]["targets"])
        self.assertEqual([], report["targets"])

    def test_it_is_module_level_and_not_a_handle_method(self):
        # It aggregates across many workspaces, so there is no single handle it
        # could hang off; the contract declares it unqualified for that reason.
        self.assertIn("fleet_status", LIBRARY_API_SURFACE)
        self.assertIs(fleet_status, evidence_wiki.fleet_status)
        self.assertFalse(hasattr(Workspace, "fleet_status"))

    def test_it_is_re_exported_lazily_and_all_stays_accurate(self):
        self.assertIn("fleet_status", evidence_wiki.__all__)
        self.assertIn("fleet_status", dir(evidence_wiki))
        for name in evidence_wiki.__all__:
            with self.subTest(name=name):
                self.assertTrue(hasattr(evidence_wiki, name))


class ForeignRefusal(Exception):
    """A refusal from a class object this package never imported.

    Deliberately *not* a subclass of any ``ScriptRefusal``: the module loader
    isolates ``_script_errors`` per script, so this is exactly the situation
    package code faces at runtime, and the only thing that makes it recognizable
    is its shape.
    """

    def __init__(self, error_code="COVERAGE_REQUIRED", message="fixture refusal", exit_code=2):
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.exit_code = exit_code

    def to_envelope(self):
        return {
            "schema_version": "1.0",
            "error_code": self.error_code,
            "message": self.message,
            "recoverable": True,
            "remediation": "fixture remediation",
            "details": {"where": "fixture"},
        }


class ForeignExportRefusal(ForeignRefusal, SystemExit):
    """The dual-inherited shape ``ExportRefusal(ScriptRefusal, SystemExit)`` has."""


class RefusalTranslationTests(unittest.TestCase):
    """``translated_refusals`` is the one place refusals and ``SystemExit`` are judged."""

    def test_a_refusal_is_recognized_by_shape_not_by_class_identity(self):
        # The bug this exists to prevent: ``except ScriptRefusal`` in package code
        # compiles, reads as though it handled the case, and catches nothing --
        # because each loaded script owns a different ``ScriptRefusal`` object.
        with self.assertRaises(errors.CoverageError) as caught:
            with _base.translated_refusals():
                raise ForeignRefusal()
        self.assertEqual("COVERAGE_REQUIRED", caught.exception.error_code)
        self.assertEqual("fixture refusal", str(caught.exception))
        self.assertEqual("fixture remediation", caught.exception.remediation)
        self.assertEqual({"where": "fixture"}, caught.exception.details)
        self.assertEqual(2, caught.exception.exit_code)

    def test_two_scripts_really_do_get_different_refusal_classes(self):
        # The premise of the test above, asserted rather than assumed.
        with Workspace.open(SCRIPTS.parent) as ws:
            first = ws._script("coverage_manifest")
            second = ws._script("export_answers")
        self.assertIsNot(first._script_errors.ScriptRefusal, second.ScriptRefusal)

    def test_a_dual_inherited_export_refusal_keeps_its_own_code(self):
        # ``ExportRefusal`` is a ``SystemExit`` too, so it reaches the SystemExit
        # arm first. Reconstructing it from its message there would silently
        # downgrade a precise code to whatever the message classifier guessed.
        with self.assertRaises(errors.GroundingError) as caught:
            with _base.translated_refusals():
                raise ForeignExportRefusal(error_code="GROUNDING_REQUIRED", message="ungrounded claim")
        self.assertEqual("GROUNDING_REQUIRED", caught.exception.error_code)

    def test_a_message_carrying_system_exit_becomes_a_typed_error(self):
        # A library that lets SystemExit through terminates its host's process.
        for message, expected_code, expected_class in (
            ("Missing config: research.yml", "CONFIG_MISSING", errors.ConfigError),
            ("Invalid research.yml: bad", "CONFIG_INVALID", errors.ConfigError),
            ("Missing packaged script: /nope.py", "TOOLING_MISSING", errors.ConfigError),
            ("Unknown source id: x", "SOURCE_UNKNOWN", errors.SourceError),
        ):
            with self.subTest(message=message):
                with self.assertRaises(expected_class) as caught:
                    with _base.translated_refusals():
                        raise SystemExit(message)
                self.assertEqual(expected_code, caught.exception.error_code)
                self.assertEqual(message, str(caught.exception))

    def test_a_system_exit_that_is_process_control_is_re_raised_untouched(self):
        # A non-string code is ``sys.exit(2)``, not a refusal. Converting it
        # would swallow a deliberate process-level decision.
        for code in (0, 2, None, 137):
            with self.subTest(code=code):
                with self.assertRaises(SystemExit) as caught:
                    with _base.translated_refusals():
                        raise SystemExit(code)
                self.assertEqual(code, caught.exception.code)

    def test_an_ordinary_bug_propagates_unchanged(self):
        # A KeyError from a script is a defect. Dressing it up as a workspace
        # error would hide it behind a plausible-looking diagnosis.
        for exception in (KeyError("boom"), ValueError("boom"), RuntimeError("boom")):
            with self.subTest(exception=type(exception).__name__):
                with self.assertRaises(type(exception)):
                    with _base.translated_refusals():
                        raise exception

    def test_a_refusal_whose_envelope_cannot_render_still_arrives_typed(self):
        class Unrenderable(ForeignRefusal):
            def to_envelope(self):
                raise RuntimeError("envelope build failed")

        with self.assertRaises(errors.CoverageError) as caught:
            with _base.translated_refusals():
                raise Unrenderable()
        self.assertEqual("COVERAGE_REQUIRED", caught.exception.error_code)

    def test_a_clean_block_returns_without_interference(self):
        with _base.translated_refusals():
            observed = {"ok": True}
        self.assertEqual({"ok": True}, observed)


class NoSystemExitEscapesTests(WorkspaceFixture):
    """No public method on the extended surface may let ``SystemExit`` reach a host."""

    def broken_workspace(self, name: str) -> Path:
        target = self.tmp / name
        target.mkdir(exist_ok=True)
        (target / "research.yml").write_text("project: [unclosed\n", encoding="utf-8")
        return target

    def test_no_call_against_a_broken_workspace_lets_system_exit_escape(self):
        target = self.broken_workspace("systemexit-probe")
        handle = self.open_workspace(target)
        for label, call in (
            ("export_answers", handle.export_answers),
            ("normalize.verify", handle.normalize.verify),
            ("doctor", handle.doctor),
        ):
            # Refusing is fine, returning is fine; only the manner of failure is
            # under test, so catch whatever escapes and then judge it.
            with self.subTest(label=label):
                raised: BaseException | None = None
                try:
                    call()
                except BaseException as exc:  # noqa: BLE001 - inspecting what escaped is the test
                    raised = exc
                self.assertNotIsInstance(
                    raised, SystemExit, f"{label} let SystemExit({getattr(raised, 'code', None)!r}) escape"
                )

    def test_a_workspace_level_refusal_arrives_typed(self):
        handle = self.open_workspace(self.broken_workspace("typed-refusal-probe"))
        with self.assertRaises(errors.ConfigError) as caught:
            handle.export_answers()
        self.assertEqual("CONFIG_INVALID", caught.exception.error_code)
        # The doctor is the exception to the rule: it diagnoses this workspace
        # rather than refusing it, which is the reason to run it at all.
        self.assertIn("verdict", handle.doctor())

    def test_normalize_verify_propagates_the_scripts_yaml_error_exactly_as_the_cli_does(self):
        # Known upstream gap, pinned rather than papered over: unlike its
        # siblings, ``normalize_verify.load_config`` calls ``yaml.safe_load``
        # outside the ``SystemExit``/``NormalizeVerifyError`` funnel, so a
        # malformed ``research.yml`` escapes as a raw ``YAMLError``. The CLI
        # tracebacks on the same input, so the API is *faithful* here; the fix
        # belongs in the script, which this unit does not own. If a later unit
        # funnels it, this test flips to a typed ConfigError and should be
        # updated -- that is the point of pinning it.
        import yaml

        target = self.broken_workspace("yaml-parity-probe")
        with self.assertRaises(yaml.YAMLError):
            self.open_workspace(target).normalize.verify()
        # ... and the CLI does the same thing, which is what makes this parity
        # rather than a regression introduced by the facade.
        with self.assertRaises(yaml.YAMLError):
            cli.main(["normalize", "verify", "--target", str(target), "--format", "json"])

    def test_a_missing_packaged_script_is_a_config_error_not_a_system_exit(self):
        # ``_script_host`` reports an unloadable script by raising SystemExit(str),
        # so the script *load* has to sit inside the translation, not beside it.
        handle = self.open_workspace()
        original = handle._script

        def missing(stem: str):
            original(stem)  # keep the closed-handle check on the real path
            raise SystemExit(f"Missing packaged script: /nowhere/{stem}.py")

        with unittest.mock.patch.object(Workspace, "_script", staticmethod(missing)):
            with self.assertRaises(errors.ConfigError) as caught:
                handle.export_answers()
        self.assertEqual("TOOLING_MISSING", caught.exception.error_code)


class DeclaredSurfaceTests(unittest.TestCase):
    """Every name this unit owns in ``library_api.surface`` must be a real one."""

    OWNED = {
        "workspace.export_answers": lambda: Workspace.export_answers,
        "workspace.doctor": lambda: Workspace.doctor,
        "normalize.verify": lambda: evidence_wiki._facades.normalize.NormalizeNamespace.verify,
        "fleet_status": lambda: evidence_wiki.fleet_status,
    }

    def test_each_owned_declaration_resolves_to_a_callable(self):
        # The contract is published output a host negotiates against, so a
        # declared name that resolves to nothing is a broken promise, not a TODO.
        import evidence_wiki._facades.normalize  # noqa: F401 - bound for the lambdas above

        for name, resolve in self.OWNED.items():
            with self.subTest(name=name):
                self.assertIn(name, LIBRARY_API_SURFACE)
                self.assertTrue(callable(resolve()))

    def test_the_surface_list_has_no_duplicates(self):
        self.assertEqual(len(LIBRARY_API_SURFACE), len(set(LIBRARY_API_SURFACE)))


if __name__ == "__main__":
    unittest.main()
