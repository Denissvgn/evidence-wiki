"""Existing authenticated attempts retain inert coverage examination findings.

This driver requires matching role/reservation hooks. Coverage links use
canonical owners; findings neither admit claims nor sign semantic reviews.
"""

from datetime import datetime, timezone

from evidence_wiki import execution_session as native
from evidence_wiki.errors import EvidenceWikiError, UsageError
from evidence_wiki.mutation_contracts import detached, digest, validate_request_id
from evidence_wiki.research_session import ResearchSession
from evidence_wiki.review_context import ReviewContext

from .counter_authority import measured_model, verify_controller
from .counter_context import translate
from .coverage_examination import fail, reduce_coverage
from .coverage_observation import observe


def settle(session, request_id, attempt, original_id, result):
    if result["status"] == "observed":
        result = {**result, "searches": apply_findings(session, request_id, attempt, result)}
    with native.gate(session):
        native.append(session, "execution_coverage_result", native.child_id(request_id, "result"), result)
        native.append(session, "execution_applied", native.child_id(attempt.attempt_id, "applied"),
            {"attempt_id": attempt.attempt_id, "operation": "examination", "original_id": original_id})
    row = next(row["data"] for row in native.retained(session)
        if row["kind"] == "execution_attempt" and row["data"]["attempt"]["attempt_id"] == attempt.attempt_id)
    return outward(session, row["examination_instruction"], result)


