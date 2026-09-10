# Capital markets

This optional guidance pack organizes issuer facts, market observations, strategy hypotheses, and contradictory or risk evidence. Select it with `evidence-wiki init --domain-pack capital-markets` or the normal pack lifecycle. Its assets contain declarative rules and prose; it does not enable providers or add dependencies.

Use the four coverage templates to ask bounded questions. Issuer questions supply `metadata.issuer_id`; price questions supply `metadata.currency` and explicitly describe the listing universe. The 48-hour retrieval rules are configurable research defaults. Retrieval recency alone does not establish publication time, historical availability, or current trading suitability. Use the shared temporal contract when a cutoff matters.

The `market_evidence/v1` intake profile accepts delegated, checksummed SEC company-concept and Alpaca stock-bars slices. The host acquires and sanitizes data and supplies independent usage authority. See the workspace's `docs/market-evidence.md` for the delivery contract. A provider policy reference records evidence of terms; it grants no permission by itself.

The pack declares human review for listing identity, hypothesis evaluation, validity, and contradictions. A price currency match does not prove that every listing matches the question. Review raw versus adjusted series, incomplete or delayed feeds, inactive listings, filing amendments, and unresolved exclusions before drawing conclusions. Preserve negative results and exact units, cite structured values, and run calculations in deterministic external tools.

Refresh uses the same conflict-aware lifecycle as other packs and preserves local configuration choices. Scientific, legal, engineering, and other workspaces can use the shared temporal, execution, permission, and snapshot APIs without adopting this pack or providing market-specific metadata.
