"""Controlled direct writers require their own live native workspace gate."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import replace
from types import SimpleNamespace

import pytest
import yaml

from evidence_wiki import Onboarding, Workspace
from evidence_wiki._facades._base import call_seam
from evidence_wiki._filesystem import os as native_os
from evidence_wiki.errors import ConfigError
from evidence_wiki.host_context import HostContext, checked_operation
from evidence_wiki.mutation_store import MutationStore, MutationStoreError

WRITERS = [
    ("coverage_manifest", "run_init_inline", {}),
    ("coverage_manifest", "run_link", {}),
    ("coverage_manifest", "run_evaluate", {}),
    ("coverage_manifest", "run_evaluate_checked", {}),
    ("coverage_manifest", "run_revision", {}),
    ("intake_questions", "run_intake", {"dry_run": False}),
    ("intake_questions", "run_intake_document", {"dry_run": False}),
]
GRANTS = {"workspace.open", "coverage_manifest.run_init_inline", "coverage_manifest.run_link",
    "coverage_manifest.run_evaluate", "coverage_manifest.run_revision", "intake_questions.run_intake"}


@pytest.fixture
def roots(tmp_path, monkeypatch):
    monkeypatch.delenv("EVIDENCE_WIKI_AUTHORITY_FILE", raising=False)
    monkeypatch.delenv("EVIDENCE_WIKI_STATE_DIR", raising=False)
    root, control = tmp_path / "workspace", tmp_path / "control"
    (root / "wiki/questions").mkdir(parents=True)
    (root / "sources").mkdir()
    native_os.mkdir(control, mode=0o700)
    (root / "research.yml").write_text(yaml.safe_dump({"wiki": {"root": "wiki"},
        "sources": {"manifest_path": "sources/manifest.jsonl", "coverage_dir": "sources/coverage"}}))
    (root / "sources/manifest.jsonl").write_text("")
    (root / "wiki/questions/sample.md").write_text("---\ntype: question\nstatus: open\nquestion: What is observed?\n---\n")
    return root.resolve(), control.resolve()


def host(roots, *, control=True, additional=()):
    root, selected = roots
    return HostContext([root, *additional], GRANTS, "caller", [root, *additional],
                       mutation_control_directory=selected if control else None)


def invoke(root, stem="coverage_manifest", seam="run_init_inline", **kwargs):
    effects = []

    def load(_):
        effects.append("loaded")
        return SimpleNamespace(**{seam: lambda *a, **k: effects.append("invoked") or {"observed": True}})

    return call_seam(load, stem, seam, root, **kwargs), effects


@pytest.mark.parametrize("stem,seam,kwargs", WRITERS)
def test_controlled_direct_writer_refuses_before_load_or_effect(roots, stem, seam, kwargs):
    root, control = roots
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    with host(roots).activate(), pytest.raises(ConfigError) as error:
        call_seam(lambda _: pytest.fail("owner loaded"), stem, seam, root, **kwargs)
    assert error.value.details == {"reason": "host_mutation_workspace_gate_required"}
    assert before == {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    assert not (control / "mutations").exists()


def test_request_boolean_cannot_replace_the_held_gate(roots):
    root, _control = roots
    with host(roots).activate(), pytest.raises(ConfigError):
        with checked_operation("coverage_manifest", "run_init_inline", (root,),
                               {"mutation_gate": True, "serialized": True}):
            pytest.fail("caller assertions bypassed the native gate")


@pytest.mark.parametrize("stem,seam,kwargs", WRITERS)
def test_legacy_no_control_routes_keep_their_direct_boundary(roots, stem, seam, kwargs):
    root, _control = roots
    assert invoke(root, stem, seam, **kwargs)[1] == ["loaded", "invoked"]
    with host(roots, control=False).activate():
        assert invoke(root, stem, seam, **kwargs)[1] == ["loaded", "invoked"]


@pytest.mark.parametrize("seam", ["run_intake", "run_intake_document"])
def test_controlled_intake_dry_run_remains_read_only(roots, seam):
    root, control = roots
    with host(roots).activate():
        assert invoke(root, "intake_questions", seam, dry_run=True)[1] == ["loaded", "invoked"]
    assert not (control / "mutations").exists()


@pytest.mark.parametrize("stem,seam,kwargs", WRITERS)
def test_each_writer_accepts_only_its_current_held_workspace_gate(roots, stem, seam, kwargs):
    root, _control = roots
    with host(roots).activate(), MutationStore(*roots).workspace_locked():
        assert invoke(root, stem, seam, **kwargs)[1] == ["loaded", "invoked"]


def test_real_coverage_owner_runs_inside_native_gate(roots):
    root, _control = roots
    template = {"coverage_profile": "declared", "required_facets": [{"facet_id": "primary",
        "description": "Primary observation", "evidence_path": "official_guidance", "source_policy": "official_primary",
        "freshness_policy": "no_staleness_check", "identity_policy": "official_domain_match", "min_sources": 1}],
        "optional_facets": []}
    with host(roots).activate(), MutationStore(*roots).workspace_locked(), Workspace.open(root) as workspace:
        result = workspace.coverage.init("sample", template=template)
    assert result["manifest"]["question_slug"] == "sample"
    assert (root / "sources/coverage/sample.yml").is_file()


def test_public_coverage_writer_refuses_before_its_handle_loader(roots, monkeypatch):
    root, control = roots
    with host(roots).activate(), Workspace.open(root) as workspace:
        monkeypatch.setattr(Workspace, "_script", lambda *a: pytest.fail("owner loaded"))
        with pytest.raises(ConfigError):
            workspace.coverage.init("sample")
    assert not (root / "sources/coverage").exists()
    assert not (control / "mutations").exists()


def test_expired_copied_gate_cannot_authorize_after_exit(roots):
    root, _control = roots
    with host(roots).activate(), MutationStore(*roots).workspace_locked():
        copied = copy_context()
    with pytest.raises(ConfigError) as error:
        copied.run(invoke, root)
    assert error.value.details == {"reason": "host_mutation_workspace_gate_required"}


def test_exception_resets_gate_and_same_store_can_acquire_again(roots):
    root, _control = roots
    store = MutationStore(*roots)
    with host(roots).activate():
        with pytest.raises(RuntimeError), store.workspace_locked():
            raise RuntimeError("synthetic interruption")
        with pytest.raises(ConfigError):
            invoke(root)
        with store.workspace_locked():
            assert invoke(root)[1] == ["loaded", "invoked"]


def test_different_context_even_same_selections_cannot_borrow_gate(roots):
    root, _control = roots
    selected = host(roots)
    with selected.activate(), MutationStore(*roots).workspace_locked():
        with replace(selected).activate(), pytest.raises(ConfigError):
            invoke(root)
        assert invoke(root)[1] == ["loaded", "invoked"]


def test_different_workspace_and_nested_gate_do_not_borrow_scope(roots, tmp_path):
    root, control = roots
    other = tmp_path / "other"
    other.mkdir()
    selected = host(roots, additional=[other])
    with selected.activate(), MutationStore(*roots).workspace_locked():
        with pytest.raises(ConfigError):
            invoke(other)
        with pytest.raises(ConfigError) as error, MutationStore(other, control).workspace_locked():
            pass
        assert error.value.details == {"reason": "host_mutation_gate_nested"}
        assert invoke(root)[1] == ["loaded", "invoked"]


def test_copied_thread_context_cannot_borrow_native_gate(roots):
    root, _control = roots
    with host(roots).activate(), MutationStore(*roots).workspace_locked(), ThreadPoolExecutor(max_workers=1) as pool:
        copied = copy_context()
        with pytest.raises(ConfigError):
            pool.submit(copied.run, invoke, root).result(timeout=5)
        assert invoke(root)[1] == ["loaded", "invoked"]


def test_child_async_task_cannot_borrow_parent_native_gate(roots):
    root, _control = roots

    async def run():
        with host(roots).activate(), MutationStore(*roots).workspace_locked():
            async def child():
                with pytest.raises(ConfigError):
                    invoke(root)
            await asyncio.create_task(child())
            assert invoke(root)[1] == ["loaded", "invoked"]
    asyncio.run(run())


def test_control_identity_replacement_refuses_before_direct_effect(roots):
    root, control = roots
    with host(roots).activate(), pytest.raises(MutationStoreError), MutationStore(*roots).workspace_locked():
        control.rename(control.with_name("old-control"))
        native_os.mkdir(control, mode=0o700)
        with pytest.raises(ConfigError):
            call_seam(lambda _: pytest.fail("owner loaded"), "coverage_manifest", "run_init_inline", root)


def test_controlled_compound_ingest_is_explicitly_unsupported(roots):
    selected = replace(host(roots), grants={"onboarding.research.ingest"})
    with selected.activate(), pytest.raises(ConfigError) as error:
        selected.require_grant("onboarding.research.ingest")
    assert error.value.details == {"reason": "host_mutation_compound_writer_unsupported"}
    legacy = replace(selected, mutation_control_directory=None)
    legacy.require_grant("onboarding.research.ingest")


def test_direct_compound_ingest_refuses_before_inputs_or_coordination(roots, monkeypatch):
    from evidence_wiki import research_operations

    root, control = roots
    monkeypatch.setattr(research_operations, "_no_secret_values", lambda *a: pytest.fail("inputs inspected"))
    monkeypatch.setattr(research_operations, "coordinated", lambda *a: pytest.fail("coordination started"))
    with host(roots).activate(), pytest.raises(ConfigError) as error:
        research_operations.ingest(root / "missing", agent_id="caller", run_id="run", request_id="request")
    assert error.value.details == {"reason": "host_operation_unsupported"}
    assert not (root / ".locks").exists() and not (control / "mutations").exists()


def test_onboarding_compound_ingest_refuses_before_workspace_lookup(roots, monkeypatch):
    root, control = roots
    onboarding = Onboarding.open(allowed_roots=[root], allow=["research.ingest"])
    monkeypatch.setattr(onboarding._scope, "builtin_workspace", lambda *a: pytest.fail("workspace inspected"))
    selected = replace(host(roots), grants={"onboarding.research.ingest"})
    try:
        with selected.activate(), pytest.raises(ConfigError) as error:
            onboarding.research(root / "missing", "ingest", agent_id="caller", run_id="run", request_id="request")
        assert error.value.details == {"reason": "host_mutation_compound_writer_unsupported"}
    finally:
        onboarding.close()
    assert not (root / ".locks").exists() and not (control / "mutations").exists()
