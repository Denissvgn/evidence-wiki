# Citation Verification

`scripts/verify_citations.py` checks academic citation identity from recorded
workspace metadata. Default mode is local-only; live mode is explicit and still
uses the acquisition provider allow-list.

```bash
.venv/bin/python scripts/verify_citations.py --format json
.venv/bin/python scripts/verify_citations.py --format json --live --provider arxiv
.venv/bin/python scripts/verify_citations.py --format json --live --provider openalex
```

Provider-backed academic sources must carry real title, author, year, and
identifier metadata before live verification can return `verified`. Empty
authors on a provider-backed source now return `insufficient_metadata`; rerun
`fetch_sources.py openalex enrich`, `source_inventory.py --report`, and
`normalize_sources.py --all` to refresh older normalized records. Result records
include `title_source` when normalized frontmatter records whether the compared
title came from provider metadata or PDF inference.

## Author Name Forms

Live author comparison stays identity-strict but canonicalizes common provider
name forms before comparison:

- Unicode NFKD folding removes diacritic variance.
- `family, given` provider forms are inverted before comparison.
- Periods in initials are ignored.
- Token-set equality handles order swaps such as `shang yang` versus
  `yang shang`.
- Same-family names can match when every given-name token is equal, an initial
  of the other side, or a prefix/suffix affix of length at least four.

The match is injective: every local author must consume a distinct provider
author. Given-name-only matches are never accepted, and incompatible same-family
names still return `mismatch`. Verification JSON includes
`comparisons.authors.matches` so reviewers can audit which local author matched
which provider author and which rule fired.

## OpenAlex Index Defects

The verifier uses the stable result vocabulary
`verified | mismatch | not_found | skipped_no_live | insufficient_metadata`.
OpenAlex upstream defects are expressed as reason codes and comparison details,
not new result values.

- `openalex_title_version_lag` returns `verified` when recorded identifiers and
  canonical authors match but OpenAlex has stale title or year metadata.
- `openalex_identity_conflict_recorded` plus
  `openalex_identity_quorum_verified` returns `verified` when enrichment
  recorded an OpenAlex wrong-work conflict and DOI resolution corroborates the
  arXiv identity.
- `openalex_identity_conflict_unrecorded` and
  `openalex_identity_conflict_uncorroborated` remain `mismatch`.

This preserves strictness: silent divergence, absent records, malformed ids,
empty provider-backed author metadata, and genuinely different works still fail
the ship gate.
