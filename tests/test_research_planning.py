"""Exercise read-only compilation, canonical owners and stale-plan refusal."""

import copy
import hashlib
import json
import subprocess
from pathlib import Path

import pytest
import yaml

from evidence_wiki._pack_io import canonical, capture_pack
from evidence_wiki._script_host import shared_assets_root
from evidence_wiki.cli import main
from evidence_wiki.errors import UsageError
from evidence_wiki.pack_discovery import owner, snapshot_metadata
from evidence_wiki.planning import check_plan, compile_plan
from evidence_wiki.planning_commands import save_plan
from evidence_wiki.planning_contracts import REQUEST
from tests._computation_fixture import aggregation, definition, reference, selector
from tests.test_onboarding_contract import request as base_request
from tests.test_source_capabilities import tools_manifest


def request(root):
    original = base_request()
    original["schema_version"] = "2.0"
    payload = original["payload"]
    payload["target"] = {"writable_root": str(root), "relative_path": "workspace"}
    payload["authority"]["writable_roots"] = [str(root)]
    payload["scope"] = [{"name": "jurisdiction", "value": "Spain"}]
    payload["strict_evidence"] = {"mode": "strict", "assurance": "artifact_checked", "policy_id": "reviewed-evidence", "policy_revision": "1"}
    return {"schema_version": REQUEST, "request": original, "decisions": {"question_plans": [question_plan("q1")]}}


def question_plan(qid):
    return {"question_id": qid, "template": None,
        "facets": [{"facet_id": "primary", "description": "Relevant primary evidence", "required": True,
            "evidence_path": "official_guidance", "source_policy": "official_primary", "freshness_policy": "no_staleness_check",
            "identity_policy": "official_domain_match", "min_sources": 1}],
        "criteria": [{"facet_id": "primary", "source_classes": ["official guidance"], "required_scope": ["jurisdiction"],
            "time": "State the observation date", "units": "Retain source units", "counterevidence": "Retain contrary findings",
            "stopping": "Every required facet has reviewed support or an explicit gap", "inference": "Label inferences and premises", "quantitative": None}]}


def build(value):
    return compile_plan(canonical(value))


def reasons(plan):
    return {row["reason"] for row in plan["blockers"]}


