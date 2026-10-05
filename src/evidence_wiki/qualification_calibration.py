"""Protected reference calibration with explicit sampling and sensitivity scope.

This owner authenticates selected host observations, not reference correctness.
Reference provenance and configuration attestations remain protected host duties.
The separate report does not reinterpret or promote legacy qualifications.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from evidence_wiki.agent_execution import ExecutionAttempt, ExecutionProfile, _ref, _utc, validate_observation
from evidence_wiki.agent_qualification import QualificationError, _ratio
from evidence_wiki.mutation_contracts import canonical, detached
from evidence_wiki.research_execution import bounded_host_call
from evidence_wiki.run_policy import RunPolicy

SUITE = "research-calibration-suite/v1"
REPORT = "research-calibration-report/v1"
REPORT_AUXILIARY = "research-auxiliary-calibration-report/v1"
AXES = ("meaning", "repeat", "identity", "position", "verbosity")
ORACLE_TIERS = ("executable_oracle", "controlled_mutation", "fixed_reference")
CASE_FIELDS = {"case_id", "unit_ref", "family_id", "domain_ref", "kind", "base_case_id",
               "input_document", "expected_label", "critical", "reference"}
REFERENCE_FIELDS = {"tier", "reference_ref", "basis_sha256"}
MAX_CASES = 2000
MAX_BYTES = 1_048_576


def fail(reason):
    raise QualificationError("calibration_" + reason) from None


def plain(value):
    try:
        result = detached(value)
        if len(canonical(result)) > MAX_BYTES:
            fail("limit")
        return result
    except QualificationError:
        raise
    except Exception:
        fail("invalid_plain_data")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def hash_ref(value):
    if type(value) is not str or len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        fail("digest_invalid")
    return value


def cases_document(cases):
    cases = plain(cases)
    if type(cases) is not list or not 1 <= len(cases) <= MAX_CASES:
        fail("cases_invalid")
    lookup, units, variants, baseline_inputs = {}, set(), set(), set()
    for case in cases:
        if type(case) is not dict or set(case) != CASE_FIELDS:
            fail("case_invalid")
        for key in ("case_id", "unit_ref", "family_id", "domain_ref"):
            _ref(case[key])
        if case["case_id"] in lookup or type(case["input_document"]) is not dict:
            fail("case_invalid")
        if type(case["kind"]) is not str or case["kind"] not in ("baseline", *AXES):
            fail("axis_invalid")
        if type(case["expected_label"]) is not str or case["expected_label"] not in {"accept", "reject", "unknown"}:
            fail("label_invalid")
        if type(case["critical"]) is not bool or case["critical"] and case["expected_label"] == "accept":
            fail("critical_reference_invalid")
        reference = case["reference"]
        if (type(reference) is not dict or set(reference) != REFERENCE_FIELDS
                or type(reference["tier"]) is not str or reference["tier"] not in (*ORACLE_TIERS, "agent_judgment")):
            fail("reference_invalid")
        _ref(reference["reference_ref"])
        hash_ref(reference["basis_sha256"])
        pending = [case["input_document"]]
        while pending:
            item = pending.pop()
            if type(item) is dict:
                if "expected_label" in item or "reference_label" in item:
                    fail("label_exposed")
                pending.extend(item.values())
            elif type(item) is list:
                pending.extend(item)
        if case["kind"] == "baseline":
            if case["base_case_id"] is not None or case["unit_ref"] in units:
                fail("logical_unit_duplicate")
            fingerprint = digest(case["input_document"])
            if fingerprint in baseline_inputs:
                fail("reference_input_duplicate")
            baseline_inputs.add(fingerprint)
            units.add(case["unit_ref"])
        else:
            _ref(case["base_case_id"])
            axis_unit = (case["base_case_id"], case["kind"])
            if axis_unit in variants:
                fail("variant_unit_duplicate")
            variants.add(axis_unit)
        lookup[case["case_id"]] = case
    for case in cases:
        if case["kind"] == "baseline":
            continue
        base = lookup.get(case["base_case_id"])
        if base is None or base["kind"] != "baseline":
            fail("baseline_missing")
        if any(case[key] != base[key] for key in ("unit_ref", "family_id", "domain_ref", "expected_label", "critical", "reference")):
            fail("variant_binding_invalid")
        if case["kind"] == "repeat" and case["input_document"] != base["input_document"]:
            fail("repeat_input_changed")
        if case["kind"] != "repeat" and case["input_document"] == base["input_document"]:
            fail("variant_unchanged")
    return cases


def calibration_suite(cases, *, suite_ref, split_ref, domains, rubric, model_settings, drift_ref=None):
    """Freeze host-protected logical references; variants do not inflate samples."""
    cases = cases_document(cases)
    _ref(suite_ref)
    _ref(split_ref)
    domains = plain(domains)
    if (type(domains) is not list or not 1 <= len(domains) <= 16
            or any(type(domain) is not str for domain in domains) or len(set(domains)) != len(domains)):
        fail("domains_invalid")
    for domain in domains:
        _ref(domain)
    rubric, model_settings = plain(rubric), plain(model_settings)
    if type(rubric) is not dict or not rubric.get("id") or not rubric.get("revision") or type(model_settings) is not dict:
        fail("configuration_invalid")
    _ref(rubric["id"])
    _ref(rubric["revision"])
    if drift_ref is not None:
        _ref(drift_ref)
    if any(case["domain_ref"] not in domains for case in cases):
        fail("domain_undeclared")
    result = {"schema_version": SUITE, "suite_ref": suite_ref, "split_ref": split_ref, "domains": sorted(domains),
              "rubric": rubric, "model_settings": model_settings, "drift_ref": drift_ref, "cases": cases}
    result["suite_sha256"] = digest(result)
    return plain(result)


def _metrics(cases, results):
    binary = [case for case in cases if case["expected_label"] != "unknown"]
    positive = [case for case in cases if case["expected_label"] == "accept"]
    necessary = [case for case in cases if case["expected_label"] == "unknown"]
    critical = [case for case in cases if case["critical"]]
    counts = {"expected_cases": len(cases), "observed_cases": sum(case["case_id"] in results for case in cases),
              "positive_cases": len(positive), "positive_acceptances": sum(results.get(case["case_id"]) == "accept" for case in positive),
              "necessary_abstention_cases": len(necessary),
              "necessary_abstentions": sum(results.get(case["case_id"]) == "unknown" for case in necessary),
              "unnecessary_abstentions": sum(results.get(case["case_id"]) == "unknown" for case in binary),
              "critical_cases": len(critical),
              "critical_false_acceptances": sum(results.get(case["case_id"]) == "accept" for case in critical)}
    complete = counts["expected_cases"] == counts["observed_cases"]
    metrics = {"positive_acceptance": _ratio(counts["positive_acceptances"], len(positive)) if complete else None,
               "necessary_abstention": _ratio(counts["necessary_abstentions"], len(necessary)) if complete else None,
               "unnecessary_abstention": _ratio(counts["unnecessary_abstentions"], len(binary), upper=True) if complete else None}
    return {"counts": counts, "metrics": metrics, "complete": complete}


def _stratum(cases, results, frozen):
    bases = [case for case in cases if case["kind"] == "baseline" and case["reference"]["tier"] in ORACLE_TIERS]
    summary = _metrics(bases, results)
    limits = frozen["thresholds"]
    families = {case["family_id"] for case in bases}
    reasons, axes = [], {}
    if len(bases) < limits["min_reference_cases"] or len(families) < limits["min_families"]:
        reasons.append("insufficient_independent_samples")
    if len({digest(case["input_document"]) for case in bases}) != len(bases):
        reasons.append("duplicate_reference_input")
    if len({case["reference"]["basis_sha256"] for case in bases}) != len(bases):
        reasons.append("duplicate_reference_basis")
    if not summary["complete"]:
        reasons.append("missing_reference_observations")
    if any(results.get(case["case_id"]) == "unknown" for case in cases if case["expected_label"] != "unknown"):
        reasons.append("unnecessary_abstention_coverage")
    if not all(summary["counts"][field] for field in ("positive_cases", "critical_cases", "necessary_abstention_cases")):
        reasons.append("missing_reference_classes")
    for kind in AXES:
        variants = [case for case in cases if case["kind"] == kind and case["reference"]["tier"] in ORACLE_TIERS]
        linked = {case["unit_ref"] for case in variants}
        observed = [case for case in variants if case["case_id"] in results and case["base_case_id"] in results]
        flips = sum(results[case["case_id"]] != results[case["base_case_id"]] for case in observed)
        adequate = (len(linked) >= limits["min_reference_cases"]
                    and len({case["family_id"] for case in variants}) >= limits["min_families"]
                    and len(observed) == len(variants))
        axes[kind] = {"expected_pairs": len(variants), "observed_pairs": len(observed),
                      "independent_units": len(linked), "flips": flips,
                      "flip_rate": _ratio(flips, len(observed), upper=True) if adequate else None}
        if not adequate:
            reasons.append("insufficient_" + kind + "_coverage")
    failures = []
    all_references = _metrics(cases, results)
    if all_references["counts"]["critical_false_acceptances"] > limits["critical_false_acceptances_max"]:
        failures.append("critical_false_acceptance")
    positive = summary["metrics"]["positive_acceptance"]
    if positive is not None and Decimal(positive) < Decimal(limits["positive_acceptance_min"]):
        failures.append("positive_acceptance_below_policy")
    for kind, value in axes.items():
        if value["flip_rate"] is not None and Decimal(value["flip_rate"]) > Decimal(limits["meaning_preserving_flip_max"]):
            failures.append(kind + "_sensitivity_exceeds_policy")
    if any(case["case_id"] in results and results[case["case_id"]] != "unknown"
           for case in cases if case["expected_label"] == "unknown"):
        reasons.append("abstention_behavior_not_qualified")
    return {**summary, "all_references": all_references, "independent_reference_units": len(bases), "families": len(families), "axes": axes,
            "state": "unqualified" if failures else "unknown" if reasons else "qualified", "reasons": sorted(set(reasons + failures))}


@dataclass(frozen=True, init=False)
class CalibrationAuthority:
    """Protect labels and signed bounded metrics outside evaluator execution."""

    issuer_ref: str
    generator_controller_ref: str
    _key: bytes = field(repr=False)

    def __init__(self, issuer_ref, key, generator_controller_ref):
        _ref(issuer_ref)
        _ref(generator_controller_ref)
        if type(key) is not bytes or not 32 <= len(key) <= 64:
            fail("key_invalid")
        object.__setattr__(self, "issuer_ref", issuer_ref)
        object.__setattr__(self, "generator_controller_ref", generator_controller_ref)
        object.__setattr__(self, "_key", key)

    def issue(self, policy, profile, config_ref, suite, observations, executor, *, expires_utc,
              observed_utc=None, verification_seconds=5, auxiliary=False):
        """Score protected references using finite selected host verification.

        The executor's calibration_configuration(config_ref) must attest its
        effective settings, reference-domain scope and model execution class.
        Existing executors without that service remain explicitly unknown here.
        """
        if type(policy) is not RunPolicy or type(profile) is not ExecutionProfile or executor.profile.sha256 != profile.sha256:
            fail("binding_invalid")
        if type(verification_seconds) not in (int, float) or not 0 < verification_seconds <= 30:
            fail("verification_bound_invalid")
        suite = plain(suite)
        if type(suite) is not dict or suite.get("schema_version") != SUITE:
            fail("suite_invalid")
        if set(suite) != {"schema_version", "suite_ref", "split_ref", "domains", "rubric", "model_settings", "drift_ref", "cases", "suite_sha256"}:
            fail("suite_invalid")
        expected = calibration_suite(suite["cases"], **{key: suite[key] for key in
            ("suite_ref", "split_ref", "domains", "rubric", "model_settings", "drift_ref")})
        if expected != suite:
            fail("suite_changed")
        role, frozen = profile.role(config_ref), policy.to_dict()
        from .review_service_policy import approved_profiles
        required = [row for row in approved_profiles(frozen, profile) if row["profile_ref"] == config_ref]
        if type(auxiliary) is not bool:
            fail("role_invalid")
        if auxiliary:
            if profile.disagreement is None:
                fail("role_invalid")
            selected = profile.disagreement.auxiliary_profile(policy, profile)
            required = [selected] if selected is not None and selected["profile_ref"] == config_ref else []
        if (role.role != "evaluator" or len(required) != 1 or required[0]["controller_ref"] != role.controller_ref
                or required[0]["human_required"]):
            fail("role_invalid")
        now = observed_utc or datetime.now(timezone.utc).isoformat()
        if not _utc(now) < _utc(expires_utc):
            fail("expiry_invalid")
        binding = {"policy_id": policy.policy_id, "execution_profile_sha256": profile.sha256, "config_ref": config_ref,
                   "controller_ref": role.controller_ref, "model_ref": role.model_ref, "prompt_version": role.prompt_version,
                   "model_settings_sha256": digest(suite["model_settings"]), "rubric_sha256": digest(suite["rubric"]),
                   "scope_id": digest(frozen["scope"]), "domains": suite["domains"], "drift_ref": suite["drift_ref"]}
        if auxiliary:
            binding["qualification_purpose"] = "auxiliary_adjudication"
        if profile.calibration is not None:
            selection = profile.calibration.selection(config_ref).document()
            if suite["suite_sha256"] != selection["suite_sha256"]:
                fail("selected_suite_changed")
            binding.update(model_revision=selection["model_revision"],
                reference_assessment_ref=selection["reference_assessment"]["assessment_ref"],
                domain_descriptor_sha256=digest(selection["domain_descriptor"]))
        until, reasons = time.monotonic() + verification_seconds, []
        configuration = getattr(executor, "calibration_configuration", None)

        def probe():
            if not callable(configuration):
                return None
            remaining = min(until - time.monotonic(), profile.cleanup_timeout_seconds)
            if remaining <= 0:
                return None
            value, error = bounded_host_call(configuration, (config_ref,), remaining)
            return plain(value) if error is None else None

        configured = probe()
        expected_configuration = {key: binding[key] for key in ("execution_profile_sha256", "config_ref", "controller_ref",
            "model_ref", "prompt_version", "model_settings_sha256", "rubric_sha256", "domains")}
        expected_configuration.update(suite_sha256=suite["suite_sha256"], split_ref=suite["split_ref"])
        if profile.calibration is not None:
            expected_configuration.update({key: binding[key] for key in
                ("model_revision", "reference_assessment_ref", "domain_descriptor_sha256")})
        if configured is None:
            reasons.append("configuration_unobserved")
        elif (type(configured) is not dict or any(configured.get(key) != value for key, value in expected_configuration.items())
              or configured.get("execution_kind") != "model" or type(configured.get("reference_assessment_ref")) is not str
              or not configured.get("reference_assessment_ref")):
            reasons.append("configuration_or_domain_unqualified")
        elif configured is not None:
            try:
                _ref(configured["reference_assessment_ref"])
            except ValueError:
                reasons.append("configuration_or_domain_unqualified")
        if role.model_ref.startswith("offline/"):
            reasons.append("offline_execution_only")
        if type(observations) not in (list, tuple) or len(observations) > len(suite["cases"]):
            fail("observations_invalid")
        lookup = {case["case_id"]: case for case in suite["cases"]}
        results, refs, attempts = {}, [], set()
        for entry in observations:
            if type(entry) not in (list, tuple) or len(entry) != 2:
                fail("observation_invalid")
            attempt, supplied = entry
            if type(attempt) is not ExecutionAttempt or attempt.attempt_id in attempts:
                fail("attempt_invalid")
            attempts.add(attempt.attempt_id)
            if (attempt.profile_sha256 != profile.sha256 or attempt.config_ref != config_ref
                    or attempt.job["operation"] != "evaluate" or attempt.job["policy_id"] != policy.policy_id
                    or set(attempt.job["scope"]) != set(frozen["scope"]["question_ids"])):
                fail("attempt_binding_invalid")
            remaining = min(until - time.monotonic(), profile.cleanup_timeout_seconds)
            if remaining <= 0:
                reasons.append("verification_budget_exhausted")
                break
            observed, error = bounded_host_call(executor.authenticate, (supplied, attempt), remaining)
            if error is not None:
                reasons.append("observation_unverified")
                break
            observed = validate_observation(observed)
            if observed != supplied:
                fail("authentication_changed_observation")
            if (observed["attempt_id"] != attempt.attempt_id or observed["nonce"] != attempt.nonce
                    or observed["input_sha256"] != attempt.input_sha256 or observed["job_sha256"] != attempt.job_sha256
                    or observed["profile_sha256"] != profile.sha256 or observed["config_ref"] != config_ref
                    or observed["controller_ref"] != role.controller_ref or observed["model_ref"] != role.model_ref
                    or observed["prompt_version"] != role.prompt_version
                    or observed["run_id"] != attempt.run_id or observed["binding_id"] != attempt.binding_id
                    or observed["job_id"] != attempt.job["id"] or observed["issued_utc"] != attempt.job["issued_utc"]
                    or observed["deadline_utc"] != attempt.job["deadline_utc"]
                    or observed["currency"] != profile.currency or observed["pricing_ref"] != profile.pricing_ref
                    or attempt.job["role"] != role.role or attempt.job["controller_ref"] != role.controller_ref
                    or attempt.job["prompt_version"] != role.prompt_version):
                fail("observation_binding_invalid")
            if profile.role_scopes is not None:
                profile.check_job_scope(config_ref, attempt.job, attempt.input_document)
                if profile.role_scopes.scope(config_ref).task_role != required[0]["role"]:
                    fail("role_scope_mismatch")
            profile_cap = profile.document()["job_budgets"]
            if (any(attempt.job["budgets"][name] > profile_cap[name] for name in ("model_calls", "tool_calls", "tokens"))
                    or Decimal(attempt.job["budgets"]["cost"]) > Decimal(profile_cap["cost"])):
                fail("job_budget_exceeded")
            usage, cap = observed["observed_usage"], attempt.job["budgets"]
            if (any(usage[name] is None or usage[name] > cap[name] for name in ("model_calls", "tool_calls", "tokens"))
                    or usage["cost"] is None or Decimal(usage["cost"]) > Decimal(cap["cost"])
                    or usage["seconds"] is None or usage["seconds"] > attempt.job["timeout_seconds"]):
                fail("receipt_budget_exceeded")
            output = observed["output"]
            if (observed["state"] != "succeeded" or not observed["final_accounting"] or observed["completed_utc"] is None or observed["started_utc"] is None
                    or not _utc(attempt.job["issued_utc"]) <= _utc(observed["started_utc"]) <= _utc(observed["completed_utc"])
                    or not _utc(observed["completed_utc"]) <= _utc(observed["observed_utc"]) <= _utc(now)
                    or _utc(observed["completed_utc"]) > _utc(attempt.job["deadline_utc"])
                    or type(output) is not dict or set(output) != {"case_id", "label"}
                    or type(output["label"]) is not str or output["label"] not in {"accept", "reject", "unknown"}):
                fail("judgment_invalid")
            case = lookup.get(output["case_id"] if type(output["case_id"]) is str else None)
            if case is None or case["case_id"] in results or attempt.input_document != case["input_document"]:
                fail("case_binding_invalid")
            if case["reference"]["tier"] in ORACLE_TIERS and (observed["reason"] == "offline_fixture"
                    or observed["observed_usage"]["model_calls"] in (None, 0)):
                reasons.append("model_execution_unobserved")
            results[case["case_id"]] = output["label"]
            refs.append({"case_id": case["case_id"], "attempt_id": attempt.attempt_id,
                         "observation_sha256": digest(observed), "input_sha256": attempt.input_sha256})
        if probe() != configured:
            reasons.append("configuration_changed")
        protected = [case for case in suite["cases"] if case["reference"]["tier"] in ORACLE_TIERS]
        domains = {domain: _stratum([case for case in protected if case["domain_ref"] == domain], results, frozen)
                   for domain in suite["domains"]}
        families = {}
        for domain in suite["domains"]:
            families[domain] = {family: _metrics([case for case in protected if case["kind"] == "baseline"
                and case["domain_ref"] == domain and case["family_id"] == family], results)
                for family in sorted({case["family_id"] for case in protected if case["domain_ref"] == domain})}
        independent = (role.controller_ref != self.generator_controller_ref
                       and len({row["controller_ref"] for row in frozen["required_evaluators"]}) == len(frozen["required_evaluators"]))
        if not independent:
            reasons.append("controllers_not_independent")
        if any(value["state"] == "unqualified" for value in domains.values()):
            state = "unqualified"
        elif reasons or any(value["state"] != "qualified" for value in domains.values()):
            state = "unknown"
        else:
            state = "qualified"
        result = {"schema_version": REPORT_AUXILIARY if auxiliary else REPORT, "issuer_ref": self.issuer_ref, "binding": binding,
                  "suite_ref": suite["suite_ref"], "split_ref": suite["split_ref"], "suite_sha256": suite["suite_sha256"],
                  "issued_utc": now, "expires_utc": expires_utc, "state": state, "independent": independent,
                  "reasons": sorted(set(reasons)), "domains": domains, "families": families,
                  "agent_judgment_tier": _metrics([case for case in suite["cases"] if case["reference"]["tier"] == "agent_judgment"], results),
                  "observation_refs": refs,
                  "limits": frozen["thresholds"], "verification_scope": "bounded_host_authentication_only",
                  "limitations": ["Reference provenance and domain representation are authenticated host assertions, not model self-confidence.",
                                  "Calibration/setup and host verification costs have their own scope; this record invents no zero total.",
                                  "No general semantic or OS-isolation qualification follows from a reference-domain record."],
                  "authorized": False, "evidence_accepted": False}
        result["record_ref"] = digest(result)
        result["authentication"] = hmac.new(self._key, b"research-calibration/v1\0" + canonical(result), hashlib.sha256).hexdigest()
        return plain(result)

    def verify(self, value):
        value = plain(value)
        if type(value) is not dict:
            fail("record_invalid")
        signature = value.pop("authentication", None)
        if (value.get("schema_version") not in {REPORT, REPORT_AUXILIARY} or value.get("issuer_ref") != self.issuer_ref
                or type(signature) is not str or not hmac.compare_digest(signature,
                    hmac.new(self._key, b"research-calibration/v1\0" + canonical(value), hashlib.sha256).hexdigest())):
            fail("authentication_refused")
        record_ref = value.pop("record_ref", None)
        if record_ref != digest(value):
            fail("record_changed")
        value["record_ref"], value["authentication"] = record_ref, signature
        return value

    def current(self, value, *, policy, profile, config_ref, rubric, model_settings, domain_ref, now_utc,
                drift_ref=None, revoked=False, auxiliary=False):
        value = self.verify(value)
        role = profile.role(config_ref)
        wanted = {"policy_id": policy.policy_id, "execution_profile_sha256": profile.sha256, "config_ref": config_ref,
                  "controller_ref": role.controller_ref, "model_ref": role.model_ref, "prompt_version": role.prompt_version,
                  "rubric_sha256": digest(rubric), "model_settings_sha256": digest(model_settings),
                  "scope_id": digest(policy.to_dict()["scope"]), "drift_ref": drift_ref}
        reasons = []
        if (type(auxiliary) is not bool or value["schema_version"] != (REPORT_AUXILIARY if auxiliary else REPORT)
                or (value["binding"].get("qualification_purpose") == "auxiliary_adjudication") != auxiliary):
            reasons.append("qualification_purpose_mismatch")
        if auxiliary:
            selected = None if profile.disagreement is None else profile.disagreement.auxiliary_profile(policy, profile)
            if selected is None or selected["profile_ref"] != config_ref:
                reasons.append("qualification_purpose_mismatch")
        if revoked:
            reasons.append("profile_revoked")
        if not _utc(value["issued_utc"]) <= _utc(now_utc) < _utc(value["expires_utc"]):
            reasons.append("profile_expired_or_clock_regressed")
        if any(value["binding"].get(key) != field for key, field in wanted.items()):
            reasons.append("configuration_or_scope_changed")
        if domain_ref not in value["binding"]["domains"]:
            reasons.append("domain_unqualified")
        state = "unknown" if reasons else value["domains"][domain_ref]["state"] if value["state"] == "qualified" else value["state"]
        return {"state": state, "reasons": reasons or value["reasons"], "record_ref": value["record_ref"],
                "authorized": False, "evidence_accepted": False}
