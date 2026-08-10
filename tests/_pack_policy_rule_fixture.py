"""One declarative-policy domain pack, and the delivered evidence its rules decide.

CR-9 lets a domain pack declare deterministic rules the package evaluates itself, so
every suite that exercises them needs the same three artifacts in agreement: a
``research.yml`` carrying both ``policy_vocabularies`` and ``policy_rules``, a delivered
source whose provenance sidecar and hash-bound structured view the rules resolve
against, and a question whose frontmatter a ``question_field`` rule reads. Restating
those in each suite would let them drift into testing three different packs.

**Every timestamp here is written relative to the caller's clock.** A ``max_age`` rule
pinned to a baked absolute instant would begin failing on its own the day the fixture
aged past the bound, turning a real regression and the passage of time into the same
red test.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

SCRIPTS = Path(__file__).resolve().parents[1] / "workspace-template" / "scripts"

PACK_NAME = "market-data"
SOURCE_POLICY = f"pack:{PACK_NAME}/supplier-quote-source"
FRESHNESS_POLICY = f"pack:{PACK_NAME}/quote-48h"
IDENTITY_POLICY = f"pack:{PACK_NAME}/sku-matches-candidate"

SOURCE_DEFINITION = "A supplier quote must come from a registered quote provider."
FRESHNESS_DEFINITION = "A supplier quote must be at most 48 hours old."
IDENTITY_DEFINITION = "The quoted SKU must match the candidate identity on the question."

PROVIDER_ID = "aliexpress-ds"
ALLOWED_PROVIDERS = [PROVIDER_ID, "partner-catalog"]
CANDIDATE_SKU = "B0ABC12345"

SOURCE_RULE = {"all_of": [{"one_of_provenance": {"providers": list(ALLOWED_PROVIDERS)}}]}
FRESHNESS_RULE = {"all_of": [{"max_age": {"field": "provenance/retrieved_at", "hours": 48}}]}
IDENTITY_RULE = {
    "all_of": [
        {"equals": {"field": "record/supplier_quote/sku", "question_field": "metadata/candidate_sku"}},
        {"one_of_provenance": {"providers": list(ALLOWED_PROVIDERS)}},
    ]
}

#: Vocabulary sections keyed exactly as ``domain_pack.policy_vocabularies`` writes them.
POLICY_VOCABULARIES = {
    "source_policy": {SOURCE_POLICY: SOURCE_DEFINITION},
    "freshness_policy": {FRESHNESS_POLICY: FRESHNESS_DEFINITION},
    "identity_policy": {IDENTITY_POLICY: IDENTITY_DEFINITION},
}
#: The fully automated pack: every declared policy carries a rule.
ALL_RULES = {
    SOURCE_POLICY: SOURCE_RULE,
    FRESHNESS_POLICY: FRESHNESS_RULE,
    IDENTITY_POLICY: IDENTITY_RULE,
}
#: The mixed pack: the identity policy stays definition-only and keeps needing a human.
PRIMITIVE_RULES_ONLY = {SOURCE_POLICY: SOURCE_RULE, FRESHNESS_POLICY: FRESHNESS_RULE}


#: What a host writes into a question's frontmatter for an identity rule to read. Kept as
#: literal lines rather than a YAML dump so a test comparing bytes compares a hand edit.
QUESTION_METADATA_BLOCK = f"metadata:\n  candidate_sku: {CANDIDATE_SKU}\n"


def add_question_metadata(workspace: Path, slug: str) -> str:
    """Insert the ``metadata:`` mapping into an existing question page's frontmatter."""
    path = workspace / "wiki" / "questions" / f"{slug}.md"
    lines = path.read_text(encoding="utf-8").split("\n")
    closing = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
    lines[closing:closing] = QUESTION_METADATA_BLOCK.rstrip("\n").split("\n")
    path.write_text("\n".join(lines), encoding="utf-8")
    return QUESTION_METADATA_BLOCK


