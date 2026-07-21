# Changelog

## Unreleased

- Make managed Codex runtime visibility exact and portable: resolve the selected
  executable and official platform package outside the writable workspace,
  grant only its canonical runtime tree as read-only, and preserve the same
  permission profile for capability probes and worker actions. Correct the
  empty Claude MCP configuration document without weakening either runner's
  sandbox or approval policy.
- Require every research action to prove scoped question progress, enforce the
  remaining per-run question budget, keep review and acquisition bound to the
  persisted request/candidate IDs, and allow rediscovery after exhausted or
  unroutable candidates without mutating terminal child runs.
- Add durable pre-transport arXiv/OpenAlex request accounting, fail closed on
  corrupt run accounting, and derive status counters from discovery requests
  rather than acquisitions. GitHub and OpenAlex acquisitions now retain both
  request and candidate provenance for deterministic orchestration checks.
- Publish complete Draft 2020-12 schemas for orchestration sessions, work
  orders, results, and managed attempts while keeping the existing schema
  version map stable for compatibility.
- Keep exact pre-action question, request, candidate, manifest, raw, and
  normalized-evidence guards in protected controller sidecars, leaving public
  work orders bounded to 256 KiB. Validate sidecar identity and content before
  replay, reject every out-of-scope mutation, and accept acquisition only when
  fulfilled evidence is either an unchanged scoped match or a genuinely new
  provenance-linked source.
- Roll an exhausted immutable child run into a fresh child for remaining
  research, discovery, or acquisition work, including source-request budget
  exhaustion while actionable questions remain.
- Bump the managed workspace starter to `0.5.2`; the default upgrade refreshes
  managed scripts while preserving existing research configuration, evidence,
  and wiki content. Optional skills and documentation remain reviewable
  `--include` groups and are never replaced silently.
- Fail closed when a pending legacy research, discovery, candidate-review, or
  acquisition action lacks the phase-specific pre-action baseline needed for
  deterministic reconciliation. Preserve the old parent session and start a
  fresh orchestration; never reconstruct these baselines after worker execution.
  Active legacy runs without the new academic-provider accounting marker also
  require a fresh run so an unknown prior call count cannot reset to zero.

## 0.2.1 - 2026-07-21

- Fix the managed Codex result schema to use the supported Structured Outputs
  subset while retaining strict host-side validation of returned artifacts.
- Fail closed before managed execution when the runner cannot protect the
  host-owned parent orchestration tree. Codex requires its supported 0.138+
  permission-profile interface, and managed Claude execution is unavailable on
  native Windows. Claude isolation requires `bubblewrap` and `socat` on
  Linux/WSL2 or `sandbox-exec` and `touch` on macOS.
- Define the semantic baseline as bounded **tripwire-protected controls**:
  workspace contract/instruction files, `scripts/`, `skills/`, `docs/`, and the
  current parent session. Ignore timestamp-only drift and report exact
  workspace-relative changes without automatically restoring or rolling back
  operator-visible files. Keep `.git/`, `.codex/`, `.claude/`, `.agents/`, and
  workspace virtual environments, plus `runs/orchestration-guards/`,
  preventively read-only without adding those roots to the post-action
  tripwire snapshot.
- Add bounded `orchestration_attempt` records, private staged-result recovery,
  never-submittable quarantined results, and a durable control-repair marker
  without retaining prompts, transcripts, diagnostics, secrets, or absolute
  paths. Retain the guard outside the parent session at
  `runs/orchestration-guards/<orchestration_id>.json`.
  `CONTROL_ARTIFACT_TAMPERED` records the tripwire failure, and
  `CONTROL_REPAIR_REQUIRED` blocks managed resume until explicit review;
  acknowledgement requires the saved tripwire-protected-control snapshot and
  fails with `CONTROL_REPAIR_MISMATCH` when it still differs or
  `CONTROL_REPAIR_BASELINE_MISSING` when no trustworthy baseline survives.
- Make managed recovery checkpoint-first: use an accepted canonical result, an
  identical clean staged result, or then the same persisted action in a fresh
  worker. Deterministic submission and trusted-input fingerprints remain
  authoritative.
- Serialize each managed parent session for its full drive and return
  `ORCHESTRATION_ALREADY_RUNNING` before launching a competing worker. External
  protocol hosts must provide equivalent session-wide coordination.
- Refuse overlapping retained attempts with `ORCHESTRATION_LEASE_ACTIVE`, fail
  malformed or expired absolute leases with `ORCHESTRATION_LEASE_INVALID` or
  `ORCHESTRATION_LEASE_EXPIRED`, and cap each runner timeout to the lease's
  remaining lifetime.
- Strengthen discovery and candidate-review immutability checks with a bounded
  content digest for up to 10,000 raw files / 2 GiB plus the exact record count
  and content digest of `sources/manifest.jsonl` up to 32 MiB.
- Forbid daemons, hooks, background jobs, and detached subprocesses in managed
  work orders. Clean up the runner process group while documenting that
  untrusted process trees require an operator-controlled container or VM.
- Move new generated run reports to `runs/run-reports/`; existing
  `docs/run-reports/` files remain historical read-only inputs.
- Bind legacy pending actions to controller-owned static-input fingerprints on
  their first replay, and recompute authoritative verification/export outputs
  before accepting completion.
- Bump the managed workspace starter to `0.5.1` with explicit parent-control
  ownership and recovery guidance for research, discovery, acquisition, and
  verification workers.

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
