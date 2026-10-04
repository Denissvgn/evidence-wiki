"""Metered native calculation reads share the canonical execution journal."""

from evidence_wiki import execution_session as native
from evidence_wiki._pack_io import yaml_document
from evidence_wiki.computation import evaluate
from evidence_wiki.mutation_contracts import validate_request_id
from evidence_wiki.pack_discovery import owner
from evidence_wiki.research_session import ResearchSession

from .calculation_contracts import checked_result, choose, refuse


def scope_inputs(session):
    """Refuse unselected inputs before any result can enter the native journal."""
    view = native.current(session)
    permitted = {row["source_id"] for row in view["execution"]["source_bindings"]}
    revision = owner("_claim_diagnostics")._capture(session.root, session.context)
    config = yaml_document(revision.files["research.yml"])
    declared = config.get("computation")
    if declared is None:
        refuse("quantitative_computation_not_configured")
    owner("_computation_contract").declaration(declared, config)
    manifest = {}
    from evidence_wiki._pack_io import json_document
    relative = config.get("sources", {}).get("manifest_path", "sources/manifest.jsonl")
    session.context.require_path(session.root / relative)
    for line in revision.files.get(relative, b"").splitlines():
        if line.strip():
            row = json_document(line)
            if row["id"] in manifest:
                refuse("calculation_manifest_identity_invalid")
            manifest[row["id"]] = row
    from fnmatch import fnmatchcase
    from pathlib import PurePosixPath
    normalized = config.get("sources", {}).get("normalized_dir", "sources/normalized")
    selectors = [item["selector"] for item in declared["aggregations"].values()]
    selectors += [item["selector"] for item in declared["invariants"].values() if item["selector"] is not None]
    for selector in selectors:
        pattern = PurePosixPath(selector["source_glob"])
        if pattern.parent.as_posix() != normalized:
            refuse("calculation_source_outside_run_scope")
        selected = {source_id for source_id, row in manifest.items() if row.get("status") not in {"rejected", "superseded"}
            and fnmatchcase(owner("normalize_sources").safe_source_id(source_id) + ".md", pattern.name)}
        if not selected <= permitted:
            refuse("calculation_source_outside_run_scope")
    for relative in revision.files:
        session.context.require_path(session.root / relative)
    return permitted


def current(session, *, request_id, as_of=None):
    """Reproduce the deterministic result; history never certifies current inputs."""
    if type(session) is not ResearchSession or session.execution is None:
        refuse("bound_execution_session_required")
    request_id = validate_request_id(request_id)
    session._check("calculate", write=True)
    session.context.require_grant("calculation_session.current")
    with native.gate(session):
        document = native._begin(session)
        if document["state"]["current"] in owner("run_controller").TERMINAL_STATES:
            identifier = native.child_id(request_id, "calculation")
            if not any(row["kind"] == "execution_native_observation"
                    and row["data"]["observation"]["attempt_id"] == identifier
                    and row["data"]["error_code"] is None for row in native.retained(session)):
                refuse("calculation_run_terminal")
    permitted = scope_inputs(session)
    arguments = {"as_of":as_of}

    def compute(_control):
        scope_inputs(session)
        value = checked_result(evaluate(session.root, as_of=as_of))
        if not {row["source_id"] for row in value["sources"]} <= permitted:
            refuse("calculation_source_outside_run_scope")
        return value

    recorded = native._native_call(session, native.child_id(request_id, "calculation"), "computation.check", arguments,
        compute, recover=None)
    # A retained return proves only the historical call. Fresh deterministic
    # reproduction must match before constructing new claim references.
    live = checked_result(evaluate(session.root, as_of=as_of))
    if live["result_id"] != recorded["result_id"]:
        refuse("current_computation_changed")
    permitted = scope_inputs(session)
    if not {row["source_id"] for row in live["sources"]} <= permitted:
        refuse("calculation_source_outside_run_scope")
    return live


def select(session, *, request_id, proposal, as_of=None):
    """Reduce interpretation against fresh owner values, never model arithmetic."""
    result = current(session, request_id=request_id, as_of=as_of)
    return choose(result, proposal)


