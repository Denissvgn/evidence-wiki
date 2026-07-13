# Orchestrator Handoff Contract

This document defines the machine-to-machine boundary between an external orchestrator (a planner or parent agent) and a research workspace. Every integration point is a deterministic CLI command with versioned, machine-readable output. No step requires reading or writing wiki Markdown from outside the workspace.

This document is the canonical contract (schemas, error codes, artifact shapes). The executable playbook that walks a PM/parent agent through this lifecycle — deploy, seed questions, drive or delegate the run loop, route blocked sources, and collect cited results — is the `research-orchestrate` skill. Locate it with `evidence-wiki orchestrator-guide` (it ships with the package and is never copied into a workspace).

## Lifecycle Overview

1. **Preflight**: check runner capabilities with `evidence-wiki doctor --format json`.
2. **Negotiate**: check supported contract and schema versions with `evidence-wiki contract`.
3. **Deploy**: create the workspace non-interactively from a setup profile that carries the upstream correlation IDs and seed questions.
4. **Deliver sources**: place raw evidence under the configured `raw/` roots and run inventory plus normalization.
5. **Inject questions**: add validated question batches to the running workspace at any point with `scripts/intake_questions.py`.
6. **Poll status**: read one aggregate document with `scripts/workspace_status.py`, including a machine-checkable completion verdict, run budget state, and `stop_reasons` when per-run counters are supplied.
7. **Collect results**: read structured answers with citations from `scripts/export_answers.py`, then evaluate `scripts/publication_readiness.py --format json`; no wiki Markdown parsing required.

High-stakes questions can also carry facet-level answerability state in
`sources/coverage/<slug>.yml`. The schema is documented in
[coverage-manifest.md](coverage-manifest.md); later tooling evaluates those
manifests before resolving covered questions as answered. Academic method or
artifact existence facets may include `claim_probe` metadata when bounded
arXiv/OpenAlex searches did not confirm a claim; those probes keep the coverage
verdict `blocked` until an accepted source exists and must not be summarized as
global nonexistence.

## Reference Fixture

The repository-level fixture `tests/fixtures/chain-handoff/` is the canonical
golden integration for this contract. `tests/test_chain_handoff_e2e.py`
deploys it into `/tmp`, delivers synthetic example.org evidence, injects a
mid-run question, exercises blocked-source routing, generates a run report,
and exports cited answers with the handoff `task_id` intact.

## Step 0: Environment Preflight

Before deploying or running an unattended workspace loop, query the runner:

```bash
evidence-wiki doctor --format json
```

After deployment, run the workspace-local wrapper from the workspace root:

```bash
python3 scripts/doctor.py --format json
```

The JSON report includes per-check `ok`, `degraded`, or `missing` statuses for
Python, PyYAML, `pdftotext`, git, workspace write permissions, and contract
metadata. Required failures, such as missing PyYAML or Python older than 3.10,
return a non-zero exit. Optional failures keep exit `0` and explain capability
loss, such as "PDF normalization degrades to stubs for PDF-only records."

## Step 1: Contract Negotiation

Before deploying or upgrading, query the installed package:

```bash
evidence-wiki contract
```

Output is JSON:

```json
{
  "schema_version": "1.0",
  "package": "evidence-wiki",
  "package_version": "0.1.0",
  "starter_version": "0.4.0",
  "starter_schema_version": "0.1",
  "compatible_research_yml_contract": "0.1",
  "profile_schema_versions": ["0.1"],
  "artifact_schemas": {
    "workspace_status": "1.0",
    "question_intake": "1.0",
    "answer_export": "1.0",
    "source_requests": "1.0",
    "citation_verification": "1.0",
    "publication_readiness": "1.0",
    "coverage_manifest": "1.0",
    "mcp_server": "1.0",
    "error_envelope": "1.0"
  },
  "policy_vocabularies": {
    "evidence_paths": [
      "academic_method_existence",
      "github_implementation",
      "legal_current_figure",
      "official_guidance",
      "product_requirement_profile",
      "standards_registry_reference",
      "vendor_product_spec"
    ],
    "artifact_kinds": [
      "release_metadata",
      "repository_metadata",
      "source_archive"
    ],
    "source_policy": [
      "academic_indexed",
      "canonical_repository",
      "domain_pack_allowed",
      "manual_review_required",
      "official_primary",
      "official_standards_registry",
      "official_vendor",
      "openalex_or_arxiv",
      "primary_or_official",
      "standards_body_primary"
    ],
    "freshness_policy": [
      "current_legal_figure",
      "current_product_spec",
      "current_product_requirement",
      "current_standard_reference",
      "manual_review",
      "no_staleness_check",
      "publication_identity",
      "release_snapshot"
    ],
    "identity_policy": [
      "citation_id_resolves",
      "none",
      "official_domain_match",
      "origin_url_matches_candidate",
      "registry_entry_matches_product_requirement",
      "repo_ref_resolves",
      "standard_designation_matches_registry"
    ]
  }
}
```

Compatibility policy:

- An orchestrator must only submit setup profiles whose `schema_version` appears in `profile_schema_versions`.
- Before `evidence-wiki upgrade`, compare the workspace's `workspace-system.yml` `compatible_research_yml_contract` with the value reported here; upgrade only when they match.
- `artifact_schemas` lists the schema version of each machine-readable artifact the workspace tooling emits. A minor package upgrade never changes a published artifact schema without bumping its version; orchestrators should pin parsers to the major version of each artifact schema and treat unknown additional fields as forward-compatible additions.
- `policy_vocabularies` lists the allowed evidence policy identifiers for coverage manifests and domain-pack coverage templates. Human-readable definitions live in [evidence-policies.md](evidence-policies.md).
- `contract.compatible_research_yml_contract` in workspace status output (see step 5) reports what an already-created workspace was built against.

### Error Envelope

When a workspace script that supports machine output fails fatally under
`--format json`, `--format jsonl`, or a JSON-only dry run, it writes one error
object to stderr and exits non-zero:

```json
{
  "schema_version": "1.0",
  "error_code": "CONFIG_MISSING",
  "message": "Missing config: /workspace/research.yml",
  "recoverable": true,
  "remediation": "Run from an initialized workspace or pass --project-root to one."
}
```

Some scripts add a small `details` object for useful correlation context, such
as `question_claim.py` and `question_resolve.py` reporting `action`, `slug`,
and `agent_id`. Text mode continues to print human-readable stderr.

#### JSON Output Scripts

Every script documented here with JSON output must import the shared
`_script_errors` helper and use the shared envelope for fatal setup, validation,
or refusal errors. Commands that return a normal JSON report with a non-zero
verdict, such as failed smoke validation, still reserve the envelope for fatal
errors that prevent the report from being built.

