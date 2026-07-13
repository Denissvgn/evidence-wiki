# Workspace Initialization

Use the installed `evidence-wiki init` command to create a configured research workspace from the reusable starter. The command copies starter files, preserves `workspace-system.yml`, personalizes `research.yml`, renders a project-specific `index.md` and `log.md`, optionally applies a domain pack, and writes an init report for profile-driven setup. From a source checkout, the same initializer is available as `scripts/init_research_workspace.py`.

The initializer does not fetch network resources, install git hooks, initialize git, copy pilot data, or move raw sources.

## CLI Usage

Create a generic workspace from explicit flags:

```bash
evidence-wiki init \
  --target ../my-research-workspace \
  --project-name my-research-workspace \
  --project-description "Research workspace for a specific topic" \
  --owner-goal "Build a source-grounded knowledge base for decisions"
```

Create a workspace with a reusable domain pack:

```bash
evidence-wiki init \
  --target ../llm-research-workspace \
  --project-name llm-research-workspace \
  --project-description "Research workspace for LLM research systems" \
  --domain-pack llm-research
```

Preview without writing files:

```bash
evidence-wiki init \
  --target ../init-smoke \
  --project-name init-smoke \
  --project-description "Smoke-test research workspace" \
  --dry-run
```

The target must be empty unless `--force` is supplied. With `--force`, the script overwrites starter-managed files but does not delete unrelated target files.

## Setup Profile Input

Agents can pass a YAML or JSON setup profile with `--profile`. Explicit CLI flags override profile values. The schema is documented in `docs/workspace-init-profile.md`.

Profiles are validated before dry-run output or file writes. The canonical document root is `workspace_init`, and the current schema version is `"0.1"`.
A minimal no-domain-pack profile records the reviewed setup decisions:

```yaml
workspace_init:
  schema_version: "0.1"
  target_path: ../my-research-workspace
  project:
    name: my-research-workspace
    description: Research workspace for a specific topic.
    owner_goal: Build a source-grounded knowledge base for decisions.
    language: en
  domain_guidance:
    mode: none
    rationale: Generic starter taxonomy is sufficient for the first setup pass.
  domain_pack:
    enabled: false
  raw:
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
    codebase_analysis:
      enabled: false
      provider: none
      command: null
      output_dir: sources/code_wikis
      read_only: true
      install_hooks: false
      background_sync: false
  assumptions:
    - Generic wiki taxonomy is sufficient for the first setup pass.
  skipped_decisions:
    - No network fetching during initialization.
```

When a setup profile is used, the created workspace includes `docs/workspace-init-report.md` by default. The report summarizes questions asked, inferred answers, domain guidance, source roots, supplied sources, output formats, claim strictness, integrations, validation commands and results, assumptions, skipped decisions, and next actions. Set `init_report.path` in the profile to choose a different workspace-relative report path.

Preview a profile before writing files:

```bash
evidence-wiki init \
  --profile /path/to/workspace-init.yml \
  --dry-run
```

To apply a reusable domain pack from a profile, set `domain_guidance.mode: domain_pack` and select exactly one of `domain_pack.name` or `domain_pack.path`. You may also override the selected pack explicitly with `--domain-pack`.

When no reusable pack matches, set `domain_guidance.mode: project_local` and include extraction targets, source priorities, claim types, output scaffolds, and filing rules in the setup profile. The initializer renders the guidance into the created workspace, defaulting to `docs/project-domain-guidance.md`.
See `docs/domain-guidance-generator.md` for the workflow and promotion rules.

When repositories are research evidence, set `integrations.codebase_analysis.enabled: true`, name a provider, keep `output_dir` under `sources/`, and add code roots such as `raw/code` to `raw.source_roots`. The initializer creates the generated output directory and records the adapter command, but it never executes codebase-analysis commands, clones repositories, installs hooks, or starts background sync.

Supported top-level profile config sections are merged into `research.yml`:

- `raw`
- `sources`
- `wiki`
- `taxonomy`
- `ingest`
- `lint`
- `outputs`
- `integrations`

The same sections may also be nested under `research_yml`. Dictionary values are deep-merged; list values replace the starter list. Unknown nested config keys are allowed for forward compatibility, but unknown profile top-level keys are refused.

## Domain Packs

`--domain-pack llm-research` resolves to `domain-packs/llm-research/` beside the starter repository. A filesystem path may also be supplied.

When a domain pack is requested, the script:

1. copies the pack to `domain-packs/<pack-name>/` inside the new workspace,
2. deep-merges `research.overlay.yml` into `research.yml`,
3. rewrites known domain-pack document paths so they are workspace-relative,
4. keeps explicit CLI and profile project identity values as the final source of truth.

If no domain pack is requested, no domain-pack directory is created.

## Created Workspace

The generated workspace includes:

- `workspace-system.yml` starter metadata,
- personalized `research.yml`,
- project-specific `index.md` and `log.md`,
- `docs/workspace-init-report.md` when created from a setup profile,
- `AGENTS.md` and `CLAUDE.md`,
- reusable docs, scripts, and skills,
- configured raw roots, source directories, wiki directories, and output directory.
- configured codebase-analysis output directory under `sources/` when enabled.