def selected(session, *, original_id, result_id, records=None):
    """Project only authenticated known-final formula choices for this original."""
    if result_id is None:
        return []
    records = native.retained(session) if records is None else records
    attempts = {native.digest(row["data"]["attempt"]): row["data"] for row in records
        if row["kind"] == "execution_attempt" and row["data"]["purpose"] == "calculation"
        and row["data"]["original_id"] == original_id}
    selections = []
    for row in records:
        if row["kind"] != "execution_calculation_selection" or row["data"]["selection"]["reference"]["result_id"] != result_id:
            continue
        prior = attempts.get(row["data"]["attempt_ref"])
        if prior is None:
            continue
        attempt = session.execution.attempt(prior["attempt"])
        observation = native._latest_observation(records, attempt.attempt_id)
        if observation is None:
            refuse("calculation_intent_invalid")
        from .agent_execution import validate_observation
        observation = validate_observation(observation)
        role, job = session.execution.profile.role(attempt.config_ref), attempt.job
        expected = {"run_id":attempt.run_id, "binding_id":attempt.binding_id,
            "profile_sha256":attempt.profile_sha256, "attempt_id":attempt.attempt_id, "nonce":attempt.nonce,
            "job_id":job["id"], "job_sha256":attempt.job_sha256, "input_sha256":attempt.input_sha256,
            "controller_ref":role.controller_ref, "config_ref":role.config_ref, "model_ref":role.model_ref,
            "prompt_version":role.prompt_version, "issued_utc":job["issued_utc"], "deadline_utc":job["deadline_utc"],
            "currency":session.execution.profile.currency, "pricing_ref":session.execution.profile.pricing_ref}
        if any(observation[key] != value for key, value in expected.items()):
            refuse("calculation_intent_invalid")
        usage = observation.get("observed_usage") or {}
        if (observation["state"] != "succeeded" or not observation["final_accounting"]
                or type(usage.get("model_calls")) is not int or usage["model_calls"] <= 0
                or observation["started_utc"] is None or observation["reason"] == "offline_fixture"
                or session.execution.profile.role(attempt.config_ref).model_ref.startswith("offline/")
                or session.execution.profile.role_scopes.scope(attempt.config_ref).task_role != "calculation"
                or choose(prior["calculation_result"], observation["output"]) != row["data"]["selection"]):
            refuse("calculation_intent_invalid")
        if row["data"]["selection"] not in selections:
            selections.append(row["data"]["selection"])
    return selections


def project_intent(session, view, observed, records):
    """Keep native claim eligibility separate from declared workflow intent."""
    from .calculation_contracts import require_selected_references
    from .errors import UsageError
    result_id = (observed.computation or {}).get("result_id")
    missing = []
    for original in view["accounting"]["original_outcomes"]:
        selections = selected(session, original_id=original["original_id"], result_id=result_id, records=records)
        slugs = {question["slug"] for question in original["questions"]}
        claims = [row["claim"] for row in observed.strict.get("result", {}).get("claims", [])]
        try:
            require_selected_references({"inventory":{"claims":claims}}, selections, slugs)
        except UsageError:
            original["accepted"] = False
            for question in original["questions"]:
                question["accepted"] = False
                question["gaps"] = sorted(set(question["gaps"]) | {"selected_calculation_reference_missing"})
            missing.append(original["original_id"])
    if missing:
        accounting = view["accounting"]
        accounting["accepted_count"] = sum(row["accepted"] for row in accounting["original_outcomes"])
        accounting["unresolved_count"] = len(accounting["original_outcomes"]) - accounting["accepted_count"]
        release = view["canonical_release"]
        release["research_complete"] = False
        release["status"] = "partial" if accounting["accepted_count"] and session.policy.to_dict()["source_rules"]["allow_partial"] else "incomplete"
        release["gaps"] = sorted(set(release["gaps"]) | {"selected_calculation_reference_missing:" + identifier for identifier in missing})
        view["limitations"].append("Native publication retains its claim verdicts; the session also requires its declared quantitative inference.")


def stop(session, *, original_ids):
    """Recheck exact current intent immediately before terminal/resolution writes."""
    from .calculation_contracts import require_selected_references
    from .research_observation import ResearchObservation
    observed = ResearchObservation(session.root, run_id=session.run_id)
    try:
        mapping = observed.mapping.get("result") or {}
        records = native.retained(session)
        claims = [row["claim"] for row in observed.strict.get("result", {}).get("claims", [])]
        for identifier in original_ids:
            original = next(row for row in mapping.get("originals", []) if row["id"] == identifier)
            slugs = {mapping["expected"][qid] for qid in original["question_ids"]}
            selections = selected(session, original_id=identifier, result_id=(observed.computation or {}).get("result_id"), records=records)
            require_selected_references({"inventory":{"claims":claims}}, selections, slugs)
    finally:
        observed.finish()
