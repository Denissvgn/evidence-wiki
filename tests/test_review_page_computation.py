"""Actual protected owner arithmetic is decisive packet material, not model approval."""

import copy
import importlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
import yaml

from evidence_wiki import Workspace
from evidence_wiki._pack_io import json_document
from evidence_wiki.computation import evaluate
from evidence_wiki.errors import EvidenceWikiError
from tests import test_calculation_execution_fixture as reference


def test_actual_protected_quantitative_refs_values_and_diagnosis_survive_required_pages(tmp_path, monkeypatch):
    root, _policy, _execution, context = reference.fixture.__wrapped__(tmp_path, monkeypatch,
        SimpleNamespace(param={"calculation": True, "coverage_examination": True, "role_isolation": True}))
    operations = importlib.import_module("evidence_wiki.review_page_operations")
    context = replace(context, grants=context.grants | {"review_page_operations.prepare", "review_page_operations.page",
        "coverage_observation.observe"})
    with context.activate(), Workspace.open(root) as workspace:
        result = evaluate(root)
        inventory = json_document((root / "docs/research-claims.json").read_bytes())
        assert inventory["schema_version"] == "evidence-strict-claims/v2"
        premise = inventory["claims"][0]
        scalar = result["graphs"]["worksheet"]["outputs"]["total"]
        derived = copy.deepcopy(premise)
        derived.update(id="calculated-observation", qualification="inference", premises=[premise["id"]],
            text="The declared native computation yields six units.", units=scalar["unit"],
            derivation="Use the exact configured multiplication and declared unit.",
            calculations=[{"result_id": result["result_id"], "pointer": "/graphs/worksheet/outputs/total",
                "expected": scalar["value"], "form": "value", "unit": scalar["unit"], "rounded": scalar["rounded"]}])
        inventory["claims"].append(derived)
        question = json_document((root / "docs/research-requirements.json").read_bytes())["request"]["payload"]["questions"][0]
        proposal = {"schema_version": "evidence-draft-proposal/v1", "inventory": inventory,
            "originals": [{"original_id": question["id"], "text": question["text"], "question_slugs": ["q"]}]}
        request = workspace.mutations.prepare(request_id="quantitative-pages", operation="strict.admit", arguments={"proposal": proposal})
        assert workspace.mutations.submit(request)["status"] == "committed"
        selected_sources = list(dict.fromkeys(row["source_id"] for claim in inventory["claims"] for row in claim["evidence"]))
        handle = operations.prepare(root, claim_id=derived["id"], context_id="quantitative-pages", page_chars=1024,
            source_ids=selected_sources)
        header = handle.header()
        pages = [handle.page(row["page_id"]) for row in header["pages"]]
        computation = json.loads("".join(row["text"] for row in pages if row.get("section_id") == "computation"))
        diagnostics = json.loads("".join(row["text"] for row in pages if row.get("section_id") == "mechanical-counter"))
        assert computation["result_id"] == result["result_id"]
        assert computation["graphs"]["worksheet"]["outputs"]["total"] == scalar
        assert computation["definition"]["arithmetic"] == result["definition"]["arithmetic"]
        assert computation["invariants"] == result["invariants"] and computation["clock"] == result["clock"]
        assert header["claim"]["calculations"][0]["expected"] == scalar["value"]
        assert len(diagnostics["mechanical"]) == 2
        assert handle.complete(pages)["complete"]
        assert not handle.complete([row for row in pages if row.get("section_id") != "computation"])["complete"]
        config = yaml.safe_load((root / "research.yml").read_bytes())
        config["computation"]["graphs"]["worksheet"]["constants"]["amount"]["value"] = "4"
        (root / "research.yml").write_text(yaml.safe_dump(config, sort_keys=False))
        with pytest.raises(EvidenceWikiError):
            handle.page(next(row["page_id"] for row in header["pages"] if row.get("section_id") == "computation"))
