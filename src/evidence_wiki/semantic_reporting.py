"""Bounded semantic metadata over owner-authenticated normalized observations.

This pure projection retains neither execution nor qualification authority.
The owning session authenticates inputs and rechecks current source rights;
the report preserves its frozen inventory, reference strata and uncertainty.
"""

from __future__ import annotations

import copy
from collections import Counter

from .agent_execution import ExecutionError
from .mutation_contracts import _shape, canonical, detached, digest

REPORT = "research-semantic-judging-report/v1"
MAX_ORIGINALS = 100
MAX_ASSESSMENTS = 1024
MAX_PAGE = 16
MAX_BYTES = 262_144
MAX_UNITS = 100
ROLES = ("evidence", "scope_units", "counterevidence")
REFERENCE_TIERS = ("executable_oracle", "controlled_mutation", "fixed_reference", "unknown")
UNCERTAINTIES = ("low", "medium", "high", "unknown")
_LIMITATIONS = (
    "Owner-authenticated normalized inputs; this projection performs no authentication or current-rights check.",
    "First passes are agent judgments; current qualified adjudications remain agent-adjudicated observations.",
    "Reference strata retain owner provenance; agreement does not establish reference-label accuracy.",
    "Reported uncertainty is self-report; unresolved-unit bounds are not statistical confidence intervals.",
    "Per-dimension statistical calibration and general semantic accuracy are not established.",
)


def refuse(reason):
    raise ExecutionError("semantic_report_" + reason) from None


def _dimensions():
    from .semantic_contracts import DIMENSIONS

    return DIMENSIONS


def _object(**fields):
    return {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}


def _text(maximum=1024):
    return {"type": "string", "minLength": 1, "maxLength": maximum}


def _reference():
    return {**_text(256), "pattern": r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}$(?![\s\S])"}


def _hashed():
    return {**_text(71), "pattern": r"^sha256:[a-f0-9]{64}$(?![\s\S])"}


def _count(maximum=MAX_ASSESSMENTS * MAX_UNITS):
    return {"type": "integer", "minimum": 0, "maximum": maximum}


def _enum(values):
    return {"type": "string", "enum": list(values)}


def _array(shape, maximum):
    return {"type": "array", "items": shape, "maxItems": maximum}


def _nullable(shape):
    return {"anyOf": [shape, {"type": "null"}]}


def _metrics_schema():
    ratio = _nullable({"type": "number", "minimum": 0, "maximum": 1})
    return _object(**dict.fromkeys(("expected", "applicable", "passed", "failed", "unknown", "not_applicable",
        "numerator", "denominator"), _count()), score=ratio,
        uncertainty_bounds=_object(lower=ratio, upper=ratio))


def _checks_schema():
    anchor = _object(pointer={**_text(), "pattern": r"^/(?:[^~]|~[01])*$(?![\s\S])"},
        start=_count(MAX_BYTES), end={"type": "integer", "minimum": 1, "maximum": MAX_BYTES}, quote=_text(4096))
    row = _object(unit_id=_reference(), verdict=_enum(("pass", "fail", "unknown", "not_applicable")),
        reason=_text(), severity=_enum(("none", "minor", "major", "critical", "unknown")),
        uncertainty=_enum(UNCERTAINTIES), anchors=_array(anchor, 4))
    return _object(**{dimension: _array(row, MAX_UNITS) for dimension in _dimensions()})


def _observation_schema(first_pass=False):
    fields = dict(profile_ref=_reference(), controller_ref=_reference(), model_ref=_reference(),
        prompt_version=_reference(), qualification_ref=_nullable(_reference()), input_ref=_hashed(),
        output_ref=_hashed(), observation_ref=_hashed(), checks=_checks_schema(),
        metrics=_object(**{dimension: _metrics_schema() for dimension in _dimensions()}))
    if first_pass:
        fields["role"] = _enum(ROLES)
    return _object(**fields)


def _assessment_schema():
    reference = _object(tier=_enum(REFERENCE_TIERS), label_ref=_nullable(_hashed()),
        label=_nullable(_enum(("accept", "reject", "unknown"))), scope=_text())
    return _object(assessment_id=_hashed(), original_id=_reference(), claim_id=_reference(), basis_id=_hashed(),
        current={"type": "boolean"}, first_passes=_array(_observation_schema(True), 3),
        adjudication=_nullable(_observation_schema()), reference=reference, unknown_reasons=_array(_text(), 32))


