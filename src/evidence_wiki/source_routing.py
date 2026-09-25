"""Requirement-accounted source choices through canonical request/transport owners."""

from __future__ import annotations

import hashlib
import sys
from decimal import Decimal

from ._pack_io import canonical, json_document, relative_path
from ._script_host import shared_assets_root
from .host_capabilities import known_credentials, read_tools, scope_allows
from .pack_discovery import owner
from .source_contracts import ROUTES, decode, refuse, reject_execution_and_secrets
from .source_inputs import SourceView
from .source_inspection import inspect as inspect_capabilities
from .source_probe import provider_probe


def _normalizer(format_name, inspection):
    native = {"markdown": "host_text", "plain_text": "host_text", "html": "html", "pdf": "pdf", "docx": "docx", "csv": "table", "url": "link_stub"}
    if format_name in native:
        return native[format_name]
    kind = "structured_data" if format_name == "json" else "image"
    for adapter in inspection["normalization"]["adapters"]:
        if kind in adapter["kinds"] and adapter["executable_present"]:
            return "adapter:" + adapter["name"]
    return None


def _command(view, script, arguments):
    if view.workspace != "present":
        return None, "workspace_required"
    relative = "scripts/" + script + ".py"
    actual = view.read(relative)
    expected = shared_assets_root() / "workspace-template" / relative
    if actual is None:
        return None, "workspace_script_missing"
    if hashlib.sha256(actual).digest() != hashlib.sha256(expected.read_bytes()).digest():
        return None, "workspace_tooling_differs_from_installation"
    data_options = {"--query", "--url", "--id", "--id-or-doi", "--request-id", "--request-file", "--provider", "--source-type"}
    escaped, position = [], 0
    while position < len(arguments):
        item = arguments[position]
        if item in data_options and position + 1 < len(arguments):
            value = arguments[position + 1]
            escaped.extend([item, value] if not value.startswith("-") else [item + "=" + value])
            position += 2
        else:
            escaped.append(item)
            position += 1
    return [sys.executable, str(view.root / relative), "--project-root", str(view.root), *escaped], None


def _remediation(reason, requirement, route=None):
    catalog = {
        "workspace_required": ("workspace", "Initialize the selected project through evidence-wiki init before executing workspace routes."),
        "workspace_script_missing": ("workspace", "Restore workspace tooling through the existing upgrade owner."),
        "workspace_tooling_differs_from_installation": ("workspace", "Inspect the tooling revision and use the existing upgrade workflow when appropriate."),
        "provider_not_authorized": ("configuration", "Review the requested provider against task scope and explicitly configure its corresponding discovery/acquisition allow-list if authorized."),
        "provider_not_installed_or_qualified": ("provider", "Use a built-in or host-delivery alternative, or explicitly probe the selected installed registration."),
        "registered_request_not_validated": ("provider", "Explicitly probe this registered provider with the supplied request; a kind declaration alone is insufficient."),
        "registered_request_refused": ("provider", "Correct the request through the provider's existing validate_request contract or choose another route."),
        "normalizer_unavailable": ("format", "Use a supported capture format or a reviewed configured normalizer adapter; do not invent a package dependency."),
        "normalizer_configuration_invalid": ("configuration", "Correct the normalization declaration through its canonical configuration contract."),
        "budget_insufficient": ("budget", "Reduce the requested work or resolve the material budget limit before execution."),
        "host_scope_not_declared": ("host_access", "Use a host tool with an explicitly declared scope covering this source, or identify an accessible source."),
        "host_permission_not_declared": ("host_access", "Resolve access in the selected host tool; credential names and installed tools do not grant permission."),
        "source_location_unresolved": ("source_input", "Discover or identify the source location before requesting its capture."),
        "source_not_ready": ("source", "Complete the selected source's missing ingestion step; healthy sources may proceed independently."),
        "request_kind_unavailable": ("request", "Use a supported source-request kind or a declared pack kind with an implemented route."),
        "web_origin_not_allowed": ("configuration", "Review this origin and the existing web allow-list, or use approved host delivery."),
        "dependency_missing": ("python_dependency", "Restore the required EvidenceWiki dependency in the selected Python environment."),
        "source_scope_unconfirmed": ("source_input", "Confirm the selected source's scope before using it for this requirement."),
        "capture_format_mismatch": ("format", "Select a route for the requested capture format or explicitly revise that requirement."),
        "provider_request_file_required": ("provider", "Store the validated JSON request at an authorized workspace-relative path and select that path before execution."),
        "source_request_required": ("request", "Create or select an open source request through the existing source-request owner before request-bound discovery."),
    }
    kind, action = catalog.get(reason, ("source", "Inspect this source's recorded reason and choose a supported delivery or extraction route."))
    return {"reason": reason, "kind": kind, "action": action, "requirement_ids": [requirement["id"]],
            "question_ids": requirement["question_ids"], "route_id": route,
            "material_input": kind in {"budget", "host_access", "source_input"}, "executed": False}


