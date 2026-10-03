"""Workspace configuration and actual descendant paths stay within owner scope."""

import pytest

from evidence_wiki import Workspace
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.host_context import HostContext


@pytest.mark.parametrize("operation", ["request", "coverage"])
def test_linked_source_directory_refuses_before_public_owner_effect(tmp_path, operation):
    root, outside = tmp_path / "workspace", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "research.yml").write_text("{}\n")
    (root / "sources").symlink_to(outside, target_is_directory=True)
    grant = "source_requests.run_add_request" if operation == "request" else "coverage_manifest.run_init_inline"
    selected = HostContext([root], [grant], "caller", [root])
    with Workspace.open(root) as workspace, selected.activate(), pytest.raises(EvidenceWikiError):
        if operation == "request":
            workspace.source_requests.add(kind="web", query_or_identifier="bounded query", rationale="Retain a gap.")
        else:
            workspace.coverage.init("question")
    assert list(outside.iterdir()) == []


def test_retained_handle_cannot_read_replaced_linked_configuration(tmp_path, monkeypatch):
    root, outside = tmp_path / "workspace", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    marker = root / "research.yml"
    marker.write_text("{}\n")
    external = outside / "research.yml"
    external.write_text("{}\n")
    workspace = Workspace.open(root)
    marker.unlink()
    marker.symlink_to(external)
    loaded = []
    monkeypatch.setattr("evidence_wiki._script_host.load_packaged_script", lambda *args: loaded.append(args))
    selected = HostContext([root], ["source_requests.run_list_requests"], "caller")
    with selected.activate(), pytest.raises(EvidenceWikiError):
        workspace.source_requests.list()
    assert loaded == []
    assert list(outside.iterdir()) == [external]


def test_owner_preflight_checks_custom_descendant_paths_and_leaf_kind(tmp_path):
    from evidence_wiki._script_host import load_packaged_script, shared_assets_root

    helper = load_packaged_script(shared_assets_root(), "_owner_paths")
    root, outside = tmp_path / "workspace", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "custom").symlink_to(outside, target_is_directory=True)
    with pytest.raises(Exception) as error:
        helper.checked_path(root, "custom/requests.jsonl", field="requests", kind="file")
    assert error.value.error_code == "WORKSPACE_PATH_REFUSED"
    assert error.value.details["reason"] == "workspace_path_link"
    assert helper.checked_path(root, "new/request.jsonl", kind="file") == root / "new/request.jsonl"
    with pytest.raises(Exception) as error:
        helper.checked_path(root, root, kind="file")
    assert error.value.error_code == "WORKSPACE_PATH_REFUSED"
    assert list(outside.iterdir()) == []


def test_owner_preflight_normalizes_only_the_selected_root_alias(tmp_path):
    from evidence_wiki._script_host import load_packaged_script, shared_assets_root

    helper = load_packaged_script(shared_assets_root(), "_owner_paths")
    root = tmp_path / "workspace"
    root.mkdir()
    alias = tmp_path / "selected-root"
    alias.symlink_to(root, target_is_directory=True)
    assert helper.checked_path(alias, alias, kind="directory") == root
    assert helper.checked_path(alias, alias / "new/store.jsonl", kind="file") == root / "new/store.jsonl"
    (root / "nested").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(Exception) as error:
        helper.checked_path(alias, alias / "nested/outside", kind="file")
    assert error.value.details["reason"] == "workspace_path_link"