def _qualification_schema():
    profile = _object(profile_ref=_reference(), role=_enum(("evaluator", "critic", "adjudicator")),
        controller_ref=_reference(), model_ref=_reference(), prompt_version=_reference(),
        qualification_ref=_nullable(_reference()), state=_enum(("qualified", "unknown", "unavailable")),
        reason=_text(), reference_cases=_nullable(_count(2000)), families=_nullable(_count(2000)),
        critical_false_acceptances=_nullable(_count(2000)), positive_acceptance=_nullable(_text(128)),
        meaning_preserving_flip=_nullable(_text(128)), calibration_scope=_text(256))
    return _object(state=_enum(("qualified", "unknown", "unavailable")), reason=_text(), profiles=_array(profile, 32))


def _input_schema():
    unit = _object(unit_id=_reference(), applicable={"type": "boolean"})
    original = _object(original_id=_reference(), original_text_sha256=_hashed(),
        question_ids=_array(_reference(), MAX_UNITS), outcome=_reference(),
        units=_object(**{dimension: _array(unit, MAX_UNITS) for dimension in _dimensions()}))
    return _object(schema_version={"const": REPORT}, run_id=_reference(), binding_id=_hashed(), policy_id=_hashed(),
        profile_sha256={**_text(64), "pattern": r"^[a-f0-9]{64}$(?![\s\S])"},
        originals=_array(original, MAX_ORIGINALS), assessments=_array(_assessment_schema(), MAX_ASSESSMENTS),
        qualification=_qualification_schema(), started_original_ids=_nullable(_array(_reference(), MAX_ORIGINALS)))


def schemas():
    """Return caller-owned closed report schemas without acceptance authority."""
    boolean, hashed = {"type": "boolean"}, _hashed()
    labels = _object(**dict.fromkeys(("accept", "reject", "unknown", "unavailable"), _count()))
    strata = _array(_object(tier=_enum(REFERENCE_TIERS), assessments=_count(MAX_ASSESSMENTS),
        current_assessments=_count(MAX_ASSESSMENTS), reported_units=_count(), labels=labels), len(REFERENCE_TIERS))
    score = _metrics_schema()
    score["properties"].update(observed=_count(), status=_enum(("available", "unavailable")),
        judgment_type=_enum(("agent_adjudicated", "unknown")), reference_strata=strata,
        judge_configuration_refs=_array(hashed, MAX_ASSESSMENTS * 4),
        reported_uncertainty=_object(basis={"const": "self_report"},
            scope={"const": "current_adjudication_observations"},
            counts=_object(**dict.fromkeys(UNCERTAINTIES, _count()))),
        unavailable_reasons=_array(_text(), 32), accuracy_claim={"const": False},
        statistical_calibration={"const": "not_established"})
    score["required"] = list(score["properties"])
    counts = _object(**dict.fromkeys(("expected", "started", "observed", "completely_adjudicated", "missing",
        "unobserved", "incomplete"), _count(MAX_ORIGINALS)))
    unit = _object(unit_id=_reference(), applicable=boolean,
        verdict=_enum(("pass", "fail", "unknown", "not_applicable")), observed=boolean,
        unavailable_reasons=_array(_text(), 32))
    unit_counts = _object(**dict.fromkeys(("expected", "applicable", "observed", "passed", "failed", "unknown",
        "not_applicable"), _count(MAX_UNITS)))
    original = _object(original_id=_reference(), original_text_sha256=hashed, question_ids=_array(_reference(), MAX_UNITS),
        outcome=_reference(), started=boolean, observed=boolean, current_assessments=_count(MAX_ASSESSMENTS),
        historical_assessments=_count(MAX_ASSESSMENTS), complete_first_passes=boolean,
        incomplete_role_triples=_count(MAX_ASSESSMENTS), missing_roles=_array(_enum(ROLES), 3),
        completely_adjudicated=boolean, dimension_counts=_object(**dict.fromkeys(_dimensions(), unit_counts)),
        units=_object(**{dimension: _array(unit, MAX_UNITS) for dimension in _dimensions()}))
    configuration = _object(configuration_id=hashed, role=_enum((*ROLES, "adjudicator")),
        profile_ref=_reference(), controller_ref=_reference(), model_ref=_reference(), prompt_version=_reference(),
        qualification_ref=_nullable(_reference()), judgment_type=_enum(("agent_judgment", "agent_adjudicated")),
        current_observations=_count(MAX_ASSESSMENTS * 4), historical_observations=_count(MAX_ASSESSMENTS * 4),
        observation_refs=_array(hashed, MAX_ASSESSMENTS * 4))
    assessment = _assessment_schema()
    assessment["properties"].update(first_pass_judgment_type={"const": "agent_judgment"},
        adjudication_judgment_type=_enum(("agent_adjudicated", "unknown")),
        scorable=boolean, complete_first_passes=boolean, missing_roles=_array(_enum(ROLES), 3),
        unavailable_reasons=_array(_text(), 16))
    assessment["required"] = list(assessment["properties"])
    shape = _object(schema_version={"const": REPORT}, run_id=_reference(), binding_id=hashed, policy_id=hashed,
        profile_sha256=_input_schema()["properties"]["profile_sha256"], snapshot_id=hashed,
        qualification=_qualification_schema(), counts=counts,
        dimensions=_object(**dict.fromkeys(_dimensions(), score)), originals=_array(original, MAX_ORIGINALS),
        judge_configurations=_array(configuration, MAX_ASSESSMENTS * 4),
        assessments=_array(assessment, MAX_PAGE),
        paging=_object(after_index=_count(MAX_ASSESSMENTS),
            max_records={"type": "integer", "minimum": 1, "maximum": MAX_PAGE},
            returned=_count(MAX_PAGE), total=_count(MAX_ASSESSMENTS), next_index=_nullable(_count(MAX_ASSESSMENTS)),
            truncated=boolean, has_more=boolean, snapshot_id=hashed),
        limitations=_array(_text(), 8), accuracy_claim={"const": False}, authorized={"const": False},
        evidence_accepted={"const": False})
    return {REPORT: {"$schema": "https://json-schema.org/draft/2020-12/schema", **shape}}


