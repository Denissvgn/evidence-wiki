"""Carry declared acquisition responsibility through canonical setup owners."""

import copy
import hashlib
import json
import shutil

import pytest
import yaml

from evidence_wiki._pack_io import canonical, capture_pack
from evidence_wiki._script_host import shared_assets_root
from evidence_wiki.errors import UsageError
from evidence_wiki.onboarding_contract import _matches
from evidence_wiki.pack_discovery import owner, snapshot_metadata
from evidence_wiki.planning import check_plan, compile_plan, plan_identity
from evidence_wiki.planning_contracts import REQUEST, decode, normalize, orchestration_schema, schema_document
from evidence_wiki.setup_application import apply_plan
from evidence_wiki.setup_store import snapshot
from tests.test_research_planning import question_plan, reasons, request, web_request
from tests.test_source_capabilities import tools_manifest
from tests.test_workspace_application import in_process as in_process


def selected_request(root, **selection):
    value = request(root)
    value["decisions"]["orchestration"] = {
        "acquisition": "delegated", "acquirer_agent_id": "external-acquirer", **selection,
    }
    return value


def build(value):
    return compile_plan(canonical(value))


@pytest.mark.parametrize("identity", ["agent", "Agent With Spaces", "供給者", " a\t", "\nactor\r", "x" * 160,
                                      " " * 170 + "actor", "", "  ", "a\x00b", "\x00", "a\x7fb", "x" * 161, 1, None])
def test_selector_schema_agrees_with_normalized_runtime_identity(identity):
    config = owner("_orchestration_config")
    selection = {"acquisition": " delegated ", "acquirer_agent_id": identity}
    if config.valid_agent_id(identity):
        _matches(selection, orchestration_schema())
    else:
        with pytest.raises(UsageError):
            _matches(selection, orchestration_schema())


def test_selector_whitespace_matches_owner_including_non_ascii_characters():
    for whitespace in (chr(code) for code in range(0x3100) if chr(code).isspace()):
        for mode in ("providers", "delegated"):
            selection = {"acquisition": whitespace + mode + whitespace}
            if mode == "delegated":
                selection["acquirer_agent_id"] = whitespace + "actor" + whitespace
            _matches(selection, orchestration_schema())
    # ECMAScript's whitespace includes BOM, which Python's strip does not.
    with pytest.raises(UsageError):
        _matches({"acquisition": "\ufeffdelegated", "acquirer_agent_id": "actor"}, orchestration_schema())


@pytest.mark.parametrize("attempts", [1, 2, 10])
def test_attempts_boundaries_and_explicit_provider_mode(tmp_path, attempts):
    value = selected_request(tmp_path, max_attempts_per_request=attempts)
    assert build(value)["initialization"]["effective_config"]["orchestration"]["max_attempts_per_request"] == attempts
    value["decisions"]["orchestration"] = {"acquisition": " providers "}
    assert build(value)["profile"]["workspace_init"]["orchestration"] == {"acquisition": "providers"}


@pytest.mark.parametrize("selection", [{}, [], False, {"acquisition": "other"}, {"acquisition": None},
    {"acquisition": "delegated"}, {"acquisition": "providers", "acquirer_agent_id": "unused"},
    {"acquisition": "providers", "max_attempts_per_request": 2},
    {"acquisition": "delegated", "acquirer_agent_id": "a", "x-extra": True},
    *[{"acquisition": "delegated", "acquirer_agent_id": "a", "max_attempts_per_request": n}
      for n in (None, False, 0, 11, 2.0, "2")]])
def test_malformed_selector_refuses_before_planning_writes(tmp_path, selection):
    value = request(tmp_path)
    value["decisions"]["orchestration"] = selection
    with pytest.raises(UsageError) as caught:
        build(value)
    assert caught.value.error_code == "ONBOARDING_INVALID"
    assert caught.value.details["field"].startswith("/decisions/orchestration")
    assert not list(tmp_path.iterdir())


def test_selector_has_explicit_raw_string_bound(tmp_path):
    value = selected_request(tmp_path, acquirer_agent_id=" " * 4096 + "a")
    with pytest.raises(UsageError) as caught:
        build(value)
    assert caught.value.error_code == "ONBOARDING_LIMIT"
    assert not list(tmp_path.iterdir())


