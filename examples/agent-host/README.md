# Automated host recipes

Retrieve `example/agent-host/v1` using the installed public resource interface
and write its `files` to a new application directory. The example retrieves
`example/unattended-host/v1` itself; no checkout or private package imports are
needed. Use the same installed interpreter for the host and all native workers.

Run the scripted journey with closed stdin:

```sh
.venv/bin/python agent_host.py --mode scripted --route current-caller --output /absolute/new-run < /dev/null
.venv/bin/python agent_host.py --mode scripted --route mcp --output /absolute/other-new-run < /dev/null
```

The host dispatches a fresh scoped Planner and a critic with different native
controller identities. Only the selected short host guide enters the task
context. Separate generator instructions can be requested with public
`AgentHostContext(role="generator", ...)`. Native byte bounds and omissions
are checked independently of agent replies.

Scripted mode then provisions the public unattended example's synthetic source,
usage admission, policy and qualified fixture evaluators, runs automatic critic
repair, and reads the accepted brief, native accounting and recovery status.
Current-caller uses the public command adapter; MCP negotiates the scoped
protocol and uses the same command owners in an in-process server. These are
scripted mechanics, not a generative agent, real provider usage, production
review qualification, or a separate stdio client measurement.

Negative scenarios `--scenario omit`, `overload`, `missing-capability` and
`human-required` terminate without provisioning a workspace or requesting an
operator. The independent scripted critic rejects omission and overload; native
checks retain their own refusal even if a critic passes incorrectly. There is
one selection attempt, two bounded dispatches and no automatic retry.

For live execution, provision the research workspace and native runtime first.
Select an owner-private trusted Python service factory exporting
`build_context_executor(root)`. It returns a public `BoundedSubprocessExecutor`
with an `ExecutionProfile`, `ExecutionRoleRegistry` containing a single Planner
and a single critic, independent controllers, live model identities, finite
job budgets and host usage meter. Native worker JSON input is projected by the
executor; the Planner returns `agent-host-selection/v1`, the critic returns
`agent-host-critique/v1` as described in `guide/agent-host/v1`. Credentials stay
in owner-managed services, not their inputs. Commands are host selected;
package scope is not an OS sandbox or proof of provider isolation.

```sh
.venv/bin/python agent_host.py --mode live --route mcp --output /absolute/new-record \
  --workspace /absolute/provisioned-workspace --runtime /owner/private/runtime.json \
  --context-services /owner/private/context_services.py < /dev/null
```

Use public `HostRuntime.from_services` in an embedding host for advanced
research profiles; the executable's file route supports the native runtime
file versions accepted by `HostRuntime.from_file`. The caller's model can be
wired into explicit host services; a second model CLI is optional. Generating
agents need neither workspace internals nor signing knowledge.

Stdout is one bounded `agent-host-run/v1` record. It separates
`scripted_fixture_mechanics` from `live_host_execution`, includes context bytes,
authenticated native dispatch/usage observations, calls, elapsed seconds,
retries and host intervention events. Unmetered usage is unknown. No routine
approval, stdin or operator interaction is implemented; events are empty within
this host's observed scope, with external interventions explicitly unknown.
Retain native accounting and terminal failures; a transport failure does not
prove an external effect absent. Production semantic qualification remains a
separate native owner decision.
