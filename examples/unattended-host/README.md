# Unattended Python host

This small application drives EvidenceWiki through public Python APIs. It has an
isolated deterministic mechanics mode and a separate live mode using explicitly
provisioned host services. It never prompts for semantic choices, credentials,
review or routine signoff. Native current evidence, mandatory independent review,
qualification and finite RunPolicy budgets determine the outcome.

## Get the example from an installed package

The installed `example/unattended-host/v1` resource contains this directory's
files. Use the public content-only Onboarding API to save the bundle in a new
application directory; choose that directory yourself:

```python
import json
from pathlib import Path
from evidence_wiki import Onboarding

with Onboarding.open() as host:
    bundle = json.loads(host.resource("example/unattended-host/v1")["content"])
destination = Path("unattended-host")
destination.mkdir()
for name, content in bundle["files"].items():
    (destination / name).write_text(content, encoding="utf-8")
```

Run every command below with the Python interpreter where EvidenceWiki is
installed. The application uses the standard library and the package's PyYAML
dependency; it imports no checkout helpers or internal EvidenceWiki modules.

## Deterministic mechanics mode

```bash
.venv/bin/python unattended-host/host.py --mode fake --output /absolute/new-fixture-directory
.venv/bin/python unattended-host/host.py --mode fake --scenario abstain \
  --output /absolute/another-new-fixture-directory --no-refresh
```

The output directory must be new. The host creates a workspace through
`Onboarding.plan/apply`, captures and normalizes one synthetic observation,
and admits the exact sanitized raw/provenance/normalized closure through the
public usage owner. It creates distinct Generator, Evidence and Critic identities
with fresh private fixture keys, explicit source permissions and a RunPolicy.
Workers receive only native scoped job/input data. Signing keys and protected
reference labels stay in the host, outside worker inputs.

A deliberately overgeneral claim is reviewed by both Evidence and Critic. The
critic's failed support/scope verdict triggers the native bounded repair route,
which preserves claim identity and requires fresh first passes. The repair mode
returns the scoped finding, current structured brief and bounded authenticated
accounting, then drives an explicit new refresh run through its frozen planner.
Refresh has a separate default eight-step bound and can stop at the native
budget gate before completing new review. Choose `--refresh-max-steps 64` for
the full refresh allowance within the same finite host deadline; prior approval
is never reused. The abstain mode retains disagreement rather than inventing approval. With
`--max-steps 1`, the existing native budget owner returns a terminal budget result.

Fake workers do not call a model or provider. Their `FixtureMeter` deliberately
simulates model-count fields to exercise accounting/review protocol mechanics;
those values are **not observed model usage, tokens, quality or provider cost**.
Every result is labeled `fixture_mechanics`, with `production_qualification: false`.
The local authority and issuer are fixture-only, use fresh random keys, and cannot
supply an existing production trust policy. Live mode refuses simulated/offline
model identities. Do not adopt the fixture authority, records or meter into a
production runtime. The synthetic observation and deterministic reviewers are
an example domain, not scientific machine-review qualification.

Private fixture authority/state files are created with owner-only permissions.
The bounded reference executor isolates process groups and clears worker
credentials; it does not establish adversarial filesystem/network isolation.
The host owns protected paths and native trust configuration. All result flags
remain inert: transport success does not authorize an external action.

## Live host mode

Provision a real workspace, current source rights, isolated generator/evaluator
controllers, machine qualification records, independent host usage observation,
and signing services in your trusted application. Then select them explicitly:

```bash
.venv/bin/python unattended-host/host.py --mode live --workspace /absolute/workspace \
  --runtime /absolute/private-runtime.json --run-id study --request-id drive
```

`HostRuntime.from_file` accepts its published owner-private JSON format for
supported native profiles v1-v3. Unsupported or missing services return a bounded
terminal refusal without provisioning credentials or requesting operator input.
For complete advanced public services or your own AgentExecutor, use a trusted
owner-private Python factory instead:

```bash
.venv/bin/python unattended-host/host.py --mode live --workspace /absolute/workspace \
  --services /absolute/private/host_services.py --run-id study --request-id drive
```

The explicitly selected file is trusted host code, with owner-only permissions,
no links and a 64 KiB bound. Its public factory has this contract:

```python
from evidence_wiki import HostRuntime, ResearchExecution

def build_runtime(workspace_root):
    # All selections below belong to your trusted host, never worker output.
    execution = ResearchExecution(
        configured_executor,
        qualifications=current_qualified_evaluators,
        review_signer=protected_review_signer,
        observer_key=protected_observer_key,
    )
    return HostRuntime.from_services(frozen_policy, selected_context, execution)
```

An AgentExecutor exposes `profile`, `invoke(attempt)`, `poll(attempt)`,
`cancel(attempt)` and `authenticate(observation, attempt)`. `BoundedSubprocessExecutor`
is the provided reference backend; a custom host backend must authenticate actual
execution/accounting and keep credentials and signed review authority outside
worker data. The [public integration contract](../../docs/library-api.md)
explains the existing service and qualification constructors. The runnable fixture
shows those public calls end to end; adapting it cannot turn its simulated records
into live qualification. Live production records must match current native policy,
rubric, scope, profile/model/prompt identities and independent controllers.

Both modes use `Workspace.research.run_cycle`, current `session.brief/report`,
and `Workspace.research.refresh`. An optional planner is chosen only from the
frozen host profile. Native owners authenticate machine review and preserve every
original, unsupported capability, unresolved result and consumed allowance.
A finite host OperationControl is closed after execution. It supplements the
original native run deadline; a new invocation cannot reset retained budget.
Use the same live runtime/run/request IDs for exact replay. Changed parameters
refuse rather than silently restarting work. A refresh allocates an explicit new
run and retains the previous terminal history and usage.

Stdout is one bounded JSON result; native setup diagnostics use stderr. No secret
values or held-out expected labels are emitted. The application returns `0` for
accepted/partial research, `3` for a native negative/budget outcome, and the native
error status for a refusal. Output contains current structured export plus bounded
native trace/accounting; it does not promise complete provider billing or whole-host
intervention telemetry. Missing live capabilities are an ordinary terminal result.
