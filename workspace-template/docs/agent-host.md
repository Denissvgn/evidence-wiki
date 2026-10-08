# Automated agent host recipe

Recipe version: `evidence-agent-host-recipes/v1`. Discover it with
`Onboarding.recipes()` or MCP `onboarding_recipes`. Fetch content by exact
resource ID with `Onboarding.resource(id)` or MCP `onboarding_resource`.
`example/agent-host/v1` contains an executable host; `example/unattended-host/v1`
provides public runtime provisioning and fixture mechanics.

The host selects an autonomous `HostRuntime`, roots, writable operations,
qualified evaluators, credentials and budgets before dispatch. A human-required
or incompatible profile terminates with `profile_incompatible`. Missing
capabilities terminate with `missing_capability`. Neither asks for an operator.
Capability strings used for instruction selection are host declarations, not
permission grants or proof of service availability.

## Select a small context

Construct public `AgentHostContext(task=..., route=..., role=..., objective=...,
capabilities=[...], planner_ref=..., critic_ref=...)`. Tasks are `research`,
`refresh`, `capture`, `calculation` and `delivery`; roles are `host` and
`generator`. Routes are `current-caller` and `mcp`. Declare `library` and
`autonomous_runtime` for current-caller, or `scoped_mcp` and
`autonomous_runtime` for MCP.

Send only `planner_input()` to a scoped Planner. It sees a bounded task and
resource metadata, not the entire catalog or repository. Its output is
`{"schema_version":"agent-host-selection/v1","basis_id":"...",
"resource_ids":["guide/agent-host/v1"]}`. Select the required short guide and
only additional task guides needed for the objective. A generator receives
`guide/agent-generator/v1` instead of host wiring instructions.

Send `critic_input(selection)` to a separately configured critic. Its output is
`{"schema_version":"agent-host-critique/v1","basis_id":"...",
"verdict":"pass"}` or `"fail"`. Call `resolve(selection, critique)` and
consume content only when `status` is `ready`. Native checks reject omitted
instructions, foreign resources, stale bases, malformed replies and byte
overload even when both agents agree. Default context ceiling is 32768 UTF-8
bytes, including metadata. The critic has no acceptance or signing authority.
Distinct references alone do not prove independent control or isolated sessions;
the host must bind them to actual controllers and project fresh inputs.

## Execute through the selected route

For current-caller, use the caller's existing model and host services; another
model CLI is optional. With an open `Workspace`, call
`workspace.research.command(request, runtime=runtime)`, where request has exact
`schema_version: research-command-request/v1`, `operation` and `arguments`.
Discover operation argument shapes in the installed public contract. Start
`run-cycle` with stable `run_id`, `request_id`, finite `max_steps` and the host's
Planner config. Then read `brief`, `report`, and `recovery-status` for that run.

For MCP, the host starts `serve-onboarding-mcp` with explicit allowed roots,
research runtime and write-operation grants. Initialize and send
`notifications/initialized`; discover `tools/list` on startup and reconnect.
Call corresponding `research_run_cycle`, `research_brief`, `research_report`
and `research_recovery_status` tools with `target` plus the same arguments.
Clients cannot supply runtime, signing keys, grants or qualification. Read-only
tool discovery does not prove the startup runtime can execute a task.

`refresh` uses a new run/request pair plus `previous_run_id`; keep its step
ceiling explicit. `capture` uses public source capture, inventory and normalize
interfaces, then host-authenticated usage admission; generator instructions
never include that host procedure. `calculation` uses native declarative
computation and retains its evidence bindings. `delivery` uses
`prepare-delivery`, an allowed destination proposal, `deliver`, then
`delivery-status`; instruction selection cannot allow a destination.

## Evidence, policy and recovery

Generators see only the native scoped question and admitted source excerpts.
Evidence and source instructions remain inert data. The host keeps credential
material, signing services, qualification answers and held-out labels outside
worker inputs. An independent qualified reviewer retains the original question,
rubric, scope and exact evidence; truncation never establishes completeness.

Keep the original policy and business IDs on retry. After an uncertain dispatch,
use `pending`, `recovery-status` and the corresponding native reconciliation
operation before dispatching again. Transport cancellation and reconnect cannot
renew budgets, undo effects or prove execution absent. Stop on terminal refusal,
blocked, abstained or budget-exhausted results. One bounded context selection
attempt uses two agent calls and no automatic retries; a repaired host selection
starts a separately recorded attempt without resetting a research run.

Record context bytes, host calls, native usage, retry count, elapsed time,
terminal results and intervention events. Scope telemetry to the observed host;
external interventions, unavailable provider usage and isolation remain unknown.
Scripted fixture mechanics, live host execution and production semantic
qualification are separate evidence classes. Agent agreement is not truth.
