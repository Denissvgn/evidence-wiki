"""The shared delivery and acquisition-attempt failure vocabularies.

Two sets live in one module and the boundary between them is load-bearing: the delivery
set is validated against provenance sidecars and marks manifest records unusable, while
the attempt set is recorded against a source request when nothing was delivered at all.
Collapsing them would make `no_result` a valid claim about a file that exists.
"""

import importlib.util
import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
SOURCE_DELIVERY_DOC = REPO_ROOT / "workspace-template" / "docs" / "source-delivery.md"


def load_script_module(name: str, filename: str):
    path = SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load script from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


TAXONOMY = load_script_module("attempt_failure_taxonomy", "source_failure_taxonomy.py")

ATTEMPT_ONLY = ("provider_throttled", "not_authorized", "no_result")


class AttemptVocabularyTests(unittest.TestCase):
    def test_attempt_codes_are_the_delivery_codes_plus_connector_outcomes(self):
        self.assertEqual(
            tuple(TAXONOMY.DELIVERY_FAILURE_CODES) + ATTEMPT_ONLY,
            TAXONOMY.ATTEMPT_FAILURE_CODES,
        )
        self.assertEqual(ATTEMPT_ONLY, TAXONOMY.ATTEMPT_ONLY_FAILURE_CODES)

    def test_every_delivery_code_is_also_a_valid_attempt_code(self):
        # An attempt that failed with a plain HTTP 500 says http_error rather than
        # inventing a second word for the same thing.
        for code in TAXONOMY.DELIVERY_FAILURE_CODES:
            with self.subTest(code=code):
                self.assertTrue(TAXONOMY.is_attempt_failure_code(code))

    def test_attempt_only_codes_are_not_valid_sidecar_codes(self):
        # The load-bearing half of the split: these mean no artifact exists, so they
        # cannot describe one. Inventory validates sidecars against the delivery set.
        for code in ATTEMPT_ONLY:
            with self.subTest(code=code):
                self.assertTrue(TAXONOMY.is_attempt_failure_code(code))
                self.assertFalse(TAXONOMY.is_delivery_failure_code(code))
                self.assertNotIn(code, TAXONOMY.DELIVERY_FAILURE_CODES)
                self.assertNotIn(code, TAXONOMY.DELIVERY_FAILURE_REMEDIATIONS)

    def test_every_attempt_code_has_a_remediation(self):
        self.assertEqual(set(TAXONOMY.ATTEMPT_FAILURE_CODES), set(TAXONOMY.ATTEMPT_FAILURE_REMEDIATIONS))
        for code in TAXONOMY.ATTEMPT_FAILURE_CODES:
            with self.subTest(code=code):
                remediation = TAXONOMY.attempt_failure_remediation(code)
                self.assertTrue(remediation and remediation.strip())

    def test_delivery_remediations_are_reused_verbatim(self):
        # One vocabulary, one wording: a code must not mean different things depending on
        # which store recorded it.
        for code, remediation in TAXONOMY.DELIVERY_FAILURE_REMEDIATIONS.items():
            with self.subTest(code=code):
                self.assertEqual(remediation, TAXONOMY.attempt_failure_remediation(code))

    def test_unknown_and_non_string_values_are_refused(self):
        for value in ("", "legal_only_failure", "HTTP_ERROR", None, 7, True, ["http_error"]):
            with self.subTest(value=repr(value)):
                self.assertFalse(TAXONOMY.is_attempt_failure_code(value))
                self.assertFalse(TAXONOMY.is_retryable_attempt_failure_code(value))
        self.assertIsNone(TAXONOMY.attempt_failure_remediation("legal_only_failure"))

    def test_codes_stay_domain_neutral(self):
        for code in TAXONOMY.ATTEMPT_FAILURE_CODES:
            with self.subTest(code=code):
                self.assertRegex(code, r"^[a-z][a-z0-9_]*$")
                self.assertNotIn("legal", code)


