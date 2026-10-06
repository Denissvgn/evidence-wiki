"""Canonical journal projection and scoped repair/disagreement bridge.

The protected execution owner authenticates the journal before this adapter
projects it. No model or host callback runs here. Proposals remain inert and all
actual effects, accounting, signing and eligibility belong to existing owners.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from .agent_execution import ExecutionError, validate_observation
from .disagreement import CRITERIA, _hash, _ref, disagreement_action
from .mutation_contracts import canonical, detached, digest
from .review_context import create_review_context

PROJECTION = "research-disagreement-projection/v1"
ATTEMPT = "research-disagreement-attempt/v1"
FINDINGS = "research-review-findings/v1"
_ATTEMPT_FIELDS = frozenset({"schema_version", "claim_id", "material_basis_id", "defect_sha256", "action", "cycle_ref"})
_SCIENCE = frozenset({"title", "authors", "url", "published_at", "source_type"})


def _distinct(values):
    return [value for _, value in sorted({canonical(value): value for value in values}.items())]


def _fail(reason):
    raise ExecutionError("disagreement_" + reason)


def _content(value):
    if type(value) is not str or len(value.encode("utf-8")) > 65536:
        _fail("source_invalid")
    try:
        parsed = json.loads(value)
    except ValueError:
        return value.replace("\r\n", "\n").strip()
    if (type(parsed) is dict and set(parsed) == {"schema_version", "facts"}
            and parsed["schema_version"] == "finite-structured-observation/v1"
            and type(parsed["facts"]) is dict and type(parsed["facts"].get("values")) is list):
        parsed = detached(parsed)
        try:
            parsed["facts"]["values"] = [str(int(item)) for item in parsed["facts"]["values"]]
        except (ValueError, TypeError):
            return value.replace("\r\n", "\n").strip()
        return parsed
    return value.replace("\r\n", "\n").strip()


def material_basis(owner_snapshot, rubric, sources):
    """Bind relevant content, excluding identity/status and transport bookkeeping.

    This is a change detector, not a semantic adequacy or source-authentication
    proof. Fresh canonical checks and first passes remain required after changes.
    """
    snapshot, rubric, sources = detached(owner_snapshot), detached(rubric), detached(sources)
    if (type(snapshot) is not dict or type(rubric) is not dict or type(rubric.get("criteria")) is not dict
            or set(rubric["criteria"]) != CRITERIA or type(sources) is not list or len(sources) > 64):
        _fail("material_unsupported")
    source_material = {}
    for source in sources:
        if type(source) is not dict or type(source.get("metadata")) is not dict:
            _fail("source_invalid")
        identifier = _ref(source["source_id"])
        if identifier in source_material:
            _fail("source_duplicate")
        source_material[identifier] = {"content": _content(source["content"]),
            "metadata": {key: source["metadata"][key] for key in sorted(_SCIENCE.intersection(source["metadata"]))}}
    computation = snapshot.get("computation")
    calculated = None
    if computation is not None:
        from .repair_calculation_material import calculation_material
        calculated = calculation_material(computation, source_material, sources)
    def claim_material(claim):
        if type(claim) is not dict:
            _fail("material_unsupported")
        result = {key: claim[key] for key in ("text", "scope", "time", "units", "derivation", "limitations")}
        if computation is not None:
            from .repair_calculation_material import reference_material
            result["calculations"] = reference_material(claim.get("calculations", []), computation)
        elif claim.get("calculations"):
            _fail("calculation_context_missing")
        result["kind"] = "inference" if claim["qualification"] == "inference" else "assertion"
        evidence = claim["evidence"]
        if type(evidence) is not list or len(evidence) > 16:
            _fail("material_unsupported")
        result["evidence"] = []
        for item in evidence:
            source = source_material.get(item["source_id"])
            if source is None:
                _fail("source_missing")
            result["evidence"].append({"source_material": digest(source),
                **{key: item[key] for key in ("capture", "quote", "location_hint", "anchor")}})
        result["evidence"] = _distinct(result["evidence"])
        return result
    try:
        premises = snapshot["premises"]
        if type(premises) is not list or len(premises) > 32:
            _fail("material_unsupported")
        references = {item["source_id"] for claim in [snapshot["claim"], *premises] for item in claim["evidence"]}
        if computation is not None:
            references.update(row["source_id"] for row in computation["sources"])
        if references != set(source_material):
            _fail("source_scope_invalid")
        nodes, intrinsic, labels = {}, {}, {}
        for claim in [snapshot["claim"], *premises]:
            identifier = _ref(claim["id"])
            if identifier in nodes:
                _fail("premise_duplicate")
            nodes[identifier] = claim
            content = claim_material(claim)
            content["derivation"] = None
            labels[identifier] = digest(content)
        def build(identifier, active):
            if identifier in intrinsic:
                return intrinsic[identifier]
            if identifier not in nodes or identifier in active:
                _fail("premise_graph_invalid")
            claim = nodes[identifier]
            edges = claim.get("premises")
            if type(edges) is not list or len(edges) > 32 or len(set(edges)) != len(edges):
                _fail("premise_graph_invalid")
            children = {child: build(_ref(child), active | {identifier}) for child in edges}
            content = claim_material(claim)
            if content["derivation"] is not None:
                if type(content["derivation"]) is not str:
                    _fail("material_invalid")
                pattern = r"(?<![A-Za-z0-9_.-])(?:" + "|".join(re.escape(name) for name in
                    sorted(nodes, key=lambda item: (-len(item), item))) + r")(?![A-Za-z0-9_.-])"
                content["derivation"] = re.sub(pattern, lambda match: "node:" + (
                    digest(children[match.group()]) if match.group() in children else labels[match.group()]), content["derivation"])
            value = {"content": content, "premises": sorted(children.values(), key=canonical)}
            intrinsic[identifier] = value
            return value
        root = build(snapshot["claim"]["id"], set())
        all_nodes = [build(identifier, set()) for identifier in nodes]
        material = {"schema_version": "research-claim-material/v1", "claim": root,
            "question": snapshot["question"]["question"], "premises": sorted(all_nodes, key=canonical),
            "sources": _distinct(source_material.values()), "rubric_criteria": rubric["criteria"]}
        if calculated is not None:
            material.update(schema_version="research-claim-material/v2", computation=calculated)
        return digest(material)
    except (KeyError, TypeError, IndexError, AttributeError):
        _fail("material_invalid")


def review_findings(output, *, allowed_source_refs=()):
    """Decode legacy verdicts or explicitly versioned cited material objections."""
    output = detached(output)
    legacy = type(output) is dict and set(output) == {"verdicts", "rationale"}
    if (not legacy and (type(output) is not dict or set(output) != {"schema_version", "verdicts", "rationale", "objections"}
                       or output["schema_version"] != FINDINGS)):
        _fail("findings_invalid")
    values = output["verdicts"]
    if (type(values) is not dict or set(values) != CRITERIA
            or any(type(value) is not str or value not in {"pass", "fail", "unknown"} for value in values.values())
            or type(output["rationale"]) is not str or not 1 <= len(output["rationale"]) <= 4096):
        _fail("findings_invalid")
    objections = [] if legacy else output["objections"]
    if type(objections) is not list or len(objections) > 16:
        _fail("objections_invalid")
    allowed = set(allowed_source_refs)
    retained = []
    for item in objections:
        if (type(item) is not dict or set(item) != {"criterion", "source_refs", "rationale", "material"}
                or type(item["criterion"]) is not str or item["criterion"] not in CRITERIA
                or type(item["material"]) is not bool or type(item["source_refs"]) is not list
                or not 1 <= len(item["source_refs"]) <= 16
                or type(item["rationale"]) is not str or not 1 <= len(item["rationale"]) <= 4096):
            _fail("objections_invalid")
        refs = sorted(_ref(value) for value in item["source_refs"])
        if len(set(refs)) != len(refs) or not set(refs) <= allowed:
            _fail("objection_source_scope_invalid")
        retained.append({**item, "source_refs": refs})
    return {"verdicts": values, "rationale": output["rationale"], "objections": retained,
            "objection_refs": sorted({digest(item) for item in retained if item["material"]})}


def attempt_metadata(action, *, cycle_ref):
    if action["operation"] not in {"repair", "adjudicate", "acquire"}:
        _fail("continuation_invalid")
    return {"schema_version": ATTEMPT, "claim_id": _ref(action["claim_id"]),
        "material_basis_id": _hash(action["basis_id"]), "defect_sha256": _hash(action["defect_sha256"]),
        "action": action["operation"], "cycle_ref": _ref(cycle_ref)}


def _metadata(value):
    if type(value) is not dict or set(value) != _ATTEMPT_FIELDS or value["schema_version"] != ATTEMPT:
        _fail("history_invalid")
    if type(value["action"]) is not str or value["action"] not in {"repair", "adjudicate", "acquire"}:
        _fail("history_invalid")
    _ref(value["claim_id"])
    _ref(value["cycle_ref"])
    _hash(value["material_basis_id"])
    _hash(value["defect_sha256"])
    return value


def _final_matches(session, attempt, final):
    if final is None or final["state"] != "succeeded" or not final["final_accounting"]:
        return False
    role, job = session.execution.profile.role(attempt.config_ref), attempt.job
    expected = {"attempt_id": attempt.attempt_id, "run_id": attempt.run_id, "binding_id": attempt.binding_id,
        "nonce": attempt.nonce, "job_sha256": attempt.job_sha256, "input_sha256": attempt.input_sha256,
        "profile_sha256": attempt.profile_sha256, "config_ref": attempt.config_ref, "job_id": job["id"],
        "issued_utc": job["issued_utc"], "deadline_utc": job["deadline_utc"], "controller_ref": role.controller_ref,
        "model_ref": role.model_ref, "prompt_version": role.prompt_version,
        "currency": session.execution.profile.currency, "pricing_ref": session.execution.profile.pricing_ref}
    return (all(final[key] == value for key, value in expected.items()) and job["role"] == role.role
            and job["controller_ref"] == role.controller_ref and job["prompt_version"] == role.prompt_version)


def project_claim(session, *, claim_id, prepared, sources):
    """Read authenticated owner records; preserve every reservation in its cycle."""
    claim_id = _ref(claim_id)
    if prepared["snapshot"]["claim"]["id"] != claim_id:
        _fail("claim_mismatch")
    basis = material_basis(prepared["snapshot"], prepared["rubric"], sources)
    strict_basis = _hash(prepared["result"]["basis_id"])
    records = session.execution.records(session)
    attempts, applications, applied, observations = {}, {}, {}, {}
    for record in records:
        data = record["data"]
        kind = record["kind"]
        if kind == "execution_attempt":
            attempt = session.execution.attempt(data["attempt"])
            if attempt.attempt_id in attempts:
                _fail("attempt_duplicate")
            attempts[attempt.attempt_id] = (attempt, data)
        elif kind == "execution_application":
            identifier = data["attempt_id"]
            if identifier in applications and applications[identifier] != data:
                _fail("application_conflict")
            applications[identifier] = data
        elif kind == "execution_applied":
            identifier = data["attempt_id"]
            if identifier in applied and applied[identifier] != data:
                _fail("application_conflict")
            applied[identifier] = data
        elif kind == "execution_observation":
            observation = validate_observation(data["observation"])
            if data["attempt_id"] != observation["attempt_id"]:
                _fail("observation_mismatch")
            previous = observations.get(data["attempt_id"])
            if previous is not None and previous["sequence"] == observation["sequence"] and previous != observation:
                _fail("observation_conflict")
            if previous is None or observation["sequence"] > previous["sequence"]:
                observations[data["attempt_id"]] = observation
    services = getattr(session.execution.profile, "service_repair", None)
    if services is not None:
        from .review_service_policy import approved_profiles
        required_rows = approved_profiles(session.policy.to_dict(), session.execution.profile)
    else:
        required_rows = session.policy.to_dict()["required_evaluators"]
    required = {item["profile_ref"]: item for item in required_rows
                if item["role"] in {"evaluator", "critic"}}
    review_rows, first_pass_findings, reservations, cycles = [], [], [], {}
    for identifier, (attempt, data) in attempts.items():
        meta = _metadata(data["disagreement"]) if "disagreement" in data else None
        if meta is not None and meta["claim_id"] == claim_id:
            cycle = meta["cycle_ref"]
            state = cycles.get(cycle)
            expected = {key: meta[key] for key in ("claim_id", "material_basis_id", "defect_sha256", "action")}
            if state is not None and state["binding"] != expected:
                _fail("cycle_binding_changed")
            if state is None:
                state = {"binding": expected, "result_basis_id": None}
                cycles[cycle] = state
            result = applied.get(identifier, {})
            final = observations.get(identifier)
            result_basis = result.get("disagreement_result_basis")
            if result_basis is not None:
                if (result.get("attempt_id") != identifier or applications.get(identifier, {}).get("attempt_id") != identifier
                        or not _final_matches(session, attempt, final)):
                    _fail("cycle_result_unobserved")
                _hash(result_basis)
                if state["result_basis_id"] is not None and state["result_basis_id"] != result_basis:
                    _fail("cycle_result_conflict")
                state["result_basis_id"] = result_basis
            reservations.append({"attempt_id": identifier, "cycle_ref": cycle, "action": meta["action"],
                "state": "dispatch_unknown" if final is None else final["state"],
                "result_basis_id": result_basis})
        result, application = applied.get(identifier), applications.get(identifier)
        if (result is None or application is None or result.get("operation") != "evaluate"
                or result.get("claim_id") != claim_id or data.get("purpose") == "adjudicate"
                or data.get("retry_purpose") == "adjudicate"):
            continue
        selected = required.get(attempt.config_ref)
        if selected is None:
            continue
        final = observations.get(identifier)
        role = session.execution.profile.role(attempt.config_ref)
        if (final is None or final["state"] != "succeeded" or not final["final_accounting"]
                or final["job_sha256"] != attempt.job_sha256 or final["input_sha256"] != attempt.input_sha256
                or final["nonce"] != attempt.nonce or final["binding_id"] != attempt.binding_id
                or final["controller_ref"] != selected["controller_ref"]
                or final["profile_sha256"] != attempt.profile_sha256 or final["config_ref"] != attempt.config_ref
                or final["run_id"] != attempt.run_id or final["job_id"] != attempt.job["id"]
                or final["issued_utc"] != attempt.job["issued_utc"] or final["deadline_utc"] != attempt.job["deadline_utc"]
                or final["model_ref"] != role.model_ref or final["prompt_version"] != role.prompt_version
                or final["currency"] != session.execution.profile.currency or final["pricing_ref"] != session.execution.profile.pricing_ref
                or role.role != "evaluator" or role.controller_ref != selected["controller_ref"]
                or attempt.job["operation"] != "evaluate" or attempt.job["role"] != "evaluator"
                or attempt.job["controller_ref"] != role.controller_ref or attempt.job["prompt_version"] != role.prompt_version
                or data.get("claim_id") != claim_id or application.get("operation") != "evaluate"
                or application.get("claim_id") != claim_id or application.get("profile_ref") != attempt.config_ref
                or result.get("profile_ref") != attempt.config_ref
                or application.get("strict_basis_id") != result.get("strict_basis_id")):
            _fail("review_correlation_invalid")
        review = application["envelope"]["payload"]["body"]["review"]
        if review.get("claim_id") != claim_id or review.get("basis_id") != result["strict_basis_id"]:
            _fail("review_correlation_invalid")
        allowed_aliases = data.get("review_context", {}).get("aliases", {}).get("sources", {})
        findings = review_findings(final["output"], allowed_source_refs=allowed_aliases.values())
        if review["verdicts"] != findings["verdicts"] or review["rationale"] != findings["rationale"]:
            _fail("review_correlation_invalid")
        material = data.get("review_material_basis")
        if result["strict_basis_id"] != strict_basis:
            continue
        if material is None:
            material = basis
        _hash(material)
        if result["strict_basis_id"] == strict_basis and material != basis:
            _fail("review_material_mismatch")
        slot_fields = {}
        if services is not None:
            from .review_service_routing import review_current
            slot = data.get("service_slot_ref")
            if (slot is None or result.get("service_slot_ref") != slot or application.get("service_slot_ref") != slot
                    or result.get("service_identity") != application.get("service_identity")):
                _fail("review_service_slot_mismatch")
            route = session.execution.profile.review_services.route(slot)
            if attempt.config_ref not in route.config_refs or selected["role"] != route.task_role:
                _fail("review_service_slot_mismatch")
            native_view = __import__("evidence_wiki.execution_session", fromlist=["current"]).current(session)
            if not review_current(session, result, native_view, selected):
                continue
            slot_fields = {"service_slot_ref": slot, "qualification_ref": result["service_identity"]["qualification_ref"]}
        review_ref = digest(review)
        review_rows.append({"review_ref": review_ref, "profile_ref": attempt.config_ref, "role": selected["role"],
            "controller_ref": final["controller_ref"], "basis_id": material, **slot_fields,
            "verdicts": findings["verdicts"], "objection_refs": findings["objection_refs"]})
        first_pass_findings.append({"review_ref": review_ref, "rationale": findings["rationale"],
            "objections": findings["objections"]})
    history = [{"action_ref": cycle, "claim_id": claim_id, "basis_id": state["binding"]["material_basis_id"],
        "action": state["binding"]["action"], "defect_sha256": state["binding"]["defect_sha256"],
        "result_basis_id": state["result_basis_id"]} for cycle, state in cycles.items()]
    return detached({"schema_version": PROJECTION, "claim_id": claim_id, "basis_id": basis,
        "strict_basis_id": strict_basis, "reviews": review_rows, "first_pass_findings": first_pass_findings,
        "history": history, "reservations": reservations})


def adjudication_packet(session, *, claim_id, prepared, sources, context_id, order_seed, gates):
    """Expose current anonymized first passes only after the bounded gate selects investigation.

    The result cannot register a native review. Its caller retains canonical
    review identities and may consume only scoped repair/acquisition proposals.
    """
    projection = project_claim(session, claim_id=claim_id, prepared=prepared, sources=sources)
    decision = disagreement_action(policy=session.policy, claim_id=claim_id, basis_id=projection["basis_id"],
        generator_controller_ref=session.execution.review_signer.generator.controller_ref,
        reviews=projection["reviews"], history=projection["history"], gates=gates,
        service_profile=session.execution.profile if getattr(session.execution.profile, "service_repair", None) is not None else None)
    if decision["operation"] != "adjudicate":
        _fail("adjudication_not_selected")
    context = create_review_context(context_id=context_id, task_role="adjudicator", basis_id=prepared["result"]["basis_id"],
        owner_snapshot=prepared["snapshot"], rubric=prepared["rubric"], sources=sources, order_seed=order_seed)
    if not context.document()["manifest"]["context_complete"]:
        _fail("adjudication_context_incomplete")
    findings = {row["review_ref"]: row for row in projection["first_pass_findings"]}
    identities = {value for role in session.execution.profile.roles for value in
        (role.config_ref, role.controller_ref, role.model_ref, role.prompt_version)}
    def blind(text):
        for identity in sorted(identities, key=lambda value: (-len(value), value)):
            text = text.replace(identity, "[identity-masked]")
        return text
    anonymous = []
    for index, row in enumerate(sorted(projection["reviews"], key=lambda row: row["review_ref"])):
        observed = findings[row["review_ref"]]
        anonymous.append({"review_id": f"review-{index + 1}", "verdicts": row["verdicts"],
            "objection_refs": row["objection_refs"], "rationale": blind(observed["rationale"]),
            "objections": [{**item, "rationale": blind(item["rationale"])} for item in observed["objections"]]})
    packet = {"schema_version": "research-adjudication-context/v1", "context_id": context_id,
        "packet": context.document(), "first_passes": anonymous, "material_basis_id": projection["basis_id"],
        "instructions": "Investigate the explicit disagreement from the exact evidence. Propose a scoped repair or source gap. Prior findings remain retained; no approval or native review is permitted.",
        "authorized": False, "evidence_accepted": False}
    return detached({"packet": packet, "binding": {"schema_version": "research-adjudication-binding/v1",
        "review_context": context.binding(), "material_basis_id": projection["basis_id"],
        "first_pass_refs": [row["review_ref"] for row in projection["reviews"]], "packet_sha256": digest(packet)}})


def validate_targeted_repair(previous_claims, proposed_claims, *, target_claim_id):
    """Require the same owned target and all peer claims before native admission."""
    target_claim_id = _ref(target_claim_id)
    previous, proposed = detached(previous_claims), detached(proposed_claims)
    if type(previous) is not list or type(proposed) is not list or len(previous) > 100 or len(proposed) > 100:
        _fail("repair_scope_invalid")
    before, after = {}, {}
    for values, result in ((previous, before), (proposed, after)):
        for claim in values:
            if type(claim) is not dict:
                _fail("repair_scope_invalid")
            identifier = _ref(claim["id"])
            if identifier in result:
                _fail("repair_scope_invalid")
            result[identifier] = claim
    if target_claim_id not in before or set(before) != set(after):
        _fail("repair_target_changed")
    if before[target_claim_id]["question_slug"] != after[target_claim_id]["question_slug"]:
        _fail("repair_target_changed")
    if any(before[identifier] != after[identifier] for identifier in before if identifier != target_claim_id):
        _fail("repair_peer_changed")


def driver_action(session, projection, *, gates, original_id, presentation_defects=None):
    """Return named proposals for the driver; caller still reserves/checks effects."""
    decision = disagreement_action(policy=session.policy, claim_id=projection["claim_id"], basis_id=projection["basis_id"],
        generator_controller_ref=session.execution.review_signer.generator.controller_ref,
        reviews=projection["reviews"], history=projection["history"], gates=gates,
        service_profile=session.execution.profile if getattr(session.execution.profile, "service_repair", None) is not None else None,
        presentation_defects=presentation_defects)
    operation = decision["operation"]
    result = {"operation": operation, "original_id": _ref(original_id), "claim_id": projection["claim_id"],
        "reason": decision["reason"], "disagreement_decision": decision}
    if operation == "collect_first_passes":
        present = {row.get("service_slot_ref", row["profile_ref"]) for row in projection["reviews"] if row["basis_id"] == projection["basis_id"]}
        missing = next(item for item in session.policy.to_dict()["required_evaluators"]
            if item["role"] in {"evaluator", "critic"} and item["profile_ref"] not in present)
        if getattr(session.execution.profile, "service_repair", None) is not None:
            from .execution_session import current, retained
            from .review_service_routing import selected
            target = selected(session, slot_ref=missing["profile_ref"], claim_id=projection["claim_id"],
                strict_basis_id=projection["strict_basis_id"], view=current(session), records=retained(session))
            if target["action"] != "select":
                _fail("service_first_pass_unavailable")
            if "retry_after_utc" in target and datetime.fromisoformat(target["retry_after_utc"]) > datetime.now(timezone.utc):
                return {"operation": "reconcile", "attempt_id": target["predecessor_attempt_id"],
                    "reason": "retry_backoff", "retry_after_utc": target["retry_after_utc"]}
            result.update(operation="evaluate", purpose="retry" if "predecessor_attempt_id" in target else "evaluate",
                **({"retry_purpose": "evaluate"} if "predecessor_attempt_id" in target else {}),
                **{key: target[key] for key in ("config_ref", "service_slot_ref", "predecessor_attempt_id", "predecessor_observation_ref", "retry_after_utc") if key in target})
        else:
            result.update(operation="evaluate", purpose="evaluate", config_ref=missing["profile_ref"])
    elif operation == "repair":
        result.update(purpose="repair", defects=projection["reviews"], target_claim_id=projection["claim_id"])
        if presentation_defects:
            result["presentation_defects"] = detached(presentation_defects)
    elif operation == "adjudicate":
        result.update(purpose="adjudicate", first_passes=projection["reviews"])
    elif operation == "acquire":
        result["source_gap_ref"] = decision["source_gap_ref"]
    return detached(result)