def test_selector_defaults_normalization_and_caller_provenance_are_stable(tmp_path):
    value = selected_request(tmp_path, acquisition=" delegated ", acquirer_agent_id="  供給者  ")
    original = copy.deepcopy(value)
    normalized, basis = normalize(decode(canonical(value)))
    selection = normalized["decisions"]["orchestration"]
    assert selection == {"acquisition": "delegated", "acquirer_agent_id": "供給者", "max_attempts_per_request": 2}
    assert normalize(decode(canonical(normalized)))[0] == normalized
    assert "orchestration" in basis["caller_fields"]
    assert value == original
    implicit = build(request(tmp_path))
    explicit = request(tmp_path)
    explicit["decisions"]["orchestration"] = None
    assert implicit["plan_id"] == build(explicit)["plan_id"]
    assert implicit["request"]["decisions"]["orchestration"] is None
    assert "orchestration" not in implicit["profile"]["workspace_init"]
    assert "orchestration" not in implicit["initialization"]["effective_config"]
    assert build(value)["plan_id"] == build(normalized)["plan_id"]
    assert build(value)["plan_id"] == compile_plan(json.dumps(value, ensure_ascii=False, indent=2).encode())["plan_id"]
    value["decisions"]["orchestration"]["max_attempts_per_request"] = 2
    assert build(value)["plan_id"] == build(normalized)["plan_id"]
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("authorized", [False, True])
@pytest.mark.parametrize("budget", [0, 10000])
@pytest.mark.parametrize("provider", ["arxiv", "unqualified-adapter"])
def test_delegation_conflicts_refuse_before_provider_filtering(tmp_path, authorized, budget, provider):
    value = selected_request(tmp_path)
    value["decisions"]["acquisition"] = [provider]
    payload = value["request"]["payload"]
    if authorized:
        payload["authority"]["allowed_actions"].append("acquisition")
    payload["budgets"].update(bytes=budget, downloads=min(budget, 10), source_requests=min(budget, 10))
    with pytest.raises(UsageError) as caught:
        build(value)
    assert caught.value.details["field"] == "/decisions/acquisition"
    assert not list(tmp_path.iterdir())


def test_delegation_does_not_disable_independent_discovery(tmp_path):
    value = selected_request(tmp_path)
    value["decisions"]["discovery"] = ["arxiv"]
    value["request"]["payload"]["authority"]["allowed_actions"].append("discovery")
    value["request"]["payload"]["budgets"]["bytes"] = 10000
    config = build(value)["initialization"]["effective_config"]
    assert config["integrations"]["discovery"]["enabled"] is True
    assert config["integrations"]["discovery"]["providers"] == ["arxiv"]
    assert config["integrations"]["acquisition"]["enabled"] is False
    assert config["integrations"]["acquisition"]["providers"] == []


def inherited_request(root):
    pack = root / "general-science"
    shutil.copytree(shared_assets_root() / "domain-packs/general-science", pack)
    path = pack / "research.overlay.yml"
    overlay = yaml.safe_load(path.read_text())
    overlay["orchestration"] = {"acquisition": "delegated", "acquirer_agent_id": "pack-acquirer"}
    path.write_text(yaml.safe_dump(overlay, sort_keys=False))
    info = snapshot_metadata(capture_pack(pack))
    value = request(root)
    value["request"]["payload"]["domain"] = {"mode": "domain_pack", "rationale": "Selected guidance.", "pack": {
        "name": info["name"], "version": info["version"], "origin": "caller_local", "locator": str(pack),
        **info["identity"], "research_contract_version": info["compatible_research_yml_contract"]}}
    return value


def test_null_inherits_pack_and_explicit_mode_does_not_delete_incompatible_fields(tmp_path):
    value = inherited_request(tmp_path)
    implicit = build(value)
    assert implicit["initialization"]["effective_config"]["orchestration"]["acquirer_agent_id"] == "pack-acquirer"
    value["decisions"]["orchestration"] = None
    assert build(value)["plan_id"] == implicit["plan_id"]
    value["decisions"]["orchestration"] = {"acquisition": "providers"}
    with pytest.raises(UsageError):
        build(value)
    value["decisions"]["orchestration"] = {"acquisition": "delegated", "acquirer_agent_id": "selected"}
    assert build(value)["initialization"]["effective_config"]["orchestration"]["acquirer_agent_id"] == "selected"
    assert not (tmp_path / "workspace").exists()


@pytest.mark.parametrize("provider", ["arxiv", "unqualified-adapter"])
def test_inherited_delegation_refuses_filtered_provider_choices(tmp_path, provider):
    value = inherited_request(tmp_path)
    value["decisions"]["acquisition"] = [provider]
    value["request"]["payload"]["budgets"].update(bytes=0, downloads=0, source_requests=0)
    with pytest.raises(UsageError) as caught:
        build(value)
    assert caught.value.details["field"] == "/decisions/acquisition"
    assert not (tmp_path / "workspace").exists()


