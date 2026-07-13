# Urban Heat Resilience Example Workspace

This is a public-safe worked example for the EvidenceWiki workspace template. It uses
synthetic urban heat resilience evidence, reserved `example.org` URLs, and local
placeholder files so it can be redistributed without private pilot data or
machine-specific paths.

## What To Inspect

- `raw/`: immutable placeholder evidence.
- `sources/manifest.jsonl`: deterministic source inventory.
- `sources/normalized/`: agent-readable normalized source records.
- `wiki/`: maintained notes, claims, synthesis, questions, decisions, and an
  example output that cite source IDs.
- `index.md`: static catalog of the maintained example pages.

## Validate

Run from this workspace root:

<!-- docs-command: executable id=public-example-offline-sequence -->
```bash
python3 scripts/smoke_validate_workspace.py --format text
python3 scripts/lint.py --format json
python3 scripts/workspace_status.py --format json --no-cache
python3 scripts/query_index.py --scope all --limit 10 --format json "cooling"
python3 scripts/verify_citations.py --format json
python3 scripts/verify_quotes.py --slug maintenance-cost-evidence-gap --format json
python3 scripts/publication_readiness.py --format json
python3 scripts/export_answers.py --format json
```

Smoke validation and lint should pass without warnings. Status, query,
citations, and export remain offline and machine-readable. The example
deliberately keeps the maintenance-cost question open, so quote verification
and publication readiness retain a nonzero, actionable result instead of
manufacturing publication completeness.
