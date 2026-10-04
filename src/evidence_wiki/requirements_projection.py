"""Pure append-only requirements proposals, frozen scope and complete accounting.

The native owner authenticates the host envelope and commits existing intake and
coverage effects. These codecs grant no authority and perform no prose scope
classification. Only exact predeclared question/facet combinations are supported.
"""

from __future__ import annotations

from .agent_execution import ExecutionError, _integer, _ref
from .mutation_contracts import canonical, detached, digest
from .planning_contracts import REQUEST, decode, normalize
from .planning_questions import intent

BASE = "evidence-research-requirements/v1"
DOCUMENT = "evidence-research-requirements/v2"
PROPOSAL = "research-requirements-proposal/v1"
ENVELOPE = "research-requirements-authorization/v1"
DECISION = "research-requirements-decision/v1"
VIEW = "research-requirements-view/v1"


def fail(reason):
    raise ExecutionError("requirements_" + reason)


def _text(value):
    if type(value) is not str or not value.strip() or len(value.encode()) > 16384:
        fail("text_invalid")
    return value


def _base(value):
    value = detached(value)
    if type(value) is not dict or set(value) != {"schema_version", "request", "decisions"} or value["schema_version"] != BASE:
        fail("base_invalid")
    try:
        checked = decode(canonical({**value, "schema_version": REQUEST}))
        normalized, _ = normalize(checked)
        blockers = []
        intent(normalized, blockers)
        if any(row["reason"] == "question_budget_or_strict_bundle_limit" for row in blockers):
            fail("question_bound")
    except (Exception, SystemExit):
        fail("native_contract_invalid")
    request = value["request"]
    if type(request) is not dict or request.get("kind") != "research_request" or request.get("schema_version") not in {"1.0", "2.0"}:
        fail("base_invalid")
    payload = request["payload"]
    originals, derived = payload["questions"], payload["derived_questions"]
    if type(originals) is not list or not 1 <= len(originals) <= 100 or type(derived) is not list or len(derived) > 200:
        fail("question_bound")
    ids, original_ids = set(), set()
    for row in originals:
        if type(row) is not dict or set(row) != {"id", "text"}:
            fail("question_invalid")
        identifier = _ref(row["id"])
        _text(row["text"])
        if identifier in ids:
            fail("question_duplicate")
        ids.add(identifier)
        original_ids.add(identifier)
    for row in derived:
        if type(row) is not dict or set(row) != {"id", "text", "original_ids"}:
            fail("question_invalid")
        identifier = _ref(row["id"])
        _text(row["text"])
        if identifier in ids or type(row["original_ids"]) is not list or not row["original_ids"] or len(row["original_ids"]) > 100:
            fail("question_invalid")
        refs = [_ref(item) for item in row["original_ids"]]
        if len(set(refs)) != len(refs) or not set(refs) <= original_ids:
            fail("original_mapping_invalid")
        ids.add(identifier)
    return value


def criteria_identity(base):
    base = _base(base)
    payload, decisions = base["request"]["payload"], detached(base["decisions"])
    originals = {row["id"] for row in payload["questions"]}
    decisions["question_plans"] = [row for row in decisions["question_plans"] if row["question_id"] in originals]
    return digest({"payload": {key: value for key, value in payload.items() if key != "derived_questions"},
                   "decisions": decisions})


def inherited_plan(base, addition):
    """Copy exactly selected original facet contracts; unresolved templates fail closed."""
    plans = {row["question_id"]: row for row in base["decisions"]["question_plans"]}
    facets, criteria = {}, {}
    for reference in addition["facet_refs"]:
        plan = plans.get(reference["original_id"])
        if plan is None or plan["template"] is not None:
            fail("inherited_template_snapshot_unsupported")
        fid = reference["facet_id"]
        selected = [row for row in plan["facets"] if row["facet_id"] == fid]
        checks = [row for row in plan["criteria"] if row["facet_id"] == fid]
        if len(selected) != 1 or len(checks) != 1:
            fail("inherited_facet_missing")
        if fid in facets and (facets[fid] != selected[0] or criteria[fid] != checks[0]):
            fail("inherited_facet_ambiguous")
        facets[fid], criteria[fid] = detached(selected[0]), detached(checks[0])
    if not any(row.get("required", True) for row in facets.values()):
        fail("required_facet_missing")
    return {"question_id": addition["id"], "template": None,
            "facets": list(facets.values()), "criteria": list(criteria.values())}