def test_selected_profile_replays_through_standalone_initializer(tmp_path):
    plan = build(selected_request(tmp_path))
    root, profile = tmp_path / "workspace", tmp_path / "profile.yml"
    profile.write_text(yaml.safe_dump(plan["profile"], allow_unicode=True))
    init = owner("init_research_workspace")
    assert init.main(["--profile", str(profile), "--dry-run"]) == 0
    assert not root.exists()
    assert init.main(["--profile", str(profile)]) == 0
    config = yaml.safe_load((root / "research.yml").read_bytes())
    assert config == plan["initialization"]["effective_config"]
    frozen = (root / "docs/research-requirements.json").read_bytes()
    assert json.loads(frozen)["decisions"]["orchestration"] == config["orchestration"]
    assert config["strict_evidence"]["instructions"]["docs/research-requirements.json"] == "sha256:" + hashlib.sha256(frozen).hexdigest()


def test_legacy_request_retains_explicit_mode_with_delegation(tmp_path):
    value = selected_request(tmp_path)
    value["request"]["schema_version"] = "1.0"
    del value["request"]["payload"]["strict_evidence"]
    value["decisions"]["mode"] = "legacy"
    plan = build(value)
    assert plan["strict"]["mode"] == "legacy"
    assert "strict_evidence" not in plan["initialization"]["effective_config"]
    assert plan["profile"]["workspace_init"]["orchestration"]["acquisition"] == "delegated"


def test_selection_is_bound_to_plan_profile_requirements_and_policy(tmp_path):
    value = selected_request(tmp_path)
    text = "  ¿Qué ocurre?\nΔοκιμή 🧪\n第二行  "
    value["request"]["payload"]["questions"][0]["text"] = text
    plan = build(value)
    choice = plan["request"]["decisions"]["orchestration"]
    profile = plan["profile"]["workspace_init"]
    config = plan["initialization"]["effective_config"]
    assert profile["orchestration"] == config["orchestration"] == choice
    frozen = owner("init_research_workspace").frozen_requirements_bytes(profile)
    assert json.loads(frozen)["decisions"]["orchestration"] == choice
    assert config["strict_evidence"]["instructions"]["docs/research-requirements.json"] == "sha256:" + hashlib.sha256(frozen).hexdigest()
    assert plan["questions"]["rows"][0]["original_text"] == text
    assert plan["questions"]["rows"][0]["original_ids"] == ["q1"]
    selected = value["request"]["payload"]["strict_evidence"]
    assert config["strict_evidence"]["policy_id"] == selected["policy_id"]
    assert config["strict_evidence"]["revision"] == selected["policy_revision"]
    assert config["strict_evidence"]["assurance"] == selected["assurance"]
    assert check_plan(canonical(plan))["status"] == "current"
    for selection in ({"acquisition": "providers"}, {"acquisition": "delegated", "acquirer_agent_id": "changed"},
                      {**choice, "max_attempts_per_request": 4}):
        changed = copy.deepcopy(value)
        changed["decisions"]["orchestration"] = selection
        assert build(changed)["plan_id"] != plan["plan_id"]
    for path in ("request", "profile", "initialization"):
        altered = copy.deepcopy(plan)
        target = {"request": altered["request"]["decisions"], "profile": altered["profile"]["workspace_init"],
                  "initialization": altered["initialization"]["effective_config"]}[path]
        target["orchestration"]["acquirer_agent_id"] = "tampered"
        with pytest.raises(UsageError) as caught:
            check_plan(canonical(altered))
        assert caught.value.error_code == "ONBOARDING_PLAN_STALE"
        altered["plan_id"] = plan_identity(altered)
        with pytest.raises(UsageError) as caught:
            check_plan(canonical(altered))
        assert caught.value.error_code == "ONBOARDING_PLAN_STALE"


