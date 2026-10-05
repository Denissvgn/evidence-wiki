"""Current native evaluator identity proof confers no semantic review or effect."""

from datetime import datetime, timezone

from evidence_wiki import execution_session as native
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.mutation_contracts import digest
from evidence_wiki.pack_discovery import owner

from .coverage_examination import fail


def verify_controller(session, attempt):
    """Use existing signer keys and canonical trust; do not register a review."""
    selected = session.execution.profile.role(attempt.config_ref)
    if selected.role != "evaluator":
        fail("counter_evaluator_required")
    qualification = native.current(session)["qualification"]
    profiles = [row for row in qualification["profiles"] if row.get("profile_ref") == attempt.config_ref]
    if qualification["state"] != "qualified" or len(profiles) != 1 or profiles[0].get("qualified") is not True or profiles[0].get("independent") is not True:
        fail("counter_current_role_qualification_unknown")
    signer = session.execution.review_signer
    identities = [identity for identity in signer.reviewers if identity.config_ref == attempt.config_ref]
    if len(identities) != 1 or identities[0].controller_ref != selected.controller_ref:
        fail("counter_signer_mapping_changed")
    now = datetime.now(timezone.utc)
    payload = {"schema_version": "research-counter-controller-proof/v1", "attempt_ref": digest(attempt.document()),
        "controller_ref": selected.controller_ref, "semantic_approval": "not_evaluated", "accepted": False}
    envelope = signer._sign(payload, identities[0], now.isoformat())
    authority = owner("_evidence_authority")
    config = owner("verify_quotes").load_config(session.root)
    trust = authority.load_trust(session.root, config, now)
    authenticated = authority.verify_attestation(envelope, "evaluator", trust, now)
    if not authenticated.get("authenticated") or authenticated.get("controller") != selected.controller_ref:
        fail("counter_controller_not_current")
    # Authenticate the current canonical producer attestation, rather than
    # manufacturing a new authorship statement for an already admitted claim.
    prepared = owner("strict_evidence").prepare_review(session.root, attempt_claim(session, attempt))
    require_current_producer(session, attempt, prepared["snapshot"]["claim"])
    usage = owner("_evidence_usage")
    if not usage.configured(config):
        fail("counter_generator_provenance_unknown")
    with usage.current_view(session.root, config) as view:
        matching = [record for record in view.state.strict_reviews.values()
            if record["envelope"]["payload"]["body"]["review"]["basis_id"] == prepared["result"]["basis_id"]
            and record["envelope"]["payload"]["body"]["review"]["claim_id"] == prepared["snapshot"]["claim"]["id"]]
        if not matching:
            fail("counter_generator_provenance_unknown")
        generator = matching[-1]["envelope"]["payload"]["body"]["review"]["generator"]
        authored = authority.verify_attestation(generator, "generator", view.trust, view.now)
        expected = {"schema_version": "evidence-strict-authorship/v1",
            "basis_id": prepared["result"]["basis_id"], "claim_id": prepared["snapshot"]["claim"]["id"]}
        if (generator["payload"] != expected or not authored.get("authenticated")
                or authored.get("controller") != signer.generator.controller_ref
                or authored.get("controller") == authenticated.get("controller")):
            fail("counter_generator_controller_not_current_or_independent")
        view.revalidate(session.root, config)
    return envelope


def measured_model(session, attempt, observation):
    """Neither signed offline output nor a zero model-call meter is examination."""
    usage = observation.get("observed_usage") or {}
    return (native._model_review_observed(session, attempt, observation)
        and type(usage.get("model_calls")) is int and usage["model_calls"] > 0
        and observation.get("started_utc") is not None and observation.get("reason") != "offline_fixture"
        and not session.execution.profile.role(attempt.config_ref).model_ref.startswith("offline/"))


def attempt_claim(session, attempt):
    """Resolve the canonical parent subject only from the authenticated journal."""
    rows = [row["data"] for row in session.execution.records(session)
        if row["kind"] == "execution_attempt" and row["data"]["attempt"]["attempt_id"] == attempt.attempt_id]
    if len(rows) != 1 or rows[0].get("claim_id") is None:
        fail("counter_claim_provenance_unknown")
    return rows[0]["claim_id"]


def require_current_producer(session, critic_attempt, current_claim):
    """Native authorship also needs an actual correlated producer application."""
    records = session.execution.records(session)
    attempts = {row["data"]["attempt"]["attempt_id"]: row["data"] for row in records if row["kind"] == "execution_attempt"}
    applied = {row["data"]["attempt_id"] for row in records if row["kind"] == "execution_applied"
        and row["data"].get("operation") == "generate"}
    subject = attempts[critic_attempt.attempt_id]
    for record in reversed(records):
        if record["kind"] != "execution_application" or record["data"].get("operation") != "generate":
            continue
        application = record["data"]
        identifier = application["attempt_id"]
        if identifier not in applied or application.get("original_id") != subject["original_id"]:
            continue
        inventory = application["proposal"]["inventory"]
        if not any(claim == current_claim for claim in inventory["claims"]):
            continue
        producer = session.execution.attempt(attempts[identifier]["attempt"])
        role = session.execution.profile.role(producer.config_ref)
        scopes = session.execution.profile.role_scopes
        if (role.role != "host_worker" or role.controller_ref != session.execution.review_signer.generator.controller_ref
                or session.context.controller_for(role.role, role.config_ref) != role.controller_ref
                or scopes is None or scopes.scope(role.config_ref).task_role not in {"researcher", "repair"}):
            continue
        observation = native._latest_observation(records, identifier)
        if (observation is None or observation["state"] != "succeeded" or not observation["final_accounting"]
                or not measured_model(session, producer, observation)):
            continue
        output = observation.get("output")
        if type(output) is not dict:
            continue
        producer_row = attempts[identifier]
        if "repair_parent_inventory" in producer_row:
            if producer_row.get("target_claim_id") != current_claim["id"]:
                continue
            from .repair_surface import project_proposal
            try:
                projected, proof = project_proposal(producer_row["repair_parent_inventory"], producer_row["repair_parent_inventory"],
                    output.get("proposal"), target_claim_id=producer_row["target_claim_id"],
                    worker_before=producer.input_document["repair_scope"]["before_inventory"])
            except (ValueError, EvidenceWikiError, KeyError, TypeError):
                continue
            if projected != application["proposal"] or proof != application.get("repair_projection"):
                continue
        elif output.get("proposal") != application["proposal"]:
            continue
        return digest(producer.document())
    fail("counter_actual_generator_provenance_unknown")
