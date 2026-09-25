"""Observe scoped source capabilities without confusing declarations with access."""

import base64
import contextlib
import io
import json
from types import SimpleNamespace

import pytest
import yaml

from evidence_wiki import source_probe
from evidence_wiki.cli import main
from evidence_wiki.errors import UsageError
from evidence_wiki.host_capabilities import read_tools, scope_allows
from evidence_wiki.source_delivery import deliver
from evidence_wiki.source_inspection import inspect
from evidence_wiki.source_routing import plan
from tests.test_host_capture import ingest, profile


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "workspace"
    with contextlib.redirect_stdout(io.StringIO()):
        assert main(["init", "--target", str(root), "--project-name", "source-capabilities", "--project-description", "Inspect selected source paths.",
                     "--domain-pack", "general-science"]) == 0
    return root


def tools_manifest():
    return {"schema_version": "evidence-host-tools/v1", "tools": [{"id": "browser", "version": "1", "kind": "browser",
        "operations": ["capture"], "scope": [{"kind": "uri_prefix", "value": "https://example.org/study"}],
        "formats": ["markdown"], "credential_refs": [], "limits": {"max_requests": 2, "max_bytes": 10000, "max_cost_usd": "0"},
        "authorization": "declared", "claims": ["sandbox", "independent_review"], "basis": "declared"}]}


def route_request(**changes):
    requirement = {"id": "study", "question_ids": ["question-one"], "kind": "web", "query_or_identifier": "https://example.org/study",
                   "source_request_id": None,
                   "scope": {}, "output_format": "markdown", "content_kinds": ["primary"], "needs_complete": True, "source_ids": []}
    requirement.update(changes)
    return {"schema_version": "evidence-source-routes/v1", "request_id": "routes", "requirements": [requirement],
            "budget": {"max_requests": 2, "max_bytes": 10000, "max_cost_usd": "0"}, "preferred_tools": ["browser"], "registered_requests": []}


def capture_request(data=b"Retained reflectance observation: 0.74."):
    return {"schema_version": "evidence-host-delivery/v1", "capture": profile(data), "content_base64": base64.b64encode(data).decode()}


def test_inspection_absent_target_does_not_load_plugins_or_execute_tools(tmp_path, monkeypatch):
    def forbidden():
        pytest.fail("Declaration inspection executed a provider")
    entry = SimpleNamespace(name="declared-only", dist=SimpleNamespace(metadata={"Name": "fixture-provider"}, version="1"), load=forbidden)
    monkeypatch.setattr(source_probe.importlib.metadata, "entry_points", lambda group: [entry] if group.endswith("acquisition_providers") else [])
    monkeypatch.setattr(source_probe, "provider_probe", lambda *a, **k: forbidden())
    result = inspect(target=tmp_path / "absent", host_tools=json.dumps(tools_manifest()).encode())
    assert result["target"]["state"] == "absent"
    assert result["strict"]["effective_assurance"] is None
    assert result["host_tools"][0]["host_protection"] == "not_verified"
    row = next(row for row in result["providers"] if row["id"] == "declared-only")
    assert row["supported"] == "unknown" and row["probe"] is None
    assert not (tmp_path / "absent").exists()


def test_credentials_report_presence_without_values(monkeypatch):
    secret = "private-credential-value-never-emit"
    monkeypatch.setenv("GITHUB_TOKEN", secret)
    result = inspect()
    assert secret not in json.dumps(result)
    github = next(row for row in result["providers"] if row["id"] == "github")
    assert github["credentials"]["references"] == [{"name": "GITHUB_TOKEN", "present": True}]


@pytest.mark.parametrize("change", ["command", "proof", "secret", "scope"])
def test_host_declarations_reject_control_and_authority_escalation(change, monkeypatch):
    value = tools_manifest()
    row = value["tools"][0]
    if change == "command":
        row["command"] = ["do-not-execute"]
    elif change == "proof":
        row["basis"] = "demonstrated"
    elif change == "secret":
        monkeypatch.setenv("PRIVATE_TOKEN", "do-not-output-this-value")
        row["version"] = "do-not-output-this-value"
    else:
        row["scope"][0]["value"] = "https://user:password@example.org/"
    with pytest.raises((UsageError, ValueError)):
        read_tools(json.dumps(value).encode())


def test_host_scope_does_not_expand_to_neighbor_origins():
    tool = tools_manifest()["tools"][0]
    assert scope_allows(tool, "https://example.org/study/chapter")
    assert not scope_allows(tool, "https://example.org.evil.invalid/study")
    assert not scope_allows(tool, "https://example.org/study-other")
    assert not scope_allows(tool, "https://example.org/study/../private")
    assert not scope_allows(tool, "https://example.org/study/%2e%2e/private")
    assert not scope_allows(tool, "https://example.org/study/%252e%252e/private")


