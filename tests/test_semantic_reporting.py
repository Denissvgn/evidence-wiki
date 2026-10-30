"""Frozen semantic inventories remain visible through incomplete reporting."""

import copy
import hashlib
import unittest

from evidence_wiki import semantic_reporting as reporting
from evidence_wiki.agent_execution import ExecutionError
from evidence_wiki.semantic_contracts import DIMENSIONS


def _hash(value):
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


_METRICS = {
    "pass": {"expected": 1, "applicable": 1, "passed": 1, "failed": 0, "unknown": 0,
        "not_applicable": 0, "numerator": 1, "denominator": 1, "score": 1.0,
        "uncertainty_bounds": {"lower": 1.0, "upper": 1.0}},
    "fail": {"expected": 1, "applicable": 1, "passed": 0, "failed": 1, "unknown": 0,
        "not_applicable": 0, "numerator": 0, "denominator": 1, "score": 0.0,
        "uncertainty_bounds": {"lower": 0.0, "upper": 0.0}},
    "unknown": {"expected": 1, "applicable": 1, "passed": 0, "failed": 0, "unknown": 1,
        "not_applicable": 0, "numerator": 0, "denominator": 1, "score": None,
        "uncertainty_bounds": {"lower": 0.0, "upper": 1.0}},
    "not_applicable": {"expected": 1, "applicable": 0, "passed": 0, "failed": 0, "unknown": 0,
        "not_applicable": 1, "numerator": 0, "denominator": 0, "score": None,
        "uncertainty_bounds": {"lower": None, "upper": None}},
    "empty": {"expected": 0, "applicable": 0, "passed": 0, "failed": 0, "unknown": 0,
        "not_applicable": 0, "numerator": 0, "denominator": 0, "score": None,
        "uncertainty_bounds": {"lower": None, "upper": None}},
}


def _original(identifier="original-a", *, claim=None, inapplicable=()):
    claim = claim or "claim:" + identifier
    identifiers = {"support": claim, "scoped_correctness": claim,
        "missing_material_evidence": "question:" + identifier,
        "appropriate_abstention": identifier, "unnecessary_refusal": identifier}
    return {"original_id": identifier, "original_text_sha256": _hash("original-text:" + identifier),
        "question_ids": ["question:" + identifier], "outcome": "candidate_observed",
        "units": {dimension: [{"unit_id": identifiers[dimension], "applicable": dimension not in inapplicable}]
            for dimension in DIMENSIONS}}


def _qualification(state="qualified"):
    return {"state": state, "reason": "Current owner qualification." if state == "qualified" else "Qualification expired.",
        "profiles": [{"profile_ref": "profile:" + role, "role": native_role,
            "controller_ref": "controller:" + role, "model_ref": "model:" + role, "prompt_version": "rubric/v1",
            "qualification_ref": "qualification:" + role, "state": state, "reason": "Finite reference scope.",
            "reference_cases": 48, "families": 12, "critical_false_acceptances": 0,
            "positive_acceptance": "1.0", "meaning_preserving_flip": "0.0",
            "calibration_scope": "finite-structured-observation"}
            for role, native_role in (("evidence", "evaluator"), ("scope_units", "evaluator"),
                ("counterevidence", "critic"), ("adjudicator", "adjudicator"))]}


