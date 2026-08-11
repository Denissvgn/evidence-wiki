# Evidence Policy Vocabulary

Evidence policies name the checks a source must satisfy before it can support a
coverage facet. Version 1.0 is a declarative vocabulary only: it records the
source authority, freshness, and identity rules that later evaluators enforce.
The allowed values are also published by `evidence-wiki contract` under
`policy_vocabularies`. The companion `policy_vocabulary_definitions` contract
field includes definition text for base policies and installed domain-pack
extensions.

Coverage manifests use three policy fields on each facet:

- `source_policy`: the authority level or source family required.
- `freshness_policy`: the currentness, release, or publication-age rule.
- `identity_policy`: the identifier, origin, or ref check that prevents
  fabricated or mismatched evidence.

## Offline Evaluation Helpers

`scripts/_evidence_policies.py` evaluates these policy fields from local
workspace artifacts only. It loads manifest records, normalized-source
frontmatter, provenance sidecars, selected discovery candidates, jurisdiction
profiles, and coverage manifests, then returns structured policy results with
`policy`, `verdict`, `source_ids`, `reasons`, and `remediation`.

The helper never performs network re-resolution. It can pass policies supported
by recorded local metadata, such as academic identifiers, selected-candidate
origin matches, official-domain matches, repository refs, and recorded
currentness metadata. `citation_id_resolves` is a local bibliographic identity
check: every accepted academic source must record a syntactically valid DOI,
arXiv ID, OpenAlex work ID, PMID, or PMCID plus a non-empty title. DOI resolver,
arXiv, and OpenAlex URLs are normalized into identifiers before evaluation.
Malformed identifiers or title-less records fail the identity policy; live
OpenAlex/arXiv re-resolution belongs to the separate citation-verifier workflow.
`current_legal_figure` and `current_product_spec` require `origin_url`,
`retrieved_at`, and one recorded date signal:
`validity_period`, `effective_date`, `publication_date`, or
`date_metadata` (`date_metadata.valid_for_year`, `valid_from`, `effective_date`,
`publication_date`, `validity_period`, or a documented
`currentness_indicator`), or `date_not_available`. `date_not_available` can pass
product-spec freshness only; legal, regulatory, and tax figures fail closed
without dated currentness metadata.

Currentness policies also fail sources marked `rejected`, `superseded`,
`source_status: error_page`, `source_status: not_found`, or
`source_status: unavailable`, and sources linked to selected candidates with
blocking risk flags such as `superseded_or_historical` or `stale_source`.
Validity periods are compared to `retrieved_at`; stale or future-effective
periods cannot satisfy currentness.

Official-domain trust is separate from acquisition transport. A reviewed
`sources/jurisdictions.yml` profile can describe generic official authority
domains, not only legal jurisdictions:

```yaml
jurisdiction_profiles:
  - jurisdiction_id: public-safety-authorities
    name: Public safety authorities
    official_domains:
      - epa.gov
      - usfa.fema.gov
    blocked_domains: []
```

Without a jurisdiction profile or a selected discovery candidate trail,
`official_primary`, `primary_or_official`, `official_vendor`, and
`official_domain_match` can require `manual_review`; that manual review can pass
coverage but keeps publication readiness at `no_ship` until
`question_resolve.py approve` or `question_resolve.py review` records an
explicit human-review approval.
`integrations.acquisition.web.allowed_domains` is only a transport allowlist for
`web get`. It is not consulted as a trust signal and must not be used to make an
official policy pass.

All source, freshness, and identity policies fail before domain-specific checks
when an accepted source is marked unusable evidence. The helper reads
`evidence_usable: false`, `unusable_evidence_reasons`, non-available
`source_status` values, and any valid `delivery_failure_code` from manifest
records, normalized frontmatter, or provenance sidecars. This keeps official
error pages, not-found captures, TLS caveats, sparse JavaScript shells, and
other structured delivery failures auditable while preventing them from
satisfying required coverage facets.

