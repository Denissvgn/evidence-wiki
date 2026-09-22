"""Separate projected routes, caller access declarations, and evidence gaps."""

from __future__ import annotations

import os

from ._pack_io import canonical
from .host_capabilities import read_tools, scope_allows
from .pack_discovery import owner
from .planning_contracts import blocker, owned_call, refuse
from .planning_inputs import local_input
from .source_inspection import normalizer_views, provider_views


def source_plan(request, config, blockers):
    payload, decisions = request["request"]["payload"], request["decisions"]
    tools = read_tools(canonical(decisions["host_tools"]))
    authorized = payload["authority"]["allowed_actions"]
    scopes = payload["authority"]["source_scope"]
    for value in scopes:
        if "://" in value:
            owned_call("/request/payload/authority/source_scope", owner("_host_capture").origin, value)
    authority_tool = {"scope": [{"kind": "uri_prefix", "value": value} for value in scopes if "://" in value]}
    views, inventory = provider_views(config, True)
    normalizers = normalizer_views(config)
    requirements = {row["source_id"]: row for row in decisions["source_requirements"]}
    routes, inputs = [], []
    sources_owner = owner("source_requests")
    context = sources_owner.acquisition_plan_context(config)
    for source in payload["sources"]:
        sid, qids = source["id"], source["question_ids"]
        requirement = requirements.get(sid)
        if requirement is None:
            blockers.append(blocker("source_capture_requirements_missing", questions=qids, field="/decisions/source_requirements"))
        else:
            normalized_scope = owner("_request_scope").normalize_scope(requirement["scope"])
            if len(normalized_scope) != len(requirement["scope"]):
                refuse("/decisions/source_requirements/scope")
        if source["kind"] == "local_file":
            observation = local_input(source["locator"], scopes, remaining=33_554_432 - sum(row.get("bytes", 0) for row in inputs))
            inputs.append({"source_id": sid, **observation})
            gaps = [] if observation["state"] == "present" else ["local_source_" + observation["state"]]
            if observation.get("bytes", 0) > payload["budgets"]["bytes"]:
                gaps.append("local_delivery_byte_budget_exceeded")
            destination = next((root for root in decisions["raw_roots"] if root != "raw/links"), None)
            if destination is None:
                gaps.append("local_delivery_raw_root_missing")
            routes.append({"source_id": sid, "question_ids": qids, "phase": "delivery", "route": "local_file",
                "state": "blocked" if gaps else "planned", "input": observation, "gaps": gaps,
                "requirements": requirement, "evidence_usability": "not_inspected", "operation": "copy_then_inventory_and_normalize",
                "destination_root": destination})
            for gap in gaps:
                blockers.append(blocker(gap, questions=qids, stage="research", field="/request/payload/sources/" + sid))
            continue
        locator = source["locator"]
        if "://" in locator:
            owned_call("/request/payload/sources/locator", owner("_host_capture").origin, locator)
        owned_call("/request/payload/sources/kind", owner("_request_kinds").validate_kind, source["kind"], config)
        scope_permitted = "://" in locator and scope_allows(authority_tool, locator)
        candidates = []
        if source["kind"] == "paper":
            _, candidates, _ = sources_owner.plan_routes_for_request(
                {"request_id": sid, "kind": "paper", "query_or_identifier": locator, "status": "open"}, context)
        elif source["kind"] == "code" and sources_owner.is_github_url(locator):
            candidates = [sources_owner.candidate_acquisition_route({"url": locator, "source_type": "code_repository"}, context, sid)]
        elif locator.startswith("https://") and source["kind"] in {"web", "other"}:
            candidates = [{"provider": "web", "route": "get", "allowed_by_config": sources_owner.provider_allowed(context, "web")}]
        start = len(routes)
        for phase in ("discovery", "acquisition"):
            for provider in config["integrations"][phase]["providers"]:
                matching = [item for item in candidates if item["provider"] == provider] if phase == "acquisition" else [None]
                for candidate in matching:
                    gaps = []
                    if not scope_permitted:
                        gaps.append("source_scope_requires_explicit_authority")
                    if phase not in authorized:
                        gaps.append("source_action_not_authorized")
                    capability = next((row for row in views if row["phase"] == phase and row["id"] == provider), {})
                    if capability.get("authorized") != "configuration_allowed":
                        gaps.append("provider_not_qualified")
                    if phase == "acquisition" and candidate and not candidate.get("allowed_by_config"):
                        gaps.append("acquisition_not_configured")
                    if provider == "web":
                        fetch = owner("fetch_sources")
                        try:
                            settings = fetch.web_config(fetch.acquisition_config(config))
                            fetch.validate_https_url(locator, allowed_domains=settings["allowed_domains"], resolve_hostnames=False)
                        except (Exception, SystemExit):
                            gaps.append("web_origin_not_allowed")
                        if phase == "acquisition" and requirement and requirement["output_format"] != "html":
                            gaps.append("capture_format_mismatch")
                    if phase == "discovery" and provider not in ({"github"} if source["kind"] == "code" else {"arxiv", "openalex"} if source["kind"] == "paper" else set()):
                        continue
                    # Canonical request IDs will be assigned only by source intake.
                    # A hint ID is never written into fulfillment/accounting records.
                    routes.append({"source_id": sid, "question_ids": qids, "phase": phase, "route": "built_in", "provider": provider,
                        "operation": candidate["route"] if candidate else "discover", "source_request_id": None,
                        "state": "blocked" if gaps else "planned_requires_reinspection", "gaps": gaps,
                        "locator": locator, "requirements": requirement, "connectivity": "not_probed", "executed": False})
        for tool in tools:
            if not {"capture", "export"}.intersection(tool["operations"]):
                continue
            gaps = []
            if "host_capture" not in authorized or not scope_permitted or not scope_allows(tool, locator):
                gaps.append("host_scope_or_authority_missing")
            if tool["authorization"] != "declared":
                gaps.append("host_permission_not_declared")
            if requirement is None or requirement["output_format"] not in tool["formats"]:
                gaps.append("host_capture_format_unavailable")
            if payload["budgets"]["bytes"] == 0 or payload["budgets"]["source_requests"] == 0 or tool["limits"]["max_requests"] == 0 or tool["limits"]["max_bytes"] == 0:
                gaps.append("host_capture_budget_exhausted")
            routes.append({"source_id": sid, "question_ids": qids, "phase": "delivery", "route": "host_capture", "tool_id": tool["id"],
                "version": tool["version"], "state": "blocked" if gaps else "planned_host_action_required", "gaps": gaps,
                "requirements": requirement, "access": "not_verified", "capture_demonstrated": False, "executed": False})
        available = [row for row in routes[start:] if row["state"] != "blocked"]
        if not available:
            blockers.append(blocker("source_route_unavailable", questions=qids, stage="research", field="/request/payload/sources/" + sid))
    for question in [*payload["questions"], *payload["derived_questions"]]:
        if not any(question["id"] in source["question_ids"] for source in payload["sources"]):
            blockers.append(blocker("question_sources_unspecified", questions=[question["id"]], stage="research", field="/request/payload/sources"))
    references = [value.removeprefix("env:") for value in payload["authority"]["credential_references"]]
    return {"routes": routes, "local_inputs": inputs, "providers": views, "provider_inventory": inventory, "normalization": normalizers,
        "credentials": [{"reference": "env:" + name, "present": bool(os.environ.get(name)), "access_verified": False} for name in references],
        "budgets": {"requested": payload["budgets"], "host_tokens": decisions["host_token_limit"],
            "library_controls": {"questions": "run.max_questions_per_run", "source_requests": "run.max_source_requests_per_run",
                "academic_requests": "run.max_academic_provider_requests_per_run", "downloads": "integrations.acquisition.max_downloads_per_run"},
            "bytes": "per-artifact web/GitHub/registered limits; aggregate bytes require host enforcement",
            "seconds_and_tokens": "host_owned_not_observed_or_enforced", "reserved": False,
            "zero_budget": "routes disabled; positive-only owner configuration uses one as inert minimum"},
        "source_access_verified": False, "host_delivery_guaranteed": False, "actions_executed": False}
