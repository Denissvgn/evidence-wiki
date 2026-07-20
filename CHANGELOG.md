# Changelog

## Unreleased

## 0.2.0 - 2026-07-20

- Bump the managed workspace starter to `0.5.0` and package the orchestration
  controller and shared provider registry as upgrade-managed scripts.
- Add durable parent orchestration sessions with model-neutral work orders,
  Codex and Claude managed runners, restart-safe status, and verified result
  submission across immutable bounded research runs.
- Add explicit discovery/acquisition provider flags, fail-closed discovery
  provider validation, and request-backed arXiv/OpenAlex academic discovery.
- Treat legacy `legal`, `authors`, and `companions` discovery entries as
  deprecated strategies rather than provider authority; migration is manual
  because upgrades preserve `research.yml`. Enabled discovery with no concrete
  provider is now a HIGH configuration error.
- Document the empty-source autonomous workflow, source-provider permissions,
  runtime credentials, and the local-files-only alternative.

## 0.1.0 - 2026-07-13

Initial standalone release of EvidenceWiki.

- Verifiable, provenance-backed research workspaces with deterministic question,
  source, citation, and publication-readiness workflows.
- The `evidence-wiki` CLI for workspace creation, upgrades, health checks,
  question intake, answer export, domain-pack validation, fleet status, and MCP
  serving.
- Reusable workspace template, domain packs, orchestrator guidance, and a
  synthetic worked example.
- Python 3.10+ support on Windows, macOS, and Ubuntu under the MIT License.
