"""Qualify authored guidance without turning structural/case success into authority."""

import json
from pathlib import Path

import pytest
import yaml

from evidence_wiki._pack_io import canonical, capture_pack
from evidence_wiki.cli import main
from evidence_wiki.errors import UsageError
from evidence_wiki.pack_acceptance import accept, resume
from evidence_wiki.pack_assessment import assess, freeze_cases, references
from evidence_wiki.pack_authoring import derive, scaffold
from evidence_wiki.pack_authoring_contracts import DERIVE, OBSERVATIONS, SPEC, SUITE, digest
from evidence_wiki.pack_authoring_store import load_draft
from evidence_wiki.pack_catalog import entries, initialize
from evidence_wiki.pack_discovery import owner
from evidence_wiki.pack_qualification import qualify_draft
from evidence_wiki.planning import check_plan, compile_plan
from tests.test_pack_discovery import decision as pack_decision
from tests.test_research_planning import request as research_request


def specification(name="soil-observations"):
    policy = "pack:" + name + "/region-match"
    return {"schema_version": SPEC, "name": name, "version": "0.1.0", "description": "Guidance for bounded soil observations",
        "scope": "Compare explicitly scoped observations", "exclusions": ["No universal agronomy conclusions"], "intended_users": ["Researchers"],
        "question_classes": ["Which observations apply to the selected region?"], "source_classes": ["measurement report"],
        "required_scope_inputs": [{"id": "region", "description": "Region to compare"}], "review_requirements": ["Review source suitability and region meaning"],
        "human_gated": True, "taxonomy": {"observations": {"page_type": "observation", "description": "Scoped observations"}},
        "claim_types": ["factual"], "claim_fields": [{"name": "region", "type": "string", "required": False, "description": "Observed region"}],
        "extraction_targets": ["region", "measurement", "units"], "filing_rules": ["Retain region and observation dates"], "outputs": ["Comparison table"],
        "policies": {"identity_policy": {policy: "Region equals the caller-selected region"}},
        "policy_rules": {policy: {"all_of": [{"equals": {"field": "record/region", "question_field": "metadata/region"}}], "manual_review_required": True}},
        "request_kinds": [{"id": "pack:" + name + "/measurement", "label": "Measurement report", "description": "Retained scoped measurement"}],
        "scaffolds": {"observation": "# Observation\n\nRetain region, units, evidence and limitations.\n"},
        "coverage_templates": {"scoped-observation": {"coverage_profile": "scoped-observation", "required_facets": [
            {"facet_id": "region", "description": "Applicable measurement", "evidence_path": "official_guidance", "source_policy": "official_primary",
             "freshness_policy": "no_staleness_check", "identity_policy": policy, "min_sources": 1}], "optional_facets": []}},
        "recommended_providers": {"discovery": [], "acquisition": []}, "computation": None,
        "requirements": [{"id": "region", "text": "Require matching region while keeping meaning review manual", "meaning": "policy"}], "unresolved": []}


def cases(root):
    _, _, draft = load_draft(root)
    rows = []
    for scenario, structured, question, outcome in [
        ("adequate", {"region": "north"}, {"metadata": {"region": "north"}}, "pass"),
        ("missing", {}, {"metadata": {"region": "north"}}, "fail"),
        ("conflicting", {"region": "south"}, {"metadata": {"region": "north"}}, "fail"),
        ("wrong_scope", {"region": "north"}, {"metadata": {"region": "east"}}, "fail"),
    ]:
        rows.append({"id": scenario, "requirement_ids": ["region"], "scenario": scenario, "kind": "policy", "target": "pack:" + draft["name"] + "/region-match",
            "inputs": {"structured": structured, "question": question, "provenance": {}, "origin_host": None, "provider_ids": [], "as_of": "2026-09-22T12:00:00Z"},
            "expected": {"status": "observed", "outcome": outcome, "human_review_required": True}, "rationale": "Independently specified region equality and missing-data outcome"})
    return {"schema_version": SUITE, "draft_id": digest(draft), "cases": rows, "exceptions": [], "limitations": ["Synthetic observations only"]}


