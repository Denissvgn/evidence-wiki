"""Data-only publication fixture shared with isolated distribution checks."""

import hashlib
import json
from pathlib import Path

import yaml


def write_ship_ready_vendor_fixture(target: Path) -> None:
    source_id = "web:vendor-official-product-spec"
    raw_path = target / "raw" / "web" / "vendor-product.html"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(
        "<html><head><title>Official product spec</title></head>"
        "<body>Vendor-controlled product specification.</body></html>\n",
        encoding="utf-8",
    )
    (target / "sources" / "manifest.jsonl").write_text(
        json.dumps(
            {
                "id": source_id,
                "kind": "html",
                "raw_paths": ["raw/web/vendor-product.html"],
                "status": "normalized",
                "detected_at": "2026-07-02T12:00:00Z",
                "provenance": {
                    "origin_url": "https://docs.vendor.example/product/spec",
                    "retrieved_at": "2026-07-02T12:00:00Z",
                    "retrieved_by": "fetch-agent/manual",
                    "license": "Vendor terms",
                    "terms_url": "https://vendor.example/terms",
                    "terms_note": "Official terms reviewed before publication.",
                    "notes": "Official vendor product page captured for publication fixture.",
                    "candidate_id": "cand-vendor-product",
                    "checksum": "sha256:" + hashlib.sha256(raw_path.read_bytes()).hexdigest(),
                    "checksum_verified": True,
                    "date_not_available": "Official vendor spec page exposes no publication date.",
                },
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    normalized = target / "sources" / "normalized" / "web--vendor-official-product-spec.md"
    normalized.parent.mkdir(parents=True, exist_ok=True)
    normalized.write_text(
        f"""---
type: normalized_source
source_id: {source_id}
source_kind: html
title: Official product spec
provenance:
  origin_url: https://docs.vendor.example/product/spec
  retrieved_at: "2026-07-02T12:00:00Z"
  date_not_available: Official vendor spec page exposes no publication date.
---

# Official product spec

Vendor-controlled product specification.
""",
        encoding="utf-8",
    )
    candidates = target / "sources" / "discovery" / "candidates.jsonl"
    candidates.parent.mkdir(parents=True, exist_ok=True)
    candidates.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "candidate_id": "cand-vendor-product",
                "provider": "search",
                "url": "https://docs.vendor.example/product/spec",
                "title": "Official product spec",
                "source_type": "web_page",
                "trust_tier": "official_primary",
                "official_source": True,
                "recommended_action": "fetch",
                "status": "fetched",
                "selected_for_request_id": "req-vendor-product",
                "fetched_source_id": source_id,
                "evidence_path": "vendor_product_spec",
                "source_policy": "official_vendor",
                "freshness_policy": "current_product_spec",
                "identity_policy": "origin_url_matches_candidate",
                "reasoning": {"risk_flags": []},
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    source_note = target / "wiki" / "sources" / "vendor-product-source.md"
    source_note.write_text(
        f"""---
type: source
created: 2026-07-02
updated: 2026-07-02
source_ids:
  - {source_id}
---

# Vendor Product Source

Official vendor source note.
""",
        encoding="utf-8",
    )
    answer = target / "wiki" / "synthesis" / "vendor-product-answer.md"
    answer.write_text(
        f"""---
type: synthesis
created: 2026-07-02
updated: 2026-07-02
source_ids:
  - {source_id}
summary: The vendor product spec is grounded in the official product page.
---

# Vendor Product Spec

The vendor product spec is grounded in the official product page.
""",
        encoding="utf-8",
    )
    question = target / "wiki" / "questions" / "vendor-product-spec.md"
    text = question.read_text(encoding="utf-8")
    text = text.replace("status: open", "status: answered", 1)
    text = text.replace(
        "source_ids: []",
        f"""source_ids:
  - {source_id}
answer_page: ../synthesis/vendor-product-answer.md
coverage_required: true
coverage_manifest: sources/coverage/vendor-product-spec.yml
answered_by: answer-agent
grounding:
  - claim: The product spec is vendor-controlled.
    source_id: {source_id}
    quote: Vendor-controlled product specification.
    location_hint: Official product spec
confidence: high
evidence_strength: corroborated""",
        1,
    )
    question.write_text(text, encoding="utf-8")
    coverage = target / "sources" / "coverage" / "vendor-product-spec.yml"
    coverage.parent.mkdir(parents=True, exist_ok=True)
    coverage.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "question_slug": "vendor-product-spec",
                "created_at": "2026-07-02T12:00:00Z",
                "updated_at": "2026-07-02T12:00:00Z",
                "coverage_profile": "vendor-product-spec",
                "coverage_verdict": "pending",
                "required_facets": [
                    {
                        "facet_id": "official-spec",
                        "description": "Confirm the product specification from an official vendor page.",
                        "required": True,
                        "evidence_path": "vendor_product_spec",
                        "source_policy": "official_vendor",
                        "freshness_policy": "current_product_spec",
                        "identity_policy": "origin_url_matches_candidate",
                        "min_sources": 1,
                        "accepted_source_ids": [source_id],
                        "blocking_request_ids": [],
                        "facet_verdict": "pending",
                    }
                ],
                "optional_facets": [],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

