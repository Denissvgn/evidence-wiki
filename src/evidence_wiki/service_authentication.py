"""Parent native credential checks and exact sealed execution facts, without enrollment."""

import secrets
from datetime import datetime, timezone

from evidence_wiki.agent_execution import ExecutionAttempt, validate_observation
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.mutation_contracts import digest
from evidence_wiki.pack_discovery import owner
from evidence_wiki.research_session import ResearchSession

from .review_service_contracts import STATUS, refuse, validate
from .review_service_policy import ReviewServicePlan


def correlated(attempt, value, profile):
    """Check exact owned output correlation; caller must authenticate its native record."""
    if type(attempt) is not ExecutionAttempt:
        refuse("service_attempt_invalid")
    value = validate_observation(value)
    role = profile.role(attempt.config_ref)
    if (attempt.profile_sha256 != profile.sha256 or attempt.job["controller_ref"] != role.controller_ref
            or attempt.job["role"] != role.role or attempt.job["prompt_version"] != role.prompt_version):
        refuse("service_attempt_scope_changed")
    expected = {"attempt_id": attempt.attempt_id, "nonce": attempt.nonce, "run_id": attempt.run_id,
        "binding_id": attempt.binding_id, "profile_sha256": attempt.profile_sha256,
        "job_id": attempt.job["id"], "job_sha256": attempt.job_sha256, "input_sha256": attempt.input_sha256,
        "issued_utc": attempt.job["issued_utc"], "deadline_utc": attempt.job["deadline_utc"],
        "config_ref": attempt.config_ref, "controller_ref": role.controller_ref, "model_ref": role.model_ref,
        "prompt_version": role.prompt_version, "currency": profile.currency, "pricing_ref": profile.pricing_ref}
    if any(value.get(key) != actual for key, actual in expected.items()):
        refuse("service_observation_not_correlated")
    profile.check_job_scope(attempt.config_ref, attempt.job, attempt.input_document)
    return value


def owned(session, attempt_id):
    """Use already HMAC-verified native intake, never a host callback under a gate."""
    records = session.execution.records(session)
    row = next((item["data"] for item in records if item["kind"] == "execution_attempt"
        and item["data"]["attempt"]["attempt_id"] == attempt_id), None)
    values = [item["data"]["observation"] for item in records if item["kind"] == "execution_observation"
        and item["data"]["attempt_id"] == attempt_id]
    if row is None or not values:
        refuse("service_owned_execution_missing")
    attempt = session.execution.attempt(row["attempt"])
    if (attempt.run_id != session.run_id or attempt.binding_id != digest(session.binding)
            or attempt.job["policy_id"] != session.policy.policy_id):
        refuse("service_attempt_owner_changed")
    latest = max(values, key=lambda value: value["sequence"])
    return row, attempt, correlated(attempt, latest, session.execution.profile)


def _credential(session, identity, authority, trust, now, task_role):
    selected = session.execution.profile.role(identity.config_ref)
    scope = session.execution.profile.role_scopes.scope(identity.config_ref)
    if (scope.task_role != task_role or selected.controller_ref != identity.controller_ref
            or session.context.controller_for(selected.role, identity.config_ref) != identity.controller_ref):
        refuse("service_controller_or_role_changed")
    scope.require("model")
    challenge = {"schema_version": "research-service-challenge/v1", "binding_id": digest(session.binding),
        "profile_id": "sha256:" + session.execution.profile.sha256, "config_ref": identity.config_ref,
        "task_role": task_role, "role_scope_sha256": scope.sha256, "nonce": secrets.token_hex(16), "issued_at": now.isoformat()}
    proof = session.execution.review_signer._sign(challenge, identity, now.isoformat())
    checked = authority.verify_attestation(proof, identity.role, trust, now)
    if (checked.get("authenticated") is not True or checked.get("controller") != identity.controller_ref
            or checked.get("principal") != identity.principal):
        refuse("service_native_credential_unavailable")
    return digest(proof)


def readability_credential(session):
    """Challenge the distinct selected critic without ordinary review enrollment."""
    session._check("current")
    session.context.require_grant("service_authentication.status")
    plan = session.execution.profile.readability
    if plan is None:
        refuse("readability_not_configured")
    plan.selected_profile(session.policy, session.execution.profile)
    matches = [identity for identity in session.execution.review_signer.reviewers if identity.config_ref == plan.config_ref]
    if len(matches) != 1:
        refuse("readability_native_credential_missing")
    config = owner("_strict_evidence").configuration(session.root)
    authority = owner("_evidence_authority")
    now = datetime.now(timezone.utc)
    trust = authority.load_trust(session.root, config, now)
    reference = _credential(session, matches[0], authority, trust, now, "critic")
    closing = datetime.now(timezone.utc)
    if (closing < now or authority.load_trust(session.root, config, closing)["content_hash"] != trust["content_hash"]
            or owner("_strict_evidence").configuration(session.root) != config):
        refuse("readability_current_credential_changed")
    return reference


