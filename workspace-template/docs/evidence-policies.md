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
`question_resolve.py approve` records an explicit human-review approval.
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
but evaluate as `manual_review` until the domain pack adds stronger local
automation. Undeclared namespaced IDs still fail closed with
`COVERAGE_POLICY_UNKNOWN`.

For `academic_method_existence`, a coverage facet may also carry
`claim_probe` metadata when bounded arXiv and OpenAlex searches did not confirm
a named method or artifact. That metadata is exportable state for downstream
agents, not accepted evidence. The facet still needs an accepted scholarly
source to pass, and the probe limitation must state:
`not found in configured providers for this bounded run; not a global nonexistence claim`.

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
