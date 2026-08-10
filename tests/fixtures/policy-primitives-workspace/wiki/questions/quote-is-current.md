---
type: question
created: '2026-08-11'
updated: '2026-08-11'
status: open
priority: high
question: Is the supplier quote for B0ABC12345 at most 48 hours old?
metadata:
  candidate_sku: B0ABC12345
source_ids:
  - quote:supplier-fresh
grounding:
  - claim: The supplier quotes SKU B0ABC12345 at 23.99 EUR per unit.
    source_id: quote:supplier-fresh
    anchor:
      pointer: supplier_quote/unit_price_eur
      expected: '23.99'
---

# Is the supplier quote for B0ABC12345 at most 48 hours old?

## Task

- Status: open
- Priority: high

Every policy on this question's coverage facet is decided by a rule the domain pack
declares, so answering it under `--require-coverage` must not route to human review.

## Answer

_Not yet answered._
