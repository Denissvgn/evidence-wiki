# domain-pack-create

Create a reusable evidence-wiki domain pack from a planner or orchestrator brief.

## Use When

Use this skill when the user, planner, or orchestrator asks for a reusable domain pack for a research area, or when repeated project-local guidance should be promoted into reusable pack files.

Inputs:

- orchestrator brief or user-provided domain scope,
- desired pack slug or target path, when supplied,
- desired research outcome and audience,
- optional source hints, provider preferences, existing project-local guidance, or example source types,
- available starter files and existing packs under `domain-packs/`.

## Operating Rules

- Infer from the orchestrator brief first. Ask at most three clarifying questions, and only when the answer materially changes the pack contract.
- Use `domain-packs/llm-research/` only as a structural reference for file shape, overlay style, scaffolds, and validation expectations.
- Keep every pack guidance-only. Do not add scripts, executable hooks, vendored tools, generated source records, raw evidence, or project-specific workspace content to a domain pack.
- Use only inert text/data files (`.md`, `.yml`, `.yaml`, `.json`, `.txt`, or
  `.csv`). Symlinks, executable permission bits, binary content, and executable
  or unsupported file types are rejected before smoke deployment.
- Do not edit core template files, starter scripts, generic skills, or `research.yml` to make a pack validate.
- Keep pack paths pack-local. Overlay references such as `taxonomy_doc`, `claims_doc`, `scaffolds`, `implemented_files`, and `planned_files` must point to files inside the pack.
- Preserve domain-neutral workspace mechanics. A pack may extend taxonomy, page placement, claim categories, extraction targets, source-type guidance, and scaffolds; it must not hardcode one workspace's sources or answers.

## Workflow

### 1. Inspect Context

Read the orchestrator brief, any project-local guidance, and existing packs:

```bash
ls domain-packs
find domain-packs/llm-research -maxdepth 2 -type f | sort
```

Record the inferred domain scope, reusable audience, source types, extraction targets, claim types, page taxonomy, likely scaffolds, and assumptions. If the domain cannot be inferred safely, ask concise questions before writing files.

### 2. Choose The Pack Location

Default to `domain-packs/<domain-slug>/` for repo packs. Use a user-supplied path only when the caller explicitly requested one.

Use a short lowercase slug with hyphens. The slug should describe the reusable domain, not a single project, customer, or task.

### 3. Draft Guidance Files

Create or update only pack-local guidance files:

- `README.md`: pack scope, intended workspaces, file inventory, and application notes.
- `taxonomy.md`: page placement, extraction targets, filing rules, and recommended synthesis outputs.
- `claims.md`: domain claim categories, required/optional fields, examples, and interpretation boundaries.
- `research.overlay.yml`: partial config fragment with `domain_pack` metadata and any domain-specific `wiki` or `taxonomy` extensions.
- `scaffolds/*.md`: optional reusable page or source-note scaffolds for the domain.
- `coverage-templates/*.yml`: optional declarative answerability templates that define required facets and policies, never source URLs or workspace evidence.

Use `domain-packs/llm-research/research.overlay.yml` as the overlay shape. Set `domain_pack.compatible_research_yml_contract` from `evidence-wiki contract` or the starter `workspace-system.yml`. Start new packs at `version: 0.1.0` unless the caller requested a different version.

When drafting `research.overlay.yml`:

- include `domain_pack.name`, `version`, `description`, `compatible_research_yml_contract`, `taxonomy_doc`, and `claims_doc`;
- declare all scaffold paths under `domain_pack.scaffolds`;
- declare reusable coverage manifest templates under `domain_pack.coverage_templates` as a mapping of lowercase hyphenated slug to pack-local YAML path;
- keep required coverage facets autonomously satisfiable: do not use a manual-only policy such as `manual_review_required`, `domain_pack_allowed`, `manual_review`, or a pack-declared source/freshness/identity policy in `required_facets` unless the pack explicitly declares `domain_pack.human_gated: true`;
- list every implemented pack-local file under `domain_pack.implemented_files`;
- keep `domain_pack.planned_files` empty unless a future file is intentionally documented;
- add only domain-specific page types, directories, frontmatter rules, taxonomy values, extraction targets, and recommended synthesis outputs.

### 4. Validate And Iterate

Run the validator before handing off the pack:

```bash
evidence-wiki pack validate --path domain-packs/<domain-slug>
```

From a source checkout, this equivalent command is also valid:

```bash
python3 tools/validate_domain_pack.py --path domain-packs/<domain-slug>
```

If validation fails, fix the pack files and rerun validation until the JSON output has `ok: true`. Treat pack-tree safety findings, traversal/symlink/executable content, missing referenced files, invalid metadata, contract mismatch, incompatible policy vocabulary, merge failures, autonomous-required-facet failures, and smoke-validation failures as pack bugs. `domain_pack.human_gated: true` is an explicit opt-out from the autonomous ship gate for packs whose required facets intentionally need human review; do not set it just to make a reusable autonomous pack pass.

### 5. Deploy A Smoke Workspace

After validation passes, deploy a temporary workspace with the new pack:

```bash
tmp_dir="$(mktemp -d)"
evidence-wiki deploy \
  --target "$tmp_dir/workspace" \
  --project-name "<domain-slug>-smoke" \
  --project-description "Smoke workspace for the <domain> domain pack." \
  --domain-pack domain-packs/<domain-slug>
python3 "$tmp_dir/workspace/scripts/smoke_validate_workspace.py" \
  --project-root "$tmp_dir/workspace" \
  --format text
```

If using only source-checkout scripts, deploy with `python3 workspace-template/scripts/init_research_workspace.py` and the same `--domain-pack` value.

### 6. Handoff

Finish with:

- pack path and slug,
- validation command and outcome,
- smoke workspace command and outcome,
- brief summary of inferred domain assumptions,
- any unresolved domain questions or source-acquisition gaps,
- confirmation that the pack is guidance-only and did not modify core template files.

## Promotion Boundary

Use project-local guidance instead of this skill when the rules are needed for only one workspace, the domain is still uncertain, or the user did not request reusable guidance. Promote to a domain pack only when guidance repeats across workspaces, stable scaffolds are useful, domain claim types need reusable validation, or the caller explicitly asks for a reusable pack.
