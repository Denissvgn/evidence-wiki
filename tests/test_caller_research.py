"""Current-caller advice, owner coordination and verified research outcomes."""

import contextlib
import io
import json
from datetime import datetime, timezone

import pytest

from evidence_wiki._pack_io import canonical
from evidence_wiki.pack_discovery import owner
from evidence_wiki.planning import compile_plan
from evidence_wiki.research_actions import guidance
from evidence_wiki.research_completion import completion
from evidence_wiki.research_operations import run_operation
from evidence_wiki.setup_worker import execute
from tests.test_research_planning import request
from tests.test_strict_evidence import host as host


@pytest.fixture
def workspace(tmp_path):
    plan = compile_plan(canonical(request(tmp_path)))
    with contextlib.redirect_stdout(io.StringIO()):
        for operation in ("initialize", "intake", "coverage", "inventory"):
            execute(operation, plan, datetime.now(timezone.utc).isoformat())
    return tmp_path / "workspace"


def files(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_readonly_advice_and_no_fabricated_completion(workspace):
    before = files(workspace)
    result = guidance(workspace, agent_id="caller")
    assert not result["research_complete"] and not result["actions_executed"]
    assert not result["cache"]["used"] and not result["cache"]["written"]
    assert result["actions"][0]["operation"] == "start", result
    assert all(not action["authorized"] for action in result["actions"])
    exported = completion(workspace)
    assert not exported["research_complete"] and exported["original_outcomes"][0]["original_id"] == "q1"
    assert files(workspace) == before


def test_caller_run_start_resume_heartbeat_and_owned_claim(workspace):
    result = run_operation(workspace, "start", agent_id="caller", run_id="caller-run")
    assert result["run"]["caller_context"]["context_id"]
    assert result["run"]["academic_provider_request_accounting"]
    result = run_operation(workspace, "resume", agent_id="caller", run_id="caller-run")
    assert any(action["operation"] == "claim" for action in result["actions"])
    run_operation(workspace, "heartbeat", agent_id="caller", run_id="caller-run")
    claimed = owner("question_claim").run_claim(workspace, slug="q1", agent_id="caller")
    assert claimed["applied"]
    result = guidance(workspace, agent_id="caller", run_id="caller-run")
    assert any(action["operation"] == "retrieve" for action in result["actions"])
    with pytest.raises(owner("question_claim").ClaimError):
        owner("question_claim").run_claim(workspace, slug="q1", agent_id="another")


@pytest.mark.parametrize("operation", ["heartbeat", "transition", "event", "finish", "recover"])
def test_wrong_owner_cannot_mutate_caller_run(workspace, operation):
    run_operation(workspace, "start", agent_id="caller", run_id="owned")
    controller = owner("run_controller")
    args = ["--project-root", str(workspace), operation, "--run-id", "owned", "--agent-id", "other"]
    if operation == "transition":
        args += ["--to-state", "planned"]
    if operation == "event":
        args += ["--event-type", "checkpoint", "--message", "Do not transfer"]
    if operation == "finish":
        args += ["--final-verdict", "failed"]
    before = files(workspace)
    with pytest.raises(Exception) as caught:
        getattr(controller, "run_" + operation)(workspace, controller.parse_args(args))
    assert caught.value.error_code == "RUN_CALLER_CONTEXT_CONFLICT"
    assert files(workspace) == before


@pytest.mark.parametrize(
    "path", ["AGENTS.md", "skills/research-run.md", "research.yml", "docs/research-requirements.json"]
)
def test_changed_controls_cannot_resume_or_claim_completion(workspace, path):
    run_operation(workspace, "start", agent_id="caller", run_id="owned")
    selected = workspace / path
    selected.write_text(selected.read_text() + "\n ")
    with pytest.raises(owner("run_controller").RunControllerError):
        run_operation(workspace, "resume", agent_id="caller", run_id="owned")
    result = completion(workspace)
    assert not result["research_complete"] and result["publication"] is None


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 0, -1])
def test_claim_and_run_recovery_thresholds_are_bounded(workspace, value):
    owner("question_claim").run_claim(workspace, slug="q1", agent_id="caller")
    with pytest.raises(owner("question_claim").ClaimError):
        owner("question_claim").run_claim(workspace, slug="q1", agent_id="other", steal=True, if_older_than=value)
    with pytest.raises(owner("run_controller").RunControllerError):
        owner("run_controller").require_stale_threshold(value, command="adopt")