| Script | JSON mode | Fatal envelope codes |
|--------|-----------|----------------------|
| `coverage_manifest.py` | `python3 scripts/coverage_manifest.py init\|show\|validate\|set-facet\|evaluate --format json` | `DEPENDENCY_MISSING`, `TOOLING_MISSING`, `CONFIG_MISSING`, `CONFIG_INVALID`, `COVERAGE_MANIFEST_INVALID`, `COVERAGE_MANIFEST_EXISTS`, `COVERAGE_CLAIM_PROBE_INVALID`, `COVERAGE_FACET_UNKNOWN`, `COVERAGE_POLICY_UNKNOWN`, `COVERAGE_TEMPLATE_INVALID`, `SOURCE_UNKNOWN`, `REQUEST_UNKNOWN`, `REQUEST_NOT_LINKED`, `VALUE_INVALID`, `SLUG_INVALID`, `SLUG_UNKNOWN`, `WORKSPACE_UNREADABLE` |
| `discover_sources.py` | `python3 scripts/discover_sources.py --format json <command>` | `DEPENDENCY_MISSING`, `CONFIG_MISSING`, `CONFIG_INVALID`, `VALUE_INVALID`, `DISCOVERY_DISABLED`, `NOT_IMPLEMENTED`, `DISCOVERY_NETWORK_ERROR`, `DISCOVERY_RESPONSE_INVALID`, `SEARCH_PROVIDER_DISABLED`, `SEARCH_PROVIDER_FAILED`, `GITHUB_AUTH_REQUIRED`, `GITHUB_RATE_LIMITED`, `CANDIDATE_UNKNOWN`, `REQUEST_UNKNOWN`, `QUESTION_UNKNOWN`, `WORKSPACE_UNREADABLE` |
| `doctor.py` | `python3 scripts/doctor.py --format json` | `WORKSPACE_UNREADABLE` for fatal setup exceptions; missing capabilities are normal report checks. |
| `export_answers.py` | `python3 scripts/export_answers.py --format json` or `--format jsonl` | `DEPENDENCY_MISSING`, `TOOLING_MISSING`, `CONFIG_MISSING`, `CONFIG_INVALID`, `HANDOFF_SIGNATURE_INVALID`, `WORKSPACE_UNREADABLE` |
| `fetch_sources.py` | `python3 scripts/fetch_sources.py --format json <provider> <command>` | `DEPENDENCY_MISSING`, `CONFIG_MISSING`, `CONFIG_INVALID`, `ACQUISITION_DISABLED`, `ACQUISITION_PROVIDER_DISABLED`, `ACQUISITION_LIMIT_EXCEEDED`, `ARXIV_ID_INVALID`, `ACQUISITION_NETWORK_ERROR`, `ACQUISITION_RESPONSE_INVALID`, `ACQUISITION_ARCHIVE_UNSAFE`, `ACQUISITION_TARGET_EXISTS`, `OPENALEX_ID_INVALID`, `OPENALEX_RESOLUTION_UNCERTAIN`, `OPENALEX_AUTH_REQUIRED`, `OPENALEX_RATE_LIMITED`, `OPENALEX_PDF_UNAVAILABLE`, `GITHUB_AUTH_REQUIRED`, `GITHUB_RATE_LIMITED`, `GITHUB_REPO_INVALID`, `GITHUB_NOT_FOUND`, `GITHUB_RELEASE_UNAVAILABLE`, `GITHUB_ARCHIVE_TOO_LARGE`, `NOT_IMPLEMENTED` |
| `intake_questions.py` | `python3 scripts/intake_questions.py --format json`, or any `--dry-run` | `DEPENDENCY_MISSING`, `TOOLING_MISSING`, `CONFIG_MISSING`, `CONFIG_INVALID`, `HANDOFF_SIGNATURE_INVALID`, `INTAKE_FIELD_TOO_LONG`, `INTAKE_TOTAL_CAP_EXCEEDED`, `INTAKE_RATE_LIMITED`, `WORKSPACE_UNREADABLE` |
| `lint.py` | `python3 scripts/lint.py --format json` | `DEPENDENCY_MISSING`, `CONFIG_MISSING`, `CONFIG_INVALID`, `WORKSPACE_UNREADABLE` |
| `normalize_sources.py` | `python3 scripts/normalize_sources.py --format json` | `DEPENDENCY_MISSING`, `CONFIG_MISSING`, `CONFIG_INVALID`, `MANIFEST_MISSING`, `MANIFEST_INVALID`, `SOURCE_UNKNOWN`, `WORKSPACE_UNREADABLE` |
| `query_index.py` | `python3 scripts/query_index.py QUERY --format json` | `DEPENDENCY_MISSING`, `CONFIG_MISSING`, `CONFIG_INVALID`, `MANIFEST_MISSING`, `MANIFEST_INVALID`, `QUERY_MISSING`, `WORKSPACE_UNREADABLE` |
| `question_claim.py` | `python3 scripts/question_claim.py claim --slug SLUG --agent-id AGENT --format json` | `TOOLING_MISSING`, `CONFIG_MISSING`, `CONFIG_INVALID`, `CLAIM_HELD`, `CLAIM_NOT_STALE`, `STEAL_THRESHOLD_REQUIRED`, `STEAL_FLAG_REQUIRED`, `STEAL_NOT_APPLICABLE`, `STATUS_NOT_CLAIMABLE`, `STATUS_NOT_RELEASABLE`, `SLUG_INVALID`, `SLUG_UNKNOWN`, `PAGE_INVALID`, `AGENT_ID_INVALID` |
| `question_resolve.py` | `python3 scripts/question_resolve.py answer\|block\|defer\|reject\|reopen --slug SLUG --agent-id AGENT ... --format json` | `TOOLING_MISSING`, `CONFIG_MISSING`, `CONFIG_INVALID`, `CLAIM_HELD`, `STATUS_NOT_RESOLVABLE`, `STATUS_NOT_REOPENABLE`, `SOURCE_NOT_NORMALIZED`, `QUESTION_NOT_CLAIMED`, `ANSWER_SOURCE_REQUIRED`, `COVERAGE_REQUIRED`, `COVERAGE_BLOCKED`, `COVERAGE_MANIFEST_INVALID`, `ANSWER_PAGE_INVALID`, `ANSWER_PAGE_MISSING`, `SOURCE_UNKNOWN`, `REQUEST_UNKNOWN`, `REQUEST_NOT_LINKED`, `RESOLUTION_REASON_INVALID`, `VALUE_INVALID`, `PAGE_INVALID`, `AGENT_ID_INVALID` |
| `question_status.py` | `python3 scripts/question_status.py --format json` | `DEPENDENCY_MISSING`, `CONFIG_MISSING`, `CONFIG_INVALID`, `WORKSPACE_UNREADABLE` |
| `publication_readiness.py` | `python3 scripts/publication_readiness.py --format json` or `python3 scripts/publication_readiness.py --format json bundle --run-id RUN_ID` | `CONFIG_MISSING`, `CONFIG_INVALID`, `RUN_ID_INVALID`, `WORKSPACE_UNREADABLE` |
| `run_controller.py` | `python3 scripts/run_controller.py start\|transition\|status\|event\|finish --format json` | `TOOLING_MISSING`, `CONFIG_MISSING`, `CONFIG_INVALID`, `RUN_ID_REQUIRED`, `RUN_ID_INVALID`, `RUN_EXISTS`, `RUN_UNKNOWN`, `RUN_STATE_INVALID`, `RUN_TRANSITION_INVALID`, `RUN_TERMINAL`, `FINAL_VERDICT_REQUIRED`, `EVENT_DATA_INVALID`, `VALUE_INVALID`, `AGENT_ID_INVALID`, `WORKSPACE_UNREADABLE` |
| `run_report.py` | `python3 scripts/run_report.py baseline --output PATH --format json`, `python3 scripts/run_report.py --baseline PATH --format json`, or `python3 scripts/run_report.py --run-id RUN_ID --format json` | `DEPENDENCY_MISSING`, `TOOLING_MISSING`, `CONFIG_MISSING`, `CONFIG_INVALID`, `BASELINE_MISSING`, `BASELINE_INVALID`, `RUN_ID_REQUIRED`, `RUN_ID_INVALID`, `RUN_UNKNOWN`, `RUN_STATE_INVALID`, `WORKSPACE_UNREADABLE` |
| `smoke_validate_workspace.py` | `python3 scripts/smoke_validate_workspace.py --format json` | `DEPENDENCY_MISSING`, `CONFIG_MISSING`, `CONFIG_INVALID`, `WORKSPACE_UNREADABLE` |
| `source_inventory.py` | `python3 scripts/source_inventory.py --report --format json`, or `--dry-run --format json` JSONL records | `DEPENDENCY_MISSING`, `CONFIG_MISSING`, `CONFIG_INVALID`, `MANIFEST_INVALID`, `INVENTORY_CHECKSUM_REQUIRED`, `INVENTORY_CHECKSUM_MISMATCH`, `WORKSPACE_UNREADABLE` |
| `source_requests.py` | `python3 scripts/source_requests.py list --format json` or `python3 scripts/source_requests.py plan-fetch --request-id ID --format json` | `DEPENDENCY_MISSING`, `CONFIG_MISSING`, `CONFIG_INVALID`, `MANIFEST_INVALID`, `QUESTION_UNKNOWN`, `REQUEST_UNKNOWN`, `WORKSPACE_UNREADABLE` |
| `verify_citations.py` | `python3 scripts/verify_citations.py --format json`, optionally `--live --provider arxiv\|openalex` | `DEPENDENCY_MISSING`, `CONFIG_MISSING`, `CONFIG_INVALID`, `VALUE_INVALID`, `ACQUISITION_DISABLED`, `ACQUISITION_PROVIDER_DISABLED`, `ACQUISITION_NETWORK_ERROR`, `ACQUISITION_RESPONSE_INVALID`, `WORKSPACE_UNREADABLE` |
| `workspace_status.py` | `python3 scripts/workspace_status.py --format json`, `--check-complete --format json`, or `--run-id RUN_ID --format json` | `DEPENDENCY_MISSING`, `TOOLING_MISSING`, `CONFIG_MISSING`, `CONFIG_INVALID`, `RUN_ID_INVALID`, `RUN_UNKNOWN`, `RUN_STATE_INVALID`, `WORKSPACE_UNREADABLE` |