`repo_ref_resolves` is also local-only. For `github_implementation` facets, the
helper accepts GitHub acquisition provenance rather than a bare GitHub URL:
`repository_owner`, `repository_name`, `repository_full_name`,
`repository_artifact_kind`, selected `repository_ref`, `retrieved_at`, and an
explicit `license` field. Source-code implementation evidence defaults to
`repository_artifact_kind: source_archive` and must include
`downloaded_archive_url` plus an archive checksum; a metadata-only repository or
release snapshot can pass only when the facet explicitly lists
`accepted_artifact_kinds: [repository_metadata]` or
`[release_metadata]`. Oversize/refused artifacts fail repository identity.

## Evidence Paths

| Value | Use |
|-------|-----|
| `legal_current_figure` | Current legal, tax, fee, threshold, deadline, benefit, or regulatory figure. |
| `academic_method_existence` | A named paper, method, dataset, benchmark, or artifact exists in scholarly evidence. |
| `github_implementation` | Code, implementation, release, or repository evidence tied to a canonical repository. |
| `official_guidance` | Official operational, safety, response, standards-body, or best-practice guidance where the claim is the guidance itself rather than a current legal figure or academic citation. |
| `standards_registry_reference` | Official standards registry metadata for designation, edition, status, replacement, and registry identity. |
| `product_requirement_profile` | Product-compliance requirement guidance, harmonised-standard linkage, OJEU/legal-act metadata, or equivalent product profile. |
| `vendor_product_spec` | Product, service, hardware, software, or API capability from a vendor-controlled source. |

## Domain-Pack Extensions

Domain packs may extend the declarative vocabulary without editing the base
enumerations. Add namespaced IDs under `domain_pack.policy_vocabularies` in the
pack overlay:

```yaml
domain_pack:
  name: general-science
  policy_vocabularies:
    freshness_policy:
      pack:general-science/study-recency: Require a reviewer to confirm that study dates and follow-up literature are recent enough.
```

