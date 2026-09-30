# Upgrade and adopt existing research

Installing EvidenceWiki changes the package in the selected Python environment.
Existing workspace scripts, raw evidence, notes, questions and pending work stay
under their existing owners. Choose one interpreter for a run and use it consistently.

Before changing an active workspace, read its installed instructions and current
run, source-request and pack-revision status. Finish or explicitly close active
work under its original controls. Preserve unresolved claims and partial deliveries;
an installation upgrade is not permission to replay or reset them.

For a workspace named `research`, preview the starter update:

```sh
evidence-wiki upgrade --target research --dry-run
```

After reviewing that preview, apply it explicitly:

```sh
evidence-wiki upgrade --target research
evidence-wiki doctor --target research --format json
evidence-wiki agent bootstrap --target research --format json
evidence-wiki agent next --target research --agent-id current
```

Ordinary upgrade refreshes starter-owned scripts and version metadata. Optional
docs and skills are separate selections; local edits can require a conflict decision.
Keep the preserved replacement history. Research configuration and domain-pack
criteria are not silently adopted from a new installation.

## Refresh native HTML classifications

Use an installed build that understands native HTML classification revision 2,
then upgrade the workspace tooling through the commands above. Installing a
package does not replace copied scripts, and upgrading scripts does not migrate
normalized records. An older package's readers are not qualified to enforce a
revision they do not understand. A matching entry-script hash is only a check
of that file, not proof that every helper is present or that records were refreshed.

Select the source IDs returned by inventory. Inspect each retained source, then
preview and apply its normalization with the interpreter from the same environment
as `evidence-wiki` (written as `PYTHON` below):

```sh
evidence-wiki agent source-status --target research --source-id SOURCE_ID
PYTHON research/scripts/normalize_sources.py --project-root research --source-id SOURCE_ID --dry-run --format json
PYTHON research/scripts/normalize_sources.py --project-root research --source-id SOURCE_ID --format json
evidence-wiki normalize verify --target research --source-id SOURCE_ID --format json
evidence-wiki agent source-status --target research --source-id SOURCE_ID
```

Repeat `--source-id` for an explicitly selected batch. Missing or older native
classification metadata, including revision 1, triggers refresh even when the raw fingerprint and global
normalizer version are unchanged. The resulting revision records classification,
not semantic approval. Current replay skips unchanged records, and selected refresh
preserves unrelated outputs and original bytes. External producers and other native
formats keep their own revision rules.

Resolve missing or changed originals before relying on the result. Changed raw
bytes require the inventory owner to record their current identity; preserve old
captures and deliver replacements through the source-delivery workflow. A missing
original can produce a failed diagnostic normalized record without a current
classification stamp. Restore the retained original through its owner and rerun
the selected normalization; never add the stamp by hand.

An interrupted write leaves the previously published record authoritative;
temporary output does not establish current evidence. A batch can finish earlier
records before a later record fails. Retry the same selected operation: completed
current records skip, and unfinished records are rechecked. Inspect reported failed
actions and re-run verification/readiness after recovery.

A future positive classification revision reports
`NORMALIZATION_PROFILE_UNSUPPORTED` during selected normalization and
`html_usability_profile_unsupported` during inspection/coverage. Use a compatible
producer and preserve the retained record and originals. Both preview and force
respect this refusal, and the selected batch is refused before output writes.

Acquisition checks correlated existing HTML before freezing a reuse baseline and
checks delivered classifications again before committing request fulfilment or
question reopening. A retained useful record with revision 1 still requires
refresh; a genuinely unnormalized source can be normalized within its authorized
acquisition order. Current shell classifications do not prevent acquiring a
different usable capture.

For an already pending order, recover any pending submission through its owning
controller first. Finish or explicitly abandon its child run, then abandon the
parent session when required. Preserve the old order and baseline for audit;
refresh the selected HTML explicitly outside that order and start a fresh session.
Do not rewrite frozen baselines or stamp a revision by hand to resume old work.
See [orchestration recovery](orchestration.md) for the owner commands and safeguards.

Re-evaluate affected coverage through its owner after refresh. The retained text
hash can stay unchanged while normalized bytes and their classifications change.
Existing byte-bound assessments or review receipts must be rechecked and renewed
through their owning authorization workflow when required; do not edit cached
passes or approval records. A valid/searchable extraction can still remain blocked
because it contains a gateway or authentication shell.

## Preserve research and review authority

Use `pack guide --topic revisions --format text` for a same-pack change. Plan the
revision, inspect conflicts and affected answers, then apply the saved plan and
explicit coverage migration. Old answers remain archived. Changed evidence,
instructions, pack rules or computation definitions need fresh checks and reviews.
Resume an unchanged run through its owner; start a new run after controls change.
Legacy sessions without adequate bindings must be explicitly closed before revision.

Strict research requires an independently controlled review authority. A local
reviewer name, successful setup, framework acknowledgment or old export is not a
current approval. The protected host is an optional macOS boundary; it is not
implemented by an ordinary framework shell or protected parent work order.

Computation accepts the closed declarative language and selected retained records.
It refuses missing required data, unsafe expressions, invalid clock selections
and failed required invariants. Warnings and due local actions require explicit,
identity-bound application. Exact decimal arithmetic does not establish factual
accuracy or domain validity.
