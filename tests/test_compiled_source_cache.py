"""Exact source compilation reuse without module or authority state reuse."""

from __future__ import annotations

import builtins
import os
from pathlib import Path

from evidence_wiki import _script_host, host_context
from tests._script_loader import load_isolated_module


def test_compiled_code_reuses_exact_bytes_and_still_executes_fresh_globals(tmp_path, monkeypatch):
    calls = []
    original = builtins.compile
    filename = str(tmp_path / "module.py")

    def compile_source(*args, **kwargs):
        calls.append(args[1])
        return original(*args, **kwargs)

    monkeypatch.setattr(builtins, "compile", compile_source)
    source = b"items = []\nclass Example: pass\n"
    first = _script_host.compiled_source_code(source, filename)
    second = _script_host.compiled_source_code(source, filename)
    assert first is second and calls == [filename]
    left, right = {}, {}
    exec(first, left)  # noqa: S102 -- fixed synthetic source, distinct namespaces
    exec(second, right)  # noqa: S102 -- fixed synthetic source, distinct namespaces
    left["items"].append("first")
    assert right["items"] == [] and left["Example"] is not right["Example"]


def test_same_length_and_mtime_rewrite_cannot_reuse_old_code(tmp_path):
    filename = tmp_path / "module.py"
    filename.write_bytes(b"value = 1\n")
    stamp = filename.stat()
    before = _script_host.compiled_source_code(filename.read_bytes(), str(filename))
    filename.write_bytes(b"value = 2\n")
    os.utime(filename, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    after = _script_host.compiled_source_code(filename.read_bytes(), str(filename))
    namespace = {}
    exec(after, namespace)  # noqa: S102 -- fixed synthetic source, fresh namespace
    assert before is not after and namespace["value"] == 2


def test_filename_and_source_are_part_of_compilation_identity(tmp_path):
    source = b"value = 1\n"
    left = _script_host.compiled_source_code(source, str(tmp_path / "left.py"))
    right = _script_host.compiled_source_code(source, str(tmp_path / "right.py"))
    assert left is not right
    assert left.co_filename.endswith("left.py") and right.co_filename.endswith("right.py")


def test_workspace_loader_uses_shared_cache_but_new_module_identity(tmp_path, monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / "workspace-template/scripts"
    loader = load_isolated_module("compiled_source_loader", scripts / "_workspace_module_loader.py")
    (tmp_path / "example.py").write_text("items = []\nclass Example: pass\n")
    compiled = []
    original = host_context.compiled_source_code

    def observe(source, filename):
        compiled.append((source, filename))
        return original(source, filename)

    monkeypatch.setattr(host_context, "compiled_source_code", observe)
    first = loader.load_workspace_module(tmp_path, "example", cache={}, host_context_bridge=host_context)
    second = loader.load_workspace_module(tmp_path, "example", cache={}, host_context_bridge=host_context)
    assert first is not second and first.Example is not second.Example
    first.items.append("first")
    assert second.items == []
    assert len(compiled) == 2 and compiled[0] == compiled[1]


def test_compilation_cache_has_fixed_capacity(tmp_path, monkeypatch):
    monkeypatch.setattr(_script_host, "_COMPILED_SOURCE_MAX_ENTRIES", 2)
    for index in range(4):
        _script_host.compiled_source_code(f"value = {index}\n".encode(), str(tmp_path / f"bounded-{index}.py"))
    assert len(_script_host._COMPILED_SOURCE) <= 2
    assert _script_host._COMPILED_SOURCE_BYTES <= _script_host._COMPILED_SOURCE_MAX_BYTES
