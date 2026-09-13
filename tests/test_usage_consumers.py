"""Exercise authenticated source use through public operations and persistence failures."""

from __future__ import annotations

import argparse
import base64
import contextlib
import io
import json
from pathlib import Path
from unittest import SkipTest

import pytest
import yaml

from evidence_wiki import Workspace, cli
from evidence_wiki.errors import ConfigError, SourceError
from tests import _usage_fixture
from tests._execution_fixture import canonical
from tests._script_loader import load_isolated_module
from tests._usage_fixture import UsageFixture

SCRIPTS = Path(__file__).resolve().parents[1] / "workspace-template" / "scripts"


@pytest.fixture
def host(tmp_path, monkeypatch):
    fixture = UsageFixture(tmp_path, monkeypatch)
    (fixture.root / "research.yml").write_text(yaml.safe_dump(fixture.config))
    module = load_isolated_module("usage_consumer_contract", SCRIPTS / "_evidence_usage.py")
    return fixture, module


def deposited(host, **kwargs):
    fixture, module = host
    fixture.transact(module, "initialize")
    body, files = fixture.source(**kwargs)
    fixture.transact(module, "deposit", body, files)
    return body, files


def test_public_operations_and_cli_transport_agree(host, monkeypatch, capsys):
    fixture, _module = host
    workspace = Workspace.open(fixture.root)
    before = {path.name for path in fixture.host.iterdir()}
    assert workspace.usage.status()["initialized"] is False
    assert {path.name for path in fixture.host.iterdir()} == before
    command = fixture.command("initialize")
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(canonical({"command": command, "artifacts": {}}))))
    assert cli.main(["usage", "transact", "--target", str(fixture.root)]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert workspace.usage.transact(command) == receipt
    fixture.checkpoint = receipt["checkpoint"]
    assert workspace.usage.status(request_id=command["payload"]["request_id"])["receipt"] == receipt
    body, files = fixture.source()
    command = fixture.command("deposit", body)
    document = {"command": command, "artifacts": {path: base64.b64encode(data).decode() for path, data in files.items()}}
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(canonical(document))))
    assert cli.main(["usage", "transact", "--target", str(fixture.root)]) == 0
    assert json.loads(capsys.readouterr().out) == workspace.usage.transact(command, artifacts=files)
    assert workspace.usage.check(body["source_revision"], uses=["training", "export"],
                                 purpose="training-snapshot", consumer="evidence-wiki")["eligible"]
    assert workspace.usage.lineage(body["source_revision"])["complete"]
    workspace.close()
    with pytest.raises(ConfigError):
        workspace.usage.status()


def test_materialization_requires_exact_replacement_and_revocation_is_current(host):
    fixture, module = host
    body, files = deposited(host)
    workspace = Workspace.open(fixture.root)
    first = workspace.usage.materialize(body["source_revision"])
    target = fixture.root / first["path"]
    assert target.read_bytes() == files["normalized.md"]
    assert workspace.usage.materialize(body["source_revision"])["changed"] is False
    corrected, originals = fixture.source(normalized=files["normalized.md"] + b"A corrected observation.\n")
    fixture.transact(module, "deposit", corrected, originals)
    with pytest.raises(SourceError, match="not authorized") as caught:
        workspace.usage.materialize(corrected["source_revision"])
    assert caught.value.details["reason"] == "normalized_destination_conflict"
    assert target.read_bytes() == files["normalized.md"]
    workspace.usage.materialize(corrected["source_revision"], expected_content_hash=first["content_hash"])
    assert target.read_bytes() == originals["normalized.md"]
    fixture.revoke(module, corrected)
    with pytest.raises(SourceError):
        workspace.usage.materialize(corrected["source_revision"])
    assert not list(target.parent.glob(".normalized-*"))