def examine(session, *, request_id, question_slug, config_ref, kind, source_ids=(), claim_id=None):
    """Reserve one real configured worker; unknown dispatch is never reinvoked."""
    if type(session) is not ResearchSession or session.execution is None:
        fail("bound_execution_session_required")
    request_id = validate_request_id(request_id)
    if type(kind) is not str or kind not in {"coverage", "counterevidence"}:
        fail("examination_kind_invalid")
    session._check("advance", write=True)
    session.context.require_grant("coverage_session.examine")
    role = session.execution.profile.role(config_ref)
    registry = session.execution.profile.role_scopes
    if registry is None:
        fail("scoped_examination_role_required")
    scope = registry.scope(config_ref)
    required_role, capability = ("coverage", "coverage.propose") if kind == "coverage" else ("critic", "objection.propose")
    if scope.task_role != required_role or session.context.controller_for(role.role, config_ref) != role.controller_ref:
        fail("examination_controller_ungranted")
    scope.require("model")
    scope.require(capability)
    identity = {"kind": kind, "question_slug": question_slug, "config_ref": config_ref,
        "source_ids": detached(list(source_ids)), "claim_id": claim_id}
    attempt_id = native.child_id(request_id, "examination")
    saved = None
    with native.gate(session):
        records = native.retained(session)
        previous = next((row for row in records if row["kind"] == "execution_coverage_instruction" and row["request_id"] == request_id), None)
        if previous is not None and previous["data"] != identity:
            fail("request_conflict")
        row = next((item["data"] for item in records if item["kind"] == "execution_attempt" and item["data"]["attempt"]["attempt_id"] == attempt_id), None)
        completed = next((item["data"] for item in records if item["kind"] == "execution_coverage_result" and item["request_id"] == native.child_id(request_id, "result")), None)
        if completed is not None:
            # A crash may leave final findings before the applied marker. Finish
            # this exact historical settlement without reinvoking any effect.
            native.append(session, "execution_applied", native.child_id(attempt_id, "applied"),
                {"attempt_id": attempt_id, "operation": "examination", "original_id": row["original_id"]})
            return outward(session, row["examination_instruction"], completed)
        saved = next((item["data"] for item in records if item["kind"] == "execution_coverage_application" and item["request_id"] == native.child_id(request_id, "application")), None)
    if saved is not None:
        if row is None:
            fail("owned_examination_attempt_missing")
        return settle(session, request_id, session.execution.attempt(row["attempt"]), row["original_id"], saved)
    with native.gate(session):
        native._begin(session)
        current = native.current(session)
        if not source_ids:
            source_ids = [item["source_id"] for item in current["execution"]["source_bindings"]]
        instruction = observe(session.root, question_slug=question_slug, source_ids=source_ids)
        if not {source["source_id"] for source in instruction["source_contexts"]} <= {source["source_id"] for source in current["execution"]["source_bindings"]}:
            fail("source_outside_run_scope")
        originals = [item for item in current["accounting"]["original_outcomes"] if any(question["slug"] == question_slug for question in item["questions"])]
        if len(originals) != 1 or current["qualification"]["state"] != "qualified":
            fail("qualified_frozen_original_required")
        original_id = originals[0]["original_id"]
        if previous is None:
            native.append(session, "execution_coverage_instruction", request_id, identity)
        if row is None:
            action = {"operation": "generate" if kind == "coverage" else "evaluate", "purpose": kind,
                "config_ref": config_ref, "original_id": original_id, "examination_instruction": instruction}
            if kind == "counterevidence":
                if claim_id not in {claim["id"] for claim in instruction["claims"]}:
                    fail("claim_outside_frozen_question")
                action["claim_id"] = claim_id
            attempt = native.reserve(session, attempt_id, action, current)
        else:
            attempt = session.execution.attempt(row["attempt"])
            instruction = row["examination_instruction"]
            original_id = row["original_id"]
    observed = native._latest_observation(native.retained(session), attempt_id)
    for _index in range(32):
        if observed is not None and observed["state"] != "running":
            break
        method = "invoke" if observed is None and row is None else "poll"
        observation, reason = native._external(session, method, attempt)
        if reason is not None:
            return {"status": "blocked_external", "reason": reason, "accepted": False, "findings": None}
        observed = native.intake(session, observation, request_id=native.child_id(request_id, "observation." + str(observation["sequence"])))
    if observed is None or observed["state"] != "succeeded" or not observed["final_accounting"]:
        return {"status": "blocked_external", "reason": "examination_not_completed", "accepted": False, "findings": None}
    observed = session.execution.authenticate(observed, attempt)
    with native.gate(session):
        current = native.current(session)
        result = {"status": "blocked_external", "reason": "examination_current_owner_unavailable", "accepted": False, "findings": None}
        try:
            refreshed = observe(session.root, question_slug=question_slug, source_ids=source_ids)
        except EvidenceWikiError:
            refreshed = None
        if refreshed is not None:
            if (current["qualification"]["state"] != "qualified" or current["basis_id"] != attempt.job["basis_id"]
                    or refreshed["basis_id"] != instruction["basis_id"] or datetime.now(timezone.utc) >= datetime.fromisoformat(attempt.job["deadline_utc"])):
                result["reason"] = "examination_basis_stale"
            elif not measured_model(session, attempt, observed):
                result["reason"] = "examination_model_provenance_unknown"
            else:
                try:
                    if kind == "coverage":
                        findings = reduce_coverage(instruction, observed["output"])
                    else:
                        verify_controller(session, attempt)
                        records = native.retained(session)
                        owned = next(item["data"] for item in records if item["kind"] == "execution_attempt" and item["data"]["attempt"]["attempt_id"] == attempt_id)
                        context = ReviewContext.restore(attempt.input_document["packet"], owned["review_context"])
                        findings = translate(instruction, context, observed["output"])
                    result = {"status": "observed", "reason": "owner_effect_and_semantic_checks_required", "accepted": False, "findings": findings,
                        "instruction_basis": instruction["basis_id"], "attempt_ref": digest(attempt.document()), "searches": []}
                except UsageError as error:
                    reason = error.details.get("reason")
                    result["reason"] = reason if error.error_code == "COVERAGE_EXAMINATION_INVALID" and type(reason) is str else "examination_output_invalid"
                except (ValueError, KeyError, TypeError):
                    result["reason"] = "examination_output_invalid"
        if result["status"] == "observed":
            native.append(session, "execution_coverage_application", native.child_id(request_id, "application"), result)
    return settle(session, request_id, attempt, original_id, result)


