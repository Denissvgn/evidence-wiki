# Changelog

## Unreleased

- Scope `human_review` escalation to the question instead of the workspace,
  behind a new optional `research.yml` `review:` section. Under
  `review.escalation_scope: question`, a question awaiting human review no
  longer flips the readiness verdict to `attention_required`: it is reported as
  `readiness.questions_awaiting_review`, orchestration keeps issuing work for
  the other questions, and a workspace whose only remaining work is pending
  reviews reports `in_progress` with the structured code
  `questions_awaiting_review_only` rather than `complete`. The default
  `review.escalation_scope: workspace` preserves 0.2.4 semantics, and
  `compatible_research_yml_contract` is unchanged at `0.1` because the section
  is optional and additive.
- Add `question_resolve.py review --slug S --policy P --verdict
  accepted|rejected --reviewed-by PRINCIPAL [--review-ref REF] [--note TEXT]`,
  which records a human review collected outside the workspace against one
  coverage policy at a time. Entries append to a `human_reviews` frontmatter
  list; the question becomes `answered` once every declared policy is accepted,
  writing the same review fields `approve` has always written, and a rejection
  returns it to `open` with the reason retained. `approve` is unchanged for
  callers and now accepts every still-pending policy through the same writer.
  Publication readiness accepts recorded external reviews exactly as it accepts
  `approve`, and still refuses to ship an answer whose required review is
  unrecorded.
- Report a review queue that has stopped moving: under
  `review.escalation_scope: question`, lint emits HIGH
  `question_human_review_stale` once a question has awaited review longer than
  `review.max_pending_review_hours` (default 168, `null` disables), which
  returns the workspace to `attention_required` through the existing
  lint-to-verdict path. A parked question with no usable
  `human_review_requested_at` emits MEDIUM `question_human_review_undated`.
- Surface the review queue for reviewers and hosts: `workspace_status.py` text
  output and MCP payload carry the awaiting-review count and slugs,
  `question_status.py` records carry `human_review_requested_at` and
  `human_review_pending_policies` (rendered as age and pending-policy count in
  text), and `fleet_status.py` and `run_report.py` report the counter per
  workspace. `export_answers.py` exports the per-policy `human_reviews` entries
  for audit.
- Declare the `human_review` question status and the review frontmatter fields
  in the starter `research.yml` frontmatter rules, so a parked question no
  longer draws a spurious `frontmatter` lint finding in a stock workspace.
- Refactor managed Codex and Claude execution behind a closed adapter registry,
  and document OpenCode, Pi, and other harnesses as external-protocol clients.
- Bump the reusable managed workspace starter to `0.5.5`, including the
  canonical `CLAUDE.md` instruction pointer in its required asset manifest.

## 0.2.4 - 2026-07-21

- Treat broken or partially initialized pypdf import-spec lookups as a missing
  required dependency in workspace health checks, returning a typed finding
  instead of raising an inspection error.
- Restore DNS and HTTPS for provider-enabled managed Codex actions on
  Linux/WSL2, including symlinked `/etc` layouts, by adding bounded read-only
  system resolver configuration to the named permission profile, while keeping
  offline actions unchanged and rejecting unexpected external resolver
  targets.
- Bump the reusable managed workspace starter to `0.5.4`; `evidence-wiki
  upgrade` refreshes its health-check and orchestration guidance while
  preserving project evidence and configuration.

## 0.2.3 - 2026-07-21

- Install pypdf as the portable, canonical PDF normalization backend so a
  normal wheel installation can process PDF-only evidence on macOS, Linux, and
  Windows without an undeclared system executable.
- Treat Poppler `pdftotext` as an explicit optional compatibility backend,
  document the pip/system-package boundary and platform installation commands,
  and align doctor/workspace-health reporting with the actual backend policy.
- Pause and replay retryable blocked orchestration actions, while treating an
  audited candidate-specific `selected` to `failed` acquisition transition as
  one exhausted route so the controller can try the next candidate before it
  declares terminal `blocked_on_sources`.
- Reject acquisition fulfillment backed by failed, stubbed, explicitly
  unusable, or text-empty normalized evidence, and validate the request
  correlation supplied for definitive candidate-route failures.
- Harden Windows workspace locking and managed-run runtime handling so
  concurrent index builders and protected virtual-environment checks fail
  predictably instead of raising platform-specific permission errors.
- Bump the managed workspace starter to `0.5.3`; `evidence-wiki upgrade`
  refreshes the compatible managed scripts while preserving project evidence and
  configuration.

## 0.2.2 - 2026-07-21

- Canonicalize package-managed runner results by discarding descriptive
  `runs/orchestrations/` artifact references before submission, while keeping
  direct protocol validation strict and retaining fail-closed control-tree
  mutation detection.
- Pin managed workspace scripts to the Python interpreter that launched
  EvidenceWiki, disable Codex login-shell PATH rewriting, grant its external
  runtime read-only, and preflight its PyYAML/TLS dependencies inside an
  isolated read-only permission profile before creating a session.

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
- Bump the managed workspace starter to `0.5.2` with explicit parent-control
  ownership and recovery guidance for research, discovery, acquisition, and
  verification workers.
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