@pytest.mark.parametrize("unsafe", ["denied", "symlink", "hardlink", "ancestor-link", "changed-bytes"])
def test_no_protected_materialization_to_unsafe_or_unapproved_destination(host, tmp_path, unsafe):
    fixture, _module = host
    body, files = deposited(host, retrieval=unsafe != "denied")
    outside = tmp_path / "outside.md"
    outside.write_bytes(b"unrelated content")
    target = fixture.root / "sources/normalized/lab--sample.md"
    if unsafe == "ancestor-link":
        (fixture.root / "sources").symlink_to(tmp_path, target_is_directory=True)
    elif unsafe != "denied":
        target.parent.mkdir(parents=True)
        if unsafe == "symlink":
            target.symlink_to(outside)
        elif unsafe == "hardlink":
            target.hardlink_to(outside)
        else:
            target.write_bytes(files["normalized.md"] + b"unapproved replacement")
    with pytest.raises(SourceError):
        Workspace.open(fixture.root).usage.materialize(body["source_revision"])
    assert outside.read_bytes() == b"unrelated content"
    assert not list(fixture.root.rglob(".normalized-*"))
    if unsafe == "denied":
        assert not (fixture.root / "sources").exists()


@pytest.mark.parametrize("failure", ["temp-sync", "rename", "directory-sync"])
def test_transaction_failure_keeps_a_complete_generation_and_receipt_reconciles(host, monkeypatch, failure):
    fixture, module = host
    body, _files = deposited(host)
    old = (fixture.host / "evidence-state.json").read_bytes()
    command = fixture.command("revoke", {"source_id": body["source_id"], "source_revision": body["source_revision"],
                                          "scope": "revision", "reason": "owner-withdrawal"})
    store = module.write_state.__globals__
    real_sync, real_rename = store["os"].fsync, store["os"].rename
    calls = 0

    def sync(descriptor):
        nonlocal calls
        calls += 1
        if calls == (2 if failure == "directory-sync" else 1) and failure != "rename":
            raise OSError("injected I/O interruption")
        return real_sync(descriptor)

    def rename(*args, **kwargs):
        if failure == "rename":
            raise OSError("injected I/O interruption")
        return real_rename(*args, **kwargs)

    with monkeypatch.context() as faults:
        faults.setattr(store["os"], "fsync", sync)
        faults.setattr(store["os"], "rename", rename)
        # Replacing the Python function must not simulate an unsupported platform.
        faults.setattr(store["os"], "supports_dir_fd", store["os"].supports_dir_fd | {rename})
        with pytest.raises(ValueError, match="host_state_unreadable"):
            module.transact(fixture.root, fixture.config, command)
    workspace = Workspace.open(fixture.root)
    status = workspace.usage.status(request_id=command["payload"]["request_id"])
    if failure == "directory-sync":
        assert status["receipt"] is not None
        assert not workspace.usage.check(body["source_revision"], uses=["retrieval"], purpose="research",
                                         consumer="evidence-wiki")["eligible"]
    else:
        assert status["receipt"] is None
        assert (fixture.host / "evidence-state.json").read_bytes() == old
    receipt = workspace.usage.transact(command)
    assert workspace.usage.status(request_id=command["payload"]["request_id"])["receipt"] == receipt
    assert workspace.usage.status()["event_count"] == 3
    assert not list(fixture.host.glob(".evidence-state-*"))


def test_interrupted_initialization_status_does_not_write_or_guess_success(host, monkeypatch):
    fixture, module = host
    command = fixture.command("initialize")
    with monkeypatch.context() as faults:
        faults.setattr(module.write_state.__globals__["os"], "fsync", lambda _fd: (_ for _ in ()).throw(OSError("interrupted")))
        with pytest.raises(ValueError):
            module.transact(fixture.root, fixture.config, command)
    workspace = Workspace.open(fixture.root)
    assert workspace.usage.status()["initialized"] is False
    assert not (fixture.host / "evidence-state.json").exists()
    workspace.usage.transact(command)
    assert workspace.usage.status()["initialized"] is True


