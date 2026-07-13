# Source Manifest

`sources/manifest.jsonl` is the deterministic inventory of raw source assets in a research workspace. Each line is one JSON object. The manifest is generated from `raw.source_roots` in `research.yml`.

## Required Fields

- `id`: stable source identifier derived from the raw path.
- `kind`: asset classification.
- `raw_paths`: one or more POSIX-style paths relative to the workspace root.
- `status`: lifecycle status from `research.yml`, initially `discovered`.
- `detected_at`: UTC timestamp for when the source ID was first discovered.

## Optional Fields

- `metadata.extension`: lowercase file extension.
- `metadata.size_bytes`: file size at inventory time.
- `latex_root`: workspace-relative directory for a LaTeX paper bundle.
- `entrypoint`: bundle-relative top-level LaTeX file selected for normalization.
- `compiler`: LaTeX compiler from `00README.json` when available.
- `raw_pdf`: workspace-relative PDF path for a paper or PDF-only record.
- `pairing_status`: PDF/source pairing state: `paired`, `pdf_only`, `latex_only`, or `ambiguous`.
- `raw_fingerprint`: `sha256:`-prefixed content hash of the raw inputs for `paper`, `pdf`, `html`, and CSV/TSV `table` records (source files plus any paired PDF and provenance sidecars). Normalization re-generates a record only when this value changes, enabling incremental re-normalization. Omitted for kinds whose normalized output is not derived from raw bytes (links, codebase records).
- `url`: HTTP(S) URL parsed from a raw link file.
- `provenance`: delivery provenance merged from a `.provenance.yml` sidecar delivered next to the raw file or directory (see `source-delivery.md`): `origin_url`, `url`, `license`, `retrieved_at`, `retrieved_by`, `checksum`, `sha256`, `checksum_verified`, optional `request_id`, `candidate_id`, `terms_url`, `terms_note`, `notes`, official web metadata (`source_type`, `jurisdiction`, `publisher`, `date_metadata`, `evidence_usability_override`, `supported_evidence_areas`, `curation_notes`), standards registry metadata (`standards` mapping), currentness fields (`effective_date`, `publication_date`, `validity_period`, `date_not_available`, `source_status`), delivery failure fields (`delivery_failure_code`, `delivery_failure_detail`, `delivery_failure_remediation`), GitHub fields (`repository_owner`, `repository_name`, `repository_full_name`, `repository_artifact_kind`, `repository_ref`, `commit_sha`, `downloaded_archive_url`), academic enrichment fields (`provider_license_slug`, `license_source`, `openalex_title_lag`, `openalex_identity_conflict`, `openalex_reported_title`, `openalex_reported_authors`, `openalex_reported_publication_year`, `openalex_identity_evidence`, `doi_resolution`), plus the `sidecar_path` it was read from. Valid `delivery_failure_code` values are `tls_verification_failed`, `http_error`, `javascript_required`, `official_error_page`, `not_found`, `content_too_sparse`, `license_or_terms_unknown`, `robots_or_terms_blocked`, and `manual_review_required`; invalid codes are dropped with a warning. Sidecar files are never inventoried as sources themselves; malformed sidecars degrade to report warnings; checksum mismatches mark the record `review_required`. Non-null licenses must match the starter's in-repo SPDX allowlist; invalid license strings warn, mark the record `review_required`, and are not propagated. `license: null` is preserved as explicit uncertainty, while ambiguous provider license slugs are preserved in `provider_license_slug` with `license: unresolved` unless the adapter can map them to an unambiguous SPDX id. When OpenAlex enrichment records a wrong-work conflict, `doi_resolution` records the independent DOI.org/DataCite redirect-only HEAD check with `status`, `resolved_url`, and `matches_arxiv_id`; only an arXiv abs redirect for the local arXiv id sets `matches_arxiv_id: true`. Automated web curation checks use these provenance fields to count missing license/terms status, source notes, origin URLs, verified checksums, selected candidate ids, and audited evidence-usability overrides.
- `evidence_usable`: `false` when provenance records a delivery failure or unavailable/error source status. Omitted means usable for compatibility with older manifests.
- `unusable_evidence_reasons`: stable reason codes explaining why evidence cannot satisfy required facets, such as `source_status:error_page`, `delivery_failure_code:official_error_page`, or `delivery_failure_code:tls_verification_failed`.
- `metadata.codebase_source_type`: `repo_link`, `local_repo`, or `code_archive` for optional codebase architecture evidence.
- `metadata.codebase_output_dir`: generated artifact directory under `sources/` for codebase analysis output.

