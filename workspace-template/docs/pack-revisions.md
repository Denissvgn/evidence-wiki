# Pack revisions and research impact

Use revisions to improve the same installed pack while retaining local evidence.
A structurally valid merge does not certify domain meaning or existing answers.

## Prepare and apply

1. Describe the evidence failure and intended requirements. Use `pack derive`
   with `kind: revision`, then `pack qualify`, `pack freeze-cases`, `pack assess`
   and `pack accept` to register an explicitly selected local revision.
2. Read `pack schemas` and `pack revision-status --target WORKSPACE --catalog DIR`.
   Catalog entries are candidates; installation authority remains in the workspace.
3. Plan with `pack revision-plan --target WORKSPACE --catalog DIR --id REVISION
   --rationale TEXT --output PLAN.json`. A caller-owned `--path PACK` is also
   supported, with structural validation and no inferred assessment. Save the plan
   outside the workspace and candidate. Unresolved three-way conflicts write nothing.
   Repeat `--keep-local config:/...` or `--accept-pack file:...` only for reported
   conflicts. First attachment and pack renaming are not refresh operations.
4. Review the declaration/file differences, affected counts, removed identifiers,
   effective local overrides, copied coverage and answer bases. Prose changes have
   uncertain semantics and conservatively affect all questions. Old strict reviews
   have a workspace-wide basis and need fresh review after pack bytes change.
5. Apply with `pack revision-apply --from-file PLAN.json`. Input, candidate and
   qualification identities are rechecked by the existing refresh transaction.
   Active runs, claimed questions, managed actions and pending computation effects
   prevent revision. Missing legacy metadata does not authorize rebinding old work.
   Finish or explicitly abandon old work through its controller first. Use
   `run-controller abandon` for a stale child and `orchestrate abandon` with its
   owner and a reason for a parent; unresolved submissions must be recovered first.

The existing `pack refresh` path also records impact and enforces revision holds.
Its backups, rollback and interrupted-transaction recovery remain authoritative.
The transaction retains the affected original question and coverage bytes in its
bounded revision history before later work can edit them. Migration archives
have checked content identities; a missing or changed archive does not clear a hold.
For an interrupted refresh, retry the saved plan: recovery restores the original
transaction before validating and replanning. Inspect a conflict before retrying;
never edit the lifecycle journal or state by hand.

## Reevaluate research

`pack revision-status --target WORKSPACE` shows the reason, qualification and
remaining coverage work for each accepted revision. Installation success is
separate from coverage, review, computation and final release eligibility.
Use `--evaluate` for explicit read-only computation and dependent checks. Applying
a revision does not implicitly run declared calculations. Pending obligations
carry across later revisions until an explicit migration resolves them.

Use `pack reevaluate --target WORKSPACE --from-file MIGRATION.json`. Its
`evidence-pack-reevaluation/v1` document names the current relevant `revision_id`,
question `slug`, rationale, an explicit coverage `template`, every
`retired_facets` identifier, `request_replacements` and `computation_migrations`.
Use empty objects when no identifiers were removed. Template fields are the normal coverage-template
fields. Include criteria only: no accepted evidence, verdicts or review approvals.

Migration archives the prior question and coverage bytes under
`runs/pack-revisions/`, reopens the question through its owner, keeps evidence for
unchanged facets, and resets evidence for changed requirements. Prior approvals
remain in history and do not approve the reopened answer. Removed policy IDs must
be explicitly replaced in the template. `request_replacements` maps each removed
request ID linked to the question to an explicitly created request with a current
kind, the same scope and question link. Historical requests remain intact.
Reevaluate coverage with its owner, supply missing evidence, and obtain the current
human or authenticated independent review. Missing signing authority remains a
blocker. Historical answers, sources and signed receipts are never erased or
reinterpreted as current approvals.

Computation differences include selectors, expressions, arithmetic, invariants and
cadence. Changed result identities invalidate calculated claims and their dependent
claims. Inspect `computation check`; explicitly invoke `computation write` only when
authorized, retaining the original output/receipt history. Refresh and reevaluation
do not dispatch schedules or create warning questions. Removed or renamed
definitions require `computation_migrations`: map every removed definition path
(for example `/graphs/estimate`) to a current definition path or `null` for an
explicit retirement. Renames are never inferred from similar expressions.

Finally use `agent research-export` or the selected publication owner. A passing
coverage migration alone cannot establish a successful research outcome. Future
projects can explicitly select the registered revision without changing any other
workspace or publishing a pack. All local records are artifact observations, not
protection against a caller who can edit the workspace or a guarantee of truth.

## Refusals and recovery

| Error code | Meaning | Action |
| --- | --- | --- |
| `COVERAGE_REVISION_REQUIRED` | The question still depends on superseded pack criteria. | Inspect `pack revision-status` and explicitly migrate affected coverage before a new answer and review cycle. |
| `DOMAIN_PACK_REVISION_CONFLICT` | Pending work, changed inputs or an unresolved local conflict prevents revision. | Preserve the original work and criteria, finish or explicitly abandon it through its owner, and replan against current inputs. |