def test_protected_queries_never_reuse_cache_or_read_unapproved_wiki(host, capsys, monkeypatch):
    fixture, module = host
    body, _files = deposited(host)
    Workspace.open(fixture.root).usage.materialize(body["source_revision"])
    wiki = fixture.root / "wiki"
    wiki.mkdir()
    (wiki / "unapproved.md").write_text("Laboratory PROTECTED_UNSCRUBBED_SENTINEL")
    query = load_isolated_module("usage_query_public", SCRIPTS / "query_index.py")

    def forbidden(*_args, **_kwargs):
        pytest.fail("Protected retrieval entered a legacy cache, provider, or enrichment path")

    for seam in ("query_with_optional_fts", "query_retrieval_provider", "citation_relation_graph", "evidence_path_graph"):
        monkeypatch.setattr(query, seam, forbidden)
    args = ["laboratory", "--project-root", str(fixture.root), "--format", "json"]
    assert query.main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["result_count"] == 1
    assert "PROTECTED_UNSCRUBBED_SENTINEL" not in json.dumps(result)
    fixture.revoke(module, body)
    assert query.main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["results"] == []
    assert result["usage"]["checkpoint"] == fixture.checkpoint
    assert result["usage"]["excluded_source_count"] == 1
    assert query.main(["build-index", "--project-root", str(fixture.root)]) == 2
    assert not (fixture.root / ".research-cache").exists()


