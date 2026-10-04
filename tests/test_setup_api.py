"""Concise public setup and inert recovery observations over real owners."""

import json
import os

import pytest

from evidence_wiki import Onboarding
from evidence_wiki._pack_io import canonical
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.host_context import HostContext
from evidence_wiki.planning import compile_plan, plan_identity
from evidence_wiki.run_policy import RunPolicy
from tests.test_research_planning import question_plan, request
from tests.test_run_policy import policy_document
from tests.test_workspace_application import in_process as in_process

GRANTS = {"onboarding.prepare", "onboarding.check_plan", "onboarding.apply", "onboarding.setup_status"}


def inputs(root, text="  ¿Qué muestran los datos?\r\n"):
    seed = compile_plan(canonical(request(root)))
    evidence = seed["strict"]["policy"]
    evidence["human_review"] = False
    evidence["instructions"].pop("docs/research-requirements.json", None)
    declaration = policy_document()
    declaration.update(objective=text, human_review=False)
    declaration["scope"] = {"question_ids": ["q1"], "source_scope": [str(root / "workspace/raw")]}
    policy = RunPolicy(declaration)
    host = {"target": {"writable_root": str(root), "relative_path": "workspace"},
        "authority": {"role": "external_host", "reference": "configured-setup", "allowed_actions": ["local_setup", "local_research"],
            "source_scope": [], "writable_roots": [str(root)], "credential_references": []},
        "scope": [{"name": "jurisdiction", "value": "Spain"}], "evidence_policy": evidence,
        "question_plans": [question_plan("q1")]}
    return policy, host


def context(root, *, write=True, grants=GRANTS, **options):
    return HostContext([root], grants, "setup", [root] if write else [], **options)