def schema_document(name=REPORT):
    if type(name) is not str or name != REPORT:
        refuse("version_unsupported")
    return copy.deepcopy(schemas()[name])


def _plain(value):
    try:
        value = detached(value)
        if len(canonical(value)) > MAX_BYTES:
            refuse("limit")
        return value
    except ExecutionError:
        raise
    except (Exception, SystemExit):
        refuse("invalid_plain_data")


def _matches(value, shape):
    try:
        _shape(value, shape)
    except (Exception, SystemExit):
        refuse("invalid_shape")


def _row_metrics(rows):
    counts = Counter(row["verdict"] for row in rows)
    passed, failed, unknown = (counts[name] for name in ("pass", "fail", "unknown"))
    applicable = passed + failed + unknown
    return {"expected": len(rows), "applicable": applicable, "passed": passed, "failed": failed,
        "unknown": unknown, "not_applicable": counts["not_applicable"], "numerator": passed,
        "denominator": applicable, "score": passed / applicable if applicable and not unknown else None,
        "uncertainty_bounds": {"lower": passed / applicable if applicable else None,
            "upper": (passed + unknown) / applicable if applicable else None}}


def _validated(value):
    value = _plain(value)
    _matches(value, _input_schema())
    originals, observations, assessment_ids = {}, set(), set()
    for original in value["originals"]:
        identifier = original["original_id"]
        if identifier in originals or len(set(original["question_ids"])) != len(original["question_ids"]):
            refuse("duplicate_identity")
        for rows in original["units"].values():
            if len({row["unit_id"] for row in rows}) != len(rows):
                refuse("duplicate_identity")
        originals[identifier] = original
    started = value["started_original_ids"]
    if started is not None:
        if len(set(started)) != len(started):
            refuse("duplicate_start_original")
        if set(started) - originals.keys():
            refuse("unknown_start_original")
    for assessment in value["assessments"]:
        identifier = assessment["assessment_id"]
        if identifier in assessment_ids:
            refuse("duplicate_identity")
        assessment_ids.add(identifier)
        original = originals.get(assessment["original_id"])
        if original is None:
            refuse("unknown_original")
        passes = assessment["first_passes"]
        if len({row["role"] for row in passes}) != len(passes):
            refuse("duplicate_role")
        for observed in (*passes, *((assessment["adjudication"],) if assessment["adjudication"] is not None else ())):
            if observed["observation_ref"] in observations:
                refuse("duplicate_observation")
            observations.add(observed["observation_ref"])
            for dimension in _dimensions():
                rows = observed["checks"][dimension]
                identifiers = [row["unit_id"] for row in rows]
                if len(set(identifiers)) != len(identifiers):
                    refuse("duplicate_unit")
                expected = {row["unit_id"] for row in original["units"][dimension]}
                if assessment["current"] and set(identifiers) - expected:
                    refuse("unknown_unit")
                for row in rows:
                    for anchor in row["anchors"]:
                        if anchor["end"] <= anchor["start"] or anchor["end"] - anchor["start"] != len(anchor["quote"]):
                            refuse("invalid_anchor")
                if observed["metrics"][dimension] != _row_metrics(rows):
                    refuse("metric_conflict")
    profile_ids = [(row["role"], row["profile_ref"]) for row in value["qualification"]["profiles"]]
    if len(set(profile_ids)) != len(profile_ids):
        refuse("duplicate_profile")
    return value


