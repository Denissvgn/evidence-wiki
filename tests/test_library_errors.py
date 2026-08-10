"""Prove the library exception hierarchy covers the workspace error vocabulary.

The stable ``error_code`` values live in the packaged asset
``workspace-template/scripts/_script_errors.py``. ``evidence_wiki.errors``
deliberately restates the families rather than importing that asset, so this
test is the seam that keeps the two honest: every documented code must reach a
typed exception with its code attached, and every code the orchestration
controller emits must reach the orchestration family even when it never made it
into the asset's remediation table.
"""

import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from evidence_wiki import errors
from evidence_wiki._script_host import load_packaged_script, shared_assets_root

ORCHESTRATION_CODE_LITERAL = re.compile(r"[\"'](ORCHESTRATION_[A-Z_]+)[\"']")


class ErrorRegistryCoverageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.assets_root = shared_assets_root()
        cls.script_errors = load_packaged_script(cls.assets_root, "_script_errors")

    def test_every_documented_code_maps_to_a_typed_family(self):
        documented = sorted(self.script_errors._REMEDIATIONS)
        self.assertGreater(len(documented), 100, "the asset's remediation table looks truncated")
        unmapped = [code for code in documented if errors.error_class_for(code) is errors.EvidenceWikiError]
        self.assertEqual(
            [],
            unmapped,
            "these documented error codes fall through to the base class; add them to errors.ERROR_FAMILIES",
        )

    def test_every_mapped_family_subclasses_the_base_error(self):
        for code in sorted(self.script_errors._REMEDIATIONS):
            with self.subTest(error_code=code):
                family = errors.error_class_for(code)
                self.assertTrue(issubclass(family, errors.EvidenceWikiError))
                self.assertIsNot(family, errors.EvidenceWikiError)

    def test_registry_values_are_all_error_classes(self):
        for key, family in errors.ERROR_FAMILIES.items():
            with self.subTest(key=key):
                self.assertTrue(isinstance(family, type) and issubclass(family, errors.EvidenceWikiError))

    def test_every_documented_code_round_trips_through_an_envelope(self):
        for code in sorted(self.script_errors._REMEDIATIONS):
            with self.subTest(error_code=code):
                envelope = self.script_errors.error_envelope(code, "x")
                error = errors.error_from_envelope(envelope)
                self.assertIsInstance(error, errors.EvidenceWikiError)
                self.assertEqual(code, error.error_code)
                self.assertEqual(envelope["remediation"], error.remediation)
                self.assertEqual(envelope["recoverable"], error.recoverable)
                self.assertEqual("x", str(error))

    def test_recoverability_defaults_match_the_asset(self):
        # errors.py restates the asset's default so a host can classify a code it
        # constructed itself; drift here would silently mislabel retryability.
        for code in sorted(self.script_errors._REMEDIATIONS):
            with self.subTest(error_code=code):
                self.assertEqual(
                    self.script_errors.default_recoverable(code),
                    errors.default_recoverable(code),
                )

    def test_schema_version_matches_the_asset(self):
        self.assertEqual(self.script_errors.SCHEMA_VERSION, errors.SCHEMA_VERSION)

    def test_orchestration_controller_codes_reach_the_orchestration_family(self):
        source = (self.assets_root / "workspace-template" / "scripts" / "orchestration_controller.py").read_text(
            encoding="utf-8"
        )
        codes = sorted(set(ORCHESTRATION_CODE_LITERAL.findall(source)))
        self.assertGreater(len(codes), 5, "regex found suspiciously few controller error codes")
        for code in codes:
            with self.subTest(error_code=code):
                self.assertIs(errors.OrchestrationError, errors.error_class_for(code))

    def test_exact_codes_win_over_their_prefix_family(self):
        self.assertIs(errors.ClaimError, errors.error_class_for("QUESTION_NOT_CLAIMED"))
        self.assertIs(errors.RequestError, errors.error_class_for("QUESTION_REOPEN_DELEGATED"))
        self.assertIs(errors.QuestionError, errors.error_class_for("QUESTION_UNKNOWN"))
        self.assertIs(errors.RequestError, errors.error_class_for("SOURCE_REQUEST_FULFILL_DELEGATED"))
        self.assertIs(errors.SourceError, errors.error_class_for("SOURCE_UNKNOWN"))


