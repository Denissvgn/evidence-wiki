#!/usr/bin/env python3
"""Export inert reference cases without importing their executable qualification code."""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "workspace-template/docs/pack-assessment-references.json"


def content():
    tree = ast.parse((ROOT / "tests/test_computation_examples.py").read_text())
    expected = next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == "EXPECTED" for target in node.targets))
    examples = ROOT / "workspace-template/docs/computation-examples"
    arithmetic = {}
    for name, values in expected.items():
        arithmetic[name] = {"definition": yaml.safe_load((examples / name / "research.overlay.yml").read_text())["computation"],
            "records": json.loads((examples / name / "records.json").read_text()), "expected_graphs": values,
            "meaning": "Synthetic arithmetic example; formula, score/percentile labels, filing rules and thresholds require independent domain review."}
    value = {"schema_version": "evidence-pack-assessment-references/v1",
        "semantic": json.loads((ROOT / "tests/fixtures/strict-evidence/review-cases.json").read_text()),
        "arithmetic": arithmetic,
        "limitations": ["Existing synthetic reference judgments, not expert-reviewed production-domain cases.",
            "Arithmetic expectations are independently specified; matching them qualifies engine behavior on these inputs only.",
            "Counterevidence and semantic judgments require independent authenticated review for actual research acceptance."]}
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    raw = content()
    same = TARGET.is_file() and TARGET.read_bytes() == raw
    if not args.check and not same:
        TARGET.write_bytes(raw)
    print(json.dumps({"current": same, "path": str(TARGET.relative_to(ROOT))}))
    return 1 if args.check and not same else 0


if __name__ == "__main__":
    raise SystemExit(main())