def _base(requirement, route_id, kind):
    return {"id": route_id, "kind": kind, "requirement_id": requirement["id"], "question_ids": requirement["question_ids"],
            "state": "blocked", "gaps": [], "command_argv": None, "network_executed": False,
            "capture": "not_demonstrated", "semantic_adequacy": "not_evaluated", "evidence_accepted": False}


def _budget(route, budget, requests=1):
    route["budget"] = {"requests_lower_bound": requests, "max_requests": budget["max_requests"], "max_bytes": budget["max_bytes"],
                       "max_cost_usd": budget["max_cost_usd"], "cost_observation": "unknown", "reservation": "not_performed",
                       "execution_guard": "the executing owner must recheck total requests, bytes and cost"}
    if budget["max_requests"] < requests or budget["max_bytes"] <= 0:
        route["gaps"].append("budget_insufficient")


def _request_binding(view, requirement):
    identifier = requirement["source_request_id"]
    if identifier is None:
        return False
    path = view.generated_path("source_requests_path", owner("source_requests").DEFAULT_REQUESTS_PATH)
    raw = view.read(path, 8_388_608)
    if raw is None:
        return False
    lines = raw.splitlines()
    if len(lines) > 4096:
        refuse("source_requests_bound", "ONBOARDING_LIMIT")
    records = [json_document(line) for line in lines if line.strip()]
    matches = [row for row in records if isinstance(row, dict) and row.get("request_id") == identifier]
    return (len(matches) == 1 and matches[0].get("status") == "open" and matches[0].get("kind") == requirement["kind"])


def _without_unbound_request(arguments, bound):
    if bound:
        return arguments
    result, skip = [], False
    for part in arguments:
        if skip:
            skip = False
        elif part == "--request-id":
            skip = True
        else:
            result.append(part)
    return result