def files(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def prepare(handle, root):
    policy, selections = inputs(root)
    result = handle.prepare(policy.to_dict()["objective"], host_scope=selections, policy=policy)
    assert result["status"] == "prepared" and result["plan_ready"], result
    return result["plan"]


def test_concise_public_setup_real_worker_check_resume_and_readiness(tmp_path, monkeypatch, capsys):
    ambient = {"EVIDENCE_WIKI_AUTHORITY_FILE": str(tmp_path.parent / "unselected-authority.json"),
               "EVIDENCE_WIKI_STATE_DIR": str(tmp_path.parent / "unselected-state"), "OPENAI_API_KEY": "synthetic-provider-value"}
    for name, value in ambient.items():
        monkeypatch.setenv(name, value)
    with context(tmp_path).activate(), Onboarding.open(allowed_roots=[tmp_path], allow=["apply"]) as handle:
        plan = prepare(handle, tmp_path)
        before = files(tmp_path)
        status = handle.setup_status(plan)
        assert status["status"] == "not_applied" and status["ownership_valid"]
        assert status["setup_ready"] is False and not (tmp_path / ".evidence-wiki").exists()
        assert status["research_compatibility"]["state"] == "blocked"
        assert "frozen_source_scope_missing" in status["research_compatibility"]["gaps"]
        assert files(tmp_path) == before and handle.check_plan(plan)["status"] == "current"
        applied = handle.apply(plan)
        assert applied["setup_ready"] and applied["evidence_empty"]
        assert not applied["research_complete"] and not applied["claims_verified"]
        before = files(tmp_path / "workspace")
        assert plan["questions"]["rows"][0]["original_text"] == plan["request"]["request"]["payload"]["goal"]
        status = handle.setup_status(plan)
        assert status["status"] == "complete" and status["recorded_setup_ready"] is True
        assert status["setup_ready"] is None and status["recovery"] == "recheck"
        assert handle.check_plan(plan)["status"] == "current"
        again = handle.apply(plan)
        assert again["transaction_id"] == applied["transaction_id"] and again["setup_ready"]
        assert files(tmp_path / "workspace") == before
    assert {name: os.environ[name] for name in ambient} == ambient
    assert capsys.readouterr().out == ""


def test_read_only_context_prepares_checks_observes_without_bootstrap_or_write(tmp_path):
    with context(tmp_path, write=False, grants=GRANTS - {"onboarding.apply"}).activate(), Onboarding.open(allowed_roots=[tmp_path]) as handle:
        before = files(tmp_path)
        plan = prepare(handle, tmp_path)
        assert handle.check_plan(plan)["status"] == "current"
        assert handle.setup_status(plan)["recovery"] == "apply" and files(tmp_path) == before
        with pytest.raises(EvidenceWikiError) as error:
            handle.apply(plan)
        assert error.value.error_code == "ONBOARDING_AUTHORITY_REQUIRED" and files(tmp_path) == before


def test_incomplete_prepare_terminal_and_process_exit_or_closed_handle_contained(tmp_path, monkeypatch):
    with context(tmp_path).activate():
        handle = Onboarding.open(allowed_roots=[tmp_path])
        before = files(tmp_path)
        result = handle.prepare("  Keep this text.\r\n")
        assert result["status"] == "insufficient_context" and result["terminal"] and result["plan"] is None
        assert result["next_action"] == "stop_context" and files(tmp_path) == before
        import evidence_wiki.request_compiler as module
        def exited(*args, **kwargs):
            raise SystemExit(9)
        monkeypatch.setattr(module, "compile_request", exited)
        with pytest.raises(EvidenceWikiError) as error:
            handle.prepare("text")
        assert error.value.error_code == "LIBRARY_PROCESS_EXIT"
        handle.close()
        with pytest.raises(EvidenceWikiError) as error:
            handle.prepare("text")
        assert error.value.error_code == "WORKSPACE_UNREADABLE" and files(tmp_path) == before


@pytest.mark.parametrize("selection", ["authority_file", "state_directory", "mutation_control_directory"])
def test_protected_selection_refuses_before_journal_and_workspace_effect(tmp_path, selection):
    selected, protected = tmp_path / "selected", tmp_path / "protected"
    selected.mkdir()
    protected.mkdir(mode=0o700)
    value = protected / "authority.json" if selection == "authority_file" else protected
    if selection == "authority_file":
        value.write_text("{}")
        value.chmod(0o600)
    with context(selected, **{selection: value}).activate(), Onboarding.open(allowed_roots=[selected], allow=["apply"]) as handle:
        plan = prepare(handle, selected)
        before = files(tmp_path)
        with pytest.raises(EvidenceWikiError) as error:
            handle.apply(plan)
        assert error.value.error_code == "ONBOARDING_ENVIRONMENT_INCOMPATIBLE"
        assert error.value.details["field"] == "setup_explicit_protected_worker_bridge_unavailable"
        assert files(tmp_path) == before and not (selected / ".evidence-wiki").exists()
        inspection = handle.setup_status(plan)
        assert inspection["status"] == "not_applied" and not inspection["actions_executed"]
        assert files(tmp_path) == before


def test_status_preserves_edits_and_recomputed_foreign_binding_refuses(tmp_path, in_process):
    with Onboarding.open(allowed_roots=[tmp_path], allow=["apply"]) as handle:
        plan = prepare(handle, tmp_path)
        handle.apply(plan)
        page = next((tmp_path / "workspace/wiki/questions").glob("*.md"))
        page.write_text(page.read_text() + "\nUser edit remains.\n")
        before = files(tmp_path)
        status = handle.setup_status(plan)
        assert status["status"] == "conflict" and status["recovery"] == "inspect_conflict"
        assert not status["ownership_valid"] and files(tmp_path) == before
        changed = json.loads(canonical(plan))
        changed["bindings"]["target"]["target"]["relative_path"] = "other"
        changed["plan_id"] = plan_identity(changed)
        with pytest.raises(EvidenceWikiError) as error:
            handle.setup_status(changed)
        assert error.value.error_code == "ONBOARDING_PLAN_STALE" and files(tmp_path) == before


def test_json_policy_mcp_prepare_status_share_core_without_apply_permission(tmp_path):
    from evidence_wiki.onboarding_mcp import OnboardingMcpServer
    policy, selections = inputs(tmp_path)
    with context(tmp_path, write=False).activate():
        server = OnboardingMcpServer(allowed_roots=[tmp_path])
        try:
            prepared = server.call_tool("onboarding_prepare", {"objective": policy.to_dict()["objective"],
                "host_scope": selections, "policy": policy.to_dict()})
            status = server.call_tool("onboarding_setup_status", {"value": prepared["plan"]})
            assert status["status"] == "not_applied" and not status["authority_verified"]
            assert not (tmp_path / ".evidence-wiki").exists()
        finally:
            server.close()


@pytest.mark.parametrize("field,value", [("questions", {}), ("questions", {"rows": None}), ("sources", {}),
                                       ("initialization", {}), ("computation", {})])
def test_rehashed_opaque_plan_does_not_enter_unvalidated_status_fields(tmp_path, field, value):
    with Onboarding.open(allowed_roots=[tmp_path]) as handle:
        plan = prepare(handle, tmp_path)
        plan[field] = value
        plan["plan_id"] = plan_identity(plan)
        before = files(tmp_path)
        status = handle.setup_status(plan)
        assert status["status"] == "stale" and not status["ownership_valid"]
        assert status["research_compatibility"]["state"] == "unknown"
        assert files(tmp_path) == before and not (tmp_path / ".evidence-wiki").exists()


def test_checkpoint_bad_clock_is_a_conflict_and_preserves_bytes(tmp_path, in_process):
    with Onboarding.open(allowed_roots=[tmp_path], allow=["apply"]) as handle:
        plan = prepare(handle, tmp_path)
        receipt = handle.apply(plan)
        from pathlib import Path
        path = Path(receipt["checkpoint"])
        checkpoint = json.loads(path.read_bytes())
        checkpoint["clock"] = "bad"
        path.write_text(json.dumps(checkpoint))
        before = files(tmp_path)
        status = handle.setup_status(plan)
        assert status["status"] == "conflict" and status["recovery"] == "inspect_conflict"
        assert status["diagnostics"][0]["reason"] == "setup_checkpoint_clock_invalid"
        assert files(tmp_path) == before


def test_isolated_installed_owner_observes_old_unowned_workspace_without_adoption(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    with Onboarding.open(allowed_roots=[tmp_path]) as handle:
        plan = prepare(handle, tmp_path)
    target = tmp_path / "workspace"
    target.mkdir()
    marker = Path(__file__).parent / "fixtures/versioned-workspaces/0.3.0/workspace-system.yml"
    (target / "workspace-system.yml").write_bytes(marker.read_bytes())
    (target / "research.yml").write_text("project:\n  name: Retain old deployment\n")
    before = files(tmp_path)
    code = """import json,sys
from evidence_wiki import Onboarding
payload=json.load(sys.stdin)
with Onboarding.open(allowed_roots=[payload['root']]) as host:
 print(json.dumps(host.setup_status(payload['plan'])))
"""
    result = subprocess.run([sys.executable, "-B", "-I", "-c", code], input=json.dumps({"root": str(tmp_path), "plan": plan}),
                            text=True, capture_output=True, cwd=tmp_path, timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    status = json.loads(result.stdout)
    assert status["status"] == "conflict" and not status["ownership_valid"]
    assert status["versions"]["workspace"]["starter_version"] == "0.3.0" and status["versions"]["skew"]
    assert "inspect_upgrade_compatibility" in status["guidance"] and "prepare_new_target" in status["guidance"]
    assert files(tmp_path) == before and not (tmp_path / ".evidence-wiki").exists()


@pytest.mark.parametrize("raw", ["scalar-marker\n", "- list-marker\n", "null\n", "workspace_system: {}\n", "partial"])
def test_selected_workspace_metadata_kind_and_absence_remain_bounded(tmp_path, raw):
    from evidence_wiki.agent_resources import installation_metadata

    with context(tmp_path, write=False).activate(), Onboarding.open(allowed_roots=[tmp_path]) as handle:
        plan = prepare(handle, tmp_path)
        target = tmp_path / "workspace"
        target.mkdir()
        if raw == "partial":
            raw = 'workspace_system:\n  starter_version: "' + installation_metadata()["starter_version"] + '"\n'
        (target / "workspace-system.yml").write_text(raw)
        before = files(tmp_path)
        status = handle.setup_status(plan)
        assert status["status"] == "conflict" and not status["ownership_valid"]
        assert status["versions"]["skew"] is None
        assert files(tmp_path) == before and not (tmp_path / ".evidence-wiki").exists()
        if "workspace_system" in raw:
            assert "inspect_upgrade_compatibility" in status["guidance"]
        else:
            assert status["diagnostics"][0]["reason"] == "setup_workspace_metadata_invalid"
