"""Synthetic delegated filings and price slices; no provider or market execution."""

from __future__ import annotations

import copy
from pathlib import Path

import yaml

from tests._execution_fixture import binding, canonical, closure

SOURCE_ID = "market:observations"


def listing(*, listing_id="listing-a", issuer_id="issuer-a", symbol="AAA", status="active", currency="USD"):
    return {"issuer_id": issuer_id, "listing_id": listing_id, "provider_id": listing_id + "-provider",
            "venue": "XNAS", "symbol": symbol, "issuer_name": "Synthetic " + issuer_id,
            "valid_from": "2020-01-01T00:00:00Z", "valid_to": None, "currency": currency,
            "status": status, "status_at": "2026-09-09T20:00:00Z"}


def assemble(document, artifacts):
    files = dict(artifacts)
    document = copy.deepcopy(document)
    document["artifacts"] = [{"path": path, **binding(data), "role": "provenance" if path != "page.json" else "input"}
                             for path, data in sorted(files.items())]
    files["market-record.json"] = canonical(document)
    return files, document


def example(route="alpaca-stock-bars"):
    artifacts = {"terms.txt": b"Synthetic policy reference; authority is supplied separately by the host.\n",
                 "universe.json": canonical({"as_of": "2026-09-08", "listings": ["listing-a", "listing-b"], "includes_delisted": True}),
                 "actions.json": canonical({"listing-a": {"split": "2", "dividend": "0.25"}})}

    def ref(path):
        return {"path": path, "content_hash": binding(artifacts[path])["content_hash"]}

    provenance = {"provider": "alpaca", "retrieved_at": "2026-09-09T21:00:00Z",
                  "policy_reference": {"policy_id": "market-terms", "policy_revision": "1", "artifact": ref("terms.txt")},
                  "durable_slice_id": "host-slice:prices-2026-09-09", "retrieval_instructions": "Ask the host for this immutable slice ID.",
                  "conflicts": [], "excluded": [],
                  "universe": {"as_of": "2026-09-08T00:00:00Z", "coverage": "complete", "listing_ids": ["listing-a", "listing-b"], "artifact": ref("universe.json")},
                  "corporate_action_coverage": "complete",
                  "corporate_actions": [{"listing_id": "listing-a", "type": "split", "effective_at": "2026-09-09T00:00:00Z",
                                         "ratio": "2", "amount": None, "currency": None, "artifact": ref("actions.json")},
                                        {"listing_id": "listing-a", "type": "dividend", "effective_at": "2026-09-09T00:00:00Z",
                                         "ratio": None, "amount": "0.25", "currency": "USD", "artifact": ref("actions.json")}]}
    identities = [listing(), listing(listing_id="listing-b", issuer_id="issuer-b", symbol="BBB", status="delisted")]
    request = {"symbols": ["AAA", "BBB"], "start": "2026-09-09T13:30:00Z", "end": "2026-09-09T20:00:00Z",
               "timeframe": "1Hour", "feed": "iex", "delay": "end-of-day", "currency": "USD", "adjustment": "split,dividend",
               "asof": "-", "session": "regular", "calendar": "XNYS/2026", "timezone": "America/New_York",
               "expected_bars": [{"listing_id": item["listing_id"], "symbol": item["symbol"],
                                  "start": "2026-09-09T13:30:00Z", "end": "2026-09-09T14:30:00Z"} for item in identities]}
    # Decimal tokens deliberately exceed binary float precision.
    artifacts["page.json"] = (b'{"bars":{"AAA":[{"t":"2026-09-09T13:30:00Z","o":10,"h":12,"l":9,"c":10.1234567890123456789,"v":1000,"n":10,"vw":10.02}],'
                              b'"BBB":[{"t":"2026-09-09T13:30:00Z","o":20,"h":22,"l":19,"c":21,"v":500,"n":5,"vw":20.50}]},"next_page_token":null}\n')
    if route == "sec-company-concept":
        artifacts.pop("actions.json")
        artifacts.pop("universe.json")
        provenance.update(provider="sec", corporate_action_coverage="not-applicable", corporate_actions=[],
                          universe={"as_of": "2026-09-08T00:00:00Z", "coverage": "not-applicable", "listing_ids": [], "artifact": None})
        identities = [listing()]
        request = {"issuer_id": "issuer-a", "cik": "0000000123", "taxonomy": "us-gaap", "tag": "Revenue",
                   "units": ["USD", "EUR"], "periods": [{"start": "2026-01-01", "end": "2026-06-30"}], "accessions": ["123-26-001", "123-26-002"]}
        artifacts["page.json"] = canonical({"cik": 123, "taxonomy": "us-gaap", "tag": "Revenue", "entityName": "Synthetic issuer-a",
                                           "units": {unit: [{"val": value, "start": "2026-01-01", "end": "2026-06-30", "accn": accession,
                                                            "form": "10-Q/A" if accession.endswith("2") else "10-Q", "filed": "2026-08-01",
                                                            "fy": 2026, "fp": "Q2", "frame": "CY2026Q2"}
                                                           for accession, value in (("123-26-001", 1234567890123456789), ("123-26-002", 1234567890123456790))]
                                                     for unit in ("USD", "EUR")}})
    document = {"schema_version": "market-evidence/v1", "profile": "market_evidence/v1", "source_id": SOURCE_ID,
                "route": route, "request": request, "listings": identities, "provenance": provenance,
                "pages": [{"artifact": ref("page.json"), "request_token": None}]}
    return assemble(document, artifacts)


def revise(files, document, mutate):
    """Refresh only the container's byte bindings; never change provider observations implicitly."""
    document = copy.deepcopy(document)
    artifacts = {path: data for path, data in files.items() if path != "market-record.json"}
    mutate(document, artifacts)
    for page in document["pages"]:
        path = page["artifact"]["path"]
        page["artifact"]["content_hash"] = binding(artifacts[path])["content_hash"]
    return assemble(document, artifacts)


def workspace(root: Path, files=None):
    if files is None:
        files, _ = example()
    config = {"project": {"name": "Delegated market observations"},
              "sources": {"manifest_path": "sources/manifest.jsonl", "normalized_dir": "sources/normalized"}}
    record = {"id": SOURCE_ID, "kind": "market_evidence", "title": "Synthetic market observations",
              "raw_paths": [], "raw_fingerprint": closure(files), "metadata": {}}
    folder = root / "sources/evidence/market--observations"
    folder.mkdir(parents=True, exist_ok=True)
    for path, data in files.items():
        (folder / path).write_bytes(data)
    (root / "sources/normalized").mkdir(exist_ok=True)
    (root / "research.yml").write_text(yaml.safe_dump(config), encoding="utf-8")
    (root / "sources/manifest.jsonl").write_bytes(canonical(record))
    return config, record, folder
