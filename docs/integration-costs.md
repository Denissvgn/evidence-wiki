# Measuring integration effort and execution cost

`tools._integration_costs` projects a frozen inventory of integration, setup,
repair, operation, review, qualification and evidence-update attempts. Keep
failed, abandoned, abstained and interrupted attempts in that inventory. Missing
receipts and unobserved activities remain explicit; they never imply zero work.

Use `freeze_cost_plan` to select the scope, binding, sources, expected attempts,
required metrics and optional positive `repeat_use_count`. Each final cumulative
event binds the plan, attempt, source and exact receipt identity. Exact duplicate
events are idempotent; conflicting finals and reuse of a physical receipt are
rejected. Supply disjoint usage observations: a parent's inclusive bill and its
children's bills must not both be attributed as independent consumption.

Metrics include elapsed time, model waiting, model calls, tool calls, tokens and
cost. Decimal durations and monetary amounts use exact strings. Cost buckets
remain separate for different currency or pricing identities; neither conversion
nor model pricing is inferred. A pricing reference is a declared identity, not
authentication of a price table or bill.

Reports distinguish known subtotals from complete totals. Partial source coverage,
unknown outcomes, missing activities or missing counters cannot establish complete
execution cost. Required-metric coverage is separate from complete telemetry.
Source provenance and coverage in JSON remain declarations, including fields
named `host_observed` or `authentication_ref`.

One-time and repeated-operation observations are separate. Amortization requires
complete telemetry and an explicit repeat-use count; its numerator and denominator
are retained exactly. The declared count does not prove observed adoption, and
observed recurring spend is not extrapolated into future operations.

The local host adapter supports `observe`, `read`, `report` and `comparative`:

```sh
.venv/bin/python tools/measure_integration_costs.py report \
  --plan /private/host/cost-plan.json \
  --events /private/host/final-events.json
```

Select owner-private regular input files and an explicitly selected 32–128-byte
observer key. Receipt directories are private; worker directories are outside
the checkout and separate from retained receipts and keys. Internal repository
outputs must pass the exact Git ignore check. Commands are explicit argv arrays,
with selected executable and script digests pinned before dispatch.

The command observer reserves an exclusive signed intent before work, retains
confirmed starts and terminal observations, and attempts ordinary process-group
cleanup. Existing reservations do not dispatch again. Preserve the returned
receipt hash outside the directory being restored and provide it to `read`.
Signed retention authenticates host records, not provider execution, billing or
hostile-worker OS isolation. Unresolved reservations and cleanup failures remain
gaps rather than successful measurements.

Standalone timing covers reservation through final observation, with process
start recorded separately. Comparative timing covers host confirmation through
retention; it is not worker or provider latency. Cumulative durations may overlap;
activity interval union and span remain separate. Scalar model-wait durations do
not establish wait-occupancy intervals or human effort.

An explicitly selected in-process meter may supply usage through the Python
adapter. The CLI never loads a callable from receipt JSON. Missing meters and
meter failures leave SDK counters, waiting and billing unknown.
Meter waiting has a finite parent deadline. A timed-out callback is detached,
and its later output cannot change the retained observation; its work may still
continue in the host process. Unresolved callback work remains an explicit gap,
so this does not establish a bound on total provider or callback consumption.
Generic comparative receipts always leave provider usage unknown, including older counters
that represented orchestration invocations. The comparative adapter freshly
verifies a selected `TrialLedger`, includes judging separately, and preserves
unobserved integration and maintenance work. The read-only opening requires its
existing lock and rejects pending recovery; recovery belongs to the original
writer, never the measurement reader.

`tools._native_cost_telemetry.from_native_accounting` accepts explicit native
verifier callbacks and attribution for every native attempt. It freshly
authenticates the native ledger before copying settled final meters once. Native
reservations and conservative maxima are not bills. Missing usage, conflicts,
missing activities and unmeasured waiting remain unknown.

Human and organizational observations remain separate from automated usage.
Imported or synthetic zero counters do not prove zero actual intervention. A
freshly authenticated native observer can establish a zero counter only for its
continuous declared scope, with the initial counter, terminal coverage and no
gaps or exclusions. Absent human-time, adoption and business telemetry stays
unknown. Agent time and simulated user scores cannot establish human savings.

For original-source authenticated external observations use the public
[`OutcomeIngestion` API](library-api.md#observed-outcomes). Its explicit host
verifier checks the original producer, evidence class, source receipt and exact
scope before import and again when reporting. Cost-report external observations
remain declared and unproved; copying them into another signed container does
not authenticate actual customer or human activity. Outcome inputs are optional
and never gate unrelated automated technical qualification.
