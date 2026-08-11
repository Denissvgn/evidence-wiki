# Publication Readiness

`scripts/publication_readiness.py --format json` is the local-only publication
gate for a research workspace. It is stricter than `workspace_status.py`: a run
can be operationally complete while still producing `no_ship`,
`blocked_on_sources`, or `attention_required` for public claims.

The command performs no network work and reports `network_io_executed: false`.
It reads existing artifacts only: workspace status, lint results, coverage
manifests, answer export records, discovery candidates, optional citation
verification JSON, and workspace files scanned for high-risk secret patterns.
The JSON report's current `schema_version` is `"1.0"`.

The report also embeds the shared `workspace_health` document used by doctor,
smoke, status, and lint. `invalid` health exits `2` with a machine-readable
`no_ship` report; `publication_blocked` health exits `1` and cannot be rounded
into `ship`. `degraded` means an optional capability is absent and remains
visible without masking any required finding. Stable finding codes and bounded
remediation are identical across the JSON and text-capable surfaces.

## Verdicts

- `ship`: local artifacts satisfy the publication gate.
- `no_ship`: a public claim would be unsafe or unsupported.
- `blocked_on_sources`: explicit missing evidence or failed required coverage
  facets block publication.
- `attention_required`: a human must review the workspace before a final
  publication verdict.

## Reason Categories

Reports group reasons under stable keys: `coverage`, `source_quality`,
`discovery_quality`, `citation_identity`, `currentness`, `curation`, and
`safety`.

### Human Review

A question whose answer requires manual review is a `safety` no-ship reason
until the review is recorded. The gate reads the aggregate `human_review` block
of the answer export, so both reviewer topologies satisfy it identically: an
in-workspace `question_resolve.py approve`, or per-policy
`question_resolve.py review --verdict accepted --review-ref …` collected in a
host's own approval queue. Every declared policy must be accepted before the
question leaves `human_review`, so a partially reviewed question still blocks.

`review.escalation_scope` does not change this. Scoping the escalation to the
question changes which *workspace verdict* a pending review produces; it never
lets an unreviewed answer publish.

The reason fires for a question in `human_review`, and for an `answered`
question whose required review was never recorded. It does not fire for a
question a rejected review returned to `open`: that question has no answer to
review, and it independently holds the workspace verdict at `in_progress`, so it
cannot reach `ship` regardless.

A recorded review also settles the coverage policy it names. Answer export
re-evaluates policies live, so a policy that needs a person keeps returning
`manual_review` however the review went — reading that as an open item
regardless would hold every workspace carrying such a policy at
`attention_required` permanently, and leave the review unable to satisfy the
gate it was collected for. The gate therefore matches each `manual_review`
verdict against the question's recorded `human_reviews` entries, per policy id,
and only an `accepted` verdict settles one. A policy with no entry, or one whose
review was rejected, is still an open item; and no review settles a policy that
returned `fail` or `contradicted` — accepting a policy is not licence to publish
evidence that failed it.

Standards coverage policies fail closed through the same `coverage` and
`currentness` paths. Common standards no-ship reasons include
`standard_reference_missing`, `standard_edition_missing`,
`standard_status_withdrawn`, `standard_status_superseded`,
`standard_status_draft`, `standard_replacement_unresolved`,
`registry_terms_unknown`, `registry_metadata_stale`,
`product_requirement_guidance_not_legal_authority`, and
`harmonised_standard_ojeu_reference_missing`.

## Commands

```bash
python3 scripts/publication_readiness.py --format json
python3 scripts/publication_readiness.py --format json \
  --citation-verification runs/<run_id>/evaluation/citation-verification.json
python3 scripts/publication_readiness.py --format json bundle --run-id <run_id>
```

The bundle is written to `runs/<run_id>/evaluation/` and includes
`status.json`, `publication-readiness.json`, `export.json`, `lint.json`,
`citation-verification.json`, `candidate-summary.json`, and
`source-request-summary.json`. Supervisor reports should cite these files rather
than chat logs.
