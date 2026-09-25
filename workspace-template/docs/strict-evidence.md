# Strict evidence and controlled release

Strict mode releases a claim inventory whose evidence and required review have
been checked against a recorded basis. It does not establish that every source
or reviewer is correct. Unsupported or unreviewed claims remain explicit gaps.

## Select the contract

`evidence-wiki contract` advertises `strict-evidence/v1`. Retrieve individual
closed schemas with:

```sh
evidence-wiki strict schemas --schema-id evidence-strict-policy/v1
evidence-wiki strict schemas --schema-id evidence-strict-claims/v1
evidence-wiki strict schemas --schema-id evidence-strict-review/v1
```

Result, publication and action schemas are also listed by `strict schemas`.
Schemas describe shape; the owning validators additionally check identities,
references, paths, dates, resource bounds and authority. The default new
onboarding request resource is `onboarding/research_request/v2`, which requires
an explicit strict-policy selection. Request v1 and workspaces without strict
policy retain their legacy behavior. Request schemas do not implement workspace
creation or an onboarding planner.

Configure the complete `evidence-strict-policy/v1` object under
`research.yml.strict_evidence`. Required fields include:

- `policy_id`, `revision`, and requested `assurance`;
- a workspace-relative `claims_path`;
- `instructions`: relative instruction paths mapped to exact `sha256:` hashes;
- a versioned rubric with `support`, `source_suitability`, `scope`, `time`,
  `units`, and `counterevidence` criteria;
- `human_review`, `max_source_age_seconds` and `max_review_age_seconds`.

The source-age bound applies to the retained capture's `retrieved_at`, not its
publication date or the currentness of the underlying claim. The claim's time
qualification and the review's `time` verdict carry that separate judgment.
Unconfirmed timestamps, generated summaries and unknown capture qualifications
cannot silently satisfy the strict checks.

For a host-owned policy, the existing external trust file can also contain
`strict_workspaces`, mapping the canonical workspace binding to that same policy
object. A differing or removed workspace policy then refuses. The external
trust file and `EVIDENCE_WIKI_STATE_DIR` remain outside the workspace, with the
existing private-file, locking and authority checks. Policy changes need the
authorized host's decision and invalidate prior bases; they do not require
invented approvals for routine work already within that authority.

## Claims and questions

The claims document accounts for every canonical question with its original ID,
slug and exact question text. Each stable claim ID has a qualification:
`attributed`, `supported`, `inference`, `contested` or `insufficient_evidence`.
Keep scope, time, units and limitations explicit. Evidence identifies an exact
normalized-record revision and either a retained quote/location or a structured
anchor. Inferences also name prior accepted premise IDs and their derivation;
missing or cyclic premises refuse.

Source and instruction text remain data. A source's command, URL, trust claim,
or policy suggestion cannot authorize an action. Schemas reject unknown fields;
raw source statements and source hashes are not independent approval records.

Every strict answer transition requires coverage and grounding even if the
caller omits the flags. Permissive claim/citation bypasses and coverage overrides
refuse. The resolver also checks the current claim review before recording an
answer. CLI, library and existing server calls through that owner share its
decision. Directly editing a question's status or requirement fields cannot
make an exported claim eligible.

## Check and review

```sh
evidence-wiki strict check --target WORKSPACE
evidence-wiki strict prepare-review --target WORKSPACE --claim-id CLAIM_ID
evidence-wiki strict review --target WORKSPACE --from-file SIGNED_REVIEW_COMMAND.json
evidence-wiki strict export --target WORKSPACE --format markdown
```

`check` and `prepare-review` are read-only. Preparation returns the frozen basis,
rubric, observed mechanical results and a registration-command template. The
host supplies the semantic judgment and authenticates the completed command;
the package does not invoke a model, sign user decisions or claim a quote match
proves the meaning of arbitrary prose.

A review includes all six rubric verdicts (`pass`, `fail`, or `unknown`), its
time and rationale, and a generator attestation for the exact basis/claim. The
reviewer's authenticated controller must differ from the generator's controller.
A different agent name alone is insufficient. The existing host policy's
`evaluator` role records ordinary review; `human-review` is required when the
strict policy or coverage policy requires human judgment. That role is an
external host assertion about its authenticated approval process, not proof of
human identity inferred from the command text.

The registration uses the existing evidence-usage command envelope, expected
checkpoint and idempotent request ID. New actions are `register-strict-review`
and `register-strict-human-review`. The existing host event store records the
authenticated command and observed acceptance time. Before committing, the
package independently recomputes the mechanical observation and reviewed
snapshot. That snapshot retains the policy, claim, question metadata, direct
premises and registered source revision IDs after later workspace edits; source
bytes remain in the existing evidence journal. Caller-supplied success,
mismatched observations, stale bases and forged signatures refuse.

