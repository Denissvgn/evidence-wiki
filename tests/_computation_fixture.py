"""Small retained structured records and independently specified declarations."""

import hashlib
import json
from pathlib import Path

import yaml


def definition():
    return {"version": "1.0", "arithmetic": {"mode": "exact", "precision": 64, "scale": None, "rounding": "ROUND_HALF_EVEN"},
            "clock": {"as_of": "2026-09-21T12:00:00Z", "timezone": "UTC", "ambiguous": "refuse", "nonexistent": "refuse", "search_days": 366},
            "tables": {}, "aggregations": {}, "graphs": {}, "invariants": {}, "cadence": {}}


def selector():
    return {"source_glob": "sources/normalized/*.md", "records_pointer": "/records", "row_key": "/id", "allow_empty": False}


def metric(op="sum", expr="record.amount", denominator=None):
    return {"op": op, "expr": expr, "denominator": denominator, "unit": "units"}


def aggregation(*, groups=None, metrics=None):
    return {"description": "Retained observations", "selector": selector(), "filter": None,
            "group_by": groups or [], "metrics": metrics or {"total": metric()}, "output_target": None}


def reference(identifier="observations", metric_name="total", group=None, reduce=None):
    return {"kind": "aggregation", "id": identifier, "metric": metric_name, "group": [] if group is None and reduce is None else group, "reduce": reduce}


def graph():
    return {"description": "Derived worksheet", "constants": {"factor": {"value": "1.25", "unit": "ratio"}},
            "inputs": {"amount": reference()}, "nodes": {"scaled": {"expr": "amount * constants.factor", "unit": "units"}},
            "output_mapping": {"total": "scaled"}, "output_page": None}


def workspace(root: Path, declaration=None, raw=b'{"records":[{"id":"a","amount":0.1},{"id":"b","amount":0.2}]}'):
    root.mkdir(parents=True, exist_ok=True)
    config = yaml.safe_load((Path(__file__).resolve().parents[1] / "workspace-template/research.yml").read_text())
    config["computation"] = declaration or definition()
    if not config["computation"]["aggregations"]:
        config["computation"]["aggregations"]["observations"] = aggregation()
    (root / "research.yml").write_text(yaml.safe_dump(config, sort_keys=False))
    normalized = root / "sources/normalized"
    normalized.mkdir(parents=True)
    raw_path = root / "raw/data/observations.json"
    raw_path.parent.mkdir(parents=True)
    raw_path.write_bytes(raw)
    source = {"id": "data:observations", "kind": "structured_data", "status": "normalized",
              "raw_paths": ["raw/data/observations.json"], "title": "Retained observations"}
    (root / "sources/manifest.jsonl").write_text(json.dumps(source) + "\n")
    sidecar = normalized / "data--observations.structured.json"
    sidecar.write_bytes(raw)
    metadata = {"type": "normalized_source", "source_id": source["id"], "source_kind": source["kind"],
        "normalized_format": 1, "normalizer": {"name": "fixture", "version": "1.0"},
        "status": "content_extracted", "evidence_usable": True, "created": "2026-09-21", "updated": "2026-09-21",
        "raw_paths": source["raw_paths"], "manifest_path": "sources/manifest.jsonl", "parse_warnings": [], "title": source["title"],
        "structured_view": {"path": "sources/normalized/" + sidecar.name, "content_hash": "sha256:" + hashlib.sha256(raw).hexdigest()}}
    sections = ["Citation Metadata", "Abstract", "Outline", "Extracted Text", "Figures and Tables", "Links", "Raw Source Paths", "Parse Warnings"]
    (normalized / "data--observations.md").write_text("---\n" + yaml.safe_dump(metadata, sort_keys=False) + "---\n\n# Retained observations\n\n" +
        "\n\n".join("## " + title + "\n\n" + ("- None recorded." if title == "Parse Warnings" else "Retained local data.") for title in sections) + "\n")
    return config
