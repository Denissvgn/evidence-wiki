# Existing Wiki Migration Checklist

Use this checklist to plan the migration of an existing research wiki into the workspace system. Plan and validate against a copy or staging workspace before changing production content.

The checklist covers raw source mapping, frontmatter updates, source IDs, index conversion, log preservation, linter migration, and skill replacement.

## 1. Preserve Production First

- [ ] Identify the production wiki path, owner, and current git status.
- [ ] Create a read-only backup or clone before changing files.
- [ ] Create a separate staging workspace from the reusable starter.
- [ ] Keep production raw files, generated files, and wiki pages unchanged during planning.
- [ ] Record the migration goal, scope, and excluded areas in the staging `log.md`.
- [ ] Run `python3 scripts/snapshot_user_edits.py` in the staging workspace before broad agent operations.

Do not migrate in place until the staging copy passes inventory, normalization, lint, and human review.

Before any in-place tooling upgrade, run `evidence-wiki contract` from the
package you intend to install and compare `upgrade_compatibility` with the
target `workspace-system.yml`. The current upgrader accepts only workspace
schema `0.1` and `research.yml` contract `0.1`; an unknown, missing, or
mismatched value is rejected before the first write. Use a compatible
intermediate package or a separately reviewed migration for unsupported
contracts—never edit the version marker merely to bypass negotiation.

Exercise the upgrade against a copy first. The default upgrade refreshes
starter-managed `scripts/` without deleting unknown files and preserves
`research.yml`, raw evidence, sources, wiki pages, index, log history, and
user-authored optional docs. A failed/interrupted replace leaves the prior
managed file intact and can be retried after resolving the reported error.

## 2. Inventory The Existing Wiki

- [ ] List current wiki directories, page counts, and page purposes.
- [ ] Identify pages that are source notes, entities, concepts, methods, systems, benchmarks, datasets, claims, synthesis pages, questions, decisions, outputs, or project-specific types.
- [ ] List existing templates, scripts, skills, prompts, Dataview queries, and Obsidian settings.
- [ ] Identify generated outputs that should move under `wiki/outputs/`.
- [ ] Identify pages that are obsolete, duplicate, temporary, or unsafe to migrate without review.
- [ ] Record any domain-specific assumptions that belong in a domain pack rather than the reusable workspace core.

Use `docs/new-project-guide.md` to configure the staging workspace before moving content into it.

## 3. Raw Source Mapping

Raw source mapping defines where evidence will live in the new workspace.

- [ ] List all existing evidence locations, including PDFs, web links, notes, datasets, code archives, screenshots, exports, and user-provided documents.
- [ ] Decide which files are source evidence and which files are maintained wiki knowledge.
- [ ] Map source evidence into configured `raw.source_roots` in `research.yml`.
- [ ] Preserve raw evidence filenames where possible to keep source IDs stable.
- [ ] Add newer versions as new raw files instead of overwriting old files.
- [ ] Move curated URLs into raw link files under a configured raw source root.
- [ ] Mark missing, untrusted, or intentionally excluded evidence for later `rejected` source records instead of deleting it during planning.
- [ ] Keep raw evidence immutable once it is copied into staging.

Run the inventory report in staging before writing a manifest:

```bash
python3 scripts/source_inventory.py --dry-run --report
```

Review unknown files, ambiguous PDF/source pairings, unparsable links, and review-required records before committing source IDs.

## 4. Source IDs And Normalized Records

- [ ] Decide whether old citation keys can map directly to manifest source IDs or must be replaced.
- [ ] Create a migration table with old citation key, new source ID, raw path, source kind, status, and notes.
- [ ] Prefer manifest source IDs for evidence discovered from raw roots.
- [ ] Use `manual:*` IDs only for legacy evidence that cannot yet be mapped to a raw asset.
- [ ] Normalize sources in small batches and review parse warnings.
- [ ] Keep source note creation separate from broader wiki integration.
- [ ] Mark superseded or rejected sources explicitly instead of silently dropping them.

Dry-run normalization first:

```bash
python3 scripts/normalize_sources.py --all --dry-run
```

After the source ID mapping is stable, run normalization for the selected batch and commit generated records separately from wiki rewrites.

## 5. Wiki Taxonomy Mapping

- [ ] Map each existing directory to a configured `wiki.required_dirs` target.
- [ ] Keep generic directories when they fit: `sources`, `entities`, `concepts`, `methods`, `systems`, `benchmarks`, `datasets`, `claims`, `synthesis`, `questions`, `decisions`, and `outputs`.
- [ ] Add a new directory only when the page type is repeated, does not fit an existing type, and needs different extraction or lint behavior.
- [ ] Update `research.yml` and the filesystem together for any renamed or added wiki directories.
- [ ] Move domain-specific taxonomy rules into a domain pack overlay or guidance document.
- [ ] Keep old paths in the migration table until backlinks and index coverage are verified.

For prototype-specific behavior, use `docs/prototype-domain-pack-proposal.md` as a pattern for separating reusable workspace mechanics from domain guidance.

## 6. Frontmatter Updates

Frontmatter updates make legacy pages visible to linting, querying, and Dataview.

- [ ] Add required fields from `research.yml` to every maintained wiki page.
- [ ] Set `type` to an allowed page type that matches the target directory.
- [ ] Set `created` and `updated` dates in the configured date format.
- [ ] Add `source_ids` to source-grounded pages.
- [ ] Add type-specific fields required by `wiki.frontmatter_type_rules`.
- [ ] Preserve meaningful legacy metadata under stable fields or a clearly named nested key.
- [ ] Do not invent source IDs for unsupported claims; create a question page or evidence gap instead.
- [ ] Convert high-risk numeric or time-sensitive statements into structured claims when claim linting is enabled.

