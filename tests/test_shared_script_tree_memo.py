"""Shared code identity preserves fresh metadata and host generation boundaries."""

import inspect
import os
import shutil
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from evidence_wiki import _script_host, host_context
from evidence_wiki.errors import ConfigError


@pytest.fixture
def memo(monkeypatch):
    monkeypatch.setattr(_script_host, "_SCRIPT_TREE_MEMO", OrderedDict())
    monkeypatch.setattr(_script_host, "_SCRIPT_TREE_MEMO_BYTES", 0)


def scripts(tmp_path, name="assets"):
    root = tmp_path / name / "workspace-template/scripts"
    root.mkdir(parents=True)
    source = Path(__file__).resolve().parents[1] / "workspace-template/scripts/_workspace_module_loader.py"
    shutil.copyfile(source, root / source.name)
    (root / "leaf.py").write_text("VALUE = 1\n", encoding="utf-8")
    return root


def loader(root, number=0):
    path = root / "_workspace_module_loader.py"
    module = ModuleType(f"isolated_loader_{number}")
    module.__file__ = str(path)
    exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)  # noqa: S102 -- exact copied fixture loader.
    return module


def counted_hash(monkeypatch):
    original = _script_host._script_content_tree_hash
    calls = []

    def counted(root):
        calls.append(root)
        return original(root)

    monkeypatch.setattr(_script_host, "_script_content_tree_hash", counted)
    return calls


def test_isolated_loaders_share_only_digest_and_still_check_each_generation(tmp_path, memo, monkeypatch):
    root = scripts(tmp_path)
    calls = counted_hash(monkeypatch)
    checked = []
    original = host_context.check_generation

    def check(root, value):
        checked.append((root, value))
        return original(root, value)

    monkeypatch.setattr(host_context, "check_generation", check)
    instances = [loader(root, number) for number in range(5)]
    for instance in instances:
        for _ in range(10):
            value = instance.load_workspace_module(root, "leaf", host_context_bridge=host_context)
            assert value.VALUE == 1
            value.VALUE = 99
    assert calls == [root.resolve()]
    assert len(checked) == 50 and len({value for _, value in checked}) == 1


def test_signature_and_digest_match_original_portable_owner(tmp_path, memo):
    root = scripts(tmp_path)
    (root / "alias.py").symlink_to(root / "leaf.py")
    (root / "missing.py").symlink_to(root / "absent.py")
    (root / "directory.py").mkdir()
    portable = loader(root)
    assert _script_host._script_tree_signature(root) == portable._tree_signature(root)
    assert host_context.script_tree_hash(root) == portable._tree_hash(root)


def test_restored_mtime_rewrite_and_loader_change_refresh_digest(tmp_path, memo, monkeypatch):
    root = scripts(tmp_path)
    calls = counted_hash(monkeypatch)
    before = host_context.script_tree_hash(root)
    leaf = root / "leaf.py"
    stamp = leaf.stat()
    leaf.write_text("VALUE = 2\n", encoding="utf-8")
    os.utime(leaf, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    after = host_context.script_tree_hash(root)
    assert after != before
    assert loader(root).load_workspace_module(root, "leaf", host_context_bridge=host_context).VALUE == 2
    path = root / "_workspace_module_loader.py"
    path.write_bytes(path.read_bytes() + b"\n# Current loader declaration.\n")
    assert host_context.script_tree_hash(root) != after
    assert len(calls) == 3


def test_concurrent_cold_loads_hash_once_with_fresh_metadata(tmp_path, memo, monkeypatch):
    root = scripts(tmp_path)
    calls = counted_hash(monkeypatch)
    barrier = threading.Barrier(5)

    def invoke(_):
        barrier.wait(timeout=5)
        return host_context.script_tree_hash(root)

    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(invoke, range(5)))
    assert len(set(results)) == 1 and calls == [root.resolve()]


def test_changed_during_cold_read_is_returned_but_not_retained(tmp_path, memo, monkeypatch):
    root = scripts(tmp_path)
    actual = _script_host._script_content_tree_hash
    calls = []

    def changed(root):
        calls.append(root)
        value = actual(root)
        if len(calls) == 1:
            (root / "leaf.py").write_text("VALUE = 2\n", encoding="utf-8")
        return value

    monkeypatch.setattr(_script_host, "_script_content_tree_hash", changed)
    first = host_context.script_tree_hash(root)
    second = host_context.script_tree_hash(root)
    assert first != second and len(calls) == 2
    assert host_context.script_tree_hash(root) == second and len(calls) == 2


