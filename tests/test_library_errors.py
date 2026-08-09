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
