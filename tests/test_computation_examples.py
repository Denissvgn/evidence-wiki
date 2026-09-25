"""Independent expected values for three declarative candidate domains."""

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from evidence_wiki.computation import evaluate
from evidence_wiki.domain_pack_validator import validate_domain_pack
from tests._computation_fixture import workspace

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "workspace-template/docs/computation-examples"
EXPECTED = {
    "sample-benchmark": {"report": {"score_pct": "90", "efficiency": "36"}},
    "sample-portfolio": {"portfolio": {"exposure": "400000", "pnl": "750", "utilization": "0.4"}},
    "sample-filing": {"filing": {"box_income": "1000", "box_costs": "200", "box_profit": "800", "box_gross": "160", "box_withheld": "50", "box_due": "110"}},
}


@pytest.mark.parametrize("name", EXPECTED)
def test_declarative_candidates_share_unchanged_engine_bytes(tmp_path, name):
    paths = sorted((ROOT / "workspace-template/scripts").glob("_computation*.py"))
    before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    candidate = EXAMPLES / name
    overlay = yaml.safe_load((candidate / "research.overlay.yml").read_text())
    workspace(tmp_path, overlay["computation"], (candidate / "records.json").read_bytes())
    result = evaluate(tmp_path)
    for graph, outputs in EXPECTED[name].items():
        assert {key: row["value"] for key, row in result["graphs"][graph]["outputs"].items()} == outputs
        assert all(row["lineage"] for row in result["graphs"][graph]["outputs"].values())
    assert result["status"] == "passed"
    assert {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths} == before
    assert all(path.suffix in {".yml", ".json", ".md"} for path in candidate.rglob('*') if path.is_file())


@pytest.mark.parametrize("name", EXPECTED)
def test_candidates_pass_the_canonical_pack_validator(name):
    report = validate_domain_pack(str(EXAMPLES / name), root=ROOT)
    assert report["ok"], json.dumps(report, indent=2)
