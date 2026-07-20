# Optional MCP Server

`evidence-wiki serve-mcp --target PATH` starts a stdio Model Context Protocol
server for one research workspace. It is an optional integration surface for
MCP-speaking orchestrators; the workspace scripts and package CLI remain the
canonical contract and schema source.

For parity checks, `evidence-wiki status --target PATH --format json` matches
`scripts/workspace_status.py`, while `evidence-wiki export --target PATH
--format json` matches `scripts/export_answers.py` and the MCP
`export_answers` tool. Material fields, exit/error semantics, and containment
must agree across these surfaces; timestamps may differ between fresh reads.
All target paths are resolved to one canonical workspace identity before use,
including relative paths, spaces/Unicode, and supported symlink aliases.

The server uses the MCP `2025-06-18` stdio transport: clients launch the command
as a subprocess, send one newline-delimited JSON-RPC message per line on stdin,
and receive one JSON-RPC response per line on stdout. The server writes no
non-protocol text to stdout.

JSON-RPC requests must include an `id`. Valid ids are strings, finite numbers, or
`null`; object, array, boolean, and non-finite numeric ids are invalid and are
not echoed. Messages without an `id` are treated as notifications:
`notifications/initialized` is accepted silently, and request-style methods sent
without an `id` are ignored without executing tool calls or writing responses.

```bash
evidence-wiki serve-mcp --target /path/to/workspace
```

## Upgrade Lifecycle

`evidence-wiki upgrade` refreshes starter-managed scripts on disk. A running MCP
server does not re-exec already loaded server code, direct imports, or
module-global sibling caches after those files change.

After upgrading a workspace, restart the MCP subprocess before relying on the
refreshed tooling. This applies to both `evidence-wiki serve-mcp --target PATH`
and workspace-local `python3 scripts/serve_mcp.py` launches.

## Threat Model

`serve-mcp` assumes a trusted single-client subprocess using MCP stdio. The
client is the parent process that launched the command, and that process is
responsible for deciding which user, agent, or orchestration layer may reach the
server.

The server does not provide authentication or per-peer authorization. It must
not be bridged to TCP, HTTP, WebSocket, systemd socket activation, `socat`, or
any other network transport, and it must not be shared among untrusted peers. A
network bridge would turn the stdio subprocess into an unauthenticated workspace
API.

The exposed tools are closed-list and read/append-only, but they are still
security-sensitive. If the stdio stream is exposed to untrusted clients, those
clients could perform unauthenticated workspace reads and question
intake/log/index mutations. Deployments that need network mediation,
multi-client sharing, or per-tool authorization should add an authenticated
front end first; future hardening could include `--allow-tools` or
`--auth-token`, but those controls are not part of this server contract today.

## Tools

Each tool returns the same JSON payload as its underlying script in
`structuredContent` and repeats that JSON as a text content item for clients
that only read textual tool output.

| MCP tool | Underlying contract | Arguments |
|----------|---------------------|-----------|
| `workspace_status` | `scripts/workspace_status.py --format json` | none |
| `question_status` | `scripts/question_status.py --format json` | optional `status: string[]` |
| `query_index` | `scripts/query_index.py QUERY --format json` | `query` required; optional `scope`, `limit` capped at 100, workspace-relative `index_path` |
| `intake_questions` | `scripts/intake_questions.py --format json` | `batch` required; optional `dry_run` |
| `export_answers` | `scripts/export_answers.py --format json` | optional `status: string[]` |
| `source_requests_list` | `scripts/source_requests.py list --format json` | optional `status: ["open"|"fulfilled"]` |

## Boundary

The v1 toolset is read/append-only. `intake_questions` may create question
pages, update `index.md`, and append `log.md`, matching the question intake API.
The server does not expose question claiming, answer mutation, source request
fulfillment, source inventory, normalization, shell execution, network access,
or arbitrary file reads/writes.

Managed orchestration is outside this boundary. The server does not expose
`evidence-wiki orchestrate run`, launch Codex or Claude workers, or acquire
remote evidence. Use the package CLI for those operations, or drive the
model-neutral `orchestrate start` / `next` / `submit` / `status` protocol from
a separately authorized parent process.

MCP intake applies a transport-level batch cap before delegating to
`scripts/intake_questions.py`: if `batch.questions` has more entries than
`run.max_mcp_intake_batch_questions` (default 100), the tool returns
`INTAKE_BATCH_TOO_LARGE` and writes nothing.

Fatal workspace-script failures are returned as MCP tool results with
`isError: true` and the shared error envelope in `structuredContent`. Protocol
errors such as malformed JSON-RPC, unknown methods, or unknown tools are
returned as JSON-RPC errors.

The stdio loop bounds each request line, rejects malformed or oversized JSON,
serializes calls within the trusted single-client loop, exits cleanly at EOF,
and may be restarted against the same canonical target. Protocol stdout stays
JSON-only; diagnostic correlations and tracebacks go to stderr. An invalid
target returns `CONFIG_MISSING`/`WORKSPACE_UNREADABLE` rather than a successful
empty status document.

Within one trusted stdio session, newline-delimited calls are processed one at a
time in input order; queued calls therefore cannot overlap writes. EOF ends the
process without a protocol trailer, and a fresh process re-reads the same
canonical workspace state. Read tools and intake dry-runs do not mutate the
workspace. Applied intake is limited to question pages plus the documented
`index.md` update, `log.md` append, and the internal `.locks/log.lock`
coordination artifact. Unknown tool arguments are rejected before execution even
when a client skipped JSON Schema validation.

Linux/POSIX deterministic tests cover absolute and relative targets, spaces and
Unicode, supported symlink aliases, and fail-closed case variants. Native
Windows junction/case behavior and macOS filesystem-specific path evidence are
release-candidate obligations, not inferred from POSIX simulation.

## Minimal Handshake

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"orchestrator","version":"1.0"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}
```

Example status call:

```json
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"workspace_status","arguments":{}}}
```

Example question intake call:

```json
{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"intake_questions","arguments":{"batch":{"schema_version":"1.0","questions":[{"question":"What benchmarks matter?","priority":"high"}]},"dry_run":true}}}
```
