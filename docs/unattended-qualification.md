# Unattended qualification

`tools/qualify_unattended.py` joins bounded Build, Attack, Integration,
Measurement and Verification jobs for one frozen candidate. Existing native
research, reference, recovery and permission owners retain their decisions.
The supported profile is `artifact_checked` scripted fixture mechanics.
Remote model execution, provider billing, OS credential isolation, adoption
and business value remain unobserved.

The host selects five actual agent assignments and retains original copied
assignment and tool request/response transcripts outside worker inputs. Separate
role names, controller strings or signed success descriptions cannot establish
independent execution. Transcript commitments identify trusted host copies;
they do not authenticate the platform or prove physical isolation.

Prepare every command, private factory, scenario and report destination before
freezing the plan. The configuration supplies `repo_root`, `assignments_path`,
an externally retained `assignments_sha256`, five ordered `jobs`, a `context`
inventory and a total `timeout_seconds` of at most 7200. Jobs select explicit
argv, finite per-job seconds and the original report paths. Required reports
are fixed: archives; attack and automatic catalogs; installed transports; costs;
and independent verification. The context retains policy, rubric, evaluators,
decision rules, source rights/packets, reference split/provenance and cost scope.
Unknown observations remain explicit.
This frozen context is a declaration of scope. Actual native policy, profile,
calibration, source and rights bindings are separately obtained from the current
original owners and exported as `native_bindings`.

```sh
.venv/bin/python tools/qualify_unattended.py freeze \
  --directory /private/host/qualification --config /private/host/config.json
.venv/bin/python tools/qualify_unattended.py start \
  --directory /private/host/qualification --observer-key /private/host-keys/observer.key
```

`start` creates the original common clock after source and command authoring.
Each assigned agent invokes `observe --role ROLE` with the same directory and
external key. It observes its frozen command once with user stdin closed and
passes the remaining original deadline. The command observer retains actual
intent, PID/start, exit, streams, timeout/cleanup and immutable final receipts.
There is no automatic retry or interactive approval/elicitation fallback.

After each job, the trusted parent preserves the actual raw `exec_command`
request and response and calls `bind --role ROLE --config TRANSCRIPT_JSON
--expected-report ORIGINAL_SHA256`. Raw outer observer argv stays distinct
from its signed nested command argv. The transcript also joins the assigned
agent/context, candidate, common deadline, original receipt and report bytes.
Keep the key and latest hashes outside all worker and evidence roots.

`seal --preliminary` joins the first four jobs. A separate Verification agent
calls `replay --preliminary --expected-report ORIGINAL_REPORT_SHA256`, records
its result against that exact preliminary hash and completes its own observed
job. Its frozen job invokes the registered `verify` action with a trusted
external anchor JSON containing `plan_sha256` and `report_sha256`, and
`--output=ORIGINAL_VERIFICATION_REPORT_PATH`. Use `--config=ANCHOR_PATH` for this
future host-written anchor; a separate argv file argument is a pinned input.
`seal` exports the final manifest. `replay --expected-report FINAL_SHA256`
authenticates original custody and independently re-reads each producer.
Missing, changed, unstarted, failed, cancelled, timed-out or unsupported required
evidence yields `not_qualified`.

A source-pinned Verification wrapper must retain the canonical replay's original
signed nested receipt plus an independent audit artifact, adapter commitment,
frozen expectation hash and exact mutation-control inventory. The first four
jobs must already be complete and passing; an incomplete baseline is not proof
that a mutation was rejected. Each negative control must add a refusal or gap
beyond the expected missing Verification job. Dated preliminary replay excludes
only authenticated Verification starts later than that snapshot. Final replay
includes those starts and their actual stop observations.

Rich task reports in audit evidence use lossless canonical JSON text and a
SHA-256 commitment. Direct role, start, terminal, outcome and receipt fields
remain visible. The gate checks the encoding, recovers the complete reports
under the original limits and compares the evidence with independently
reconstructed original tasks. This framing keeps repeated control evidence
within the artifact's byte and structural bounds without dropping report facts.

The automatic catalog contains eighteen product scenarios separately from the
adversarial catalog. Both reuse the existing exact-node acceptance owner:

```sh
.venv/bin/python tools/verify_acceptance_paths.py --catalog automatic \
  --evidence-dir /private/host/scenarios --observer-key /private/host-keys/observer.key
```

The aggregate retains original conformance grades and six terminal dispositions:
accepted, expected abstention, failed, cancelled, budget stopped and externally
blocked. It distinguishes an explicit qualification disposition from observed
process termination; missing final receipts preserve actual starts and unknown
stops. Case/node, native attempt, original question and protected reference
inventories remain separately bound to their original evidence. An expected
abstention can pass its scenario while the research claim stays unaccepted.

Failed phases immediately retain known built-in exception names and locations
from frozen source declarations, bound to the original worker and failure
digest. Other class and location identities remain opaque hashes. Exception
prose, locals and optional exception metadata are omitted. These bounded
diagnostics remain available after a later process timeout; they do not supply
acceptance or full traceback replay.

Installed transport evidence uses one genuine business execution and three
complete actual Python, console and real stdio journeys. The report declares
`one_business_execution`, `three_transport_journeys` and `exact_request_replay`:
all adapters use the same canonical run, request, refresh-parent and planner
arguments. Console and stdio replay the original requests while current, brief
and closing native proofs remain live. Replayed requests do not establish fresh
generation per adapter. The one-step refresh may stop at its budget; this is a
bounded refresh observation, not accepted refreshed research. Original native
attempts and questions are counted once by workspace/run/owner identity, with
all three route observations retained. Missing or conflicting copies refuse
qualification. One fresh complete reference inventory is retained once. The console bytes and installed package must match the
same Build archive. The fixed public native replay owner checks the current v13
profile, original authenticated journal, review signatures, source eligibility,
host configuration and reference accounting from the same installed interpreter.
The producer retains the complete original proof in private ordered parts of at
most 32,768 bytes, with a 4 MiB aggregate bound. Its bounded stdout contains the
original manifest commitment. The reader checks every part, exact order and
inventory, original byte lengths and hashes, and the complete canonical payload
before validating all reference, native attempt and question obligations.
Missing, changed, extra or truncated parts refuse qualification; a summary or a
caller-supplied hash cannot replace the original proof.

Protected reference authentication precedes an explicit authenticated current
configuration probe. Provider callbacks stay outside the native workspace gate;
the final effect-free read captures current evidence and complete main and child
journals under that gate, checks closing source and state bytes, and must finish
inside the original 30-second witness. Each adapter retains its actual proof and
journal head. Later route reads may extend an earlier authenticated journal only
through genuine calibration probe records with unchanged business, source,
profile and configuration bindings. Business mutations, rewritten prefixes,
missing records or withdrawn current qualification refuse the replay.

Private factories must not read stdin or write protocol stdout/stderr. Ordinary
output or consumed initialization cannot qualify a machine transcript. A timed
out factory can have uncertain effects; the original enclosing process deadline
covers filesystem startup, callback imports and all subsequent work.

Qualification costs cover the original observed host intervals, including setup,
reference work, retries, repair and reviews within the jobs. The Measurement job
reproduces the prior completed jobs; final aggregation includes Measurement and
Verification themselves. Provider tokens, charges and pre-freeze implementation
work are outside that measured scope and remain unknown.