Stable error codes:

| Code | Meaning | Typical remediation |
|------|---------|---------------------|
| `DEPENDENCY_MISSING` | Required runtime dependency is unavailable. | Install the dependency and rerun. |
| `CONFIG_MISSING` | `research.yml` is missing. | Run from an initialized workspace or pass `--project-root`. |
| `CONFIG_INVALID` | `research.yml` or contract metadata is malformed. | Fix the workspace config. |
| `WORKSPACE_UNREADABLE` | Required workspace state could not be read. | Check the path and starter files. |
| `UPGRADE_WRITE_FAILED` | `evidence-wiki upgrade` could not atomically write starter-managed content. | Restore workspace write access and free space, preview with `--dry-run`, then retry. |
| `MANIFEST_MISSING` | `sources/manifest.jsonl` is missing. | Run `scripts/source_inventory.py --report`. |
| `MANIFEST_INVALID` | Manifest JSONL is malformed. | Fix or regenerate the manifest. |
| `INVENTORY_CHECKSUM_REQUIRED` | Strict inventory mode refused one or more records without verified checksums. | Add provenance sidecars with verified `sha256:` checksums or rerun without `--require-checksum`. |
| `INVENTORY_CHECKSUM_MISMATCH` | Strict inventory mode refused one or more records with unverified checksum provenance. | Replace the delivered source or checksum after review, or rerun without `--reject-mismatch`. |
| `BASELINE_MISSING` | Run-report baseline is missing. | Capture `scripts/run_report.py baseline --output PATH` first. |
| `BASELINE_INVALID` | Run-report baseline has the wrong shape. | Use a run-report baseline artifact or unmodified question-status JSON document. |
| `RUN_ID_REQUIRED` | A run-controller command that needs an existing run omitted `--run-id`. | Pass `--run-id`, or use `run_controller.py start` to create a new run. |
| `RUN_ID_INVALID` | A run id is empty, path-like, or invalid on Windows. | Use a plain filename-safe run id. |
| `RUN_EXISTS` | `start` would overwrite an existing run directory. | Choose a different run id or inspect the existing run. |
| `RUN_UNKNOWN` | The requested run directory or snapshot does not exist. | List `runs/` and choose an existing run id. |
| `RUN_STATE_INVALID` | `runs/<run_id>/run-state.json` is malformed or has the wrong schema shape. | Repair or restore the run-state snapshot before continuing. |
| `RUN_TRANSITION_INVALID` | A requested state transition is not allowed by the run state machine. | Move only to an allowed next state, or use `finish` for terminal verdicts. |
| `RUN_TERMINAL` | A command attempted to mutate a terminal run. | Start a new run so the old terminal result remains auditable. |
| `FINAL_VERDICT_REQUIRED` | `finish` omitted the terminal verdict. | Pass `--final-verdict complete`, `blocked_on_sources`, `no_ship`, or `failed`. |
| `EVENT_DATA_INVALID` | `event --data-json` was not a JSON object. | Pass a JSON object or omit `--data-json`. |
| `COVERAGE_REQUIRED` | `question_resolve.py answer --require-coverage` could not find the selected coverage manifest. | Initialize or select a coverage manifest under `sources.coverage_dir`, then evaluate it before answering. |
| `COVERAGE_BLOCKED` | The selected coverage manifest evaluated to `blocked` or `pending`, so the answer cannot be marked complete. | Resolve failed required facets with accepted sources or blocking source requests before retrying. |
| `COVERAGE_MANIFEST_INVALID` | Coverage manifest YAML is malformed, unsafe to select, for another slug, or violates the manifest schema. | Fix the manifest under `sources/coverage/<slug>.yml`. |
| `COVERAGE_MANIFEST_EXISTS` | `coverage_manifest.py init` would overwrite an existing manifest. | Use the existing manifest, choose another slug, or pass `--force` deliberately. |
| `COVERAGE_CLAIM_PROBE_INVALID` | A coverage facet has malformed bounded negative-probe metadata. | Record only `method_or_artifact_existence` probes with arXiv and OpenAlex results, zero exact matches, and the required limitation text. |
| `COVERAGE_FACET_UNKNOWN` | `set-facet` referenced a facet id not present in the manifest. | Choose a facet id from the manifest. |
| `COVERAGE_POLICY_UNKNOWN` | A manifest or template used an unknown evidence path, source policy, freshness policy, or identity policy. | Use the identifiers documented in `docs/evidence-policies.md`. |
| `COVERAGE_TEMPLATE_INVALID` | `init --template` could not read or normalize the declarative template. | Fix the template YAML before initializing the manifest. |
| `QUERY_MISSING` | Retrieval query text is missing. | Provide query terms. |
| `QUESTION_UNKNOWN` | Referenced question slug does not exist. | Use an existing `wiki/questions/` slug. |
| `REQUEST_UNKNOWN` | Referenced source-request id is unknown. | List requests and choose an existing id. |
| `CANDIDATE_UNKNOWN` | Referenced discovery candidate id is unknown. | List candidates with `discover_sources.py candidates list` and choose an existing id. |
| `SOURCE_UNKNOWN` | Referenced manifest source id is unknown. | Inventory sources and choose an existing source id. |
| `TOOLING_MISSING` | A packaged or sibling script is missing. | Restore or upgrade workspace scripts. |
| `INTAKE_FIELD_TOO_LONG` | A question batch includes an over-limit `question`, `text`, `summary`, or `context` field. | Shorten oversized intake fields before retrying. |
| `INTAKE_TOTAL_CAP_EXCEEDED` | A question batch would exceed `run.max_open_questions_total`. | Resolve, defer, reject, or raise the total cap after reviewing the backlog. |
| `INTAKE_RATE_LIMITED` | A question batch would exceed `run.max_intake_per_hour`. | Retry after the intake window expires or raise the hourly cap deliberately. |
| `INTAKE_BATCH_TOO_LARGE` | An MCP intake call exceeds `run.max_mcp_intake_batch_questions`. | Submit a smaller MCP batch or raise the MCP batch cap deliberately. |
| `HANDOFF_SIGNATURE_INVALID` | A configured handoff secret requires a valid HMAC signature, but the handoff was unsigned or changed. | Sign the handoff with the configured secret, or unset the secret to stay in unsigned compatibility mode. |
| `ACQUISITION_DISABLED` | Optional provider-backed acquisition is disabled. | Enable acquisition explicitly or use manual source delivery. |
| `DISCOVERY_DISABLED` | Optional source discovery is disabled. | Set `integrations.discovery.enabled: true` to opt in; discovery still performs no network I/O until a provider transport is implemented. |
| `DISCOVERY_PROVIDER_DISABLED` | A discovery provider route is not allow-listed for the workspace. | Add the provider to `integrations.discovery.providers` or choose an enabled discovery route. |
| `DISCOVERY_NETWORK_ERROR` | A discovery provider request failed due to network or server errors. | Retry later, check network access, or lower request volume. |
| `DISCOVERY_RESPONSE_INVALID` | A discovery provider response was malformed or missing required data. | Retry later or inspect the provider response manually. |
| `SEARCH_PROVIDER_DISABLED` | No search provider is configured for `discover_sources.py search`. | Configure `integrations.discovery.search` with a fixture, command, or http provider. |
| `SEARCH_PROVIDER_FAILED` | The configured search command or fixture could not produce results. | Check the configured command/fixture path and rerun. |
| `GITHUB_AUTH_REQUIRED` | GitHub returned an authentication-required response. | Set a valid `GITHUB_TOKEN` in the environment, or unset an invalid token to use unauthenticated discovery or acquisition. |
| `GITHUB_RATE_LIMITED` | GitHub rate-limited the request. | Retry later, lower request volume, or set `GITHUB_TOKEN` for a higher rate limit. |
| `GITHUB_REPO_INVALID` | A GitHub acquisition repository, URL, or ref was missing or malformed. | Pass exactly one of `--repo owner/repo` or `--url`, and a valid `--ref` for archive downloads. |
| `GITHUB_NOT_FOUND` | A GitHub repository or ref was not found or is not accessible. | Verify the owner/repo and ref exist and are readable with the current token. |
| `GITHUB_RELEASE_UNAVAILABLE` | No matching GitHub release (latest or `--tag`) was available. | Choose a repository or tag with a published release, or capture a source archive instead. |
| `GITHUB_ARCHIVE_TOO_LARGE` | A GitHub source archive exceeds the configured size limit. | Raise `integrations.acquisition.github.max_archive_bytes` after review, or capture a smaller ref. |
| `ACQUISITION_PROVIDER_DISABLED` | Requested provider is not allow-listed for the workspace. | Add the provider to `integrations.acquisition.providers` or choose an enabled provider. |
| `ACQUISITION_LIMIT_EXCEEDED` | Requested downloads exceed the workspace acquisition limit. | Lower the request count or raise the limit after reviewing provider constraints. |
| `ARXIV_ID_INVALID` | arXiv identifier syntax is invalid. | Pass a versioned post-2007 arXiv id. |
| `ACQUISITION_NETWORK_ERROR` | Provider request failed due to network or server errors. | Retry later, check network access, or lower request volume. |
| `ACQUISITION_RESPONSE_INVALID` | Provider response was malformed or missing required data. | Retry later or inspect the provider response manually. |
| `ACQUISITION_ARCHIVE_UNSAFE` | Downloaded archive has unsafe paths or unsupported members. | Reject the archive or inspect it outside the workspace. |
| `ACQUISITION_TARGET_EXISTS` | Acquisition target path already exists. | Move or review the existing raw evidence before retrying. |
| `OPENALEX_ID_INVALID` | OpenAlex work id or DOI syntax is invalid. | Pass an explicit OpenAlex work id or DOI. |
| `OPENALEX_RESOLUTION_UNCERTAIN` | OpenAlex query did not resolve to a safe exact work. | Inspect candidates manually and retry with an explicit id or DOI. |
| `OPENALEX_AUTH_REQUIRED` | OpenAlex returned an authentication-required response. | Set `OPENALEX_API_KEY` in the process environment and rerun. |
| `OPENALEX_RATE_LIMITED` | OpenAlex rate-limited the request. | Retry later, reduce request volume, or authenticate. |
| `OPENALEX_PDF_UNAVAILABLE` | No usable open-access PDF was available. | Choose another work or deliver the source manually. |
| `NOT_IMPLEMENTED` | Requested provider command is not implemented. | Choose an implemented command or add the missing adapter. |
| `CLAIM_HELD` | Another agent holds the question claim. | Use stale-claim recovery only when appropriate. |
| `CLAIM_NOT_STALE` | A requested claim steal is too early. | Wait or use the configured staleness threshold. |
| `STEAL_THRESHOLD_REQUIRED` | `--steal` lacks `--if-older-than`. | Pass both flags. |
| `STEAL_FLAG_REQUIRED` | `--if-older-than` lacks `--steal`. | Pass both flags. |
| `STEAL_NOT_APPLICABLE` | Steal was requested for an open question. | Remove `--steal`. |
| `STATUS_NOT_CLAIMABLE` | Question status cannot be claimed. | Claim only open questions or stale in-progress claims. |
| `STATUS_NOT_RELEASABLE` | Question status cannot be released. | Release only held in-progress claims. |
| `STATUS_NOT_RESOLVABLE` | Question status cannot be resolved. | Resolve only open or in-progress questions. |
| `STATUS_NOT_REOPENABLE` | Question status cannot be reopened. | Reopen only `blocked` questions. |
| `SOURCE_NOT_NORMALIZED` | A reopen source id has no normalized record yet. | Inventory and normalize the delivered source before reopening. |
| `QUESTION_NOT_CLAIMED` | Resolution requires an explicit claim or override. | Claim the question first or pass `--allow-unclaimed`. |
| `ANSWER_SOURCE_REQUIRED` | Answer resolution lacks cited source ids. | Pass at least one `--source-id` or use `--allow-uncited` deliberately. |
| `ANSWER_PAGE_INVALID` | Answer page path is outside the accepted workspace scope. | Pass a workspace-relative answer page under the wiki root. |
| `ANSWER_PAGE_MISSING` | Referenced answer page does not exist. | Create the answer page before marking the question answered. |
| `REQUEST_NOT_LINKED` | Source request is not linked to the question being blocked. | Link the request to this question slug before using it. |
| `RESOLUTION_REASON_INVALID` | Terminal resolution reason is empty or missing. | Pass a non-empty blocked, deferred, or rejected reason. |
| `VALUE_INVALID` | Required option value is empty. | Pass non-empty option values. |
| `SLUG_INVALID` | Question slug syntax is invalid. | Pass a plain slug, not a path. |
| `SLUG_UNKNOWN` | Question page does not exist. | Use an existing question slug. |
| `PAGE_INVALID` | Question page frontmatter is invalid. | Fix the page frontmatter. |
| `AGENT_ID_INVALID` | `--agent-id` is empty. | Pass a non-empty agent id. |