Supported sections are `evidence_paths`, `source_policy`, `freshness_policy`,
and `identity_policy`. Every key must use `pack:<pack-name>/<policy-id>` and
every value must be non-empty definition text. Declared namespaced source,
freshness, and identity policies are accepted by coverage-template validation
and evaluate as `manual_review`: definition text tells a reviewer what to decide,
and nothing in it tells this package how. A pack that wants one of its policies
decided mechanically declares a rule for it under `domain_pack.policy_rules` —
see [Policy Rules](#policy-rules). Undeclared namespaced IDs still fail closed
with `COVERAGE_POLICY_UNKNOWN`.

### What Happens To A `manual_review` Verdict

A pack-namespaced policy with no rule behind it evaluates to `manual_review`, so
declaring one decides how the question is resolved. Three settings control the
consequences, and they compose:

1. **Resolution.** `question_resolve.py answer --require-coverage` records
   `status: human_review` rather than `answered`, and retains the policies that
   demanded review in `human_review_policies` plus the clock
   `human_review_requested_at`.
2. **Blast radius.** `research.yml` `review.escalation_scope` decides how far
   that reaches. Under the default `workspace`, the pending review sets the
   workspace verdict to `attention_required` and orchestration refuses to
   operate. Under `question`, it parks only its own question and is reported as
   `readiness.questions_awaiting_review`, so work continues on every other
   question. See [research-yml.md](research-yml.md) and
   [workspace-status.md](workspace-status.md).
3. **Recording the review.** `question_resolve.py approve` covers an
   in-workspace reviewer; `question_resolve.py review --policy P --verdict
   accepted --reviewed-by PRINCIPAL --review-ref REF` records a review a host
   collected in its own approval queue, one policy at a time, with `--review-ref`
   as the opaque pointer back to it. Publication readiness accepts both
   identically and still refuses to ship an unreviewed answer. See
   [question-api.md](question-api.md) and
   [publication-readiness.md](publication-readiness.md).

Scoping and external recording change *where the reviewer sits and how far one
pending review reaches* — never whether a manual-review policy needs a human.
Reducing how *often* a human is needed is a different lever, and
[Policy Rules](#policy-rules) is that lever. A pack composes deterministic
primitives — `max_age`, `equals`, `numeric_range`, `regex`,
`one_of_provenance`, joined by `all_of` and `any_of` — over fields the workspace
already holds, and the package decides the policy itself. "A supplier quote must
be at most 48 hours old" is a subtraction, and a queue that holds subtractions is
a queue nobody drains.

`manual_review` remains the correct answer wherever the policy states a judgement
rather than a computation. `pack:general-science/study-recency` — recent enough
*for the scientific question* — has no threshold that is right across studies, so
no rule can express it and none should be written: an approximation would replace
a reviewer's judgement with a number nobody agreed on. The same holds for any
definition that asks whether evidence is adequate, appropriate, comparable, or
representative. A rule can also keep the human deliberately:
`manual_review_required: true` runs the mechanical checks first and still routes
to review, which narrows what the reviewer must look at without pretending the
judgement was made.

For `academic_method_existence`, a coverage facet may also carry
`claim_probe` metadata when bounded arXiv and OpenAlex searches did not confirm
a named method or artifact. That metadata is exportable state for downstream
agents, not accepted evidence. The facet still needs an accepted scholarly
source to pass, and the probe limitation must state:
`not found in configured providers for this bounded run; not a global nonexistence claim`.

## Policy Rules

A pack policy's definition text says what the policy *means*; it does not say how
to decide it, which is why a namespaced policy on its own evaluates to
`manual_review`. `domain_pack.policy_rules` is the vocabulary for saying the
rest — for the part of a policy that is genuinely mechanical:

```yaml
domain_pack:
  name: market-data
  policy_vocabularies:            # what the policy means, to a reviewer
    freshness_policy:
      pack:market-data/quote-48h: A supplier quote must be at most 48 hours old.
    identity_policy:
      pack:market-data/sku-matches-candidate: The quoted SKU must match the candidate identity on the question.
  policy_rules:                   # how this workspace decides it, unaided
    pack:market-data/quote-48h:
      all_of:
        - max_age: {field: provenance/retrieved_at, hours: 48}
    pack:market-data/sku-matches-candidate:
      manual_review_required: false   # optional, default false
      all_of:
        - equals: {field: record/supplier_quote/sku, question_field: metadata/candidate_sku}
        - one_of_provenance: {providers: [aliexpress-ds, partner-catalog]}
```

A rule is **data, never code**. There is no expression language, no callable, and
no import hook: a pack names primitives from a closed set and this package
evaluates them. That is what keeps "what does this workspace do?" answerable from
the pack's own text, which is the property the rest of the evidence chain rests
on.

Each key must name a policy the same pack already declares under
`policy_vocabularies.source_policy`, `.freshness_policy`, or `.identity_policy`,
and its `pack:<pack-name>/` namespace must equal the pack's own
`domain_pack.name`. `evidence_paths` carries no rules: an evidence path says
*which facet* must be covered, which the coverage manifest resolves structurally
before any policy runs. A rule body declares exactly one of `all_of` or `any_of`,
plus the optional `manual_review_required` flag; any other key is reported as the
typo it is rather than ignored.

### Field References

Every `field` is an RFC 6901 pointer — the same syntax anchor-form grounding
uses, with the leading `/` optional — prefixed by the document it resolves
against:

| Reference | Document |
|-----------|----------|
| `record/...` | The source's structured-view sidecar, bound to its normalized record by hash. The same document an anchor resolves against; see [normalized-source-format.md](normalized-source-format.md). |
| `provenance/...` | The source's merged delivery provenance, as inventoried from its `.provenance.yml` sidecar; see [source-delivery.md](source-delivery.md). |
| `question_field: metadata/...` | The question page's whole frontmatter. Written as a bare pointer with no root segment, because there is only one such document to address. |

One addressing scheme, three consumers. A pointer that reaches nothing, or that
reaches a mapping or array rather than a single scalar, is a failure — see
[Fail-Closed Evaluation](#fail-closed-evaluation).

### Primitives

| Primitive | Shape | Passes when |
|-----------|-------|-------------|
| `max_age` | `{field, hours}`, `hours` greater than zero | The field resolves to an ISO 8601 timestamp and `now − value` is at most `hours`. A timestamp more than five minutes in the future fails as clock skew rather than passing as brand new; that tolerance is fixed and not pack-configurable, because a pack able to widen it would be loosening a fail-closed bound from inside the thing being bounded. A value that names no UTC offset — a bare date, or a timestamp written without one — is valid ISO 8601 and reads as the earliest instant it could denote, its local time at the furthest offset any zone uses. That is conservative on purpose: the zone that stamped it is unknown, so reading it at that extreme can only make a source look older, never fresher. Deliver an explicit offset when you want the age measured exactly. |
| `equals` | `{field, value}` or `{field, question_field}`, exactly one of the two | Field and expected value are equal as canonical scalars. The comparison rule is chosen by the value the *field* resolves to, since that is the evidence and the other side is the assertion about it: a number compares as a decimal on both sides, so a resolved `23.99` matches an expected `"23.990"` while `"23.99 EUR"` does not; a string compares through the workspace's one text normalization (NFKC, quote and dash folding, whitespace collapse, case folding). Equality, never containment. |
| `numeric_range` | `{field, min, max}`; either bound may be written as `min_question_field` / `max_question_field` instead, never both forms of the same bound, and at least one bound is required | The field parses as a decimal and lies within the bounds. Both bounds are inclusive. |
| `regex` | `{field, pattern}` | `pattern` **fully** matches the field's canonical text. Full match, never search: implicit containment is the weakness scalar equality exists to remove, so an author who wants a substring writes `.*B0.*` and says so. Patterns are capped at 512 characters, on the grounds that a pattern too long to read is long enough to hide catastrophic backtracking from the reviewer approving the pack. |
| `one_of_provenance` | `{providers: [...]}`, `{domains: [...]}`, or both; each list non-empty | The source was delivered by one of the named providers, or its `origin_url` host matches one of the named domains. See [source-delivery.md](source-delivery.md) for which sidecar fields carry a provider identity, and which deliberately do not. |
| `all_of` / `any_of` | a non-empty list of primitives | Every child passes / at least one child passes. Compositions nest at most three deep, which keeps a declaration readable and its evaluation cost bounded by the declaration rather than by the data. |

`all_of` reports every failing child instead of stopping at the first, so one
evaluation names the whole list of artifacts to fix; `any_of` stops at the branch
that carried it. A facet accepts its sources jointly, so every accepted source
must satisfy the rule — one stale quote among two is the facet's problem however
fresh the other is.

### Fail-Closed Evaluation

A rule that cannot be decided evaluates to `fail`, never to `manual_review`. That
covers a missing or corrupt structured view, a pointer that resolves to nothing, a
target that is a mapping or array rather than a scalar, and a timestamp `max_age`
cannot parse. Degrading to review instead would return exactly the least
trustworthy sources to the queue rules exist to drain, and would do it silently.

Every failure reason carries a stable prefix naming the source and the field that
was read: `rule_field_unresolved`, `rule_value_mismatch`, `rule_out_of_range`,
`rule_stale`, `rule_future_timestamp`, `rule_regex_mismatch`, and
`rule_provenance_not_allowed`, plus `structured_view_missing` and
`structured_view_corrupt` when the sidecar itself is the problem — the same two
codes anchor grounding reports, so a host that already handles one handles the
other.

A **malformed declaration** fails earlier and louder than any single source can.
`evidence-wiki pack validate` reports a `policy_rules` check failure before the
pack ships, and at answer time evaluation refuses the command with
`CONFIG_INVALID` rather than treating the pack as declaring no rules at all:
silently dropping a pack's automation would send every one of its policies back to
manual review without saying so.

### Rule Verdicts

| Rule outcome | `manual_review_required` | Facet policy verdict |
|--------------|--------------------------|----------------------|
| any check fails | either value | `fail` |
| every check passes | `false` (the default) | `pass` |
| every check passes | `true` | `manual_review` |

`manual_review_required: true` keeps the human step **in addition** to the
mechanical checks rather than instead of them, which is what a policy that is
partly computable and partly a judgement call needs.

Rollup is unchanged. A `fail` blocks a required facet exactly as any other failing
policy does, and a `manual_review` still records
`question_resolve.py answer --require-coverage` as `status: human_review`, with
the escalation and review-recording consequences described above.

### What The Tooling Reports

`evidence-wiki pack validate` gains a `policy_rules` check, and summarizes each
declared rule in its `domain_pack.policy_rules` payload as the primitive names the
rule uses plus its `manual_review_required` flag. The autonomous-required-facets
lint reads that summary: a rule-backed policy without `manual_review_required` is
no longer counted as manual-only, because it now *is* a deterministic policy, so a
required facet may use it without `domain_pack.human_gated: true`. One that sets
the flag still counts as manual-only — the pack asked for the human on purpose.

`evidence-wiki contract` publishes the same summary for every installed pack under
an additive top-level `policy_rules` key:

```json
"policy_rules": {
  "market-data": {
    "pack:market-data/quote-48h": {
      "primitives": ["all_of", "max_age"],
      "manual_review_required": false
    }
  }
}
```

`policy_vocabulary_definitions` keeps its existing shape. A host reads the
definitions to learn what a policy means, and `policy_rules` to learn whether this
installation can decide it without a human.

## Source Policies

| Value | Meaning |
|-------|---------|
| `official_primary` | Requires the primary authority of record, such as a government agency, standards body, publisher record, or repository owner. |
| `primary_or_official` | Allows either a primary source or an official aggregator that republishes authoritative source material. |
| `academic_indexed` | Requires a scholarly index, publisher, DOI resolver, arXiv record, OpenAlex record, or equivalent bibliographic index. |
| `openalex_or_arxiv` | Narrows scholarly evidence to OpenAlex or arXiv-backed metadata. |
| `canonical_repository` | Requires the canonical project repository, owner namespace, release page, or commit/tag source. |
| `official_vendor` | Requires a vendor-owned page, documentation source, support page, release note, or equivalent official product source. |
| `official_standards_registry` | Requires an official standards-body, government register, OJEU, EUR-Lex, or recognized registry source for the standards claim. |
| `standards_body_primary` | Requires the standards body's own catalogue, open-data, browsing, or publication record for the referenced standard. |
| `domain_pack_allowed` | Uses a domain-pack-defined source family that has been reviewed as acceptable for that domain. |
| `manual_review_required` | Cannot pass on automation alone; a reviewer must inspect and record acceptance. |

## Freshness Policies

| Value | Meaning |
|-------|---------|
| `current_legal_figure` | The source must represent the current legal, regulatory, tax, fee, threshold, deadline, or benefit value for the relevant jurisdiction, using recorded retrieval and validity/effective/publication metadata. |
| `current_product_spec` | The source must represent the currently published product, service, API, or vendor capability, using recorded retrieval metadata plus a date signal or explicit `date_not_available` note. |
| `current_standard_reference` | The registry metadata must show a current or published standard reference without withdrawn, superseded, draft, or unresolved replacement status. |
| `current_product_requirement` | The product requirement must have retrieval metadata plus a publication, validity, OJEU, legal-act, or equivalent currentness signal. |
| `publication_identity` | The source must establish bibliographic publication identity rather than currentness. |
| `release_snapshot` | The source must identify a stable release, tag, commit, package version, or repository ref. |
| `no_staleness_check` | No deterministic freshness check is required for this facet. |
| `manual_review` | Freshness cannot be determined locally and must be reviewed manually. |

## Identity Policies

| Value | Meaning |
|-------|---------|
| `citation_id_resolves` | Local metadata records a valid DOI, arXiv ID, OpenAlex work ID, PMID, or PMCID plus title metadata for the cited work. |
| `origin_url_matches_candidate` | The normalized source origin matches the reviewed discovery candidate or selected acquisition request. |
| `repo_ref_resolves` | A repository ref, tag, release, commit, or package version resolves in the canonical repository. |
| `official_domain_match` | The source origin matches an allowed official domain or jurisdiction profile. |
| `standard_designation_matches_registry` | The cited designation and edition/year exactly match the recorded standards registry metadata. |
| `registry_entry_matches_product_requirement` | The registry entry links to the declared product category, legal act, OJEU/harmonised reference, or equivalent requirement metadata. |
| `none` | No additional identity check is required beyond the accepted source record. |

## Artifact Kinds

| Value | Meaning |
|-------|---------|
| `source_archive` | A bounded GitHub source archive was downloaded for an explicit ref and recorded with checksum provenance. |
| `repository_metadata` | Repository metadata was snapshotted without repository source bytes. |
| `release_metadata` | Release and release-asset metadata was snapshotted without downloading source or asset bytes. |

## Path Mapping

| Evidence path | Typical source policies | Typical freshness policies | Typical identity policies |
|---------------|-------------------------|----------------------------|---------------------------|
| `legal_current_figure` | `official_primary`, `primary_or_official`, `domain_pack_allowed`, `manual_review_required` | `current_legal_figure`, `manual_review` | `official_domain_match`, `origin_url_matches_candidate`, `none` |
| `academic_method_existence` | `academic_indexed`, `openalex_or_arxiv`, `primary_or_official`, `manual_review_required` | `publication_identity`, `no_staleness_check`, `manual_review` | `citation_id_resolves`, `origin_url_matches_candidate`, `none` |
| `github_implementation` | `canonical_repository`, `domain_pack_allowed`, `manual_review_required` | `release_snapshot`, `no_staleness_check`, `manual_review` | `repo_ref_resolves`, `origin_url_matches_candidate`, `none` |
| `official_guidance` | `official_primary`, `primary_or_official`, `manual_review_required` | `no_staleness_check`, `current_legal_figure`, `manual_review` | `official_domain_match`, `origin_url_matches_candidate`, `none` |
| `standards_registry_reference` | `official_standards_registry`, `standards_body_primary`, `manual_review_required` | `current_standard_reference`, `current_product_requirement`, `manual_review` | `standard_designation_matches_registry`, `registry_entry_matches_product_requirement`, `origin_url_matches_candidate` |
| `product_requirement_profile` | `official_primary`, `official_standards_registry`, `primary_or_official`, `manual_review_required` | `current_product_requirement`, `manual_review` | `registry_entry_matches_product_requirement`, `official_domain_match`, `origin_url_matches_candidate` |
| `vendor_product_spec` | `official_vendor`, `primary_or_official`, `domain_pack_allowed`, `manual_review_required` | `current_product_spec`, `no_staleness_check`, `manual_review` | `official_domain_match`, `origin_url_matches_candidate`, `none` |

## Examples

- Legal current amount: `legal_current_figure` with `official_primary`,
  `current_legal_figure`, and `official_domain_match`.
- Academic citation: `academic_method_existence` with `openalex_or_arxiv`,
  `publication_identity`, and `citation_id_resolves`. If bounded arXiv/OpenAlex
  probing finds no exact match, record it as `claim_probe.claim_verdict:
  unconfirmed` only; do not phrase the result as global nonexistence.
- GitHub release: `github_implementation` with `canonical_repository`,
  `release_snapshot`, and `repo_ref_resolves`.
- Official safety or operational guidance: `official_guidance` with
  `official_primary`, `no_staleness_check` or an explicit currentness policy,
  and `official_domain_match`.
- Standards registry reference: `standards_registry_reference` with
  `official_standards_registry`, `current_standard_reference`, and
  `standard_designation_matches_registry`.
- EU product requirement profile: `product_requirement_profile` with
  `official_primary` or `official_standards_registry`,
  `current_product_requirement`, and
  `registry_entry_matches_product_requirement`.
- Vendor product page: `vendor_product_spec` with `official_vendor`,
  `current_product_spec`, and `origin_url_matches_candidate`.

Standards policy failures use stable reason codes in local policy results and
publication-readiness reports: `standard_reference_missing`,
`standard_edition_missing`, `standard_title_mismatch`,
`standard_status_withdrawn`, `standard_status_superseded`,
`standard_status_draft`, `standard_replacement_unresolved`,
`registry_terms_unknown`, `registry_metadata_stale`,
`product_requirement_guidance_not_legal_authority`, and
`harmonised_standard_ojeu_reference_missing`.