def status(session, *, plan=None):
    """Read configured parent credentials; actual service execution is a separate fact."""
    from . import execution_session as driver

    if type(session) is not ResearchSession or session.execution is None:
        refuse("service_execution_missing")
    session._check("current")
    session.context.require_grant("service_authentication.status")
    session.execution.check(session.binding)
    if plan is not None:
        if type(plan) is not ReviewServicePlan:
            refuse("service_plan_invalid")
        plan.check(session.execution.profile, session.policy.to_dict())
    frozen_plan = getattr(session.execution.profile, "review_services", None)
    if plan is not None and plan != frozen_plan:
        refuse("service_declared_plan_changed")
    plan = frozen_plan
    if plan is not None:
        plan.check(session.execution.profile, session.policy.to_dict())
    held_epoch = driver._CURRENT_FACTS.get() is not None
    before = driver._current_identity(session)
    config = owner("_strict_evidence").configuration(session.root)
    authority = owner("_evidence_authority")
    now = datetime.now(timezone.utc)
    try:
        trust = authority.load_trust(session.root, config, now)
    except (Exception, SystemExit):
        missing = []
        for identity in _ordinary_identities(session):
            scope = session.execution.profile.role_scopes.scope(identity.config_ref)
            missing.append({"config_ref": identity.config_ref, "slot_ref": None if identity.role == "generator" else identity.config_ref,
                "task_role": scope.task_role, "principal": None, "controller_ref": identity.controller_ref,
                "credential_state": "unavailable", "qualification_state": "not_required" if identity.role == "generator" else "unknown",
                "execution_state": "not_observed", "required": True, "reason": "native_authority_unavailable",
                "challenge_ref": None, "owned_observation_ref": None})
        return validate(STATUS, {"schema_version": STATUS, "run_id": session.run_id, "binding_id": digest(session.binding),
            "profile_id": "sha256:" + session.execution.profile.sha256, "policy_id": session.policy.policy_id,
            "authority_id": None, "observed_at": now.isoformat(), "state": "blocked", "reason": "native_authority_unavailable",
            "services": missing, "credential_probe": "parent_native_credential_challenge", "execution_probe": "existing_correlated_owned_attempts_only",
            "enrollment": "not_performed", "authorized": False, "evidence_accepted": False})
    required = {row["profile_ref"]: row for row in session.policy.to_dict()["required_evaluators"]}
    qualifications = {row["profile_ref"]: row for row in before["qualification"]["profiles"]}
    route_slots = ({config: route.slot_ref for route in plan.routes for config in route.config_refs}
        if plan is not None else {config: config for config in required})
    records = session.execution.records(session)
    rows = []
    for identity in _ordinary_identities(session):
        registry = session.execution.profile.role_scopes
        scope = registry.scope(identity.config_ref) if registry is not None else None
        task_role = scope.task_role if scope is not None else "host_worker" if identity.role == "generator" else "evaluator"
        slot = None if identity.role == "generator" else route_slots.get(identity.config_ref)
        qualification = qualifications.get(identity.config_ref, {})
        qualified = (qualification.get("qualified") is True and qualification.get("independent") is True
            and qualification.get("role") == task_role and qualification.get("controller_ref") == identity.controller_ref)
        required_row = identity.role == "generator" or slot is not None
        row = {"config_ref": identity.config_ref, "slot_ref": slot, "task_role": task_role, "principal": None,
            "controller_ref": identity.controller_ref, "credential_state": "unavailable",
            "qualification_state": "not_required" if identity.role == "generator" else "qualified" if qualified else "unknown",
            "execution_state": "not_observed", "required": required_row, "reason": "credential_or_scope_unavailable",
            "challenge_ref": None, "owned_observation_ref": None}
        try:
            if scope is None or (identity.role == "generator" and task_role not in {"researcher", "repair"}) or (
                    identity.role == "evaluator" and task_role not in {"evaluator", "critic", "adjudicator"}):
                refuse("service_scoped_role_required")
            row.update(credential_state="verified", principal=identity.principal, reason="native_parent_credential_verified",
                challenge_ref=_credential(session, identity, authority, trust, now, task_role))
            for item in reversed(records):
                if item["kind"] != "execution_observation":
                    continue
                identifier = item["data"]["attempt_id"]
                retained = next((event["data"] for event in records if event["kind"] == "execution_attempt"
                    and event["data"]["attempt"]["attempt_id"] == identifier), None)
                if retained is None or retained["attempt"]["config_ref"] != identity.config_ref:
                    continue
                attempt = session.execution.attempt(retained["attempt"])
                fact = correlated(attempt, item["data"]["observation"], session.execution.profile)
                if fact["state"] == "succeeded" and fact["final_accounting"]:
                    row.update(execution_state="authenticated_owned_execution", owned_observation_ref=digest(fact))
                break
        except (EvidenceWikiError, ValueError, TypeError, KeyError):
            row.update(credential_state="unavailable", principal=None, challenge_ref=None,
                reason="credential_or_scope_unavailable")
        rows.append(row)
    represented = {row["slot_ref"] for row in rows if row["slot_ref"] is not None}
    generators = [row for row in rows if row["qualification_state"] == "not_required"]
    ready = (before["policy_compatible"] is True and set(required) <= represented and len(generators) == 1
        and generators[0]["credential_state"] == "verified" and all(any(row["slot_ref"] == slot
            and row["credential_state"] == "verified" and row["qualification_state"] == "qualified" for row in rows)
            for slot in required))
    after = datetime.now(timezone.utc)
    if (after < now or authority.load_trust(session.root, config, after)["content_hash"] != trust["content_hash"]
            or owner("_strict_evidence").configuration(session.root) != config):
        refuse("service_current_identity_changed")
    closing = driver._current_identity(session)
    if held_epoch:
        completed = datetime.now(timezone.utc)
        if (completed < after
                or authority.load_trust(session.root, config, completed)["content_hash"] != trust["content_hash"]
                or owner("_strict_evidence").configuration(session.root) != config):
            refuse("service_current_identity_changed")
    if (closing["basis_id"] != before["basis_id"]
            or held_epoch and closing["qualification"] != before["qualification"]):
        refuse("service_current_identity_changed")
    return validate(STATUS, {"schema_version": STATUS, "run_id": session.run_id, "binding_id": digest(session.binding),
        "profile_id": "sha256:" + session.execution.profile.sha256, "policy_id": before["policy_id"],
        "authority_id": authority.authority_basis(trust), "observed_at": now.isoformat(), "state": "ready" if ready else "blocked",
        "reason": "required_credentials_and_qualification_current" if ready else "credential_qualification_or_policy_unavailable",
        "services": rows, "credential_probe": "parent_native_credential_challenge", "execution_probe": "existing_correlated_owned_attempts_only",
        "enrollment": "not_performed", "authorized": False, "evidence_accepted": False})