Future tasks may add fields such as extraction warnings or normalization status. Scripts should preserve unknown fields when practical.

Lint findings for normalized sources without wiki source notes include stable
`category`, `severity`, `files`, `recommendation`, `source_id`, and
`expected_path` fields so curators can create the missing note without reading
the manifest by hand.

## Classifier Rules

`source_inventory.py` classifies raw files by extension and lightweight content checks:

| Kind | Rule |
|------|------|
| `paper` | logical LaTeX source bundle detected from an arXiv-style directory or `00README.json` |
| `repo_link` | GitHub repository URL parsed from a raw link file |
| `web_link` | non-GitHub HTTP(S) URL parsed from a raw link file |
| `codebase_architecture` | opt-in repository, local repo, or source archive evidence when `integrations.codebase_analysis.enabled` is `true` |
| `markdown` | `.md`, `.markdown`, `.mdown` |
| `pdf` | `.pdf` |
| `latex` | `.tex`, `.sty`, `.cls` |
| `bibtex` | `.bib`, `.bbl`, `.bst` |
| `html` | `.html`, `.htm`, `.xhtml` — normalized via stdlib HTML extraction (no JS rendering, no asset fetching) |
| `image` | `.png`, `.jpg`, `.jpeg`, `.gif`, `.svg`, `.webp`, `.tif`, `.tiff`, `.bmp`, `.eps` |
| `table` | `.csv`, `.tsv`, `.xlsx`, `.xls`, `.parquet`, `.feather`, `.jsonl` — only `.csv`/`.tsv` are normalized; the rest stay classified-only |
| `link` | `.url`, `.webloc`, `.txt` files under `raw/links`, or `.txt` files containing only HTTP(S) URLs |
| `code_archive` | `.zip`, `.tar`, `.gz`, `.tgz`, `.bz2`, `.xz`, `.7z`, `.rar`, `.tar.gz`, `.tar.bz2`, `.tar.xz` |
| `unknown` | any other file |

`link` is reserved for raw link files that could not be parsed into URL records and require review. Hidden files and placeholder files such as `.gitkeep` are ignored.

## LaTeX Bundle Records

`source_inventory.py` emits one logical `paper` record for a detected LaTeX bundle and suppresses separate manifest records for files inside that bundle.
This keeps downstream normalization focused on the paper entrypoint instead of figures, styles, bibliography files, and included sections.

Bundle detection supports:

- directories named like `arXiv-2604.13018v1`;
- directories with `00README.json` and a usable top-level `.tex` source.

Entrypoint selection is deterministic:

1. Use the first existing `00README.json` source where `usage` is `toplevel` and `filename` is a safe relative `.tex` path.
2. Fall back to `main.tex`, `main_arxiv.tex`, `arxiv.tex`, or `example_paper.tex`.
3. Fall back to the first root-level `.tex` file containing `\documentclass`.

Bundle metadata may include:

- `metadata.bundle_type`: `arxiv` or `latex_bundle`.
- `metadata.arxiv_id`: parsed arXiv ID for arXiv bundles.
- `metadata.readme_path`: workspace-relative `00README.json` path.
- `metadata.entrypoint_source`: `readme`, `fallback_name`, or `fallback_documentclass`.
- `metadata.entrypoint_candidates`: top-level candidates from README metadata.
- `metadata.texlive_version`: TeX Live version from README metadata.
- `metadata.file_count`: count of non-hidden files inside the bundle.
- `metadata.warnings`: non-fatal detection warnings.