def test_planned_delegation_applies_replays_and_drives_a_real_order(tmp_path):
    from tests.test_delegated_acquisition_e2e import ACQUIRER, QUESTION_SLUG, DelegatedStructuredSourceTests

    value = selected_request(tmp_path, acquirer_agent_id=ACQUIRER)
    value["request"]["payload"]["questions"][0]["id"] = QUESTION_SLUG
    value["decisions"]["question_plans"] = [question_plan(QUESTION_SLUG)]
    value["decisions"]["raw_roots"] = ["raw/data"]
    plan = build(value)
    result = apply_plan(canonical(plan))
    assert result["setup_ready"] and not result["research_complete"] and not result["claims_verified"]
    root = tmp_path / "workspace"
    before = snapshot(root)
    replay = apply_plan(canonical(plan))
    assert replay["transaction_id"] == result["transaction_id"]
    assert snapshot(root) == before
    config_bytes = (root / "research.yml").read_bytes()
    assert yaml.safe_load(config_bytes) == plan["initialization"]["effective_config"]
    driver = DelegatedStructuredSourceTests()
    request_id = driver.block_question_on_a_request(root)
    assert driver.start(root)["acquirer_agent_id"] == ACQUIRER
    code, order = driver.next_action(root)
    assert code == 0 and order["phase"] == "acquisition"
    assert order["assigned_agent_id"] == ACQUIRER and order["scope"]["request_ids"] == [request_id]
    driver.acquire_csv(root, request_id)
    code, completed = driver.submit(root, order["action_id"], artifacts=[f"raw/data/{driver.CSV_NAME}"])
    assert code == 0 and completed["phase"] == "research"
    assert (root / "research.yml").read_bytes() == config_bytes


@pytest.mark.parametrize("operation", ["initialize", "coverage"])
@pytest.mark.parametrize("moment", ["before", "after"])
def test_delegated_recovery_retains_selection_and_refuses_unowned_effects(tmp_path, monkeypatch, in_process, operation, moment):
    from evidence_wiki import setup_application as app

    plan = build(selected_request(tmp_path))
    class Interrupted(BaseException):
        pass
    def measured(name, *args):
        if name == operation and moment == "before":
            raise Interrupted()
        result = in_process(name, *args)
        if name == operation and moment == "after":
            raise Interrupted()
        return result
    monkeypatch.setattr(app, "measured", measured)
    with pytest.raises(Interrupted):
        apply_plan(canonical(plan))
    root = tmp_path / "workspace"
    before = snapshot(root)
    monkeypatch.setattr(app, "measured", in_process)
    if moment == "before":
        assert apply_plan(canonical(plan))["setup_ready"]
    else:
        with pytest.raises(UsageError) as caught:
            apply_plan(canonical(plan))
        assert caught.value.error_code == "ONBOARDING_OWNERSHIP_CONFLICT"
        assert snapshot(root) == before
    if root.exists():
        assert yaml.safe_load((root / "research.yml").read_bytes())["orchestration"] == plan["initialization"]["effective_config"]["orchestration"]


@pytest.mark.parametrize("changed", ["mode", "acquirer", "attempts", "requirements"])
def test_delegated_apply_refuses_configuration_or_requirement_tampering(tmp_path, in_process, changed):
    plan = build(selected_request(tmp_path))
    assert apply_plan(canonical(plan))["setup_ready"]
    root = tmp_path / "workspace"
    if changed == "requirements":
        (root / "docs/research-requirements.json").write_text('{}')
    else:
        path = root / "research.yml"
        config = yaml.safe_load(path.read_text())
        field, value = {"mode": ("acquisition", "providers"), "acquirer": ("acquirer_agent_id", "tampered"),
                        "attempts": ("max_attempts_per_request", 7)}[changed]
        config["orchestration"][field] = value
        path.write_text(yaml.safe_dump(config))
    before = snapshot(root)
    with pytest.raises(UsageError) as caught:
        apply_plan(canonical(plan))
    assert caught.value.error_code == "ONBOARDING_OWNERSHIP_CONFLICT"
    assert snapshot(root) == before


def test_delegated_receipt_recovery_and_managed_runner_refusal(tmp_path, monkeypatch, in_process, capsys):
    from evidence_wiki import cli, orchestration
    from evidence_wiki.setup_store import SetupStore

    plan = build(selected_request(tmp_path))
    original = SetupStore.write
    def interrupted(self, name, *args, **kwargs):
        if name == "receipt.json":
            raise OSError("interrupted receipt publication")
        return original(self, name, *args, **kwargs)
    monkeypatch.setattr(SetupStore, "write", interrupted)
    with pytest.raises(OSError):
        apply_plan(canonical(plan))
    root = tmp_path / "workspace"
    before = snapshot(root)
    monkeypatch.setattr(SetupStore, "write", original)
    assert apply_plan(canonical(plan))["setup_ready"]
    assert snapshot(root) == before
    monkeypatch.setattr(orchestration, "_runner_executable", lambda *_: pytest.fail("runner was resolved"))
    for operation, extra in (("run", []), ("resume", ["--orchestration-id", "absent"])):
        assert cli.main(["orchestrate", operation, "--target", str(root), "--runner", "codex", *extra]) == orchestration.EXIT_INVALID
        assert "RUNNER_DELEGATED_ACQUISITION_UNSUPPORTED" in capsys.readouterr().err
        assert snapshot(root) == before