def load_structured_view():
    """The sidecar reader, so fixtures write exactly what the evaluator will accept."""
    path = SCRIPTS / "_structured_view.py"
    spec = importlib.util.spec_from_file_location("structured_view_for_pack_fixture", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def declare_pack(workspace: Path, *, rules: dict[str, Any] | None = None) -> None:
    """Add the pack's vocabularies, and optionally its rules, to an existing research.yml."""
    config_path = workspace / "research.yml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    domain_pack: dict[str, Any] = {"name": PACK_NAME, "policy_vocabularies": POLICY_VOCABULARIES}
    if rules is not None:
        domain_pack["policy_rules"] = rules
    config["domain_pack"] = domain_pack
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


def deliver_quote(
    workspace: Path,
    source_id: str,
    *,
    age_hours: float,
    sku: str = CANDIDATE_SKU,
    provider_id: str | None = PROVIDER_ID,
    structured_view: str = "bound",
    usable: bool = True,
) -> str:
    """Deliver one structured supplier quote: raw payload, provenance, record, sidecar.

    ``structured_view`` selects what the record binds: ``bound`` a matching sidecar,
    ``absent`` none at all, ``corrupt`` one whose bytes no longer hash to the digest the
    record declares.
    """
    structured = load_structured_view()
    safe = structured.safe_source_id(source_id)
    raw_relative = f"raw/data/{safe}.json"
    (workspace / "raw" / "data").mkdir(parents=True, exist_ok=True)
    (workspace / "sources" / "normalized").mkdir(parents=True, exist_ok=True)
    (workspace / raw_relative).write_text(
        json.dumps({"asin": sku, "supplier_quote": f"{sku} 23.99 EUR"}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    retrieved_at = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    provenance: dict[str, Any] = {
        "origin_url": f"https://api.supplier.test/product/{sku}",
        "retrieved_at": retrieved_at,
        # Path-shaped on purpose: this names the fetching *agent*, and no rule may read it
        # as the provider that delivered the evidence.
        "retrieved_by": f"fetch_sources.py/{PROVIDER_ID}",
        "license": "CC-BY-4.0",
    }
    if provider_id is not None:
        provenance["provider_registration"] = {"id": provider_id}
    (workspace / f"{raw_relative}.provenance.yml").write_text(
        yaml.safe_dump(provenance, sort_keys=False), encoding="utf-8"
    )

    manifest = workspace / "sources" / "manifest.jsonl"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    with manifest.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "id": source_id,
                    "kind": "structured_data",
                    "raw_paths": [raw_relative],
                    "status": "normalized",
                    "detected_at": retrieved_at,
                },
                sort_keys=True,
            )
            + "\n"
        )

    frontmatter: dict[str, Any] = {
        "type": "normalized_source",
        "source_id": source_id,
        "source_kind": "structured_data",
        "status": "content_extracted",
        "raw_paths": [raw_relative],
        "manifest_path": "sources/manifest.jsonl",
        "parse_warnings": [],
    }
    if not usable:
        frontmatter["evidence_usable"] = False
    if structured_view != "absent":
        sidecar = structured.sidecar_path(workspace / "sources" / "normalized", source_id)
        document = json.dumps({"supplier_quote": {"sku": sku, "price": "23.99 EUR"}}, indent=2, sort_keys=True) + "\n"
        digest = structured.content_hash(document.encode("utf-8"))
        if structured_view == "corrupt":
            # Rewritten after the digest was taken, so the record binds a view that no
            # longer describes these bytes: tampering, not a missing file.
            document = document.replace(sku, "B0TAMPERED")
        sidecar.write_bytes(document.encode("utf-8"))
        frontmatter["structured_view"] = {
            "path": f"sources/normalized/{sidecar.name}",
            "content_hash": digest,
        }
    record = workspace / "sources" / "normalized" / f"{safe}.md"
    record.write_text(
        "---\n" + yaml.safe_dump(frontmatter, sort_keys=False) + "---\n\n# Supplier quote\n",
        encoding="utf-8",
    )
    return source_id


def coverage_manifest_document(
    slug: str,
    *,
    source_ids: list[str],
    identity_policy: str = "none",
    coverage_profile: str = "supplier-quote",
) -> dict[str, Any]:
    """A manifest whose required facet is decided by the pack's own policies."""
    return {
        "schema_version": "1.0",
        "question_slug": slug,
        "created_at": "2026-06-29T00:00:00Z",
        "updated_at": "2026-06-29T00:00:00Z",
        "coverage_profile": coverage_profile,
        "coverage_verdict": "pending",
        "required_facets": [
            {
                "facet_id": "supplier-quote",
                "description": "A current supplier quote for the candidate SKU.",
                "required": True,
                "evidence_path": "vendor_product_spec",
                "source_policy": SOURCE_POLICY,
                "freshness_policy": FRESHNESS_POLICY,
                "identity_policy": identity_policy,
                "min_sources": 1,
                "accepted_source_ids": list(source_ids),
                "blocking_request_ids": [],
                "facet_verdict": "pending",
            }
        ],
        "optional_facets": [],
    }


def write_coverage_manifest(workspace: Path, slug: str, document: dict[str, Any]) -> Path:
    path = workspace / "sources" / "coverage" / f"{slug}.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path