## Step 2: Deploy With Handoff Correlation

Initialize from a setup profile (full schema: [workspace-init-profile.md](workspace-init-profile.md)). Two profile features matter for the chain:

- the optional `handoff` block carries upstream correlation IDs,
- the `questions` list seeds the backlog so research can start immediately.

```yaml
workspace_init:
  schema_version: "0.1"
  target_path: ../my-research-workspace
  handoff:
    task_id: chain-task-0042
    requested_by: planner-agent
    chain_run_id: run-2026-06-09-a
  project:
    name: my-research-workspace
    description: Research workspace for a specific topic.
    owner_goal: Answer the planner's open research questions.
    language: en
  domain_pack:
    enabled: false
  domain_guidance:
    mode: none
    rationale: Generic starter taxonomy is sufficient.
  raw:
    immutable: true
    source_roots:
      - raw/papers
      - raw/links
  claim_strictness: structured_claims
  ingest:
    claim_extraction: true
  outputs:
    supported_formats:
      - markdown
      - json
  integrations:
    git:
      snapshot_user_edits: explicit
  questions:
    - question: What evaluation benchmarks matter for reasoning?
      priority: high
      origin: parent_agent
  assumptions:
    - Generic wiki taxonomy is sufficient for the first setup pass.
  skipped_decisions:
    - No network fetching during initialization.
```

```bash
evidence-wiki init --profile /path/to/workspace-init.yml --dry-run   # preview
evidence-wiki init --profile /path/to/workspace-init.yml             # create
```

### Planner-Driven Domain Pack Creation

If a planner needs a reusable domain pack before deployment and no existing
pack clearly matches, delegate that pre-deploy step to
`skills/domain-pack-create.md`. The skill infers from the orchestrator brief,
drafts guidance-only pack files, runs `evidence-wiki pack validate --path`,
and deploys a smoke workspace with the new pack before returning control to
the deploy step.

The `handoff` block accepts `task_id`, `requested_by`, and `chain_run_id`. All are free-form non-empty strings; at least one must be present; unknown keys are rejected. Accepted values are persisted verbatim into the created workspace's `research.yml` under `project.handoff` and flow through every status report, so upstream systems can correlate workspace state with their own task records.

For high-trust deployments, set `EVIDENCE_WIKI_HANDOFF_SECRET` before `evidence-wiki init` or write a workspace-local `.research-handoff-secret` file. The environment variable wins over the sidecar; blank values mean unsigned compatibility mode. When a secret is configured, init stores only `project.handoff_signature` (`hmac-sha256:<hex>`) beside `project.handoff`; the secret itself is never written to `research.yml`, and the starter `.gitignore` ignores the sidecar. `workspace_status.py` reports `project.handoff_signature_status` as `verified`, `invalid`, `unsigned`, or `unconfigured`.

## PM Subagent Handoff Envelope

The deployed workspace's `project.handoff` block is upstream correlation
metadata. The PM subagent handoff envelope is the per-delegation payload the PM
agent gives to child agents while one controller-managed run is active. It is a
runtime contract, not a new `research.yml` schema, and must contain only
non-secret identifiers, counters, provider names, and workspace-relative paths.

Required envelope fields:

