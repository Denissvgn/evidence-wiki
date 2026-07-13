# Agent-Quality Evaluation Workspace

This manual/periodic harness measures how well a research agent answers a
fixed synthetic workspace across template versions. It is intentionally not a
pull-request CI gate because agent output depends on model behavior, prompting,
and run budget. Use it for release comparison, prompt changes, or agent-runner
changes where a reproducible score from artifacts is useful.

## Fixture

The seed fixture lives at `tests/fixtures/agent-quality-eval/`:

- `workspace-init.yml` seeds three questions and handoff metadata.
- `delivery/raw/` contains two answer-bearing synthetic sources and one
  distractor source.
- `expected-answers.yml` defines deterministic scoring expectations.

All fixture evidence is synthetic and uses reserved `example.org` provenance.
The delivered sources support two answered questions. The maintenance-cost
question is deliberately blocked because the maintenance cost memo is not
delivered.

## Scoring Rubric

`tools/score_eval_workspace.py` compares an `export_answers.py` JSON dump with
the expected-answer key. It scores only artifacts, not a live workspace.

Answered questions are worth 100 points:

- 70 points: answer correctness, measured by required phrases in
  `answer_summary`.
- 30 points: citation precision, measured as F1 between exported `source_ids`
  and expected `source_ids`. Extra distractor citations reduce this score.

Blocked questions are worth 100 points:

- one third for `status: blocked`,
- one third for required phrases in `blocked_reason`,
- one third for not carrying an answer page or answer summary.

The report includes per-question findings for missing phrases, missing expected
source IDs, unexpected distractor source IDs, answer-page path mismatches, and
unexpected exported questions.

## Manual Run

From the repository root, create a temporary profile, deploy the workspace,
copy the synthetic delivery batch, and run the normal inventory/normalization
steps:

```bash
tmp_dir="$(mktemp -d /tmp/evidence-wiki-agent-eval.XXXXXX)"
profile="$tmp_dir/workspace-init.yml"
workspace="$tmp_dir/workspace"
python3 - <<PY
from pathlib import Path
source = Path("tests/fixtures/agent-quality-eval/workspace-init.yml")
target = Path("$profile")
target.write_text(source.read_text().replace("__TARGET__", "$workspace"), encoding="utf-8")
PY
python3 -B workspace-template/scripts/init_research_workspace.py --profile "$profile"
cp -R tests/fixtures/agent-quality-eval/delivery/raw/. "$workspace/raw/"
python3 -B "$workspace/scripts/source_inventory.py" --project-root "$workspace" --report
python3 -B "$workspace/scripts/normalize_sources.py" --project-root "$workspace" --all
```

Run the research agent under evaluation against `$workspace`. When it stops,
export and score the answers:

```bash
python3 -B "$workspace/scripts/export_answers.py" \
  --project-root "$workspace" \
  --output "$tmp_dir/export.json"
python3 -B tools/score_eval_workspace.py --export "$tmp_dir/export.json" \
  --expected tests/fixtures/agent-quality-eval/expected-answers.yml \
  --format json \
  --output "$tmp_dir/score-report.json"
```

Compare `score.percent` and the per-question findings across template versions.
Keep the raw export and score report with release notes when the result informs
a release decision.
