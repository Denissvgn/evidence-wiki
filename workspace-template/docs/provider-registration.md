# Provider Registration

This document defines how a pip-installed Python distribution adds an
acquisition or discovery provider to a research workspace, and states exactly
which parts of that provider's declaration the package **enforces**, which it
**validates once at registration**, and which it only **records**. It is written
for two readers: the engineer packaging a provider, who needs to know what to
put in `pyproject.toml` and what the package will do on their behalf, and the
auditor reading a workspace, who needs to know which guarantees survive contact
with third-party code and which are only claims.

The built-in provider IDs are a closed set — `arxiv`, `openalex`, `github`, and
`web` for acquisition; `arxiv`, `openalex`, `github`, `search`, `standards`, and
the `standards:*` routes for discovery. A workspace whose evidence is market
data, marketplace listings, or instrument output has no built-in that fits, and
forking the starter scripts to add one discards every transport, provenance,
budget, and quarantine guarantee those scripts carry. Registration opens the ID
space without opening the enforcement boundary: a registered provider plans
requests and interprets responses, and the package's own transport still
performs every fetch.

## Registration Makes A Provider Available, Never Enabled

Registration is **packaging metadata**. Installing a distribution that declares
an entry point makes its provider ID *available* to a workspace: the ID stops
being unknown, `evidence-wiki doctor` lists it, and the validators will accept
it in a provider list. That is all installation does.

Authorization is unchanged and lives where it has always lived — in
`research.yml`:

```yaml
integrations:
  acquisition:
    enabled: true
    providers:
      - keepa
    max_downloads_per_run: 10
```

Without `integrations.acquisition.enabled: true` no command touches the network,
and without `keepa` in `integrations.acquisition.providers` a `keepa` fetch
refuses with `ACQUISITION_PROVIDER_DISABLED` — the byte-identical envelope a
disabled built-in produces. Installing a distribution never enables acquisition,
never adds a provider to an allow-list, and never widens an existing one. The
explicit-network-authorization property described in
[acquisition.md](acquisition.md) is preserved exactly; registration only decides
which IDs an operator is *allowed to write down*.

The two states are therefore independent, and both doctor and the failure modes
below name them separately:

