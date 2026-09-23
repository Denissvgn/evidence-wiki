# Declarative computation

The `computation:` block in `research.yml` defines grouped metrics, formula
graphs, invariants and schedules. These are inert data interpreted by one shared
engine. The engine uses retained normalized evidence and produces reproducible
values with provenance. Source accuracy and the meaning of a domain rule still
require review.

## Discover and run

```sh
evidence-wiki computation schemas --schema-id evidence-computation-definition/v1
evidence-wiki computation schemas --schema-id evidence-computation-result/v1
evidence-wiki computation check --target WORKSPACE
evidence-wiki computation aggregate --target WORKSPACE
evidence-wiki computation evaluate --target WORKSPACE
evidence-wiki computation verify --target WORKSPACE
evidence-wiki computation schedule --target WORKSPACE --as-of 2026-09-21T12:00:00Z
```

The four views evaluate the shared declaration and return the same complete
result, including applicable invariant findings. The copied standalone entry
points are `scripts/aggregate_records.py`, `scripts/evaluate_formulas.py`,
`scripts/verify_assertions.py` and `scripts/schedule_milestones.py`. They accept
`--project-root` and `--as-of` and need no import from the installed package.
They use the workspace's selected Python interpreter and required dependencies.

Python hosts can call `evidence_wiki.computation.evaluate(root, as_of=...)` or
`execute(root, operation, **options)`. `schema_document(resource_id)` returns an
individual caller-owned schema. Errors use `COMPUTATION_REFUSED` with a
content-free reason; a busy lock uses `COMPUTATION_BUSY`, exit 6. CLI operations
emit one JSON stdout document. An evaluated error invariant exits 3; malformed
or unavailable required inputs exit 2.

| Error code | Exit | Recoverable | Action |
| --- | --- | --- | --- |
| `COMPUTATION_REFUSED` | 2 | No | Inspect the bounded reason and correct the declaration, selected inputs, CLI arguments or clock before reevaluating. |
| `COMPUTATION_BUSY` | 6 | Yes | Wait for the current writer, inspect the retained request and result identities, and retry without removing locks or journals. |

## Declaration and numeric contract

A complete declaration has these required sections, even when a collection is
empty:

```yaml
computation:
  version: "1.0"
  arithmetic:
    mode: exact
    precision: 64
    scale: null
    rounding: ROUND_HALF_EVEN
  clock:
    as_of: "2026-09-21T12:00:00Z"
    timezone: UTC
    ambiguous: refuse
    nonexistent: refuse
    search_days: 366
  tables: {}
  aggregations: {}
  graphs: {}
  invariants: {}
  cadence: {}
```

Unknown versions and fields refuse. YAML duplicate keys, recursive aliases and
aliases inside computation data refuse. Pack declarations merge through the
existing configuration owner: the pack overlays the starter, then project
profile values override named fields; nested mappings merge and lists replace.
An empty mapping does not delete inherited rules. Pack refresh keeps its
existing ownership and conflict protocol. Workspaces without this block retain
their existing behavior.

Decimal constants are strings, such as `"0.20"`. Structured JSON number tokens
are read directly as decimals; large integers do not pass through a floating
point representation. A structured record may also use `{"decimal":"0.20"}`.
An ordinary string such as `"0.20"` remains a string when it is a record field;
use the explicit `decimal(record.field)` conversion when a declaration intends
to interpret a finite numeric string. Conversion does not parse units or guess
locale-specific separators.
Numeric result values are strings. JSON integers are used only for bounded
control fields and counts.

