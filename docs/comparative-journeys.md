# Autonomous journey comparisons

`tools/run_comparative_trials.py` retains an outer comparison ledger over
existing native research operations. It supports three interface treatments:
explicit low-level composition, source documents alone, and the core research
facade. Every treatment returns the same bounded answer and exact citations.
Native acceptance stays separate from answer conformance, so a document answer
is not penalized for lacking a native publication receipt.

Freeze a configuration with `tools._comparative_protocol.freeze_plan`. It pins
candidate, model/settings declarations, capabilities, policy, source bytes,
interface guides, generation/judging caps, repetitions and randomized assignment
order. Keep the full plan in a private host directory. `generator_packet`
returns only the current original, usable evidence, common conditions and that
arm's interface guide. Denied or unknown source rights withhold content.

The bounded host supports at most 18 assignments and three attempts per slot.
It admits one active attempt at a time. Reservations precede dispatch; actual
starts, failed or abandoned work, abstentions, interruptions and unresolved
completion remain retained. Exact replay never starts another process. A retry
requires an explicitly settled failure and shares the original slot deadline.
Selected executable and script bytes are pinned before and after execution.

The command-line actions are `freeze`, `run`, `seal`, `judge` and `report`:

```sh
.venv/bin/python tools/run_comparative_trials.py freeze \
  --config /private/host/comparison.json \
  --directory /private/host/study \
  --work-root /private/work/comparison \
  --observer-key /private/host/observer.key
```

The observer key must be an explicitly selected private 32–128-byte file.
Work roots are outside the checkout and separate from private plans/reports.
Only ignore-verified internal destinations may be used inside the checkout.
HMAC records, a signed latest head and pending-append recovery detect incomplete
retention. Reopening requires `--expected-head` from the latest host observation,
held outside the directory being restored. Restoring the complete directory
without trusted latest-head custody is unsupported. These controls do not create
an OS sandbox against workers sharing the host user's filesystem privileges.

Host process elapsed time and attempt counts are observed and bounded. Model,
tool, token and cost caps are passed as frozen instructions; this generic
process host does not independently enforce a model provider's resource usage.
Missing meters stay unknown, and a declared generative model or a running process
does not prove provider execution, reasoning independence or billing.

Seal every generation attempt before final comparison grading. The neutral
judgment packet contains exact originals, authorized source bytes and anonymous
answers, omitting explicit arm, model, generator and native-approval metadata.
Preserved answer and source text may contain identifying cues; complete blinding
is not established. Host-only alias reconstruction remains separate. Final
judging inputs and outputs must not
enter generation workspaces or later-cycle context. Operational native review
feedback retains its normal product boundary.

Imported grading labels remain unverified declarations. Effective descriptive
conformance requires a host-observed grading receipt bound to the sealed packet,
output, frozen rubric, timestamps and judging budget. Unknown usage cannot
establish complete budget control. Neither agent adjudication nor finite
reference conformance qualifies general semantic accuracy.

Keep controlled snapshots, authorized-acquisition observations and repeated
updates in separate strata. A supplied source with an acquisition label does
not prove an acquisition call. Revised snapshots in fresh directories do not
prove repeated native operation; retain same-workspace refresh/history receipts
and predecessor linkage separately. Historical human baselines remain
observational and are excluded from matched assignments. Reports preserve all
expected slots, missing outcomes and every retry; they make no causal savings,
human-time or adoption claim.
