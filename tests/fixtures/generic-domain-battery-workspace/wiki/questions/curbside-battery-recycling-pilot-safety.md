---
type: question
created: '2026-07-04'
updated: '2026-07-04'
status: human_review
priority: high
question: For a small city evaluating curbside battery recycling bins, what safety, operations, and evidence requirements should be considered before a pilot?
answer_page: ../outputs/curbside-battery-recycling-pilot-safety.md
coverage_required: true
coverage_manifest: sources/coverage/curbside-battery-recycling-pilot-safety.yml
human_review_required: true
human_review_policies:
  - manual_review_required
source_ids:
  - web:official-safety-guidance
  - web:fire-risk-guidance
  - web:vendor-container-spec
grounding:
  - claim: Separate damaged batteries before transport.
    source_id: web:official-safety-guidance
    quote: Battery collection sites should separate damaged, defective, or recalled batteries from routine household batteries before transport.
  - claim: Public bins need signage, inspection, and terminal protection.
    source_id: web:official-safety-guidance
    quote: Collection bins should include clear signage, staff inspection, terminal protection, and instructions that keep loose lithium-ion batteries away from ordinary trash.
  - claim: Fire planning must account for heat, toxic gas, and reignition.
    source_id: web:fire-risk-guidance
    quote: The U.S. Fire Administration describes lithium-ion battery incidents as fires that can produce intense heat, toxic gases, and reignition hazards.
  - claim: The vendor container kit includes packaging materials and shipping instructions.
    source_id: web:vendor-container-spec
    quote: The 5-gallon DDR kit is described for damaged, defective, or recalled batteries and includes a UN-rated container, liner, cushioning, and shipping instructions.
---

Answer is complete for automated coverage and grounding, but intentionally awaits explicit human review approval before publication.