def test_capture_delivery_is_immutable_replayable_and_usable(workspace):
    raw = json.dumps(capture_request()).encode()
    result = deliver(raw, target=workspace, path="raw/web/capture.md", host_tools=json.dumps(tools_manifest()).encode())
    assert result["status"] == "delivered" and not result["research_ready"]
    assert deliver(raw, target=workspace, path="raw/web/capture.md")["status"] == "already_present"
    with pytest.raises(UsageError):
        deliver(json.dumps(capture_request(b"Changed data.")).encode(), target=workspace, path="raw/web/capture.md")
    record, *_ = ingest(workspace)
    inspected = inspect(target=workspace, source_ids=[record["id"]], host_tools=json.dumps(tools_manifest()).encode())
    assert inspected["sources"][0]["usability"] == "usable", inspected["sources"]
    assert inspected["host_tools"][0]["demonstration"]["capture_ids"] == ["study"]
    assert inspected["host_tools"][0]["access"] == "not_probed"


def test_routes_preserve_scope_format_and_source_progress(workspace):
    delivery = deliver(json.dumps(capture_request()).encode(), target=workspace, path="raw/web/capture.md")
    ingest(workspace)
    value = route_request(source_ids=[delivery["source_id"]])
    result = plan(json.dumps(value).encode(), target=workspace, host_tools=json.dumps(tools_manifest()).encode())
    assert result["requirements"][0]["status"] == "usable_for_caller_review"
    value["requirements"][0]["scope"] = {"jurisdiction": "declared-area"}
    result = plan(json.dumps(value).encode(), target=workspace, host_tools=json.dumps(tools_manifest()).encode())
    local = next(row for row in result["routes"] if row["kind"] == "local_source")
    assert local["state"] == "blocked" and "source_scope_unconfirmed" in local["gaps"]
    assert result["requirements"][0]["status"] == "host_action_required"
    value["requirements"][0]["output_format"] = "csv"
    result = plan(json.dumps(value).encode(), target=workspace, host_tools=json.dumps(tools_manifest()).encode())
    assert all(row["state"] == "blocked" for row in result["routes"] if row["kind"] in {"local_source", "host_delivery"})


def test_registered_kind_declaration_does_not_validate_a_request(workspace, monkeypatch):
    entry = SimpleNamespace(name="fixture-provider", dist=SimpleNamespace(metadata={"Name": "fixture"}, version="1"))
    monkeypatch.setattr(source_probe.importlib.metadata, "entry_points", lambda group: [entry] if group.endswith("acquisition_providers") else [])
    config_path = workspace / "research.yml"
    config = yaml.safe_load(config_path.read_text())
    config["integrations"]["acquisition"] = {"enabled": True, "providers": ["fixture-provider"]}
    config_path.write_text(yaml.safe_dump(config))
    value = route_request(output_format="csv")
    value["preferred_tools"] = []
    value["registered_requests"] = [{"requirement_id": "study", "phase": "acquisition", "provider_id": "fixture-provider",
                                      "request": {"symbol": "EXAMPLE"}, "request_path": None, "registration": "fixture/fixture-provider"}]
    result = plan(json.dumps(value).encode(), target=workspace)
    row = next(row for row in result["routes"] if row["kind"] == "registered_provider")
    assert "registered_request_not_validated" in row["gaps"] and row["state"] == "blocked"


def test_selected_source_inspection_does_not_normalize_or_touch_other_sources(workspace):
    delivery = deliver(json.dumps(capture_request()).encode(), target=workspace, path="raw/web/capture.md")
    ingest(workspace)
    unrelated = workspace / "raw/web/unrelated.bin"
    unrelated.write_bytes(b"\x00\x01unrelated")
    before = {str(path.relative_to(workspace)): path.read_bytes() for path in workspace.rglob('*') if path.is_file()}
    result = inspect(target=workspace, source_ids=[delivery["source_id"]])
    assert len(result["sources"]) == 1 and result["sources"][0]["usability"] == "usable"
    assert before == {str(path.relative_to(workspace)): path.read_bytes() for path in workspace.rglob('*') if path.is_file()}


def test_routing_budgets_and_unusable_sources_do_not_become_ready(workspace):
    value = capture_request()
    value["capture"]["content_kind"] = "generated_summary"
    delivery = deliver(json.dumps(value).encode(), target=workspace, path="raw/web/capture.md")
    ingest(workspace)
    result = inspect(target=workspace, source_ids=[delivery["source_id"]])
    assert result["sources"][0]["usability"] == "not_ready"
    request = route_request()
    request["budget"]["max_requests"] = 0
    result = plan(json.dumps(request).encode(), target=workspace, host_tools=json.dumps(tools_manifest()).encode())
    assert next(row for row in result["routes"] if row["kind"] == "host_delivery")["state"] == "blocked"