Minimum page frontmatter shape:

```yaml
---
type: concept
created: YYYY-MM-DD
updated: YYYY-MM-DD
source_ids: []
---
```

## 7. Source Notes And Claim Migration

- [ ] Create `wiki/sources/` notes for migrated source records before broad integration.
- [ ] Ensure each source note cites at least one valid `source_ids` entry.
- [ ] Link source notes to normalized records when available.
- [ ] Move reusable factual claims into `wiki/claims/` or embedded `claims` frontmatter records.
- [ ] Include `subject`, `predicate`, `object`, and `source_ids` on structured claims.
- [ ] Include `value`, `unit`, and `scope` when a claim is numeric or likely to conflict with another claim.
- [ ] Capture contradictions as claim conflicts or question pages instead of resolving them silently.

## 8. Index Conversion

Index conversion makes migrated pages discoverable to humans, agents, and the linter.

- [ ] Decide whether `index.md` will use static Markdown links, Dataview sections, or both.
- [ ] Add every migrated page to a static section or a Dataview-covered directory.
- [ ] Use quoted Dataview `FROM "wiki/<dir>"` paths when Dataview sections are enabled.
- [ ] Preserve important legacy navigation groupings as headings or synthesis pages.
- [ ] Remove stale links only after target pages exist or are intentionally excluded.
- [ ] Run lint after each index conversion batch.

Static index coverage remains supported even when Obsidian Dataview is enabled. See `docs/obsidian-dataview.md` for Dataview-compatible examples.

## 9. Log Preservation

Log preservation keeps the migration auditable without rewriting history.

- [ ] Preserve the existing project history in its original file when possible.
- [ ] If the old log format conflicts with `log.md`, archive it under `docs/` or `wiki/outputs/` and link it from the new `log.md`.
- [ ] Add a new `setup` or `migration` entry that explains the staging copy, source mapping, and migration start date.
- [ ] Do not rewrite old log entries except to fix formatting introduced during the current migration.
- [ ] Append inventory, normalization, ingest, lint, and synthesis checkpoints as migration batches are completed.
- [ ] Include source IDs, changed path groups, lint issue counts, and remaining blockers when useful.

## 10. Linter Migration

- [ ] Configure `research.yml` before using lint results as acceptance gates.
- [ ] Enable structure, frontmatter, links, source coverage, and claim checks according to the desired strictness.
- [ ] Replace prototype-specific lint scripts or regex checks with configured workspace lint rules where possible.
- [ ] Move domain-specific validation expectations into a domain pack or project documentation.
- [ ] Run lint on small migration batches and fix structural issues before migrating more content.
- [ ] Treat unknown source IDs, missing source notes, broken links, uncovered pages, and claim conflicts as migration blockers unless explicitly deferred.

Workspace lint command:

```bash
python3 scripts/lint.py --format text
```

Use `--append-log` only when the lint run is a meaningful migration checkpoint.

## 11. Skill Replacement

- [ ] List existing agent instructions, prompts, custom skills, and automated workflows.
- [ ] Map reusable behavior to `research-ingest`, `research-query`, `research-lint`, `research-synthesis`, and `research-scout`.
- [ ] Move domain-specific extraction targets, page rules, source priorities, and output scaffolds into a domain pack.
- [ ] Remove old instructions that hardcode obsolete paths, page types, source roots, or lifecycle states.
- [ ] Keep `AGENTS.md` as the operating contract for the configured workspace.
- [ ] Validate that agents can perform inventory, normalization, source note creation, query, lint, and synthesis from the new instructions.

Skill replacement is complete when generic skills plus optional domain-pack guidance can reproduce the intended legacy workflows without hardcoding prototype paths.

## 12. Production Cutover Readiness

- [ ] For existing question pages that predate coverage manifests, decide which
  high-stakes questions require `coverage_required: true` and create
  `sources/coverage/<slug>.yml` before marking them publication-ready. Leave
  ordinary historical answers unchanged unless a supervisor designates them as
  high-stakes.
- [ ] For old discovery candidate records that lack `evidence_path`,
  `source_policy`, `freshness_policy`, or `identity_policy`, add those fields
  during review or keep the candidate as historical context. Do not treat a
  legacy candidate as selected/fetched publication evidence until its policy
  fields and provenance are explicit.
- [ ] Run `python3 scripts/publication_readiness.py --format json` after export
  and before publishing migrated answers; a `no_ship`,
  `blocked_on_sources`, or `attention_required` verdict should become a
  migration blocker or documented no-ship decision.
- [ ] Staging `research.yml` matches the intended production policy.
- [ ] Raw source mapping is complete or documented with explicit gaps.
- [ ] Manifest source IDs are stable enough to cite.
- [ ] Normalized records exist for the first migrated source batch.
- [ ] Source notes exist for migrated source IDs.
- [ ] Frontmatter is valid for migrated maintained pages.
- [ ] `index.md` covers migrated pages through static links or Dataview sections.
- [ ] Existing log history is preserved and new migration checkpoints are appended.
- [ ] Linter results are clean or every remaining issue is documented with an owner and disposition.
- [ ] Generic skills and domain-pack guidance replace legacy automation.
- [ ] A human has reviewed the staging wiki before production paths are changed.

Only after this checklist is satisfied should a team plan production cutover.
Prefer a final read-only backup, a scoped copy operation, and one migration commit that records the source mapping and validation results.
