"""Drive explicit local guidance authoring and later coverage migration."""

import json
import shutil
from pathlib import Path

import yaml

from tools._journey_cases import canonical, facet, require


def author(journey):
    command, root = journey.command, journey.root
    spec = json.loads(command("pack", "guide", "--topic", "specification")["content"])
    spec["unresolved"] = []
    path = root / "spec.json"
    path.write_bytes(canonical(spec))
    draft = root / "draft"
    result = command("pack", "scaffold", "--from-file", path, "--output", draft)
    rows = []
    for scenario, region, question, outcome in (("adequate", "north", "north", "pass"),
            ("missing", None, "north", "fail"), ("conflicting", "south", "north", "fail"), ("wrong_scope", "north", "east", "fail")):
        rows.append({"id": scenario, "requirement_ids": ["region"], "scenario": scenario, "kind": "policy",
            "target": "pack:" + spec["name"] + "/region-match",
            "inputs": {"structured": {} if region is None else {"region": region}, "question": {"metadata": {"region": question}},
                "provenance": {}, "origin_host": None, "provider_ids": [], "as_of": "2026-09-22T12:00:00Z"},
            "expected": {"status": "observed", "outcome": outcome, "human_review_required": True},
            "rationale": "Frozen synthetic region-equality judgment"})
    suite = {"schema_version": "evidence-pack-cases/v1", "draft_id": result["draft_id"], "cases": rows,
             "exceptions": [], "limitations": ["Synthetic conformance only; no expert-domain certification"]}
    path = root / "pack-cases.json"
    path.write_bytes(canonical(suite))
    require(not command("pack", "freeze-cases", "--draft", draft, "--from-file", path)["gaps"], 'journey_expectation:not command("pack", "freeze-cases", "--draft", draft, "--from-file", path)["gaps"]')
    assessment = command("pack", "assess", "--draft", draft)
    require(assessment["assessment"]["mechanical_cases_passed"], 'journey_expectation:assessment["assessment"]["mechanical_cases_passed"]')
    catalog = root / "catalog"
    command("pack", "catalog", "init", "--catalog", catalog, "--root", "drafts=" + str(root))
    command("pack", "accept", "--draft", draft, "--assessment-id", assessment["record"]["sha256"],
            "--catalog", catalog, "--root-id", "drafts", "--id", "scoped-one", "--scope", "Synthetic northern observations")
    journey.candidate, journey.catalog = Path(result["candidate"]), catalog


def revise(journey):
    command, root, workspace = journey.command, journey.root, journey.workspace
    candidate = root / "revision" / journey.candidate.name
    shutil.copytree(journey.candidate, candidate)
    overlay = yaml.safe_load((candidate / "research.overlay.yml").read_bytes())
    overlay["domain_pack"]["version"] = "0.2.0"
    (candidate / "research.overlay.yml").write_text(yaml.safe_dump(overlay, sort_keys=False), encoding="utf-8", newline="\n")
    path = candidate / "claims.md"
    path.write_text(path.read_text(encoding="utf-8") + "\nRetain the distinction between northern observations and absent southern evidence.\n", encoding="utf-8", newline="\n")
    before = (workspace / "wiki/questions/q1.md").read_bytes()
    saved = root / "revision-plan.json"
    command("pack", "revision-plan", "--target", workspace, "--path", candidate,
            "--rationale", "Clarify the retained scope limitation", "--output", saved, authority=True)
    applied = command("pack", "revision-apply", "--from-file", saved, authority=True)
    require(applied["research"]["pending_questions"] == ["q1"], 'journey_expectation:applied["research"]["pending_questions"] == ["q1"]')
    require(command("pack", "revision-apply", "--from-file", saved, authority=True)["status"] == "already_applied", 'journey_expectation:command("pack", "revision-apply", "--from-file", saved, authority=True)["status"] == "already_applied"')
    require((workspace / "wiki/questions/q1.md").read_bytes() == before, 'journey_expectation:(workspace / "wiki/questions/q1.md").read_bytes() == before')
    blocked = command("agent", "research-export", "--target", workspace, "--allow-partial", expected=(0, 3), authority=True)
    require(not blocked["research_complete"], 'journey_expectation:not blocked["research_complete"]')
    migration = {"schema_version": "evidence-pack-reevaluation/v1", "revision_id": applied["revision_id"], "slug": "q1",
        "rationale": "Retain the original facet and explicitly reassess the changed guidance", "retired_facets": [],
        "request_replacements": {}, "computation_migrations": {},
        "template": {"coverage_profile": "manual", "required_facets": [facet()], "optional_facets": []}}
    path = root / "migration.json"
    path.write_bytes(canonical(migration))
    migrated = command("pack", "reevaluate", "--target", workspace, "--from-file", path, authority=True)
    require(migrated["status"] == "migrated" and not migrated["release_accepted"], 'journey_expectation:migrated["status"] == "migrated" and not migrated["release_accepted"]')
    require((workspace / migrated["archive"]).is_file(), 'journey_expectation:(workspace / migrated["archive"]).is_file()')
    require(command("pack", "reevaluate", "--target", workspace, "--from-file", path, authority=True)["status"] == "already_migrated", 'journey_expectation:command("pack", "reevaluate", "--target", workspace, "--from-file", path, authority=True)["status"] == "already_migrated"')
    return {"revision": applied["revision_id"], "migration": migrated, "old_export_invalidated": True}
