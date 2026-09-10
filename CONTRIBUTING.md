# Contributing

Thank you for helping improve EvidenceWiki. The project is intended to stay
reusable across research domains, so changes should be focused, tested, and
documented from the perspective of workspace users.

## Forks and contributions

You are welcome to fork EvidenceWiki, experiment with it, and adapt it to your
own research workflows. At this time, the maintainers are not actively seeking
additional contributors or organizing an active contributor program. If you
choose to propose a change, please use a fork and follow the guidance below;
contributions may be reviewed as maintainer time permits.

## Local setup

EvidenceWiki supports Python 3.10 and newer. Create a virtual environment and
install the development dependencies.

POSIX shells:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

Windows PowerShell:

```powershell
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

The commands below use the POSIX path. On Windows, replace
`.venv/bin/python` with `.venv\Scripts\python.exe`.

## Repository boundaries

- Keep reusable starter behavior in `workspace-template/`.
- `workspace-template/scripts/` is the source of truth for workspace tooling.
  Worked examples vendor the scripts so that they behave like real workspaces.
  After changing a starter script, synchronize those copies:

  ```bash
  .venv/bin/python tools/sync_vendored_scripts.py
  .venv/bin/python tools/sync_vendored_scripts.py --check
  ```

- Keep domain-specific reusable guidance in `domain-packs/`.
- Keep test-only workspaces and source examples in `tests/fixtures/`.
- Register every fixture path in `tests/fixtures/fixture-provenance.yml`.
  Prefer synthetic or reserved-domain content; third-party material needs
  explicit redistribution evidence and attribution.
- Do not copy project-specific wiki content into the reusable starter.
- Do not mutate raw source fixtures unless a test explicitly needs new evidence.

## Development rules

- Treat `research.yml` as the public workspace contract. Scripts should read
  configured paths, page types, statuses, and integration settings rather than
  hardcoding them.
- Keep generated codebase-analysis output under `sources/`, not maintained
  `wiki/` pages.
- Do not add hooks, automatic commits, background synchronization, or network
  fetching as defaults.
- Preserve compatibility with Python 3.10+ on Windows, macOS, and Ubuntu.
- Avoid broad refactors while implementing a focused change.

## Verification

Run the full suite before opening a pull request:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
.venv/bin/python tools/sync_vendored_scripts.py --check
git diff --check
```

Useful focused checks include:

```bash
.venv/bin/python -m pytest -q tests/test_package_cli.py
.venv/bin/python -m pytest -q tests/test_init_research_workspace.py tests/test_smoke_validate_workspace.py
.venv/bin/python -m pytest -q tests/test_inventory_normalization.py
.venv/bin/python -m pytest -q tests/test_end_to_end_init_fixture.py
.venv/bin/python -m pytest -q tests/test_pdf_success_path.py
```

Optional scale checks remain outside the default suite:

```bash
EVIDENCE_WIKI_RUN_SCALE=1 .venv/bin/python -m pytest -q tests/test_scale_smoke.py
```

Scale budgets are enforced by automation, not by the fast gate. The
`Scale and coverage evidence` workflow measures the frozen `standard` profile
weekly, on demand, and for pull requests labelled `performance` (both profiles
on the schedule); the release gate measures `standard` for every release
candidate and stores the JSON beside the distributions. Each run calls
`tools/scale_benchmark.py --profile <name> --require-budget --output <file>`,
which exits `3` on a budget violation and records the commit, Python,
platform, and runner beside the timings. The budgets in
`tools/scale_benchmark.py` are calibrated for GitHub-hosted `ubuntu-latest`
runners; cold `workspace_status` and warm `workspace_status_cached` are
measured separately. A violation is a measurement to read against the recorded
environment, not a prompt to rerun until a sample passes: a slower runner or a
regression both fail, and telling them apart is the reviewer's job. Run the
same command locally to compare a laptop sample, remembering that it is
evidence about the laptop. The same workflow publishes branch coverage of the
default suite, with workspace-script subprocesses and copied workspace scripts
folded back onto their template sources; treat the report as evidence of what
ran, with unmeasured execution explicit in it, not as a threshold.

Agent-quality evaluation is a manual check for comparing prompts or agent-runner
behavior against the deterministic fixture in
`tests/fixtures/agent-quality-eval/`:

```bash
.venv/bin/python tools/score_eval_workspace.py \
  --export /path/to/export.json \
  --expected tests/fixtures/agent-quality-eval/expected-answers.yml \
  --format json
```

The setup flow and scoring rubric are documented in
`workspace-template/docs/agent-quality-evaluation.md`.

## Packaging

Before proposing a release, build and inspect both distribution formats:

```bash
.venv/bin/python -m build --sdist --wheel --no-isolation --outdir /tmp/evidence-wiki-dist
.venv/bin/python -m twine check /tmp/evidence-wiki-dist/*
```

The wheel must contain the starter workspace, domain packs, and orchestrator
guide. The source distribution additionally carries tests and development
tools. Neither artifact should contain reports, caches, virtual environments,
scratch workspaces, or build output. One tool checks all of that and is the
gate both CI and the publishing workflow run:

```bash
.venv/bin/python tools/validate_installed_artifacts.py --dist-dir /tmp/evidence-wiki-dist
```

It checks archive membership against the packaging policy, installs the wheel
into a fresh virtual environment and exercises it from outside the checkout,
then unpacks the sdist, builds a wheel from it, and repeats the same checks on
that install. Its JSON summary records each artifact's SHA-256 digest. Pass
`--membership-only` for the fast archive check alone. A release is cut by bumping the version in
`pyproject.toml` and `src/evidence_wiki/__init__.py`, dating the changelog
heading, tagging the merged commit `v<version>`, and publishing a GitHub
Release; the publishing workflow in `.github/workflows/publish.yml` verifies
the tag, version, changelog heading, and built wheel before uploading. PyPI
uploads are intentionally unavailable from pull requests, tag pushes, and manual
workflow dispatches; only a published GitHub Release can start that workflow.

## Documentation

Update documentation whenever public behavior changes. Common destinations are:

- User setup: `workspace-template/docs/new-project-guide.md`
- Initialization profiles: `workspace-template/docs/workspace-initialization.md`
- Configuration: `workspace-template/docs/research-yml.md`
- Source records: `workspace-template/docs/source-manifest.md`
- Normalized records: `workspace-template/docs/normalized-source-format.md`
- Agent playbooks: `workspace-template/skills/`

Prefer explicit command examples over vague descriptions. A user should be able
to tell whether each command is read-only, a dry run, or writes files.

## Pull request checklist

- The change has focused tests, or a clear reason tests are unnecessary.
- The full test and lint suite passes.
- `git diff --check` passes.
- New files are intentional and no scratch directories are present.
- Public behavior changes are documented.