def test_cli_refusals_remain_one_redacted_document(capsys):
    assert main(["agent", "inspect", "--unexpected-private-value"]) == 2
    result = capsys.readouterr()
    assert not result.err and "unexpected-private-value" not in result.out
    assert json.loads(result.out)["error_code"] == "ONBOARDING_INVALID"
    assert main(["agent", "source-status", "--target", "."]) == 2
    assert json.loads(capsys.readouterr().out)["details"]["field"] == "source_status_requires_selection"


def test_configured_manifest_location_is_authoritative(workspace):
    delivery = deliver(json.dumps(capture_request()).encode(), target=workspace, path="raw/web/capture.md")
    ingest(workspace)
    manifest = workspace / "sources/manifest.jsonl"
    moved = workspace / "sources/selected-manifest.jsonl"
    manifest.rename(moved)
    config_path = workspace / "research.yml"
    config = yaml.safe_load(config_path.read_text())
    config["sources"]["manifest_path"] = "sources/selected-manifest.jsonl"
    config_path.write_text(yaml.safe_dump(config))
    result = inspect(target=workspace, source_ids=[delivery["source_id"]])
    # The existing record still names its original manifest and must be corrected by its owner.
    assert result["sources"][0]["inventory"] == "recorded"
    assert result["observation"]["files_observed"] > 2


def test_capture_digest_and_linked_destination_refuse_before_publication(workspace, tmp_path):
    value = capture_request()
    value["capture"]["content_sha256"] = "sha256:" + "0" * 64
    with pytest.raises(UsageError):
        deliver(json.dumps(value).encode(), target=workspace, path="raw/web/capture.md")
    assert not (workspace / "raw/web/capture.md").exists()
    external = tmp_path / "outside"
    external.mkdir()
    (workspace / "raw/web/redirect").symlink_to(external, target_is_directory=True)
    with pytest.raises(OSError):
        deliver(json.dumps(capture_request()).encode(), target=workspace, path="raw/web/redirect/capture.md")
    assert list(external.iterdir()) == []


def test_incompatible_workspace_contract_blocks_execution_routes(workspace):
    marker = workspace / "workspace-system.yml"
    value = yaml.safe_load(marker.read_text())
    value["workspace_system"]["compatible_research_yml_contract"] = "999"
    marker.write_text(yaml.safe_dump(value))
    result = plan(json.dumps(route_request()).encode(), target=workspace, host_tools=json.dumps(tools_manifest()).encode())
    assert result["blocked"] == ["study"]
    assert all("workspace_contract_incompatible" in row["gaps"] for row in result["routes"])


def test_web_and_academic_routes_use_real_configuration_and_request_bindings(workspace):
    config_path = workspace / "research.yml"
    config = yaml.safe_load(config_path.read_text())
    config["integrations"]["acquisition"] = {"enabled": True, "providers": ["web"], "web": {"allowed_domains": ["example.org"]}}
    config["integrations"]["discovery"] = {"enabled": True, "providers": ["arxiv"]}
    config_path.write_text(yaml.safe_dump(config))
    value = route_request(output_format="html")
    value["preferred_tools"] = []
    result = plan(json.dumps(value).encode(), target=workspace)
    web = next(row for row in result["routes"] if row.get("provider") == "web")
    assert web["state"] == "ready_to_attempt", web
    assert "--request-id" not in web["command_argv"]
    value["requirements"][0].update(kind="paper", query_or_identifier="study methods", source_request_id="req-current", output_format="pdf")
    (workspace / "sources/source-requests.jsonl").write_text(json.dumps({"request_id": "req-current", "kind": "paper", "status": "open"}) + "\n")
    result = plan(json.dumps(value).encode(), target=workspace)
    academic = next(row for row in result["routes"] if row.get("operation") == "academic" and row["provider"] == "arxiv")
    assert academic["state"] == "ready_to_attempt", academic
    assert "req-current" in academic["command_argv"]


def test_unsupported_scope_keys_cannot_disappear_during_matching(workspace):
    value = route_request(scope={"INVALID SCOPE": "must not disappear"})
    with pytest.raises(UsageError) as error:
        plan(json.dumps(value).encode(), target=workspace, host_tools=json.dumps(tools_manifest()).encode())
    assert error.value.details["field"] == "source_scope_invalid"