@pytest.mark.parametrize("restriction", [
    {"retrieval_eligible": False},
    {"metadata": {"retrieval_eligible": False}},
    {"provenance": {"retrieval_eligible": False}},
    {"retrieval_eligible": "true"},
])
def test_protected_queries_enforce_configured_manifest_restrictions(host, capsys, restriction):
    fixture, _module = host
    body, _files = deposited(host)
    Workspace.open(fixture.root).usage.materialize(body["source_revision"])
    fixture.config["sources"] = {"manifest_path": "sources/custom-manifest.jsonl"}
    (fixture.root / "research.yml").write_text(yaml.safe_dump(fixture.config))
    manifest = fixture.root / "sources/custom-manifest.jsonl"
    manifest.write_bytes(canonical({"id": body["source_id"], **restriction}))
    query = load_isolated_module("manifest_usage_query", SCRIPTS / "query_index.py")
    assert query.main(["laboratory", "--project-root", str(fixture.root), "--format", "json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["results"] == []
    assert result["usage"]["excluded_source_count"] == 1


def test_protected_query_uses_manifest_revision_to_disambiguate_identical_normalized_bytes(host, capsys):
    fixture, module = host
    first, files = deposited(host)
    selected, originals = fixture.source(parents=[first["source_revision"]], normalized=files["normalized.md"])
    fixture.transact(module, "deposit", selected, originals)
    Workspace.open(fixture.root).usage.materialize(selected["source_revision"])
    manifest = fixture.root / "sources/manifest.jsonl"
    manifest.write_bytes(canonical({"id": selected["source_id"], "usage_revision_id": selected["source_revision"]}))
    query = load_isolated_module("manifest_revision_query", SCRIPTS / "query_index.py")
    args = ["laboratory", "--project-root", str(fixture.root), "--format", "json"]
    assert query.main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["result_count"] == 1
    assert result["usage"]["excluded_source_count"] == 0
    manifest.write_bytes(canonical({"id": selected["source_id"], "usage_revision_id": "sha256:" + "0" * 64}))
    assert query.main(args) == 0
    assert json.loads(capsys.readouterr().out)["results"] == []


@pytest.mark.parametrize("content", [b"{invalid\n", b"[]\n", b'{"id":"lab:sample"}\n{"id":"lab:sample"}\n'])
def test_protected_query_refuses_unreadable_or_ambiguous_manifest(host, capsys, content):
    fixture, _module = host
    body, _files = deposited(host)
    Workspace.open(fixture.root).usage.materialize(body["source_revision"])
    (fixture.root / "sources/manifest.jsonl").write_bytes(content)
    query = load_isolated_module("invalid_manifest_query", SCRIPTS / "query_index.py")
    assert query.main(["laboratory", "--project-root", str(fixture.root), "--format", "json"]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err)["error_code"] == "EVIDENCE_USAGE_REFUSED"


@pytest.mark.parametrize("existing", [False, True])
def test_protected_query_rechecks_manifest_at_the_read_boundary(host, monkeypatch, capsys, existing):
    fixture, _module = host
    body, _files = deposited(host)
    Workspace.open(fixture.root).usage.materialize(body["source_revision"])
    manifest = fixture.root / "sources/manifest.jsonl"
    if existing:
        manifest.write_bytes(canonical({"id": body["source_id"]}))
    query = load_isolated_module("changing_manifest_query", SCRIPTS / "query_index.py")
    rank = query.rank_documents

    def restrict_during_ranking(*args, **kwargs):
        manifest.write_bytes(canonical({"id": body["source_id"], "retrieval_eligible": False}))
        return rank(*args, **kwargs)

    monkeypatch.setattr(query, "rank_documents", restrict_during_ranking)
    assert query.main(["laboratory", "--project-root", str(fixture.root), "--format", "json"]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err)["details"]["reason"] == "usage_workspace_changed"


@pytest.mark.parametrize("stem,args", [
    ("normalize_sources", ["--all", "--format", "json"]),
    ("source_inventory", ["--format", "json"]),
    ("fetch_sources", ["--format", "json", "openalex", "get", "--id", "W123"]),
    ("source_requests", ["fulfill", "--request-id", "sample", "--source-id", "lab:sample", "--format", "json"]),
])
def test_legacy_intake_refuses_before_any_package_write(host, stem, args, capsys):
    fixture, _module = host
    script = load_isolated_module("usage_intake_" + stem, SCRIPTS / (stem + ".py"))
    before = sorted(path.relative_to(fixture.root).as_posix() for path in fixture.root.rglob("*"))
    assert script.main(["--project-root", str(fixture.root), *args]) == 2
    assert sorted(path.relative_to(fixture.root).as_posix() for path in fixture.root.rglob("*")) == before
    assert "protected_capture_requires_host_sanitization" in capsys.readouterr().err


@pytest.mark.parametrize("title", ["Ordinary title", "Ordinary --- title", '"--- quoted title"'])
def test_explicit_legacy_restriction_cannot_enter_export_or_cache(tmp_path, monkeypatch, capsys, title):
    monkeypatch.delenv("EVIDENCE_WIKI_STATE_DIR", raising=False)
    root = tmp_path / "legacy"
    root.mkdir()
    (root / "research.yml").write_text("project: {}\n")
    workspace = Workspace.open(root)
    assert workspace.export_answers()["questions"] == []
    target = root / "sources/normalized/restricted.md"
    target.parent.mkdir(parents=True)
    target.write_text(f"---\nsource_id: restricted\ntitle: {title}\nexport_eligible: false\n---\nRestricted content.\n")
    with pytest.raises(SourceError):
        workspace.export_answers()
    query = load_isolated_module("legacy_usage_cache", SCRIPTS / "query_index.py")
    with pytest.raises(SystemExit):
        query.write_fts_index(root, {}, "all", root / "index.sqlite")
    assert not (root / "index.sqlite").exists()
    assert query.main(["restricted", "--project-root", str(root), "--format", "json"]) == 2
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("operation", ["publication", "start", "next", "submit"])
def test_protected_publication_and_orchestration_refuse_before_writes(host, operation, monkeypatch):
    fixture, _module = host
    before = {path.relative_to(fixture.root): path.read_bytes() for path in fixture.root.rglob("*") if path.is_file()}
    if operation == "publication":
        script = load_isolated_module("protected_publication", SCRIPTS / "publication_readiness.py")
        invoke = lambda: script.build_bundle(fixture.root, "missing-run")
    else:
        script = load_isolated_module("protected_orchestration", SCRIPTS / "orchestration_controller.py")
        args = argparse.Namespace()
        guards = []
        if operation == "submit":
            # Isolate intake after successful read-only runtime guards. Their
            # real locking and refusal paths are covered by controller checks.
            args = argparse.Namespace(orchestration_id="existing-session", action_id="pending-action",
                                      result_file=str(fixture.root / "result.json"), agent_id=None)
            monkeypatch.setattr(script, "load_result", lambda *_: {})
            monkeypatch.setattr(script, "driver_session_lock", lambda *_, **__: contextlib.nullcontext())
            monkeypatch.setattr(script, "load_session", lambda *_: {})
            monkeypatch.setattr(script, "enforce_control_repair_gate", lambda *_: guards.append("repair"))
            monkeypatch.setattr(script, "retained_result", lambda *_: None)
            monkeypatch.setattr(script, "load_json_object", lambda *_, **__: {})
            monkeypatch.setattr(script, "require_action_baselines", lambda *_: guards.append("baseline"))
            monkeypatch.setattr(script, "verify_runtime_guards", lambda *_: guards.append("runtime"))
        invoke = lambda: getattr(script, {"start": "start_session", "next": "next_work", "submit": "submit_result"}[operation])(
            fixture.root, args)
    with pytest.raises(SystemExit) as caught:
        invoke()
    assert caught.value.error_code == "EVIDENCE_USAGE_REFUSED"
    if operation == "submit":
        assert guards == ["repair", "baseline", "runtime"]
    assert {path.relative_to(fixture.root): path.read_bytes() for path in fixture.root.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("mutation", ["leaf", "ancestor"])
def test_materialization_detects_destination_replacement_after_rename(host, monkeypatch, mutation):
    fixture, _module = host
    body, _files = deposited(host)
    workspace = Workspace.open(fixture.root)
    script = workspace._script("evidence_usage")
    materialization = script.run_materialize.__globals__["materialize"].__globals__
    system = materialization["os"]
    original_rename = system.rename
    target = fixture.root / "sources/normalized/lab--sample.md"

    def rename(*args, **kwargs):
        result = original_rename(*args, **kwargs)
        if str(args[0]).startswith(".normalized-"):
            if mutation == "leaf":
                target.unlink()
                target.write_bytes(b"external replacement")
            else:
                original_rename(target.parent, target.parent.with_name("moved"))
                target.parent.mkdir()
        return result

    monkeypatch.setattr(system, "rename", rename)
    monkeypatch.setattr(system, "supports_dir_fd", system.supports_dir_fd | {rename})
    with pytest.raises(SourceError) as caught:
        workspace.usage.materialize(body["source_revision"])
    assert caught.value.details["reason"] == "normalized_destination_changed"


def test_unsupported_host_storage_fails_without_initializing(tmp_path, monkeypatch):
    root, host = tmp_path / "workspace", tmp_path / "host"
    root.mkdir()
    host.mkdir(mode=0o700)
    monkeypatch.setenv("EVIDENCE_WIKI_STATE_DIR", str(host.resolve()))
    store = load_isolated_module("unsupported_host_storage", SCRIPTS / "_host_evidence_store.py")
    monkeypatch.setattr(store, "fcntl", None)
    with pytest.raises(ValueError, match="host_state_unsupported"):
        with store.locked_state(root, write=True, initialize=True):
            pytest.fail("unsupported storage yielded a writable state")
    assert not list(host.iterdir())


def test_host_fixture_skips_before_setup_without_posix_locking(tmp_path, monkeypatch):
    original = _usage_fixture.importlib.util.find_spec
    monkeypatch.setattr(_usage_fixture.importlib.util, "find_spec", lambda name: None if name == "fcntl" else original(name))
    with pytest.raises(SkipTest, match="host evidence storage requires POSIX"):
        UsageFixture(tmp_path, monkeypatch)
    assert not list(tmp_path.iterdir())