def _complete(assessment):
    return {row["role"] for row in assessment["first_passes"]} == set(ROLES)


def _qualification_reasons(assessment, qualification):
    observations = [(row, row["role"]) for row in assessment["first_passes"]]
    if assessment["adjudication"] is not None:
        observations.append((assessment["adjudication"], "adjudicator"))
    role_map = {"evidence": "evaluator", "scope_units": "evaluator", "counterevidence": "critic",
        "adjudicator": "adjudicator"}
    binding_fields = ("profile_ref", "controller_ref", "model_ref", "prompt_version", "qualification_ref")
    profiles = {(row["role"], row["profile_ref"]): row for row in qualification["profiles"]}
    reasons = []
    for observed, role in observations:
        profile = profiles.get((role_map[role], observed["profile_ref"]))
        if observed["qualification_ref"] is None:
            reasons.append(role + "_qualification_missing")
        elif profile is None:
            reasons.append(role + "_qualification_profile_missing")
        elif profile["qualification_ref"] is None:
            reasons.append(role + "_qualification_missing")
        elif any(observed[field] != profile[field] for field in binding_fields):
            reasons.append(role + "_qualification_binding_mismatch")
        elif profile["state"] != "qualified":
            reasons.append(role + "_qualification_not_current")
    if len({row["controller_ref"] for row, _ in observations}) != len(observations):
        reasons.append("judge_controllers_not_distinct")
    if len({row["profile_ref"] for row, _ in observations}) != len(observations):
        reasons.append("judge_configurations_not_distinct")
    return reasons


def _unavailable(assessment, qualification):
    reasons = []
    if not assessment["current"]:
        reasons.append("historical_assessment")
    if not _complete(assessment):
        reasons.append("incomplete_first_passes")
    if assessment["adjudication"] is None:
        reasons.append("missing_adjudication")
    if qualification["state"] != "qualified":
        reasons.append("qualification_not_currently_qualified")
    if assessment["current"]:
        reasons.extend(_qualification_reasons(assessment, qualification))
    return reasons


def _addresses(assessment, dimension, unit_id):
    if dimension not in ("support", "scoped_correctness") or assessment["claim_id"] == unit_id:
        return True
    observations = (*assessment["first_passes"], *((assessment["adjudication"],)
        if assessment["adjudication"] is not None else ()))
    return any(row["unit_id"] == unit_id for observed in observations for row in observed["checks"][dimension])


def _unit_summary(unit, dimension, current, qualification):
    addressed = [assessment for assessment in current if _addresses(assessment, dimension, unit["unit_id"])]
    observations = []
    reasons = set()
    for assessment in addressed:
        adjudication = assessment["adjudication"]
        rows = [] if adjudication is None else [row for row in adjudication["checks"][dimension]
            if row["unit_id"] == unit["unit_id"]]
        observations.extend(rows)
        reasons.update(_unavailable(assessment, qualification))
        if not rows:
            reasons.add("missing_unit_observation")
    if not unit["applicable"]:
        verdict, reasons = "not_applicable", set()
    else:
        if not addressed:
            reasons.add("missing_current_assessment")
        verdicts = {row["verdict"] for row in observations}
        if "not_applicable" in verdicts:
            reasons.add("applicability_conflict")
        if "unknown" in verdicts:
            reasons.add("unresolved_observation")
        if "pass" in verdicts and "fail" in verdicts:
            reasons.add("conflicting_observations")
        verdict = next(iter(verdicts)) if not reasons and verdicts in ({"pass"}, {"fail"}) else "unknown"
        if verdict == "unknown" and not reasons:
            reasons.add("missing_unit_observation")
    return {**unit, "verdict": verdict, "observed": bool(observations), "unavailable_reasons": sorted(reasons)}