Init-generated workspace metadata and content files are written with restrictive
permissions on POSIX-style systems: generated files such as `research.yml`,
`index.md`, `log.md`, seeded question pages, project-local guidance, and init
reports use owner-only file modes, and workspace-created directories use
owner-only directory modes. On Windows, Python's `chmod` behavior is best effort.

After creation, run smoke validation from the new workspace root:

```bash
python3 scripts/smoke_validate_workspace.py --format text
```

Smoke validation checks the initialized workspace structure, starter metadata, configured directories, setup log, domain-pack paths, and enabled codebase analysis output directory. It should pass before inventory, normalization, or broader lint checks run.

Then run the broader workspace checks:

```bash
python3 scripts/source_inventory.py --dry-run --report
python3 scripts/normalize_sources.py --all --dry-run
python3 scripts/lint.py --format text
```

These are setup and early health gates. After the workspace has completed an initial source cycle and is being considered for sustained use, evaluate it with `docs/production-readiness-checklist.md`.

If validation results were `pending` in the generated init report, update the report after running these checks so maintainers can review setup evidence without reading the initialization conversation.

## Cross-Platform Installation Check

Use an isolated virtual environment for each installation check. Install the
wheel you intend to validate instead of importing from a source checkout, so
the check exercises the packaged CLI and starter files. Replace the example
wheel name below with the built artifact's filename and calculate its SHA-256
before installation so the tested artifact can be identified later.

For every platform:

- use Python 3.10 or newer and record the exact Python, OS, architecture, shell,
  and filesystem details;
- keep the wheel, virtual environment, and target outside the repository and
  outside cloud-synchronized or protected system directories;
- quote every path containing spaces or Unicode and use a target such as
  `Research Workspaces/Café Heat` to exercise both;
- use `/` in Markdown links and workspace-relative YAML values, even when the
  shell accepts `\`;
- preserve exact on-disk capitalization, use NFC when creating new Unicode
  names, and never create paths that differ only by case or Unicode
  normalization;
- invoke the virtual environment's executable directly. Activation is optional
  and must not be required for a passing lane.

### Ubuntu

Record `/etc/os-release`, `uname -m`, the shell version, filesystem type, and
whether the target filesystem is case-sensitive. Then run:

```bash
WHEEL="/absolute/path/evidence_wiki-X.Y.Z-py3-none-any.whl"
TARGET="$HOME/Research Workspaces/Café Heat"
python3 -m venv .venv
.venv/bin/python -m pip install --no-deps "$WHEEL"
.venv/bin/python -c 'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' "$WHEEL"
.venv/bin/evidence-wiki --help
.venv/bin/evidence-wiki init --target "$TARGET" --project-name cafe-heat --project-description "Unicode and spaced-path rehearsal"
.venv/bin/python "$TARGET/scripts/smoke_validate_workspace.py" --project-root "$TARGET" --format text
```

### macOS

Record `sw_vers`, `uname -m`, the shell version, volume format, and its actual
case-sensitivity setting. Do not infer case sensitivity from the `APFS` label.
Use the framework, Homebrew, or other reviewed Python installation selected for
the lane, then run:

```bash
WHEEL="/absolute/path/evidence_wiki-X.Y.Z-py3-none-any.whl"
TARGET="$HOME/Research Workspaces/Café Heat"
python3 -m venv .venv
.venv/bin/python -m pip install --no-deps "$WHEEL"
.venv/bin/python -c 'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' "$WHEEL"
.venv/bin/evidence-wiki --help
.venv/bin/evidence-wiki init --target "$TARGET" --project-name cafe-heat --project-description "Unicode and spaced-path rehearsal"
.venv/bin/python "$TARGET/scripts/smoke_validate_workspace.py" --project-root "$TARGET" --format text
```

### Windows PowerShell

Record the Windows edition/build, processor architecture, PowerShell version,
Python launcher resolution, filesystem, and long-path policy. Direct executable
invocation avoids changing the user's PowerShell execution policy:

```powershell
$Wheel = 'C:\artifacts\evidence_wiki-X.Y.Z-py3-none-any.whl'
$Target = Join-Path $HOME 'Research Workspaces\Café Heat'
py -3.10 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install --no-deps $Wheel
$WheelHash = (Get-FileHash -Algorithm SHA256 $Wheel).Hash.ToLowerInvariant()
$WheelHash
& .\.venv\Scripts\evidence-wiki.exe --help
& .\.venv\Scripts\evidence-wiki.exe init --target $Target --project-name cafe-heat --project-description 'Unicode and spaced-path rehearsal'
& .\.venv\Scripts\python.exe "$Target\scripts\smoke_validate_workspace.py" --project-root $Target --format text
```

If `py -3.10` is unavailable, select another installed Python 3.10+ executable,
record its resolved path, and use that same interpreter throughout the lane. Do
not repair an activation-policy failure by weakening machine policy; continue
with direct `.exe` invocation.

After initialization, follow the plain-Markdown, Obsidian, and Dataview checks
in [Obsidian Dataview Guidance](obsidian-dataview.md) and the manual template
checks in [Obsidian Page Templates](obsidian-templates.md). When the results
matter for a handoff, retain commands, failures, remediation, retest results,
artifact hashes, and screenshots in `log.md` or under `wiki/outputs/`.