def _assessment(original, *, name="assessment-a", claim=None, current=True, roles=reporting.ROLES,
        adjudication=True, verdicts=None, tier="unknown"):
    claim = claim or original["units"]["support"][0]["unit_id"]
    verdicts = verdicts or {}
    checks, metrics = {}, {}
    for dimension in DIMENSIONS:
        unit = original["units"][dimension][0]
        unit_id = claim if dimension in ("support", "scoped_correctness") else unit["unit_id"]
        verdict = verdicts.get(dimension, "pass" if unit["applicable"] else "not_applicable")
        checks[dimension] = [{"unit_id": unit_id, "verdict": verdict, "reason": "Anchored observation.",
            "severity": "major" if verdict == "fail" else "none", "uncertainty": "medium",
            "anchors": [{"pointer": "/sections/0/body", "start": 0, "end": 11, "quote": "Known fact."}]}]
        metrics[dimension] = copy.deepcopy(_METRICS[verdict])

    def observation(role):
        value = {"profile_ref": "profile:" + role, "controller_ref": "controller:" + role,
            "model_ref": "model:" + role, "prompt_version": "rubric/v1",
            "qualification_ref": "qualification:" + role, "input_ref": _hash(name + ":input:" + role),
            "output_ref": _hash(name + ":output:" + role), "observation_ref": _hash(name + ":receipt:" + role),
            "checks": copy.deepcopy(checks), "metrics": copy.deepcopy(metrics)}
        if role != "adjudicator":
            value["role"] = role
        return value

    return {"assessment_id": _hash(name), "original_id": original["original_id"], "claim_id": claim,
        "basis_id": _hash(name + ":basis"), "current": current,
        "first_passes": [observation(role) for role in roles],
        "adjudication": observation("adjudicator") if adjudication else None,
        "reference": {"tier": tier, "label_ref": None if tier == "unknown" else _hash(name + ":label"),
            "label": None if tier == "unknown" else "accept", "scope": "Named owner reference only."},
        "unknown_reasons": ["reference_unavailable"] if tier == "unknown" else []}


def _report(originals, assessments, **options):
    return reporting.report(run_id="run-semantic", binding_id=_hash("binding"), policy_id=_hash("policy"),
        profile_sha256=hashlib.sha256(b"profile").hexdigest(), originals=originals, assessments=assessments,
        qualification=options.pop("qualification", _qualification()), **options)


