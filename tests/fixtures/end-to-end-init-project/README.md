# End-To-End Init Fixture

This fixture represents the minimal-preparation workspace bootstrap flow.
`prompt.md` records the short user request, and `workspace-init.yml` records the
reviewable setup profile an agent inferred from that request.

Tests use the profile as the deterministic handoff artifact. They do not parse
the prompt, fetch sources, run codebase-analysis adapters, or edit
`research.yml` after initialization.