## PDF Pairing Records

`source_inventory.py` pairs top-level PDF assets with detected LaTeX bundles after bundle discovery:

- arXiv PDFs such as `raw/pdf/2604.13018v1.pdf` pair with bundle records whose metadata contains `metadata.arxiv_id: 2604.13018v1`;
- non-arXiv PDFs pair by normalized slug, such as `raw/pdf/autogenesis.pdf` and `raw/other/autogenesis`;
- matched PDF records are folded into the logical `paper` record and are not emitted separately;
- unmatched PDFs remain `kind: pdf` with `pairing_status: pdf_only`;
- unmatched bundle records remain `kind: paper` with `pairing_status: latex_only`;
- many-to-one or one-to-many candidates are kept with `pairing_status: ambiguous`, `metadata.review_required: true`, candidate paths, and warnings.

Every run prints a pairing summary to stderr:

```text
summary paired=0 pdf_only=0 latex_only=0 ambiguous=0
```

## Link Records

`source_inventory.py` parses raw link files without fetching network content.
Each valid URL becomes one logical source record:

- `raw/links/*.txt`: newline-separated HTTP(S) URLs; blank lines and lines starting with `#` are ignored;
- `.url`: `URL=...` lines, with plain URL extraction as fallback;
- `.webloc`: first HTTP(S) URL found in the file text.

Parsed link records include:

- `url`: parsed HTTP(S) URL.
- `raw_paths`: the raw link file used as evidence.
- `metadata.link_file`: workspace-relative raw link file path.
- `metadata.raw_line`: source line number when available.
- `metadata.host`: normalized URL host.

GitHub repository URLs become `kind: repo_link` with `metadata.owner`, `metadata.repo`, and `metadata.repo_full_name` when codebase analysis is disabled. Other URLs become `kind: web_link`. If a link file cannot be parsed, it remains a raw `kind: link` record with `metadata.review_required: true` and warnings.

## Codebase Architecture Records

When `integrations.codebase_analysis.enabled` is `true`, repository evidence is represented as `kind: codebase_architecture` instead of being treated as a plain link or archive. Inventory still does not fetch network content or run adapter commands.

Supported sources:

- GitHub URLs in raw link files, recorded with `metadata.codebase_source_type:repo_link` and GitHub owner/repo metadata.
- Local repository directories under configured code roots such as `raw/code`, detected by markers like `pyproject.toml`, `package.json`, `go.mod`, `Cargo.toml`, `.agent-wiki`, or `.git`.
- Code archives classified from raw file extensions, recorded with `metadata.codebase_source_type: code_archive`.

Each codebase record includes `metadata.codebase_output_dir`, a generated artifact directory under `sources/`, for example `sources/code_wikis/codebase--github-example-project-a1b2c3d4e5`. Files inside detected local repositories are suppressed as separate raw records so the repo is normalized as one source evidence unit.

## Inventory Report

`source_inventory.py` supports two report formats:

```bash
python3 scripts/source_inventory.py --report --format text
python3 scripts/source_inventory.py --report --format json
```

`--format text` is the default and prints a Markdown report to stdout. `--format json --report` prints one machine-readable JSON report object with `schema_version: "1.0"` and `document_type: "source_inventory_report"`. Both report formats include:

- total record count and counts by `kind`;
- PDF/LaTeX pairing counts;
- evidence usability counts and records marked unusable evidence;
- review-required records;
- unknown files;
- raw link files that need review;
- warnings grouped as anomalies;
- readiness status: `ready_for_normalization` or `needs_review`;
- deterministic next actions.

When `--dry-run` is used without `--report`, stdout remains newline-delimited JSON manifest records (JSONL), not a JSON array. This preserves the dry-run stream contract for tools that preview the exact manifest records that would be written.

