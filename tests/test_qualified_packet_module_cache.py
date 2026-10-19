"""Module reuse preserves current packet eligibility and host scope."""

import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from evidence_wiki import _script_host, host_context
from tests._script_loader import load_isolated_module
from tests.test_qualified_packet_intake import FIXTURES, SOURCE_ID, delivery

SCRIPTS = Path(__file__).resolve().parents[1] / "workspace-template/scripts"


@pytest.mark.parametrize("scripts", [SCRIPTS, SCRIPTS.parents[1] / "examples/urban-heat-resilience-workspace/scripts"])
def test_policy_reuses_module_but_rechecks_delivery_and_policy(tmp_path, monkeypatch, scripts):
    policies = load_isolated_module("cached_packet_policy", scripts / "_evidence_policies.py")
    policies._QUALIFIED_PACKET_CACHE.clear()
    loaded = []
    original = policies.load_workspace_module

    def observe(root, stem, **kwargs):
        module = original(root, stem, **kwargs)
        if stem == "_qualified_packet":
            loaded.append(module)
        return module

    monkeypatch.setattr(policies, "load_workspace_module", observe)
    config, record, folder = delivery(tmp_path, next(iter(sorted(FIXTURES.glob("*.json")))).read_bytes())
    packet = original(scripts, "_qualified_packet", cache=policies._QUALIFIED_PACKET_CACHE)
    report = packet.inspect_packet(tmp_path, config, record)
    assert report["valid"] and report["policy_satisfied"]

    def reasons(selected_config, selected_record, normalized):
        inputs = policies.PolicyInputs(tmp_path, selected_config, {SOURCE_ID: selected_record},
            {SOURCE_ID: normalized}, {}, [], {}, {}, {})
        return policies.source_unusable_evidence_reasons(inputs, SOURCE_ID)

    normalized = {"qualified_context": report}
    baseline = reasons(config, record, normalized)
    assert baseline == []
    required = copy.deepcopy(config)
    required["integrations"]["codebase_analysis"]["require_live_reconciliation"] = True
    required_report = packet.inspect_packet(tmp_path, required, record)
    assert "live_reconciliation_required" in reasons(required, record, {"qualified_context": required_report})
    conflicting_record = copy.deepcopy(record)
    conflicting_record["metadata"]["codebase_intake_profile"] = "unsupported"
    assert "unsupported_or_conflicting_intake_profile" in reasons(config, conflicting_record, normalized)
    (folder / "packet.json").write_bytes(b"changed original")
    assert "packet_normalized_binding_mismatch" in reasons(config, record, normalized)
    assert loaded and all(module is packet for module in loaded)
    assert len(policies._QUALIFIED_PACKET_CACHE) == 1


def test_module_cache_checks_generation_roots_bridges_and_current_scope(tmp_path):
    loader = _script_host._load_workspace_loader(SCRIPTS)
    roots = [tmp_path / "first", tmp_path / "second"]
    for root in roots:
        root.mkdir()
        (root / "sample.py").write_text("from _host_context import current_context\ndef actor():\n    context = current_context()\n    return context.actor_ref if context is not None else None\n")
    cache = {}
    # Fake bridges exercise the loader's explicit bridge separation without
    # asserting that a synthetic module is authenticated as a native bridge.
    checks = []
    bridge = SimpleNamespace(current_context=host_context.current_context,
        check_generation=lambda root, generation: checks.append((root, generation)),
        loading_lock=lambda: loader._IMPORT_LOCK)
    first = loader.load_workspace_module(roots[0], "sample", cache=cache, host_context_bridge=bridge)
    assert loader.load_workspace_module(roots[0], "sample", cache=cache, host_context_bridge=bridge) is first
    assert len(checks) == 2
    for actor in ("reader-one", "reader-two"):
        context = host_context.HostContext((tmp_path,), frozenset(), actor)
        token = host_context._ACTIVE.set(context)
        try:
            assert first.actor() == actor
        finally:
            host_context._ACTIVE.reset(token)
    assert first.actor() is None
    other = loader.load_workspace_module(roots[1], "sample", cache=cache, host_context_bridge=bridge)
    assert other is not first
    second_bridge = SimpleNamespace(current_context=host_context.current_context,
        check_generation=bridge.check_generation, loading_lock=bridge.loading_lock)
    assert loader.load_workspace_module(roots[0], "sample", cache=cache, host_context_bridge=second_bridge) is not first
    prior = loader.load_workspace_module(roots[0], "sample", cache=cache, host_context_bridge=bridge)
    (roots[0] / "sibling.py").write_text("VALUE = 1\n")
    assert loader.load_workspace_module(roots[0], "sample", cache=cache, host_context_bridge=bridge) is not prior
    assert len([key for key in cache if key.startswith(str(roots[0]) + "\0sample\0")]) == 1


def test_cached_module_does_not_bypass_pinned_generation(tmp_path):
    loader = _script_host._load_workspace_loader(SCRIPTS)
    (tmp_path / "sample.py").write_text("VALUE = 1\n")
    cache = {}
    bridge = SimpleNamespace(check_generation=host_context.check_generation,
        loading_lock=lambda: loader._IMPORT_LOCK)
    module = loader.load_workspace_module(tmp_path, "sample", cache=cache, host_context_bridge=bridge)
    context = host_context.HostContext((tmp_path,), frozenset(), "reader")
    active = host_context._ACTIVE.set(context)
    pinned = host_context._GENERATION.set((tmp_path, "different-generation"))
    try:
        from evidence_wiki.errors import ConfigError
        with pytest.raises(ConfigError):
            loader.load_workspace_module(tmp_path, "sample", cache=cache, host_context_bridge=bridge)
        assert module.VALUE == 1
    finally:
        host_context._GENERATION.reset(pinned)
        host_context._ACTIVE.reset(active)
