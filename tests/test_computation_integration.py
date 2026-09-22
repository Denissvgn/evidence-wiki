"""Public operation parity and shared release-gate integration."""

import contextlib
import copy
import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from evidence_wiki.cli import main
from evidence_wiki.computation import evaluate, execute
from evidence_wiki.errors import EvidenceWikiError
from tests._computation_fixture import definition, workspace
from tests._script_loader import load_isolated_module
from tests.test_orchestration_contract_schemas import assert_matches_schema
from tests.test_strict_evidence import CORE as STRICT
from tests.test_strict_evidence import host as host
from tests.test_strict_evidence import review, save_claims

ROOT = Path(__file__).resolve().parents[1]
SERVICE = load_isolated_module("computation_integration", ROOT / "workspace-template/scripts/_computation_service.py")


def constant_definition():
    data = definition()
    data["graphs"]["worksheet"] = {"description": "Declared arithmetic", "constants": {"amount": {"value": "3", "unit": "units"}},
        "inputs": {}, "nodes": {"total": {"expr": "amount * 2", "unit": "units"}},
        "output_mapping": {"total": "total"}, "output_page": None}
    return data


def test_python_cli_and_copied_scripts_return_the_same_result(tmp_path):
    workspace(tmp_path)
    scripts = tmp_path / "scripts"
    shutil.copytree(ROOT / "workspace-template/scripts", scripts, ignore=shutil.ignore_patterns("__pycache__"))
    expected = evaluate(tmp_path)
    for operation in ("check", "aggregate", "evaluate", "verify", "schedule"):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            assert main(["computation", operation, "--target", str(tmp_path)]) == 0
        assert json.loads(output.getvalue()) == expected
    for script in ("aggregate_records.py", "evaluate_formulas.py", "verify_assertions.py", "schedule_milestones.py"):
        completed = subprocess.run([sys.executable, str(scripts / script), "--project-root", str(tmp_path)],
                                   capture_output=True, text=True, check=False, timeout=30)
        assert completed.returncode == 0, completed.stderr
        assert json.loads(completed.stdout) == expected
    schemas = execute(tmp_path, "schemas")
    assert_matches_schema(expected, schemas[expected["schema_version"]])


def test_initializer_and_package_validator_share_declaration_rules(tmp_path):
    initializer = SERVICE.sibling("init_research_workspace")
    valid = workspace(tmp_path)
    initializer.validate_config_paths(valid)
    invalid = copy.deepcopy(valid)
    invalid["computation"]["graphs"]["worksheet"] = constant_definition()["graphs"]["worksheet"]
    invalid["computation"]["graphs"]["worksheet"]["nodes"]["total"]["expr"] = "unknown + 1"
    with pytest.raises(SystemExit, match="Invalid computation declaration"):
        initializer.validate_config_paths(invalid)
    profile = {"research_yml": {"computation": valid["computation"]}}
    assert initializer.profile_config_overrides(profile)["computation"] == valid["computation"]


def test_lint_and_plain_export_cannot_hide_an_invariant_error(tmp_path):
    data = definition()
    data["invariants"]["required"] = {"description": "Required check", "target": "computed", "selector": None,
        "inputs": {}, "filter": None, "assertion": "1 == 2", "severity": "error", "failure_message": "Required balance failed"}
    config = workspace(tmp_path, data)
    report = SERVICE.sibling("lint").run_checks(tmp_path, config)
    assert report["computation"]["status"] == "blocked"
    assert any(row["category"] == "computation_required_check_failed" and row["severity"] == "HIGH" for row in report["issues"])
    with pytest.raises(Exception) as caught:
        SERVICE.sibling("export_answers").build_export(tmp_path, None)
    assert caught.value.error_code == "COMPUTATION_REFUSED"


def test_unknown_keys_duplicates_and_unsafe_yaml_have_typed_refusals(tmp_path):
    config = workspace(tmp_path)
    path = tmp_path / "research.yml"
    original = path.read_text()
    for invalid in (original + "\ncomputation: {}\n", original.replace("version: '1.0'", "version: '999.0'"),
                    original.replace("precision: 64", "precision: 64\n    precision: 8")):
        path.write_text(invalid)
        with pytest.raises(EvidenceWikiError) as caught:
            evaluate(tmp_path)
        assert caught.value.error_code == "COMPUTATION_REFUSED"
    path.write_text(yaml.safe_dump(config))


def test_inapplicable_cli_flags_are_refused_before_any_mutation(tmp_path):
    config = workspace(tmp_path)
    config["computation"]["aggregations"]["observations"]["output_target"] = "wiki/outputs/value.md"
    (tmp_path / "research.yml").write_text(yaml.safe_dump(config))
    result = evaluate(tmp_path)
    before = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()}
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = main(["computation", "write", "--target", str(tmp_path), "--expected-result-id", result["result_id"],
                     "--request-id", "invalid", "--schema-id", "anything"])
    assert code == 2
    assert json.loads(output.getvalue())["details"]["reason"] == "computation_schema_option_requires_schemas"
    assert {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()} == before
    with pytest.raises(EvidenceWikiError):
        execute(tmp_path, "check", dry_run="false")


def test_strict_calculation_references_require_current_review_and_value(host):
    host.config["computation"] = constant_definition()
    (host.root / "research.yml").write_text(yaml.safe_dump(host.config, sort_keys=False))
    computed = evaluate(host.root)
    host.claims["schema_version"] = "evidence-strict-claims/v2"
    host.claims["claims"][0]["calculations"] = []
    inferred = copy.deepcopy(host.claims["claims"][0])
    inferred.update(id="declared-calculation", qualification="inference", premises=["product-source"],
                    text="The declared worksheet computes 6 units.", units="units",
                    derivation="Apply the declared multiplication to its explicit constant.", calculations=[{
                        "result_id": computed["result_id"], "pointer": "/graphs/worksheet/outputs/total", "expected": "6",
                        "form": "value", "unit": "units", "rounded": False}])
    host.claims["claims"].append(inferred)
    save_claims(host)
    review(host)
    envelope = review(host, claim_id="declared-calculation")
    assert envelope["payload"]["body"]["review"]["schema_version"] == "evidence-strict-review/v2"
    assert envelope["payload"]["body"]["review"]["snapshot"]["computation"]["result_id"] == computed["result_id"]
    result = STRICT.publication(host.root)
    assert result["schema_version"] == "evidence-strict-publication/v2" and result["verdict"] == "ship"
    assert "Calculated value: 6 units (exact arithmetic)" in STRICT.render_markdown(result)
    inferred["calculations"][0]["expected"] = "600"
    save_claims(host)
    review(host)
    review(host, claim_id="declared-calculation")
    blocked = STRICT.publication(host.root)
    assert blocked["verdict"] != "ship"
    assert "strict_computation_value_mismatch" in blocked["questions"][0]["gaps"]