def prepared(tmp_path):
    root = tmp_path / "draft"
    created = scaffold(canonical(specification()), output=root)
    freeze_cases(root, canonical(cases(root)))
    return root, created


def test_scaffold_is_deterministic_and_records_are_outside_pack(tmp_path):
    first = scaffold(canonical(specification()), output=tmp_path / "first")
    second = scaffold(canonical(specification()), output=tmp_path / "second")
    assert first["identity"] == second["identity"] and first["draft_id"] == second["draft_id"]
    candidate = Path(first["candidate"])
    assert {"README.md", "taxonomy.md", "claims.md", "research.overlay.yml"} <= {p.name for p in candidate.iterdir()}
    assert not (candidate / "records").exists() and not list(candidate.rglob("*.py"))
    result = qualify_draft(tmp_path / "first")["validation"]
    assert result["ok"] and result["semantic_adequacy"] == "not_evaluated"
    assert any(row["id"] == "smoke_validation" and row["status"] == "pass" for row in result["checks"])
    with pytest.raises(UsageError):
        scaffold(canonical(specification()), output=tmp_path / "first")


def test_frozen_cases_and_arithmetic_references_do_not_certify_domain(tmp_path):
    root, created = prepared(tmp_path)
    assessment = assess(root)["assessment"]
    assert not assessment["gaps"] and assessment["mechanical_cases_passed"]
    assert assessment["semantic_adequacy"] == "not_certified" and assessment["independent_review"] == "not_verified"
    assert all(row["passed"] for row in assessment["reference_basis"]["arithmetic_observations"])
    assert len(assessment["reference_basis"]["arithmetic_observations"]) == 3
    assert "unresolved-conflict" in references()["semantic_case_ids"]
    assert capture_pack(Path(created["candidate"])).tree_sha256 == created["identity"]["tree_sha256"]
    changed = cases(root)
    changed["cases"][1]["expected"]["outcome"] = "pass"
    with pytest.raises(UsageError):
        freeze_cases(root, canonical(changed))
    assert assess(root)["assessment"] == assessment


def test_catalog_acceptance_and_resume_keep_review_pending(tmp_path):
    root, created = prepared(tmp_path)
    result = assess(root)
    catalog = tmp_path / "catalog"
    initialize(catalog, {"drafts": str(tmp_path)})
    accepted = accept(root, assessment_id=result["record"]["sha256"], catalog=catalog, root_id="drafts", revision="soil-one", scope="Regional measurement guidance")
    assert accepted["semantic_adequacy"] == "not_certified"
    row = entries(catalog)[0]
    assert row["state"] == "available" and row["assessment"]["independent_review"] == "not_verified"
    request = research_request(tmp_path)
    request["request"]["payload"]["scope"].append({"name": "region", "value": "north"})
    plan = resume(canonical(request), catalog=catalog, revision="soil-one")
    assert plan["setup_ready"] and not plan["research_ready"]
    assert plan["bindings"]["accepted_pack"]["assessment_sha256"] == result["record"]["sha256"]
    assert "local_pack_domain_review_not_verified" in {gap["reason"] for gap in plan["blockers"]}
    assert not (tmp_path / "workspace").exists()
    (Path(created["candidate"]) / "taxonomy.md").write_text("Changed guidance")
    assert entries(catalog)[0]["state"] == "mutated"
    with pytest.raises(UsageError):
        resume(canonical(request), catalog=catalog, revision="soil-one")