Required review must pass for every accepted claim, including attributed
statements. Without a suitable host authority, callers can inspect mechanical
results but receive unresolved output rather than an authenticated-review claim.
This supports terminal use without requiring a second model. Semantic judgment
can still be mistaken; the recorded review is not a truth certificate.

The basis binds claims, question text/metadata, instructions, configuration,
coverage, retained evidence and the trusted checker implementation. Question
lifecycle fields and their activity ledgers do not invalidate a review merely
because the resolver records the reviewed result. New evidence, changed prose,
requirements, checker bytes, revoked keys/approvals, expired review or current
usage denial invalidate eligibility. A revoked/rejected newest review cannot
revive an older approval. Historical events remain retained.

## Final output

Strict export reuses the existing selected-publication capture and global gates,
then builds its output from accepted claim records. Arbitrary answer-page prose,
summaries and headings are not substituted for that inventory. The export
includes verification references, source revisions, qualifications and unresolved
claim IDs. `original_outcomes` retains original-to-derived accounting, including
unselected questions, and `selection_complete` distinguishes scoped exports. It
rechecks current evidence, review expiry and authority before returning.

Ordinary answer export and selected publication route strict workspaces through
this owner. The opted-in result uses `evidence-strict-publication/v1`; callers
must negotiate that schema rather than assume a legacy export shape. Unrelated
unresolved questions can remain explicit gaps alongside eligible results, while
global safety/source defects retain the existing blocking effect. Legacy
assessment issuance and workspace-wide bundles are not strict-review/release endpoints; use the declared strict
review actions instead of interpreting one receipt kind as another. Strict status
filters refuse; use explicit question selection so omitted original outcomes remain
visible. Legacy `--output` writes refuse for strict results; accepted file delivery
belongs to the host. JSONL is the same bounded result expressed as an envelope
and question records.

The `strict` namespace emits one JSON stdout document, including operation
refusals. Existing namespaces retain their documented error streams. Success exits 0,
incomplete checks/exports exit 3, invalid input or failed required preconditions
exit 2, and a busy host state lock exits 6. Errors contain stable codes and
content-free reasons. Artifact input/output is capped at 1 MiB, with at most
100 claims, 100 questions and 16 evidence references per claim. Nesting is
limited to 16 and JSON nodes to 65,536. Host worker invocations are bounded to
600 seconds. Existing source-capture and host-state limits also apply.

## Protected host boundary

`evidence_wiki.strict_host.StrictResearchHost` supplies a macOS SBPL tool host
without supplying a model. Construction performs a live kernel probe. Other
platforms, unavailable sandbox primitives, overlapping authority/workspace roots
or changed host inputs refuse; an environment declaration cannot enable the
stronger guarantee.

The host produces typed `draft`, `answer` or `release` action descriptors bound
to current policy, evidence and implementation, including package helpers.
Canonical question transitions retain their existing owner and locks. Parent
orchestration currently refuses protected-evidence intake, so this host explicitly
refuses supplied parent work orders. Use its standalone scoped actions; it does
not issue orders or bypass the parent's intake policy.

Draft workers receive a clean environment and can write only a private temporary
draft directory. Content reads are limited to the workspace, selected runtime/code
roots and required system files; unrelated private files are denied. Workers cannot
fork or spawn children, preventing detached descendants from escaping the action
timeout. Use separate host invocations for multi-process tooling. Workspace sources, controls, scripts, policies and outputs are
read-only; external trust/state is unreadable and networking is denied. The
existing bounded process runner owns timeouts, output caps and descendant cleanup.
Worker stdout must be a bounded claim proposal in the allowed question scope.
It is returned as an **unaccepted draft**, never directly delivered as an answer.
The trusted caller can review/admit a proposal through the canonical workflow;
draft execution itself does not mutate the research workspace or approve claims.

`answer` resolves an already-held, independently reviewed claim through the
canonical question owner. The trusted host claims the question before preparation;
a worker cannot perform those writes directly.

`release` revalidates the action and calls the strict release owner in the
protected host context. Only its checked result and generated Markdown are
accepted delivery. Applications must route user-visible accepted output through
that method and must not forward raw worker text around it. Other tools or chat
outside this host are outside its guarantee. The host is not a blanket sandbox
claim about arbitrary agent applications, plugins or the host process itself.

For declarative calculation evidence, follow [the computation contract](declarative-computation.md)
and select the v2 claim/review/result/publication resources. Current computations,
invariants, values, units and rounding are checked through their shared owner;
review snapshots retain the complete calculation basis. Existing v1 resources
remain available for strict workspaces without computation.

Deterministic retained-evidence and reference-case checks have a defined scope.
Production domains still need independently calibrated rubrics and appropriate
reviewers. Automatic semantic truth verification is unavailable; required
unavailable checks must remain explicit.