def apply_findings(session, request_id, attempt, result):
    """Replay exact native intents; worker prose cannot change frozen criteria."""
    row = next(row["data"] for row in native.retained(session)
        if row["kind"] == "execution_attempt" and row["data"]["attempt"]["attempt_id"] == attempt.attempt_id)
    instruction = row["examination_instruction"]
    def check():
        session._check("advance", write=True)
        fresh = observe(session.root, question_slug=instruction["question_slug"],
            source_ids=[source["source_id"] for source in instruction["source_contexts"]])
        if (fresh["basis_id"] != instruction["basis_id"]
                or native.current(session)["qualification"]["state"] != "qualified"
                or datetime.now(timezone.utc) >= datetime.fromisoformat(attempt.job["deadline_utc"])):
            fail("examination_basis_stale")
    completed = completed_effects(session, request_id, row, result)
    if completed is not None:
        return completed
    check()
    searches = []
    if row["purpose"] == "coverage":
        for index, link in enumerate(result["findings"]["link_proposals"]):
            native._mutation(session, native.child_id(request_id, "link." + str(index)), "coverage.link",
                {"slug": link["slug"], "facet_id": link["facet_id"], "accepted_source_ids": link["source_ids"],
                    "blocking_request_ids": None, "clear_accepted_source_ids": False,
                    "clear_blocking_request_ids": False}, effect_deadline=attempt.job["deadline_utc"], effect_precondition=check)
        if result["findings"]["link_proposals"]:
            native._mutation(session, native.child_id(request_id, "evaluate"), "coverage.evaluate",
                {"slug": instruction["question_slug"]}, effect_deadline=attempt.job["deadline_utc"], effect_precondition=check)
    else:
        from .counter_search import retained as search
        for index, finding in enumerate(result["findings"]["findings"]):
            selected = [source["source_id"] for source in instruction["source_contexts"]]
            identifier = native.child_id(request_id, "search." + str(index))
            observed = search(session, request_id=identifier,
                question_slug=instruction["question_slug"], query=finding["query"], source_ids=selected,
                exclusions=finding["exclusions"], expected_instruction_basis=instruction["basis_id"])
            searches.append({"request_id": identifier, "observation_ref": digest(observed),
                "query": finding["query"], "exclusions": finding["exclusions"], "source_ids": selected,
                "limit": 8, "search_execution": "observed", "absence_conclusion": "unknown"})
    check()
    return searches


def completed_effects(session, request_id, row, result):
    """Authenticate exact prior effect observations for historical settlement."""
    records = native.retained(session)
    intents = {item["data"]["reservation"]["attempt_id"]: item["data"] for item in records
        if item["kind"] == "execution_native"}
    observations = {item["data"]["observation"]["attempt_id"]: item["data"] for item in records
        if item["kind"] == "execution_native_observation"}
    instruction = row["examination_instruction"]
    planned = []
    if row["purpose"] == "coverage":
        for index, link in enumerate(result["findings"]["link_proposals"]):
            planned.append((native.child_id(request_id, "link." + str(index)), "coverage.link",
                {"slug": link["slug"], "facet_id": link["facet_id"], "accepted_source_ids": link["source_ids"],
                    "blocking_request_ids": None, "clear_accepted_source_ids": False, "clear_blocking_request_ids": False}))
        if planned:
            planned.append((native.child_id(request_id, "evaluate"), "coverage.evaluate", {"slug": instruction["question_slug"]}))
    else:
        for index, finding in enumerate(result["findings"]["findings"]):
            planned.append((native.child_id(request_id, "search." + str(index)), "coverage.counter_search",
                {"question_slug": instruction["question_slug"], "query": finding["query"],
                    "source_ids": [source["source_id"] for source in instruction["source_contexts"]],
                    "exclusions": finding["exclusions"], "expected_instruction_basis": instruction["basis_id"], "limit": 8}))
    searches = []
    for identifier, operation, arguments in planned:
        intent, observed = intents.get(identifier), observations.get(identifier)
        if intent is None or observed is None:
            return None
        if intent["operation"] != operation or intent["arguments"] != arguments or observed["error_code"] is not None:
            fail("owned_effect_conflict")
        actual = observed["result"]
        if operation == "coverage.counter_search":
            if actual.get("search_execution") != "observed":
                fail("owned_search_unobserved")
            searches.append({"request_id": identifier, "observation_ref": digest(actual),
                "query": arguments["query"], "exclusions": arguments["exclusions"], "source_ids": arguments["source_ids"],
                "limit": 8, "search_execution": "observed", "absence_conclusion": "unknown"})
        elif actual.get("status") not in {"committed", "reconciled"}:
            fail("owned_coverage_unobserved")
    return searches


def outward(session, instruction, result):
    """Historical findings expose quotations only under current exact material."""
    if result["findings"] is None:
        return result
    try:
        live = observe(session.root, question_slug=instruction["question_slug"],
            source_ids=[source["source_id"] for source in instruction["source_contexts"]])
    except EvidenceWikiError:
        live = None
    if live is None or live["basis_id"] != instruction["basis_id"]:
        return {"status": "blocked_external", "reason": "examination_historical_material_unavailable",
            "accepted": False, "findings": None, "searches": []}
    return result
