# Academic Replay Fixtures

Offline provider responses used by the academic replay and provider-contract
tests. The tests build temporary workspace skeletons and inject these responses
into the arXiv and OpenAlex transports; no network access or provider credentials
are used.

- `arxiv-version-conflict-feed.xml` retains a requested-work/different-version
  negative control.
- `openalex-provider-contracts.json` retains duplicate/ambiguous title identities,
  an unknown raw license term, and a missing-DOI identity conflict.