| Field | Meaning |
|-------|---------|
| `task_id` | Upstream task correlation id, copied from `project.handoff` when available. |
| `chain_run_id` | Upstream chain or orchestrator run id, copied from `project.handoff` when available. |
| `run_id` | PM controller run under `runs/<run_id>/run-state.json`. |
| `domain_pack` | Domain pack name/version or `null` when the generic starter guidance is used. |
| `evidence_paths` | Workspace-relative raw, manifest, normalized, request, candidate, or report paths the delegate may read or update. |
| `question_batch` | Question slugs or intake batch metadata assigned to this delegate. |
| `budgets` | Per-run limits from `workspace_status.py` plus any remaining counters the PM is enforcing. |
| `allowed_providers` | Provider ids the delegate may use, such as `manual`, `arxiv`, `openalex`, `github`, `web`, or `search`; an empty list means no provider-backed acquisition is allowed. |

All child agents for one PM run use the same `run_id` and one
`runs/<run_id>/run-state.json`. Child agents use distinct `agent_id` values only
for attribution in claim, resolve, discovery, acquisition, event, and report
records; they do not create sibling run-controller artifacts for the same PM
run.

Delegation by phase:

- **Discovery** receives `run_id`, `question_batch`, open source-request paths,
  candidate paths, `domain_pack`, `budgets`, and discovery-capable
  `allowed_providers`. It writes candidate records and selection/rejection
  audit trails only; candidates are not evidence.
- **Acquisition** receives selected candidates or source-request routes,
  `evidence_paths`, `allowed_providers`, and the same `run_id`. It delivers raw
  files with provenance sidecars, inventories and normalizes them, fulfills
  source requests, and reopens blocked questions when normalized evidence exists.
- **Research** receives `question_batch`, normalized `evidence_paths`,
  `budgets`, and the same `run_id`. It claims and resolves assigned questions
  through `question_claim.py` and `question_resolve.py`.
- **Verification** receives export, status, lint, run-report, citation
  verification, and publication-readiness paths plus the same `run_id`. It
  reads `workspace_status.py --run-id RUN_ID --format json`, `run_report.py
  --run-id RUN_ID --format json`, `export_answers.py --format json`, and
  `publication_readiness.py --format json` to decide whether the PM may ship
  results.

If the PM cannot spawn, contact, or delegate to a required child agent, it must
leave a durable controller trail and stop in a terminal state. It must never
silently stop after an attempted delegation. First append a non-secret event:

```bash
python3 scripts/run_controller.py event \
  --run-id run-2026-06-29T010203Z \
  --agent-id pm-agent \
  --event-type delegation_failed \
  --message "Could not spawn discovery child agent." \
  --data-json '{"phase":"discovery","delegate_role":"discovery","outcome":"child_spawn_failed","recoverable":false}' \
  --format json
```

Then finish the run as `failed` when orchestration or tooling broke:

```bash
python3 scripts/run_controller.py finish \
  --run-id run-2026-06-29T010203Z \
  --agent-id pm-agent \
  --final-verdict failed \
  --reason "Could not spawn discovery child agent." \
  --format json
```

Use `no_ship` instead when delegation is impossible because policy or provider
constraints make the requested result unpublishable, not because tooling broke:

```bash
python3 scripts/run_controller.py finish \
  --run-id run-2026-06-29T010203Z \
  --agent-id pm-agent \
  --final-verdict no_ship \
  --reason "No allowed provider can satisfy the requested fetch policy." \
  --format json
```

Finally verify the terminal state through the aggregate status surface:

```bash
python3 scripts/workspace_status.py --run-id run-2026-06-29T010203Z --format json
```

The status document must expose `run_controller.terminal: true`,
`run_controller.final_verdict`, `run_controller.blocking_reason`, and the
failure count when the verdict is `failed`.

## Step 3: Deliver Sources

Place evidence files under the configured `raw/` source roots, then from the workspace root:

```bash
python3 scripts/source_inventory.py --report
python3 scripts/normalize_sources.py
```

Orchestrators that need a parseable inventory handoff should request JSON mode:

```bash
python3 scripts/source_inventory.py --report --format json
```

The JSON inventory report uses the shared error envelope on fatal failures. A
dry run without `--report` continues to stream manifest records as JSONL.

Raw files are immutable once delivered; add newer versions as new files. The status document's `sources.unnormalized` count (step 5) reports how much delivered evidence is not yet searchable.

Automated deliveries follow the full delivery contract in [source-delivery.md](source-delivery.md): target roots by evidence kind, atomic delivery, and a `.provenance.yml` sidecar next to every delivered artifact (origin URL, license, retrieval time, agent id, checksum). Sidecar provenance is merged into the manifest, propagated into normalized records, and surfaces in exported citations.

If a workspace is configured for optional acquisition, fetch agents must also
follow [acquisition.md](acquisition.md): acquisition is disabled by default,
provider IDs are allow-listed, downloads stay under `raw/`, and provider terms
plus license uncertainty must be surfaced before handoff. The executable
fetch-agent playbook is `skills/research-acquire.md`.

Before acquisition, an optional discovery stage can propose candidate sources
without fetching them. [source-discovery.md](source-discovery.md) defines the
`source_candidate` schema (durably stored in `sources/discovery/candidates.jsonl`),
its trust and reasoning fields, `network_io_executed`, and the shared policy fields
`evidence_path`, `source_policy`, `freshness_policy`, and `identity_policy`.
Official-source evaluation records also carry `evidence_areas`,
`source_request_id`, `selection_status`, and `fetch_status` so discovery,
selection, rejection, and pending manual delivery are visible from
`sources/discovery/candidates.jsonl`.
Every durable candidate has exactly one origin: `request_id`, `seed_source_id`,
or the exploratory `discovery_run_id`. Candidates are proposals, not evidence:
they become evidence only when a candidate selected for a source request
(`selected_for_request_id`) is fetched into `raw/` with a provenance sidecar
through the delivery and acquisition contracts above.

Autonomous source delivery must close the candidate lifecycle before any fetch:

```bash
python3 scripts/discover_sources.py --format json candidates list --request-id req-1a2b3c4d5e
python3 scripts/discover_sources.py --format json candidates transition --candidate-id cand-1a2b3c4d5e --expected-state proposed --to-state reviewed --reason "authority and evidence fit reviewed" --actor agent-pm --run-id run-2026-07-11T120000Z
python3 scripts/discover_sources.py --format json candidates select --candidate-id cand-1a2b3c4d5e --expected-state reviewed --request-id req-1a2b3c4d5e --reason "official_primary trust tier satisfies the linked source policy" --actor agent-pm --run-id run-2026-07-11T120000Z
python3 scripts/discover_sources.py --format json candidates reject --candidate-id cand-9z8y7x6w5v --expected-state proposed --reason "lower-trust duplicate of the selected source" --actor agent-pm --run-id run-2026-07-11T120000Z
```

The selected candidate's `trust_tier` and policy fit belong in the selection
reason, discarded candidates need concrete rejection reasons, and the later
provider command must carry both `--request-id req-1a2b3c4d5e` and
`--candidate-id cand-1a2b3c4d5e`. A `manual_review` facet policy verdict during
an autonomous run is an in-run repair signal: re-review, select, restamp,
rerun `source_inventory.py --report` and `normalize_sources.py --all`, then
rerun the coverage/readiness gate. Do not treat it as a wait-for-human state.

The canonical lifecycle states are `proposed`, `reviewed`, `selected`,
`rejected`, `deferred`, `fetched`, `failed`, and `superseded`. Orchestrators
must use the transition table in [source-discovery.md](source-discovery.md),
send `--expected-state` from the freshly read record, and stop on
`CANDIDATE_STATE_STALE`, `CANDIDATE_TRANSITION_INVALID`, or
`CANDIDATE_CORRELATION_CONFLICT` rather than retrying blindly. `rejected`,
`fetched`, and `superseded` are terminal. A completed delivery transitions
`selected` to `fetched` only after inventory has produced the cited
`--source-id`; acquisition failure transitions `selected` to `failed` with its
reason. Every applied transition records actor, prior/new state, reason,
request/run/candidate/source correlation, and UTC time in the append-only audit.

The discovery command surface is `scripts/discover_sources.py`, with read-only
`search`, `legal`, `github`, `authors`, and `companions` subcommands. Discovery is disabled by
default: until `integrations.discovery.enabled: true` opts the workspace in,
every command refuses with `DISCOVERY_DISABLED` before any network I/O.