def test_specialization_preserves_base_and_renames_all_policy_namespaces(tmp_path):
    root, created = prepared(tmp_path)
    source = Path(created["candidate"])
    before = capture_pack(source)
    value = {"schema_version": DERIVE, "base": {"selector": None, "path": str(source), "target": None, "catalog": None, "tree_sha256": before.tree_sha256},
        "mode": "specialization", "name": "coastal-soil", "version": "0.1.0", "rationale": "New reusable coastal scope requires review",
        "changes": [], "requirements": specification()["requirements"], "unresolved": []}
    result = derive(canonical(value), output=tmp_path / "fork")
    after = capture_pack(Path(result["candidate"]))
    assert all(b"pack:soil-observations/" not in raw for raw in after.files.values())
    assert b"pack:coastal-soil/" in after.files["research.overlay.yml"]
    assert capture_pack(source).files == before.files
    assert qualify_draft(tmp_path / "fork")["validation"]["ok"]
    assert load_draft(tmp_path / "fork")[2]["classification"]["routine_repair"] is False


def test_failed_case_or_changed_candidate_cannot_be_accepted(tmp_path):
    root, created = prepared(tmp_path)
    first = assess(root)
    path = Path(created["candidate"]) / "research.overlay.yml"
    overlay = yaml.safe_load(path.read_text())
    overlay["domain_pack"]["policy_rules"]["pack:soil-observations/region-match"]["all_of"] = [{"equals": {"field": "record/region", "value": "south"}}]
    path.write_text(yaml.safe_dump(overlay))
    changed = assess(root)["assessment"]
    assert changed["gaps"] and not changed["changes"]["routine_repair"]
    catalog = tmp_path / "catalog"
    initialize(catalog, {"drafts": str(tmp_path)})
    with pytest.raises(UsageError):
        accept(root, assessment_id=first["record"]["sha256"], catalog=catalog, root_id="drafts", revision="soil-one", scope="Scope")
    assert entries(catalog) == []


