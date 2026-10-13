"""Observe source-native boundaries and separate installed read transports."""

from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import importlib.util
import json
import os
import sys
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def refuse_call(function, *, error_code, details):
    from evidence_wiki.errors import EvidenceWikiError
    try:
        function()
    except EvidenceWikiError as error:
        require(error.error_code == error_code and error.details == details, "Native refusal did not match the frozen attack expectation")
        return {"state": "refused", "error_code": error.error_code, "details": error.details}
    raise ValueError("Expected native boundary refusal was not observed")


def qualify(output, installed, checkout, native):
    import evidence_wiki
    from evidence_wiki import HostRuntime, Workspace, host_qualification
    from evidence_wiki.agent_resources import resource_document
    from evidence_wiki.mutation_contracts import digest
    from evidence_wiki.onboarding_mcp import OnboardingMcpServer
    from evidence_wiki.research_journal_contracts import digest_record

    package = Path(evidence_wiki.__file__).resolve()
    require(native == checkout and package == native / "src/evidence_wiki/__init__.py",
        "Native parent is not the selected frozen project source")
    require(not output.is_relative_to(checkout) or __import__("subprocess").run(
        ["git", "check-ignore", "-q", "--", str(output)], cwd=checkout, check=False).returncode == 0, "Repository evidence target is not ignored")
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    runner_owner = load(checkout / "tools/_qualification_process.py", "host_qualification_process")
    runner = runner_owner.CommandRunner(output / "commands")
    recipe = resource_document("example/unattended-host/v1")
    recipe_root = output / "native-recipe"
    recipe_root.mkdir(mode=0o700)
    for name, content in json.loads(recipe["content"])["files"].items():
        path = Path(name)
        require(not path.is_absolute() and ".." not in path.parts, "Recipe path is not bounded")
        target = recipe_root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    sys.path.insert(0, str(recipe_root))
    fixture = load(recipe_root / "fixture_host.py", "source_native_host_recipe")
    require(Path(fixture.Workspace.__module__.replace(".", "/")).parts[0] == "evidence_wiki", "Recipe uses an unexpected package")
    result = {"schema_version": "configured-host-observations/v1", "started_utc": datetime.now(timezone.utc).isoformat(),
        "observation_class": "trusted_native_synthetic_services", "native_package": str(package),
        "native_package_scope": "selected_frozen_project_source", "native_root": str(native), "installed_root": str(installed),
        "installed_setup_child_bridge": {"state": "unsupported", "reason": "isolated_setup_child_uses_project_editable_package",
            "scope": "installed-target parent with project virtual environment child; native evaluator qualification is source-only"},
        "recipe_sha256": recipe["sha256"], "candidate_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "cases": {}, "calls": [], "native": [], "qualification": [], "started_attempts": [], "reference_observations": [],
        "entrypoints": [], "console_responses": [], "stdio_responses": [],
        "limitations": ["Synthetic native credential/controller services and scripted semantic outputs only; no remote LLM or network execution.",
            "Distinct parent-held native principal keys are not independently isolated credential custody or reasoning.",
            "Installed candidate uses the existing project virtual environment dependencies; no fresh-environment claim.",
            "Native setup, evaluator execution and current host observations use the selected frozen project source.",
            "Verified installed wheel console and stdio reads are separate cells; installed-target native setup/evaluator execution is unsupported.",
            "In-process research MCP and real stdio read transport are separate observed combinations."]}

    def save():
        result["command_receipts"] = copy.deepcopy(runner.records)
        (output / "observations.json").write_bytes(encoded(result))

    save()
    runtimes = []
    for name in ("one", "two"):
        runner.event("started", "provision-" + name)
        def retain_reference(attempt, receipt, name=name):
            row = {"workspace": name, "attempt": attempt, "observation": receipt, "scope": "synthetic_reference_service"}
            result["reference_observations"].append(row)
            with (output / "reference-observations.jsonl").open("ab") as stream:
                stream.write(encoded(row))
        with contextlib.redirect_stdout(sys.stderr):
            root, runtime, _setup = fixture.provision(output / name, "What solar reflectance was retained?", observation_sink=retain_reference)
        context = replace(runtime.context, grants=runtime.context.grants | {"host_qualification.current", "service_authentication.status", "onboarding.bootstrap"})
        runtime = HostRuntime.from_services(runtime.policy, context, runtime.execution)
        runtimes.append((root, runtime, "native-" + name))
        runner.event("passed", "provision-" + name)
        save()

    servers = []
    initialize = {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "host-qualification", "version": "1"}}
    for root, runtime, _run_id in runtimes:
        server = OnboardingMcpServer(allowed_roots=[root], runtime=runtime, research_allow=["advance"])
        require("error" not in server.handle_message({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": initialize}), "In-process MCP initialization failed")
        server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        servers.append(server)
    terminal = [False, False]
    boundary_complete = [False, False]
    try:
        for number in range(128):
            for index, (root, runtime, run_id) in enumerate(runtimes):
                if boundary_complete[index]:
                    continue
                route = ("python", "current_caller_command_in_process", "mcp_jsonrpc_in_process")[number % 3]
                with runtime.context.activate(), Workspace.open(root) as workspace:
                    with workspace.research.session(runtime.policy, run_id=run_id, execution=runtime.execution) as session:
                        basis = session.current()["basis_id"]
                        arguments = {"run_id": run_id, "request_id": "advance-" + str(number), "expected_basis_id": basis}
                        if route == "python":
                            reply = session.advance(request_id=arguments["request_id"], expected_basis_id=basis)
                        elif route == "current_caller_command_in_process":
                            envelope = workspace.research.command({"schema_version": "research-command-request/v1",
                                "operation": "advance", "arguments": arguments}, runtime=runtime)
                            require(envelope["exit_code"] == 0, "Current-caller native advance refused")
                            reply = envelope["result"]
                        else:
                            envelope = servers[index].handle_message({"jsonrpc": "2.0", "id": number + 1, "method": "tools/call",
                                "params": {"name": "research_advance", "arguments": {"target": str(root), **arguments}}})
                            if "error" in envelope or envelope.get("result", {}).get("isError") is not False:
                                result["cases"]["failed_mcp_advance"] = {"workspace": index, "request_ref": digest(arguments), "response": envelope}
                                save()
                            require("error" not in envelope, "In-process MCP native advance refused")
                            command = json.loads(envelope["result"]["content"][0]["text"])
                            require(command["exit_code"] == 0, "In-process MCP owner refusal")
                            reply = command["result"]
                        terminal[index] = reply["terminal"]
                        result["calls"].append({"workspace": index, "route": route, "request_ref": digest(arguments),
                            "reply_ref": digest(reply), "terminal": terminal[index]})
                        records = runtime.execution.records(session)
                        if len(result["native"]) <= index:
                            result["native"].append(records)
                        else:
                            result["native"][index] = records
                        result["started_attempts"] = [{"workspace": slot, "attempt_id": item["data"]["attempt"]["attempt_id"],
                            "attempt_ref": digest(item["data"]["attempt"])} for slot, entries in enumerate(result["native"])
                            for item in entries if item["kind"] == "execution_attempt"]
                        save()
                        required = {item["profile_ref"] for item in runtime.policy.to_dict()["required_evaluators"]}
                        registered = {item["data"].get("profile_ref") for item in records
                            if item["kind"] == "execution_applied" and item["data"]["operation"] == "evaluate"}
                        if required <= registered:
                            launched = [item["data"]["attempt"] for item in records if item["kind"] == "execution_attempt"]
                            latest = {}
                            for item in records:
                                if item["kind"] == "execution_observation":
                                    receipt = item["data"]["observation"]
                                    latest[receipt["attempt_id"]] = receipt
                            require(len(launched) == 1 + len(required) and all(
                                latest.get(item["attempt_id"], {}).get("final_accounting") is True for item in launched),
                                "Registered-controller boundary includes extra or unresolved worker attempts")
                            report = host_qualification(session)
                            require(report["capability_matrix"][0]["state"] == "observed"
                                and len(report["control"]["authenticated_review_controller_refs"]) == len(required),
                                "Registered native reviews did not establish current authenticated controller/context observations")
                            result["qualification"].append(report)
                            save()
                            boundary_complete[index] = True
                        save()
                        require(not terminal[index] or boundary_complete[index], "Business workflow terminated before the required-controller boundary was observed")
            if all(boundary_complete):
                break
            time.sleep(0.02)
        require(all(boundary_complete), "Interleaved required-controller observations did not complete")
        result["business_terminal_flags"] = terminal
        result["native_completion_scope"] = "both required registered native reviewers with current immutable contexts and authenticated controller attestations; no pending worker"
        require(all({row["route"] for row in result["calls"] if row["workspace"] == index} == {
            "python", "current_caller_command_in_process", "mcp_jsonrpc_in_process"} for index in (0, 1)), "Three native surfaces were not observed")
        for root, runtime, run_id in runtimes:
            with runtime.context.activate(), Workspace.open(root) as workspace:
                with workspace.research.session(runtime.policy, run_id=run_id, execution=runtime.execution) as session:
                    report = next(item for item in result["qualification"] if item["run_id"] == run_id)
                    session._check("current")
                    require(session.current()["basis_id"] == report["basis_id"], "Retained original host observation no longer matches current native basis")
                    current_records = runtime.execution.records(session)
                    require((digest_record(current_records[-1]) if current_records else None) == report["journal_head_ref"],
                        "Retained original host observation journal changed before boundary controls")
                    require(report["capability_matrix"][0]["state"] == "observed", "Current native bound review evidence was not observed")
                    require(len(report["control"]["authenticated_review_controller_refs"]) == 2, "Two native review controllers were not observed")
                    foreign = runtimes[1 if root == runtimes[0][0] else 0][1].context
                    with foreign.activate():
                        result["cases"]["cross_workspace_" + run_id] = refuse_call(lambda: host_qualification(session),
                            error_code="HOST_CONTEXT_REFUSED", details={"reason": "host_path_outside_scope"})
                    records = runtime.execution.records(session)
                    application = next(item["data"] for item in reversed(records) if item["kind"] == "execution_application" and "envelope" in item["data"])
                    from evidence_wiki import execution_session as native
                    attempt = runtime.execution.attempt(next(item["data"]["attempt"] for item in records if item["kind"] == "execution_attempt"
                        and item["data"]["attempt"]["attempt_id"] == application["attempt_id"]))
                    forged = copy.deepcopy(application["envelope"])
                    forged["authentication"]["principal"] = runtime.execution.review_signer.generator.principal
                    result["cases"]["forged_principal_" + run_id] = refuse_call(lambda attempt=attempt, forged=forged: native._verify_review_controller(session, attempt, forged),
                        error_code="RESEARCH_REVIEW_CONTROLLER_MISMATCH", details={"field": "/"})
                    authority_path = runtime.context.authority_file
                    original = authority_path.read_bytes()
                    trust = json.loads(original)
                    trust["revoked_keys"].append(runtime.execution.review_signer.reviewers[0].key_id)
                    try:
                        authority_path.write_bytes(encoded(trust))
                        result["cases"]["revoked_credential_" + run_id] = refuse_call(lambda: host_qualification(session),
                            error_code="RESEARCH_BINDING_CHANGED", details={"field": "/"})
                    finally:
                        authority_path.write_bytes(original)
                result["cases"]["closed_session_" + run_id] = refuse_call(lambda: host_qualification(session),
                    error_code="RESEARCH_SESSION_CLOSED", details={"field": "/"})
        save()
    finally:
        for server in servers:
            server.close()

    return _remaining_controls(output, installed, [item[0] for item in runtimes],
        [item[1].policy.to_dict() for item in runtimes], [item[1].execution.profile.document() for item in runtimes],
        [item[1].context for item in runtimes], result, runner, save)


def _remaining_controls(output, installed, roots, policies, profiles, contexts, result, runner, save):
    import evidence_wiki
    from evidence_wiki.mutation_contracts import digest

    initialize = {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "host-qualification", "version": "1"}}
    bootstrap = """import sys,json,inspect,hashlib
from pathlib import Path
selected=Path(sys.argv.pop(1)).resolve()
record=Path(sys.argv.pop(1))
sys.path.insert(0,str(selected))
from importlib.metadata import distribution
import evidence_wiki
declared=distribution('evidence-wiki')
entries=[entry for entry in declared.entry_points if entry.group=='console_scripts' and entry.name=='evidence-wiki']
if len(entries)!=1: raise SystemExit('Selected console entry is missing or ambiguous')
entry=entries[0]
invoke=entry.load()
origin=Path(inspect.getsourcefile(invoke)).resolve()
package=Path(evidence_wiki.__file__).resolve()
metadata=Path(declared.locate_file('')).resolve()
if not all(path.is_relative_to(selected) for path in (origin,package,metadata)): raise SystemExit('Console entry origin is not the installed candidate')
if declared.version!=evidence_wiki.__version__: raise SystemExit('Selected distribution version differs from package')
observation={'distribution_version':declared.version,'distribution_root':str(metadata),'package_origin':str(package),
 'entry_name':entry.name,'entry_group':entry.group,'entry_value':entry.value,'function_module':invoke.__module__,
 'function_origin':str(origin),'function_sha256':hashlib.sha256(origin.read_bytes()).hexdigest(),
 'entry_metadata_sha256':hashlib.sha256(declared.read_text('entry_points.txt').encode()).hexdigest()}
record.write_text(json.dumps(observation,sort_keys=True)+'\\n')
raise SystemExit(invoke())
"""
    def installed_cli(arguments, label, stdin=None, expected=(0,)):
        identity_path = output / (label + "-entrypoint.json")
        completed = runner.run([sys.executable, "-I", "-c", bootstrap, str(installed), str(identity_path), *arguments], label=label,
            cwd=output, env={"PATH": os.defpath, "PYTHONNOUSERSITE": "1"}, timeout=60, input=stdin, expected=expected)
        result["entrypoints"].append({"command": label, "identity": json.loads(identity_path.read_bytes())})
        save()
        return completed
    for number, index in enumerate((0, 1, 0, 1)):
        root = roots[index]
        root_info = root.stat()
        target_inputs = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in ("research.yml", "workspace-system.yml")}
        from evidence_wiki.agent import bootstrap as native_bootstrap
        baseline = native_bootstrap(str(root))
        request_id = "console-" + str(number)
        completed = installed_cli(["agent", "bootstrap", "--target", str(root), "--request-id", request_id, "--format", "json"], "installed-console-" + str(number))
        response = json.loads(completed.stdout)
        result["console_responses"].append({"workspace": index, "target_identity": {"device": root_info.st_dev, "inode": root_info.st_ino},
            "target_input_sha256": digest(target_inputs), "native_metadata_ref": digest(baseline["environment"]), "response": response})
        save()
        require(response.get("schema_version") == "2.0" and response.get("kind") == "bootstrap" and response.get("request_id") == request_id
            and response.get("payload", {}).get("workspace") == "present" and response["payload"]["installation"] == baseline["installation"]
            and response["payload"]["environment"] == baseline["environment"], "Installed console bootstrap did not return exact successful target metadata")
        require(root.stat().st_dev == root_info.st_dev and root.stat().st_ino == root_info.st_ino
            and all(hashlib.sha256((root / name).read_bytes()).hexdigest() == hashed for name, hashed in target_inputs.items()), "Console target changed")
    save()
    result["cases"]["installed_console_read_interleaving"] = {"state": "observed", "scope": "installed current-caller console bootstrap only"}
    messages = [{"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": initialize},
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}]
    for number, index in enumerate((0, 1, 0, 1)):
        messages.append({"jsonrpc": "2.0", "id": number + 1, "method": "tools/call", "params": {
            "name": "onboarding_inspect", "arguments": {"target": str(roots[index])}}})
    from evidence_wiki.source_inspection import inspect as native_inspect
    expected_inputs = [native_inspect(target=str(root))["observation"] for root in roots]
    foreign = output / "outside-scoped-roots"
    foreign.mkdir()
    messages.append({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {
        "name": "onboarding_inspect", "arguments": {"target": str(foreign)}}})
    completed = installed_cli(["serve-onboarding-mcp", "--allow-root", str(roots[0]), "--allow-root", str(roots[1])],
        "installed-stdio-interleaving", b"".join(encoded(message) for message in messages))
    responses = [json.loads(line) for line in completed.stdout.splitlines()]
    result["stdio_responses"] = responses
    save()
    require(len(responses) == 6 and [row.get("id") for row in responses] == list(range(6))
        and all(row.get("jsonrpc") == "2.0" and "error" not in row for row in responses), "Real stdio response IDs or success envelopes were not exact")
    require(responses[0]["result"].get("protocolVersion") == "2024-11-05"
        and responses[0]["result"].get("serverInfo", {}).get("name") == "evidence-wiki-onboarding", "Real stdio initialization was not the selected protocol")
    for response, index in zip(responses[1:5], (0, 1, 0, 1), strict=True):
        require(response["result"].get("isError") is False and len(response["result"].get("content", [])) == 1, "Real stdio read returned an error or ambiguous payload")
        payload = json.loads(response["result"]["content"][0]["text"])
        require(payload.get("schema_version") == "evidence-source-inspection/v1" and payload.get("target", {}).get("state") == "present"
            and payload.get("observation") == expected_inputs[index] and payload.get("installation") == baseline["installation"], "Real stdio read did not match exact native target inputs")
    require(responses[-1]["result"].get("isError") is True, "Real stdio foreign root was not refused")
    foreign_error = json.loads(responses[-1]["result"]["content"][0]["text"])
    require(foreign_error.get("error_code") == "ONBOARDING_AUTHORITY_REQUIRED"
        and foreign_error.get("details") == {"field": "onboarding_path_outside_host_scope"}, "Real stdio foreign root refusal did not match the frozen expectation")
    result["stdio_responses"] = responses
    result["stdio_target_inputs"] = [{"target": str(root), "device": root.stat().st_dev, "inode": root.stat().st_ino,
        "observation": observation} for root, observation in zip(roots, expected_inputs, strict=True)]
    result["cases"]["installed_mcp_stdio_read_interleaving"] = {"state": "observed", "scope": "onboarding scoped inspect A/B/A/B, foreign root refusal and clean EOF"}
    save()
    try:
        from evidence_wiki.runtime_configuration import _profile
        _profile(profiles[0])
    except Exception as error:
        result["cases"]["current_model_profile_stdio"] = {"state": "unsupported", "reason": getattr(error, "details", {}).get("reason", type(error).__name__)}
    else:
        raise ValueError("Unexpected current profile file support needs an actual stdio execution probe")
    private_runtime = output / "unsupported-current-runtime.json"
    unsupported = {"schema_version": "research-host-runtime/v1", "policy": policies[0],
        "profile": profiles[0], "context": {}, "commands": {}, "cwd": str(output),
        "meter": "native_process", "keys": {}, "qualification_authority": {}, "qualification_records": [], "review_signer": {}}
    descriptor = os.open(private_runtime, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(encoded(unsupported))
    stopped = installed_cli(["serve-onboarding-mcp", "--allow-root", str(roots[0]), "--research-runtime", str(private_runtime)],
        "unsupported-current-profile-stdio-startup", expected=(2,))
    require(stopped.stdout == "", "Unsupported stdio runtime exposed protocol service")
    reason_probe = ("import sys,json;sys.path.insert(0,sys.argv[1]);from evidence_wiki import HostRuntime;"
        "from evidence_wiki.errors import EvidenceWikiError\n"
        "try: HostRuntime.from_file(sys.argv[2])\n"
        "except EvidenceWikiError as error: print(json.dumps(error.details));raise SystemExit(error.exit_code)\n"
        "raise SystemExit(1)")
    diagnosed = runner.run([sys.executable, "-I", "-c", reason_probe, str(installed), str(private_runtime)],
        label="installed-current-profile-loader-reason", cwd=output, env={"PATH": os.defpath, "PYTHONNOUSERSITE": "1"},
        timeout=30, expected=(2,))
    require(json.loads(diagnosed.stdout).get("reason") == "runtime_profile_unsupported", "Installed loader did not refuse the selected profile")
    result["cases"]["current_model_profile_stdio"]["startup"] = "refused_before_service"
    result["cases"]["current_model_profile_stdio"]["startup_reason_basis"] = "correlated installed native loader diagnostic; stdio intentionally emits a generic private-scope refusal"
    save()

    from evidence_wiki.agent_execution import (
        BoundedSubprocessExecutor,
        ExecutionAttempt,
        ExecutionProfile,
        ExecutionRole,
    )
    from evidence_wiki.runtime_configuration import NativeProcessMeter
    from evidence_wiki.workflow_contracts import EXECUTION_OBSERVATION, JOB_V2
    worker = output / "shutdown-worker"
    worker.mkdir()
    profile = ExecutionProfile("shutdown-probe", 1, "EUR", "native-process", (
        ExecutionRole("worker", "host_worker", "shutdown-controller", "offline/native-process", "probe/v1"),), 0, 1, 0, "0", 1)
    executor = BoundedSubprocessExecutor(profile, commands={"worker": (sys.executable, "-I", "-c", "import sys,time;sys.stdin.read();time.sleep(60)")},
        cwd=worker, meter=NativeProcessMeter())
    instant = datetime.now(timezone.utc)
    job = {"schema_version": JOB_V2, "id": "shutdown", "operation": "generate", "role": "host_worker", "controller_ref": "shutdown-controller",
        "grant_ref": "local-process", "input_id": "shutdown-input", "basis_id": "shutdown-basis", "policy_id": "shutdown-policy",
        "policy_version": "1", "prompt_version": "probe/v1", "revision_precondition": "shutdown-basis", "original_text": "Bounded process cancellation",
        "scope": ["shutdown"], "required_capabilities": ["offline_check"], "payload": {"question": "Bounded process cancellation"},
        "issued_utc": instant.isoformat(), "deadline_utc": (instant + timedelta(seconds=10)).isoformat(), "timeout_seconds": 5,
        "max_attempts": 1, "expected_result_schema": EXECUTION_OBSERVATION, "budgets": profile.document()["job_budgets"],
        "qualifications": {"assurance": "artifact_checked", "status": "unknown", "evaluator_ref": None, "qualification_ref": None, "context_basis": None}}
    attempt = ExecutionAttempt.create(attempt_id="shutdown", nonce="shutdown-nonce", run_id="shutdown", binding_id="shutdown",
        profile=profile, config_ref="worker", job=job, input_document={"question": "Bounded process cancellation"})
    started = executor.invoke(attempt)
    result["cases"]["subprocess_shutdown"] = {"state": "started", "attempt": attempt.document(), "initial": started}
    save()
    require(started["started_utc"] is not None, "Cancellation probe did not start")
    ended = executor.cancel(attempt)
    authenticated = executor.authenticate(ended, attempt)
    require(authenticated["final_accounting"] and authenticated["cancellation_confirmed"], "Subprocess cancellation did not close custody")
    result["cases"]["subprocess_shutdown"] = {"state": "observed", "attempt": attempt.document(), "initial": started, "terminal": authenticated,
        "scope": "trusted worker process-group cleanup only; no malicious escape confinement"}
    save()

    if sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file():
        from evidence_wiki.strict_host import StrictResearchHost
        root, context = roots[0], contexts[0]
        previous = {name: os.environ.get(name) for name in ("EVIDENCE_WIKI_AUTHORITY_FILE", "EVIDENCE_WIKI_STATE_DIR")}
        try:
            os.environ["EVIDENCE_WIKI_AUTHORITY_FILE"] = str(context.authority_file)
            os.environ["EVIDENCE_WIKI_STATE_DIR"] = str(context.state_directory)
            try:
                result["cases"]["kernel_draft_boundary"] = StrictResearchHost(root).qualification()
            except evidence_wiki.errors.ConfigError as error:
                reason = error.details.get("reason")
                if reason not in {"strict_host_platform_unsupported", "strict_worker_failed_or_unavailable"}:
                    raise
                result["cases"]["kernel_draft_boundary"] = {"state": "unsupported" if reason == "strict_host_platform_unsupported" else "not_observed",
                    "reason": reason, "semantic_host_enforced": "unsupported"}
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
    else:
        result["cases"]["kernel_draft_boundary"] = {"state": "unsupported", "reason": "darwin_sbpl_unavailable"}
    result["completed_utc"] = datetime.now(timezone.utc).isoformat()
    result["state"] = "controls_observed_pending_custody_close" if result.get("mode") == "missing_controls" else "observed"
    save()
    return result


def missing_controls(output, installed, checkout, native, original_path, expected_sha256):
    """Finish read/authentication/transport controls without reopening worker services."""
    import secrets

    import evidence_wiki
    from evidence_wiki import HostReviewSigner, SigningIdentity
    from evidence_wiki.agent_resources import resource_document
    from evidence_wiki.host_context import HostContext
    from evidence_wiki.mutation_contracts import digest
    from evidence_wiki.mutation_operations import _host_selection
    from evidence_wiki.mutation_store import capture_owner_generation
    from evidence_wiki.pack_discovery import owner
    from evidence_wiki.research_execution import JOURNAL
    from evidence_wiki.research_journal_contracts import digest_record
    from evidence_wiki.run_policy import RunPolicy

    require(native == checkout and Path(evidence_wiki.__file__).resolve() == native / "src/evidence_wiki/__init__.py",
        "Read controls do not use the selected frozen project source")
    raw = original_path.read_bytes()
    require(len(raw) <= 16_777_216 and len(expected_sha256) == 64 and hashlib.sha256(raw).hexdigest() == expected_sha256,
        "Original owner-verified evidence does not match the externally pinned digest")
    original = json.loads(raw)
    require(len(original["qualification"]) == 2 and len(original["native"]) == 2, "Original required-controller observations are incomplete")
    require(not output.is_relative_to(checkout) or __import__("subprocess").run(
        ["git", "check-ignore", "-q", "--", str(output)], cwd=checkout, check=False).returncode == 0, "Repository control target is not ignored")
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    runner_owner = load(checkout / "tools/_qualification_process.py", "host_missing_control_process")
    runner = runner_owner.CommandRunner(output / "commands")
    result = {"schema_version": "configured-host-missing-controls/v1", "mode": "missing_controls", "state": "incomplete",
        "started_utc": datetime.now(timezone.utc).isoformat(), "original_observations_sha256": expected_sha256,
        "original_observations": str(original_path), "original_report_refs": [digest(item) for item in original["qualification"]],
        "original_core_scope": "externally pinned original owner-verified dated source-native snapshots; not reminted or promoted",
        "original_native_package_generations": [item["runtime"]["package_source_sha256"] for item in original["qualification"]],
        "current_control_owner_generation": capture_owner_generation(),
        "original_package_generation_state": "historical_snapshot; current read and transport controls use their separately observed package generation",
        "native_package": str(Path(evidence_wiki.__file__).resolve()), "installed_root": str(installed),
        "cases": {}, "entrypoints": [], "console_responses": [], "stdio_responses": [],
        "limitations": ["Original observer, qualifier and model execution services are not reconstructed or invoked.",
            "Journal authentication remains the original owner-verified snapshot; current controls compare exact pinned records and native file identities.",
            "Fresh native challenges establish current selected credential usability only; original semantic qualification remains dated.",
            "Installed console/stdio reads and a trusted process cancellation are separate observed cells; the prior incomplete phase is not marked passed."]}

    def save():
        result["command_receipts"] = copy.deepcopy(runner.records)
        (output / "observations.json").write_bytes(encoded(result))

    save()
    recipe = resource_document("example/unattended-host/v1")
    support_path = original_path.parent / "native-recipe/support.py"
    require(recipe["sha256"] == original["recipe_sha256"] and support_path.read_text(encoding="utf-8")
        == json.loads(recipe["content"])["files"]["support.py"], "Retained native selection helper differs from its canonical original recipe")
    support = load(support_path, "retained_host_control_support")
    controller = owner("run_controller")
    authority = owner("_evidence_authority")
    strict = owner("_strict_evidence")
    roots, policies, profiles, contexts = [], [], [], []
    closings = []

    def native_records(root, run_id):
        events = controller._bounded_journal(controller.events_path(root, run_id))["events"]
        return [{**event["data"]["record"], "event_ref": event["event_id"]} for event in events
            if (event.get("data") or {}).get("record", {}).get("schema_version") == JOURNAL]

    try:
        for index, name in enumerate(("one", "two")):
            report = next(item for item in original["qualification"] if item["run_id"] == "native-" + name)
            root = original_path.parent / name / "workspace"
            run_id = report["run_id"]
            state_path = controller.run_state_path(root, run_id)
            state_raw = state_path.read_bytes()
            binding = json.loads(state_raw)["research_session"]
            require(binding["target"] == str(root) and digest(binding) == report["binding_id"], "Original native run binding differs from its pinned observation")
            policy = RunPolicy(binding["policy"])
            records = native_records(root, run_id)
            require(records == original["native"][index] and digest_record(records[-1]) == report["journal_head_ref"], "Current journal differs from original owner custody")
            profile = next(item["data"]["profile"] for item in records if item["kind"] == "execution_begin")
            require(digest(profile) == binding["execution_profile_id"], "Original declared execution profile differs from its binding")
            base = root.parent
            context = HostContext([root], support.GRANTS | {"host_qualification.current", "service_authentication.status", "onboarding.bootstrap"},
                "fixture-host", [root], authority_file=base / "fixture-private/authority.json", state_directory=base / "fixture-private",
                mutation_control_directory=base / "fixture-control",
                controller_refs={"host_worker": "generator-controller", "generator": "generator-controller", "evaluator": "evidence-controller"},
                role_controller_refs=tuple((role["config_ref"], role["role"], role["controller_ref"]) for role in profile["roles"]))
            roots.append(root)
            policies.append(policy.to_dict())
            profiles.append(profile)
            contexts.append(context)
            with context.activate():
                selected = _host_selection()
                require(selected == binding["host_context_sha256"], "Original trusted root/reference/grant/authority selection is no longer current")
                config = strict.configuration(root)
                trust = authority.load_trust(root, config, datetime.now(timezone.utc))
                auth_raw = context.authority_file.read_bytes()
                auth_info = context.authority_file.stat()
                attempts = [item["data"]["attempt"] for item in records if item["kind"] == "execution_attempt"]
                latest = {item["data"]["observation"]["attempt_id"]: item["data"]["observation"] for item in records if item["kind"] == "execution_observation"}
                require(len(attempts) == 3 and all(latest.get(item["attempt_id"], {}).get("final_accounting") is True for item in attempts),
                    "Original native attempt custody is not complete")
                applications = [item["data"] for item in records if item["kind"] == "execution_application" and "envelope" in item["data"]]
                require(len(applications) == 2, "Original native evaluator attestation inventory changed")
                verified = []
                identities = []
                generator = None
                for application in applications:
                    envelope = application["envelope"]
                    authenticated = authority.verify_attestation(envelope, "evaluator", trust, datetime.now(timezone.utc))
                    require(authenticated.get("authenticated") is True, "Retained native evaluator credential is not current")
                    selected_role = next(role for role in profile["roles"] if role["config_ref"] == application["profile_ref"])
                    require(authenticated["controller"] == selected_role["controller_ref"], "Retained native reviewer controller changed")
                    with strict.sibling("_evidence_usage").current_view(root, config) as view:
                        strict.validate_review(root, config, envelope, view)
                    auth = envelope["authentication"]
                    identities.append(SigningIdentity(application["profile_ref"], auth["principal"], auth["key_id"], authenticated["controller"],
                        "evaluator", bytes.fromhex(trust["policy"]["principals"][auth["principal"]]["keys"][auth["key_id"]])))
                    authored = envelope["payload"]["body"]["review"]["generator"]
                    generator_auth = authority.verify_attestation(authored, "generator", trust, datetime.now(timezone.utc))
                    require(generator_auth.get("authenticated") is True, "Retained native generator credential is not current")
                    generator_role = next(role for role in profile["roles"] if role["role"] == "host_worker" and role["config_ref"] == "generator")
                    generator_meta = authored["authentication"]
                    generator = SigningIdentity(generator_role["config_ref"], generator_meta["principal"], generator_meta["key_id"], generator_auth["controller"],
                        "generator", bytes.fromhex(trust["policy"]["principals"][generator_meta["principal"]]["keys"][generator_meta["key_id"]]))
                    verified.append({"profile_ref": application["profile_ref"], "envelope_ref": digest(envelope),
                        "current_native_authentication": authenticated, "current_native_review_validation": "passed"})
                    result["cases"]["retained_review_" + name + "_" + application["profile_ref"]] = verified[-1]
                    save()
                signer = HostReviewSigner(trust["policy"]["policy_id"], trust["policy"]["policy_revision"], generator, identities, trust["policy"]["expires_at"])
                require("sha256:" + signer.sha256 == binding["signer_id"], "Selected native signing identities differ from the original trusted service descriptor")
                for identity in (generator, *identities):
                    challenge = {"schema_version": "research-host-read-control-challenge/v1", "original_binding_id": report["binding_id"],
                        "profile_ref": identity.config_ref, "nonce": secrets.token_hex(16)}
                    proof = signer._sign(challenge, identity, datetime.now(timezone.utc).isoformat())
                    checked = authority.verify_attestation(proof, identity.role, trust, datetime.now(timezone.utc))
                    require(checked.get("authenticated") is True and checked.get("controller") == identity.controller_ref,
                        "Fresh selected native credential challenge failed")
                    result["cases"]["fresh_native_challenge_" + name + "_" + identity.config_ref] = {
                        "scope": "current selected native credential usability only", "proof": proof, "verified": checked}
                    save()
                revoked = json.loads(auth_raw)
                revoked["revoked_keys"].append(identities[0].key_id)
                try:
                    context.authority_file.write_bytes(encoded(revoked))
                    drifted = _host_selection()
                    require(drifted != selected, "Native revocation did not invalidate the original authority selection hash")
                    now = datetime.now(timezone.utc)
                    revoked_trust = authority.load_trust(root, config, now)
                    rejection = authority.verify_attestation(applications[0]["envelope"], "evaluator", revoked_trust, now)
                    result["cases"]["authority_selection_revocation_" + name] = {"original_selection": selected, "changed_selection": drifted,
                        "original_binding_eligible": False, "native_verification": rejection,
                        "scope": "exact authority hash drift and current native authentication; original canonical binding refusal remains in the earlier phase trace"}
                    save()
                    require(rejection.get("authenticated") is False and rejection.get("reason") == "authentication_revoked",
                        "Native credential revocation did not match the exact frozen authentication reason")
                finally:
                    context.authority_file.write_bytes(auth_raw)
                require(_host_selection() == selected and (context.authority_file.stat().st_dev, context.authority_file.stat().st_ino)
                    == (auth_info.st_dev, auth_info.st_ino), "Original authority bytes/inode were not restored")
                require(state_path.read_bytes() == state_raw and native_records(root, run_id) == records and strict.configuration(root) == config,
                    "Read controls changed the original native run, journal or configuration")
                closings.append((context, root, run_id, state_raw, records, config, selected))
                result["cases"]["original_custody_" + name] = {"state": "unchanged", "original_binding_id": report["binding_id"],
                    "journal_head_ref": report["journal_head_ref"], "attempts": len(attempts), "final_accounting": True,
                    "current_hmac_reverification": "not_performed; opaque observer remains unavailable"}
                save()
        _remaining_controls(output, installed, roots, policies, profiles, contexts, result, runner, save)
        for context, root, run_id, state_raw, records, config, selected in closings:
            with context.activate():
                require(_host_selection() == selected and controller.run_state_path(root, run_id).read_bytes() == state_raw
                    and native_records(root, run_id) == records and strict.configuration(root) == config,
                    "Original custody changed during missing transport/process controls")
        result["state"] = "missing_controls_observed"
        save()
        return result
    except (Exception, SystemExit) as error:
        result["state"] = "incomplete"
        result["failure"] = {"type": type(error).__name__, "error_code": getattr(error, "error_code", None),
            "details": getattr(error, "details", None), "reason": str(error)[:256]}
        save()
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--installed-root", type=Path, required=True)
    parser.add_argument("--checkout-root", type=Path, required=True)
    parser.add_argument("--native-root", type=Path, required=True)
    parser.add_argument("--controls-from", type=Path)
    parser.add_argument("--controls-sha256")
    args = parser.parse_args(argv)
    if args.controls_from:
        require(args.controls_sha256 is not None, "Missing controls require an externally pinned original SHA-256")
        result = missing_controls(args.output.resolve(), args.installed_root.resolve(), args.checkout_root.resolve(), args.native_root.resolve(),
            args.controls_from.resolve(), args.controls_sha256)
    else:
        require(args.controls_sha256 is None, "Original SHA-256 is only valid with missing-controls input")
        result = qualify(args.output.resolve(), args.installed_root.resolve(), args.checkout_root.resolve(), args.native_root.resolve())
    print(json.dumps({"state": result["state"], "cases": sorted(result["cases"]), "report": str(args.output / "observations.json")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
