# Automatic release qualification

`tools/decide_release.py` is a checkout maintainer tool that freezes evidence
obligations, produces an automatic decision and replays that decision against
current original owners. It does not publish a package or authorize release.
The tools are not part of the installed product API.

The trusted host selects a Python factory and original evidence sources before
freezing. Candidate bytes use `tools.verify_acceptance_paths.candidate_manifest`
and the canonical `tools._release_decision.digest`. Host identity binds the
factory's exact source bytes, entry point, local interpreter, Python version,
system and machine. This is a local execution binding, not remote platform or
OS isolation attestation. The factory and its imported owner implementations
are trusted host code; evidence JSON cannot choose executable readers. Imported
dependencies and private configuration must be controlled by that host and
validated by the original owners.

## Freeze and run

The configuration is the keyword input to
`tools._release_decision.freeze_plan`: `candidate_sha256`, `profile`,
`required_sources`, `budgets`, `scope`, `frozen_at`, `expires_at` and `judge`.
Times are UTC ISO strings. Digests are lowercase bare SHA-256 hex strings.
Each source supplies `category`, `owner_ref`, `raw_sha256` (exact original
bytes) and `source_head_sha256` (the original owner's externally anchored
current head). Retain anchors outside worker-controlled evidence. A host
identity and reserved `scope.runtime` are added by the CLI.

At least one source is mandatory for each category: `conformance`, `semantic`,
`integration`, `cost`, `drift`, `uncertainty`. Up to 32 sources are permitted.
All five budget keys are required: `elapsed_seconds`, `model_calls`,
`tool_calls`, `tokens`, `cost`. Elapsed time has a positive cap; other caps may
be `null` for unqualified metrics. Counts are integers. Time and cost accept
nonnegative numbers or exact decimal strings. Required unknown usage is
inconclusive; a known subtotal above its cap requires revision even when other
usage is unknown. Multiple cost owners must represent disjoint inventories,
since their usage is summed. Frozen scope identifies those inventories and
explicit exclusions; an excluded provider charge is not a whole-host total.

```sh
.venv/bin/python tools/decide_release.py freeze \
  --config /private/host/config.json --factory /private/host/readers.py:make_readers \
  --timeout-seconds 300 --output /private/host/plan.json
.venv/bin/python tools/decide_release.py decide \
  --plan /private/host/plan.json --expected-plan EXTERNAL_PLAN_SHA256 \
  --factory /private/host/readers.py:make_readers --output /private/host/decision.json
.venv/bin/python tools/decide_release.py replay \
  --plan /private/host/plan.json --expected-plan EXTERNAL_PLAN_SHA256 \
  --factory /private/host/readers.py:make_readers --report /private/host/decision.json
```

The factory is a canonical regular `.py` file of at most 64 KiB, without group
or world write permissions. Exact pinned source is compiled directly, bypassing
cached bytecode. Input JSON is bounded to 1 MiB and rejects duplicate keys and
nonfinite numbers. Outputs are exclusive new files; existing evidence is never
overwritten. Initialization and all reads share one frozen POSIX host deadline
of at most 900 seconds and the plan's remaining lifetime. Stdin is closed;
factory/reader stdout or stderr refuses the machine protocol. This controls
cooperative trusted callbacks and does not sandbox hostile Python code or undo
effects. JSON stdout and exit zero indicate `qualified`; other outcomes exit 2.

## Trusted original-owner readers

`make_readers(plan)` returns `OwnerReader(category, owner_ref, read)` instances.
Each `read()` returns `OwnerEvidence` after calling the existing owner's
authentication and replay APIs. Those APIs must check current candidate,
configuration, source head, eligibility, coverage and freshness. Do not derive
authentication from a serialized `authenticated` flag, a role label, prose,
the evidence class alone or an unsigned summary. Reader failures and unknowns
must stay conservative.

`OwnerEvidence` contains `raw`, `source_head_sha256`, `candidate_sha256`,
`host_sha256`, `scope`, `observed_at`, `expires_at`, `evidence_class`,
`projection` and `citations`. Its projection has exactly four fields:

- `status`: `pass`, `critical_failure`, `actionable_failure` or `unknown`.
- `metrics`: the five nullable usage keys for cost owners, otherwise `null`.
- `judge`: an exact current qualified judge selector, otherwise `null`.
- `business`: original scoped outcome observations, otherwise `null`.

`OriginalCitation(pointer, value)` identifies an exact JSON pointer and value
in the authenticated original raw document. The engine checks exact citation
equality, byte/head pins, candidate, host, scope and live windows. Trusted
owners remain responsible for the semantic relevance of citations and the
meaning of their projections, including coverage and uncertainty. A generic
engine cannot authenticate an arbitrary provider simply because a callback
returns the right class. Every required owner is read twice, and closing
projections/citations must match the opening reads. Missing, changed, invalid
or expired sources cannot become passes. `EvidenceUnavailable` is inconclusive;
only an explicit trusted `ExternalBlocked` observation represents an unavailable
external dependency.

For genuine retained unattended fixture qualification, a private host factory
can call `tools._release_decision_owners.unattended_readers(plan,
report_path=..., observer_key=..., expected_report_sha256=...)`. Supply the key
through the host's private custody, not worker inputs. Set `scope.kind` to
`unattended_original_roles` and retain exactly the six category references to
that same original report/head. The adapter delegates to the original
qualification replay without dispatching workloads, binds raw bytes to the
authenticated report, authenticates its original execution deadline and
re-reads the report. Its elapsed budget covers the five original role
intervals; wider host/provider usage stays unknown. Fixture observations are
never promoted to real semantic quality or platform isolation.

## Frozen decisions and claim scope

The executable policy is pinned in the plan and must match the installed
policy exactly. Its precedence is `reject`, `revise`, `inconclusive`,
`blocked_external`, `qualified`. Critical native failures reject; actionable
failures or exceeded budgets require revision. Missing/unknown/ineligible
evidence is inconclusive and takes precedence over an external block.
Qualification requires all six current owners to pass with all required
budgets known and within bounds. Observed native failures retain their stronger
precedence even if evidence is later withdrawn. Thresholds cannot change during
decision or replay.

`technical_fixture` qualifies only `fixture_mechanics`. Its semantic source
must be simulated and does not claim open-ended quality. `bounded_semantic`
requires `agent_adjudicated` semantic evidence and a qualified judge matching
the frozen `domain`, `rubric_sha256`, `model`, `settings_sha256`,
`controller_sha256`, `profile`, plus current qualification and expiry. An
ineligible judge cannot establish either positive or negative semantic quality.
Judge qualification must come from the original protected evaluator owner;
model identity, a grading receipt or a signed success label alone is insufficient.

Reports preserve `deterministic_verified`, `agent_adjudicated`, `simulated` and
`externally_observed` evidence classes and provide reason codes with original
citations. Optional `business` sources retain only exact cited native
`outcome_observations` rows with original source, period, denominator, coverage,
exclusions and batch commitments. Missing business evidence does not gate
technical qualification. Customer adoption and payment remain scoped original
observations; inferred ROI, willingness to pay, saved human effort or customer
proxies are not produced. Top-level business value and ROI remain unobserved.

The report digest detects altered output. Replay re-reads current original
owners under the externally retained plan hash and requires the same canonical
decision bytes. A retained `qualified` report is inert evidence, not enduring
authority: consume it through current replay after source/configuration changes
or elapsed time. `authorized`, `evidence_accepted` and `release_published` always
remain false. Publication needs its separately authorized host workflow.