def test_plan_is_read_only_stable_and_canonical_dry_run_accepts(tmp_path, monkeypatch):
    init = owner("init_research_workspace")
    monkeypatch.setattr(init, "safe_registered_ids", lambda *_: pytest.fail("unselected plugin import"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("external process"))
    value = request(tmp_path)
    plan = build(value)
    assert not list(tmp_path.iterdir())
    assert plan["setup_ready"] and not plan["research_ready"]
    assert not plan["actions_executed"] and plan["initialization"]["dry_run"] == "passed"
    assert plan["profile"]["workspace_init"]["questions"] == []
    assert plan["strict"]["require_coverage"] and plan["strict"]["require_grounding"]
    assert plan["strict"]["effective_assurance"] is None
    assert {"trusted_independent_review_required", "verification_authority_not_verified", "facet_evidence_not_observed"} <= reasons(plan)
    again = compile_plan(json.dumps(value, ensure_ascii=False, indent=2).encode())
    assert plan["plan_id"] == again["plan_id"]
    explicit = build(plan["request"])
    assert explicit["plan_id"] == plan["plan_id"]
    assert explicit["decision_basis"] != plan["decision_basis"]
    assert check_plan(canonical(plan))["status"] == "current"


def test_unicode_multiline_and_derived_accounting(tmp_path):
    value = request(tmp_path)
    text = "  ¿Qué ocurre?\nΔοκιμή 🧪\n第二行  "
    value["request"]["payload"]["questions"][0]["text"] = text
    value["request"]["payload"]["derived_questions"] = [{"id": "q2", "text": "Why?", "original_ids": ["q1"]}]
    value["decisions"]["question_plans"].append(question_plan("q2"))
    plan = build(value)
    assert plan["questions"]["original_count"] == 1 and plan["questions"]["derived_count"] == 1
    assert plan["questions"]["rows"][0]["original_text"] == text
    assert plan["questions"]["batch"]["questions"][0]["metadata"]["original_text"] == text
    assert plan["questions"]["original_map"][0]["question_slugs"] == ["q1", "q2"]


def test_duplicate_text_and_missing_material_scope_are_not_dropped(tmp_path):
    value = request(tmp_path)
    payload = value["request"]["payload"]
    payload["questions"].append({"id": "q2", "text": payload["questions"][0]["text"]})
    payload["scope"] = []
    plan = build(value)
    assert plan["questions"]["original_count"] == 2 and len(plan["questions"]["batch"]["questions"]) == 2
    assert not plan["setup_ready"]
    assert {"duplicate_question_text_requires_decision", "required_scope_missing", "material_scope_unspecified", "question_evidence_criteria_missing"} <= reasons(plan)


@pytest.mark.parametrize("mutation", ["duplicate_id", "bad_parent", "scope_duplicate", "extra", "downgrade", "secret", "bad_policy", "bad_computation"])
def test_invalid_intent_or_policy_refused(tmp_path, mutation):
    value = request(tmp_path)
    payload = value["request"]["payload"]
    if mutation == "duplicate_id":
        payload["questions"].append(payload["questions"][0])
    elif mutation == "bad_parent":
        payload["derived_questions"] = [{"id": "q2", "text": "Derived", "original_ids": ["missing"]}]
    elif mutation == "scope_duplicate":
        payload["scope"].append(payload["scope"][0])
    elif mutation == "extra":
        value["decisions"]["approval"] = True
    elif mutation == "downgrade":
        value["decisions"]["mode"] = "legacy"
    elif mutation == "secret":
        value["decisions"]["api_key"] = "must-not-appear"
    elif mutation == "bad_policy":
        value["decisions"]["policy"] = {"enabled": False}
    else:
        value["decisions"]["computation"] = {"version": "1.0", "graphs": {"bad": "eval(1)"}}
    with pytest.raises(UsageError):
        build(value)
    assert not list(tmp_path.iterdir())


def test_explicit_legacy_and_host_assurance_remain_distinct(tmp_path):
    value = request(tmp_path)
    value["request"]["schema_version"] = "1.0"
    del value["request"]["payload"]["strict_evidence"]
    with pytest.raises(UsageError):
        build(value)
    value["decisions"]["mode"] = "legacy"
    legacy = build(value)
    assert legacy["strict"]["mode"] == "legacy"
    assert "strict_evidence" not in legacy["initialization"]["effective_config"]
    strict = request(tmp_path)
    strict["request"]["payload"]["strict_evidence"]["assurance"] = "host_enforced"
    strict["decisions"]["host_reference"] = "caller-host-assertion"
    strict["decisions"]["reviewer_reference"] = "caller-reviewer-assertion"
    result = build(strict)
    assert "protected_host_not_verified" in reasons(result)
    assert result["strict"]["effective_assurance"] is None


def test_owner_limits_produce_precise_blocker(tmp_path):
    value = request(tmp_path)
    value["request"]["payload"]["questions"][0]["text"] = "測" * 500
    result = build(value)
    assert not result["setup_ready"] and "question_exceeds_intake_limit" in reasons(result)
    assert result["questions"]["rows"][0]["original_text"] == "測" * 500


def test_local_absence_content_and_target_drift(tmp_path):
    value = request(tmp_path)
    source = tmp_path / "evidence.csv"
    payload = value["request"]["payload"]
    payload["authority"]["source_scope"] = [str(tmp_path)]
    payload["sources"] = [{"id": "source", "locator": str(source), "kind": "local_file", "question_ids": ["q1"]}]
    value["decisions"]["source_requirements"] = [{"source_id": "source", "output_format": "csv", "needs_complete": True, "scope": {}}]
    result = build(value)
    assert result["bindings"]["inputs"][0]["state"] == "absent"
    source.write_text("value\n1\n")
    with pytest.raises(UsageError, match="planning request refused") as caught:
        check_plan(canonical(result))
    assert caught.value.error_code == "ONBOARDING_PLAN_STALE"
    result = build(value)
    source.write_text("value\n2\n")
    with pytest.raises(UsageError):
        check_plan(canonical(result))
    result = build(value)
    (tmp_path / "workspace").mkdir()
    with pytest.raises(UsageError):
        check_plan(canonical(result))


def test_saved_plan_is_immutable_atomic_and_tampering_is_refused(tmp_path):
    plan = build(request(tmp_path))
    saved = tmp_path / "plan.json"
    save_plan(plan, saved)
    assert json.loads(saved.read_bytes()) == plan
    assert check_plan(saved.read_bytes())["status"] == "current"
    with pytest.raises(UsageError):
        save_plan(plan, saved)
    assert not list(tmp_path.glob(".evidence-plan-*"))
    altered = copy.deepcopy(plan)
    altered["setup_ready"] = False
    with pytest.raises(UsageError) as caught:
        check_plan(canonical(altered))
    assert caught.value.error_code == "ONBOARDING_PLAN_STALE"


def test_cli_schema_guide_and_saved_plan(tmp_path, capsys):
    path = tmp_path / "request.json"
    path.write_bytes(canonical(request(tmp_path)))
    assert main(["agent", "plan", "--from-file", str(path), "--output", str(tmp_path / "plan.json")]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["setup_ready"]
    assert main(["agent", "plan-check", "--from-file", str(tmp_path / "plan.json")]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "current"
    for command in ("plan-guide", "plan-schemas"):
        assert main(["agent", command]) == 0
        assert json.loads(capsys.readouterr().out)


def test_bundled_pack_merge_and_computation_are_inert(tmp_path):
    value = request(tmp_path)
    path = shared_assets_root() / "domain-packs" / "general-science"
    snap = capture_pack(path)
    info = snapshot_metadata(snap)
    value["request"]["payload"]["domain"] = {"mode": "domain_pack", "rationale": "Scientific methods apply", "pack": {
        "name": info["name"], "version": info["version"], "origin": "bundled", "locator": "general-science",
        **info["identity"], "research_contract_version": info["compatible_research_yml_contract"]}}
    definition = yaml.safe_load((shared_assets_root() / "workspace-template/docs/computation-examples/sample-portfolio/research.overlay.yml").read_text())["computation"]
    value["decisions"]["computation"] = definition
    result = build(value)
    assert result["computation"]["definition"] == definition
    assert not result["computation"]["executed"] and result["computation"]["result_id"] is None
    assert result["computation"]["engine_id"].startswith("sha256:")
    assert not list(tmp_path.iterdir())


def test_profile_replays_through_standalone_initializer_and_intake(tmp_path):
    value = request(tmp_path)
    plan = build(value)
    root = tmp_path / "workspace"
    profile = tmp_path / "profile.yml"
    profile.write_text(yaml.safe_dump(plan["profile"], allow_unicode=True))
    init = owner("init_research_workspace")
    assert init.main(["--profile", str(profile), "--dry-run"]) == 0
    assert not root.exists()
    assert init.main(["--profile", str(profile)]) == 0
    config = yaml.safe_load((root / "research.yml").read_text())
    assert config == plan["initialization"]["effective_config"]
    frozen = (root / "docs/research-requirements.json").read_bytes()
    assert config["strict_evidence"]["instructions"]["docs/research-requirements.json"] == "sha256:" + hashlib.sha256(frozen).hexdigest()
    assert json.loads(frozen)["decisions"]["question_plans"] == value["decisions"]["question_plans"]
    report = owner("intake_questions").run_intake_document(root, plan["questions"]["batch"], dry_run=True, from_file_label="planned")
    assert report["counts"]["created"] == 1


def web_request(tmp_path):
    value = request(tmp_path)
    payload = value["request"]["payload"]
    payload["sources"] = [{"id": "web", "kind": "web", "locator": "https://example.org/study", "question_ids": ["q1"]}]
    payload["budgets"].update(bytes=100000, downloads=2)
    value["decisions"].update(acquisition=["web"], discovery=[], allowed_domains=["example.org"],
        source_requirements=[{"source_id": "web", "output_format": "html", "needs_complete": True, "scope": {}}])
    return value


def test_restricted_network_and_separate_phase_authority(tmp_path):
    value = web_request(tmp_path)
    without = build(value)
    assert not without["initialization"]["effective_config"]["integrations"]["acquisition"]["enabled"]
    payload = value["request"]["payload"]
    payload["authority"]["allowed_actions"].append("acquisition")
    payload["authority"]["source_scope"] = ["https://example.org/study"]
    result = build(value)
    route = next(row for row in result["sources"]["routes"] if row["phase"] == "acquisition")
    assert route["state"] == "planned_requires_reinspection" and route["source_request_id"] is None
    assert not result["initialization"]["effective_config"]["integrations"]["discovery"]["enabled"]
    payload["authority"]["source_scope"] = ["https://example.org/another"]
    blocked = build(value)
    assert all(row["state"] == "blocked" for row in blocked["sources"]["routes"])
    assert "source_route_unavailable" in reasons(blocked)
    payload["budgets"]["downloads"] = 0
    assert not build(value)["initialization"]["effective_config"]["integrations"]["acquisition"]["enabled"]


def test_host_declarations_do_not_grant_scope_or_review_authority(tmp_path):
    value = web_request(tmp_path)
    value["decisions"].update(acquisition=[], host_tools=tools_manifest())
    value["decisions"]["source_requirements"][0]["output_format"] = "markdown"
    result = build(value)
    assert all(row["state"] == "blocked" for row in result["sources"]["routes"])
    authority = value["request"]["payload"]["authority"]
    authority["allowed_actions"].append("host_capture")
    authority["source_scope"] = ["https://example.org/study"]
    result = build(value)
    assert result["sources"]["routes"][0]["state"] == "planned_host_action_required"
    assert result["strict"]["effective_assurance"] is None
    assert not result["sources"]["host_delivery_guaranteed"]


@pytest.mark.parametrize("locator", ["https://secret@example.org/study", "https://example.org/study?api_key=secret", "https://example.org/study?token=secret"])
def test_credential_bearing_urls_refused(tmp_path, locator):
    value = web_request(tmp_path)
    value["request"]["payload"]["sources"][0]["locator"] = locator
    with pytest.raises(UsageError):
        build(value)


def test_known_credential_and_cli_diagnostics_are_redacted(tmp_path, monkeypatch, capsys):
    secret = "sensitive-credential-value"
    monkeypatch.setenv("GITHUB_TOKEN", secret)
    value = request(tmp_path)
    value["request"]["payload"]["goal"] = secret
    path = tmp_path / "request.json"
    path.write_bytes(canonical(value))
    assert main(["agent", "plan", "--from-file", str(path)]) == 2
    output = capsys.readouterr().out
    assert secret not in output and "ONBOARDING" in output


def test_unknown_adapters_stay_disabled_and_framework_modes_are_bound(tmp_path):
    value = request(tmp_path)
    value["decisions"].update(acquisition=["unknown-plugin"], discovery=["search"],
        codebase={"provider": "unqualified-analyzer", "question_ids": ["q1"]},
        framework={"id": "pi", "version": "0.0.0", "mode": "rpc"})
    result = build(value)
    integrations = result["initialization"]["effective_config"]["integrations"]
    assert all(integrations[key]["enabled"] is False for key in ("acquisition", "discovery", "codebase_analysis"))
    assert {"provider_adapter_qualification_required", "codebase_adapter_qualification_required", "framework_version_or_mode_unqualified"} <= reasons(result)


def test_coverage_templates_use_owner_validation_and_never_accept_evidence(tmp_path):
    value = request(tmp_path)
    value["decisions"]["question_plans"][0]["template"] = "nonexistent"
    assert "coverage_template_unavailable" in reasons(build(value))
    value["decisions"]["question_plans"][0]["facets"][0]["accepted_source_ids"] = ["pretend-evidence"]
    with pytest.raises(UsageError):
        build(value)
    coverage = owner("coverage_manifest")
    with pytest.raises(coverage.CoverageManifestError):
        coverage.normalize_template_document({"unknown": []})


def test_target_and_saved_output_symlink_boundaries(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "workspace").symlink_to(outside, target_is_directory=True)
    with pytest.raises(UsageError):
        build(request(tmp_path))
    (tmp_path / "workspace").unlink()
    plan = build(request(tmp_path))
    output = tmp_path / "plan.json"
    output.symlink_to(outside / "missing.json")
    with pytest.raises(UsageError):
        save_plan(plan, output)
    assert not list(outside.iterdir())


def test_mid_plan_input_mutation_is_refused(tmp_path, monkeypatch):
    import evidence_wiki.planning as service

    original = service.source_plan
    calls = []
    request_value = request(tmp_path)
    request_value["request"]["payload"]["sources"] = [{"id": "source", "kind": "local_file", "locator": str(tmp_path / "source.csv"), "question_ids": ["q1"]}]
    request_value["request"]["payload"]["authority"]["source_scope"] = [str(tmp_path)]
    def changing(*args, **kwargs):
        value = original(*args, **kwargs)
        calls.append(None)
        if len(calls) == 1:
            (tmp_path / "source.csv").write_text("value\n1\n")
        return value
    monkeypatch.setattr(service, "source_plan", changing)
    with pytest.raises(UsageError) as caught:
        build(request_value)
    assert caught.value.error_code == "ONBOARDING_PLAN_STALE"


def test_quantitative_facets_bind_fields_arithmetic_and_invariants(tmp_path):
    value = request(tmp_path)
    declaration = definition()
    declaration["aggregations"]["observations"] = aggregation()
    declaration["invariants"]["nonnegative"] = {"description": "Values are nonnegative", "target": "records", "selector": selector(),
        "inputs": {}, "filter": None, "assertion": "record.amount >= 0", "severity": "error", "failure_message": "Negative amount"}
    value["decisions"]["computation"] = declaration
    value["request"]["payload"]["sources"] = [{"id": "data", "kind": "local_file", "locator": str(tmp_path / "data.json"), "question_ids": ["q1"]}]
    value["request"]["payload"]["authority"]["source_scope"] = [str(tmp_path)]
    quantitative = {"fields": [{"source_id": "data", "pointer": "/records/0/amount", "unit": "units"}],
                    "references": [reference()], "invariants": ["nonnegative"]}
    value["decisions"]["question_plans"][0]["criteria"][0]["quantitative"] = quantitative
    result = build(value)
    gap = next(row for row in result["blockers"] if row["reason"] == "usable_numeric_evidence_required")
    assert gap["facet_id"] == "primary" and gap["question_ids"] == ["q1"]
    assert result["computation"]["arithmetic"] == declaration["arithmetic"]
    assert result["computation"]["clock"] == declaration["clock"]
    quantitative["references"][0]["kind"] = "unimplemented"
    with pytest.raises(UsageError):
        build(value)


def test_pack_template_produces_executable_owner_template(tmp_path):
    value = request(tmp_path)
    path = shared_assets_root() / "domain-packs/llm-research"
    info = snapshot_metadata(capture_pack(path))
    value["request"]["payload"]["domain"] = {"mode": "domain_pack", "rationale": "Study evaluation evidence", "pack": {
        "name": info["name"], "version": info["version"], "origin": "bundled", "locator": "llm-research",
        **info["identity"], "research_contract_version": info["compatible_research_yml_contract"]}}
    selected = value["decisions"]["question_plans"][0]
    selected.update(template="vendor-product-spec", facets=[], criteria=[])
    result = build(value)
    planned = result["coverage"][0]
    assert planned["template"]["sha256"] == info["coverage_templates"]["vendor-product-spec"]["sha256"]
    template_path = tmp_path / "template.yml"
    template_path.write_text(yaml.safe_dump(planned["template_document"]))
    coverage = owner("coverage_manifest")
    config = result["initialization"]["effective_config"]
    normalized = coverage.load_template(str(template_path), policy_vocabularies=coverage.merged_policy_vocabularies(config))
    manifest = coverage.build_manifest("q1", None, normalized)
    coverage.validate_manifest(manifest, expected_slug="q1", policy_vocabularies=coverage.merged_policy_vocabularies(config))
    assert "pack_required_scope_missing" in reasons(result)


def test_raw_root_diagnostics_and_absent_source_output_collision(tmp_path):
    value = request(tmp_path)
    value["decisions"]["raw_roots"] = ["../outside"]
    with pytest.raises(UsageError) as caught:
        build(value)
    assert caught.value.details["field"] == "/decisions/raw_roots"
    value = request(tmp_path)
    value["request"]["payload"]["sources"] = [{"id": "source", "kind": "local_file", "locator": str(tmp_path / "source.json"), "question_ids": ["q1"]}]
    value["request"]["payload"]["authority"]["source_scope"] = [str(tmp_path)]
    plan = build(value)
    with pytest.raises(UsageError) as caught:
        save_plan(plan, tmp_path / "source.json")
    assert caught.value.details["field"] == "plan_output_collides_with_source_input"
    assert not list(tmp_path.iterdir())


def test_package_code_is_not_a_plan_or_workspace_destination(tmp_path):
    import evidence_wiki.planning as service

    package = Path(service.__file__).parent
    plan = build(request(tmp_path))
    with pytest.raises(UsageError):
        save_plan(plan, package / "unrequested-plan.json")
    value = request(tmp_path)
    value["request"]["payload"]["target"] = {"writable_root": str(package), "relative_path": "unrequested-workspace"}
    with pytest.raises(UsageError):
        build(value)
    assert not (package / "unrequested-plan.json").exists()
    assert not (package / "unrequested-workspace").exists()


def test_blank_material_scope_is_named(tmp_path):
    value = request(tmp_path)
    value["request"]["payload"]["scope"][0]["value"] = "  "
    with pytest.raises(UsageError) as caught:
        build(value)
    assert caught.value.details["field"] == "/request/payload/scope/0/value"


def test_local_delivery_retains_known_byte_budget_gap(tmp_path):
    value = request(tmp_path)
    source = tmp_path / "source.csv"
    source.write_text("amount\n12\n")
    value["request"]["payload"]["sources"] = [{"id": "local", "kind": "local_file", "locator": str(source), "question_ids": ["q1"]}]
    value["request"]["payload"]["authority"]["source_scope"] = [str(tmp_path)]
    result = build(value)
    assert result["sources"]["routes"][0]["state"] == "blocked"
    assert "local_delivery_byte_budget_exceeded" in reasons(result)
    assert not (tmp_path / "workspace").exists()