def _unit_counts(rows):
    metrics = _row_metrics(rows)
    return {key: metrics[key] for key in ("expected", "applicable", "passed", "failed", "unknown", "not_applicable")} | {
        "observed": sum(row["applicable"] and row["observed"] for row in rows)}


def _original_summary(original, assessments, qualification, started_original_ids):
    current = [assessment for assessment in assessments if assessment["current"]]
    units = {dimension: [_unit_summary(unit, dimension, current, qualification)
        for unit in original["units"][dimension]] for dimension in _dimensions()}
    complete_roles = any(_complete(assessment) for assessment in current)
    known = all(row["verdict"] != "unknown" for rows in units.values() for row in rows if row["applicable"])
    eligible = any(not _unavailable(assessment, qualification) for assessment in current)
    missing = {role for assessment in current for role in ROLES if role not in {
        observed["role"] for observed in assessment["first_passes"]}} if current else set(ROLES)
    return {key: original[key] for key in ("original_id", "original_text_sha256", "question_ids", "outcome")} | {
        "started": original["original_id"] in started_original_ids or any(
            assessment["first_passes"] for assessment in assessments),
        "observed": any(assessment["adjudication"] is not None for assessment in assessments),
        "current_assessments": len(current), "historical_assessments": len(assessments) - len(current),
        "complete_first_passes": complete_roles,
        "incomplete_role_triples": sum(not _complete(assessment) for assessment in current),
        "missing_roles": [role for role in ROLES if role in missing],
        "completely_adjudicated": bool(eligible and complete_roles and known),
        "dimension_counts": {dimension: _unit_counts(rows) for dimension, rows in units.items()}, "units": units}


def _configuration(observed, role):
    result = {key: observed[key] for key in ("profile_ref", "controller_ref", "model_ref", "prompt_version",
        "qualification_ref")}
    result["role"] = role
    return {"configuration_id": digest(result), **result}


def _configurations(assessments):
    configurations, dimensions = {}, {dimension: set() for dimension in _dimensions()}
    for assessment in assessments:
        observations = [(row, row["role"]) for row in assessment["first_passes"]]
        if assessment["adjudication"] is not None:
            observations.append((assessment["adjudication"], "adjudicator"))
        for observed, role in observations:
            metadata = _configuration(observed, role)
            identifier = metadata["configuration_id"]
            row = configurations.setdefault(identifier, {**metadata,
                "judgment_type": "agent_adjudicated" if role == "adjudicator" else "agent_judgment",
                "current_observations": 0, "historical_observations": 0, "observation_refs": []})
            row["current_observations" if assessment["current"] else "historical_observations"] += 1
            row["observation_refs"].append(observed["observation_ref"])
            for dimension in _dimensions():
                if observed["checks"][dimension]:
                    dimensions[dimension].add(identifier)
    return [configurations[key] for key in sorted(configurations)], dimensions


def _reference_strata(assessments, dimension):
    strata = {tier: {"tier": tier, "assessments": 0, "current_assessments": 0, "reported_units": 0,
        "labels": dict.fromkeys(("accept", "reject", "unknown", "unavailable"), 0)} for tier in REFERENCE_TIERS}
    for assessment in assessments:
        reference = assessment["reference"]
        row = strata[reference["tier"]]
        row["assessments"] += 1
        row["current_assessments"] += int(assessment["current"])
        label = reference["label"] if reference["label_ref"] is not None and reference["label"] is not None else "unavailable"
        row["labels"][label] += 1
        if assessment["current"] and assessment["adjudication"] is not None:
            row["reported_units"] += len(assessment["adjudication"]["checks"][dimension])
    return list(strata.values())


def _dimension_summary(dimension, originals, assessments, configurations, missing_original,
        missing_first_passes, qualified):
    rows = [row for original in originals for row in original["units"][dimension]]
    metrics = _row_metrics(rows)
    reasons = {reason for row in rows if row["applicable"] for reason in row["unavailable_reasons"]}
    if not metrics["applicable"]:
        reasons.add("zero_applicable_denominator")
    if missing_original:
        reasons.add("missing_original_start")
    if missing_first_passes:
        reasons.add("missing_original_first_passes")
    if not qualified:
        reasons.add("qualification_not_currently_qualified")
    if reasons:
        metrics["score"] = None
    uncertainty = Counter(row["uncertainty"] for assessment in assessments
        if assessment["current"] and assessment["adjudication"] is not None
        for row in assessment["adjudication"]["checks"][dimension])
    return {**metrics, "observed": sum(row["applicable"] and row["observed"] for row in rows),
        "status": "available" if metrics["score"] is not None else "unavailable",
        "judgment_type": "agent_adjudicated" if metrics["score"] is not None else "unknown",
        "reference_strata": _reference_strata(assessments, dimension),
        "judge_configuration_refs": sorted(configurations[dimension]),
        "reported_uncertainty": {"basis": "self_report", "scope": "current_adjudication_observations",
            "counts": {name: uncertainty[name] for name in UNCERTAINTIES}},
        "unavailable_reasons": sorted(reasons), "accuracy_claim": False, "statistical_calibration": "not_established"}