`precision` is the working number of significant digits, from 8 to 128.
`scale` is an optional display scale from 0 to 18 decimal places. Calculations
consume the full working value; `formatted` is a separate display value.
`exact` refuses an inexact operation or display quantization, including `1 / 3`.
`rounded` permits the declared context's rounding and propagates `rounded: true`
when information was lost. Rounding modes are the eight standard Decimal modes.
The context is isolated from the calling process. See the
[Python Decimal context and signals](https://docs.python.org/3.10/library/decimal.html).

The language allows finite numeric and string literals, `True`, `False` and `None`; named
data fields; `+`, `-`, `*`, `/`, bounded integer `**`; comparisons; Boolean
operators; and conditional expressions. Fields access data mappings or bounded
list positions. It exposes no Python objects, imports, methods, comprehensions,
assignment, executable strings or filesystem/network operations.

Registered pure functions are `min`, `max`, `abs`, `clamp`, `round(value, scale)`,
`decimal(value)` and `bracket_lookup("table_name", value)`. Lookup tables declare contiguous,
nonoverlapping intervals with inclusive lower and exclusive upper boundaries;
the final upper boundary may be null. Missing variables, invalid brackets,
division by zero and nonfinite values refuse. Booleans never become numbers.
Units are preserved declarations; the engine does not infer dimensional algebra.

## Records and aggregation

Each aggregation has a `description`, `selector`, optional `filter`, `group_by`
list, `metrics` mapping and nullable `output_target`. For example:

```yaml
description: Total retained observations
selector:
  source_glob: "sources/normalized/*.md"
  records_pointer: /records
  row_key: /id
  allow_empty: false
filter: null
group_by: []
metrics:
  total:
    op: sum
    expr: record.amount
    denominator: null
    unit: units
output_target: wiki/outputs/totals.md
```

Selectors address normalized Markdown records in the configured normalized
directory. Their canonical structured sidecars must exist and match the retained
hash binding. The normalized contract, manifest agreement, raw artifacts and
current permitted-use checks apply. Matching an arbitrary JSON file is
insufficient. `records_pointer` selects an object or an array of objects using
RFC 6901 syntax. Required empty selections refuse.

Records have stable identities from their source revision, bytes and pointer.
An optional `row_key` rejects duplicate keys across the selection. Without that
key, equal rows at different positions are distinct observations. Missing or
nonnumeric required fields refuse; null values are not silently dropped.
Excluded records and filter counts remain visible. A filter may intentionally
select no rows.

Reducers are `sum`, `count`, `avg`, `min`, `max` and `ratio`. Count has a null
`expr`; ratio requires both `expr` and `denominator` and divides their sums.
A zero denominator refuses. Empty ungrouped sum/count are zero; empty
avg/min/max are explicit null values. A graph cannot release a null mapped
output as a completed value. Maximum is never labeled a percentile.

Groups use stable, typed keys. Numeric keys use decimal objects on the wire,
for example `[{"decimal":"1"}]`, to distinguish them from string keys `["1"]`.
Graph references either select an exact key or explicitly reduce groups:

```yaml
kind: aggregation
id: totals
metric: amount
group: []
reduce: null
```

Use `group: null` with `reduce: sum`, `avg`, `min` or `max` to combine groups.
The engine never chooses an arbitrary group. Filter, grouping, metric and
formula rules are included in per-value lineage alongside source fields,
constants and lookup tables.

## Formula graphs and invariants

Graphs declare `description`, `constants`, `inputs`, `nodes`, `output_mapping`
and nullable `output_page`. Constants contain `value` and `unit`; nodes contain
`expr` and `unit`. Names are unique across constants, inputs and nodes. Use bare
names or `constants.name`, `inputs.name` and `nodes.name`. A cross-graph input
uses `kind: graph`, `id` and `output`. Unknown references and cycles refuse.
Stable topological ordering makes declaration order irrelevant.

Invariants explicitly target `records` or `computed` values. Record invariants
use a selector; computed invariants use named aggregation/graph inputs. They
declare `filter`, Boolean `assertion`, `severity` and `failure_message`.
Message placeholders name data fields only; formatting specifications and
conversions refuse. Error findings block lint and publication, including strict
release. Missing, failed or interrupted required evaluation also blocks.

Warnings produce findings. Read-only checks do not create questions. Explicit
warning intake uses the canonical question owner and puts finding details in
labeled untrusted-evidence blocks. Repeated findings reuse their question.
Findings that disappear are reported as resolved by the current evaluation;
existing questions retain their separate lifecycle and are not silently closed.
Publication retains its normal review requirements: an unresolved warning can
require attention even when arithmetic evaluation succeeds. A dispatched local
status flag does not resolve the finding or approve an answer.

## Explicit-clock schedules

Every schedule evaluation uses the supplied `--as-of` or the declared
`clock.as_of`; there is no implicit current clock. Instants have explicit
offsets and whole-second precision. Naive trigger dates/times are interpreted
in the declared timezone. UTC is a fixed offset; named zones use the installed
`tzdata` package and carry its version and file digest, independent of the OS
zone database.

Triggers are `fixed_date`, `elapsed_units`, `cron` and `condition`. Seconds,
minutes, hours and days are elapsed durations; months and years follow the
local calendar with an explicit clamp-or-refuse policy. Ambiguous local times
choose `earlier`, `later` or `refuse`; nonexistent times skip or refuse. A
skipped single occurrence is reported as unknown rather than guessed.

Cron uses five numeric fields, bounded lists/ranges, `*` and steps over ranges
or `*`. Day matching is explicitly `and` or `or`; unrestricted day fields do
not override the other constraint. Named fields, macros and shell extensions
are unavailable. Searches stop at `search_days` and report unknown when no
qualified occurrence exists within the horizon.

Polling reports the latest due occurrence and the next occurrence; missed runs
use latest-only catch-up. A condition trigger is keyed to its input basis,
not every polling timestamp. Lead alerts state their units. Due action names
are limited to `evaluate_graph`, `check_invariants` and `status_flag`. They do
not authorize a daemon, shell, hook, acquisition, model call or external action.
Workspace status reports recorded dispatch separately from current computation;
an operational record is not independent review.

## Explicit effects and recovery

Read a current result first, then provide its identity and an idempotent request
ID for a mutation:

```sh
evidence-wiki computation write --target WORKSPACE --expected-result-id RESULT_ID --request-id output_1 --dry-run
evidence-wiki computation write --target WORKSPACE --expected-result-id RESULT_ID --request-id output_1
evidence-wiki computation apply-warnings --target WORKSPACE --expected-result-id RESULT_ID --request-id warnings_1
evidence-wiki computation dispatch --target WORKSPACE --expected-result-id RESULT_ID --request-id dispatch_1 --cadence-id review
```

Pass the same `--as-of` when one was used to obtain the result. Dry-run creates
no locks, questions or artifacts. Writes are limited to declared Markdown/JSON
destinations under the configured `outputs.default_dir`, inside the wiki and
outside raw, normalized, card and question roots. JSON files require owned
output state. Markdown updates replace only an owned generated block and
preserve surrounding prose; edited generated blocks refuse.

The existing descriptor-anchored file publisher and workspace lock owner
perform writes. `runs/computation/` retains operational receipts and pending
transactions. Replay verifies the current basis and reproduces planned output;
interrupted requests resume with the same request/result identity. Conflicting
user edits or changed inputs refuse and retain the pending record for repair.
Pending effects block publication. Stored result hashes cannot authorize a
strict claim or replace recomputation. File API support is checked explicitly;
unsupported descriptor/no-follow capabilities refuse.

## Strict claims and review

Strict workspaces with computation use `evidence-strict-claims/v2`,
`evidence-strict-review/v2`, and the corresponding v2 result/publication shapes.
Legacy v1 remains available without computation. Every v2 claim has a
`calculations` list. A derived claim is an inference and names a result identity,
a scalar output pointer, its expected value, `form` (`value` or `formatted`),
unit and rounded flag. Current computation must reproduce all of them, and the
claim must cite all contributing source IDs.

Review snapshots retain the complete computation result and declaration, with
input, engine, arithmetic and clock identities. Final release recomputes them
through the same owner and still requires coverage, grounding, permitted use
and authenticated semantic/human review. Changed inputs, rules, code, clocks,
values, units or rounding invalidate the affected basis. A derived number is
not a primary source or independently approved domain conclusion.

## Bounds and examples

Declarations and results are capped at 1 MiB. Bounds include 128 sources,
2,048 records, 128 groups per aggregation, 1,024 graph nodes overall,
4,096 expression bytes, 256 syntax nodes, depth 24 and 200,000 evaluation
operations. Numeric coefficients have at most 128 digits and exponents are
bounded to ±256. Selected sidecars total at most 8 MiB. Transaction plans have
at most 64 writes and 4 MiB of new content; operational state is capped at
8 MiB and 128 requests. Combined limits may refuse before each individual
maximum is reached. Existing capture and authority bounds also apply.

Complete synthetic candidate packs are included for
[benchmark observations](computation-examples/sample-benchmark/README.md),
[portfolio totals](computation-examples/sample-portfolio/README.md), and
[a filing worksheet](computation-examples/sample-filing/README.md).
They use the same engine and make no claim about real entities or current
scientific, financial or legal rules. They are examples, not registered
built-in packs. Existing Markdown pack guidance remains supported.