def test_roots_are_distinct_and_evicted_digest_is_recomputed(tmp_path, memo, monkeypatch):
    first, second = scripts(tmp_path, "first"), scripts(tmp_path, "second")
    calls = counted_hash(monkeypatch)
    monkeypatch.setattr(_script_host, "_SCRIPT_TREE_MEMO_MAX_ROOTS", 1)
    value = host_context.script_tree_hash(first)
    assert host_context.script_tree_hash(second) == value
    assert host_context.script_tree_hash(first) == value
    assert calls == [first.resolve(), second.resolve(), first.resolve()]
    assert len(_script_host._SCRIPT_TREE_MEMO) == 1


def test_byte_bound_returns_valid_digest_without_retaining_large_entry(tmp_path, memo, monkeypatch):
    root = scripts(tmp_path)
    calls = counted_hash(monkeypatch)
    monkeypatch.setattr(_script_host, "_SCRIPT_TREE_MEMO_MAX_BYTES", 1)
    assert host_context.script_tree_hash(root) == host_context.script_tree_hash(root)
    assert len(calls) == 2 and not _script_host._SCRIPT_TREE_MEMO
    assert _script_host._SCRIPT_TREE_MEMO_BYTES == 0


def test_bridge_cannot_accept_computed_digest_or_mutable_memo(tmp_path, memo):
    root = scripts(tmp_path)
    assert list(inspect.signature(host_context.script_tree_hash).parameters) == ["root"]
    with pytest.raises(TypeError):
        host_context.script_tree_hash(root, signature=(), digest="0" * 64)
    returned = host_context.script_tree_hash(root)
    assert type(returned) is str and returned == loader(root)._content_tree_hash(root)


def test_lookalike_bridge_does_not_supply_shared_digest(tmp_path, memo):
    root = scripts(tmp_path)
    fake = SimpleNamespace(__name__="evidence_wiki.host_context",
        check_generation=host_context.check_generation, loading_lock=host_context.loading_lock,
        compiled_source_code=host_context.compiled_source_code,
        script_tree_hash=lambda _root: pytest.fail("Untrusted computed digest accepted"))
    assert loader(root).load_workspace_module(root, "leaf", host_context_bridge=fake).VALUE == 1
    assert not _script_host._SCRIPT_TREE_MEMO


def test_real_pinned_generation_refuses_warm_cached_module_after_rewrite(tmp_path, memo, monkeypatch):
    root = scripts(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(_script_host, "shared_assets_root", lambda: root.parents[1])
    context = host_context.HostContext(allowed_roots=[workspace], grants=[], actor_ref="owner")
    with context.activate():
        with pytest.raises(ConfigError):
            with host_context.call_binding():
                value = _script_host.load_packaged_script(root.parents[1], "leaf")
                assert value.VALUE == 1
                (root / "leaf.py").write_text("VALUE = 2\n", encoding="utf-8")
                with pytest.raises(ConfigError):
                    _script_host.load_packaged_script(root.parents[1], "leaf")


def test_unbridged_loader_keeps_original_local_memo(tmp_path, memo):
    root = scripts(tmp_path)
    portable = loader(root)
    assert portable.load_workspace_module(root, "leaf").VALUE == 1
    assert root.resolve() in portable._TREE_HASH_MEMO
    assert not _script_host._SCRIPT_TREE_MEMO


def test_changed_owner_algorithm_identity_cannot_reuse_previous_digest(tmp_path, memo, monkeypatch):
    root = scripts(tmp_path)
    calls = counted_hash(monkeypatch)
    first = host_context.script_tree_hash(root)
    original = _script_host._script_tree_signature

    def current_signature(root):
        return original(root)

    monkeypatch.setattr(_script_host, "_script_tree_signature", current_signature)
    assert host_context.script_tree_hash(root) == first
    assert len(calls) == 2


def test_payload_budget_evicts_old_root_without_reusing_its_identity(tmp_path, memo, monkeypatch):
    first, second = scripts(tmp_path, "first"), scripts(tmp_path, "other")
    calls = counted_hash(monkeypatch)
    expected = host_context.script_tree_hash(first)
    payload = _script_host._SCRIPT_TREE_MEMO_BYTES
    monkeypatch.setattr(_script_host, "_SCRIPT_TREE_MEMO_MAX_BYTES", payload)
    assert host_context.script_tree_hash(second) == expected
    assert _script_host._SCRIPT_TREE_MEMO_BYTES <= payload
    assert host_context.script_tree_hash(first) == expected
    assert calls == [first.resolve(), second.resolve(), first.resolve()]