def block_request(root, *, url="https://example.org/study", scope=None):
    source = owner("source_requests")
    args = [
        "--project-root",
        str(root),
        "add",
        "--kind",
        "web",
        "--query-or-identifier",
        url,
        "--rationale",
        "Need the complete original",
        "--question-slug",
        "q1",
    ]
    for k, v in (scope or {}).items():
        args += ["--scope", k + "=" + v]
    request = source.run_add(source.parse_args(args))["request"]
    owner("question_claim").run_claim(root, slug="q1", agent_id="caller")
    owner("question_resolve").run_block(
        root, slug="q1", agent_id="caller", blocked_reason="Need complete evidence", request_id=[request["request_id"]]
    )
    return request


def test_host_capture_closes_request_and_replay_preserves_original(workspace):
    from evidence_wiki.research_operations import ingest
    from evidence_wiki.source_delivery import deliver
    from tests.test_source_capabilities import capture_request, tools_manifest

    request = block_request(workspace)
    run_operation(workspace, "start", agent_id="caller", run_id="capture-run")
    value = capture_request(b"Retained reflectance observation: 0.74 for the selected surface.")
    value["capture"]["request_id"] = request["request_id"]
    value["capture"]["scope"] = {}
    result = deliver(
        canonical(value), target=workspace, path="raw/papers/capture.md", host_tools=canonical(tools_manifest())
    )
    original = (workspace / result["path"]).read_bytes()
    result = ingest(
        workspace,
        agent_id="caller",
        run_id="capture-run",
        request_id=request["request_id"],
        source_path="raw/papers/capture.md",
    )
    assert result["request"]["request"]["status"] == "fulfilled"
    assert result["reopened"]
    replay = ingest(
        workspace,
        agent_id="caller",
        run_id="capture-run",
        request_id=request["request_id"],
        source_id=result["source"]["source_id"],
    )
    assert not replay["reopened"] and not replay["request"]["updated"]
    assert (workspace / "raw/papers/capture.md").read_bytes() == original
    assert not completion(workspace)["research_complete"]


