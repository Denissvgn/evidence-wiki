"""Finite current host observations outside every coordination gate."""

from __future__ import annotations

import secrets
import time
from datetime import datetime, timedelta, timezone

from .finite_domain import observe_domain
from .mutation_contracts import detached, digest, validate_request_id
from .research_execution import bounded_host_call, now_utc

_CONFIG_FIELDS = frozenset({"execution_profile_sha256", "config_ref", "controller_ref", "model_ref",
    "prompt_version", "model_revision", "model_settings_sha256", "rubric_sha256", "suite_sha256", "split_ref",
    "reference_assessment_ref", "domain_descriptor_sha256", "active", "execution_kind"})


def configuration_document(value):
    """Reduce only a closed inert configuration, never host secrets or keys."""
    if type(value) is not dict or set(value) not in (_CONFIG_FIELDS, _CONFIG_FIELDS | {"domains"}):
        raise ValueError("qualification_configuration_invalid")
    for key in _CONFIG_FIELDS - {"active"}:
        if type(value[key]) is not str or not 1 <= len(value[key]) <= 256:
            raise ValueError("qualification_configuration_invalid")
    if type(value["active"]) is not bool or value["execution_kind"] not in {"model", "offline", "unavailable"}:
        raise ValueError("qualification_configuration_invalid")
    if "domains" in value and (type(value["domains"]) is not list or len(value["domains"]) > 16
            or any(type(item) is not str or not 1 <= len(item) <= 256 for item in value["domains"])):
        raise ValueError("qualification_configuration_invalid")
    return detached(value)


def domain_observation(session):
    """Capture canonical current domain inputs; no arbitrary host callbacks."""
    from . import execution_session as driver
    from .research_observation import ResearchObservation

    view = driver.current(session)
    observed = ResearchObservation(session.root, run_id=session.run_id if driver.retained(session) else None)
    sources, _ = driver._sources(session, observed)
    rubric = (observed.policy or {}).get("rubric", {})
    result = observe_domain(originals=view["accounting"]["original_outcomes"],
        strict_claims=[row["claim"] for row in observed.strict.get("result", {}).get("claims", [])],
        rubric=rubric, sources=sources)
    observed.finish()
    if driver.current(session)["basis_id"] != view["basis_id"]:
        return {**result, "state": "unknown", "semantic_basis_id": None, "reason": "domain_capture_changed"}
    return result


def refresh(session, request_id):
    """Witness current selected host configuration, then persist exact correlation.

    The host's qualification_configuration(request) service must echo its exact
    nonce/profile/registry/semantic-basis request, configuration and active state.
    Missing or late observations never provide qualification. This finite
    snapshot cannot promise instantaneous future service withdrawal.
    """
    from . import execution_session as driver

    plan = session.execution.profile.calibration
    if plan is None:
        return
    request_id = validate_request_id(request_id)
    with driver.gate(session):
        session.context.require_grant("research_session.intake_observation")
        session.context.require_grant("research_session.host_job.evaluate")
        driver.require_machine_compatible(session)
        driver._begin(session)
        if (datetime.now(timezone.utc) >= driver.deadline(session)
                or driver.execution_budget_overrun(session, driver.retained(session))):
            driver.refuse("BUDGET_EXHAUSTED")
    domain = domain_observation(session)
    now = datetime.now(timezone.utc)
    expires = min(now + timedelta(seconds=plan.probe_lifetime_seconds), driver.deadline(session))
    probe = {"schema_version": "research-qualification-probe/v1", "profile_sha256": session.execution.profile.sha256,
        "registry_sha256": session.execution.qualifications.sha256,
        "semantic_basis_id": domain["semantic_basis_id"], "observed_utc": now.isoformat(),
        "expires_utc": expires.isoformat(), "configurations": {}, "reasons": []}
    function = getattr(session.execution.executor, "qualification_configuration", None)
    until = time.monotonic() + min(plan.probe_seconds, max(0, (expires - now).total_seconds()))
    if domain["state"] != "observed":
        probe["reasons"].append("domain_unqualified")
    elif not callable(function):
        probe["reasons"].append("qualification_service_unavailable")
    else:
        optional = getattr(session.execution.profile, "disagreement", None)
        auxiliary = None if optional is None else optional.adjudicator_config_ref
        service_routes = session.execution.profile.review_services is not None
        if auxiliary is not None or service_routes:
            probe["configuration_reasons"] = {}
        for selection in sorted(plan.selections, key=lambda row: (row.config_ref == auxiliary, row.config_ref)):
            def failure(reason, selection=selection):
                if selection.config_ref == auxiliary or service_routes:
                    probe["configuration_reasons"].setdefault(selection.config_ref, []).append(reason)
                    return False
                probe["reasons"].append(reason)
                return True
            remaining = min(until - time.monotonic(), session.execution.profile.cleanup_timeout_seconds)
            request = {"schema_version": "research-qualification-probe-request/v1", "nonce": secrets.token_hex(16),
                "config_ref": selection.config_ref, "profile_sha256": probe["profile_sha256"],
                "registry_sha256": probe["registry_sha256"], "semantic_basis_id": probe["semantic_basis_id"],
                "expires_utc": probe["expires_utc"]}
            returned, reason = bounded_host_call(function, (detached(request),), remaining)
            if reason is not None:
                if failure(reason):
                    break
                continue
            try:
                returned = detached(returned)
                if (type(returned) is not dict or set(returned) != {"request", "configuration"}
                        or returned["request"] != request or type(returned["configuration"]) is not dict):
                    if failure("qualification_probe_uncorrelated"):
                        break
                    continue
                probe["configurations"][selection.config_ref] = configuration_document(returned["configuration"])
            except (Exception, SystemExit):
                if failure("qualification_probe_invalid"):
                    break
    with driver.gate(session):
        current_domain = domain_observation(session)
        if current_domain["semantic_basis_id"] != probe["semantic_basis_id"]:
            probe["configurations"] = {}
            probe["reasons"].append("domain_changed_during_probe")
        if datetime.now(timezone.utc) >= expires:
            probe["configurations"] = {}
            probe["reasons"].append("qualification_probe_expired")
        if driver.execution_budget_overrun(session, driver.retained(session)):
            probe["configurations"] = {}
            probe["reasons"].append("qualification_budget_exhausted")
        driver.append(session, "execution_qualification_probe", driver.child_id(request_id, "qualification." + secrets.token_hex(8)),
            {"probe": probe, "domain_ref": digest(current_domain), "completed_utc": now_utc()})
