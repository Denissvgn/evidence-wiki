"""Gate-safe per-derived eligibility from the existing authenticated owners."""

from datetime import datetime, timezone

from evidence_wiki import execution_session as native
from evidence_wiki.agent_execution import ExecutionError
from evidence_wiki.pack_discovery import owner
from evidence_wiki.service_authentication import correlated


class _Pass:
    """A private per-call owner cache; no opening facts survive into closing."""
    def __init__(self, session, config, *, packets=None):
        self.session, self.config = session, config
        self.policy = owner("_strict_evidence").resolve_policy(session.root, config)
        self._packets = packets
        self._prepared = {}

    def packets(self):
        if self._packets is None:
            self._packets = native._sources(self.session)[0]
        return self._packets

    def prepared(self, claim_id):
        if claim_id not in self._prepared:
            self._prepared[claim_id] = owner("strict_evidence").prepare_review(self.session.root, claim_id)
        return self._prepared[claim_id]


class _InputPass(_Pass):
    """Only the actual input owner can select accounting-independent evidence."""
    def __init__(self, session, config, admission, *, packets):
        admission.held(session)
        super().__init__(session, config, packets=packets)
        self.admission = admission


def _input_claim_ready(session, current, claim_id, facts):
    """Recheck material/current votes; confer no final continuation decision."""
    facts.admission.held(session)
    from .execution_continuations import authenticated_gates, claim_sources, current_gates
    from .execution_disagreement import project_claim
    prepared = facts.prepared(claim_id)
    sources = claim_sources(prepared, facts.packets())
    projection = project_claim(session, claim_id=claim_id, prepared=prepared, sources=sources)
    gates = authenticated_gates(session, projection, current_gates(session, current, prepared, sources), prepared=prepared)
    if (gates["authentication"] != "pass" or gates["mechanical"] != "pass"
            or any(gates[name] is not True for name in ("context_complete", "source_present", "qualified", "authority_granted"))):
        return False
    policy = session.policy.to_dict()
    required = {row["profile_ref"]: row for row in policy["required_evaluators"]}
    if {row["role"] for row in required.values()} != {"evaluator", "critic"}:
        return False
    current_reviews = [row for row in projection["reviews"] if row["basis_id"] == projection["basis_id"]]
    actual = {}
    for review in current_reviews:
        key = review.get("service_slot_ref", review["profile_ref"])
        wanted = required.get(key)
        if (wanted is None or review["role"] != wanted["role"] or review["objection_refs"]
                or any(value != "pass" for value in review["verdicts"].values())
                or review["controller_ref"] == session.execution.review_signer.generator.controller_ref):
            return False
        if key in actual and (actual[key]["verdicts"] != review["verdicts"] or actual[key]["objection_refs"] != review["objection_refs"]):
            return False
        actual[key] = review
    if set(actual) != set(required) or len({row["controller_ref"] for row in actual.values()}) != len(actual):
        return False
    history = projection["history"]
    if any(row["action"] == "adjudicate" and row["basis_id"] == projection["basis_id"] for row in history):
        return False
    material = [row for row in history if row["action"] in {"repair", "acquire"}]
    if material and (material[-1]["result_basis_id"] is None or material[-1]["result_basis_id"] == material[-1]["basis_id"]
            or projection["basis_id"] in {row["basis_id"] for row in material}):
        return False
    limits = policy["continuation_limits"]
    claim_limits = session.execution.profile.disagreement.document()
    for action, counter in (("repair", "repair_cycles"), ("adjudicate", "adjudication_cycles"), ("acquire", "acquisition_attempts")):
        count = sum(row["action"] == action for row in history)
        claim_max = claim_limits["claim_repair_limit"] if action == "repair" else claim_limits["claim_adjudication_limit"] if action == "adjudicate" else limits[counter]
        if count > min(limits[counter], claim_max) or current["usage"][counter] is None or count > current["usage"][counter]:
            return False
    return True

def _local_rejection(session, current, question, facts):
    """An authenticated current claim gate may reject; reason text cannot."""
    claims = question["unresolved_claims"]
    if session.execution.profile.disagreement is None or not claims:
        return False
    from evidence_wiki.execution_continuations import claim_action
    packets = facts.packets()
    decisions = [claim_action(session, current, claim["id"], question["original_id"], packets) for claim in claims]
    return bool(decisions and all(row["operation"] == "reject" for row in decisions))