def _unchanged_base(base, effective):
    projected = detached(effective)
    initial = base["request"]["payload"]["derived_questions"]
    projected["request"]["payload"]["derived_questions"] = projected["request"]["payload"]["derived_questions"][:len(initial)]
    count = len(base["decisions"]["question_plans"])
    projected["decisions"]["question_plans"] = projected["decisions"]["question_plans"][:count]
    return projected == base


def _facets(base):
    originals = {row["id"] for row in base["request"]["payload"]["questions"]}
    result = set()
    for plan in base["decisions"]["question_plans"]:
        if plan["question_id"] in originals:
            for criterion in plan["criteria"]:
                result.add((plan["question_id"], criterion["facet_id"]))
    return result


def _addition(value, originals, facets):
    if type(value) is not dict or set(value) != {"id", "text", "original_ids", "facet_refs"}:
        fail("addition_invalid")
    value = detached(value)
    _ref(value["id"])
    _text(value["text"])
    if type(value["original_ids"]) is not list or not 1 <= len(value["original_ids"]) <= 100:
        fail("addition_invalid")
    refs = [_ref(item) for item in value["original_ids"]]
    if len(set(refs)) != len(refs) or not set(refs) <= originals:
        fail("original_mapping_invalid")
    if type(value["facet_refs"]) is not list or not 1 <= len(value["facet_refs"]) <= 32:
        fail("facet_invalid")
    pairs = []
    for item in value["facet_refs"]:
        if type(item) is not dict or set(item) != {"original_id", "facet_id"}:
            fail("facet_invalid")
        pair = (_ref(item["original_id"]), _ref(item["facet_id"]))
        if pair not in facets or pair[0] not in refs or pair in pairs:
            fail("facet_invalid")
        pairs.append(pair)
    if {pair[0] for pair in pairs} != set(refs):
        fail("facet_original_coverage_missing")
    value["original_ids"] = sorted(refs)
    value["facet_refs"] = [{"original_id": original, "facet_id": facet} for original, facet in sorted(pairs)]
    return value



def _prior_additions_unchanged(base, effective, envelope):
    initial = len(base["request"]["payload"]["derived_questions"])
    rows = effective["request"]["payload"]["derived_questions"][initial:]
    expected = []
    for row in rows:
        candidates = [allowed for allowed in envelope["allowed_questions"]
                      if {key: allowed.get(key) for key in ("text", "original_ids")} == {key: row[key] for key in ("text", "original_ids")}]
        if len(candidates) != 1:
            return False
        candidate = _addition({**row, "facet_refs": candidates[0]["facet_refs"]}, set(envelope["allowed_original_ids"]), _facets(base))
        expected.append(inherited_plan(base, candidate))
    return effective["decisions"]["question_plans"][len(base["decisions"]["question_plans"]):] == expected

