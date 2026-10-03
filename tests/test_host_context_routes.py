"""Public routes cannot bypass an explicit host selection."""

import pytest

from evidence_wiki import Workspace
from evidence_wiki._facades._base import call_seam
from evidence_wiki._facades.snapshots import verify_snapshot
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.host_context import HostContext


def test_deployed_orchestration_refuses_before_external_execution(tmp_path, monkeypatch):
    (tmp_path / "research.yml").write_text("{}\n")
    executed = []
    monkeypatch.setattr("evidence_wiki.orchestration.protocol_status", lambda *a, **k: executed.append(True))
    selected = HostContext([tmp_path], ["orchestrate.status"], "caller")
    with Workspace.open(tmp_path) as workspace, selected.activate(), pytest.raises(EvidenceWikiError) as error:
        workspace.orchestrate.session("session").status()
    assert error.value.details["reason"] == "host_operation_unsupported"
    assert executed == []


def test_rootless_snapshot_validation_requires_its_actual_grant(tmp_path):
    selected = HostContext([tmp_path], [], "caller")
    with selected.activate(), pytest.raises(EvidenceWikiError) as error:
        verify_snapshot(b"{}", trust_policy_bytes=b"{}")
    assert error.value.details["reason"] == "host_operation_not_granted"


@pytest.mark.parametrize("stem,seam,roots,options", [
    ("coverage_manifest", "run_evaluate", "single", {"slug": "question"}),
    ("doctor", "run_doctor", "single", {}),
    ("evidence_snapshots", "run_export", "single", {"selection": {}, "registration_request_id": "request"}),
    ("workspace_status", "run_status_report", "single", {}),
    ("fleet_status", "run_fleet_status", "many", {}),
])
def test_workspace_write_routes_refuse_before_owner_loading(tmp_path, stem, seam, roots, options):
    loaded = []
    selected = HostContext([tmp_path], [f"{stem}.{seam}"], "caller")
    argument = [tmp_path] if roots == "many" else tmp_path
    with selected.activate(), pytest.raises(EvidenceWikiError) as error:
        call_seam(lambda name: loaded.append(name), stem, seam, argument, **options)
    assert error.value.details["reason"] == "host_path_outside_scope"
    assert loaded == []


@pytest.mark.parametrize("stem,seam,many", [
    ("workspace_status", "run_status_report", False),
    ("fleet_status", "run_fleet_status", True),
])
def test_status_no_cache_is_a_read_route(tmp_path, stem, seam, many):
    from types import SimpleNamespace

    calls = []

    def operation(*args, **kwargs):
        calls.append((args, kwargs))
        return {"read": True}

    selected = HostContext([tmp_path], [f"{stem}.{seam}"], "caller")
    argument = [tmp_path] if many else tmp_path
    with selected.activate():
        assert call_seam(lambda name: SimpleNamespace(**{seam: operation}),
                         stem, seam, argument, no_cache=True) == {"read": True}
    assert len(calls) == 1


@pytest.mark.parametrize("value", [0, 1, None, "true"])
def test_status_cache_effect_must_be_an_exact_boolean(tmp_path, value):
    loaded = []
    selected = HostContext([tmp_path], ["workspace_status.run_status_report"], "caller", [tmp_path])
    with selected.activate(), pytest.raises(EvidenceWikiError) as error:
        call_seam(lambda name: loaded.append(name), "workspace_status", "run_status_report", tmp_path, no_cache=value)
    assert error.value.details["reason"] == "host_operation_effect_invalid"
    assert loaded == []


def test_direct_workspace_reads_cannot_escape_scope_or_grants(tmp_path):
    allowed, outside = tmp_path / "allowed", tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    (outside / "research.yml").write_text("{}\n")
    (outside / "workspace-system.yml").write_text("workspace_system:\n  starter_version: outside-version\n")
    workspace = Workspace.open(outside)
    selected = HostContext([allowed], ["workspace.open", "workspace.versions"], "caller")
    with selected.activate():
        with pytest.raises(EvidenceWikiError) as error:
            Workspace.open(outside)
        assert error.value.details["reason"] == "host_path_outside_scope"
        with pytest.raises(EvidenceWikiError) as error:
            workspace.versions()
        assert error.value.details["reason"] == "host_path_outside_scope"
    assert workspace.versions()["workspace"]["starter_version"] == "outside-version"
    selected = HostContext([outside], [], "caller")
    with selected.activate():
        with pytest.raises(EvidenceWikiError) as error:
            Workspace.open(outside)
        assert error.value.details["reason"] == "host_operation_not_granted"
        with pytest.raises(EvidenceWikiError) as error:
            workspace.versions()
        assert error.value.details["reason"] == "host_operation_not_granted"


@pytest.mark.parametrize("replacement", ["symlink", "directory"])
def test_descendant_workspace_replacement_during_load_denies_before_effect(tmp_path, replacement):
    from types import SimpleNamespace

    allowed, outside = tmp_path / "allowed", tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    root = allowed / "workspace"
    root.mkdir()
    selected = HostContext([allowed], ["coverage_manifest.run_evaluate"], "caller", [allowed])
    invoked = []

    def operation(path, **options):
        invoked.append(True)
        (path / "effect").write_text("unexpected")
        return {}

    def loader(stem):
        root.rename(allowed / "retained")
        if replacement == "symlink":
            root.symlink_to(outside, target_is_directory=True)
        else:
            root.mkdir()
        return SimpleNamespace(run_evaluate=operation)

    with selected.activate(), pytest.raises(EvidenceWikiError):
        call_seam(loader, "coverage_manifest", "run_evaluate", root, slug="question")
    assert invoked == []
    assert not (outside / "effect").exists()
    assert not (root / "effect").exists()


@pytest.mark.parametrize("filename,operation", [("research.yml", "open"), ("workspace-system.yml", "versions")])
def test_direct_workspace_file_reads_refuse_external_symlinks(tmp_path, filename, operation):
    root, outside = tmp_path / "workspace", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "research.yml").write_text("{}\n")
    workspace = Workspace.open(root)
    external_file = outside / filename
    external_file.write_text("workspace_system:\n  starter_version: outside-version\n")
    (root / filename).unlink(missing_ok=True)
    (root / filename).symlink_to(external_file)
    selected = HostContext([root], ["workspace.open", "workspace.versions"], "caller")
    with selected.activate(), pytest.raises(EvidenceWikiError) as error:
        Workspace.open(root) if operation == "open" else workspace.versions()
    assert error.value.details["reason"] == "host_path_link"