def test_cli_scaffold_and_qualification(tmp_path, capsys):
    source = tmp_path / "spec.json"
    source.write_bytes(canonical(specification()))
    assert main(["pack", "scaffold", "--from-file", str(source), "--output", str(tmp_path / "draft")]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "draft_created"
    assert main(["pack", "qualify", "--draft", str(tmp_path / "draft")]) == 0
    assert json.loads(capsys.readouterr().out)["validation"]["ok"]
    assert main(["pack", "guide", "--topic", "authoring"]) == 0
    assert "Author and qualify" in json.loads(capsys.readouterr().out)["content"]


@pytest.mark.parametrize("mutation", ["foreign_policy", "foreign_kind", "bad_rule", "evidence_in_template", "duplicate_facet", "bad_path", "executable_scaffold"])
def test_bad_spec_is_refused_before_creating_output(tmp_path, mutation):
    value = specification()
    if mutation == "foreign_policy":
        value["policies"]["identity_policy"]["pack:another/foreign"] = "Foreign"
    elif mutation == "foreign_kind":
        value["request_kinds"][0]["id"] = "pack:another/foreign"
    elif mutation == "bad_rule":
        value["policy_rules"]["pack:soil-observations/region-match"]["all_of"] = [{"run_code": "pass"}]
    elif mutation == "evidence_in_template":
        value["coverage_templates"]["scoped-observation"]["required_facets"][0]["accepted_source_ids"] = ["invented"]
    elif mutation == "duplicate_facet":
        value["coverage_templates"]["scoped-observation"]["required_facets"] *= 2
    elif mutation == "bad_path":
        value["scaffolds"]["../outside"] = "Bad path"
    else:
        value["scaffolds"]["code.py"] = "print('data')"
    with pytest.raises((UsageError, ValueError, SystemExit, owner("_request_kinds").RequestKindError, owner("_policy_primitives").PolicyRuleError)):
        scaffold(canonical(value), output=tmp_path / "draft")
    assert not list(tmp_path.iterdir())


def test_qualified_observation_preserves_canonical_failure_categories(tmp_path):
    root, created = prepared(tmp_path)
    path = Path(created["candidate"]) / "research.overlay.yml"
    overlay = yaml.safe_load(path.read_text())
    overlay["domain_pack"]["compatible_research_yml_contract"] = "unsupported"
    path.write_text(yaml.safe_dump(overlay))
    result = qualify_draft(root)["validation"]
    assert not result["ok"]
    assert "contract_compatibility" in {row["id"] for row in result["checks"] if row["status"] == "fail"}
    assert result["identity"]["tree_sha256"] == capture_pack(path.parent).tree_sha256


def test_unknown_guidance_and_missing_cases_stay_gaps(tmp_path):
    value = specification()
    value["unresolved"] = ["Need an independent agronomy scope review"]
    scaffold(canonical(value), output=tmp_path / "draft")
    suite = cases(tmp_path / "draft")
    suite["cases"] = suite["cases"][:1]
    assert freeze_cases(tmp_path / "draft", canonical(suite))["gaps"]
    result = assess(tmp_path / "draft")["assessment"]
    assert {"unresolved_guidance", "required_case_missing"} <= {row["reason"] for row in result["gaps"]}


def test_semantic_observations_reuse_frozen_rubric_without_approval(tmp_path):
    root = tmp_path / "draft"
    scaffold(canonical(specification()), output=root)
    suite = cases(root)
    from evidence_wiki.pack_assessment import reference_data
    from evidence_wiki.pack_discovery import owner

    reference_cases = {case["id"]: case for case in reference_data()["semantic"]["cases"]}
    observations = []
    for case, reference in zip(suite["cases"], ("supported-scope", "insufficient-evidence", "unresolved-conflict", "irrelevant-comparison"), strict=True):
        verdicts = {**dict.fromkeys(owner("_strict_contract").CHECKS, "pass"), **reference_cases[reference]["verdicts"]}
        case.update(kind="semantic", target=None, inputs={"reference_case_id": reference}, expected={"status": "declared", "verdicts": verdicts})
        observations.append({"case_id": case["id"], "verdicts": verdicts, "rationale": "Recorded reference judgment with synthetic limits",
            "reviewer_reference": "declared-reviewer", "basis": "caller_declared"})
    freeze_cases(root, canonical(suite))
    assert assess(root)["assessment"]["gaps"]
    value = {"schema_version": OBSERVATIONS, "suite_sha256": digest(suite), "observations": observations}
    result = assess(root, observations=canonical(value))["assessment"]
    assert not result["gaps"] and result["mechanical_cases_passed"] is None
    assert result["mechanical_case_count"] == 0 and result["independent_review"] == "not_verified"
    assert not result["human_gates_removed"] and result["semantic_adequacy"] == "not_certified"


def test_fractional_case_inputs_require_lossless_encoding(tmp_path):
    root, _ = prepared(tmp_path)
    suite = cases(root)
    suite["cases"][0]["inputs"]["structured"]["amount"] = 0.1
    with pytest.raises(UsageError) as caught:
        freeze_cases(root, canonical(suite))
    assert caught.value.details["field"] == "authoring_case_decimal_requires_lossless_encoding"


def test_unavailable_capability_cannot_become_a_successful_expected_case(tmp_path):
    root = tmp_path / "draft"
    scaffold(canonical(specification()), output=root)
    suite = cases(root)
    suite["cases"][1].update(target="pack:soil-observations/unknown", expected={"status": "unsupported", "reason": "policy_has_no_mechanical_rule"})
    freeze_cases(root, canonical(suite))
    result = assess(root)["assessment"]
    assert not result["mechanical_cases_passed"]
    assert any(row["reason"] == "case_capability_unavailable" for row in result["gaps"])


def test_partial_output_and_symlink_boundaries_are_preserved(tmp_path, monkeypatch):
    import evidence_wiki.pack_authoring_store as store

    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(UsageError):
        scaffold(canonical(specification()), output=tmp_path / "linked")
    assert not list(outside.iterdir())
    original = store.publish
    def fail(directory, name, raw):
        if name == "draft.json":
            raise OSError("injected publication failure")
        return original(directory, name, raw)
    monkeypatch.setattr(store, "publish", fail)
    with pytest.raises(OSError):
        scaffold(canonical(specification()), output=tmp_path / "partial")
    assert (tmp_path / "partial/packs/soil-observations/research.overlay.yml").is_file()
    assert not (tmp_path / "partial/records/draft.json").exists()
    with pytest.raises(UsageError):
        load_draft(tmp_path / "partial")


def test_replaced_output_directory_is_not_followed(tmp_path, monkeypatch):
    import evidence_wiki.pack_authoring_store as store

    output, moved, outside = tmp_path / "draft", tmp_path / "moved", tmp_path / "outside"
    outside.mkdir()
    original = store.publish
    swapped = []
    def replace(directory, name, raw):
        if name == "specification.json" and not swapped:
            output.rename(moved)
            output.symlink_to(outside, target_is_directory=True)
            swapped.append(True)
        return original(directory, name, raw)
    monkeypatch.setattr(store, "publish", replace)
    with pytest.raises(UsageError):
        scaffold(canonical(specification()), output=output)
    assert not list(outside.iterdir())


def test_stale_checker_or_assessment_cannot_be_selected(tmp_path, monkeypatch):
    import evidence_wiki.pack_discovery as discovery

    root, _ = prepared(tmp_path)
    result = assess(root)
    catalog = tmp_path / "catalog"
    initialize(catalog, {"drafts": str(tmp_path)})
    accept(root, assessment_id=result["record"]["sha256"], catalog=catalog, root_id="drafts", revision="soil", scope="Regional research")
    monkeypatch.setattr(discovery, "checker_identity", lambda: "0" * 64)
    assert entries(catalog)[0]["state"] == "assessment_stale"
    with pytest.raises(UsageError):
        resume(canonical(research_request(tmp_path)), catalog=catalog, revision="soil")


def test_missing_assessment_record_is_not_an_available_local_revision(tmp_path):
    root, _ = prepared(tmp_path)
    result = assess(root)
    catalog = tmp_path / "catalog"
    initialize(catalog, {"drafts": str(tmp_path)})
    accept(root, assessment_id=result["record"]["sha256"], catalog=catalog, root_id="drafts", revision="soil", scope="Regional research")
    (catalog / ("assessment-" + result["record"]["sha256"] + ".json")).unlink()
    assert entries(catalog)[0]["state"] == "unavailable"


def test_planning_only_authors_for_an_explicit_mapped_guidance_gap(tmp_path, monkeypatch):
    value = research_request(tmp_path)
    assert compile_plan(canonical(value))["authoring_action"] is None
    decision = pack_decision()
    decision.update(choice="create", selections=[], gaps=[{"requirement_id": "region", "kind": "guidance", "detail": "Reusable regional criteria are missing"}])
    decision["requirements"] = [{"id": "region", "text": "Specify regional criteria", "kind": "scope"}]
    decision["mapping"] = [{"requirement_id": "region", "support": "gap", "pack_basis": [], "rationale": "Missing reusable guidance"}]
    value["decisions"]["pack_authoring"] = {"decision": decision, "specification": specification(), "derivation": None}
    import evidence_wiki.pack_discovery as discovery

    monkeypatch.setattr(discovery, "validate_snapshot", lambda *_a, **_k: pytest.fail("planning executed canonical pack validation"))
    planned = compile_plan(canonical(value))
    assert planned["authoring_action"]["state"] == "ready_for_explicit_authoring" and not planned["setup_ready"]
    assert not list(tmp_path.iterdir())
    assert check_plan(canonical(planned))["status"] == "current"
    decision["gaps"][0]["kind"] = "source"
    blocked = compile_plan(canonical(value))
    assert blocked["authoring_action"]["state"] == "unsupported"
    assert "guidance_gap_required_for_pack_authoring" in {row["reason"] for row in blocked["blockers"]}


def test_package_destinations_and_candidate_symlinks_are_refused(tmp_path):
    import evidence_wiki.pack_authoring as authoring

    protected = Path(authoring.__file__).parent / "unexpected-draft"
    with pytest.raises(UsageError):
        scaffold(canonical(specification()), output=protected)
    assert not protected.exists()
    root, created = prepared(tmp_path)
    candidate = Path(created["candidate"])
    original = candidate / "taxonomy.md"
    original.unlink()
    original.symlink_to(tmp_path / "outside.md")
    with pytest.raises(UsageError):
        qualify_draft(root)


def test_foreign_manual_policy_is_rejected_by_canonical_owner():
    coverage = owner("coverage_manifest")
    config = {"domain_pack": {"name": "own-pack", "policy_vocabularies": {"source_policy": {"pack:foreign/manual": "Needs review"}}}}
    with pytest.raises(coverage.CoverageManifestError):
        coverage.domain_pack_policy_vocabularies(config)


def test_core_claim_types_and_required_fields_cannot_be_weakened(tmp_path):
    value = specification()
    value["claim_fields"].append({"name": "source_ids", "type": "string", "required": False, "description": "Invalid type/requirement change"})
    with pytest.raises(UsageError):
        scaffold(canonical(value), output=tmp_path / "bad")
    assert not list(tmp_path.iterdir())


def test_current_base_digest_and_revision_identity_are_required(tmp_path):
    root, created = prepared(tmp_path)
    overlay_path = Path(created["candidate"]) / "research.overlay.yml"
    overlay_path.write_text("# Preserve the interpretation note.\n" + overlay_path.read_text())
    created["identity"]["tree_sha256"] = capture_pack(Path(created["candidate"])).tree_sha256
    base = {"selector": None, "path": created["candidate"], "target": None, "catalog": None, "tree_sha256": created["identity"]["tree_sha256"]}
    value = {"schema_version": DERIVE, "base": base, "mode": "revision", "name": "soil-observations", "version": "0.2.0",
        "rationale": "Retain criteria while revising guidance", "changes": [], "requirements": specification()["requirements"], "unresolved": []}
    result = derive(canonical(value), output=tmp_path / "revision")
    assert (Path(result["candidate"]) / "research.overlay.yml").read_text().startswith("# Preserve the interpretation note.\n")
    assert load_draft(tmp_path / "revision")[2]["base"]["tree_sha256"] == base["tree_sha256"]
    assert result["identity"] != created["identity"]
    base["tree_sha256"] = "0" * 64
    with pytest.raises(UsageError):
        derive(canonical(value), output=tmp_path / "stale")
    assert not (tmp_path / "stale").exists()


def test_mutation_during_canonical_validation_invalidates_observation(tmp_path, monkeypatch):
    from evidence_wiki import domain_pack_validator

    root, created = prepared(tmp_path)
    original = domain_pack_validator.validate_domain_pack
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        (Path(created["candidate"]) / "taxonomy.md").write_text("Changed during validation")
        return result
    monkeypatch.setattr(domain_pack_validator, "validate_domain_pack", changed)
    with pytest.raises(UsageError) as caught:
        qualify_draft(root)
    assert caught.value.error_code == "ONBOARDING_PLAN_STALE"
    assert not list((root / "records").glob("validation-*.json"))


def test_known_secret_is_refused_before_destination_creation(tmp_path, monkeypatch):
    secret = "private-authoring-credential"
    monkeypatch.setenv("GITHUB_TOKEN", secret)
    with pytest.raises(UsageError):
        scaffold(canonical(specification()), output=tmp_path / secret)
    assert not list(tmp_path.iterdir())


def test_parent_alias_resolves_to_the_held_output_directory(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    created = scaffold(canonical(specification()), output=alias / "draft")
    assert Path(created["root"]) == actual / "draft"
    assert load_draft(alias / "draft")[0] == actual / "draft"


def test_new_directory_identity_must_match_the_held_descriptor(tmp_path, monkeypatch):
    import evidence_wiki.pack_authoring_store as store

    original = store.identity
    def changed(path):
        value = original(path)
        if path.name == "draft":
            value["inode"] = "0"
        return value
    monkeypatch.setattr(store, "identity", changed)
    with pytest.raises(UsageError) as caught:
        scaffold(canonical(specification()), output=tmp_path / "draft")
    assert caught.value.error_code == "ONBOARDING_PLAN_STALE"
    assert not (tmp_path / "draft/records/draft.json").exists()