def revision_decision(base, effective, proposal, envelope):
    """Bind a proposal to an authenticated host/native envelope supplied by the owner.

    The returned decision is inert. Authentication, idle/currentness checks and
    real staged commit remain mandatory outside this pure codec.
    """
    base, effective = _base(base), _base(effective)
    proposal, envelope = detached(proposal), detached(envelope)
    parent, criteria = digest(effective), criteria_identity(base)
    common = {"schema_version": DECISION, "parent_sha256": parent, "criteria_sha256": criteria,
        "proposal_sha256": digest(proposal), "envelope_sha256": digest(envelope),
        "outcome": "rejected", "reason": "invalid_revision", "additions": [],
        "invalidates_eligibility": False, "authorized": False, "evidence_accepted": False}
    if not _unchanged_base(base, effective):
        return {**common, "reason": "original_or_criteria_changed"}
    initial_derived = base["request"]["payload"]["derived_questions"]
    if effective["request"]["payload"]["derived_questions"][:len(initial_derived)] != initial_derived:
        return {**common, "reason": "derived_prefix_changed"}
    if (type(proposal) is not dict or set(proposal) != {"schema_version", "request_id", "parent_sha256", "additions"}
            or proposal["schema_version"] != PROPOSAL or proposal["parent_sha256"] != parent
            or type(envelope) is not dict or set(envelope) != {"schema_version", "policy_ref", "base_sha256", "criteria_sha256",
                "allowed_original_ids", "allowed_questions", "max_additions", "max_total_questions"}
            or envelope["schema_version"] != ENVELOPE or envelope["base_sha256"] != digest(base)
            or envelope["criteria_sha256"] != criteria):
        return common
    _ref(proposal["request_id"])
    _ref(envelope["policy_ref"])
    _integer(envelope["max_additions"], 200)
    _integer(envelope["max_total_questions"], 300)
    originals = {row["id"] for row in base["request"]["payload"]["questions"]}
    if (type(envelope["allowed_original_ids"]) is not list or len(envelope["allowed_original_ids"]) > 100
            or len(set(envelope["allowed_original_ids"])) != len(envelope["allowed_original_ids"])
            or not set(envelope["allowed_original_ids"]) <= originals):
        return common
    if type(envelope["allowed_questions"]) is not list or len(envelope["allowed_questions"]) > 200:
        return common
    allowed_seen = set()
    for allowed in envelope["allowed_questions"]:
        if type(allowed) is not dict or set(allowed) != {"text", "original_ids", "facet_refs"}:
            fail("authorization_question_invalid")
        normalized = _addition({"id": "authorized-question", **allowed}, set(envelope["allowed_original_ids"]), _facets(base))
        fit = {key: normalized[key] for key in ("text", "original_ids", "facet_refs")}
        if fit != allowed or digest(fit) in allowed_seen:
            fail("authorization_question_invalid")
        allowed_seen.add(digest(fit))
    if not _prior_additions_unchanged(base, effective, envelope):
        return {**common, "reason": "prior_derived_contract_changed"}
    if type(proposal["additions"]) is not list or not 1 <= len(proposal["additions"]) <= envelope["max_additions"]:
        return {**common, "reason": "addition_allowance_exhausted"}
    current = effective["request"]["payload"]
    if len(current["derived_questions"]) - len(initial_derived) + len(proposal["additions"]) > envelope["max_additions"]:
        return {**common, "reason": "addition_allowance_exhausted"}
    maximum = min(envelope["max_total_questions"], current["budgets"]["questions"], 100 if effective["decisions"].get("mode") == "strict" else 300)
    if len(current["questions"]) + len(current["derived_questions"]) + len(proposal["additions"]) > maximum:
        return {**common, "reason": "question_allowance_exhausted"}
    existing = {row["id"] for row in [*current["questions"], *current["derived_questions"]]}
    text_seen = {row["text"].strip().casefold() for row in [*current["questions"], *current["derived_questions"]]}
    additions, facets = [], _facets(base)
    for supplied in proposal["additions"]:
        row = _addition(supplied, set(envelope["allowed_original_ids"]), facets)
        fit = {key: row[key] for key in ("text", "original_ids", "facet_refs")}
        if fit not in envelope["allowed_questions"]:
            return {**common, "reason": "scope_fit_not_authorized"}
        if row["id"] in existing or row["text"].strip().casefold() in text_seen:
            return {**common, "reason": "duplicate_question"}
        existing.add(row["id"])
        text_seen.add(row["text"].strip().casefold())
        inherited_plan(base, row)
        additions.append(row)
    return {**common, "outcome": "append", "reason": "bounded_declared_scope", "additions": additions,
            "invalidates_eligibility": True}


def append_projection(base, effective, decision):
    """Construct expected bytes only; the native owner authenticates/replays decision."""
    base, effective, decision = _base(base), _base(effective), detached(decision)
    if (decision.get("schema_version") != DECISION or decision.get("outcome") != "append"
            or decision.get("parent_sha256") != digest(effective) or decision.get("criteria_sha256") != criteria_identity(base)):
        fail("decision_mismatch")
    result = detached(effective)
    result["request"]["payload"]["derived_questions"].extend(
        {key: row[key] for key in ("id", "text", "original_ids")} for row in decision["additions"])
    result["decisions"]["question_plans"].extend(inherited_plan(base, row) for row in decision["additions"])
    return _base(result)


