# Historical simulation evidence

`market-simulation/v1` is an optional arithmetic qualification within the
generic `execution_evidence/v1` record. It supports a bounded fractional
buy-and-hold portfolio and a gross price-return benchmark. The library reads
captured artifacts, recalculates the declared result, and qualifies historical
inputs. It does not execute strategies, contact feeds, or place orders.
Other domains use the same execution record without selecting this profile.

## Select the profile

An authenticated observation selects the profile through `parameters`:

```json
{"market_simulation":{"schema_version":"market-simulation/v1","plan":{"path":"plan.json","content_hash":"sha256:<digest>"},"prices":{"path":"prices.json","content_hash":"sha256:<digest>"},"result":{"path":"result.json","content_hash":"sha256:<digest>"}}}
```

The plan and prices must be declared input artifacts with exact accepted source
revisions. The result must be a declared output. The plan references distinct
strategy and historical-universe input artifacts. Every artifact retains the
shared record's hash, byte length, original bytes, and source ancestry.
Use `historical-audit` or `historical-available` under the
[temporal evidence contract](temporal-evidence.md).

The `market-simulation-plan/v1` document records:

- `strategy` and `universe` artifact references; `evaluation` (`in-sample`,
  `held-out`, or `walk-forward`); and `fold` with integer `index` and `count`.
- `selected_at`, `selection_cutoff`, chronological `training`, `validation`,
  and `test` intervals with explicit `start` and `end`; and `selection_history`
  entries containing `trial_id`, `evaluated_until`, and the preserved `outcome`.
- `run_configuration`: exact `seed`, `tool`, `model`, `environment`,
  `dependencies`, `container`, `patch`, `verification_scope`, and other
  `parameters` from the execution payload. Model prompt and context bindings
  remain subject to the shared historical-input contract.
- `benchmark` listing identity, `capital`, `currency`, money and ratio
  `precision`, `costs`, `fill`, `adjustment`, `metric_definitions`,
  `excluded_data`, and `unresolved_bias`.

The plan, strategy, universe, and every additional decision input must qualify
at `selection_cutoff`; only the explicit price artifact uses the later run
cutoff. In held-out and walk-forward evaluation, training ends before validation,
validation ends before selection, and the selection cutoff precedes the held-out
interval. A later plan deposit cannot establish an earlier host observation.
Historical public availability needs a separately controlled attester and exact
accepted proof artifacts. Each walk-forward fold is a separate observation;
in-sample results retain the `training` role and cannot become held-out results
by relabeling the report.

`market-buy-hold/v1` supplies `allocations` of listing identities and weights
summing to at most one. `market-simulation-universe/v1` supplies `as_of`,
`basis: historical-membership`, `complete`, `members` (listing identity and
active/inactive/delisted status), and explicit `excluded` identities and reasons.
The universe includes inactive and delisted members when present historically;
exclusions and incomplete membership remain evidence gaps.

`market-simulation-prices/v1` supplies `currency`, `adjustment_lineage`, and
`bars` containing listing identity, start, end, close, volume, completion, and
adjustment basis. Every interval must include the complete declared universe
and benchmark. Intervals cover the evaluation interval without gaps or overlap;
unfinished bars and mixed adjustment bases cannot qualify a positive example.

## Arithmetic and limits

Numeric values are bounded decimal strings. `costs` contains per-side
`commission_bps`, `spread_bps`, and `slippage_bps`, each between zero and 1,000.
`fill` uses `model: fractional-close`, `max_volume_participation`, and
`cash_shortfall: refuse`. `adjustment` uses `basis: split-dividend-adjusted`,
an explicit `lineage` list, and `cash_distributions: embedded`.

The initial allocation budget includes purchase costs. Fractional units equal
the allocation budget divided by the first close and one plus the summed
per-side cost rate. Unallocated capital stays in cash. Each reported equity
point values liquidation after sale costs. Entry and final liquidation must
fit the declared volume-participation limit.

The required metric definitions are:

| Metric | Definition identifier | Calculation |
| --- | --- | --- |
| `net_profit` | `liquidation-equity-minus-initial-capital/v1` | Final liquidation equity minus initial capital |
| `net_return` | `net-profit-divided-by-initial-capital/v1` | Net profit divided by initial capital |
| `benchmark_return` | `gross-close-price-return/v1` | Last benchmark close divided by first close, minus one |
| `max_drawdown` | `maximum-peak-to-liquidation-equity-decline/v1` | Largest proportional decline from prior peak liquidation equity, including initial capital |

`market-simulation-result/v1` binds a `computation_id`, evaluation, fold,
currency, those metrics, and an `equity_curve` of timestamps and liquidation
equity. The computation identity hashes the profile settings excluding its
result reference, all declared input identities, seed, and other parameters.
The preselected plan binds the remaining tool, model, and environment settings.
Comparison uses exact canonical output bytes after decimal rounding to the
declared money and ratio quanta with half-even rounding. Supported quanta are
powers of ten from 1 to 0.00000001. Caller decimal settings do not alter results.

Bounds are 64 universe members, 4,096 bars, 128 intervals, 128 selection-history
entries, and 128 walk-forward folds. Each numeric string has at most 32 digits,
absolute exponent at most 16, and value at most one trillion unless a tighter
field bound applies. Shared execution and snapshot bounds also apply.

## Read the qualifications

The execution report exposes `market_simulation` with separate `evidence`,
`calculation`, `simulated_performance`, `uncertainty`, and `preselection` fields.
A matching result may describe a loss. An independently signed passing receipt
cannot override incorrect arithmetic, unavailable inputs, or an evidence gap.
Authenticated failed calculations remain eligible for explicitly selected
negative evidence under the ordinary usage and snapshot requirements.

Synthetic fills omit market impact, latency, and queue position. Provider
corporate-action arithmetic is declared and bound but is not reproduced by
this adapter. Historical input availability does not establish a model's
training cutoff. These limitations and excluded data remain visible alongside
measured simulated performance; live execution performance remains
`not_established`.

[Snapshot contract v4](evidence-snapshots.md) declares required optional
execution profiles and preserves all calculation artifacts. Its independent
offline verifier repeats arithmetic, authority, and historical-input checks.