def _ordinary_identities(session):
    signer = session.execution.review_signer
    optional = getattr(session.execution.profile, "readability", None)
    return (signer.generator, *(identity for identity in signer.reviewers
        if optional is None or identity.config_ref != optional.config_ref))


def require_output(session, attempt, observation, prepared):
    """Require current configured credential, measured correlated output and native mechanics."""
    retained, exact_attempt, exact_observation = owned(session, attempt.attempt_id)
    if attempt.document() != exact_attempt.document() or observation != exact_observation:
        refuse("service_owned_output_changed")
    observation = exact_observation
    claim_id = retained.get("claim_id")
    if prepared["snapshot"]["claim"]["id"] != claim_id:
        refuse("service_prepared_claim_changed")
    from . import execution_session as native_driver
    from .review_context import ReviewContext
    current = owner("strict_evidence").prepare_review(session.root, claim_id)
    if (current["snapshot"] != prepared["snapshot"] or current["rubric"] != prepared["rubric"]
            or current["result"]["basis_id"] != prepared["result"]["basis_id"]):
        refuse("service_prepared_basis_changed")
    if "review_context" not in retained:
        refuse("service_context_owner_missing")
    context = ReviewContext.restore(attempt.input_document["packet"], retained["review_context"])
    packets, _ = native_driver._sources(session)
    selected = set(retained["review_context"]["aliases"]["sources"])
    context.check(current["snapshot"], current["rubric"], [row for row in packets if row["source_id"] in selected])
    if not context.document()["manifest"]["context_complete"]:
        refuse("service_context_incomplete")
    native_driver.require_machine_compatible(session)
    from .counter_authority import require_current_producer
    require_current_producer(session, attempt, current["snapshot"]["claim"])
    if (observation["state"] != "succeeded" or not observation["final_accounting"]
            or observation["started_utc"] is None or observation["observed_usage"]["model_calls"] is None
            or observation["observed_usage"]["model_calls"] <= 0 or observation["reason"] == "offline_fixture"
            or session.execution.profile.role(attempt.config_ref).model_ref.startswith("offline/")):
        refuse("service_model_execution_not_observed")
    report = status(session)
    row = next((row for row in report["services"] if row["config_ref"] == attempt.config_ref), None)
    if (row is None or row["credential_state"] != "verified" or row["qualification_state"] != "qualified"
            or row["task_role"] != attempt.job.get("task_role")):
        refuse("service_credentials_or_qualification_unavailable")
    native = next((row for row in prepared["result"]["claims"] if row["claim"]["id"] == prepared["snapshot"]["claim"]["id"]), None)
    if native is None or native["observation"]["reasons"]:
        refuse("service_native_mechanics_not_passed")
    return {"config_ref": attempt.config_ref, "observation_ref": digest(observation),
        "native_basis_id": prepared["result"]["basis_id"], "credential_challenge_ref": row["challenge_ref"],
        "authorization": "none"}