def test_builtin_web_capture_and_verified_ingestion(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from evidence_wiki.research_operations import acquire, ingest

    value = request(tmp_path)
    value["request"]["payload"]["authority"]["allowed_actions"].append("acquisition")
    value["request"]["payload"]["authority"]["source_scope"] = ["https://example.org/"]
    value["request"]["payload"]["budgets"].update(downloads=2, bytes=100000)
    value["decisions"].update(
        acquisition=["web"], allowed_domains=["example.org"], raw_roots=["raw/papers", "raw/links", "raw/web"]
    )
    plan = compile_plan(canonical(value))
    with contextlib.redirect_stdout(io.StringIO()):
        for operation in ("initialize", "intake", "coverage", "inventory"):
            execute(operation, plan, datetime.now(timezone.utc).isoformat())
    root = tmp_path / "workspace"
    selected = block_request(root)
    run_operation(root, "start", agent_id="caller", run_id="web-run")
    data = b"<html><head><title>Observed surface measurements</title></head><body><h1>Observed surface measurements</h1><p>The retained selected document reports measured reflectance of 0.74 for the selected surface, with context and units.</p></body></html>"
    calls = []

    def response(url, config, args):
        owner("fetch_sources").validate_https_url(
            url, allowed_domains=config["allowed_domains"], resolve_hostnames=False
        )
        calls.append(url)
        return SimpleNamespace(
            final_url=url,
            content=data,
            byte_count=len(data),
            content_type="text/html",
            http_status=200,
            redirect_chain=[],
            tls_verified=True,
        )

    monkeypatch.setattr(owner("fetch_sources"), "web_fetch_url", response)
    result = acquire(
        root, agent_id="caller", run_id="web-run", request_id=selected["request_id"], url="https://example.org/study"
    )
    assert not result["request_fulfilled"]
    result = ingest(
        root,
        agent_id="caller",
        run_id="web-run",
        request_id=selected["request_id"],
        source_path=result["capture"]["target_path"],
    )
    assert result["request"]["request"]["status"] == "fulfilled" and result["reopened"]
    assert calls == ["https://example.org/study"]


def freeze_reference_request(host):
    import yaml

    from tests._assessment_fixture import QUESTION
    from tests.test_strict_evidence import save_claims

    path = host.root / ("wiki/questions/" + QUESTION + ".md")
    text = path.read_text()
    _, header, body = text.split("---", 2)
    page = yaml.safe_load(header)
    page["metadata"] = {
        "research_id": QUESTION,
        "original_ids_json": json.dumps([QUESTION]),
        "original_text": page["question"],
    }
    path.write_text("---\n" + yaml.safe_dump(page, sort_keys=False) + "---" + body)
    value = request(host.root.parent)
    value["request"]["payload"]["target"]["relative_path"] = host.root.name
    value["request"]["payload"]["questions"] = [{"id": QUESTION, "text": page["question"]}]
    (host.root / "docs/research-requirements.json").write_bytes(
        canonical({"schema_version": "evidence-research-requirements/v1", "request": value["request"], "decisions": {}})
    )
    save_claims(host)


def test_original_completion_uses_current_strict_release(host):
    from tests.test_strict_evidence import review

    freeze_reference_request(host)
    assert not completion(host.root)["research_complete"]
    review(host)
    result = completion(host.root)
    assert result["research_complete"], result
    assert result["publication"]["questions"][0]["claims"][0]["verification"]["review"]["passed"]
    host.claims["claims"][0]["text"] = "Changed after independent review"
    from tests.test_strict_evidence import save_claims

    save_claims(host)
    result = completion(host.root, allow_partial=True)
    assert not result["research_complete"] and not result["original_outcomes"][0]["accepted"]


def test_progress_has_unknown_grading_and_separate_estimates(workspace, tmp_path):
    from evidence_wiki.research_progress import progress, save_report

    run_operation(workspace, "start", agent_id="caller", run_id="metrics")
    before = files(workspace)
    result = progress(
        workspace,
        run_id="metrics",
        estimates={"basis": "caller_estimate", "tokens": 123, "seconds": 10, "tool_calls": 2},
    )
    assert result["semantic_evaluation"]["unsupported_claim_escapes"]["value"] is None
    assert result["measured"]["tool_calls"] is None and result["caller_estimates"]["tool_calls"] == 2
    assert result["measured"]["question_outcomes"] == {"open": 1}
    assert files(workspace) == before
    save_report(tmp_path / "observed.json", result, target=workspace)
    assert (tmp_path / "observed.json").stat().st_mode & 0o077 == 0


def test_resume_is_readonly_when_started_through_canonical_owner(workspace):
    controller = owner("run_controller")
    args = controller.parse_args(
        ["--project-root", str(workspace), "start", "--caller", "--agent-id", "caller", "--run-id", "direct"]
    )
    controller.run_start(workspace, args)
    before = files(workspace)
    run_operation(workspace, "resume", agent_id="caller", run_id="direct")
    assert files(workspace) == before
    assert not (workspace / ".locks/caller-research.lock").exists()


def test_missing_claim_clock_cannot_prove_staleness(workspace):
    import yaml

    claim = owner("question_claim")
    claim.run_claim(workspace, slug="q1", agent_id="caller")
    path = workspace / "wiki/questions/q1.md"
    _, header, body = path.read_text().split("---", 2)
    metadata = yaml.safe_load(header)
    metadata.pop("claimed_at")
    path.write_text("---\n" + yaml.safe_dump(metadata) + "---" + body)
    with pytest.raises(claim.ClaimError) as error:
        claim.run_claim(workspace, slug="q1", agent_id="another", steal=True, if_older_than=1)
    assert error.value.error_code == "CLAIM_NOT_STALE"


def test_secret_input_refuses_before_mutation(workspace, monkeypatch):
    from evidence_wiki.errors import UsageError

    secret = "actual-secret-value-for-local-fixture"
    monkeypatch.setenv("PRIVATE_TOKEN", secret)
    before = files(workspace)
    with pytest.raises(UsageError):
        run_operation(workspace, "start", agent_id=secret, run_id="unsafe")
    assert files(workspace) == before


def test_partial_capture_does_not_fulfill_full_text_request(workspace):
    from evidence_wiki.errors import UsageError
    from evidence_wiki.research_operations import ingest
    from evidence_wiki.source_delivery import deliver
    from tests.test_source_capabilities import capture_request, tools_manifest

    request = block_request(workspace)
    run_operation(workspace, "start", agent_id="caller", run_id="partial")
    value = capture_request(b"A retained excerpt only.")
    value["capture"].update(request_id=request["request_id"], content_kind="excerpt", completeness="partial", scope={})
    deliver(canonical(value), target=workspace, path="raw/papers/excerpt.md", host_tools=canonical(tools_manifest()))
    with pytest.raises(UsageError) as error:
        ingest(
            workspace,
            agent_id="caller",
            run_id="partial",
            request_id=request["request_id"],
            source_path="raw/papers/excerpt.md",
        )
    assert error.value.details["field"] == "caller_source_not_usable"
    source = owner("source_requests")
    records = source.load_requests(source.requests_path(workspace, source.load_config(workspace)))
    assert records[0]["status"] == "open"
    assert owner("question_status").collect_questions(workspace / "wiki/questions")[0]["status"] == "blocked"


def test_cli_target_routes_to_current_advice_and_supplemental_commands(workspace, capsys):
    from evidence_wiki.cli import main

    before = files(workspace)
    assert main(["agent", "--target", str(workspace), "--format", "json"]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["schema_version"] == "evidence-research-guidance/v1"
    assert not value["cache"]["written"]
    assert main(["agent", "summary", "--require", "agent acquire", "--format", "json"]) == 0
    assert len(json.loads(capsys.readouterr().out)["payload"]["operations"]) <= 64
    assert main(["agent", "research-schemas"]) == 0
    assert "agent ingest" in json.loads(capsys.readouterr().out)["commands"]
    assert files(workspace) == before


def test_missing_original_is_never_dropped_from_partial_release(host):
    from tests.test_strict_evidence import review

    freeze_reference_request(host)
    path = host.root / "docs/research-requirements.json"
    value = json.loads(path.read_text())
    value["request"]["payload"]["questions"].append(
        {"id": "missing-original", "text": "An unresolved original question"}
    )
    path.write_bytes(canonical(value))
    review(host)
    result = completion(host.root, allow_partial=True)
    assert result["status"] == "partial", result
    assert len(result["original_outcomes"]) == 2
    assert result["original_outcomes"][1]["missing_question_ids"] == ["missing-original"]
    assert not result["research_complete"] and not result["accounting_complete"]


def test_strict_run_finish_uses_release_owner_not_a_run_label(host):
    from tests.test_strict_evidence import review

    freeze_reference_request(host)
    run_operation(host.root, "start", agent_id="caller", run_id="completion")
    controller = owner("run_controller")
    for state in ("planned", "answering", "verifying"):
        controller.run_transition(
            host.root,
            controller.parse_args(
                [
                    "--project-root",
                    str(host.root),
                    "transition",
                    "--run-id",
                    "completion",
                    "--agent-id",
                    "caller",
                    "--to-state",
                    state,
                ]
            ),
        )
    args = controller.parse_args(
        [
            "--project-root",
            str(host.root),
            "finish",
            "--run-id",
            "completion",
            "--agent-id",
            "caller",
            "--final-verdict",
            "complete",
        ]
    )
    with pytest.raises(controller.RunControllerError):
        controller.run_finish(host.root, args)
    review(host)
    result = controller.run_finish(host.root, args)
    assert result["final_verdict"] == "complete"
    assert completion(host.root)["research_complete"]


def test_interrupted_fulfillment_resumes_reopen_without_duplicate_delivery(workspace, monkeypatch):
    from evidence_wiki.research_operations import ingest
    from evidence_wiki.source_delivery import deliver
    from tests.test_source_capabilities import capture_request, tools_manifest

    request = block_request(workspace)
    run_operation(workspace, "start", agent_id="caller", run_id="interrupted")
    value = capture_request(b"Retained complete original content for the selected study.")
    value["capture"].update(request_id=request["request_id"], scope={})
    deliver(canonical(value), target=workspace, path="raw/papers/original.md", host_tools=canonical(tools_manifest()))
    resolver = owner("question_resolve")
    reopen = resolver.run_reopen

    def interrupted(*args, **kwargs):
        raise OSError("simulated interrupted reopen")

    monkeypatch.setattr(resolver, "run_reopen", interrupted)
    with pytest.raises(OSError):
        ingest(
            workspace,
            agent_id="caller",
            run_id="interrupted",
            request_id=request["request_id"],
            source_path="raw/papers/original.md",
        )
    source = owner("source_requests")
    rows = source.load_requests(source.requests_path(workspace, source.load_config(workspace)))
    assert rows[0]["status"] == "fulfilled"
    original = (workspace / "raw/papers/original.md").read_bytes()
    monkeypatch.setattr(resolver, "run_reopen", reopen)
    result = ingest(
        workspace,
        agent_id="caller",
        run_id="interrupted",
        request_id=request["request_id"],
        source_id=rows[0]["source_id"],
    )
    assert not result["request"]["updated"] and result["reopened"]
    assert (workspace / "raw/papers/original.md").read_bytes() == original


def test_active_run_cannot_be_silently_replaced(workspace):
    from evidence_wiki.errors import UsageError

    run_operation(workspace, "start", agent_id="caller", run_id="first")
    with pytest.raises(UsageError):
        run_operation(workspace, "start", agent_id="other", run_id="second")
    assert not (workspace / "runs/second/run-state.json").exists()
    assert owner("run_controller").load_run_state(workspace, "first")["agent_id"] == "caller"


def test_copied_checker_changes_block_resume(workspace):
    run_operation(workspace, "start", agent_id="caller", run_id="bound")
    path = workspace / "scripts/question_claim.py"
    path.write_text(path.read_text() + "\n# changed local checker\n")
    with pytest.raises(owner("run_controller").RunControllerError):
        run_operation(workspace, "resume", agent_id="caller", run_id="bound")
    result = guidance(workspace, agent_id="caller", run_id="bound")
    assert result["actions"][0]["operation"] == "repair"


def test_managed_advice_does_not_issue_or_resume_parent_orders(workspace, monkeypatch):
    monkeypatch.setattr(
        owner("_delegation_gate"),
        "live_pending_orders",
        lambda root: [{"orchestration_id": "parent", "action_id": "issued", "work_order": None}],
    )
    before = files(workspace)
    result = guidance(workspace, agent_id="worker")
    assert [row["operation"] for row in result["actions"]] == ["managed_resume"]
    assert result["actions"][0]["argv"] == []
    assert not result["actions_executed"] and files(workspace) == before


def test_explicit_stale_adoption_keeps_claim_ownership(workspace, monkeypatch):
    from datetime import timedelta

    controller = owner("run_controller")
    run_operation(workspace, "start", agent_id="caller", run_id="aging")
    owner("question_claim").run_claim(workspace, slug="q1", agent_id="caller")
    old = controller.run_staleness
    monkeypatch.setattr(
        controller,
        "run_staleness",
        lambda root, doc, threshold, **kw: old(
            root, doc, threshold, now=datetime.now(timezone.utc) + timedelta(days=2)
        ),
    )
    # State-aware inspection uses its owning module family; explicit stale
    # adoption remains owner-controlled and does not transfer a question claim.
    args = controller.parse_args(
        [
            "--project-root",
            str(workspace),
            "adopt",
            "--run-id",
            "aging",
            "--agent-id",
            "new-owner",
            "--if-stale-hours",
            "4",
        ]
    )
    adopted = controller.run_adopt(workspace, args)
    assert adopted["agent_id"] == "new-owner"
    assert adopted["recovery_history"][-1]["previous_agent_id"] == "caller"
    assert owner("question_status").collect_questions(workspace / "wiki/questions")[0]["claimed_by"] == "caller"
    with pytest.raises(controller.RunControllerError):
        run_operation(workspace, "resume", agent_id="caller", run_id="aging")


def test_failed_state_event_commit_uses_canonical_recovery(workspace, monkeypatch):
    controller = owner("run_controller")
    run_operation(workspace, "start", agent_id="caller", run_id="recovery")
    append = controller.append_event

    def fail(*args, **kwargs):
        raise OSError("simulated event persistence failure")

    monkeypatch.setattr(controller, "append_event", fail)
    with pytest.raises((OSError, controller.RunControllerError)):
        run_operation(workspace, "heartbeat", agent_id="caller", run_id="recovery")
    monkeypatch.setattr(controller, "append_event", append)
    args = controller.parse_args(
        ["--project-root", str(workspace), "recover", "--run-id", "recovery", "--agent-id", "caller"]
    )
    restored = controller.run_recover(workspace, args)
    assert "_pending_event" not in restored and restored["agent_id"] == "caller"
    assert restored["recovery_history"][-1]["ownership_changed"] is False
    assert run_operation(workspace, "resume", agent_id="caller", run_id="recovery")["run"]["agent_id"] == "caller"


def test_revoked_review_cannot_reuse_completed_output(host):
    from tests.test_strict_evidence import review

    freeze_reference_request(host)
    envelope = review(host)
    assert completion(host.root)["research_complete"]
    host.policy["revoked_envelopes"].append(
        owner("_evidence_revision").content_id("evidence-authenticated-payload/v1", envelope["payload"])
    )
    host.save_policy()
    assert not completion(host.root)["research_complete"]


def test_computation_progress_and_due_advice_do_not_dispatch(tmp_path):
    from evidence_wiki.research_progress import progress
    from tests._computation_fixture import definition

    value = request(tmp_path)
    declaration = definition()
    declaration["graphs"]["constant"] = {
        "description": "Declared constant calculation",
        "constants": {"amount": {"value": "3", "unit": "units"}},
        "inputs": {},
        "nodes": {"total": {"expr": "amount * 2", "unit": "units"}},
        "output_mapping": {"total": "total"},
        "output_page": None,
    }
    declaration["cadence"]["review"] = {
        "description": "Inspect local status",
        "trigger": {"type": "fixed_date", "at": "2026-09-20"},
        "lead_alerts": [],
        "action": {"kind": "status_flag", "target": "ready"},
    }
    value["decisions"]["computation"] = declaration
    plan = compile_plan(canonical(value))
    with contextlib.redirect_stdout(io.StringIO()):
        for operation in ("initialize", "intake", "coverage", "inventory"):
            execute(operation, plan, datetime.now(timezone.utc).isoformat())
    root = tmp_path / "workspace"
    run_operation(root, "start", agent_id="caller", run_id="calculation")
    before = files(root)
    result = progress(root, run_id="calculation")
    assert result["computation"]["measurement"]["operations"] > 0
    assert result["computation"]["measurement"]["records"] == 0
    assert result["computation"]["observation_seconds"] >= 0
    result = guidance(root, agent_id="caller", run_id="calculation")
    due = next(row for row in result["actions"] if row["operation"] == "inspect_schedule")
    assert due["parameters"]["result_id"] and not due["parameters"]["dispatch_authorized"]
    assert files(root) == before


def test_installed_assets_refuse_research_writes():
    from evidence_wiki._script_host import shared_assets_root
    from evidence_wiki.errors import UsageError

    with pytest.raises(UsageError) as error:
        run_operation(shared_assets_root() / "workspace-template", "start", agent_id="caller", run_id="do-not-write")
    assert error.value.details["field"] == "catalog_installed_assets_forbidden"


def test_readonly_advice_never_loads_unqualified_provider(workspace, monkeypatch):
    import yaml

    config = owner("source_requests").load_config(workspace)
    config["integrations"]["discovery"] = {"enabled": False, "providers": ["unqualified-provider"]}
    (workspace / "research.yml").write_text(yaml.safe_dump(config))
    monkeypatch.setattr(
        owner("workspace_status"), "run_status_report", lambda *a, **k: pytest.fail("unsafe status provider activation")
    )
    monkeypatch.setattr(
        owner("strict_evidence"), "run_operation", lambda *a, **k: pytest.fail("unsafe strict provider activation")
    )
    before = files(workspace)
    result = guidance(workspace)
    assert result["status"]["availability"] == "unavailable"
    assert result["actions"][0]["argv"][4:6] == ["agent", "inspect"]
    assert completion(workspace)["publication"] is None
    assert files(workspace) == before


def test_duplicate_request_preserves_additional_question_links(workspace, monkeypatch):
    intake = owner("intake_questions")
    intake.run_intake_document(
        workspace,
        {"schema_version": "1.0", "questions": [{"id": "q2", "question": "Another question"}]},
        dry_run=False,
        from_file_label="explicit request",
    )
    first = block_request(workspace)
    source = owner("source_requests")
    args = source.parse_args(
        [
            "--project-root",
            str(workspace),
            "add",
            "--kind",
            "web",
            "--query-or-identifier",
            first["query_or_identifier"],
            "--rationale",
            "Same source supports another question",
            "--question-slug",
            "q2",
        ]
    )
    changed = source.run_add(args)
    assert not changed["created"] and changed["updated"]
    assert changed["request"]["question_slugs"] == ["q1", "q2"]
    assert "Linked additional questions" in source.render_text_report(changed)
    assert source.run_add(args)["request"] == changed["request"]
    owner("question_claim").run_claim(workspace, slug="q2", agent_id="caller")
    assert owner("question_resolve").run_block(
        workspace, slug="q2", agent_id="caller", blocked_reason="Same gap", request_id=[first["request_id"]]
    )


def test_one_source_can_be_fulfilled_while_another_question_blocker_remains(workspace):
    from evidence_wiki.research_operations import ingest
    from evidence_wiki.source_delivery import deliver
    from tests.test_source_capabilities import capture_request, tools_manifest

    source = owner("source_requests")
    first = source.run_add(
        source.parse_args(
            [
                "--project-root",
                str(workspace),
                "add",
                "--kind",
                "web",
                "--query-or-identifier",
                "https://example.org/study",
                "--rationale",
                "Need first record",
                "--question-slug",
                "q1",
            ]
        )
    )["request"]
    second = source.run_add(
        source.parse_args(
            [
                "--project-root",
                str(workspace),
                "add",
                "--kind",
                "web",
                "--query-or-identifier",
                "https://example.org/second",
                "--rationale",
                "Need both records",
                "--question-slug",
                "q1",
            ]
        )
    )["request"]
    owner("question_claim").run_claim(workspace, slug="q1", agent_id="caller")
    owner("question_resolve").run_block(
        workspace,
        slug="q1",
        agent_id="caller",
        blocked_reason="Need both originals",
        request_id=[first["request_id"], second["request_id"]],
    )
    run_operation(workspace, "start", agent_id="caller", run_id="two-sources")
    value = capture_request(b"Retained complete first evidence document.")
    value["capture"].update(request_id=first["request_id"], scope={})
    deliver(canonical(value), target=workspace, path="raw/papers/first.md", host_tools=canonical(tools_manifest()))
    result = ingest(
        workspace,
        agent_id="caller",
        run_id="two-sources",
        request_id=first["request_id"],
        source_path="raw/papers/first.md",
    )
    assert result["request"]["request"]["status"] == "fulfilled" and not result["reopened"]
    assert result["pending_questions"] == [
        {"slug": "q1", "reason": "QUESTION_BLOCKERS_UNFULFILLED", "status": "blocked"}
    ]


@pytest.mark.parametrize("name", ["yaml.py", "shadow.pyc", "extension.so"])
def test_unexpected_runtime_files_never_receive_a_caller_binding(workspace, name):
    (workspace / "scripts" / name).write_bytes(b"untrusted runtime content")
    result = guidance(workspace, agent_id="caller")
    assert result["actions"][0]["operation"] == "repair"
    assert "caller_workspace_scripts_not_qualified" in result["actions"][0]["reasons"]
    assert not (workspace / "runs/unqualified/run-state.json").exists()


def test_unknown_requirements_version_cannot_start_a_caller_run(workspace):
    from evidence_wiki.errors import UsageError

    path = workspace / "docs/research-requirements.json"
    value = json.loads(path.read_text())
    value["schema_version"] = "evidence-research-requirements/v99"
    path.write_bytes(canonical(value))
    with pytest.raises(UsageError):
        run_operation(workspace, "start", agent_id="caller", run_id="unsupported")
    assert not (workspace / "runs/unsupported/run-state.json").exists()
