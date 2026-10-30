"""Explicit host selections, automatic permissions and isolated owner loading."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from evidence_wiki import Onboarding, Workspace, _script_host
from evidence_wiki import host_context as bridge
from evidence_wiki._facades._base import call_seam
from evidence_wiki._filesystem import os as native_os
from evidence_wiki.errors import ConfigError
from evidence_wiki.host_context import HostContext, checked_operation

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "workspace-template/scripts"


def context(root, *, grants=(), writes=(), authority=None, state=None, controllers=(), credentials=()):
    return HostContext(allowed_roots=[root], grants=grants, actor_ref="host-caller",
                       writable_roots=writes, authority_file=authority, state_directory=state,
                       controller_refs=controllers, credential_refs=credentials)


def owner(stem):
    return _script_host.load_packaged_script(_script_host.shared_assets_root(), stem)


def test_root_capture_preserves_the_owners_io_error(tmp_path):
    failure = OSError("Synthetic owning operation interruption.")
    with pytest.raises(OSError) as error:
        with bridge._root_descriptor(tmp_path.resolve()):
            raise failure
    assert error.value is failure


@pytest.fixture
def selections(tmp_path):
    values = []
    for name in ("first", "second"):
        root = tmp_path / name
        root.mkdir()
        (root / "research.yml").write_text("{}\n")
        authority = tmp_path / (name + "-authority.json")
        authority.write_bytes(name.encode())
        native_os.chmod(authority, 0o600)
        state = tmp_path / (name + "-state")
        native_os.mkdir(state, mode=0o700)
        values.append((root, authority, state))
    return values


@pytest.fixture
def strict_host(tmp_path, monkeypatch, request):
    from tests.test_strict_evidence import host

    return host.__wrapped__(tmp_path, monkeypatch, request)


def test_context_detaches_and_freezes_nested_selections(selections):
    root, authority, state = selections[0]
    roots, writes, grants = [root], [root], ["coverage_manifest.run_evaluate"]
    controllers = {"evaluator": "machine-evaluator"}
    credentials = {"evaluator": "private-capability"}
    selected = HostContext(roots, grants, "host-caller", writes, controllers, authority, state, credentials)
    roots.clear()
    writes.clear()
    grants.clear()
    controllers["evaluator"] = "other-machine"
    credentials.clear()
    assert selected.allowed_roots == (root.resolve(),)
    assert selected.writable_roots == (root.resolve(),)
    assert selected.grants == frozenset({"coverage_manifest.run_evaluate"})
    assert selected.controller_refs == (("evaluator", "machine-evaluator"),)
    assert selected.credential_refs == (("evaluator", "private-capability"),)
    with pytest.raises(FrozenInstanceError):
        selected.actor_ref = "other"
    rendered = repr(selected)
    assert str(authority) not in rendered and str(state) not in rendered
    assert "private-capability" not in rendered


def test_explicit_absent_selections_suppress_poisoned_environment(selections, monkeypatch):
    root, authority, state = selections[0]
    monkeypatch.setenv("EVIDENCE_WIKI_AUTHORITY_FILE", str(authority))
    monkeypatch.setenv("EVIDENCE_WIKI_STATE_DIR", str(state))
    authority_owner, store, usage, strict = (owner(name) for name in
        ("_evidence_authority", "_host_evidence_store", "_evidence_usage", "_strict_evidence"))
    assert authority_owner.read_host_policy(root) == b"first"
    assert usage.configured({})
    with context(root).activate():
        assert bridge.authority_path() is None and bridge.state_path() is None
        assert not usage.configured({}) and not strict.configured(root, {})
        assert strict.resolve_policy(root, {}) is None
        with pytest.raises(ValueError, match="host_trust_unavailable"):
            authority_owner.read_host_policy(root)
        with pytest.raises(ValueError, match="host_state_unavailable"), store.host_directory(root):
            pass
    assert bridge.authority_path() == str(authority)


def test_selected_authority_reaches_separate_cached_and_lazy_owners(selections, monkeypatch):
    first, second = selections
    monkeypatch.setenv("EVIDENCE_WIKI_AUTHORITY_FILE", str(second[1]))
    monkeypatch.setenv("EVIDENCE_WIKI_STATE_DIR", str(second[2]))
    direct = owner("_evidence_authority")
    strict = owner("_strict_evidence")
    assert direct.read_host_policy is not strict.read_host_policy
    assert direct.authority_path is strict.authority_path is bridge.authority_path
    with context(first[0], authority=first[1], state=first[2]).activate():
        assert direct.read_host_policy(first[0]) == b"first"
        assert strict.read_host_policy(first[0]) == b"first"
        lazy_usage = strict.sibling("_evidence_usage")
        assert lazy_usage.state_path is bridge.state_path
        assert lazy_usage.configured({})
        with lazy_usage.locked_state.__wrapped__.__globals__["host_directory"](first[0]) as descriptor:
            actual = os.fstat(descriptor)
            assert (actual.st_dev, actual.st_ino) == (first[2].stat().st_dev, first[2].stat().st_ino)
        with pytest.raises(ConfigError):
            direct.read_host_policy(second[0])


def test_nested_exception_scope_restores_selection(selections):
    first, second = selections
    assert bridge.current_context() is None
    with context(first[0], authority=first[1]).activate():
        assert bridge.authority_path() == str(first[1])
        with pytest.raises(RuntimeError), context(second[0], authority=second[1]).activate():
            assert bridge.authority_path() == str(second[1])
            raise RuntimeError("synthetic failure")
        assert bridge.authority_path() == str(first[1])
    assert bridge.current_context() is None


def test_interleaved_threads_and_async_tasks_do_not_share_selections(selections, monkeypatch):
    import threading

    monkeypatch.setenv("EVIDENCE_WIKI_AUTHORITY_FILE", "/unavailable/ambient-authority")
    gate = threading.Barrier(2)
    authority_owner = owner("_evidence_authority")

    def read(value):
        root, authority, state = value
        with context(root, authority=authority, state=state).activate():
            gate.wait(timeout=5)
            return authority_owner.read_host_policy(root), bridge.state_path()

    with ThreadPoolExecutor(max_workers=2) as pool:
        actual = list(pool.map(read, selections))
    assert actual == [(b"first", str(selections[0][2])), (b"second", str(selections[1][2]))]

    async def task(value):
        root, authority, state = value
        with context(root, authority=authority, state=state).activate():
            await asyncio.sleep(0)
            return authority_owner.read_host_policy(root), bridge.state_path()

    async def run():
        return await asyncio.gather(*(task(value) for value in selections))

    assert asyncio.run(run()) == actual
    assert bridge.current_context() is None
    assert os.environ["EVIDENCE_WIKI_AUTHORITY_FILE"] == "/unavailable/ambient-authority"


@pytest.mark.parametrize("failure", ["grant", "root", "write", "unknown", "actor", "human_review", "input_path"])
def test_public_boundary_denies_before_owner_load_or_effect(selections, failure):
    first, second = selections
    stem, seam = "coverage_manifest", "run_evaluate"
    root, kwargs, writes = first[0], {}, ()
    grants = [f"{stem}.{seam}"]
    if failure == "grant":
        grants = []
    elif failure == "root":
        root = second[0]
    elif failure in {"write", "actor"}:
        stem, seam = "question_claim", "run_claim"
        grants = [f"{stem}.{seam}"]
        kwargs = {"agent_id": "other" if failure == "actor" else "host-caller"}
        writes = [first[0]] if failure == "actor" else []
    elif failure == "unknown":
        stem, seam = "new_owner", "run_mutation"
        grants = [f"{stem}.{seam}"]
    elif failure == "human_review":
        stem, seam = "question_resolve", "run_approve"
        grants, writes, kwargs = [f"{stem}.{seam}"], [first[0]], {"reviewer": "machine"}
    elif failure == "input_path":
        stem, seam = "intake_questions", "run_intake"
        grants, writes, kwargs = [f"{stem}.{seam}"], [first[0]], {"from_file": str(second[0] / "batch.json")}
    effects = []

    def load(_):
        effects.append("loaded")
        return SimpleNamespace(**{seam: lambda *args, **kw: effects.append("called")})

    with context(first[0], grants=grants, writes=writes).activate(), pytest.raises(ConfigError) as exc:
        call_seam(load, stem, seam, root, **kwargs)
    assert exc.value.error_code == "HOST_CONTEXT_REFUSED"
    assert not effects


@pytest.mark.parametrize("write", [False, True])
def test_conditional_write_effect_checked_from_actual_arguments(selections, write):
    root = selections[0][0]
    effects = []
    selected = context(root, grants=["verify_quotes.run_verify"])
    with selected.activate():
        if write:
            with pytest.raises(ConfigError):
                call_seam(lambda _: effects.append("loaded"), "verify_quotes", "run_verify", root, write=True)
        else:
            result = call_seam(lambda _: SimpleNamespace(run_verify=lambda *a, **k: {"ok": True}),
                               "verify_quotes", "run_verify", root, write=False)
            assert result == {"ok": True}
    assert not effects


def test_existing_workspace_handle_is_authorized_automatically(selections, monkeypatch):
    root = selections[0][0]
    effects = []
    monkeypatch.setattr(_script_host, "load_packaged_script", lambda *args: effects.append("loaded"))
    with Workspace.open(root) as workspace, context(root).activate(), pytest.raises(ConfigError):
        workspace.coverage.evaluate("question")
    assert not effects


def test_root_replacement_and_authority_overlap_refuse_before_effect(selections):
    root, authority, state = selections[0]
    selected = context(root, grants=["coverage_manifest.run_evaluate"], writes=[root])
    root.rename(root.with_name("retired"))
    root.mkdir()
    with selected.activate(), pytest.raises(ConfigError, match="does not permit"):
        call_seam(lambda _: pytest.fail("owner loaded"), "coverage_manifest", "run_evaluate", root)
    for protected in (root / "authority.json", root / "state"):
        with pytest.raises(ConfigError):
            context(root, authority=protected)
    with pytest.raises(ConfigError):
        context(root, writes=[state])
    assert authority.exists()


def test_onboarding_intersects_existing_roots_and_grants(selections, monkeypatch):
    first, second = selections
    monkeypatch.setenv("EVIDENCE_WIKI_STATE_DIR", str(first[0] / "ambient-state"))
    with Onboarding.open(allowed_roots=[first[0]]) as handle:
        with context(first[0], grants=["onboarding.resources"]).activate():
            page = handle.resources()
            returned, total = page["bounds"]["returned"], page["bounds"]["total"]
            assert returned == len(page["resources"]) <= 128
            assert page["bounds"]["truncated"] is (returned < total)
            with pytest.raises(ConfigError):
                handle.recipes()
            assert handle._scope.path(first[0]) == first[0].resolve()
        with context(second[0], grants=["onboarding.bootstrap"]).activate(), pytest.raises(ConfigError):
            handle.bootstrap(first[0])
        with context(first[0], grants=["onboarding.apply"], writes=[first[0]]).activate(), pytest.raises(Exception) as exc:
            handle.apply({})
        assert exc.value.error_code == "ONBOARDING_AUTHORITY_REQUIRED"


def synthetic_assets(tmp_path):
    assets = tmp_path / "assets"
    scripts = assets / "workspace-template/scripts"
    scripts.mkdir(parents=True)
    shutil.copyfile(SCRIPTS / "_workspace_module_loader.py", scripts / "_workspace_module_loader.py")
    shutil.copyfile(SCRIPTS / "_host_context.py", scripts / "_host_context.py")
    (scripts / "_native_fs.py").write_text("import os\n")
    (scripts / "leaf.py").write_text("from _host_context import authority_path\ndef read(): return authority_path()\n")
    (scripts / "core.py").write_text(
        "from pathlib import Path\nfrom _host_context import authority_path\n"
        "from _workspace_module_loader import load_workspace_module\n"
        "def lazy(): return load_workspace_module(Path(__file__).parent, 'leaf')\n")
    (scripts / "nested.py").write_text(
        "from pathlib import Path\nfrom _workspace_module_loader import load_workspace_module\n"
        "def lazy(): return load_workspace_module(Path(__file__).parent, 'leaf')\n")
    (scripts / "indirect.py").write_text("from nested import lazy\n")
    return assets, scripts


def test_bridge_binding_is_cache_specific_and_restores_module_table(tmp_path, monkeypatch):
    assets, scripts = synthetic_assets(tmp_path)
    loader = _script_host._load_workspace_loader(scripts)
    cache = {}
    monkeypatch.setenv("EVIDENCE_WIKI_AUTHORITY_FILE", "ambient")
    previous = sys.modules.get("_host_context")
    standalone = loader.load_workspace_module(scripts, "core", cache=cache)
    bound = loader.load_workspace_module(scripts, "core", cache=cache, host_context_bridge=bridge)
    assert standalone is not bound
    assert standalone.authority_path is not bridge.authority_path
    assert bound.authority_path is bridge.authority_path
    assert standalone.authority_path() == "ambient"
    root = tmp_path / "workspace"
    root.mkdir()
    with context(root).activate():
        assert standalone.authority_path() is None
    assert standalone.authority_path() == "ambient"
    assert sys.modules.get("_host_context") is previous
    packaged = _script_host.load_packaged_script(assets, "core")
    assert packaged.lazy().authority_path is bridge.authority_path
    indirect = _script_host.load_packaged_script(assets, "indirect")
    assert indirect.lazy().authority_path is bridge.authority_path


def test_generation_change_refuses_cached_and_lazy_owners(tmp_path, monkeypatch):
    assets, scripts = synthetic_assets(tmp_path)
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setattr(_script_host, "shared_assets_root", lambda: assets)
    selected = context(root, grants=["coverage_manifest.run_evaluate"], writes=[root])
    with selected.activate(), checked_operation("coverage_manifest", "run_evaluate", (root,), {}):
        core = _script_host.load_packaged_script(assets, "core")
        assert core.lazy().authority_path is bridge.authority_path
    with selected.activate(), pytest.raises(ConfigError, match="does not permit"):
        with checked_operation("coverage_manifest", "run_evaluate", (root,), {}):
            core = _script_host.load_packaged_script(assets, "core")
            (scripts / "leaf.py").write_text("VALUE = 'replacement'\n")
            with pytest.raises(ConfigError):
                _script_host.load_packaged_script(assets, "core")
            with pytest.raises(ConfigError):
                core.lazy()


def test_retained_usage_view_cannot_cross_contexts_or_legacy_boundary(selections, monkeypatch):
    usage = owner("_evidence_usage")
    first, second = selections
    now = datetime.now(timezone.utc)
    with context(first[0], authority=first[1], state=first[2]).activate():
        view = usage.UsageView(SimpleNamespace(), {}, now)
        view.require_selection()
    with context(first[0], authority=second[1], state=second[2]).activate():
        for invoke in (lambda: view.require_selection(), lambda: view.state, lambda: view.trust, lambda: view.checked,
                       lambda: view.revalidate(first[0], {}),
                       lambda: view.check("invalid", uses=[], purpose="p", consumer="c"),
                       lambda: view.lineage("invalid")):
            with pytest.raises(ValueError, match="host_view_selection_changed"):
                invoke()
        monkeypatch.setattr(usage, "captured_view", lambda *args: view)
        with pytest.raises(ValueError, match="host_view_selection_changed"), usage.current_view(first[0], {}):
            pass
    with pytest.raises(ValueError, match="host_view_selection_changed"):
        view.require_selection()


def test_controller_reference_does_not_authenticate_and_machine_role_stays_evaluator(selections):
    authority = owner("_evidence_authority")
    now = datetime.now(timezone.utc)
    issued, expiry = ((now + timedelta(seconds=value)).isoformat() for value in (-1, 60))
    policy = {"policy_id": "synthetic-policy", "policy_revision": "one", "not_before": issued,
              "expires_at": expiry, "revoked_keys": [], "revoked_envelopes": [],
              "principals": {"machine-principal": {"controller": "machine-controller", "roles": ["evaluator"],
                                                   "keys": {"synthetic-key": "11" * 32}}}}
    payload = {"synthetic": True}
    metadata = {"schema_version": authority.AUTH_SCHEMA, "scheme": "hmac-sha256", "principal": "machine-principal",
                "key_id": "synthetic-key", "role": "evaluator", "policy_id": "synthetic-policy",
                "policy_revision": "one", "issued_at": issued, "expires_at": expiry}
    message = b"evidence-attestation/v1\0" + authority.canonical_bytes({"payload": payload, "authentication": metadata})
    envelope = {"payload": payload, "authentication": {**metadata,
        "signature": hmac.new(bytes.fromhex("11" * 32), message, hashlib.sha256).hexdigest()}}
    trust = {"policy": policy, "content_hash": "sha256:" + "0" * 64}
    with context(selections[0][0], controllers={"evaluator": "machine-controller"}).activate():
        assert not authority.verify_attestation({"payload": payload}, "evaluator", trust, now)["authenticated"]
        assert authority.verify_attestation(envelope, "evaluator", trust, now)["authenticated"]
        assert not authority.verify_attestation(envelope, "human-review", trust, now)["authenticated"]
        revoked = json.loads(json.dumps(trust))
        revoked["policy"]["revoked_keys"] = ["synthetic-key"]
        assert authority.verify_attestation(envelope, "evaluator", revoked, now)["reason"] == "authentication_revoked"
    with context(selections[0][0], controllers={"evaluator": "other-controller"}).activate():
        result = authority.verify_attestation(envelope, "evaluator", trust, now)
        assert not result["authenticated"] and result["reason"] == "authentication_controller_not_selected"


def test_strict_publication_uses_explicit_selections_through_private_captures(strict_host, monkeypatch):
    from tests.test_strict_evidence import CORE, QUESTION, review

    review(strict_host)
    expected = CORE.publication(strict_host.root)
    assert expected["verdict"] == "ship"
    selected = context(strict_host.root, authority=strict_host.policy_path, state=strict_host.host,
                       grants=["workspace.open", "export_answers.run_export", "publication_readiness.run_selected_publication"])
    monkeypatch.setenv("EVIDENCE_WIKI_AUTHORITY_FILE", "/unavailable/ambient-authority")
    monkeypatch.setenv("EVIDENCE_WIKI_STATE_DIR", "/unavailable/ambient-state")
    with selected.activate(), Workspace.open(strict_host.root) as workspace:
        assert workspace.export_answers()["questions"] == expected["questions"]
        assert workspace.publish_selected([QUESTION])["questions"] == expected["questions"]


def test_onboarding_actor_mismatch_is_denied_before_owner_invocation(selections, monkeypatch):
    root = selections[0][0]
    effects = []
    monkeypatch.setattr("evidence_wiki.research_actions.guidance", lambda *a, **k: effects.append("called"))
    with Onboarding.open(allowed_roots=[root]) as handle:
        with context(root, grants=["onboarding.research_next"]).activate(), pytest.raises(ConfigError) as exc:
            handle.research_next(root, agent_id="other-controller")
    assert exc.value.details["reason"] == "host_actor_mismatch"
    assert not effects