Fatal errors under JSON mode, including `--format json` and JSONL dry runs, are written to stderr as the shared error envelope from [orchestrator-handoff.md](orchestrator-handoff.md#error-envelope). For example, a missing workspace config reports `error_code: CONFIG_MISSING` and exits non-zero. Explicit `--format text` keeps human-readable stderr.

`--append-log` may be used with `--report` to append a compact inventory entry to `log.md`. During `--dry-run`, `--append-log` is skipped and a warning is printed to stderr.

High-trust deployments can opt into stricter provenance checks:

```bash
python3 scripts/source_inventory.py --report --reject-mismatch
python3 scripts/source_inventory.py --report --require-checksum
```

`--reject-mismatch` filters out records whose sidecar checksum is present but
not verified. `--require-checksum` filters out records without
`provenance.checksum_verified: true`. Both modes apply before manifest writing
and return exit code `1` when records are refused; JSON mode emits the shared
error envelope with `INVENTORY_CHECKSUM_MISMATCH` or
`INVENTORY_CHECKSUM_REQUIRED`.

## Example Records

```json
{"compiler":"pdflatex","detected_at":"2026-05-09T12:00:00Z","entrypoint":"main.tex","id":"paper:2604.13018v1","kind":"paper","latex_root":"raw/other/arXiv-2604.13018v1","metadata":{"arxiv_id":"2604.13018v1","bundle_type":"arxiv","entrypoint_candidates":["main.tex"],"entrypoint_source":"readme","file_count":12,"pairing_keys":["arxiv:2604.13018v1"],"readme_path":"raw/other/arXiv-2604.13018v1/00README.json","texlive_version":"2025"},"pairing_status":"paired","raw_paths":["raw/other/arXiv-2604.13018v1","raw/pdf/2604.13018v1.pdf"],"raw_pdf":"raw/pdf/2604.13018v1.pdf","status":"discovered"}
{"detected_at":"2026-05-09T12:00:00Z","id":"raw:raw-pdf-example-a1b2c3d4e5","kind":"pdf","metadata":{"extension":".pdf","review_required":true,"size_bytes":12345,"warnings":["raw:raw-pdf-example-a1b2c3d4e5: no matching LaTeX source bundle found"]},"pairing_status":"pdf_only","raw_paths":["raw/pdf/example.pdf"],"raw_pdf":"raw/pdf/example.pdf","status":"discovered"}
{"detected_at":"2026-05-09T12:00:00Z","id":"link:github-aweai-team-aiscientist-a1b2c3d4e5","kind":"repo_link","metadata":{"host":"github.com","link_file":"raw/links/aiScientist.txt","owner":"AweAI-Team","raw_line":1,"repo":"AiScientist","repo_full_name":"AweAI-Team/AiScientist"},"raw_paths":["raw/links/aiScientist.txt"],"status":"discovered","url":"https://github.com/AweAI-Team/AiScientist"}
{"detected_at":"2026-05-09T12:00:00Z","id":"codebase:github-aweai-team-aiscientist-a1b2c3d4e5","kind":"codebase_architecture","metadata":{"codebase_output_dir":"sources/code_wikis/codebase--github-aweai-team-aiscientist-a1b2c3d4e5","codebase_source_type":"repo_link","codebase_tool":"agent-wiki-cli","host":"github.com","link_file":"raw/links/aiScientist.txt","owner":"AweAI-Team","raw_line":1,"repo":"AiScientist","repo_full_name":"AweAI-Team/AiScientist"},"raw_paths":["raw/links/aiScientist.txt"],"status":"discovered","url":"https://github.com/AweAI-Team/AiScientist"}
```

## Determinism

For unchanged raw files, repeated inventory runs preserve existing `detected_at` values and produce records sorted by `id`. New source IDs receive a fresh UTC timestamp on first discovery.