The `github` subcommand is implemented: when discovery is enabled it searches
GitHub repository metadata through a bounded, transport-injected adapter, writes
ranked `source_candidate` records to `sources/discovery/candidates.jsonl`, and
records `network_io_executed: true`. It still never clones a repository,
downloads an archive, or reads file contents, and `GITHUB_TOKEN` is read from the
environment only (never stored or emitted). The `search` subcommand is also
implemented through a provider-neutral interface (`fixture`, `command`, or `http`
backend configured under `integrations.discovery.search`; no commercial API is
hard-coded). It **plans by default**: it expands the research need into a small,
bounded set of explained queries (read-only, no network — explainable before any
backend is contacted) and runs them only with `--execute`, refusing with
`SEARCH_PROVIDER_DISABLED` when execution is requested but no provider is
configured. Results are **ranked by the documented trust-tier policy**, not the
provider's ordering: official sources (`.gov`/`.mil`, the optional
`integrations.discovery.search.official_domains` list, an official/legal
allowlist, or a provider `official` hint) outrank higher-ranked generic pages;
suspicious downloads, mirrors, and lower-trust duplicates of an official source
are emitted with `recommended_action: reject` and rationale. The `legal`
subcommand is also implemented: it is profile-driven (reads a jurisdiction profile
from `sources/jurisdictions.yml`, validated by `discover_sources.py jurisdictions
validate`), plans official-source-first legal queries by default, and with
`--execute` ranks candidates so official gazette/legislature/regulator/court
sources outrank aggregators while recognized secondary legal databases are kept as
supplemental. The `authors` subcommand extracts a bounded author seed list (name,
ORCID, affiliation, source, confidence) from a normalized paper source and any
provider author metadata, read-only and with no network I/O; it never infers
personal data. With `--discover-publications` it resolves each seed author to an
OpenAlex identity (by ORCID when present, otherwise by name) and proposes that
author's other works as related-publication candidates, ranked by identity
confidence, topical similarity to the analyzed paper, year, citations, and
open-access availability — clearly flagging ambiguous name matches and rejecting
out-of-scope works while never downloading anything. In all cases discovery never
fetches the candidate it proposes — acquisition selection stays explicit. The
agent playbook for the discovery stage is
[skills/research-discover.md](../skills/research-discover.md): plan read-only,
review candidate trust tiers and rationale, prefer official sources for legal
questions, select explicitly, run `plan-fetch`, and hand off to `research-acquire`
only for selected candidates.

```bash
python3 scripts/discover_sources.py --format json search --query "emissions reporting" --jurisdiction us-federal   # plan only
python3 scripts/discover_sources.py --format json search --query "code of federal regulations" --domain-allow govinfo.gov --execute
python3 scripts/discover_sources.py --format json legal --jurisdiction us-federal --topic "emissions reporting"
python3 scripts/discover_sources.py --format json github --query "retrieval augmented generation"
python3 scripts/discover_sources.py --format json authors --source-id paper:2601.00001v1
python3 scripts/discover_sources.py --format json authors --source-id paper:2601.00001v1 --discover-publications --max-results 10
```

Reviewing and selecting candidates is a separate, offline `candidates`
subcommand that reads and updates `sources/discovery/candidates.jsonl` and never
contacts a provider (it runs even when discovery is disabled). `select` only
links a candidate to a source request — or mints one with `--create-request` —
and `reject` records a reason; neither fetches. `transition` handles review,
deferral, acquisition success/failure, and supersession bookkeeping without
performing acquisition. Every applied change writes an atomic, lock-guarded
state update plus a durable event to `sources/discovery/audit.jsonl`:

```bash
python3 scripts/discover_sources.py --format json candidates list --status new
python3 scripts/discover_sources.py --format json candidates list --state proposed
python3 scripts/discover_sources.py --format json candidates transition --candidate-id cand-1a2b3c4d5e --expected-state proposed --to-state reviewed --reason "review complete"
python3 scripts/discover_sources.py --format json candidates select --candidate-id cand-1a2b3c4d5e --expected-state reviewed --request-id req-1a2b3c4d5e
python3 scripts/discover_sources.py --format json candidates reject --candidate-id cand-other0000 --expected-state proposed --reason "lower-trust mirror of the official source"
```

After review, `scripts/workspace_status.py --format json` exposes a top-level
`candidates` section with lifecycle counts by evidence path, trust tier,
`selection_status`, `fetch_status`, recommended action, official candidates,
aggregator candidates, selected/request-linked status, fetched status, and
rejection reason. R6-style evaluations can inspect that status block after
selection/rejection and before acquisition to prove ranking and rejection
behavior happened without treating candidates as fetched evidence.

Evidence gaps flow the other way as structured source requests (`sources/source-requests.jsonl`, managed by `scripts/source_requests.py`). A fetch agent's loop is:

```bash
python3 scripts/source_requests.py list --status open --format json
python3 scripts/source_requests.py plan-fetch --request-id req-1a2b3c4d5e --format json
# ... deliver the requested files with provenance sidecars ...
python3 scripts/source_inventory.py --report
python3 scripts/normalize_sources.py --all
python3 scripts/source_requests.py fulfill --request-id req-1a2b3c4d5e --source-id paper:2601.00001v1
```

`plan-fetch` is read-only and emits provider command suggestions with
`network_io_executed: false`. Orchestrators must still enforce the acquisition
configuration before running a suggested provider command. When a request has
discovery candidates selected for it (`candidates select`, above), `plan-fetch`
also returns a `candidate_routes` array — an explicit `fetch_sources.py` command
for arXiv/OpenAlex/GitHub candidates, or a manual-delivery target for
official-legal/web/dataset candidates. For academic paper candidates, selected
route records copy provider-neutral `paper` metadata plus
`candidate_network_io_executed` and `provider_budget`; the planner prefers those
fields over URL parsing so OpenAlex/arXiv candidates produce exact existing
`fetch_sources.py` syntax without inventing commands. Warnings identify unknown
license metadata, non-open-access OpenAlex records, and uncertain provider
resolution before acquisition runs. When linked coverage facets name the request
in `blocking_request_ids`, the response includes `policy_source:
coverage_manifest`, report-level `policy_facets`, and route-level
`policy_alignment`/`policy_min_trust_tier`; warnings identify low-trust or
evidence-path-mismatched selected candidates (see
[source-delivery.md](source-delivery.md)).

For negative academic claim probes, orchestrators may run bounded provider
queries such as `fetch_sources.py openalex resolve --allow-unconfirmed`. A
normal unconfirmed result can be copied into coverage `claim_probe` metadata,
but it must not be treated as a delivered source, citation, or global
nonexistence finding.

When `research-acquire` fulfills a request linked to blocked question slugs, it
reopens those questions with `scripts/question_resolve.py reopen --slug SLUG
--agent-id AGENT --source-id MANIFEST_ID`, which refuses (`SOURCE_NOT_NORMALIZED`)
until the fulfilled manifest source also has a normalized record. `fulfill` only
links the source to the request; `reopen` is the deterministic verb that moves
the question `blocked` → `open` and attaches the delivered `source_id`.

## Step 4: Inject Questions Mid-Run

A planner can add validated question batches to a *running* workspace at any lifecycle point; seeding at deploy time is not the only entry path. From the workspace root:

```bash
python3 scripts/intake_questions.py --from-file batch.yaml --dry-run   # preview as JSON
python3 scripts/intake_questions.py --from-file batch.yaml --format json
```

Or through the package CLI from outside the workspace:

```bash
evidence-wiki questions add --target my-research-workspace --from-file batch.yaml
```

The batch document carries `schema_version`, an optional `handoff` correlation block, optional `handoff_signature`, and a `questions[]` list (`question` required; `text` alias, `id`, `priority`, `origin`, `summary`, `context` optional). When a handoff secret is configured, any batch with a `handoff` must include a valid `handoff_signature`; unsigned or tampered batches fail with `HANDOFF_SIGNATURE_INVALID` before anything is written. The whole batch is validated before anything is written; `question`/`text` and `summary` are capped at 1024 UTF-8 bytes, and `context` is capped at 8192 UTF-8 bytes. Duplicates against the existing backlog are skipped, never overwritten, so re-submitting a batch is idempotent. After deduplication, intake enforces `run.max_open_questions_total` and `run.max_intake_per_hour`; cap failures return `INTAKE_FIELD_TOO_LONG`, `INTAKE_TOTAL_CAP_EXCEEDED`, or `INTAKE_RATE_LIMITED` and write nothing. MCP intake also rejects oversized transport batches before delegation with `INTAKE_BATCH_TOO_LARGE` when `questions[]` exceeds `run.max_mcp_intake_batch_questions`. Created pages pass lint and appear in `question_status.py` and `workspace_status.py` immediately. The full batch and report schemas are in [question-api.md](question-api.md).

