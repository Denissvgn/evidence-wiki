---
type: normalized_source
source_id: quote:supplier-fresh
source_kind: structured_data
status: content_extracted
evidence_usable: true
created: 2026-08-11
updated: 2026-08-11
raw_paths:
  - raw/data/supplier-quote-fresh.json
manifest_path: sources/manifest.jsonl
parse_warnings: []
title: Supplier quote for B0ABC12345
# The binding is what makes the sidecar this record's own evidence rather than a file
# that happens to sit beside it. Editing the sidecar without recomputing this digest is
# refused as structured_view_corrupt; the fixture guard test in
# tests/test_policy_primitives_e2e.py names that mistake directly.
structured_view:
  path: sources/normalized/quote--supplier-fresh.structured.json
  content_hash: sha256:07740e8c8b8c2b66a24da14b315067a7ea3eabba253e13c2f1e624fede3e36e8
---

# Supplier quote for B0ABC12345

## Citation Metadata

- URL: https://api.supplier.test/product/B0ABC12345

## Abstract

Current supplier quote captured from the registered dropshipping feed.

## Outline

- supplier_quote

## Extracted Text

### supplier_quote

- sku: B0ABC12345
- unit_price_eur: 23.99
- currency: EUR

## Figures and Tables

- None recorded.

## Links

- https://api.supplier.test/product/B0ABC12345

## Raw Source Paths

- `raw/data/supplier-quote-fresh.json`

## Parse Warnings

- None recorded.
