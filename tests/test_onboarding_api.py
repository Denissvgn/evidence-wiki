"""Scoped API values, effects, lifecycle and malformed-input boundaries."""

import json
import sys
from pathlib import Path

import pytest

from evidence_wiki import Onboarding, cli
from evidence_wiki._pack_io import canonical
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.planning import compile_plan
from tests.test_research_planning import request
from tests.test_workspace_application import local_request


def test_bootstrap_cli_parity_and_closed_lifetime(capsys):
    handle = Onboarding.open()
    value = handle.bootstrap(request_id="parity")
    assert cli.main(["agent", "summary", "--request-id", "parity", "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out) == value
    assert handle.resources()["bounds"]["truncated"] is False
    handle.close()
    handle.close()
    with pytest.raises(EvidenceWikiError) as error:
        handle.bootstrap()
    assert error.value.error_code == "WORKSPACE_UNREADABLE"


def test_setup_scope_grant_and_shared_values(tmp_path):
    value = request(tmp_path)
    with Onboarding.open(allowed_roots=[tmp_path]) as handle:
        prepared = handle.plan(value)
        assert prepared == compile_plan(canonical(value))
        assert handle.check_plan(prepared)["status"] == "current"
        with pytest.raises(EvidenceWikiError) as error:
            handle.apply(prepared)
        assert error.value.error_code == "ONBOARDING_AUTHORITY_REQUIRED"
        assert not (tmp_path / "workspace").exists()
    with Onboarding.open() as handle, pytest.raises(EvidenceWikiError) as error:
        handle.plan(value)
    assert error.value.details["field"] == "onboarding_path_outside_host_scope"


def test_root_replacement_and_generation_change_refuse(tmp_path, monkeypatch):
    root = tmp_path / "selected"
    root.mkdir()
    with Onboarding.open(allowed_roots=[root]) as handle:
        root.rename(tmp_path / "previous")
        root.mkdir()
        with pytest.raises(EvidenceWikiError, match="refused"):
            handle.recipes()
    with Onboarding.open() as handle:
        monkeypatch.setattr("evidence_wiki.onboarding.generation", lambda: {})
        with pytest.raises(EvidenceWikiError) as error:
            handle.recipes()
        assert error.value.details["field"] == "onboarding_generation_changed_restart_required"


@pytest.mark.parametrize("method", ["plan", "check_plan", "revision_plan", "migration_plan", "composition_plan", "fleet_plan", "transition_plan", "instructions_plan"])
@pytest.mark.parametrize("value", [{}, [], {"schema_version": "unknown"}])
def test_malformed_documents_have_typed_errors(method, value):
    with Onboarding.open() as handle, pytest.raises(EvidenceWikiError):
        getattr(handle, method)(value)


def test_escape_and_host_authority_overlap_refuse(tmp_path, monkeypatch):
    selected = tmp_path / "selected"
    selected.mkdir()
    (selected / "escape").symlink_to(tmp_path, target_is_directory=True)
    with Onboarding.open(allowed_roots=[selected]) as handle, pytest.raises(EvidenceWikiError):
        handle.bootstrap(selected / "escape")
    monkeypatch.setenv("EVIDENCE_WIKI_STATE_DIR", str(selected / "protected"))
    with Onboarding.open(allowed_roots=[selected]) as handle, pytest.raises(EvidenceWikiError):
        handle.bootstrap(selected)


def test_setup_can_apply_via_scoped_api_and_reconcile_over_mcp(tmp_path):
    from evidence_wiki.onboarding_mcp import OnboardingMcpServer

    with Onboarding.open(allowed_roots=[tmp_path], allow=["apply"]) as host:
        prepared = host.plan(request(tmp_path))
        first = host.apply(prepared)
        assert first["setup_ready"] and not first["research_complete"] and not first["claims_verified"]
    server = OnboardingMcpServer(allowed_roots=[tmp_path], allow=["apply"])
    try:
        again = server.call_tool("onboarding_apply", {"value": prepared})
        assert again["setup_ready"] and again["plan_id"] == first["plan_id"]
        assert len(list((tmp_path / "workspace/wiki/questions").glob("*.md"))) == 1
    finally:
        server.close()


@pytest.mark.parametrize("with_source", [False, True], ids=["empty", "local_source"])
def test_setup_next_actions_execute_unchanged(tmp_path, run_package_command, with_source):
    """Execute public setup inspection and replay arrays without rebuilding their transport."""
    root = (tmp_path / "public setup").resolve()
    root.mkdir()
    value = local_request(root) if with_source else request(root)
    with Onboarding.open(allowed_roots=[root], allow=["apply"]) as host:
        plan = host.plan(value)
        result = host.apply(plan)
    assert result["status"] == "ready" and result["setup_ready"]
    assert not result["research_complete"] and not result["claims_verified"]
    target = Path(result["target"])
    before = {str(path.relative_to(target)): path.read_bytes() for path in target.rglob("*") if path.is_file()}
    executable = [row for row in result["next_actions"] if "argv" in row]
    assert [row["action"] for row in executable] == ["inspect_sources", "resume_setup"]
    assert plan["bindings"]["interpreter"]["invocation"] == sys.executable
    for action in executable:
        argv = action["argv"]
        assert argv[:4] == [plan["bindings"]["interpreter"]["invocation"], "-B", "-m", "evidence_wiki"]
        process = run_package_command(argv, timeout=120)
        assert process.returncode == 0, process.stdout + process.stderr
        assert process.stderr == ""
        observed = json.loads(process.stdout)
        if action["action"] == "inspect_sources":
            assert argv[argv.index("--target") + 1] == str(target)
            assert observed["schema_version"] == "evidence-source-inspection/v1"
            assert observed["target"]["selected"] and observed["target"]["state"] == "present"
            assert not observed["research_ready"]
            if with_source:
                assert argv[4:6] == ["agent", "source-status"]
                expected_paths = result["sources"][0]["observations"][0]["raw_paths"]
                assert [argv[index + 1] for index, arg in enumerate(argv) if arg == "--source-path"] == expected_paths
                assert len(observed["sources"]) == 1
                assert observed["sources"][0]["raw_paths"] == expected_paths
                assert observed["sources"][0]["usability"] == "usable"
                assert result["sources"][0]["semantic_scope"] == "not_evaluated"
                assert (target / expected_paths[0]).read_bytes() == (root / "originals/paper.html").read_bytes()
            else:
                assert argv[4:6] == ["agent", "inspect"] and "--source-path" not in argv
                assert observed["sources"] == []
        else:
            assert argv[argv.index("--from-file") + 1] == str(Path(result["checkpoint"]).with_name("plan.json"))
            assert observed["schema_version"] == "evidence-setup-result/v1"
            assert observed["status"] == "ready" and observed["setup_ready"]
            assert observed["plan_id"] == result["plan_id"]
            assert observed["transaction_id"] == result["transaction_id"]
        assert before == {str(path.relative_to(target)): path.read_bytes() for path in target.rglob("*") if path.is_file()}


def test_setup_replay_command_preserves_changed_workspace(tmp_path, run_package_command):
    """An emitted replay command retains caller edits and propagates the owning refusal."""
    root = (tmp_path / "changed setup").resolve()
    root.mkdir()
    with Onboarding.open(allowed_roots=[root], allow=["apply"]) as host:
        result = host.apply(host.plan(request(root)))
    assert result["setup_ready"]
    target = Path(result["target"])
    question = target / "wiki/questions/q1.md"
    question.write_text(question.read_text(encoding="utf-8") + "\nCaller annotation.\n", encoding="utf-8")
    (target / "caller-note.txt").write_text("Keep this observation.\n", encoding="utf-8")
    before = {str(path.relative_to(target)): path.read_bytes() for path in target.rglob("*") if path.is_file()}
    saved_plan = Path(result["checkpoint"]).with_name("plan.json")
    plan_bytes = saved_plan.read_bytes()
    action = next(row for row in result["next_actions"] if row["action"] == "resume_setup")
    process = run_package_command(action["argv"], timeout=120)
    assert process.returncode == 3 and process.stderr == "", process.stdout + process.stderr
    refusal = json.loads(process.stdout)
    assert refusal["error_code"] == "ONBOARDING_OWNERSHIP_CONFLICT"
    assert refusal["details"]["field"] == "setup_owned_files_changed_or_interrupted_write"
    assert before == {str(path.relative_to(target)): path.read_bytes() for path in target.rglob("*") if path.is_file()}
    assert saved_plan.read_bytes() == plan_bytes


def test_catalog_cannot_delegate_a_root_outside_the_host_scope(tmp_path, monkeypatch):
    from evidence_wiki import pack_catalog
    from evidence_wiki._onboarding_scope import Scope

    selected, outside = tmp_path / "selected", tmp_path / "outside"
    selected.mkdir()
    outside.mkdir()
    pack_catalog.initialize(selected / "catalog", {"external": str(outside)})
    with Onboarding.open(allowed_roots=[selected]) as host, pytest.raises(EvidenceWikiError):
        host.pack_list(catalog=selected / "catalog")
    # A changed catalog must also encounter the owner-time scope guard, even if
    # a prior validation observed a different root mapping.
    monkeypatch.setattr(Scope, "catalog", lambda self, value: value)
    with Onboarding.open(allowed_roots=[selected]) as host:
        from evidence_wiki._onboarding_scope import ACTIVE_SCOPE

        token = ACTIVE_SCOPE.set(host._scope)
        try:
            with pytest.raises(EvidenceWikiError):
                pack_catalog._root(pack_catalog._read(selected / "catalog"), "external")
        finally:
            ACTIVE_SCOPE.reset(token)


def test_handle_refuses_changed_packaged_runtime_even_with_unchanged_catalog(tmp_path, monkeypatch):
    scripts = tmp_path / "workspace-template/scripts"
    scripts.mkdir(parents=True)
    helper = scripts / "helper.py"
    helper.write_text("identity = 1\n")
    catalog = tmp_path / "workspace-template/docs/agent-resources/catalog.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_text("{}")
    monkeypatch.setattr("evidence_wiki.runtime_identity.shared_assets_root", lambda: tmp_path)
    monkeypatch.setattr("evidence_wiki.onboarding._PROCESS_GENERATION", None)
    with Onboarding.open() as host:
        helper.write_text("identity = 2\n")
        with pytest.raises(EvidenceWikiError) as error:
            host.recipes()
    assert error.value.error_code == "ONBOARDING_PLAN_STALE"


def test_a_new_handle_cannot_reload_changed_code_in_the_same_process(monkeypatch):
    with Onboarding.open():
        pass
    monkeypatch.setattr("evidence_wiki.onboarding.generation", lambda: {"different": "installed generation"})
    with pytest.raises(EvidenceWikiError) as error:
        Onboarding.open()
    assert error.value.details["field"] == "onboarding_generation_changed_restart_required"


@pytest.mark.parametrize("value", ["", "invalid\0path", 12, None])
def test_invalid_path_is_a_typed_refusal_before_filesystem_access(value):
    with Onboarding.open() as host, pytest.raises(EvidenceWikiError) as error:
        host.inspect(value)
    assert error.value.error_code == "ONBOARDING_INVALID"