class ErrorEnvelopeToleranceTests(unittest.TestCase):
    def test_unknown_code_degrades_to_the_base_class_with_the_code_preserved(self):
        error = errors.error_from_envelope(
            {
                "schema_version": "1.0",
                "error_code": "FUTURE_CODE_X",
                "message": "a newer workspace said something this host has never seen",
                "recoverable": True,
                "remediation": "upgrade the host",
            }
        )
        self.assertIs(errors.EvidenceWikiError, type(error))
        self.assertEqual("FUTURE_CODE_X", error.error_code)
        self.assertEqual("a newer workspace said something this host has never seen", str(error))
        self.assertEqual("upgrade the host", error.remediation)
        self.assertTrue(error.recoverable)

    def test_malformed_input_never_raises(self):
        cases = [
            None,
            {},
            [],
            "COVERAGE_BLOCKED",
            42,
            {"error_code": "LOCK_UNAVAILABLE"},
            {"message": "no code here"},
            {"error_code": "", "message": ""},
            {"error_code": None, "message": None},
            {"error_code": "COVERAGE_BLOCKED", "message": "m", "details": "not-a-dict"},
        ]
        for case in cases:
            with self.subTest(envelope=repr(case)):
                error = errors.error_from_envelope(case)
                self.assertIsInstance(error, errors.EvidenceWikiError)
                self.assertIsInstance(error.error_code, str)
                self.assertIsInstance(error.message, str)
                self.assertIsInstance(error.remediation, str)
                self.assertIsInstance(error.recoverable, bool)
                self.assertIsInstance(error.details, dict)
                self.assertIsInstance(error.exit_code, int)
                self.assertEqual(error.message, str(error))

    def test_absent_code_reports_unknown(self):
        error = errors.error_from_envelope({"message": "no code here"})
        self.assertIs(errors.EvidenceWikiError, type(error))
        self.assertEqual(errors.UNKNOWN_ERROR_CODE, error.error_code)

    def test_non_dict_input_describes_the_malformation(self):
        error = errors.error_from_envelope([])
        self.assertIs(errors.EvidenceWikiError, type(error))
        self.assertEqual(errors.MALFORMED_ENVELOPE_ERROR_CODE, error.error_code)
        self.assertIn("list", str(error))

    def test_missing_message_stays_on_the_base_class_but_keeps_the_code(self):
        error = errors.error_from_envelope({"error_code": "LOCK_UNAVAILABLE"})
        self.assertIs(errors.EvidenceWikiError, type(error))
        self.assertEqual("LOCK_UNAVAILABLE", error.error_code)
        self.assertIn("LOCK_UNAVAILABLE", str(error))

    def test_details_and_exit_code_survive_the_envelope(self):
        error = errors.error_from_envelope(
            {
                "schema_version": "1.0",
                "error_code": "CLAIM_HELD",
                "message": "held",
                "recoverable": False,
                "remediation": "wait",
                "details": {"claimed_by": "agent-7"},
            }
        )
        self.assertIsInstance(error, errors.ClaimError)
        self.assertEqual({"claimed_by": "agent-7"}, error.details)
        self.assertFalse(error.recoverable)
        self.assertEqual(errors.EXIT_CONFLICT, error.exit_code)

    def test_constructed_error_defaults_are_usable_without_an_envelope(self):
        error = errors.CoverageError("COVERAGE_BLOCKED", "blocked")
        self.assertEqual("blocked", str(error))
        self.assertTrue(error.recoverable)
        self.assertEqual(errors.DEFAULT_REMEDIATION, error.remediation)
        self.assertEqual({}, error.details)
        self.assertEqual(errors.EXIT_INVALID, error.exit_code)


if __name__ == "__main__":
    unittest.main()


class EmittedCodeFamilyTests(unittest.TestCase):
    """Every code a workspace script can emit must land in a family, not the base class.

    ``test_every_documented_code_maps_to_a_family`` above walks
    ``_script_errors._REMEDIATIONS``, which is the *documented* registry. Scripts
    emit codes that are absent from it -- the orchestration controller alone
    raises ``RESULT_INVALID``, ``WORK_ORDER_INVALID`` and seven more with no
    ``ORCHESTRATION_`` prefix -- and those reached a host as the bare base class.
    A host writing ``except OrchestrationError`` around ``session.submit`` caught
    nothing, for the refusal that call makes most often.

    Scanning the sources rather than the remediation table is what notices the
    next one. The exclusion set below is the honest cost: a screaming-snake string
    literal is not proof of an error code, so genuine non-codes are named
    individually with a reason instead of being filtered by a pattern that would
    also hide real gaps.
    """

    #: Screaming-snake literals that are demonstrably not error codes.
    NOT_ERROR_CODES = {
        "O_BINARY": "os.open flag",
        "O_NOFOLLOW": "os.open flag",
        "EVIDENCE_WIKI_HANDOFF_SECRET": "environment variable name",
        "EVIDENCE_WIKI_SINGLE_WRITER": "environment variable name",
        "QUERY_INDEX_FALLBACK": "retrieval mode name, not a refusal",
    }

    CODE_SHAPED = re.compile(r'"([A-Z][A-Z0-9]*(?:_[A-Z0-9]+){1,})"')

    def test_every_code_a_script_can_emit_maps_to_a_family(self):
        emitted: dict[str, set[str]] = {}
        scripts = REPO_ROOT / "workspace-template" / "scripts"
        for path in sorted(scripts.glob("*.py")):
            for code in self.CODE_SHAPED.findall(path.read_text(encoding="utf-8")):
                if code not in self.NOT_ERROR_CODES:
                    emitted.setdefault(code, set()).add(path.name)

        unmapped = {
            code: sorted(files)
            for code, files in emitted.items()
            if errors.error_class_for(code) is errors.EvidenceWikiError
        }
        self.assertEqual(
            {},
            unmapped,
            "these codes reach a host as the base class, so a family-scoped `except` misses them; "
            "add each to ERROR_FAMILIES, or to NOT_ERROR_CODES with the reason it is not a code",
        )

    def test_the_exclusion_set_only_holds_things_that_are_not_codes(self):
        """An exclusion must be justified, so the set cannot become a silencer."""
        for code, reason in self.NOT_ERROR_CODES.items():
            with self.subTest(code=code):
                self.assertTrue(reason.strip(), f"{code} is excluded without a reason")


