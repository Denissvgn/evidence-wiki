"""Native instruction locations retain user content and recover interrupted writes."""

import json
from pathlib import Path

import pytest

from evidence_wiki import native_instructions as native
from evidence_wiki._pack_io import canonical
from evidence_wiki.agent_resources import resource_document
from evidence_wiki.errors import EvidenceWikiError


def request(root, framework="pi", scope="project"):
    matrix = json.loads(resource_document("framework/compatibility/v1")["content"])
    version = next(row["version"] for row in matrix["frameworks"] if row["id"] == framework)
    return {"schema_version": native.REQUEST, "framework": framework, "version": version, "scope": scope, "root": str(root)}


@pytest.mark.parametrize("framework", ["pi", "opencode", "gemini"])
@pytest.mark.parametrize("scope", ["project", "user"])
def test_install_remove_preserves_canonical_bytes_and_other_instructions(tmp_path, framework, scope):
    original = tmp_path / "AGENTS.md"
    original.write_text("Keep my instructions.\n")
    prepared = native.plan(canonical(request(tmp_path, framework, scope)))
    assert list(tmp_path.iterdir()) == [original]
    result = native.apply(canonical(prepared))
    installed = Path(result["path"])
    assert (installed / "SKILL.md").read_bytes() == native._files()[1]["SKILL.md"]
    assert native.apply(canonical(prepared))["status"] == "already_installed"
    removed = native.apply(canonical(prepared), remove=True)
    assert not installed.exists()
    assert (Path(removed["archive"]) / "SKILL.md").exists()
    assert native.apply(canonical(prepared), remove=True)["status"] == "already_removed"
    assert original.read_text() == "Keep my instructions.\n"


def test_existing_or_edited_instructions_are_not_overwritten(tmp_path):
    prepared = native.plan(canonical(request(tmp_path)))
    installed = tmp_path / prepared["relative_path"]
    installed.mkdir(parents=True)
    (installed / "SKILL.md").write_text("Owner content")
    with pytest.raises(EvidenceWikiError):
        native.apply(canonical(prepared))
    assert (installed / "SKILL.md").read_text() == "Owner content"


def test_interrupted_install_reconciles_exact_owned_files(tmp_path, monkeypatch):
    prepared = native.plan(canonical(request(tmp_path)))
    publish = native.publish
    def interrupt(directory, name, raw):
        if name == "SKILL.md":
            raise KeyboardInterrupt
        return publish(directory, name, raw)
    with monkeypatch.context() as changed:
        changed.setattr(native, "publish", interrupt)
        with pytest.raises(KeyboardInterrupt):
            native.apply(canonical(prepared))
    assert native.apply(canonical(prepared))["status"] == "installed"
    installed = tmp_path / prepared["relative_path"]
    (installed / "SKILL.md").write_text("User revision")
    with pytest.raises(EvidenceWikiError) as error:
        native.apply(canonical(prepared), remove=True)
    assert error.value.details["field"] == "native_instructions_user_changes_preserved"
    assert (installed / "SKILL.md").read_text() == "User revision"


def test_native_parent_link_and_unknown_version_refused(tmp_path):
    request_value = request(tmp_path)
    with pytest.raises(EvidenceWikiError):
        native.plan(canonical({**request_value, "version": "future"}))
    prepared = native.plan(canonical(request_value))
    (tmp_path / ".pi").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises((EvidenceWikiError, OSError)):
        native.apply(canonical(prepared))
    assert not (tmp_path / "skills").exists()


def test_interrupted_archive_reservation_is_recoverable(tmp_path, monkeypatch):
    prepared = native.plan(canonical(request(tmp_path)))
    native.apply(canonical(prepared))
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt
    with monkeypatch.context() as changed:
        changed.setattr(native.os, "rename", interrupt)
        with pytest.raises(KeyboardInterrupt):
            native.apply(canonical(prepared), remove=True)
    assert native.apply(canonical(prepared), remove=True)["status"] == "removed"