## Step 5: Poll Status And Detect Completion

```bash
python3 scripts/workspace_status.py --format json
python3 scripts/workspace_status.py --check-complete --format json
python3 scripts/workspace_status.py --check-complete --format json \
  --questions-processed-this-run 0 \
  --source-requests-opened-this-run 0 \
  --releases-this-run 0 \
  --discovery-results-this-run 0 \
  --acquisition-downloads-this-run 0 \
  --github-archive-bytes-this-run 0 \
  --academic-provider-requests-this-run 0 \
  --web-downloads-this-run 0 \
  --manual-url-deliveries-this-run 0
```

The status document schema is specified field by field in [workspace-status.md](workspace-status.md). The readiness verdict is one of:

- `complete`: all questions resolved, validation checks pass.
- `in_progress`: actionable questions remain.
- `blocked_on_sources`: only blocked questions remain, and each blocked question has `blocking_request_ids` linked to valid open source requests. The verdict reasons name those linked open request IDs, so the orchestrator can route them to a fetch agent and resume per [source-delivery.md](source-delivery.md). A blocked question with no linked open request is `attention_required`, because the acquisition gap is not actionable from artifacts.
- Clean blocking rule: blocked question with no linked open request is `attention_required`.
- `attention_required`: smoke validation failed, lint reported HIGH issues, or a blocked question has missing, closed, or unrelated `blocking_request_ids`.
- `attention_required`: smoke validation failed or lint reported HIGH issues; the workspace needs maintenance before results can be trusted.

`--check-complete` maps verdicts to exit codes: `0` complete, `1` in progress, `3` blocked on sources, `4` attention required, and `2` workspace unreadable. A polling loop can branch on those exit codes directly. Budget exhaustion is reported separately as `readiness.budget_state.should_stop` and `readiness.budget_state.stop_reasons` inside an `in_progress` payload, so read those fields on exit `1` when per-run counters are supplied.

## Step 6: Collect Results

```bash
python3 scripts/export_answers.py --format json
python3 scripts/publication_readiness.py --format json
python3 scripts/publication_readiness.py --format json bundle --run-id <run_id>
evidence-wiki questions export --target my-research-workspace   # equivalent
```

The export gives downstream agents everything without reading the wiki: per question, the answer summary, the workspace-relative `answer_page`, the grounding `source_ids`, and `citations[]` resolving each source id against the manifest (raw paths, normalized record, title, provenance `origin_url`/`license`, and academic venue/status metadata when present). Coverage-required answers carry `coverage_status`, `coverage_verdict`, `coverage_facets`, `failed_facets`, `linked_source_requests`, `missing_source_request_ids`, and `unconfirmed_claims` so a downstream agent can distinguish grounded, blocked, out-of-scope, request-backed, and bounded unconfirmed facets. Blocked questions carry `blocked_reason`, `blocking_request_ids`, linked `blocking_requests`, and `missing_blocking_request_ids`, while verified answers carry `confidence`/`evidence_strength` when the verification pass (`skills/research-verify.md`) recorded them. The envelope repeats `project.handoff` so results correlate with upstream task records, and missing answer pages or unknown source ids surface as `warnings[]`, never crashes. When a handoff secret is configured, export first verifies `project.handoff_signature` and refuses unsigned or changed handoff metadata with `HANDOFF_SIGNATURE_INVALID`. Field-by-field schema: [question-api.md](question-api.md).

`scripts/question_status.py --format json` remains available as the lighter backlog-counts surface.

Publication readiness is the final local publication gate. It reports
`network_io_executed: false` and one of `ship`, `no_ship`,
`blocked_on_sources`, or `attention_required`. Reasons are grouped under
`coverage`, `source_quality`, `discovery_quality`, `citation_identity`,
`currentness`, `curation`, and `safety`. The `bundle --run-id` command writes
`runs/<run_id>/evaluation/` with `publication-readiness.json`, status, export,
lint, citation verification, candidate summary, and source-request summary
artifacts. Supervisors should cite those files instead of chat logs.

### Official-Source Regression Replay

The network-free official-source replay fixture is
`tests/fixtures/official-source-regression-workspace`. Replay it from a copied workspace in
this order so the final verdict is derived from artifacts rather than narrative:

```bash
.venv/bin/python scripts/source_inventory.py --report --format json
.venv/bin/python scripts/coverage_manifest.py evaluate --slug official-source-regression --format json
.venv/bin/python scripts/export_answers.py --format json
.venv/bin/python scripts/workspace_status.py --check-complete --format json
.venv/bin/python scripts/publication_readiness.py --format json
.venv/bin/python scripts/run_report.py --run-id run-2026-07-04-official-source --format json
```

The expected terminal workspace verdict is `blocked_on_sources`: answered
questions keep their official citations, while blocked questions carry
`blocking_request_ids` linked to open source requests.
Selected official web candidates should keep `fetch_status` set to
`pending_manual_delivery` until the canonical raw HTML and provenance sidecar
arrive.

### PM Supervisor Checklist

Before finishing a PM-controlled run as publishable, verify these artifact-backed
checks:

- run-state created by `run_controller.py start` for the active `run_id`;
- candidates ranked by evidence path and trust tier, with lower-trust rejects kept in the candidate store;
- selected candidates fetched or explicitly blocked through source requests;
- coverage evaluated for every required high-stakes facet;
- status polled with `workspace_status.py --run-id RUN_ID --format json`;
- export generated with `export_answers.py --format json`;
- publication readiness run with `publication_readiness.py --format json`;
- secret scan clean in the publication-readiness `safety` category;
- final verdict recorded in the run-controller terminal state.

Explicit no-ship triggers include `no_ship`, `blocked_on_sources`, or
`attention_required` from publication readiness; failed coverage, citation
identity mismatch, stale currentness, missing curation metadata for cited
automated web deliveries, rejected or missing required sources, and any secret
leak finding. Copy the trigger into the run-controller finish reason rather than
hiding it in prose.

Academic runs that need publication-grade citation identity can also emit a
separate citation-verification artifact:

```bash
python3 scripts/verify_citations.py --format json
python3 scripts/verify_citations.py --format json --source-id paper:2601.00001v1
python3 scripts/verify_citations.py --format json --live --provider arxiv
python3 scripts/verify_citations.py --format json --live --provider openalex
```

Default mode is network-free and scans citation-bearing academic manifest and
normalized records. It reports `verified` only when local arXiv/OpenAlex/DOI
metadata is backed by acquisition provenance from `fetch_sources.py/arxiv` or
`fetch_sources.py/openalex`; otherwise valid local metadata is
`skipped_no_live`. Live mode re-resolves IDs against the selected provider only
when `integrations.acquisition.enabled: true` and the provider is allow-listed.
The report's `overall_result` is `verified` only when every selected citation is
verified; otherwise it is `no_ship`, with per-source `mismatch`, `not_found`,
`skipped_no_live`, or `insufficient_metadata` reasons. Provider request URLs and
API keys are never serialized into the artifact or error envelope.

## Optional MCP Surface

MCP-speaking orchestrators may use the stdio server instead of shelling out for
the read/append-only surfaces:

```bash
evidence-wiki serve-mcp --target my-research-workspace
```

The server exposes `workspace_status`, `question_status`, `query_index`,
`intake_questions`, `export_answers`, and `source_requests_list`. It returns the
same JSON payloads as the scripts in MCP `structuredContent`; the scripts and
package CLI remain the canonical contract. Boundary and handshake details:
[mcp-server.md](mcp-server.md).

Threat model: the MCP server assumes a trusted single-client stdio subprocess.
It has no built-in authentication and must not be bridged to a network transport
or shared among untrusted peers. See the threat-model section in
[mcp-server.md](mcp-server.md) before deploying it behind any mediator.

## The Research Agent Run Loop

Between steps 4 and 5, the research agent works the backlog unattended following `skills/research-run.md`. The deterministic state lives in three machine surfaces an orchestrator can also use directly:

