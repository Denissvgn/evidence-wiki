# Library API

`evidence-wiki` can be driven two ways: by spawning the CLI, or by calling the
package in-process. This document is the contract for the second. It is aimed at
a host that embeds EvidenceWiki as an evidence layer inside a longer-lived
process — an ASGI service, a scheduler, a batch worker — and wants coverage
evaluation, quote verification, and the orchestration protocol without paying a
process spawn and a JSON round-trip per operation.

The subprocess boundary is not removed and is not deprecated. `evidence-wiki` the
command remains the supported way to drive a workspace from a shell, from a
Makefile, or from a host that wants hard process isolation. The API is the
*choice*, not the replacement, and the two doors render from one seam per
operation — see [seam conformance](#the-cli-and-the-api-cannot-disagree) — so a
host can move an operation from one door to the other without changing what the
operation means.

```python
import evidence_wiki
from evidence_wiki import Workspace

with Workspace.open("/path/to/workspace") as ws:
    status = ws.status()
    report = ws.coverage.evaluate("benchmarks")
```

Everything the API returns is a plain `dict` — exactly the document the matching
`--format json` command prints, freshly built and owned by the caller. Everything
it refuses with is an [`EvidenceWikiError`](#errors) carrying the same stable
`error_code` the CLI writes into its stderr envelope.

## Opening A Workspace

`Workspace.open(path)` validates and never creates:

```python
from evidence_wiki import Workspace

ws = Workspace.open("~/research/solid-state-batteries")
print(ws.root)      # resolved, user-expanded
print(ws.closed)    # False
```

The path is `expanduser()`-ed and `resolve()`-d, must be a directory, and must
contain `research.yml`. Two refusals, both `ConfigError`:

| Condition | `error_code` |
|-----------|--------------|
| Not a usable directory, or a path that cannot be resolved at all (symlink loop, over-long path, `~` with no home) | `WORKSPACE_UNREADABLE` |
| A directory with no `research.yml` | `CONFIG_MISSING` |

Pointing at the wrong path is therefore a typed refusal, never a stray directory
tree. There is no programmatic `init` or `upgrade`; use `evidence-wiki init` and
`evidence-wiki upgrade` for those (see [what the API does not
expose](#what-the-api-deliberately-omits)).

`Workspace` is a context manager, and `with` is the shape to prefer — see
[handle lifetime](#handle-lifetime-and-close) for what `close()` does and,
importantly, what it does not.

## The Surface

Twenty-six operations. Most hang off an open handle, in namespaces; the
exceptions are `Workspace.open` itself and the two module-level functions that
belong to no single workspace.

**`Workspace`** — the handle itself:

| Operation | Signature |
|-----------|-----------|
| `workspace.open` | `Workspace.open(path: str \| Path) -> Workspace` |
| `workspace.close` | `ws.close() -> None` |
| `workspace.versions` | `ws.versions() -> dict` |
| `workspace.status` | `ws.status(*, no_cache=False, run_id=None, **counters) -> dict` |
| `workspace.export_answers` | `ws.export_answers(status: list[str] \| None = None) -> dict` |
| `workspace.doctor` | `ws.doctor() -> dict` |

`ws.status()` takes nine optional counter keywords — `questions_processed_this_run`,
`source_requests_opened_this_run`, `releases_this_run`,
`discovery_results_this_run`, `acquisition_downloads_this_run`,
`github_archive_bytes_this_run`, `academic_provider_requests_this_run`,
`web_downloads_this_run`, `manual_url_deliveries_this_run` — each mirroring the
CLI flag of the same name one for one, plus `no_cache` and `run_id`.

**`ws.coverage`**, **`ws.grounding`**, **`ws.normalize`** — evidence checks:

| Operation | Signature |
|-----------|-----------|
| `coverage.evaluate` | `ws.coverage.evaluate(slug: str) -> dict` |
| `grounding.verify` | `ws.grounding.verify(slugs: Sequence[str], *, write=False, verified_by=None) -> dict` |
| `normalize.verify` | `ws.normalize.verify(source_ids: Sequence[str] \| None = None) -> dict` |

`coverage.evaluate` is not a read: the recomputed facet verdicts, coverage
verdict and `updated_at` are written back to `sources/coverage/<slug>.yml`,
exactly as the CLI leaves them. `normalize.verify(None)` means `--all`.

**`ws.questions`** — the question lifecycle. Every keyword mirrors the CLI flag
of the same name, and every mutating call writes its `log.md` entry and its page
frontmatter inside the call, so a question moved in-process leaves the same audit
trail as one moved from a shell:

| Operation | Signature |
|-----------|-----------|
| `questions.claim` | `claim(*, slug, agent_id, steal=False, if_older_than=None)` |
| `questions.release` | `release(*, slug, agent_id)` |
| `questions.answer` | `answer(*, slug, agent_id, answer_page, source_id=None, allow_uncited=False, allow_unclaimed=False, confidence=None, evidence_strength=None, require_coverage=False, require_grounding=False, coverage_manifest=None, grounding_file=None)` |
| `questions.block` | `block(*, slug, agent_id, blocked_reason, request_id=None, allow_unclaimed=False)` |
| `questions.defer` | `defer(*, slug, agent_id, reason, allow_unclaimed=False)` |
| `questions.reject` | `reject(*, slug, agent_id, reason, allow_unclaimed=False)` |
| `questions.reopen` | `reopen(*, slug, agent_id, source_id, request_id=None)` |
| `questions.approve` | `approve(*, slug, reviewer)` |
| `questions.review` | `review(*, slug, policy, verdict, reviewed_by, review_ref=None, note=None)` |
| `questions.set_grounding` | `set_grounding(*, slug, agent_id, from_file, allow_unclaimed=False)` |
| `questions.add_batch` | `add_batch(*, from_file="-", dry_run=False)` |

Three details are deliberate rather than oversights. `reopen` has no
`allow_unclaimed`, because a blocked question is never claimed. `approve` and
`review` record a `reviewer`/`reviewed_by` rather than an `agent_id`, matching
the CLI's trust model: this authenticates nobody, and the audit trail is the
frontmatter entry plus `log.md`. And `add_batch(from_file="-")` reads the batch
from *this process's* stdin, exactly as the CLI does — rarely what a server wants,
so pass a path. `add_batch(dry_run=True)` returns the report the writes *would*
have produced; it is the supported way to preview intake, since the page writes,
the `index.md` update, and the `log.md` entry are otherwise part of the same
operation as the report.

**`ws.orchestrate`** — the protocol, one work order at a time:

| Operation | Signature |
|-----------|-----------|
| `orchestrate.start` | `ws.orchestrate.start(agent_id, *, orchestration_id=None, max_actions=None, action_timeout_seconds=None, total_timeout_seconds=None) -> OrchestrationSession` |
| `orchestrate.session.next` | `session.next(*, agent_id=None, resume=False) -> dict` |
| `orchestrate.session.submit` | `session.submit(action_id, result: dict \| str \| PathLike, *, agent_id=None) -> dict` |
| `orchestrate.session.status` | `session.status() -> dict` |

Every limit left as `None` is omitted from the controller's argv, so the
workspace's deployed controller applies its own default rather than this package
pinning one a newer workspace has moved on from. See [version
authority](#version-authority) for why these four are the operations that keep a
subprocess.

**Module level** — no single handle owns these:

| Operation | Signature |
|-----------|-----------|
| `fleet_status` | `evidence_wiki.fleet_status(targets: Sequence[str \| Path], *, no_cache=False) -> dict` |
| `contract` | `evidence_wiki.contract() -> dict` |

`fleet_status` takes paths rather than open handles because it aggregates across
many workspaces at once. `contract()` is the same payload `evidence-wiki
contract` prints, built once below both front ends.

### Negotiating Against The Contract

The authoritative list of what this installation implements is in the capability
contract, and a host should negotiate against it rather than hard-coding a
version comparison:

```python
import evidence_wiki

library_api = evidence_wiki.contract()["library_api"]
assert library_api["version"] == "1"
assert "coverage.evaluate" in library_api["surface"]
```

`surface` is a list of `<namespace>.<operation>` names; `version` is the
compatibility signal. The list is a *declaration*, deliberately not introspected
from live objects — walking the classes at call time would make the published
contract depend on import order and would silently widen or narrow the API every
time an internal helper was renamed. A change that a version `"1"` caller cannot
absorb bumps `version` rather than editing the list in place, so a host that
understands `"1"` knows exactly which names the list may contain.

One public method is intentionally absent from that list:
`ws.orchestrate.session(orchestration_id)` returns a driver for an existing
session without touching it (naming a session is not reading one), so a host that
restarts can reconstruct its drivers without a controller spawn per session. It
is not a declared v1 operation; the four operations it gives access to are.

## Errors

Every refusal is an `evidence_wiki.errors.EvidenceWikiError` or a subclass, and
carries the whole error envelope:

| Attribute | Meaning |
|-----------|---------|
| `error_code` | The stable code. Branch on this. |
| `message` | Human-readable. Log as-is; `str(exc)` is the same string. |
| `recoverable` | Whether a retry is meaningful. `False` for `CLAIM_HELD` and `CLAIM_NOT_STALE`. |
| `remediation` | What an operator should do. Surface it. |
| `details` | Structured context, possibly empty. |
| `exit_code` | The status the CLI would have exited with: `2` for a fatal caller-fixable error, `3` for a conflict. |

Thirteen families sit under the base class. The family is selected from the code
by prefix, with exact codes winning over prefixes and longer prefixes over
shorter ones — which is how `QUESTION_NOT_CLAIMED` lands in `ClaimError` while
`QUESTION_REOPEN_DELEGATED` lands in `RequestError`:

| Family | Covers |
|--------|--------|
| `ConfigError` | The workspace, its configuration, or the runtime cannot support the call: `CONFIG_*`, `WORKSPACE_UNREADABLE`, `DEPENDENCY_MISSING`, `TOOLING_MISSING`, `UPGRADE_WRITE_FAILED`. |
| `LockError` | `LOCK_UNAVAILABLE` — a workspace lock is held by another writer; that includes the per-session orchestration lock. |
| `ClaimError` | A claim could not be taken, stolen, released, or resolved: `CLAIM_*`, `STEAL_*`, `STATUS_NOT_*`, `QUESTION_NOT_CLAIMED`. |
| `QuestionError` | A question, slug, or answer page is unusable: `QUESTION_*`, `SLUG_*`, `ANSWER_*`, `PAGE_INVALID`, `RESOLUTION_REASON_INVALID`. |
| `CoverageError` | `COVERAGE_*`, `FACET_SCOPE_CONFLICT`. |
| `GroundingError` | `GROUNDING_*` — a claim is not grounded in a verifiable quote from an accepted source. |
| `RequestError` | Source requests: `REQUEST_*`, `SOURCE_REQUEST_FULFILL_DELEGATED`, `QUESTION_REOPEN_DELEGATED`, `ATTEMPT_FAILURE_CODE_INVALID`. |
| `SourceError` | Sources and their inventory: `SOURCE_*`, `MANIFEST_*`, `INVENTORY_*`. |
| `IntakeError` | Externally supplied intake refused by a cap, rate limit, or signature check: `INTAKE_*`, `HANDOFF_SIGNATURE_INVALID`. |
| `ProviderError` | Discovery and acquisition: `PROVIDER_*`, `ACQUISITION_*`, `DISCOVERY_*`, `GITHUB_*`, `OPENALEX_*`, `ARXIV_*`. |
| `RunError` | `RUN_*`, `BASELINE_*`, `EVENT_*`, `FINAL_VERDICT_REQUIRED`. |
| `OrchestrationError` | `ORCHESTRATION_*`, including the two host-side codes below. |
| `UsageError` | The call itself is malformed, refused before any workspace state is read: `AGENT_ID_INVALID`, `QUERY_MISSING`, `VALUE_INVALID`, `NOT_IMPLEMENTED`. |

Two `ORCHESTRATION_`-prefixed codes are minted on the host side of the process
boundary, where the workspace's vocabulary cannot reach:
`ORCHESTRATION_HOST_FAILED` (the controller failed without emitting a usable
envelope) and `ORCHESTRATION_HOST_EXITED` (a workspace script exited instead of
returning). They keep the prefix so a host already catching `OrchestrationError`
catches them too.

### An Unknown Code Never Explodes

A code this version has never seen — a newer workspace, a newer domain pack —
degrades to the base class with the code preserved:

```python
from evidence_wiki import errors

error = errors.error_from_envelope(
    {"error_code": "SOMETHING_FROM_THE_FUTURE", "message": "a newer workspace said so"}
)
type(error)         # <class 'evidence_wiki.errors.EvidenceWikiError'>
error.error_code    # 'SOMETHING_FROM_THE_FUTURE'
```

This is the property that keeps an older host working against a newer workspace,
and it is why **`except EvidenceWikiError` must be the outermost arm** in any
host that catches by family. Family catches are a convenience, not a partition of
the code space: a handful of codes the deployed orchestration controller can emit
today — `RESULT_INVALID`, `WORK_ORDER_INVALID`, `ACTION_ID_INVALID`,
`ARTIFACT_PATH_INVALID`, `CANDIDATE_STORE_INVALID` — have no family entry and
arrive as the base class, so `except OrchestrationError` alone would miss them.
Dispatch on `error_code` when the distinction matters.

Two more degradations, all on the failure path where a second exception would
bury the original diagnosis: an envelope with no code reports `UNKNOWN`, and
input that is not a usable envelope reports `ENVELOPE_MALFORMED` while keeping
whatever code it did carry. `error_from_envelope` never raises.

Note also what is *not* translated. An exception that is neither a workspace
refusal nor a `SystemExit` propagates unchanged — a `TypeError` from a bad
argument is a bug in the calling code, not a workspace condition, and dressing it
as an `EvidenceWikiError` would hide it. `SystemExit` is always translated,
because a library that let one escape would terminate an ASGI worker.

### The CLI And The API Cannot Disagree

The codes above are the same strings the CLI writes into its stderr envelopes,
and that is a structural property rather than a convention that has to be
maintained. Each operation has exactly one implementation — a `run_<op>(...) ->
dict` seam in the workspace script — and both front ends render from it: the CLI
prints the returned document or the raised refusal's envelope, the API returns
the document or raises the typed exception built from that same envelope.
`tests/test_seam_conformance.py` runs the CLI as a real subprocess against the
seam over identical inputs and requires them to agree on the success document, on
the refusal envelope, and on the exit code, for every enrolled script. A change
to one path cannot quietly move only one of them.

## Thread Safety

**Concurrent API calls are as safe as concurrent CLI processes, and no safer.**
That is the guarantee, and it is the reason to adopt the API at all: the
filesystem arbitrates, exactly as it does between processes.

- **Contention surfaces as a typed refusal, never as corruption.** Two writers
  racing for the same claim produce one winner and one `ClaimError` /
  `CLAIM_HELD` (`recoverable=False`, `exit_code=3`), or a `LockError` /
  `LOCK_UNAVAILABLE` if the workspace write lock itself was contended. Both
  outcomes are correct; what never happens is two writers both believing they
  hold the claim.
- **The API introduces no process-global mutation.** In particular it **never
  redirects `sys.stdout`**, not on the success path and not on the refusal path
  where the CLI renders an envelope. That is what makes it usable from a
  multithreaded server: a host that captured its own stdout concurrently would
  have had its capture swapped out from under it, and putting the original back
  afterwards would not help. `tests/test_library_concurrency.py` asserts this by
  object *identity*, which is the only assertion that notices.
- **Two threads through one handle each get a whole answer.** Not two halves of
  one, and not one shared `dict` handed to every caller — results are distinct
  objects, so a host that mutates its own result cannot corrupt another
  request's.
- **A cold start under contention is safe.** Loading a packaged workspace script
  briefly mutates `sys.path` and `sys.modules`; one process-wide reentrant lock
  covers every such load, including the lazy sibling loads a script performs
  *while a seam is running*. The lock is a load-time lock only — a warm operation
  takes no loads at all, so concurrent operations stay concurrent.

What remains the host's job:

> **Serialize `next`/`submit` per orchestration session.** The protocol is a
> single-driver protocol. The workspace enforces this with a per-session lock at
> `runs/orchestrations/<id>/.locks/session.lock`: a second driver waits for the
> lock's bounded window and is refused with `LOCK_UNAVAILABLE` if the window
> expires. It never interleaves two, so a violation is a delay or a refusal
> rather than a corrupted session. A host that wants forward progress instead of
> a refusal — and that does not want to spend a controller subprocess per waiter
> — holds its own per-session lock; the [ASGI
> example](#embedding-in-an-asgi-service) below shows the shape.
>
> `next` is idempotent, which softens this: eight threads calling `next()` on one
> session with no host lock all succeed and all receive the *same* pending work
> order, because the session lock serialized them and the controller replayed
> rather than issuing eight actions. `submit` is where an unserialized second
> driver actually costs something.

Nothing here makes a *question* safe to drive from two places at once either.
The workspace protects its own state; it does not merge two agents' intentions.

## Handle Lifetime And `close()`

Packaged assets are held by a **process-wide shared assets root**, entered once
on first use and released at interpreter exit.

This matters for a zip or otherwise non-extracted install, where
`evidence_wiki.resources.assets_root()` has to materialize the asset tree into a
temporary directory that only survives inside a `with` block. If every handle
entered its own, N open handles would mean N full asset extractions — and N
disjoint families of cached script modules, since that cache is keyed by the
resolved script directory. Entering exactly once per process bounds both. (The
CLI keeps its per-command entry; a one-shot process cannot tell the difference.)

So:

```python
ws = Workspace.open(root)
ws.status()
ws.close()

ws.closed          # True
repr(ws)           # '<Workspace /path/to/workspace (closed)>'
ws.root            # still readable
ws.status()        # ConfigError: WORKSPACE_UNREADABLE
```

`close()` invalidates *this handle only*. Every later operation on it raises
`ConfigError` / `WORKSPACE_UNREADABLE` — calling one then is a use-after-close bug
in the host, not a question about the workspace — while the inert identity
accessors (`root`, `closed`, `repr()`) keep working, so a host can still log which
handle it over-released. `close()` is idempotent.

`close()` does **not** tear down the shared assets root, because sibling handles
in the same process are still using it. Releasing it at interpreter exit is the
whole design, not a leak.

A handle is cheap: opening one validates a path and touches no assets at all. The
usual shape is one long-lived handle per workspace root for the life of the
process.

## Version Authority

Two different rules apply, and the difference is the point.

**Read, evaluate, and resolve operations run the packaged scripts in-process.**
The scripts come from the assets that ship with the *installed package* —
deliberately not from `<workspace>/scripts` — so one installed version drives
every open workspace, and a host cannot be made to execute code out of a
workspace directory. The installed library version is the behavior version,
exactly as it already is for `evidence-wiki status`.

**Orchestration `start`/`next`/`submit`/`status` keeps a subprocess.** Each of
those four spawns the workspace's *own deployed* controller at
`<workspace>/scripts/orchestration_controller.py`:

```text
sys.executable -B <workspace>/scripts/orchestration_controller.py \
    --project-root <workspace> next --orchestration-id ... [--agent-id ...]
```

That deployed controller is authoritative for run-state mutation precisely
because it is version-matched to the session state it owns. Loading it in-process
— or substituting this distribution's packaged copy — would let one installed
library version mutate run state belonging to a different workspace version. It
is the same reason managed orchestration is absent from the MCP server.

What the facade removes is the *CLI* process: argument parsing, config discovery,
and the interpreter start for `evidence_wiki.cli`. It does not remove the
controller boundary, and **the subprocess must not be optimized away.** If a
future change makes orchestration feel slow, the answer is fewer controller
round-trips, not an in-process shortcut.

## Version Skew

`Workspace.open` has **no version gate**, on purpose. It refuses only structural
absence — a path that is not a usable directory, or one without `research.yml`.
It does not compare the installed package's version against the workspace's
deployed scripts, because the CLI has no such gate: adding one here would make
the API refuse workspaces the CLI happily serves, which is exactly the CLI/API
divergence this API exists to remove. The workspace scripts already own their own
compatibility checks.

Skew therefore surfaces where it already surfaced: as a script's own typed
refusal, at the call that actually depends on the incompatible piece.

What the API adds is *visibility*. `ws.versions()` reports what is installed
beside what is deployed:

```python
ws.versions()
# {'package': '0.3.0',
#  'workspace': {'starter_version': '0.5.5',
#                'schema_version': '0.1',
#                'compatible_research_yml_contract': '0.1'}}
```

This is a pure read and never refuses *on the basis of what it reads*. Every
workspace key degrades to `None` when `workspace-system.yml` is absent,
unreadable, not valid YAML, or shaped unexpectedly — reporting skew is the whole
job, so raising on the very file that would reveal it would defeat the purpose. A
host that wants a hard failure compares the values itself. The one refusal is a
closed handle, which is not about content.

`ws.doctor()` is the documented preflight. It diagnoses runtime, tooling, and
configuration and returns the same report `evidence-wiki doctor --format json`
prints; almost nothing refuses, because diagnosing a broken workspace is the
reason to call it. A workspace the doctor cannot read at all comes back as
`verdict: "missing"` with a full report rather than an exception:

```python
report = ws.doctor()
if report["verdict"] != "ok":
    failed = [check for check in report["checks"] if check["status"] != "ok"]
    for check in failed:
        check["id"], check["label"], check["message"], check["remediation"]
```

Every entry in `report["checks"]` carries `id`, `label`, `status`, `required`,
`message`, `remediation`, and `implication`; some also carry `version` and
`details`. A domain error raised while running a check is folded into that
check's own entry rather than aborting the report, and the failing checks of an
unreadable workspace are marked `degraded` or `missing` rather than omitted.

## Two Behaviors A Host Will Otherwise Get Wrong

### A Failed Verification Is Not A Refusal

`ws.grounding.verify` and `ws.normalize.verify` **return** a report whose
`overall_result` is `not_verified` when something does not verify. The CLI prints
that same document and *then* exits non-zero — the non-zero status is a verdict,
not an error, and the document is the answer:

```python
report = ws.grounding.verify(["benchmarks"])
if report["overall_result"] != "verified":
    for question in report["questions"]:
        if not question["all_verified"]:
            ...   # which claim, against which record, and what would fix it
```

Which claim failed, against which record, and what would fix it are the entire
point of asking, and none of it can be read off an exception. Only a genuine
refusal raises — a malformed grounding block (`GroundingError`), an unknown slug
(`QuestionError`), an empty `slugs` sequence (`QuestionError` / `SLUG_INVALID`,
exactly as omitting `--slug` does), a workspace that cannot be read at all
(`ConfigError`).

### `fleet_status` Never Raises For A Bad Target

Ten good workspaces and one bad path yield eleven answers:

```python
report = evidence_wiki.fleet_status([good_root, "/definitely/not/a/workspace"])
report["counts"]["targets"]   # 2
report["counts"]["errors"]    # 1

for entry in report["targets"]:
    if not entry["ok"]:
        entry["path"], entry["error_code"], entry["message"]
```

Each entry carries `path` and `ok`; a failing entry adds that target's own
`error_code` and `message`. That degradation is the contract of this call, not an
accident, and it is why an operator can point it at a whole fleet without
pre-validating it. `fleet_status` raises only if the packaged scripts themselves
cannot be loaded, which is an installation problem rather than a fleet finding.

## Embedding In An ASGI Service

**The package ships no HTTP server, and will not.** Transport, authentication,
tenancy, and rate limiting are the host's, and an HTTP surface baked into this
package would have to guess all four. What follows is the shape a host builds on
top — framework-agnostic, and complete enough to lift.

Four properties carry the weight:

1. **One `Workspace` per workspace root**, held for the life of the process.
   Opening is cheap; extraction and script caches are process-wide anyway.
2. **Calls are offloaded to a thread.** The API is blocking filesystem I/O, not
   async. Calling it directly from a coroutine blocks the event loop.
3. **Orchestration `next`/`submit` is serialized per session** by the host's own
   lock, so concurrent requests queue instead of colliding with the workspace's
   per-session lock.
4. **Typed errors map to HTTP responses** using `error_code` and `recoverable`.

```python
"""An embedding layer for EvidenceWiki. Wire the two coroutines into any ASGI
framework's routes; nothing here depends on one."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from evidence_wiki import Workspace, errors

# 1. One handle per workspace root, for the life of the process.
_workspaces: dict[str, Workspace] = {}
_workspaces_guard = asyncio.Lock()

# 3. One lock per orchestration session; the protocol is single-driver.
_session_locks: dict[str, asyncio.Lock] = {}


async def workspace_for(root: str | Path) -> Workspace:
    key = str(Path(root).expanduser().resolve())
    async with _workspaces_guard:
        handle = _workspaces.get(key)
        if handle is None or handle.closed:
            handle = _workspaces[key] = Workspace.open(key)
        return handle


async def session_lock(orchestration_id: str) -> asyncio.Lock:
    async with _workspaces_guard:
        return _session_locks.setdefault(orchestration_id, asyncio.Lock())


# 4. error_code and recoverable decide the status; the envelope is the body.
_NOT_FOUND = {"CONFIG_MISSING", "WORKSPACE_UNREADABLE"}


def http_error(exc: errors.EvidenceWikiError) -> tuple[int, dict[str, Any]]:
    if isinstance(exc, errors.ConfigError) and exc.error_code in _NOT_FOUND:
        status = 404
    elif isinstance(exc, errors.UsageError):
        status = 400
    elif exc.exit_code == 3:        # a conflict: CLAIM_HELD, CLAIM_NOT_STALE
        status = 409
    elif isinstance(exc, errors.LockError):
        status = 503               # contended, and worth retrying
    elif exc.recoverable:
        status = 422
    else:
        status = 409
    return status, {
        "error_code": exc.error_code,
        "message": exc.message,
        "remediation": exc.remediation,
        "recoverable": exc.recoverable,
        "details": exc.details,
    }


async def get_coverage(root: str, slug: str) -> tuple[int, dict[str, Any]]:
    """GET /workspaces/{root}/coverage/{slug}"""
    try:
        # Opening refuses too, so it belongs inside the handler.
        ws = await workspace_for(root)
        # 2. Blocking I/O belongs on a worker thread, never on the event loop.
        return 200, await asyncio.to_thread(ws.coverage.evaluate, slug)
    except errors.EvidenceWikiError as exc:
        return http_error(exc)


async def post_orchestration_next(root: str, orchestration_id: str) -> tuple[int, dict[str, Any]]:
    """POST /workspaces/{root}/orchestrations/{id}/next"""
    try:
        ws = await workspace_for(root)
        session = ws.orchestrate.session(orchestration_id)
        async with await session_lock(orchestration_id):
            return 200, await asyncio.to_thread(session.next)
    except errors.EvidenceWikiError as exc:
        return http_error(exc)
```

Notes on the shape:

- `except errors.EvidenceWikiError` is the outermost arm, for the reason given
  under [errors](#an-unknown-code-never-explodes): a newer workspace can emit a
  code this host has never seen, and it must become a 4xx/5xx rather than a 500
  traceback.
- A returned work order is validated by the same `_validate_work_order` the
  managed runner uses — bounded size, safe relative paths, no absolute paths, no
  environment credential values — so an embedding host gets exactly the
  guarantees the managed path gets.
- `session.next()` returns the *work order* while the session can still make
  progress and the *session document* once it cannot. A session that has ended is
  an answer, not an error, even though the controller signals it with a non-zero
  exit; loop on the returned `artifact_type`
  (`orchestration_work_order` vs `orchestration_session`), not on an exception.
- `session.submit(action_id, result)` takes the result document itself — written
  to a temporary file outside the workspace tree and removed again, on every path
  including the one where the controller refuses — or a path to a file that
  already holds one. The document is a *claim*: the controller re-verifies the
  workspace artifacts it names and refuses a result whose postconditions the
  workspace does not actually satisfy, so a well-formed document is not a way to
  make a session progress.
- If the host process holds handles across a workspace upgrade, drop and reopen
  them; `versions()` is the cheap way to notice.

## What The API Deliberately Omits

Absences worth stating, so they are not read as gaps:

- **`--format` and `--output` have no counterparts.** They choose how a document
  is rendered and where it is delivered, not what it says. The API returns the
  document; a host that wants `jsonl` bytes or a file on disk reshapes and writes
  it itself.
- **`status`'s `--append-log` has no counterpart.** Appending to `log.md` is
  something the CLI does *after* the status document is produced, not part of
  producing it. A host that wants a log entry writes one.
- **`doctor`'s `env` injection point is not exposed.** `DoctorEnvironment` is
  defined inside a packaged script asset rather than in this package, so a host
  has no supported way to name the type it would have to construct; it exists for
  the doctor's own tests, which reach the seam directly. Exposing it would publish
  a parameter whose only legal value the public API cannot hand out.
- **Programmatic `init` and `upgrade` are out of scope.** `Workspace.open`
  validates and never creates. Workspace creation and starter upgrades stay with
  `evidence-wiki init`, `evidence-wiki deploy`, and `evidence-wiki upgrade`, which
  own their own dry-run, conflict, and preservation rules.
- **Managed orchestration runners are not on the API.** `orchestrate run` and
  `orchestrate resume` drive a package-managed Codex or Claude worker under an
  isolation boundary; the API exposes the model-neutral protocol
  (`start`/`next`/`submit`/`status`) that an embedding host drives itself.

## See Also

- [Parent orchestration](../workspace-template/docs/orchestration.md) — session
  artifacts, routing, recovery, and the isolation boundaries the protocol assumes.
- [Orchestrator handoff](../workspace-template/docs/orchestrator-handoff.md) —
  what an external driver owes the workspace.
- [Question API](../workspace-template/docs/question-api.md) — slugs, grounding
  blocks, resolution outcomes, and the full refusal-code table.
- [Coverage manifests](../workspace-template/docs/coverage-manifest.md) — what
  `coverage.evaluate` reads and writes.
- [Workspace status](../workspace-template/docs/workspace-status.md) — the
  document `status()` returns.