def validated_lineage(document):
    """Replay a versioned bounded data chain; receipt references require owner verification."""
    document = detached(document)
    if document.get("schema_version") == BASE:
        base = _base(document)
        return {"base": base, "effective": base, "revisions": [], "owner_authentication": "not_inferred"}
    if (type(document) is not dict or set(document) != {"schema_version", "base", "revisions"}
            or document["schema_version"] != DOCUMENT or type(document["revisions"]) is not list or not 1 <= len(document["revisions"]) <= 64):
        fail("lineage_invalid")
    base, revisions = _base(document["base"]), []
    effective, ids, envelope_ref = base, set(), None
    for row in document["revisions"]:
        if type(row) is not dict or set(row) != {"revision_id", "proposal", "envelope", "decision", "owner_receipt_ref"}:
            fail("lineage_invalid")
        identifier = _ref(row["revision_id"])
        _ref(row["owner_receipt_ref"])
        if identifier in ids:
            fail("lineage_duplicate")
        ids.add(identifier)
        actual_envelope = digest(row["envelope"])
        if envelope_ref is not None and actual_envelope != envelope_ref:
            fail("frozen_authorization_changed")
        envelope_ref = actual_envelope
        expected = revision_decision(base, effective, row["proposal"], row["envelope"])
        if expected != row["decision"] or expected["outcome"] != "append":
            fail("lineage_decision_changed")
        effective = append_projection(base, effective, expected)
        revisions.append(row)
    return {"base": base, "effective": effective, "revisions": revisions, "owner_authentication": "not_inferred"}


def requirements_view(effective, mapping, outcomes):
    """Expose every original with finite dispositions; missing coverage stays visible."""
    effective, mapping, outcomes = _base(effective), detached(mapping), detached(outcomes)
    payload, decisions = effective["request"]["payload"], effective["decisions"]
    by_original = {row["original_id"]: row for row in outcomes}
    if len(by_original) != len(outcomes) or set(by_original) - {row["id"] for row in payload["questions"]}:
        fail("accounting_invalid")
    originals = []
    mappings = {row["id"]: row for row in mapping.get("originals", [])}
    if len(mappings) != len(mapping.get("originals", [])):
        fail("accounting_invalid")
    for original in payload["questions"]:
        wanted = [original["id"], *[row["id"] for row in payload["derived_questions"] if original["id"] in row["original_ids"]]]
        observed = by_original.get(original["id"], {})
        if observed and observed.get("original_text") != original["text"]:
            fail("original_text_changed")
        rows = observed.get("questions", [])
        by_question = {row["question_id"]: row for row in rows}
        if len(by_question) != len(rows) or set(by_question) - set(wanted):
            fail("accounting_invalid")
        missing = sorted(set(wanted) - set(by_question))
        mapped = mappings.get(original["id"])
        relevant_gaps = []
        for gap in mapping.get("gaps", []):
            if type(gap) is not str:
                fail("accounting_invalid")
            if gap.startswith("original_or_derived_question_missing:"):
                relevant = gap.partition(":")[2] in wanted
            elif gap.startswith("question_mapping_or_text_changed:"):
                native_slugs = {mapping.get("expected", {}).get(qid) for qid in wanted}
                relevant = gap.partition(":")[2] in native_slugs or not mapping.get("expected")
            else:
                relevant = True
            if relevant:
                relevant_gaps.append(gap)
        mapping_current = bool(mapped and mapped.get("text") == original["text"] and mapped.get("question_ids") == wanted and not relevant_gaps)
        accepted = sum(row.get("accepted") is True for row in rows)
        disposition = "blocked" if not mapping_current else "accepted" if accepted == len(wanted) and not missing else "partial" if accepted else (
            "abstained" if not missing and rows and all(row.get("terminal_disposition") == "abstained" for row in rows) else "blocked")
        originals.append({"original_id": original["id"], "original_text": original["text"],
            "question_ids": wanted, "missing_question_ids": missing, "disposition": disposition,
            "reason": "current_acceptance" if disposition == "accepted" else "terminal_abstention" if disposition == "abstained"
                else "mapping_incomplete" if not mapping_current else "unmet_or_pending_obligations"})
    return {"schema_version": VIEW, "requirements_sha256": digest(effective), "criteria_sha256": criteria_identity(effective),
        "originals": originals, "mapping": mapping, "accounting_complete": not bool(mapping.get("gaps")), "question_plans": decisions["question_plans"],
        "exclusions": decisions.get("exclusions"), "as_of": decisions.get("as_of"),
        "stopping_rules": [{"question_id": plan["question_id"], "facet_id": criterion["facet_id"],
            "stopping": criterion["stopping"]} for plan in decisions["question_plans"] for criterion in plan["criteria"]],
        "temporal_criteria": [{"question_id": plan["question_id"], "facet_id": criterion["facet_id"],
            "time": criterion["time"]} for plan in decisions["question_plans"] for criterion in plan["criteria"]],
        "declared_scope": payload["scope"], "search_completeness": "bounded_declared_scope",
        "authorized": False, "evidence_accepted": False}
