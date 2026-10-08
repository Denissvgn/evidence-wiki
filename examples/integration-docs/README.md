# Generated integration documentation

Fetch `example/integration-docs/v1` with public `Onboarding.resource` and write
its `files` to a new application directory. Fetch `guide/integration/v1` for
the short research journey and operation reference. No source checkout or
private helper access is required.

```sh
.venv/bin/python documentation_host.py --mode scripted --output /absolute/new-docs < /dev/null
.venv/bin/python documentation_host.py --mode scripted --scenario repair --output /absolute/new-repair < /dev/null
```

The Documentation agent uses the existing scoped integration role to assemble
sections and examples from `IntegrationDocumentation().reference()`. A critic
with another native controller reviews the complete canonical document and
declaration coverage. Native validation rejects invented sections/operations,
missing coverage, stale bases and malformed replies even if the critic agrees.
Only canonical owner-rendered prose can be released. This bounds model errors;
agent agreement does not certify semantic or runtime correctness.

Reproducible document failures produce inert `integration-repair-proposal/v1`
records. The host permits at most two assembly/reverification attempts under
one sixty-second deadline. `--scenario always-fail` demonstrates bounded terminal
refusal. API integration failures and missing capabilities can be recorded with
public `repair(failure_kind=..., attempt=..., basis_id=...)`; their proposals
select API investigation and do not execute a patch, path or callback.

For live generation, select an owner-private trusted factory exporting
`build_context_executor(root)`. Return a native `BoundedSubprocessExecutor` with
one `integration` role permitting `model`/`public_contract.read`, one independent
`critic` role permitting `model`, distinct controllers, finite budgets and a
host usage meter. Native scopes deliver projected public input; credentials
stay in host-managed services. Live model identity declarations alone do not
prove actual provider execution or OS isolation.

```sh
.venv/bin/python documentation_host.py --mode live --output /absolute/new-live-docs \
  --services /owner/private/documentation_services.py < /dev/null
```

Stdout contains one bounded record with result, context sizes, authenticated
native dispatch usage, repairs, retries, runtime and scoped intervention events.
Scripted documentation mechanics and live host execution are separate evidence
classes; external interventions and unmetered usage remain unknown. This host
does not publish a document or change a workspace. Follow the generated journey
with explicitly provisioned runtime/services; structured requests are built by
public `research_request` rather than manual schema authoring.