def plan(raw, *, target=None, host_tools=None, probe_providers=()):
    request = decode(ROUTES, raw)
    reject_execution_and_secrets(request, credential_values=known_credentials())
    ids = [row["id"] for row in request["requirements"]]
    if len(ids) != len(set(ids)):
        refuse("source_requirement_ids_duplicate")
    if any(row["requirement_id"] not in ids for row in request["registered_requests"]):
        refuse("provider_requirement_unknown")
    selectors = [(row["requirement_id"], row["phase"], row["provider_id"]) for row in request["registered_requests"]]
    if len(selectors) != len(set(selectors)):
        refuse("provider_request_duplicate")
    tools = read_tools(host_tools)
    if set(request["preferred_tools"]) - {tool["id"] for tool in tools}:
        refuse("preferred_host_tool_unknown")
    view = SourceView(target)
    source_ids = sorted({source for item in request["requirements"] for source in item["source_ids"]})
    inspection = inspect_capabilities(target=target, host_tools=host_tools, source_ids=source_ids, _view=view)
    providers = {(row["phase"], row["id"]): row for row in inspection["providers"]}
    registered_keys = {f"{row['phase']}:{row['registration']}" for row in request["registered_requests"]}
    if set(probe_providers) - registered_keys:
        refuse("provider_probe_requires_selected_request")
    source_map = {row["source_id"]: row for row in inspection["sources"]}
    manifest = view.manifest() if source_ids and view.workspace == "present" else {}
    requests_owner = owner("source_requests")
    acquisition = requests_owner.acquisition_plan_context(view.config)
    rows, outcomes, remediation = [], [], []
    budget = request["budget"]
    remaining = budget["max_requests"]
    for requirement in request["requirements"]:
        start = len(rows)
        bound = _request_binding(view, requirement)
        request_id = requirement["source_request_id"] if bound else requirement["id"]
        try:
            owner("_request_kinds").validate_kind(requirement["kind"], view.config)
            kind_valid = True
        except Exception:
            kind_valid = False
        for source_id in requirement["source_ids"]:
            row = _base(requirement, f"{requirement['id']}/source/{len(rows)}", "local_source")
            source = source_map[source_id]
            row.update(source_id=source_id, readiness=source, capture=source["delivery"])
            if source["usability"] not in {"usable", "partial"} or source.get("evidence_usable") is False or requirement["needs_complete"] and not source["complete"]:
                row["gaps"].append("source_not_ready")
            profile = source.get("host_capture")
            if profile is not None and profile["content_kind"] not in requirement["content_kinds"]:
                row["gaps"].append("source_not_ready")
            observed_format = profile["content_format"] if profile else {"html": "html", "pdf": "pdf", "docx": "docx", "table": "csv", "structured_data": "json"}.get(source.get("kind"))
            if observed_format != requirement["output_format"]:
                row["gaps"].append("capture_format_mismatch")
            provenance = manifest.get(source_id, {}).get("provenance") or {}
            conflicts, absences = owner("_request_scope").scope_match(requirement["scope"], provenance.get("scope", {}))
            if conflicts or absences:
                row["gaps"].append("source_scope_unconfirmed")
            row["state"] = "usable_for_caller_review" if not row["gaps"] else "blocked"
            rows.append(row)
        query = requirement["query_or_identifier"]
        canonical_routes = []
        if kind_valid and requirement["kind"] == "paper":
            _, canonical_routes, _ = requests_owner.plan_routes_for_request(
                {"request_id": request_id, "kind": "paper", "query_or_identifier": query, "status": "open"}, acquisition)
        elif kind_valid and requirement["kind"] == "code" and requests_owner.is_github_url(query):
            canonical_routes = [requests_owner.candidate_acquisition_route({"url": query, "source_type": "code_repository"}, acquisition, request_id)]
        elif kind_valid and requirement["kind"] in {"web", "other"} and query.startswith("https://"):
            owner("_host_capture").origin(query)
            canonical_routes = [{"provider": "web", "route": "get", "allowed_by_config": requests_owner.provider_allowed(acquisition, "web"),
                "command_argv": ["python3", "scripts/fetch_sources.py", "--format", "json", "web", "get", "--url", query,
                                 "--source-type", "web_page", "--request-id", request_id]}]
        for candidate in canonical_routes:
            row = _base(requirement, f"{requirement['id']}/builtin/{len(rows)}", "built_in")
            provider = candidate["provider"]
            known = providers.get(("acquisition", provider))
            row.update(provider=provider, operation=candidate["route"], phase="acquisition", connectivity="not_probed")
            row["stage"] = "capture" if candidate["route"] in {"download-source", "get"} and provider != "openalex" else "identify_source"
            if not known or known["authorized"] != "configuration_allowed" or not candidate.get("allowed_by_config"):
                row["gaps"].append("provider_not_authorized")
            if provider == "web":
                try:
                    fetch = owner("fetch_sources")
                    settings = fetch.web_config(fetch.acquisition_config(view.config))
                    fetch.validate_https_url(query, allowed_domains=settings["allowed_domains"], resolve_hostnames=False)
                except (Exception, SystemExit):
                    row["gaps"].append("web_origin_not_allowed")
            arguments = _without_unbound_request(candidate.get("command_argv", [])[2:], bound)
            row["source_request_id"] = requirement["source_request_id"] if bound else None
            row["command_argv"], gap = _command(view, "fetch_sources", arguments)
            if gap:
                row["gaps"].append(gap)
            row["normalizer"] = "latex/pdf" if candidate["route"] == "download-source" else "html" if provider == "web" else "capture_selection_required"
            if row["stage"] == "capture" and requirement["output_format"] not in ({"pdf", "markdown", "plain_text"} if candidate["route"] == "download-source" else {"html"}):
                row["gaps"].append("capture_format_mismatch")
            installed = {item["distribution"]: item["state"] for item in inspection["dependencies"]}
            if row["normalizer"] == "latex/pdf" and installed.get("pypdf") != "installed":
                row["gaps"].append("dependency_missing")
                row["dependency_reference"] = "pypdf"
            if inspection["normalization"]["configuration"] == "invalid_declaration":
                row["gaps"].append("normalizer_configuration_invalid")
            row["companion_commands"] = []
            for companion in candidate.get("companion_commands", []):
                command, companion_gap = _command(view, "fetch_sources", _without_unbound_request(companion["command_argv"][2:], bound))
                row["companion_commands"].append(command)
                if companion_gap:
                    row["gaps"].append(companion_gap)
            _budget(row, budget, 1 + len(row["companion_commands"]))
            row["state"] = "ready_to_attempt" if not row["gaps"] else "blocked"
            rows.append(row)
        for provider in (("github",) if requirement["kind"] == "code" else ("search",)):
            known = providers.get(("discovery", provider))
            if not kind_valid or known is None:
                continue
            row = _base(requirement, f"{requirement['id']}/discovery/{provider}", "built_in")
            row.update(provider=provider, phase="discovery", stage="identify_source", operation=provider,
                       connectivity="not_probed", normalizer="capture_selection_required")
            arguments = ["--format", "json", provider, "--query", query, "--max-results", "5"]
            if provider == "search":
                row["execution_mode"] = "query_plan_only"
                row["next_action"] = "Review the planned search queries and current configured search transport before explicit execution."
                discovery = view.config.get("integrations", {}).get("discovery", {})
                if not isinstance(discovery, dict) or discovery.get("enabled") is not True or known["configured"] == "invalid":
                    row["gaps"].append("provider_not_authorized")
            elif known["authorized"] != "configuration_allowed":
                row["gaps"].append("provider_not_authorized")
            row["command_argv"], gap = _command(view, "discover_sources", arguments)
            if gap:
                row["gaps"].append(gap)
            _budget(row, budget, 0 if provider == "search" else 1)
            row["state"] = "query_plan_available" if provider == "search" and not row["gaps"] else "ready_to_attempt" if not row["gaps"] else "blocked"
            rows.append(row)
        if kind_valid and requirement["kind"] == "paper":
            for provider in ("arxiv", "openalex"):
                row = _base(requirement, f"{requirement['id']}/academic/{provider}", "built_in")
                known = providers.get(("discovery", provider))
                row.update(provider=provider, phase="discovery", stage="identify_source", operation="academic",
                           normalizer="capture_selection_required", connectivity="not_probed")
                if not bound:
                    row["gaps"].append("source_request_required")
                if not known or known["authorized"] != "configuration_allowed":
                    row["gaps"].append("provider_not_authorized")
                if bound:
                    row["command_argv"], gap = _command(view, "discover_sources", ["--format", "json", "academic", "--request-id", request_id,
                        "--provider", provider, "--query", query, "--max-results", "5"])
                    if gap:
                        row["gaps"].append(gap)
                _budget(row, budget)
                row["state"] = "ready_to_attempt" if not row["gaps"] else "blocked"
                rows.append(row)
        for declared in request["registered_requests"]:
            if declared["requirement_id"] != requirement["id"]:
                continue
            row = _base(requirement, f"{requirement['id']}/registered/{len(rows)}", "registered_provider")
            provider, phase = declared["provider_id"], declared["phase"]
            known = providers.get((phase, provider))
            row.update(provider=provider, phase=phase, request=declared["request"], connectivity="not_probed", request_validation="not_run")
            if not known or known["kind"] != "registered_provider" or known["installed"] == "collision":
                row["gaps"].append("provider_not_installed_or_qualified")
            if not known or known["authorized"] not in {"configuration_allowed", "selected_registration_unverified"}:
                row["gaps"].append("provider_not_authorized")
            if f"{phase}:{declared['registration']}" in probe_providers:
                observation = provider_probe(phase, declared["registration"], declared["request"])
                row["probe"] = observation
                if observation.get("request_validation") != "passed" or observation.get("id") != provider:
                    row["gaps"].append("registered_request_refused")
                elif requirement["kind"] not in observation["capabilities"]["request_kinds"]:
                    row["gaps"].append("request_kind_unavailable")
                else:
                    row["request_validation"] = "passed"
                    row["gaps"] = [gap for gap in row["gaps"] if gap != "provider_not_installed_or_qualified"]
            else:
                row["gaps"].append("registered_request_not_validated")
            row["normalizer"] = _normalizer(requirement["output_format"], inspection)
            if row["normalizer"] is None:
                row["gaps"].append("normalizer_unavailable")
            row["execution_interface"] = {"script": "fetch_sources.py" if phase == "acquisition" else "discover_sources.py",
                "provider": provider, "request_file": declared["request_path"], "request_file_written": False,
                "owner_revalidates_request": True}
            request_path = declared["request_path"]
            if request_path is None or view.workspace != "present":
                row["gaps"].append("provider_request_file_required")
            else:
                relative_path(request_path)
                document = view.read(request_path)
                if document is None or json_document(document) != declared["request"]:
                    row["gaps"].append("provider_request_file_required")
                else:
                    command = "get" if phase == "acquisition" else "search"
                    script = "fetch_sources" if phase == "acquisition" else "discover_sources"
                    row["command_argv"], command_gap = _command(view, script,
                        ["--format", "json", "registered", command, "--id", provider, "--request-file", request_path])
                    if command_gap:
                        row["gaps"].append(command_gap)
            _budget(row, budget)
            row["state"] = "ready_to_attempt" if not row["gaps"] else "blocked"
            rows.append(row)
        for tool in tools:
            if request["preferred_tools"] and tool["id"] not in request["preferred_tools"]:
                continue
            if not {"capture", "export", "search"}.intersection(tool["operations"]):
                continue
            row = _base(requirement, f"{requirement['id']}/host/{len(rows)}", "host_delivery")
            formats = [item for item in (requirement["output_format"],) if item in tool["formats"]]
            row.update(tool_id=tool["id"], operation="capture" if "capture" in tool["operations"] else "export" if "export" in tool["operations"] else "search",
                       capture_format=formats[0] if formats else None, access="host_must_verify", authorization="caller_declared")
            if tool["authorization"] != "declared":
                row["gaps"].append("host_permission_not_declared")
            if "://" not in query:
                row["gaps"].append("source_location_unresolved")
            elif not scope_allows(tool, query):
                row["gaps"].append("host_scope_not_declared")
            if not formats or formats[0] not in {"markdown", "plain_text", "html", "pdf", "docx", "csv"}:
                row["gaps"].append("normalizer_unavailable")
            row["normalizer"] = _normalizer(formats[0], inspection) if formats else None
            row["delivery_schema"] = "evidence-host-delivery/v1" if formats and formats[0] in {"markdown", "plain_text"} else "existing_provenance_sidecar"
            bounded_budget = {"max_requests": min(budget["max_requests"], tool["limits"]["max_requests"]),
                "max_bytes": min(budget["max_bytes"], tool["limits"]["max_bytes"]),
                "max_cost_usd": str(min(Decimal(budget["max_cost_usd"]), Decimal(tool["limits"]["max_cost_usd"])))}
            _budget(row, bounded_budget)
            row["state"] = "host_action_required" if not row["gaps"] else "blocked"
            rows.append(row)
        candidates = rows[start:]
        if view.workspace == "present" and inspection["target"]["contract"]["state"] != "compatible":
            for row in candidates:
                row["gaps"].append("workspace_contract_incompatible")
                row["state"] = "blocked"
        if not kind_valid:
            for row in candidates:
                row["gaps"].append("request_kind_unavailable")
                row["state"] = "blocked"
        eligible = [row for row in candidates if row["state"] != "blocked"]
        eligible.sort(key=lambda row: (0 if row["kind"] == "local_source" else
            1 if row["kind"] == "host_delivery" and request["preferred_tools"] else
            2 if row.get("stage") == "capture" or row["kind"] in {"host_delivery", "registered_provider"} else
            4 if row["state"] == "query_plan_available" else 3))
        selected = next((row for row in eligible if row["kind"] == "local_source"), None)
        if selected is None:
            selected = next((row for row in eligible if row.get("budget", {}).get("requests_lower_bound", 1) <= remaining), None)
            if selected is not None:
                remaining -= selected["budget"]["requests_lower_bound"]
        gaps = sorted({gap for row in candidates for gap in row["gaps"]})
        if selected is None and not gaps:
            gaps = ["budget_insufficient" if eligible else "source_location_unresolved"]
        outcomes.append({"requirement_id": requirement["id"], "question_ids": requirement["question_ids"],
                         "selected_route": selected["id"] if selected else None,
                         "allowed_alternatives": [row["id"] for row in eligible if row is not selected],
                         "status": selected["state"] if selected else "blocked", "gaps": [] if selected else gaps})
        for row in candidates:
            remediation.extend(_remediation(gap, requirement, row["id"]) for gap in sorted(set(row["gaps"])))
            for reason in row.get("readiness", {}).get("reasons", []):
                action = _remediation(reason, requirement, row["id"])
                source_id = row.get("source_id")
                if reason == "normalization_not_run" and source_id:
                    action.update(action="Normalize only this source through the existing owner.", kind="normalization")
                    action["command_argv"], _ = _command(view, "normalize_sources", ["--source-id", source_id])
                elif reason == "ocr_required":
                    action.update(action="Obtain an accessible text document or use an already authorized OCR tool and retain its qualified capture.", kind="host_access", material_input=True)
                elif reason == "normalized_contract_failed" and source_id:
                    action.update(action="Inspect this record's canonical violations before restoring or re-normalizing it.", kind="normalization")
                    action["command_argv"] = [sys.executable, "-m", "evidence_wiki.cli", "normalize", "verify", "--target", str(view.root), "--source-id", source_id, "--format", "json"]
                elif reason == "partial_rendered_table_or_values":
                    action.update(action="Use the validated structured view for omitted values, or obtain a complete capture for this requirement.", kind="format")
                elif reason in {"host_capture_rights_unknown", "host_capture_rights_restricted"}:
                    action.update(action="Resolve this source's actual use permission with its owner; a capability or capture declaration cannot grant it.", kind="host_access", material_input=True)
                remediation.append(action)
        if not candidates:
            remediation.append(_remediation(gaps[0], requirement))
    if len(rows) > 128:
        refuse("source_route_bound", "ONBOARDING_LIMIT")
    return {"schema_version": "evidence-source-route-result/v1", "request_id": request["request_id"],
        "request_sha256": hashlib.sha256(canonical(request)).hexdigest(), "requirements": outcomes, "routes": rows,
        "remediation": remediation, "may_continue": [row["requirement_id"] for row in outcomes if row["status"] != "blocked"],
        "blocked": [row["requirement_id"] for row in outcomes if row["status"] == "blocked"], "observation": view.finish(),
        "budget": {"basis": "selected lower bounds; no reservation", "remaining_request_lower_bound": remaining},
        "providers_enabled": False, "research_ready": False, "semantic_adequacy": "not_evaluated"}