def questions(session, current, records, *, usage_view=None, facts):
    """Never infer a mandatory vote from the native latest generic review alone."""
    publication = current["canonical_release"]["publication"]
    if publication is None:
        return {}
    attempts = native._attempt_rows(records)
    applications = {row["data"]["attempt_id"]: row["data"] for row in records if row["kind"] == "execution_application"
        and row["data"].get("operation") == "evaluate"}
    applied = {}
    for event in records:
        row = event["data"]
        if event["kind"] == "execution_applied" and row.get("operation") == "evaluate":
            applied[(row.get("claim_id"), row.get("service_slot_ref", row.get("profile_ref")), row.get("strict_basis_id"))] = row
    required = session.policy.to_dict()["required_evaluators"]
    profiles = {row["profile_ref"]: row for row in current["qualification"].get("profiles", [])}
    result = {}
    for question in publication["questions"]:
        reasons, refs = [], []
        if question["accepted"] is not True or not question["claims"]:
            result[question["slug"]] = {"eligible": False, "rejected": _local_rejection(session, current, question, facts),
                "claim_refs": [], "reasons": ["native_question_not_eligible"]}
            continue
        native._counter_stop(session, question_slug=question["slug"])
        for claim in question["claims"]:
            refs.append(claim["id"])
            if session.execution.profile.disagreement is not None:
                from evidence_wiki.execution_continuations import claim_action
                packets = facts.packets()
                try:
                    ready = (_input_claim_ready(session, current, claim["id"], facts) if type(facts) is _InputPass else
                        claim_action(session, current, claim["id"], question["original_id"], packets)["operation"] == "return_gate")
                    if not ready:
                        reasons.append("claim_continuation_not_at_gate")
                except ExecutionError:
                    reasons.append("claim_continuation_unknown")
            for wanted in required:
                review = applied.get((claim["id"], wanted["profile_ref"], publication["basis_id"]))
                if review is None or review.get("passed") is not True:
                    reasons.append("mandatory_first_pass_missing_or_failed")
                    continue
                row = attempts.get(review["attempt_id"])
                if row is None or row.get("original_id") != question["original_id"] or row.get("claim_id") != claim["id"]:
                    reasons.append("mandatory_attempt_subject_mismatch")
                    continue
                attempt = session.execution.attempt(row["attempt"])
                role = session.execution.profile.role(attempt.config_ref)
                observed = native._latest_observation(records, attempt.attempt_id)
                if observed is None:
                    reasons.append("mandatory_owned_intake_missing")
                    continue
                observed = correlated(attempt, observed, session.execution.profile)
                usage = observed.get("observed_usage") or {}
                if (attempt.config_ref != review.get("profile_ref") or observed["state"] != "succeeded"
                        or not observed["final_accounting"] or not native._model_review_observed(session, attempt, observed)
                        or type(usage.get("model_calls")) is not int or usage["model_calls"] <= 0
                        or observed["started_utc"] is None or observed["reason"] == "offline_fixture"
                        or role.role != "evaluator" or row.get("purpose", "evaluate") not in {"evaluate", "retry"}
                        or row.get("retry_purpose", "evaluate") != "evaluate"):
                    reasons.append("mandatory_owned_execution_ineligible")
                    continue
                from .counter_authority import require_current_producer
                prepared = facts.prepared(claim["id"])
                require_current_producer(session, attempt, prepared["snapshot"]["claim"])
                scope = session.execution.profile.role_scopes
                if scope is None or scope.scope(attempt.config_ref).task_role != wanted["role"]:
                    reasons.append("mandatory_task_role_unknown")
                    continue
                from evidence_wiki.review_context import ReviewContext
                application = applications.get(attempt.attempt_id)
                if application is None or usage_view is None or application.get("passed") is not True:
                    reasons.append("mandatory_native_application_unknown")
                    continue
                native._verify_review_controller(session, attempt, application["envelope"])
                config = facts.config
                payload = application["envelope"]["payload"]["body"]["review"]
                policy = facts.policy
                age = (datetime.now(timezone.utc) - owner("_evidence_authority").timestamp(payload["reviewed_at"])).total_seconds()
                if (application.get("claim_id") != claim["id"] or payload["claim_id"] != claim["id"]
                        or application.get("strict_basis_id") != publication["basis_id"]
                        or payload["basis_id"] != publication["basis_id"] or not 0 <= age <= policy["max_review_age_seconds"]):
                    reasons.append("mandatory_current_review_binding_or_age_invalid")
                    continue
                owner("_strict_evidence").validate_review(session.root, config, application["envelope"], usage_view)
                rubric = application["envelope"]["payload"]["body"]["review"]["verdicts"]
                if (observed["output"].get("verdicts") != rubric or not rubric
                        or any(value != "pass" for value in rubric.values())):
                    reasons.append("mandatory_verdict_application_mismatch")
                    continue
                prepared = facts.prepared(claim["id"])
                if prepared["result"]["basis_id"] != publication["basis_id"] or "review_context" not in row:
                    reasons.append("mandatory_current_context_unknown")
                    continue
                if "review_assembly_request_id" in row:
                    from .review_packet_execution import require_current
                    require_current(session, attempt, row)
                context = ReviewContext.restore(attempt.input_document["packet"], row["review_context"])
                packets = facts.packets()
                source_ids = set(row["review_context"]["aliases"]["sources"])
                context.check(prepared["snapshot"], prepared["rubric"], [packet for packet in packets if packet["source_id"] in source_ids])
                if not context.document()["manifest"]["context_complete"]:
                    reasons.append("mandatory_current_context_incomplete")
                    continue
                if session.execution.profile.review_services is not None:
                    from evidence_wiki.review_service_routing import review_current
                    valid = review_current(session, review, current, wanted)
                else:
                    profile = profiles.get(attempt.config_ref, {})
                    valid = (attempt.config_ref == wanted["profile_ref"] and role.controller_ref == wanted["controller_ref"]
                        and profile.get("qualified") is True and profile.get("independent") is True
                        and profile.get("controller_ref") == role.controller_ref)
                if not valid:
                    reasons.append("mandatory_actual_target_not_current")
        result[question["slug"]] = {"eligible": not reasons, "claim_refs": sorted(set(refs)) if not reasons else [],
            "rejected": False, "reasons": sorted(set(reasons))}
    return result