| Installed | Authorized in `research.yml` | Result |
|---|---|---|
| no | no | The ID is unknown. Every validator refuses it as it always has. |
| yes | no | *Available, not enabled.* Doctor lists the provider; commands refuse with `ACQUISITION_PROVIDER_DISABLED`. |
| no | yes | *Authorized but unsatisfiable.* Smoke validation fails with `PROVIDER_NOT_REGISTERED` (see [Failure Modes](#failure-modes)). |
| yes | yes | Enabled. `registered get` may run, under the workspace's own budgets. |

With no provider entry points installed anywhere in the environment, behavior
is bit-for-bit identical to a workspace that has never heard of registration.
The built-in lists are the whole universe, and every existing error message
still lists that closed set verbatim.

## The Planner/Executor Split

A registered provider is a **request planner and response interpreter**, not a
fetcher. One acquisition proceeds in four steps:

1. The package loads the request document, hands it to the provider's
   `validate_request`, and refuses the command if the provider rejects it.
2. The provider's `plan_fetch` returns a bounded tuple of `PlannedRequest`
   values — URL, method, headers, optional body — describing what it *wants*
   fetched. Credentials appear in that plan only as placeholders, never as
   values.
3. **The package executes the plan.** Each request goes through the same pinned
   HTTPS transport the built-in providers use: `allowed_domains` checked against
   the provider's own declaration, DNS pinning, public-address enforcement,
   bounded download size, and diagnostic redaction. A request the declaration
   does not cover is refused before any socket work.
4. The provider's `interpret` turns the responses into a `SourceArtifact`
   descriptor, and **the package writes it** — provenance sidecar first, then
   the artifact bytes, atomically, with the same `.acquisition-incomplete.json`
   quarantine discipline every built-in acquisition already uses.

The provider never opens a socket, never holds a credential value, and never
writes a file into the workspace.

### What The Boundary Does And Does Not Guarantee

Be precise here, because the value of this document is that an auditor can trust
it.

**Installing a Python distribution is arbitrary code execution.** A registered
provider runs in the same interpreter as the workspace scripts that call it. A
*malicious* plugin can import `socket` and open its own connection to anywhere,
and no in-process design can stop it. Any claim that the package "sandboxes"
third-party providers would be false, and this document does not make it.

The enforceable claim is narrower and worth stating exactly:

> Every fetch performed **through the package's acquisition flow** is executed
> by the package's transport and checked against the provider's declaration, and
> nothing enters the workspace except through the package's writer.

Concretely, that means: an artifact under `raw/` acquired via `registered get`
was fetched from a host the provider declared, within the declared rate limit
and the workspace's own download budget, and carries a sidecar recording the
declaration that authorized it. Bytes a plugin obtained some other way are not
acquisition output — they cannot reach `raw/`, cannot be inventoried, and cannot
be cited, because the only writer is the package's own.

This is the trust posture the workspace already applies to normalizer adapters
(authorized code, untrusted output, fail-closed, bounded), with one improvement:
the network is no longer the plugin's own affair. The mitigation for a hostile
distribution is the same as for any other dependency — review what you install,
pin it, and read the capability declaration doctor prints.

### `registered get` Is Not Delegated Acquisition

The two are easy to confuse and are opposites. `registered get` is
**workspace-side** acquisition: the fetch happens inside the workspace's own
process, through the package's transport, under the package's budgets, and the
package writes the sidecar. Delegated acquisition
(`orchestration.acquisition: delegated`, see
[orchestrator-handoff.md](orchestrator-handoff.md)) is **host-side**: the
workspace issues a work order and an external acquirer fetches out of process,
then delivers files and sidecars back through the manual delivery contract. A
registered provider is not an external acquirer, and a delegated workspace does
not need one.

## The Authoring Contract

### Entry Point Groups

A distribution registers providers through `importlib.metadata` entry points in
two groups:

```toml
[project.entry-points."evidence_wiki.acquisition_providers"]
keepa = "autoseller_connectors.evidence_wiki:KeepaProvider"

[project.entry-points."evidence_wiki.discovery_providers"]
keepa = "autoseller_connectors.evidence_wiki:KeepaDiscoveryProvider"
```

The entry-point *name* is not the provider ID; the class's own `id` attribute
is, and a mismatch is a registration error rather than a silent rename. One
distribution may register any number of providers, and the same class may be
registered in both groups when it implements both phases.

Provider IDs match `^[a-z0-9][a-z0-9._-]*$` and may not collide with a built-in
acquisition or discovery ID, with the reserved `standards:*` family, or with the
legacy discovery strategy IDs. Built-in IDs stay reserved and unprefixed;
third-party IDs need no namespace prefix but can never shadow one. If two
installed distributions claim the same ID, **both** registrations are invalid —
deterministically, because first-wins would make a workspace's behavior depend
on installation order — and doctor names both distributions.

### The Class Shape

The authoring types live in `evidence_wiki.providers` and are public API:

```python
from evidence_wiki.providers import (
    PROVIDER_API_VERSION,     # 1
    MAX_PLANNED_REQUESTS,     # 8
    AcquisitionProvider,
    DiscoveryProvider,
    PlannedRequest,
    ProviderCapabilities,
    RateLimit,
    SourceArtifact,
)
```

Subclassing `AcquisitionProvider` or `DiscoveryProvider` is a **convenience, not
the contract**. Registration validates loaded objects *structurally* — the
attributes, shapes, and value rules below — never with `isinstance` against the
package's classes. A provider written without `evidence_wiki` installed but
matching the shape is valid; a provider that subclasses the base class and then
violates a rule is not. This is deliberate: workspace scripts are standalone and
never import the package, so a deployed workspace and an installed package can
be at different versions without the registration silently changing meaning.
`provider_api_version` is the version gate; v1 is `1`, and anything else is
refused rather than guessed at.

Every provider declares three attributes:

| Attribute | Meaning |
|---|---|
| `id` | The provider ID an operator writes in `research.yml`. |
| `provider_api_version` | `1` for this contract. |
| `capabilities` | A `ProviderCapabilities` value (or a structurally identical object). |

An acquisition provider implements three methods:

| Method | Signature | Contract |
|---|---|---|
| `validate_request` | `(request) -> None` | Raise to refuse a request document the provider does not understand. The raised message is carried into the `PROVIDER_REQUEST_INVALID` envelope's detail, so write it for an operator. |
| `plan_fetch` | `(request) -> tuple[PlannedRequest, ...]` | Return at most `MAX_PLANNED_REQUESTS` (8) planned requests. One command produces one artifact; a plan is not a crawl. |
| `interpret` | `(request, responses) -> SourceArtifact` | Turn the executed responses — delivered in plan order — into one artifact descriptor. Pure: it must not perform I/O of its own. |

A discovery provider implements `validate_request`, `plan_search`, and
`interpret_candidates`, where `interpret_candidates` returns mappings matching
the `source_candidate` shape documented in
[source-discovery.md](source-discovery.md). Discovery is read-only: it proposes
candidates and never delivers evidence.

`PlannedRequest` carries `url` (`https://` only), `method` (`GET` or `POST`),
`headers`, a `body` permitted only on `POST`, and an advisory `timeout_hint`.
`SourceArtifact` carries `filename` (a bare name with no path separators — the
package chooses the directory under the resolved target root), `source_type`
from the delivery contract's vocabulary, the artifact `content` bytes,
`provenance_metadata` (a JSON-safe mapping merged into the sidecar under a
provider-namespaced key), and `warnings` — non-fatal observations recorded with
the artifact, which is where a provider says something like "this API states no
reuse license" rather than swallowing it.

### Declared Capabilities

`ProviderCapabilities` is the declaration the package checks the provider
against. Every field is either enforced at run time, validated once when the
registration loads, or recorded in provenance and surfaced by doctor — and the
table says which, per field, because "declared" and "enforced" are not the same
promise.

| Field | v1 semantics | Enforcement |
|---|---|---|
| `allowed_domains` | Non-empty tuple of bare lowercase hostnames — no scheme, no path, no port, the same shape rule the `web` provider's configured allow-list already uses. Every planned request's host must match an entry, with the same subdomain semantics the built-ins get from `validate_https_url`. | **Enforced** at transport, before DNS or socket work. A host outside the declaration raises `ACQUISITION_DOMAIN_NOT_DECLARED`. An empty or missing tuple makes the registration invalid. |
| `terms_urls` | Non-empty tuple. Each entry is an `https://` URL naming the provider's terms of service or API terms, or the literal string `per-origin` for a provider whose terms vary by fetched origin (the `web` provider's posture). | **Recorded** in provenance and printed by doctor. The package does not read the terms; an operator does. |
| `license_inference` | Exactly one of `yes`, `partial`, or `none`, with the meanings the built-in provider registry table in [acquisition.md](acquisition.md) already uses. | **Recorded** in provenance and printed by doctor. A `none` declaration does not stop acquisition; it tells the operator the sidecar's license status will need review before publication. |
| `captures_raw` | Whether raw bytes are retained as evidence. Defaults to `True` and **must** be `True` in v1. | **Validated at registration** and recorded. The package's writer always retains raw bytes, so a `False` declaration is a contract the package would not honor; it is refused rather than ignored. |
| `quarantine_on_incomplete` | Whether an interrupted acquisition quarantines rather than publishing a partial artifact. Defaults to `True` and **must** be `True` in v1. | **Validated at registration** and recorded, for the same reason: the writer quarantines unconditionally. |
| `rate_limit` | `RateLimit(requests, per)` with `requests >= 1` and `per` either `"minute"` or `"hour"`, or `None` for no declared ceiling. | **Enforced** against a durable per-run ledger. The declared window is checked before transport; exceeding it raises `ACQUISITION_PROVIDER_RATE_LIMITED`. A declared limit tightens, never loosens, `integrations.acquisition.max_downloads_per_run`. |
| `credentials` | Tuple of **environment variable names only**, each matching `^[A-Z][A-Z0-9_]*$`. Defaults to `()`. Never values. | **Enforced**: a plan may reference only declared names, and the package resolves the values. The *names* are recorded in provenance; the values never appear in any output. |
| `request_kinds` | Tuple of source-request kind IDs this provider can serve — a built-in kind such as `structured_data`, or a pack-namespaced kind such as `pack:market-data/price_history`. Defaults to `()`. | **Recorded** and printed by doctor. v1 validates the shape but does not route work orders on it (see [v1 Limits](#v1-limits)). |

### A Worked Example

A complete acquisition provider. The `pyproject.toml` fragment:

```toml
[project]
name = "autoseller-connectors"
version = "0.3.0"

[project.entry-points."evidence_wiki.acquisition_providers"]
keepa = "autoseller_connectors.evidence_wiki:KeepaProvider"
```

And `autoseller_connectors/evidence_wiki.py`:

```python
from evidence_wiki.providers import (
    AcquisitionProvider,
    PlannedRequest,
    ProviderCapabilities,
    RateLimit,
    SourceArtifact,
)


class KeepaProvider(AcquisitionProvider):
    id = "keepa"
    provider_api_version = 1
    capabilities = ProviderCapabilities(
        allowed_domains=("api.keepa.com",),
        terms_urls=("https://keepa.com/#!api",),
        license_inference="none",
        captures_raw=True,
        quarantine_on_incomplete=True,
        rate_limit=RateLimit(60, per="minute"),
        credentials=("KEEPA_API_KEY",),
        request_kinds=("structured_data",),
    )

    def validate_request(self, request):
        asin = request.get("asin")
        if not isinstance(asin, str) or not asin.isalnum():
            raise ValueError("keepa requests need an alphanumeric 'asin' string")

    def plan_fetch(self, request):
        return (
            PlannedRequest(
                url=f"https://api.keepa.com/product?domain=1&asin={request['asin']}",
                method="GET",
                headers=(("X-Api-Key", "{{credential:KEEPA_API_KEY}}"),),
                timeout_hint=30,
            ),
        )

    def interpret(self, request, responses):
        return SourceArtifact(
            filename=f"keepa-{request['asin']}.json",
            source_type="dataset",
            content=responses[0].content,
            provenance_metadata={"asin": request["asin"], "keepa_domain": 1},
            warnings=("Keepa does not state a reuse license; review before publication.",),
        )
```

Install the distribution, authorize `keepa` in `research.yml`, export
`KEEPA_API_KEY` for the one run that needs it, and fetch:

```bash
printf '{"asin": "B0ABC12345"}' > /tmp/keepa-request.json
python3 scripts/fetch_sources.py --format json registered get \
  --id keepa \
  --request-file /tmp/keepa-request.json \
  --target-root raw/data
```

`--target-root` defaults to `raw/data` and must stay under `raw/`, exactly as
for a built-in provider; add it to `raw.source_roots` so inventory records what
lands there. Discovery mirrors the same shape, read-only:

```bash
python3 scripts/discover_sources.py --format json registered search \
  --id keepa \
  --request-file /tmp/keepa-search.json
```

The subcommand word is `registered` and the ID flag is `--id`. The built-in
provider subcommand tree already stores the subcommand name in `provider`, so an
option named `--provider` inside this subparser would silently overwrite it;
`--id` is the flag `arxiv download` already uses.

### What The Sidecar Records

An artifact acquired through a registered provider carries two blocks in its
`.provenance.yml` sidecar, beside the usual `origin_url`, `retrieved_at`,
`retrieved_by`, `license`, and `checksum` fields described in
[acquisition.md](acquisition.md):

```yaml
provider_registration:
  id: keepa
  phase: acquisition
  distribution: autoseller-connectors
  version: "0.3.0"
  entry_point: keepa
  provider_api_version: 1
provider_capabilities:
  allowed_domains: [api.keepa.com]
  terms_urls: ["https://keepa.com/#!api"]
  license_inference: none
  captures_raw: true
  quarantine_on_incomplete: true
  rate_limit: {requests: 60, per: minute}
  credentials: [KEEPA_API_KEY]
  request_kinds: [structured_data]
```

This is what makes the record auditable after the fact. `provider_registration`
answers *which installed code produced this*, down to the distribution version
and entry-point name; `provider_capabilities` answers *what did it claim it
could reach, and under what limits*. Registered candidate records written by
`registered search` carry the same identity, so a candidate's origin is
traceable before anything is fetched. `credentials` lists names only — a
sidecar never carries a secret value.

## Credential Custody

The design rule is that **the plugin never holds a secret**.

- A provider declares credential **names** in `capabilities.credentials`. Names
  only, matching `^[A-Z][A-Z0-9_]*$`. A declaration containing anything that
  looks like a value is a registration error, not a warning.
- A plan references a credential with the placeholder syntax
  `{{credential:NAME}}` — matching `\{\{credential:([A-Z][A-Z0-9_]*)\}\}`.
- Placeholders are valid in **header values only, never in URLs**. This is not a
  stylistic rule: URLs are redacted for logging and diagnostics by a URL-aware
  redactor, and a credential smuggled into a query string would survive that
  redaction and leak into error text, logs, and the sidecar's `origin_url`. A
  placeholder anywhere in a planned URL is refused with `PROVIDER_PLAN_INVALID`.
- The package resolves each placeholder at execution time from the process
  environment, registers the resolved value with the redaction machinery for the
  lifetime of the command, and substitutes it into the outgoing header. A name
  the provider did not declare is refused; a declared name whose variable is
  unset is refused by name rather than sent as an empty header.

Operationally this means the same rules the built-in providers already follow
apply unchanged, and are documented in *Provider Secrets And Rotation* in
[acquisition.md](acquisition.md): inject the variable into the process
environment for the one run that needs it, keep it out of `research.yml`,
sidecars, run reports, logs, and wiki pages, and rotate it through the issuing
service when a `.env`, shell history, or shared workspace may have exposed it.
`scripts/doctor.py` includes declared credential names among the variables it
warns about finding in a readable repo-root `.env`, and never prints their
values.

## Failure Modes

Six stable error codes belong to registration. They are also listed in the
stable error-code table in [orchestrator-handoff.md](orchestrator-handoff.md);
hosts switch on them, so the spellings never change.

| Code | Fires when | What the operator does |
|---|---|---|
| `PROVIDER_NOT_REGISTERED` | A configured or requested ID is not a built-in and no valid registration supplies it. | Read the remediation: it distinguishes *not installed* from *installed but invalid*. For the first, `pip install` the distribution into the environment that runs the workspace. For the second, fix or remove the broken registration. |
| `PROVIDER_REGISTRATION_INVALID` | An entry point imports, but its declaration violates the contract — bad ID syntax, collision with a reserved or duplicate ID, empty `allowed_domains`, a malformed credential name, an unsupported `provider_api_version`, or `captures_raw`/`quarantine_on_incomplete` not `True`. | The detail names the distribution and every violation. Fix the provider package, or uninstall it; a broken registration is never partially honored. |
| `PROVIDER_REQUEST_INVALID` | The `--request-file` document is not a single JSON object, or the provider's own `validate_request` refused it. | The provider's message is carried in the detail. Fix the request document to what that provider documents. |
| `PROVIDER_PLAN_INVALID` | The provider returned a plan the envelope refuses: a non-`https` or unparseable URL, a credential placeholder in a URL, an undeclared credential name, more than `MAX_PLANNED_REQUESTS` (8) requests, or a malformed `PlannedRequest`/`SourceArtifact`. | This is a provider bug, not an operator mistake. Report it to the distribution's maintainer with the envelope. Nothing was fetched and nothing was written. |
| `ACQUISITION_DOMAIN_NOT_DECLARED` | A planned request's host is outside the provider's declared `allowed_domains`. | Nothing was fetched. Either the provider must declare the host (a new release of the distribution) or the request is out of scope for it. The code is named after what it proves: the declaration, not convention, is the boundary. |
| `ACQUISITION_PROVIDER_RATE_LIMITED` | Executing the plan would exceed the provider's declared `rate_limit` for the accounting window, or the run's `max_downloads_per_run`. | The remediation states the window and when it clears. Wait, or start a new run; do not edit the ledger. |

Existing codes are reused unchanged, so a registered provider refuses exactly
like a built-in one where the condition is the same: `ACQUISITION_DISABLED` when
acquisition is off, `ACQUISITION_PROVIDER_DISABLED` when the ID is not in
`integrations.acquisition.providers`, `ACQUISITION_LIMIT_EXCEEDED`,
`ACQUISITION_CONTENT_TOO_LARGE`, the `ACQUISITION_RUN_*` family, and every
transport error the built-ins already map.

### Authorized But Not Installed

The one case worth calling out is a `research.yml` that authorizes `keepa` in an
environment where the distribution is not installed — a fresh virtualenv, a
deploy that skipped a dependency, a container built from a stale lockfile.

**This is a smoke failure.** `scripts/smoke_validate_workspace.py` reports
`PROVIDER_NOT_REGISTERED`, which flips `workspace_status` and stops the
orchestration controller before any session starts. It is not downgraded to a
warning and there is no lint finding for it.

The reasoning is worth understanding, because it is what keeps registered
providers as honest as built-in ones. An authorization the environment cannot
satisfy is deploy drift on an *authorization boundary*, and every provider-list
error in this workspace is already smoke-fatal. Carving out a softer path for
exactly the third-party case would make a registered provider *less* strictly
handled than a built-in one, which inverts the point of the contract. Lint
deliberately says nothing: a lint finding is a pure function of the workspace
tree, and whether a distribution is installed is a property of the environment,
so a lint check would make two lints of the same tree in two virtualenvs
disagree. Smoke is the "safe to operate *here*" gate, doctor is the environment
explainer, and lint stays deterministic.

The remediation names the missing distribution. The fix is either `pip install`
into the environment that runs the workspace, or removing the authorization from
`research.yml`. Both are correct; choose the one that matches intent.

### Seeing What Is Registered

`scripts/doctor.py` reports a **Registered providers** section. For each valid
registration it prints the ID, distribution and version, the phase or phases it
registered for, a capability summary (declared domains, rate limit, credential
*names*, request kinds), and whether `research.yml` authorizes it — the
available-versus-enabled distinction, made visible. Each invalid registration is
printed with its distribution and the reason it was refused: a registration that
fails to import or validate never crashes enumeration and never silently
disappears.

```bash
python3 scripts/doctor.py --format json
```

This is the command to run when a provider "isn't working": it distinguishes not
installed, installed but invalid, installed but unauthorized, and installed and
enabled, which are four different problems with four different fixes.

## v1 Limits

These are deliberate boundaries, not oversights. Each is deferred honestly
rather than smuggled in as plugin-owned I/O.

- **No request signing, no OAuth refresh, no non-header credential transport.**
  v1 serves APIs that authenticate with a key in a request header. Signing
  schemes such as AWS SigV4 and OAuth token-refresh flows need either a
  plugin-held secret (which breaks credential custody) or package-side signing
  algorithms (a real design, not yet built). A provider that requires them does
  not fit v1.
- **No subprocess isolation of provider code.** Provider code runs in the
  workspace's interpreter. Isolation was considered and deferred: the
  planner/executor protocol would need a bidirectional multi-round pipe
  contract, and in-process planning with package-executed transport captures
  most of the value at a fraction of the surface. See
  [What The Boundary Does And Does Not Guarantee](#what-the-boundary-does-and-does-not-guarantee)
  for what this does and does not mean.
- **`request_kinds` is recorded, not routed.** The field is declared, validated
  for shape, written to provenance, and shown by doctor, but nothing selects a
  provider for a work order based on it yet. Acquisition is still driven by an
  explicit command.
- **No configuration-file registration.** A provider cannot be declared in
  `research.yml` without a Python distribution. Registration is packaging
  metadata by design; configuration declares *authorization*, never *existence*.
  Keeping those two in separate files is what makes the available-versus-enabled
  distinction checkable.
- **No built-in behavior changes.** Built-in providers, their subcommand trees,
  and every existing error envelope are untouched. A workspace with no plugins
  installed behaves exactly as it did before this contract existed.

## Related Documents

- [acquisition.md](acquisition.md) defines the acquisition safety model, the
  built-in provider registry, target roots, and the provenance sidecar contract
  that registered acquisitions write into.
- [research-yml.md](research-yml.md) documents `integrations.acquisition` and
  `integrations.discovery`, the blocks that authorize a provider ID.
- [source-discovery.md](source-discovery.md) defines the `source_candidate`
  schema and the trust policy that `registered search` output passes through.
- [source-delivery.md](source-delivery.md) defines target roots, atomic
  delivery, and the `.provenance.yml` contract shared by every delivery path.
- [orchestrator-handoff.md](orchestrator-handoff.md) carries the stable error
  codes and the delegated-acquisition contract that `registered get` is distinct
  from.
- [prompt-injection-hardening.md](prompt-injection-hardening.md) applies
  unchanged to registered providers: a provider's output is evidence data and
  its warnings are observations, never instructions.
