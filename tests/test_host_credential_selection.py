"""Opaque host selections never inherit ambient credential observations."""

import json
from types import SimpleNamespace

from evidence_wiki import Onboarding, host_capabilities
from evidence_wiki.host_context import HostContext
from tests.test_research_planning import request
from tests.test_source_capabilities import tools_manifest


def test_credential_observation_preserves_legacy_and_explicit_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(host_capabilities, "os", SimpleNamespace(environ={"SYNTHETIC_TOKEN": "synthetic-private"}))
    assert host_capabilities.credential_observation("SYNTHETIC_TOKEN") == {"present": True}
    for reference in (None, "first-capability", "second-capability"):
        refs = {} if reference is None else {"SYNTHETIC_TOKEN": reference}
        context = HostContext([tmp_path], [], "caller", credential_refs=refs)
        with context.activate():
            observed = host_capabilities.credential_observation("SYNTHETIC_TOKEN")
        assert observed == {"present": None, "selected_reference": reference,
                            "availability": "not_observed", "basis": "explicit_host_selection"}


def test_public_inspection_separates_tool_and_provider_credential_selections(tmp_path, monkeypatch):
    monkeypatch.setattr(host_capabilities, "os", SimpleNamespace(environ={
        "SYNTHETIC_TOKEN": "synthetic-private", "GITHUB_TOKEN": "synthetic-github-private"}))
    declared = tools_manifest()
    declared["tools"][0]["credential_refs"] = ["SYNTHETIC_TOKEN"]
    context = HostContext([tmp_path], ["onboarding.inspect"], "caller",
                          credential_refs={"SYNTHETIC_TOKEN": "capture-capability"})
    with Onboarding.open(allowed_roots=[tmp_path], allow=["inspect"]) as host, context.activate():
        observed = host.inspect(tmp_path / "absent", host_tools=json.dumps(declared).encode())
    tool = observed["host_tools"][0]["credential_presence"][0]
    assert tool["present"] is None and tool["selected_reference"] == "capture-capability"
    github = next(row for row in observed["providers"] if row["id"] == "github")
    credential = github["credentials"]["references"][0]
    assert credential["present"] is None and credential["selected_reference"] is None
    assert "synthetic-private" not in json.dumps(observed)
    assert "synthetic-github-private" not in json.dumps(observed)


def test_public_setup_plan_does_not_claim_an_ambient_secret_is_selected(tmp_path, monkeypatch):
    monkeypatch.setattr(host_capabilities, "os", SimpleNamespace(environ={"SYNTHETIC_TOKEN": "synthetic-private"}))
    value = request(tmp_path)
    value["request"]["payload"]["authority"]["credential_references"] = ["env:SYNTHETIC_TOKEN"]
    context = HostContext([tmp_path], ["onboarding.plan"], "caller", [tmp_path])
    with Onboarding.open(allowed_roots=[tmp_path], allow=["plan"]) as host, context.activate():
        observed = host.plan(value)
    credential = observed["sources"]["credentials"][0]
    assert credential["reference"] == "env:SYNTHETIC_TOKEN"
    assert credential["present"] is None and credential["selected_reference"] is None
    assert credential["access_verified"] is False
    assert "synthetic-private" not in json.dumps(observed)