class SemanticReportingTests(unittest.TestCase):
    def test_missing_original_keeps_all_expected_denominators(self):
        answered, missing = _original(), _original("original-b")
        result = _report([answered, missing], [_assessment(answered)])
        self.assertEqual({"expected": 2, "started": 1, "observed": 1, "completely_adjudicated": 1,
            "missing": 1, "unobserved": 1, "incomplete": 1}, result["counts"])
        for dimension in DIMENSIONS:
            score = result["dimensions"][dimension]
            self.assertEqual((2, 2, 1, 1), (score["expected"], score["denominator"], score["passed"], score["unknown"]))
            self.assertIsNone(score["score"])
            unit = result["originals"][1]["units"][dimension][0]
            self.assertEqual("unknown", unit["verdict"])
            self.assertFalse(unit["observed"])
        self.assertFalse(result["originals"][1]["started"])
        self.assertFalse(result["originals"][1]["completely_adjudicated"])

    def test_missing_check_cannot_use_supplied_zero_denominator(self):
        original = _original()
        assessment = _assessment(original)
        for observed in [*assessment["first_passes"], assessment["adjudication"]]:
            observed["checks"]["support"] = []
            observed["metrics"]["support"] = copy.deepcopy(_METRICS["empty"])
        result = _report([original], [assessment])
        score = result["dimensions"]["support"]
        self.assertEqual((1, 1, 1, 0), (score["expected"], score["denominator"], score["unknown"], score["observed"]))
        self.assertIsNone(score["score"])
        self.assertFalse(result["originals"][0]["completely_adjudicated"])

    def test_unknown_is_not_pass_and_zero_denominator_is_unavailable(self):
        original = _original(inapplicable=("appropriate_abstention", "unnecessary_refusal"))
        assessment = _assessment(original, verdicts={"support": "unknown"})
        result = _report([original], [assessment])
        support = result["dimensions"]["support"]
        self.assertEqual((0, 1, 1), (support["passed"], support["unknown"], support["denominator"]))
        self.assertEqual({"lower": 0.0, "upper": 1.0}, support["uncertainty_bounds"])
        self.assertIsNone(support["score"])
        for dimension in ("appropriate_abstention", "unnecessary_refusal"):
            score = result["dimensions"][dimension]
            self.assertEqual((1, 1, 0), (score["expected"], score["not_applicable"], score["denominator"]))
            self.assertIsNone(score["score"])
            self.assertEqual({"lower": None, "upper": None}, score["uncertainty_bounds"])
            self.assertIn("zero_applicable_denominator", score["unavailable_reasons"])

    def test_unstarted_inapplicable_original_suppresses_other_successes(self):
        answered = _original()
        missing = _original("original-b", inapplicable=DIMENSIONS)
        result = _report([answered, missing], [_assessment(answered)])
        for score in result["dimensions"].values():
            self.assertEqual((1, 0, 1), (score["passed"], score["unknown"], score["denominator"]))
            self.assertIsNone(score["score"])
            self.assertIn("missing_original_start", score["unavailable_reasons"])

    def test_owner_observed_start_without_applied_findings_remains_unobserved(self):
        original = _original()
        result = _report([original], [], started_original_ids=[original["original_id"]])
        self.assertEqual({"expected": 1, "started": 1, "observed": 0, "completely_adjudicated": 0,
            "missing": 0, "unobserved": 1, "incomplete": 1}, result["counts"])
        summary = result["originals"][0]
        self.assertTrue(summary["started"])
        self.assertFalse(summary["observed"])
        self.assertFalse(summary["complete_first_passes"])
        self.assertEqual(list(reporting.ROLES), summary["missing_roles"])
        for score in result["dimensions"].values():
            self.assertEqual((0, 1, 1), (score["passed"], score["unknown"], score["denominator"]))
            self.assertIsNone(score["score"])
            self.assertIn("missing_original_first_passes", score["unavailable_reasons"])
            self.assertNotIn("missing_original_start", score["unavailable_reasons"])
        self.assertNotIn("started_original_ids", result)
        self.assertNotIn("started_original_ids", reporting.schema_document()["properties"])

    def test_explicit_starts_supplement_inferred_applied_starts(self):
        answered, dispatched = _original(), _original("original-b")
        assessment = _assessment(answered)
        default = _report([answered, dispatched], [assessment])
        empty_explicit = _report([answered, dispatched], [assessment], started_original_ids=[])
        explicit = _report([answered, dispatched], [assessment], started_original_ids=[dispatched["original_id"]])
        self.assertEqual(default["counts"], empty_explicit["counts"])
        self.assertEqual(default["originals"], empty_explicit["originals"])
        self.assertEqual(1, default["counts"]["started"])
        self.assertEqual((2, 1, 1, 0), (explicit["counts"]["started"], explicit["counts"]["observed"],
            explicit["counts"]["completely_adjudicated"], explicit["counts"]["missing"]))
        self.assertTrue(explicit["originals"][0]["started"])
        self.assertTrue(explicit["originals"][1]["started"])
        self.assertFalse(explicit["originals"][1]["observed"])
        self.assertIsNone(explicit["dimensions"]["support"]["score"])

    def test_dispatched_inapplicable_original_does_not_clear_aggregate_coverage(self):
        answered = _original()
        dispatched = _original("original-b", inapplicable=DIMENSIONS)
        result = _report([answered, dispatched], [_assessment(answered)],
            started_original_ids=[dispatched["original_id"]])
        self.assertEqual((2, 1, 1, 0), (result["counts"]["started"], result["counts"]["observed"],
            result["counts"]["completely_adjudicated"], result["counts"]["missing"]))
        for score in result["dimensions"].values():
            self.assertEqual((1, 0, 1, 1), (score["passed"], score["unknown"], score["denominator"], score["not_applicable"]))
            self.assertIsNone(score["score"])
            self.assertIn("missing_original_first_passes", score["unavailable_reasons"])
            self.assertNotIn("missing_original_start", score["unavailable_reasons"])

    def test_explicit_start_ids_are_closed_bounded_and_known(self):
        original = _original()
        values = (True, "original-a", ("original-a",), {"original-a": True}, [True],
            ["foreign-original"], ["original-a", "original-a"], ["other-" + str(index) for index in range(101)])
        for value in values:
            with self.subTest(value=value), self.assertRaises(ExecutionError):
                _report([original], [], started_original_ids=value)

    def test_explicit_start_snapshot_binding_preserves_all_originals_on_pages(self):
        answered, dispatched = _original(), _original("original-b")
        assessments = [_assessment(answered), _assessment(answered, name="old", current=False)]
        ids = [dispatched["original_id"]]
        first = _report([answered, dispatched], assessments, started_original_ids=ids, max_records=1)
        second = _report([answered, dispatched], assessments, started_original_ids=ids,
            after_index=1, expected_snapshot_id=first["snapshot_id"])
        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(first["counts"], second["counts"])
        self.assertEqual(first["originals"], second["originals"])
        self.assertEqual(first["dimensions"], second["dimensions"])
        self.assertEqual(2, second["counts"]["started"])
        self.assertEqual(2, len(second["originals"]))
        with self.assertRaisesRegex(ExecutionError, "snapshot_conflict"):
            _report([answered, dispatched], assessments, started_original_ids=[],
                after_index=1, expected_snapshot_id=first["snapshot_id"])
        default = _report([answered, dispatched], assessments)
        with self.assertRaisesRegex(ExecutionError, "snapshot_conflict"):
            _report([answered, dispatched], assessments, started_original_ids=[],
                expected_snapshot_id=default["snapshot_id"])

    def test_same_material_unit_across_claims_does_not_inflate_or_vote(self):
        original = _original(claim="claim-a")
        for dimension in ("support", "scoped_correctness"):
            original["units"][dimension].append({"unit_id": "claim-b", "applicable": True})
        first = _assessment(original, name="first", claim="claim-a")
        second = _assessment(original, name="second", claim="claim-b", verdicts={"missing_material_evidence": "fail"})
        result = _report([original], [first, second])
        material = result["dimensions"]["missing_material_evidence"]
        self.assertEqual((1, 1, 1, 1), (material["expected"], material["denominator"], material["observed"], material["unknown"]))
        self.assertIsNone(material["score"])
        self.assertIn("conflicting_observations", material["unavailable_reasons"])
        support = result["dimensions"]["support"]
        self.assertEqual((2, 2, 2, 1.0), (support["expected"], support["denominator"], support["passed"], support["score"]))
        self.assertEqual(0, result["counts"]["completely_adjudicated"])

    def test_historical_pass_cannot_replace_current_failure(self):
        original = _original()
        old = _assessment(original, name="old", current=False)
        current = _assessment(original, name="current", verdicts={"support": "fail"})
        result = _report([original], [old, current])
        support = result["dimensions"]["support"]
        self.assertEqual((0, 1, 0, 0.0), (support["passed"], support["failed"], support["unknown"], support["score"]))
        self.assertEqual(1, result["originals"][0]["historical_assessments"])
        self.assertFalse(result["assessments"][0]["scorable"])
        adjudicator = next(row for row in result["judge_configurations"] if row["role"] == "adjudicator")
        self.assertEqual((1, 1), (adjudicator["current_observations"], adjudicator["historical_observations"]))

    def test_historical_only_and_unqualified_current_rows_remain_unknown(self):
        original = _original()
        cases = (
            ([_assessment(original, current=False)], _qualification()),
            ([_assessment(original)], _qualification("unknown")),
            ([_assessment(original)], _qualification("unavailable")),
        )
        for assessments, qualification in cases:
            with self.subTest(state=qualification["state"], current=assessments[0]["current"]):
                result = _report([original], assessments, qualification=qualification)
                for score in result["dimensions"].values():
                    self.assertEqual((1, 1, 0), (score["unknown"], score["denominator"], score["passed"]))
                    self.assertIsNone(score["score"])
                self.assertEqual(0, result["counts"]["completely_adjudicated"])

    def test_expired_withdrawn_and_missing_auxiliary_qualifications_suppress_scores(self):
        original = _original()
        assessment = _assessment(original)
        baseline = _report([original], [assessment])
        self.assertEqual(1, baseline["counts"]["completely_adjudicated"])
        self.assertEqual(1.0, baseline["dimensions"]["support"]["score"])
        cases = []
        expired = _qualification()
        expired["profiles"][1].update(state="unknown", reason="qualification_expired")
        cases.append(("expired", expired, "scope_units_qualification_not_current"))
        withdrawn = _qualification()
        withdrawn["profiles"][3].update(state="unavailable", reason="qualification_withdrawn")
        cases.append(("withdrawn", withdrawn, "adjudicator_qualification_not_current"))
        for index, role in ((1, "scope_units"), (3, "adjudicator")):
            missing = _qualification()
            del missing["profiles"][index]
            cases.append(("missing:" + role, missing, role + "_qualification_profile_missing"))
        for name, qualification, reason in cases:
            with self.subTest(case=name):
                result = _report([original], [assessment], qualification=qualification)
                self.assertEqual("qualified", result["qualification"]["state"])
                self.assertEqual((1, 1, 0), (result["counts"]["started"], result["counts"]["observed"],
                    result["counts"]["completely_adjudicated"]))
                self.assertFalse(result["assessments"][0]["scorable"])
                self.assertIn(reason, result["assessments"][0]["unavailable_reasons"])
                for score in result["dimensions"].values():
                    self.assertEqual((0, 1, 1), (score["passed"], score["unknown"], score["denominator"]))
                    self.assertIsNone(score["score"])
                    self.assertIn(reason, score["unavailable_reasons"])

    def test_observed_qualification_binding_requires_every_native_identity_field(self):
        original = _original()
        assessment = _assessment(original)
        for index, role in ((0, "evidence"), (1, "scope_units"), (2, "counterevidence"), (3, "adjudicator")):
            for field in ("controller_ref", "model_ref", "prompt_version", "qualification_ref"):
                with self.subTest(role=role, field=field):
                    qualification = _qualification()
                    qualification["profiles"][index][field] = "changed:" + field
                    result = _report([original], [assessment], qualification=qualification)
                    self.assertIsNone(result["dimensions"]["support"]["score"])
                    self.assertIn(role + "_qualification_binding_mismatch",
                        result["assessments"][0]["unavailable_reasons"])
            with self.subTest(role=role, field="profile_ref"):
                qualification = _qualification()
                qualification["profiles"][index]["profile_ref"] = "changed:profile"
                result = _report([original], [assessment], qualification=qualification)
                self.assertIsNone(result["dimensions"]["support"]["score"])
                self.assertIn(role + "_qualification_profile_missing", result["assessments"][0]["unavailable_reasons"])

    def test_null_qualification_and_wrong_native_role_cannot_use_top_qualified_state(self):
        original = _original()
        for observed_null in (False, True):
            with self.subTest(observed_null=observed_null):
                assessment, qualification = _assessment(original), _qualification()
                if observed_null:
                    assessment["adjudication"]["qualification_ref"] = None
                else:
                    qualification["profiles"][3]["qualification_ref"] = None
                result = _report([original], [assessment], qualification=qualification)
                self.assertIsNone(result["dimensions"]["support"]["score"])
                self.assertIn("adjudicator_qualification_missing", result["assessments"][0]["unavailable_reasons"])
        qualification = _qualification()
        qualification["profiles"][1]["role"] = "critic"
        result = _report([original], [_assessment(original)], qualification=qualification)
        self.assertIsNone(result["dimensions"]["support"]["score"])
        self.assertIn("scope_units_qualification_profile_missing", result["assessments"][0]["unavailable_reasons"])

    def test_reused_qualified_controller_or_configuration_cannot_score(self):
        original = _original()
        for reuse_configuration in (False, True):
            with self.subTest(reuse_configuration=reuse_configuration):
                assessment, qualification = _assessment(original), _qualification()
                evidence, scope = assessment["first_passes"][:2]
                fields = ("profile_ref", "controller_ref", "model_ref", "prompt_version", "qualification_ref") \
                    if reuse_configuration else ("controller_ref",)
                for field in fields:
                    scope[field] = evidence[field]
                    qualification["profiles"][1][field] = evidence[field]
                if reuse_configuration:
                    del qualification["profiles"][1]
                result = _report([original], [assessment], qualification=qualification)
                self.assertIsNone(result["dimensions"]["support"]["score"])
                self.assertIn("judge_controllers_not_distinct", result["assessments"][0]["unavailable_reasons"])
                if reuse_configuration:
                    self.assertIn("judge_configurations_not_distinct", result["assessments"][0]["unavailable_reasons"])

    def test_old_unit_identities_are_retained_without_addressing_current_inventory(self):
        current = _original(claim="current-claim")
        old = _original(claim="old-claim")
        for dimension in DIMENSIONS:
            old["units"][dimension][0]["unit_id"] = "old-unit:" + dimension
        history = _assessment(old, current=False)
        for observed in [*history["first_passes"], history["adjudication"]]:
            for rows in observed["checks"].values():
                rows[0]["anchors"] = []
                rows[0]["reason"] = "Historical unit observation."
        result = _report([current], [history])
        self.assertEqual(history["basis_id"], result["assessments"][0]["basis_id"])
        self.assertEqual(1, result["originals"][0]["historical_assessments"])
        self.assertFalse(result["assessments"][0]["scorable"])
        self.assertEqual(0, result["counts"]["completely_adjudicated"])
        for score in result["dimensions"].values():
            self.assertEqual((1, 1, 0, 1), (score["expected"], score["denominator"], score["observed"], score["unknown"]))
            self.assertIsNone(score["score"])
        changed = copy.deepcopy(history)
        changed["adjudication"]["metrics"]["support"]["passed"] = 0
        with self.assertRaisesRegex(ExecutionError, "metric_conflict"):
            _report([current], [changed])

    def test_withheld_historical_findings_preserve_commitments_only(self):
        original = _original()
        history = _assessment(original, current=False)
        for observed in [*history["first_passes"], history["adjudication"]]:
            observed["checks"] = {dimension: [] for dimension in DIMENSIONS}
            observed["metrics"] = {dimension: copy.deepcopy(_METRICS["empty"]) for dimension in DIMENSIONS}
        result = _report([original], [history])
        returned = result["assessments"][0]
        for field in ("input_ref", "output_ref", "observation_ref"):
            self.assertEqual(history["adjudication"][field], returned["adjudication"][field])
        self.assertEqual(history["basis_id"], returned["basis_id"])
        self.assertEqual(history["adjudication"]["checks"], returned["adjudication"]["checks"])
        self.assertEqual((1, 1, 0), (result["counts"]["started"], result["counts"]["observed"],
            result["counts"]["completely_adjudicated"]))
        for score in result["dimensions"].values():
            self.assertIsNone(score["score"])
            self.assertEqual(0, score["observed"])

    def test_first_pass_agreement_and_incomplete_triples_are_visible(self):
        original = _original()
        for adjudication in (False, True):
            with self.subTest(adjudication=adjudication):
                assessment = _assessment(original, roles=("evidence", "scope_units"), adjudication=adjudication)
                result = _report([original], [assessment])
                summary = result["originals"][0]
                self.assertTrue(summary["started"])
                self.assertEqual(adjudication, summary["observed"])
                self.assertEqual(["counterevidence"], summary["missing_roles"])
                self.assertEqual(1, summary["incomplete_role_triples"])
                row = result["assessments"][0]
                self.assertEqual("agent_judgment", row["first_pass_judgment_type"])
                self.assertEqual("unknown", row["adjudication_judgment_type"])
                self.assertFalse(row["scorable"])
                for score in result["dimensions"].values():
                    self.assertIsNone(score["score"])
                    self.assertEqual(1, score["unknown"])

    def test_partial_current_claim_suppresses_shared_material_coverage(self):
        original = _original(claim="claim-a")
        for dimension in ("support", "scoped_correctness"):
            original["units"][dimension].append({"unit_id": "claim-b", "applicable": True})
        first = _assessment(original, name="complete", claim="claim-a")
        second = _assessment(original, name="incomplete", claim="claim-b", roles=("evidence",), adjudication=False)
        result = _report([original], [first, second])
        material = result["dimensions"]["missing_material_evidence"]
        self.assertEqual((1, 1), (material["denominator"], material["unknown"]))
        self.assertIsNone(material["score"])
        self.assertIn("incomplete_first_passes", material["unavailable_reasons"])

    def test_reference_strata_and_agent_judgments_stay_separate(self):
        originals = [_original("original-" + str(index)) for index in range(4)]
        assessments = [_assessment(original, name="assessment-" + str(index), tier=tier)
            for index, (original, tier) in enumerate(zip(originals, reporting.REFERENCE_TIERS, strict=True))]
        result = _report(originals, assessments)
        self.assertFalse(result["accuracy_claim"])
        self.assertFalse(result["authorized"])
        self.assertFalse(result["evidence_accepted"])
        for score in result["dimensions"].values():
            self.assertEqual("agent_adjudicated", score["judgment_type"])
            self.assertEqual("not_established", score["statistical_calibration"])
            self.assertFalse(score["accuracy_claim"])
            strata = {row["tier"]: row for row in score["reference_strata"]}
            for tier in reporting.REFERENCE_TIERS:
                self.assertEqual(1, strata[tier]["assessments"])
            self.assertEqual(1, strata["fixed_reference"]["labels"]["accept"])
            self.assertEqual(1, strata["unknown"]["labels"]["unavailable"])
        self.assertEqual("agent_judgment", result["assessments"][0]["first_pass_judgment_type"])
        self.assertEqual(["reference_unavailable"], result["assessments"][3]["unknown_reasons"])

    def test_observed_configuration_and_uncertainty_are_retained(self):
        original = _original()
        assessment = _assessment(original)
        result = _report([original], [assessment])
        self.assertEqual(set((*reporting.ROLES, "adjudicator")), {row["role"] for row in result["judge_configurations"]})
        for row in result["judge_configurations"]:
            self.assertEqual("controller:" + row["role"], row["controller_ref"])
            self.assertEqual("model:" + row["role"], row["model_ref"])
            self.assertEqual("rubric/v1", row["prompt_version"])
            self.assertEqual((1, 0, 1), (row["current_observations"], row["historical_observations"], len(row["observation_refs"])))
        score = result["dimensions"]["support"]
        self.assertEqual(4, len(score["judge_configuration_refs"]))
        self.assertEqual({"basis": "self_report", "scope": "current_adjudication_observations",
            "counts": {"low": 0, "medium": 1, "high": 0, "unknown": 0}}, score["reported_uncertainty"])
        self.assertEqual(48, result["qualification"]["profiles"][0]["reference_cases"])

    def test_duplicates_and_foreign_identities_are_refused(self):
        original = _original()
        assessment = _assessment(original)
        cases = []
        cases.append(([original, copy.deepcopy(original)], [assessment]))
        changed = copy.deepcopy(original)
        changed["units"]["support"].append(copy.deepcopy(changed["units"]["support"][0]))
        cases.append(([changed], [assessment]))
        cases.append(([original], [assessment, copy.deepcopy(assessment)]))
        changed = copy.deepcopy(assessment)
        changed["first_passes"][1]["role"] = "evidence"
        cases.append(([original], [changed]))
        changed = _assessment(original, name="second")
        changed["first_passes"][0]["observation_ref"] = assessment["first_passes"][0]["observation_ref"]
        cases.append(([original], [assessment, changed]))
        changed = copy.deepcopy(assessment)
        changed["original_id"] = "foreign-original"
        cases.append(([original], [changed]))
        changed = copy.deepcopy(assessment)
        changed["adjudication"]["checks"]["support"][0]["unit_id"] = "foreign-unit"
        cases.append(([original], [changed]))
        for index, (originals, assessments) in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(ExecutionError):
                _report(originals, assessments)

    def test_malformed_counts_types_and_closed_fields_are_refused(self):
        original = _original()
        assessment = _assessment(original)
        for field, value in (("expected", True), ("passed", -1), ("denominator", 2), ("score", True), ("score", float("nan"))):
            changed = copy.deepcopy(assessment)
            changed["adjudication"]["metrics"]["support"][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ExecutionError):
                _report([original], [changed])
        changed = copy.deepcopy(assessment)
        changed["current"] = 1
        with self.assertRaises(ExecutionError):
            _report([original], [changed])
        changed = copy.deepcopy(assessment)
        changed["authorized"] = True
        with self.assertRaises(ExecutionError):
            _report([original], [changed])
        changed = _qualification()
        changed["profiles"][0]["reference_cases"] = True
        with self.assertRaises(ExecutionError):
            _report([original], [assessment], qualification=changed)
        with self.assertRaises(ExecutionError):
            _report((original,), [assessment])

    def test_invalid_anchor_and_applicability_claim_do_not_clear_inventory(self):
        original = _original()
        changed = _assessment(original)
        changed["adjudication"]["checks"]["support"][0]["anchors"][0]["end"] = 12
        with self.assertRaisesRegex(ExecutionError, "invalid_anchor"):
            _report([original], [changed])
        changed = _assessment(original, verdicts={"support": "not_applicable"})
        result = _report([original], [changed])
        score = result["dimensions"]["support"]
        self.assertEqual((1, 1, 0), (score["denominator"], score["unknown"], score["not_applicable"]))
        self.assertIsNone(score["score"])
        self.assertIn("applicability_conflict", score["unavailable_reasons"])

    def test_bounds_and_strict_cursors(self):
        original = _original()
        assessment = _assessment(original)
        for options in ({"after_index": True}, {"after_index": -1}, {"after_index": 1.0},
                {"after_index": "1"}, {"max_records": True}, {"max_records": 0}, {"max_records": 17},
                {"after_index": 1}, {"expected_snapshot_id": "unbound"}):
            with self.subTest(options=options), self.assertRaises(ExecutionError):
                _report([original], [assessment], **options)
        with self.assertRaises(ExecutionError):
            _report([_original("original-" + str(index)) for index in range(101)], [])
        oversized = [_assessment(original, name="large-" + str(index), roles=(), adjudication=False)
            for index in range(10)]
        for row in oversized:
            row["unknown_reasons"] = ["x" * 1024] * 32
        with self.assertRaisesRegex(ExecutionError, "limit"):
            _report([original], oversized)

    def test_pagination_snapshot_binds_full_inventory_and_all_owner_inputs(self):
        originals = [_original("original-" + str(index)) for index in range(3)]
        assessments = [_assessment(original, name="assessment-" + str(index)) for index, original in enumerate(originals)]
        first = _report(originals, assessments, max_records=1)
        self.assertEqual((1, 3, 1, True, True), (first["paging"]["returned"], first["paging"]["total"],
            first["paging"]["next_index"], first["paging"]["has_more"], first["paging"]["truncated"]))
        self.assertEqual(3, len(first["originals"]))
        self.assertEqual(3, first["counts"]["completely_adjudicated"])
        self.assertEqual(3, first["dimensions"]["support"]["denominator"])
        second = _report(originals, assessments, after_index=1, max_records=1, expected_snapshot_id=first["snapshot_id"])
        self.assertEqual(assessments[1]["assessment_id"], second["assessments"][0]["assessment_id"])
        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(first["dimensions"], second["dimensions"])
        self.assertEqual(first["originals"], second["originals"])
        last = _report(originals, assessments, after_index=2, expected_snapshot_id=first["snapshot_id"])
        self.assertFalse(last["paging"]["has_more"])
        self.assertIsNone(last["paging"]["next_index"])
        changed = copy.deepcopy(assessments)
        changed[2]["reference"]["scope"] = "Changed owner reference scope."
        with self.assertRaisesRegex(ExecutionError, "snapshot_conflict"):
            _report(originals, changed, after_index=1, expected_snapshot_id=first["snapshot_id"])
        qualification = _qualification()
        qualification["reason"] = "Changed owner qualification observation."
        with self.assertRaisesRegex(ExecutionError, "snapshot_conflict"):
            _report(originals, assessments, qualification=qualification, expected_snapshot_id=first["snapshot_id"])
        with self.assertRaisesRegex(ExecutionError, "cursor_invalid"):
            _report(originals, assessments, after_index=4, expected_snapshot_id=first["snapshot_id"])

    def test_results_and_schemas_are_detached(self):
        original = _original()
        assessment = _assessment(original)
        qualification = _qualification()
        first = _report([original], [assessment], qualification=qualification)
        second = _report([original], [assessment], qualification=qualification)
        self.assertEqual(first, second)
        first["assessments"][0]["adjudication"]["checks"]["support"][0]["anchors"][0]["quote"] = "Changed."
        first["qualification"]["profiles"][0]["reason"] = "Changed."
        first["originals"][0]["question_ids"].append("added")
        self.assertEqual("Known fact.", assessment["adjudication"]["checks"]["support"][0]["anchors"][0]["quote"])
        self.assertEqual("Finite reference scope.", qualification["profiles"][0]["reason"])
        self.assertEqual(["question:original-a"], second["originals"][0]["question_ids"])
        self.assertEqual(second, _report([original], [assessment], qualification=qualification))
        schema = reporting.schema_document()
        schema["additionalProperties"] = True
        schema["properties"]["authorized"]["const"] = True
        fresh = reporting.schema_document()
        self.assertFalse(fresh["additionalProperties"])
        self.assertFalse(fresh["properties"]["authorized"]["const"])
        with self.assertRaises(ExecutionError):
            reporting.schema_document("foreign-report/v1")


if __name__ == "__main__":
    unittest.main()
