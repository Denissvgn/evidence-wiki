# Legal Regulatory Domain Pack

Domain pack for research workspaces focused on legal, tax, regulatory, administrative, and public-policy questions that need current official-source grounding.

This pack extends the reusable research workspace with legal/regulatory taxonomy guidance and a reusable coverage template for current official figures. It does not include raw evidence, source records, or jurisdiction-specific official URLs.

## Files

- `research.overlay.yml`: configuration fragment to merge into a workspace `research.yml`.
- `taxonomy.md`: page placement and filing rules for legal/regulatory evidence.
- `claims.md`: structured claim categories and examples for official rules and figures.
- `coverage-templates/official-current-figure.yml`: answerability template for current legal, tax, fee, threshold, deadline, or benefit figures.

## Applying The Pack

1. Start from a configured research workspace.
2. Deep-merge `research.overlay.yml` into that workspace's `research.yml` so base wiki rules are preserved.
3. Use `taxonomy.md` and `claims.md` to keep official rules, jurisdictions, procedures, and legal figures separate from commentary.
4. Use `coverage-templates/official-current-figure.yml` when a high-stakes answer depends on a current official figure.
5. Keep reusable workspace scripts unchanged.