def _assessment_summary(assessment, qualification):
    unavailable = _unavailable(assessment, qualification)
    return {**assessment, "first_pass_judgment_type": "agent_judgment",
        "adjudication_judgment_type": "unknown" if unavailable else "agent_adjudicated",
        "scorable": not unavailable, "complete_first_passes": _complete(assessment),
        "missing_roles": [role for role in ROLES if role not in {row["role"] for row in assessment["first_passes"]}],
        "unavailable_reasons": unavailable}


def report(*, run_id, binding_id, policy_id, profile_sha256, originals, assessments, qualification,
        started_original_ids=None, after_index=0, max_records=MAX_PAGE, expected_snapshot_id=None):
    """Project owner inputs; explicit observed starts supplement retained first passes."""
    if type(after_index) is not int or not 0 <= after_index <= MAX_ASSESSMENTS:
        refuse("cursor_invalid")
    if type(max_records) is not int or not 1 <= max_records <= MAX_PAGE:
        refuse("page_limit")
    if expected_snapshot_id is not None:
        _matches(expected_snapshot_id, _hashed())
    if after_index and expected_snapshot_id is None:
        refuse("snapshot_required")
    value = _validated({"schema_version": REPORT, "run_id": run_id, "binding_id": binding_id,
        "policy_id": policy_id, "profile_sha256": profile_sha256, "originals": originals,
        "assessments": assessments, "qualification": qualification, "started_original_ids": started_original_ids})
    snapshot = digest(value)
    if expected_snapshot_id is not None and expected_snapshot_id != snapshot:
        refuse("snapshot_conflict")
    if after_index > len(value["assessments"]):
        refuse("cursor_invalid")
    qualified = value["qualification"]["state"] == "qualified"
    grouped = {original["original_id"]: [] for original in value["originals"]}
    for assessment in value["assessments"]:
        grouped[assessment["original_id"]].append(assessment)
    started_ids = set(value["started_original_ids"] or ())
    missing_first_passes = any(not any(assessment["first_passes"] for assessment in rows)
        for rows in grouped.values())
    summaries = [_original_summary(original, grouped[original["original_id"]], value["qualification"], started_ids)
        for original in value["originals"]]
    counts = {"expected": len(summaries), "started": sum(row["started"] for row in summaries),
        "observed": sum(row["observed"] for row in summaries),
        "completely_adjudicated": sum(row["completely_adjudicated"] for row in summaries)}
    counts.update(missing=counts["expected"] - counts["started"],
        unobserved=counts["expected"] - counts["observed"],
        incomplete=counts["expected"] - counts["completely_adjudicated"])
    configurations, references = _configurations(value["assessments"])
    page = value["assessments"][after_index:after_index + max_records]
    next_index = after_index + len(page)
    has_more = next_index < len(value["assessments"])
    result = {key: value[key] for key in ("schema_version", "run_id", "binding_id", "policy_id", "profile_sha256",
        "qualification")} | {"snapshot_id": snapshot, "counts": counts,
        "dimensions": {dimension: _dimension_summary(dimension, summaries, value["assessments"], references,
            bool(counts["missing"]), missing_first_passes, qualified) for dimension in _dimensions()}, "originals": summaries,
        "judge_configurations": configurations,
        "assessments": [_assessment_summary(row, value["qualification"]) for row in page],
        "paging": {"after_index": after_index, "max_records": max_records, "returned": len(page),
            "total": len(value["assessments"]), "next_index": next_index if has_more else None,
            "truncated": after_index > 0 or has_more, "has_more": has_more, "snapshot_id": snapshot},
        "limitations": list(_LIMITATIONS), "accuracy_claim": False, "authorized": False, "evidence_accepted": False}
    result = _plain(result)
    _matches(result, schema_document())
    return result
