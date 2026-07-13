# Legal Regulatory Claims

Legal and regulatory claims should keep the authority, jurisdiction, date, and currentness boundary explicit.

## Claim Categories

- `legal_rule`: a rule, obligation, permission, or prohibition from an official source.
- `current_figure`: a current fee, tax rate, threshold, benefit amount, deadline, or similar figure.
- `eligibility_rule`: who qualifies for a status, benefit, procedure, or exception.
- `procedure_step`: an administrative step, filing, form, registration, or appeal path.
- `jurisdiction_scope`: the jurisdiction, authority, effective area, or applicability boundary.
- `factual`: a supporting factual claim that does not fit the categories above.

## Required Interpretation Fields

- `jurisdiction`: country, region, city, regulator, or other authority scope.
- `authority`: official issuing body or source family.
- `effective_date`: when the rule or figure applies, when known.
- `currentness`: how the source establishes current validity.
- `source_ids`: normalized source records backing the claim.

## Examples

- A current reduced social-security fee should be a `current_figure` claim backed by an official social-security or government source.
- A registration deadline should be a `procedure_step` or `legal_rule` claim backed by the official procedure page or governing norm.
- A jurisdiction-specific tax advantage should include both the jurisdiction and the official authority that publishes the figure.