class ExitStatusReconstructionTests(unittest.TestCase):
    """Where a typed exception's ``exit_code`` comes from, and what it costs.

    ``docs/library-api.md`` documents the attribute as "the status the CLI would
    have exited with". No envelope carries it: ``_script_errors.error_envelope``
    emits ``schema_version``, ``error_code``, ``message``, ``recoverable``,
    ``remediation`` and an optional ``details``, and never an exit status. So
    ``errors.error_from_envelope`` *reconstructs* the number from
    ``errors._EXIT_CODE_OVERRIDES``, a table this package maintains by hand
    against the scripts' own ``EXIT_*`` constants -- and that is only ever as
    good as the table.

    CR-8 supplied the first miss. ``ORCHESTRATION_DRIVER_BUSY`` exits the
    controller with ``EXIT_DRIVER_BUSY`` (6), the first refusal in the workspace
    scripts to exit with anything the table had not been told about; every earlier
    one exits 2, or the 3 the two claim codes already occupy. Until the table
    learned it, the library reported 2 while both shells reported 5.

    Both halves are pinned here: the mechanism, and the agreement the table entry
    buys. The cross-check below reads the controller's own ``EXIT_DRIVER_BUSY``
    out of the packaged asset rather than trusting a literal, so the two sides
    cannot drift apart silently -- moving the constant without updating the table
    fails here, which is the failure the hand-maintained table exists to invite.
    The end-to-end demonstration lives in
    ``tests/test_library_error_reachability.py``; this is the fast structural
    statement of why it comes out that way.
    """

    @classmethod
    def setUpClass(cls):
        cls.assets_root = shared_assets_root()
        cls.script_errors = load_packaged_script(cls.assets_root, "_script_errors")

    def test_no_error_envelope_carries_an_exit_status(self):
        """The root of it: the number cannot survive the seam, because it is never sent."""
        for code in sorted(self.script_errors._REMEDIATIONS):
            with self.subTest(error_code=code):
                envelope = self.script_errors.error_envelope(code, "x", details={"holder": None})
                self.assertNotIn("exit_code", envelope)

    def test_the_driver_busy_code_is_retryable_and_lands_in_the_orchestration_family(self):
        """The two attributes a host reconstructs correctly without an envelope.

        A host that builds the error itself -- from a logged code, or from a
        refusal it forwarded -- gets the same recoverability and the same family
        the controller sends, which is what makes ``except OrchestrationError``
        plus a retry on ``recoverable`` a complete handler for contention.
        """
        self.assertIs(errors.OrchestrationError, errors.error_class_for("ORCHESTRATION_DRIVER_BUSY"))
        self.assertTrue(errors.default_recoverable("ORCHESTRATION_DRIVER_BUSY"))

    def test_the_driver_busy_exit_status_matches_the_controllers_own_constant(self):
        """The third attribute, and the hand-maintained table that has to earn it.

        Read from the packaged controller rather than asserted as a literal: the
        point is that the two sides *agree*, so changing ``EXIT_DRIVER_BUSY``
        without teaching ``_EXIT_CODE_OVERRIDES`` fails here rather than silently
        making a library caller's ``exit_code`` disagree with its own shell.
        """
        controller = (self.assets_root / "workspace-template" / "scripts" / "orchestration_controller.py").read_text(
            encoding="utf-8"
        )
        declared = re.search(r"^EXIT_DRIVER_BUSY = (\d+)$", controller, re.MULTILINE)
        self.assertIsNotNone(declared, "the controller no longer declares EXIT_DRIVER_BUSY")
        self.assertEqual(
            int(declared.group(1)),
            errors.default_exit_code("ORCHESTRATION_DRIVER_BUSY"),
            "the controller's EXIT_DRIVER_BUSY and errors._EXIT_CODE_OVERRIDES disagree; a library "
            "caller's exit_code would no longer match the status its own CLI exits with",
        )
        self.assertEqual(errors.EXIT_DRIVER_BUSY, errors.default_exit_code("ORCHESTRATION_DRIVER_BUSY"))