**Question claiming and resolution** (`scripts/question_claim.py`, `scripts/question_resolve.py`): multiple agents share one backlog without duplicating work. `claim --slug S --agent-id A` transitions `open` to `in_progress` and writes `claimed_by`/`claimed_at` atomically under a per-question lock file at `wiki/questions/.locks/<slug>.lock`; that stable lock survives temp-file replacement of the question page, so competing agents always re-read current state before mutating it. A question held by another agent is refused with exit `3` and a machine-readable refusal naming the holder. `release --slug S --agent-id A` reverts to `open`. Stale-claim recovery is orchestrator-mediated and never automatic: `claim --steal --if-older-than HOURS` transfers only claims older than the threshold. `question_resolve.py answer|block|defer|reject --slug S --agent-id A ...` applies terminal outcomes under the same stable lock, requires the same holder unless `--allow-unclaimed` is explicit, validates answer pages/source IDs/source requests, requires at least one `--source-id` for `answer` unless `--allow-uncited` is explicit, and can require a passing coverage manifest for high-stakes answers with `--require-coverage`. Coverage-gated answers should also carry `grounding` entries (`claim`, `source_id`, `quote`, optional `location_hint`) and use `--require-grounding`; the resolver invokes `scripts/verify_quotes.py` logic and refuses missing grounding (`GROUNDING_REQUIRED`) or quote mismatches (`GROUNDING_QUOTE_INVALID`) before writing terminal state. `--coverage-manifest PATH` selects a workspace-relative manifest under `sources.coverage_dir`; without `--require-coverage`, it does not gate ordinary answers. A successful gated answer records `coverage_required: true`, `coverage_manifest: sources/coverage/<slug>.yml`, `grounding_required: true`, and `answered_by` in question frontmatter. A later independent verifier can run `scripts/verify_quotes.py --slug S --write --verified-by verifier-agent`; `verified_by` must not equal `answered_by` for final high-stakes verification. A successful blocked resolution with `--request-id` records `blocking_request_ids` in stable de-duplicated order; source IDs support answers, while blocking request IDs explain why an answer cannot be grounded yet. Successful resolutions clear claim fields and append a structured log entry. `question_status.py --format json` exposes `claimed_by`/`claimed_at` and `blocking_request_ids` per question, and lint reports `in_progress` questions without claim fields (MEDIUM), stale claims (LOW), answered coverage-required questions with missing, blocked, or invalid coverage manifests (HIGH), missing grounding (HIGH), and same-agent quote verification (MEDIUM).

**Run controller state** (`runs/<run_id>/run-state.json`, surfaced in the status document's `run_controller` section): a PM run records its current state, allowed next states, candidate counts, coverage counts, budget state, budget overrides, failure count, recovery history, heartbeat timestamp, and final verdict. `workspace_status.py --run-id RUN_ID --format json` reads an exact run; without `--run-id`, status reports the newest active run or, when no active run exists, the newest terminal run. Active runs whose latest heartbeat/event/update exceeds `run.stale_run_threshold_hours` report `run_controller.stale: true`. Recovery is explicit: `run_controller.py adopt --if-stale-hours HOURS` transfers stale ownership, and `run_controller.py abandon --if-stale-hours HOURS --reason REASON` fails a stale active run with machine reason `stale_run_abandoned`. `run_report.py` additionally emits `official_source_evaluation` with the artifact verdict, `blocked_request_ids`, open source requests, candidate summary, coverage summary, source summary, and budget state for final PM handoff. This lets an orchestrator distinguish `in_progress`, `blocked_on_sources`, `no_ship`, and `failed` from one status poll without reading `events.jsonl`.

**Run budgets** (`research.yml` `run` block, surfaced in the status document's `run` section): `max_questions_per_run`, `max_source_requests_per_run`, `max_releases_per_run`, `max_discovery_results_per_run`, `max_academic_provider_requests_per_run`, `max_web_downloads_per_run`, and `max_manual_url_deliveries_per_run` bound one unattended pass; `max_web_downloads_per_run` inherits the manual URL limit when unset. `max_acquisition_downloads_per_run` and `max_github_archive_bytes_per_run` are derived from acquisition config so provider download and byte limits have one source of truth. `max_open_questions_total`, `max_intake_per_hour`, and `max_mcp_intake_batch_questions` bound externally supplied intake. With a selected `run_id`, `readiness.budget_state` is artifact-derived from the run window and legacy counter flags are reported as `runner_reported` with `counter_divergence` on disagreement. Defaults are generous but finite. Wall-clock and token budgets belong to the orchestrator, not the workspace. The canonical stop conditions for a loop are:

- `workspace_status.py --check-complete` exits `0` (`complete`), `3` (`blocked_on_sources`), or `4` (`attention_required`),
- `readiness.budget_state.should_stop` is true, with `stop_reasons` naming the exhausted budget.

A loop implementation needs only `workspace_status.py` output to decide continue/stop.

**Run reports** (`scripts/run_report.py`): the agent captures a baseline at run start (`run_report.py baseline --output baseline.json`) and finishes with `run_report.py --baseline baseline.json --format json`. For controller-managed runs, `run_report.py --run-id RUN_ID --format json` loads the baseline path from `runs/<run_id>/run-state.json` when `--baseline` is omitted and includes the run id, state transitions, candidate counts, coverage counts, budget state, and final verdict in a top-level `run_controller` block. The baseline artifact includes the full `question_status.py --format json` snapshot, currently open source requests, current normalized source IDs with `normalized_at`, and a generated timestamp. The report (also written to `docs/run-reports/run-<UTC timestamp>.md`) carries backlog counts before/after, every question touched with its status transition, source requests opened/fulfilled during the window, normalized sources with `normalized_at >= baseline.generated_at`, and the current lint summary. Older normalized records without `normalized_at` are reported separately as `sources_normalized_legacy_date_match` with a warning because their date-only `updated` value cannot prove they were generated during the run. Legacy unmodified `question_status.py --format json` baselines remain accepted for v1 compatibility.

## Copy-Pasteable Sequence

```bash
evidence-wiki doctor --format json
evidence-wiki contract
evidence-wiki init --profile workspace-init.yml
cd my-research-workspace
python3 scripts/doctor.py --format json
# ... deliver files into raw/ ...
python3 scripts/source_inventory.py --report
python3 scripts/normalize_sources.py
python3 scripts/intake_questions.py --from-file batch.yaml --format json
python3 scripts/workspace_status.py --format json
# ... research agent works the backlog (skills/research-run.md):
python3 scripts/run_report.py baseline --output /tmp/run-baseline.json
python3 scripts/run_controller.py start --run-id run-2026-06-29T010203Z --agent-id agent-a --format json
python3 scripts/question_claim.py claim --slug <slug> --agent-id agent-a --format json
python3 scripts/question_resolve.py answer --slug <slug> --agent-id agent-a --answer-page wiki/synthesis/example.md --source-id <source:id> --format json
python3 scripts/question_resolve.py answer --slug <high-stakes-slug> --agent-id agent-a --answer-page wiki/synthesis/example.md --source-id <source:id> --require-coverage --require-grounding --format json
# ... resolve per skills/research-answer.md, repeat within budgets ...
python3 scripts/run_report.py --baseline /tmp/run-baseline.json --agent-id agent-a --format json
python3 scripts/run_report.py --run-id run-2026-06-29T010203Z --agent-id agent-a --format json
python3 scripts/workspace_status.py --check-complete --format json
python3 scripts/workspace_status.py --run-id run-2026-06-29T010203Z --format json
python3 scripts/verify_quotes.py --slug <high-stakes-slug> --format json
python3 scripts/verify_quotes.py --slug <high-stakes-slug> --write --verified-by verifier-agent
python3 scripts/verify_citations.py --format json
python3 scripts/export_answers.py --format json
python3 scripts/publication_readiness.py --format json
python3 scripts/publication_readiness.py --format json bundle --run-id run-2026-06-29T010203Z
# Optional tool-native equivalent for status, query, intake, export, and source-request listing:
evidence-wiki serve-mcp --target .
```

## Guarantees

- All commands above are deterministic and, except initialization, inventory, normalization, and question intake, read-only.
- Question intake is all-or-nothing and idempotent: invalid batches write nothing, and re-submitted batches skip duplicates instead of overwriting pages.
- Profiles without a `handoff` block behave exactly as before; the block is optional and additive, and intake/export carry it through to results.
- Machine-readable artifacts carry explicit schema versions; breaking shape changes bump them.
- The MCP server is optional and read/append-only; the CLI scripts remain canonical.
- Nothing in this contract installs hooks, starts background processes, or fetches remote content.
