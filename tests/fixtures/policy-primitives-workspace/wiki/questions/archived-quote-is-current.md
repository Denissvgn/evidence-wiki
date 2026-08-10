---
type: question
created: '2026-08-11'
updated: '2026-08-11'
status: open
priority: medium
question: Is the archived supplier quote for B0ABC12345 still current?
metadata:
  candidate_sku: B0ABC12345
source_ids:
  - quote:supplier-archived
grounding:
  - claim: The archived quote lists SKU B0ABC12345 at 24.50 EUR per unit.
    source_id: quote:supplier-archived
    anchor:
      pointer: supplier_quote/unit_price_eur
      expected: '24.50'
---

# Is the archived supplier quote for B0ABC12345 still current?

## Task

- Status: open
- Priority: medium

The accepted delivery is 50 hours old, so the pack's `max_age: 48h` rule fails it and the
required facet blocks. Redelivering the same quote inside the window is the whole repair.

## Answer

_Not yet answered._