def test_delegation_preserves_access_budget_and_review_gaps(tmp_path, monkeypatch):
    value = web_request(tmp_path)
    value["decisions"].update(acquisition=[], orchestration={"acquisition": "delegated", "acquirer_agent_id": "external"})
    baseline = build(value)
    assert "source_route_unavailable" in reasons(baseline)
    assert baseline["setup_ready"] and not baseline["research_ready"] and not baseline["actions_executed"]
    value["decisions"]["host_tools"] = tools_manifest()
    value["decisions"]["source_requirements"][0]["output_format"] = "markdown"
    assert all(row["state"] == "blocked" for row in build(value)["sources"]["routes"])
    payload = value["request"]["payload"]
    payload["authority"]["allowed_actions"].append("host_capture")
    payload["authority"]["source_scope"] = ["https://example.org/study"]
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("planning executed a process"))
    permitted = build(value)
    assert permitted["sources"]["routes"][0]["state"] == "planned_host_action_required"
    assert not permitted["sources"]["source_access_verified"] and not permitted["sources"]["host_delivery_guaranteed"]
    assert permitted["strict"]["effective_assurance"] is None
    assert "verification_authority_not_verified" in reasons(permitted)
    payload["budgets"].update(bytes=0, source_requests=0)
    assert all(row["state"] == "blocked" for row in build(value)["sources"]["routes"])


def test_published_schema_discovery_and_closed_older_decisions(tmp_path, capsys):
    from evidence_wiki import cli, contract

    assert cli.main(["agent", "plan-schemas", "--schema-id", REQUEST]) == 0
    exported = json.loads(capsys.readouterr().out)
    assert exported == schema_document(REQUEST)
    discovered = contract()
    assert discovered["library_api"]["version"] == discovered["onboarding_extensions"]["python_api_version"] == "14"
    assert discovered["research_planning"]["orchestration_selection"]["schema"] == exported["properties"]["decisions"]["properties"]["orchestration"]
    previous = copy.deepcopy(exported)
    del previous["properties"]["decisions"]["properties"]["orchestration"]
    old_request = request(tmp_path)
    _matches(old_request, previous)
    normalized = build(old_request)["request"]
    _matches(normalized, exported)
    with pytest.raises(UsageError):
        _matches(normalized, previous)
    with pytest.raises(UsageError):
        _matches(selected_request(tmp_path), previous)


def test_installed_example_cli_sdk_and_mcp_share_plan_apply_and_replay(tmp_path, capsys, in_process):
    from evidence_wiki import Onboarding, cli
    from evidence_wiki.onboarding_mcp import OnboardingMcpServer

    assert cli.main(["agent", "plan-guide", "--format", "text"]) == 0
    guide = capsys.readouterr().out.split("### Complete delegated setup example", 1)[1]
    value = json.loads(guide.split("```json\n", 1)[1].split("\n```", 1)[0])
    payload = value["request"]["payload"]
    payload["target"]["writable_root"] = str(tmp_path)
    payload["authority"]["writable_roots"] = [str(tmp_path)]
    source, saved = tmp_path / "request.json", tmp_path / "plan.json"
    source.write_bytes(canonical(value))
    assert cli.main(["agent", "plan", "--from-file", str(source), "--output", str(saved)]) == 0
    expected = json.loads(capsys.readouterr().out)
    assert cli.main(["agent", "plan-check", "--from-file", str(saved)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "current"
    with Onboarding.open(allowed_roots=[tmp_path], allow=["apply"]) as host:
        assert host.bootstrap()["payload"]["installation"]["library_api_version"] == "14"
        plan = host.plan(value)
        assert plan == expected and host.check_plan(plan)["status"] == "current"
        server = OnboardingMcpServer(allowed_roots=[tmp_path], allow=["apply"])
        try:
            assert server.call_tool("onboarding_plan", {"value": value}) == plan
            assert server.call_tool("onboarding_check_plan", {"value": plan})["status"] == "current"
            result = host.apply(plan)
            before = snapshot(tmp_path / "workspace")
            assert server.call_tool("onboarding_apply", {"value": plan})["transaction_id"] == result["transaction_id"]
            assert result["setup_ready"] and not result["research_complete"] and not result["claims_verified"]
            assert snapshot(tmp_path / "workspace") == before
            assert yaml.safe_load((tmp_path / "workspace/research.yml").read_bytes()) == plan["initialization"]["effective_config"]
        finally:
            server.close()
