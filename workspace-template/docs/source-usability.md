# Inspect capabilities and usable sources

Capability inspection separates installed software, configuration, authorization,
credentials, connectivity, delivered bytes, extraction and retrieval. It does not
initialize a workspace, activate providers or establish that a source is correct.

```sh
evidence-wiki agent inspect --target /path/to/workspace
evidence-wiki agent inspect --host-tools host-tools.json
evidence-wiki agent source-status --target /path/to/workspace --source-id SOURCE_ID
evidence-wiki agent source-status --target /path/to/workspace --source-path raw/web/capture.md
evidence-wiki agent source-schemas
```

A missing target still produces an installation report. Optional absent tools
do not disable unrelated capabilities. Only explicitly selected sources are
inspected; inspection does not run inventory or normalization. Temporary private
copies support record checks. A file under `raw/` alone is not usable evidence.
Statuses distinguish missing inventory, unperformed extraction, stale originals,
invalid records, OCR needs, partial renderings and lexically indexable content.
Semantic adequacy and evidence acceptance remain separate.

## Declare host tools

Retrieve `evidence-host-tools/v1` with
`agent source-schemas --schema-id evidence-host-tools/v1`. A declaration names
the tool/version, operations, URI or workspace scope, formats, credential
references, request/byte/cost limits, authorization declaration and optional
review/isolation/transport claims. The schema is closed and bounded. It accepts
credential names, never credential values or executable commands.

The input basis is always `declared`. A tool name, installed entry point, claimed
sandbox or different reviewer name cannot prove access, independent review or
host enforcement. A checked deposited capture substantiates only its recorded
format, size and declared origin; it does not authenticate the producing tool or
prove live account access. Framework qualification is reported by observed
version/mode/platform separately from local executable presence. Pi declarations
do not imply native MCP support. Model runtimes and project extensions are not
started by declaration inspection.

### Explicit probes

Default inspection does not load registered providers or run version commands.
Metadata lists an entry point, which can have a different name from its actual
provider ID. When authorized to inspect that installed extension, select its
distribution/entry-point pair:

```sh
evidence-wiki agent inspect --probe-provider acquisition:distribution-name/entrypoint-name
evidence-wiki agent inspect --probe-tool git
```

These options execute a bounded local process. Provider probing imports only
the selected installed entry point with a credential-stripped environment and
temporary working directory; it may also validate a supplied routing request.
It does not invoke fetch/search. An audit guard rejects ordinary network,
process and write attempts; this is not a certified sandbox for hostile native
extensions. Timeout and output bounds produce an inconclusive/refused result.
Only Git and Poppler version commands have explicit OS-tool probes. Model
startup, external host tools and network connectivity remain unprobed.

## Choose routes for evidence requirements

```sh
evidence-wiki agent source-schemas --schema-id evidence-source-routes/v1
evidence-wiki agent routes --target /path/to/workspace --from-file requirements.json --host-tools host-tools.json
```

Each requirement names original question IDs, a canonical request kind, source
query or identifier, scope, requested capture format, acceptable content kinds,
completeness requirement and optional existing source IDs. The caller supplies
an optional `source_request_id` only when an open canonical request already
exists; requirement IDs do not invent request-store entries. Academic discovery
needs that binding, while unbound acquisition omits request bookkeeping.
The caller supplies
bounded budgets and tool preferences. The result accounts for every requirement
with a selected route, allowed alternatives, gaps, affected questions and work
that may continue. It never enables a provider or executes a route.

Built-in routes reuse source-request planning and existing discovery/acquisition
commands. Metadata searches identify candidates; they are not full-text captures.
Search query planning remains distinct from `search --execute`. The executing
owner must recheck current provider authorization, credentials, transport policy
and the actual request/byte/cost budget; route planning reserves nothing and does
not prove connectivity. Unknown prices do not become zero-cost observations.

Registered routes require a provider-defined JSON request, a selected installed
registration and a workspace-relative request file. `request_kinds` alone cannot
make a route executable. Supply the matching explicit `--probe-provider` option
to validate the request through its owner. The requested provider ID must match
the loaded registration, the request file must match the supplied request, and
the expected capture format needs a compatible normalizer. The execution owner
validates the request again before use.

Host routes retain caller-declared scope and limits and require the host's own
current permission/access checks. Missing credentials stay in host custody.
Format mismatches, missing source locations and budget limits remain explicit;
the library does not silently reinterpret a CSV/PDF requirement as a text capture.

## Retain a Markdown or text capture

Only explicit `evidence-host-capture/v1` captures gain the native `host_text`
normalizer. Bare Markdown keeps its existing unsupported/adapter behavior.
Capture metadata preserves tool/version, origin, retrieval time, method, content
format, content kind, completeness, scope, rights qualifications and exact byte
identity. `primary`, `excerpt`, `search_snippet` and `generated_summary` remain
distinct. Snippets and generated summaries cannot become primary source evidence;
unknown/restricted rights and empty content remain unusable.

Retrieve `evidence-host-delivery/v1`, then supply its capture metadata and the
base64 encoding of the exact original UTF-8 bytes:

```sh
evidence-wiki agent source-schemas --schema-id evidence-host-delivery/v1
evidence-wiki agent capture --target /path/to/workspace --path raw/web/capture.md --from-file capture.json --host-tools host-tools.json
```

Delivery creates new raw bytes and their `.provenance.yml` sidecar; existing
different content is never overwritten. Identical replay is reported explicitly.
The native writer requires POSIX descriptor operations. Other hosts can deposit
the same byte/provenance pair through their authorized delivery mechanism.
Use a configured raw root outside `raw/links`; the latter is for URL-list intake.
Capture declaration and byte integrity do not establish source authenticity,
license correctness, independent review or permission to bypass protected intake.

Inventory the delivered capture through the existing source-inventory command,
then normalize only the returned source ID with the selected workspace interpreter:

```sh
PYTHON WORKSPACE/scripts/source_inventory.py --project-root WORKSPACE
PYTHON WORKSPACE/scripts/normalize_sources.py --project-root WORKSPACE --source-id SOURCE_ID
evidence-wiki agent source-status --target WORKSPACE --source-id SOURCE_ID
```

Here `PYTHON` is the caller-selected environment's Python executable. Inventory
updates the manifest; selected normalization updates only that source's generated
record. Raw bytes remain unchanged. Delivery itself does neither step and does
not fulfill a source request. Use the existing request/managed-order owners for
that bookkeeping. Follow [source delivery](source-delivery.md) and
[normalized records](normalized-source-format.md) for the underlying contracts.

## Bounds and remediation

Inputs/outputs are bounded to 1 MiB, declarations to 32 tools, selections to 32
sources, routing to 16 requirements and 128 alternatives, and a text capture to
512 KiB. Source observations cap retained input bytes at 32 MiB. These bounds do
not imply absence of omitted evidence. Original-profile checks retain their
own published bounds. Inspection never broadens into renormalization.

Remediation identifies configuration, verified package dependencies, optional OS
tools, host access, source/format gaps and material missing inputs separately.
It names affected questions and work that can continue. No installation occurs
during inspection. Prefer an already available native path, such as the required
Python PDF backend, where its contract fits; use host delivery or a reviewed
adapter for unavailable formats. Obtain actual authority before changing access
or spending, and keep unverified facts explicit through strict review/export.
