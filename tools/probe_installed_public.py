"""Public installed interfaces and packaged, unattended synthetic host journeys."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import math
import os
import platform
import runpy
import sys
import time
from pathlib import Path

import evidence_wiki
from evidence_wiki import Onboarding, Workspace, contract

PUBLIC_CASES = (
    "exports", "contracts", "resources", "schema_documents", "core_dependencies",
    "cli_parity", "cli_research_refusal", "mcp_stdio", "legacy_vectors", "direct_host", "caller_adapter", "mcp_adapter",
)
CORE_DISTRIBUTIONS = ("evidence-wiki", "pypdf", "pyyaml", "ruamel-yaml", "tzdata")
INSTALLER_DISTRIBUTIONS = ("pip", "setuptools", "wheel")
CORE_REQUIREMENTS = ("pypdf>=6.14,<7", "PyYAML>=6.0", "ruamel.yaml>=0.19.1,<0.20", "tzdata>=2024.1")
PUBLIC_STAGE_SECONDS = 5400
PUBLIC_APPLICATIONS = ("public-direct-host", "public-current-caller", "public-mcp")
PUBLIC_FIXED_LIMITS = {"public-pip-check": 60, "public-cli-contract": 900, "public-cli-resources": 900,
                       "public-mcp-stdio": 60, "public-cli-runtime-refusal": 60}
PUBLIC_COMMAND_ORDER = ("public-pip-check", "public-cli-contract", "public-cli-resources", "public-mcp-stdio",
                        "public-direct-host", "public-cli-runtime-refusal", "public-current-caller", "public-mcp")


def supervision_contract():
    return {"schema_version": "public-stage-supervision/v1", "stage_seconds_maximum": PUBLIC_STAGE_SECONDS,
            "applications": list(PUBLIC_APPLICATIONS), "fixed_command_limits": dict(PUBLIC_FIXED_LIMITS),
            "command_order": list(PUBLIC_COMMAND_ORDER),
            "allocation": "one_shared_remaining_monotonic_deadline"}


def finite_number(value):
    return type(value) in (int, float) and math.isfinite(value)


class PublicStage:
    """One externally bounded stage; applications receive only its remaining time."""

    def __init__(self, start, deadline, *, clock=time.monotonic):
        require(finite_number(start) and finite_number(deadline) and start < deadline <= start+PUBLIC_STAGE_SECONDS,
                "Public stage deadline differs from frozen maximum")
        self.start, self.deadline, self.clock = start, deadline, clock
        self.entered = clock()
        require(finite_number(self.entered) and start <= self.entered < deadline, "Public stage expired before probe start")
        self.selections = []

    def select(self, label):
        instant = self.clock()
        require(finite_number(instant) and self.start <= instant < self.deadline, "Public stage expired before command start")
        remaining = self.deadline - instant
        require(label in PUBLIC_APPLICATIONS or label in PUBLIC_FIXED_LIMITS, "Public command has no frozen deadline rule")
        limit = remaining if label in PUBLIC_APPLICATIONS else min(PUBLIC_FIXED_LIMITS[label], remaining)
        selection = {"stage": label, "selected_monotonic": instant, "remaining_seconds": remaining,
                     "timeout_seconds": limit, "stage_start_monotonic": self.start, "stage_deadline_monotonic": self.deadline}
        self.selections.append(selection)
        return selection

    def run(self, runner, argv, *, label, **options):
        selection = self.select(label)
        return runner.run(argv, label=label, timeout=selection["timeout_seconds"],
                          deadline_monotonic=self.deadline, supervision=selection, **options)

    def finish(self):
        instant = self.clock()
        require(finite_number(instant) and self.entered <= instant <= self.deadline, "Public stage did not finish inside original deadline")
        return {"start_monotonic": self.start, "deadline_monotonic": self.deadline, "entered_monotonic": self.entered,
                "completed_monotonic": instant, "selections": self.selections}


def checked_supervision(report, *, parent_record=None, parent_stage=None):
    stage, records = report["supervision"], report["commands"]
    start, deadline = stage["start_monotonic"], stage["deadline_monotonic"]
    require(all(finite_number(value) for value in
                (start, deadline, stage["entered_monotonic"], stage["completed_monotonic"]))
            and start < deadline <= start+PUBLIC_STAGE_SECONDS
            and start <= stage["entered_monotonic"] <= stage["completed_monotonic"] <= deadline,
            "Public stage clock observations differ from frozen bound")
    require([row["stage"] for row in records] == list(PUBLIC_COMMAND_ORDER)
            and len(stage["selections"]) == len(records), "Public deadline selection inventory differs")
    previous = stage["entered_monotonic"]
    for selection, record in zip(stage["selections"], records, strict=True):
        name, instant = selection["stage"], selection["selected_monotonic"]
        require(all(finite_number(selection[key]) for key in ("selected_monotonic","remaining_seconds","timeout_seconds",
                    "stage_start_monotonic","stage_deadline_monotonic"))
                and name == record["stage"] and previous <= instant < deadline
                and selection["stage_start_monotonic"] == start and selection["stage_deadline_monotonic"] == deadline,
                "Public command reset or contradicted original stage deadline")
        remaining = deadline - instant
        limit = remaining if name in PUBLIC_APPLICATIONS else min(PUBLIC_FIXED_LIMITS[name], remaining)
        require(selection["remaining_seconds"] == remaining and selection["timeout_seconds"] == limit,
                "Public command timeout selection differs from remaining stage allowance")
        require(all(finite_number(record[key]) for key in ("budget_started_monotonic","started_monotonic","completed_monotonic",
                    "timeout_seconds","deadline_monotonic","effective_deadline_monotonic")), "Public receipt contains invalid clock or limit")
        budget_start = record["budget_started_monotonic"]
        effective_deadline = min(deadline, budget_start + limit)
        require(record["supervision"] == selection and record["deadline_monotonic"] == deadline
                and instant <= budget_start <= record["started_monotonic"] <= record["completed_monotonic"] <= effective_deadline
                and record["effective_deadline_monotonic"] == effective_deadline
                and record.get("seconds", 0) <= record["timeout_seconds"] + 0.001
                and record["timeout_seconds"] == min(limit, deadline-budget_start),
                "Actual public command receipt differs from its deadline selection")
        previous = record["completed_monotonic"]
    require(previous <= stage["completed_monotonic"], "Public stage completed before its last child")
    if parent_record is not None:
        require(all(finite_number(parent_record[key]) for key in ("budget_started_monotonic","started_monotonic","completed_monotonic",
                    "timeout_seconds","deadline_monotonic","effective_deadline_monotonic")), "Parent receipt contains invalid clock or limit")
        require(parent_stage is not None and parent_stage == {"start_monotonic": start, "deadline_monotonic": deadline}
                and parent_record["deadline_monotonic"] == deadline
                and start <= parent_record["budget_started_monotonic"] <= parent_record["started_monotonic"] <= stage["entered_monotonic"]
                and stage["completed_monotonic"] <= parent_record["completed_monotonic"] <= deadline,
                "Public child stage differs from original parent supervision")
        parent_selection = parent_record["supervision"]
        require(all(finite_number(parent_selection[key]) for key in ("selected_monotonic","remaining_seconds","timeout_seconds",
                    "stage_start_monotonic","stage_deadline_monotonic"))
                and parent_selection["stage_start_monotonic"] == start and parent_selection["stage_deadline_monotonic"] == deadline
                and parent_selection["selected_monotonic"] == start
                and 0 < parent_selection["timeout_seconds"] <= PUBLIC_STAGE_SECONDS
                and parent_selection["remaining_seconds"] == parent_selection["timeout_seconds"]
                and deadline == start+parent_selection["timeout_seconds"]
                and parent_record["effective_deadline_monotonic"] == min(deadline,parent_record["budget_started_monotonic"]+parent_selection["timeout_seconds"])
                and parent_record["timeout_seconds"] == min(parent_selection["timeout_seconds"],deadline-parent_record["budget_started_monotonic"]),
                "Parent public stage timeout differs from original finite allowance")
    return stage


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def public_expectations():
    """Freeze source-owned declarations before either installed artifact is executed."""
    from evidence_wiki.agent_execution import PROFILE_SCHEMA_VERSIONS

    declaration = contract()
    require(declaration["research_sessions"]["execution_profile_schemas"] == list(PROFILE_SCHEMA_VERSIONS),
            "Execution profile advertisement differs from public owner")
    with Onboarding.open() as host:
        index = host.resources()
        documents = {row["id"]: digest(host.resource(row["id"])) for row in index["resources"]}
        schemas = {row["id"]: digest(json.loads(host.resource(row["id"])["content"]))
                   for row in index["resources"] if row["media_type"] == "application/schema+json"}
        declarations = host.contracts()
        metadata = host.bootstrap()["payload"]["installation"]
        matrix = json.loads(host.resource("framework/compatibility/v1")["content"])
    require(metadata["library_api_version"] == declaration["library_api"]["version"], "Catalog API version differs from owner")
    require({row["id"] for row in matrix["frameworks"]} == {"pi", "opencode", "gemini"},
            "Required packaged framework row missing")
    from evidence_wiki.onboarding_mcp import OnboardingMcpServer

    server = OnboardingMcpServer()
    try:
        server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "expectations", "version": "1"}}})
        server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools = server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    finally:
        server.close()
    return {"schema_version": "installed-public-expectations/v1", "exports": sorted(evidence_wiki.__all__),
            "contract": digest(declaration), "onboarding_contract": digest(declarations),
            "resource_index": index, "resource_documents": documents, "schema_documents": schemas, "installation": metadata,
            "framework_matrix": matrix, "mcp_tools": tools, "legacy_vectors": legacy_vectors(),
            "core_requirements": sorted(CORE_REQUIREMENTS), "supervision_contract": supervision_contract(),
            "probe_inputs": {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                             for name in ("probe_installed_public.py", "_qualification_process.py", "_installation_contract.py")}}


def legacy_vectors():
    """Exercise published legacy constructors against independent fixed byte identities."""
    from evidence_wiki import (
        DisagreementPlan,
        ExecutionProfile,
        ExecutionRole,
        ExecutionRoleLimit,
        ExecutionRoleRegistry,
        ExecutionRoleScope,
        ExecutionSchedule,
        ReviewDelivery,
        RunPolicy,
    )

    effects = ["offline_check", "model", "acquire", "revise", "retry", "adjudicate", "deliver"]
    policy = {
        "schema_version": "research-run-policy/v1", "policy_id": None,
        "objective": "  Compare α and β.\r\nKeep the exact original.  ",
        "scope": {"question_ids": ["question-a", "question-b"], "source_scope": ["source-a", "source-b"]},
        "allowed_effects": effects,
        "host_capability_refs": {effect: "configured-" + effect for effect in effects if effect != "offline_check"},
        "assurance": "artifact_checked", "human_review": False,
        "required_evaluators": [{"role": "evaluator", "profile_ref": "profile-a", "controller_ref": "controller-a", "human_required": False}],
        "source_rules": {"require_current": True, "require_present": True, "require_complete_context": True, "allow_partial": True},
        "thresholds": {"critical_false_acceptances_max": 0, "min_reference_cases": 100, "min_families": 10,
                       "positive_acceptance_min": "0.90", "meaning_preserving_flip_max": "0.05"},
        "budgets": {"seconds": 300, "model_calls": 0, "tool_calls": 50, "tokens": 0, "cost_limit": "0", "questions": 5, "claims": 20, "sources": 20},
        "continuation_limits": {"repair_cycles": 2, "adjudication_cycles": 1, "retry_attempts": 2, "acquisition_attempts": 2},
        "terminal_outcomes": ["accepted", "partial", "abstained", "rejected", "stop_budget", "blocked_external", "policy_incompatible"],
        "continuation_outcomes": ["revise", "acquire_more", "retry"],
    }
    hashes = {"policy_v1": hashlib.sha256(RunPolicy(policy).to_json()).hexdigest()}
    policy["schema_version"] = "research-run-policy/v2"
    policy["scope"]["source_scope_kind"] = "identity"
    hashes["policy_v2"] = hashlib.sha256(RunPolicy(policy).to_json()).hexdigest()
    worker = ExecutionRole("worker", "host_worker", "owner", "offline/model", "prompt")
    first = ExecutionProfile("legacy", 4, "EUR", "fixed", (worker,), 1, 1, 100, "0")
    second = ExecutionProfile("legacy", 4, "EUR", "fixed", (worker,), 1, 1, 100, "0",
                              schedule=ExecutionSchedule((ExecutionRoleLimit("host_worker", "owner", 2, 2, 200, "0", 60),)))
    hashes.update(profile_v1=digest(first.document()), profile_v2=digest(second.document()))
    roles = (worker, ExecutionRole("review", "evaluator", "review-owner", "local/model", "prompt"))
    registry = ExecutionRoleRegistry((ExecutionRoleScope("worker", "researcher", ("model",)),
                                     ExecutionRoleScope("review", "evaluator", ("model",))))
    for name, fields in (("scoped", {}), ("disagreement", {"disagreement": DisagreementPlan()}),
                         ("delivery", {"review_delivery": ReviewDelivery()})):
        hashes[name] = digest(ExecutionProfile("legacy", 4, "EUR", "fixed", roles, 1, 1, 100, "0", role_scopes=registry, **fields).document())
    compute = ExecutionRole("compute", "host_worker", "owner", "offline/model", "prompt")
    scopes = ExecutionRoleRegistry((*registry.scopes, ExecutionRoleScope("compute", "calculation", ("model", "calculation.propose"))))
    hashes["calculation"] = digest(ExecutionProfile("legacy", 4, "EUR", "fixed", (*roles, compute), 1, 1, 100, "0", role_scopes=scopes).document())
    expected = {
        "policy_v1": "a2e4e571061689b9154adeb6f63755260f749551534435e2980f5d7fd8a22d76",
        "policy_v2": "2d604fd7b85e1147bb9c0a12f2278b711ab923921aa5afd017c4873edbd9e068",
        "profile_v1": "4b7802ba8fecf9be3863320d32e70c15a5c1737db574c0693aace554599ab444",
        "profile_v2": "1f01fbbc06974ee15d23af0adba2cbd03e2446edb7bc5b3d2492ea5d4eefca25",
        "scoped": "456a53ec5f12c4fac9cfb75b886456c8431626d8835e65af3118f961deae0d0f",
        "disagreement": "3ece16a16ecd454275885a5d54e600fa7a33e00b9a1a3b1a842b224e5567c3fc",
        "delivery": "c6e9c6baa996a33fc94bf536a2420f33a84eda51bdd8828ea8a476a7bb5e4f4c",
        "calculation": "567efa3e716f6ee961d16442c4200efbf64eecfbe40404cb73b46e015989f2a2",
    }
    require(hashes == expected, "Legacy serialized public vectors differ")
    return hashes


def runtime_identity():
    return {"python_version": platform.python_version(), "implementation": platform.python_implementation(),
            "system": platform.system(), "machine": platform.machine()}


def checked_public_outcomes(report, expected):
    """No missing or skipped obligation can be represented as successful qualification."""
    require(isinstance(report, dict) and report.get("observation_class") == "public_installed_fixture_mechanics",
            "Public installed observation class differs")
    require(report.get("outcomes") == dict.fromkeys(PUBLIC_CASES, "passed"), "Public installed outcomes incomplete")
    require(report.get("expectations_sha256") == digest(expected), "Public expectation identity differs")
    require(report.get("production_qualification") is False, "Synthetic example promoted to production qualification")
    require(expected["supervision_contract"] == supervision_contract(), "Public source supervision contract differs")
    checked_supervision(report)
    require(report.get("framework_matrix_sha256") == digest(expected["framework_matrix"]), "Framework matrix rows differ")
    require(report.get("optional_hosts") == {"git": "not_invoked", "poppler": "not_invoked", "model_clis": "not_invoked",
                                               "live_models": "not_invoked", "host_enforcement": "not_established"},
            "Optional host observations differ")
    support = runpy.run_path(str(Path(__file__).with_name("_installation_contract.py")))
    support["validate_resolved_metadata"](report.get("resolved_metadata"), expected["project_metadata"],
        expected["dependency_rules"], {**report["runtime"], "marker_environment": report["marker_environment"]})
    support["validate_dependency_origins"](report["resolved_metadata"], report.get("dependency_origins"), report["prefix"])
    require(report.get("resolved_distributions") == {name: value["version"] for name, value in report["resolved_metadata"].items()},
            "Resolved version inventory differs from its metadata")
    return report


def extract_bundle(host, resource_id, destination):
    files = json.loads(host.resource(resource_id)["content"])["files"]
    require(isinstance(files, dict) and files, "Empty public example")
    destination.mkdir(mode=0o700)
    for name, content in files.items():
        require(name == Path(name).name and name not in {".", ".."} and isinstance(content, str), "Unsafe public example member")
        (destination / name).write_text(content, encoding="utf-8", newline="\n")


def checked_brief(brief, objective):
    require(brief["schema_version"] == "research-current-brief/v1" and brief["verdict"] == "accept"
            and brief["lifecycle"]["terminal"] is True and brief["lifecycle"]["outcome"] == "accepted"
            and brief["accounting_complete"] is True and not brief["global_blockers"]
            and brief["authorized"] is False and brief["external_action_authorized"] is False,
            "Current structured brief is incomplete or not eligible")
    originals = brief["original_outcomes"]
    require(len(originals) == 1 and originals[0]["original_text"] == objective and originals[0]["outcome"] == "accept",
            "Structured brief did not preserve original question")
    require(not originals[0]["missing_question_ids"] and len(originals[0]["eligible_questions"]) == 1,
            "Structured brief omitted eligible question")
    question = originals[0]["eligible_questions"][0]
    claims = question["claims"]
    require(question["accepted"] is True and len(claims) == 1 and claims[0]["text"] == "Observed solar reflectance was 0.74."
            and len(claims[0]["evidence"]) == 1 and claims[0]["evidence"][0]["quote"] == "Measured solar reflectance was 0.74.",
            "Structured brief did not export the scoped retained observation")


def checked_accounting(accounting, run_id):
    require(accounting["schema_version"] == "research-execution-report/v2" and accounting["run_id"] == run_id
            and accounting["chain_complete"] is True and accounting["record_count"] > 0
            and accounting["activity_summary"]["snapshot_complete"] is True
            and accounting["authorized"] is False and accounting["evidence_accepted"] is False,
            "Native accounting report is incomplete")
    counts = accounting["activity_summary"]["counts"]
    require(counts["critical_review_failures"] >= 1 and counts["repair_attempt_intents"] >= 1
            and counts["failed_review_applications"] >= 1 and counts["application_failure_records"] == 0,
            "Native accounting did not retain failed critique and successful bounded repair")


def checked_direct_result(direct):
    require(direct["schema_version"] == "unattended-host-result/v1" and direct["observation_class"] == "fixture_mechanics"
            and direct["production_qualification"] is False and len(direct["reports"]) == 2
            and direct["authorized"] is False and direct["evidence_accepted"] is False, "Direct host result differs")
    original, refresh = direct["reports"]
    require(original["terminal"] is True and original["run_id"] == "study" and original["outcome"] == "accepted",
            "Original research cycle did not complete")
    checked_brief(direct["brief"], "What observation was retained?")
    checked_accounting(direct["accounting"], "study")
    require(refresh["schema_version"] == "research-refresh-result/v1" and refresh["terminal"] is True
            and refresh["run_id"] == "study-refresh" and refresh["previous_run_id"] == "study"
            and refresh["parent_binding_id"] == original["binding_id"]
            and refresh["cycle"]["binding_id"] != original["binding_id"]
            and refresh["cycle"]["run_id"] == "study-refresh" and refresh["cycle"]["terminal"] is True
            and refresh["outcome"] == "accepted" and refresh["approval_reuse"] is False
            and refresh["prior_current_eligibility"] == "not_evaluated"
            and refresh["budget_scope"] == "explicit_new_run_allocation; previous_accounting_retained_not_reset",
            "Refresh did not preserve parent and independent current authority/budget")
    checked_brief(refresh["superseding_brief"], "What observation was retained?")
    return direct


def checked_adapter_result(result, route):
    require(result["observation_class"] == "scripted_fixture_mechanics" and result["production_qualification"] is False
            and result["status"] == "returned" and result["telemetry"]["operator_requests"] == 0,
            "Public command adapter failed: " + route)
    context = result["context"]
    require(context["selection"]["status"] == "ready" and context["selection"]["operator_request"] is False
            and [(row["role"], row["state"]) for row in context["telemetry"]["calls"]] == [("planner", "succeeded"), ("critic", "succeeded")],
            "Independent context planner and critic did not complete")
    journey = result["journey"]
    for operation in ("cycle", "brief", "report", "recovery"):
        envelope = journey[operation]
        require(envelope["schema_version"] == "research-command-result/v1" and envelope["status"] == "returned"
                and envelope["exit_code"] == 0 and envelope["error"] is None,
                "Public adapter native command refused: " + operation)
    cycle = journey["cycle"]["result"]
    require(cycle["terminal"] is True and cycle["outcome"] == "accepted", "Adapter research did not complete")
    checked_brief(journey["brief"]["result"], "Assess the observed solar reflectance with explicit uncertainty.")
    checked_accounting(journey["report"]["result"], cycle["run_id"])
    from evidence_wiki.recovery_contracts import checked as checked_recovery

    recovery = checked_recovery(journey["recovery"]["result"])
    pending = recovery["pending"]
    settled = (pending is None or pending["state"] == "succeeded" and pending["applied"] is True
               and pending["reservation_occupied"] is False and type(pending["cancellation_requested"]) is bool
               and pending["cancellation_confirmed"] is False and type(pending["observation_ref"]) is str
               and bool(pending["observation_ref"]))
    require(recovery["schema_version"] == "research-recovery-status/v1" and recovery["run_id"] == cycle["run_id"]
            and recovery["binding_id"] == cycle["binding_id"] and recovery["terminal"] is True
            and recovery["status"] == "terminal" and settled and recovery["next_work"] is None
            and recovery["authorized"] is False and recovery["evidence_accepted"] is False,
            "Adapter recovery did not observe the completed original run")
    return result


def probe(root, expected, cli, checkout, runner, stage):
    prefix = Path(sys.prefix).resolve()
    require(not root.resolve().is_relative_to(checkout.resolve()) and not prefix.is_relative_to(checkout.resolve()), "Public probe overlaps checkout")
    require(Path(evidence_wiki.__file__).resolve().is_relative_to(prefix), "Package imported outside selected environment")
    require(platform.python_implementation() == "CPython" and sys.version_info >= (3, 10), "Runtime outside declared Python boundary")
    require(sorted(evidence_wiki.__all__) == expected["exports"], "Public export inventory differs")
    for name in evidence_wiki.__all__:
        value = getattr(evidence_wiki, name)
        if name == "__version__":
            continue
        module = value if hasattr(value, "__file__") else importlib.import_module(value.__module__)
        require(Path(module.__file__).resolve().is_relative_to(prefix), "Public export resolved outside installation: " + name)
    observed = public_expectations()
    require(observed == {key: expected[key] for key in observed}, "Installed declarations/resources differ from frozen source")
    installed = {}
    resolved = {}
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata["Name"].lower().replace("_", "-").replace(".", "-")
        require(name in CORE_DISTRIBUTIONS + INSTALLER_DISTRIBUTIONS + ("typing-extensions",), "Undeclared installed dependency: " + name)
        require(Path(distribution.locate_file("")).resolve().is_relative_to(prefix), "Dependency metadata outside selected environment")
        if name not in INSTALLER_DISTRIBUTIONS:
            require(name not in installed, "Duplicate installed dependency: " + name)
            installed[name] = distribution.version
            resolved[name] = {"version": distribution.version, "requires_python": distribution.metadata.get("Requires-Python"),
                              "requires_dist": sorted(distribution.requires or []),
                              "entry_points": {row.name: row.value for row in distribution.entry_points if row.group == "console_scripts"}}
    from pip._vendor.packaging.markers import default_environment

    markers = default_environment()
    support = runpy.run_path(str(Path(__file__).with_name("_installation_contract.py")))
    support["validate_resolved_metadata"](resolved, expected["project_metadata"], expected["dependency_rules"],
                                         {**runtime_identity(), "marker_environment": markers})
    origins = {}
    origin_root = Path(runner.output) / "dependency-origins"
    origin_root.mkdir()
    for name in set(resolved) - {"evidence-wiki"}:
        module_name = support["DEPENDENCY_MODULES"][name]
        module = importlib.import_module(module_name)
        path = Path(module.__file__).resolve()
        require(path.is_relative_to(prefix) and path.is_file(), "Imported core dependency outside selected environment: " + name)
        data = path.read_bytes()
        origins[name] = {"module": module_name, "origin": str(path), "version": getattr(module, "__version__", None),
                         "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        (origin_root / (name + ".py")).write_bytes(data)
    support["validate_dependency_origins"](resolved, origins, str(prefix), read_files=True)
    environment = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"}}
    environment.update(PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
    def command(argv, label, expected_code=0, input=None):
        result = stage.run(runner, argv, label=label, cwd=root, env=environment, input=input, expected=(expected_code,))
        require(result.returncode == expected_code, label + " failed: " + result.stderr[-4096:])
        return json.loads(result.stdout)
    dependency_check = stage.run(runner, [sys.executable, "-I", "-m", "pip", "check"], label="public-pip-check", cwd=root,
                                env=environment)
    require(dependency_check.returncode == 0 and dependency_check.stdout == "No broken requirements found.\n", "Installed dependency check failed")
    require(digest(command([str(cli), "contract"], "public-cli-contract")) == expected["contract"], "CLI contract differs")
    with Onboarding.open() as host:
        catalog = command([str(cli), "agent", "resources", "--format", "json"], "public-cli-resources")
        require(catalog["payload"] == {key: value for key, value in expected["resource_index"].items() if key != "schema_version"}, "CLI resource catalog differs")
        for row in expected["resource_index"]["resources"]:
            if row["media_type"] == "application/schema+json":
                document = json.loads(host.resource(row["id"])["content"])
                require(isinstance(document, dict) and bool(document), "Empty installed schema document")
        extract_bundle(host, "example/unattended-host/v1", root / "direct-app")
        extract_bundle(host, "example/agent-host/v1", root / "adapter-app")
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "public-probe", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "onboarding_resource", "arguments": {"resource_id": "framework/compatibility/v1"}}},
    ]
    transport = stage.run(runner, [str(cli), "serve-onboarding-mcp"], label="public-mcp-stdio", cwd=root, env=environment,
                          input="\n".join(json.dumps(row) for row in messages) + "\n")
    require(transport.returncode == 0 and not transport.stderr, "MCP stdio transport failed")
    responses = [json.loads(line) for line in transport.stdout.splitlines()]
    require([row.get("id") for row in responses] == [1, 2, 3], "MCP responses missing or duplicated")
    with Onboarding.open() as host:
        require(responses[1]["result"]["tools"] == expected["mcp_tools"], "MCP tool schemas differ")
        require(json.loads(responses[2]["result"]["content"][0]["text"]) == host.resource("framework/compatibility/v1"), "MCP resource differs")
    direct = command([sys.executable, str(root / "direct-app/host.py"), "--mode", "fake", "--output", str(root / "direct-journey"), "--refresh-max-steps", "64"], "public-direct-host")
    checked_direct_result(direct)
    refusal = command([str(cli), "agent", "run-cycle", "--target", str(root / "direct-journey/workspace"),
                       "--run-id", "unconfigured", "--request-id", "unconfigured", "--max-steps", "1", "--noninteractive"],
                      "public-cli-runtime-refusal", expected_code=2)
    require(refusal["schema_version"] == "research-command-result/v1" and refusal["status"] == "refused"
            and refusal["terminal"] is True and refusal["error"]["error_code"] == "RESEARCH_EXECUTION_CONFIG_INVALID"
            and refusal["authorized"] is False and refusal["evidence_accepted"] is False,
            "CLI missing-runtime outcome differs")
    for route in ("current-caller", "mcp"):
        result = command([sys.executable, str(root / "adapter-app/agent_host.py"), "--mode", "scripted", "--route", route,
                          "--output", str(root / (route + "-journey"))], "public-" + route)
        checked_adapter_result(result, route)
    with Workspace.open(root / "direct-journey/workspace") as workspace:
        owners = [workspace.strict.packet_schemas, workspace.strict.service_schemas, workspace.strict.resolution_schemas,
                  workspace.strict.brief_schemas, workspace.strict.repair_schemas, workspace.research.command_schemas,
                  workspace.research.semantic_schemas, workspace.research.performance_schemas,
                  workspace.research.refresh_schedule_schemas, workspace.research.refresh_schemas]
        for owner in owners:
            for schema_id, document in owner().items():
                require(schema_id in expected["schema_documents"] and digest(document) == expected["schema_documents"][schema_id],
                        "Public schema owner differs from installed catalog: " + schema_id)
    report = {"observation_class": "public_installed_fixture_mechanics", "outcomes": dict.fromkeys(PUBLIC_CASES, "passed"),
              "expectations_sha256": digest(expected), "framework_matrix_sha256": digest(expected["framework_matrix"]),
              "production_qualification": False, "runtime": runtime_identity(), "resolved_distributions": installed,
              "resolved_metadata": resolved, "marker_environment": markers,
              "dependency_origins": origins, "prefix": str(prefix),
              "commands": runner.records,
              "supervision": stage.finish(),
              "optional_hosts": {"git": "not_invoked", "poppler": "not_invoked", "model_clis": "not_invoked", "live_models": "not_invoked", "host_enforcement": "not_established"}}
    return checked_public_outcomes(report, expected)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--expectations", type=Path, required=True)
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--checkout-root", type=Path, required=True)
    parser.add_argument("--logs", type=Path, required=True)
    parser.add_argument("--stage-start-monotonic", type=float, required=True)
    parser.add_argument("--stage-deadline-monotonic", type=float, required=True)
    args = parser.parse_args()
    support = runpy.run_path(str(Path(__file__).with_name("_qualification_process.py")))
    with support["interruptible"]():
        stage = PublicStage(args.stage_start_monotonic, args.stage_deadline_monotonic)
        args.root.mkdir()
        report = probe(args.root, json.loads(args.expectations.read_text(encoding="utf-8")), args.cli, args.checkout_root,
                       support["CommandRunner"](args.logs), stage)
        print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