class RetryabilityTests(unittest.TestCase):
    def test_every_attempt_code_is_classified_exactly_once(self):
        retryable = set(TAXONOMY.RETRYABLE_ATTEMPT_FAILURE_CODES)
        non_retryable = set(TAXONOMY.NON_RETRYABLE_ATTEMPT_FAILURE_CODES)
        self.assertEqual(set(TAXONOMY.ATTEMPT_FAILURE_CODES), retryable | non_retryable)
        self.assertEqual(set(), retryable & non_retryable)

    def test_standing_decisions_are_not_retryable(self):
        for code in ("not_authorized", "robots_or_terms_blocked", "license_or_terms_unknown", "manual_review_required"):
            with self.subTest(code=code):
                self.assertFalse(TAXONOMY.is_retryable_attempt_failure_code(code))

    def test_transient_conditions_are_retryable(self):
        for code in ("provider_throttled", "no_result", "http_error", "tls_verification_failed"):
            with self.subTest(code=code):
                self.assertTrue(TAXONOMY.is_retryable_attempt_failure_code(code))

    def test_retryability_is_the_complement_of_the_explicit_non_retryable_set(self):
        # The direction that matters: non-retryable is the enumerated exception, so a code
        # added to the vocabulary and not listed there is retryable. Retries are bounded by
        # the per-request attempt budget, which makes that the cheaper mistake — the other
        # default would silently retire requests on a code nobody had classified yet.
        # Adding a code and classifying it nowhere is caught by the completeness test above.
        for code in TAXONOMY.ATTEMPT_FAILURE_CODES:
            with self.subTest(code=code):
                self.assertEqual(
                    code not in TAXONOMY.NON_RETRYABLE_ATTEMPT_FAILURE_CODES,
                    TAXONOMY.is_retryable_attempt_failure_code(code),
                )
        self.assertLess(
            len(TAXONOMY.NON_RETRYABLE_ATTEMPT_FAILURE_CODES),
            len(TAXONOMY.RETRYABLE_ATTEMPT_FAILURE_CODES),
            "non-retryable is meant to be the small enumerated exception",
        )

    def test_a_code_outside_the_vocabulary_is_never_retryable(self):
        # Distinct from the default above: unknown to the taxonomy is not the same as
        # known-but-unclassified. A router asking about a code this package does not
        # recognize has no evidence that retrying would help.
        self.assertFalse(TAXONOMY.is_retryable_attempt_failure_code("some_future_code"))

    def test_non_retryable_codes_are_real_codes(self):
        for code in TAXONOMY.NON_RETRYABLE_ATTEMPT_FAILURE_CODES:
            with self.subTest(code=code):
                self.assertTrue(TAXONOMY.is_attempt_failure_code(code))


class DocumentationTests(unittest.TestCase):
    def test_attempt_only_codes_are_documented_with_remediations(self):
        text = SOURCE_DELIVERY_DOC.read_text(encoding="utf-8")
        self.assertIn("### Acquisition-attempt failures", text)
        for code in ATTEMPT_ONLY:
            with self.subTest(code=code):
                self.assertIn(f"`{code}`", text)

    def test_docs_state_the_sidecar_boundary_and_retryability(self):
        # Collapse wrapping: where a sentence breaks is not something a test should pin.
        collapsed = re.sub(r"\s+", " ", SOURCE_DELIVERY_DOC.read_text(encoding="utf-8"))
        self.assertIn("not** valid `delivery_failure_code` values", collapsed)
        self.assertIn("not retryable", collapsed)
        for code in TAXONOMY.NON_RETRYABLE_ATTEMPT_FAILURE_CODES:
            with self.subTest(code=code):
                self.assertIn(f"`{code}`", collapsed)


class ExistingConsumersUnaffectedTests(unittest.TestCase):
    """The delivery vocabulary is a published contract; B1 must not widen it."""

    def test_delivery_codes_are_unchanged(self):
        self.assertEqual(
            (
                "tls_verification_failed",
                "http_error",
                "javascript_required",
                "official_error_page",
                "not_found",
                "content_too_sparse",
                "license_or_terms_unknown",
                "robots_or_terms_blocked",
                "manual_review_required",
            ),
            TAXONOMY.DELIVERY_FAILURE_CODES,
        )

    def test_attempt_only_codes_do_not_make_evidence_unusable(self):
        # unusable_evidence_reasons keys on the delivery set. If the sets were merged, a
        # sidecar carrying an attempt-only code would silently mark a record unusable.
        for code in ATTEMPT_ONLY:
            with self.subTest(code=code):
                self.assertEqual([], TAXONOMY.unusable_evidence_reasons({"delivery_failure_code": code}))
                self.assertTrue(TAXONOMY.evidence_is_usable({"delivery_failure_code": code}))

    def test_delivery_codes_still_make_evidence_unusable(self):
        self.assertEqual(
            ["delivery_failure_code:http_error"],
            TAXONOMY.unusable_evidence_reasons({"delivery_failure_code": "http_error"}),
        )


if __name__ == "__main__":
    unittest.main()
