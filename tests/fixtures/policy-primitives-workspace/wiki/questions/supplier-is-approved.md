---
type: question
created: '2026-08-11'
updated: '2026-08-11'
status: open
priority: high
question: Is the quoting supplier approved to supply B0ABC12345 under a current contract?
metadata:
  candidate_sku: B0ABC12345
source_ids:
  - quote:supplier-fresh
grounding:
  - claim: The approved supplier quotes SKU B0ABC12345.
    source_id: quote:supplier-fresh
    anchor:
      pointer: supplier_quote/sku
      expected: B0ABC12345
---

# Is the quoting supplier approved to supply B0ABC12345 under a current contract?

## Task

- Status: open
- Priority: high

This question's facet keeps the two policies a rule cannot finish: one whose declared
checks all pass but which still asks for a recorded review, and one the pack never gave a
rule at all.

## Answer

_Not yet answered._
