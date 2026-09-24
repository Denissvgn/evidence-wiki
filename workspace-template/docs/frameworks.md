# Agent and framework integration

Use your current agent with the installed `evidence-wiki agent` guide. Read
`evidence-wiki agent frameworks` for the exact versions, modes, source commits
and observed qualification. A version string, skill match or successful process
does not establish research accuracy, isolation or controlled final delivery.
For a checked selection, use `agent frameworks --framework pi --version 0.87.0
--mode rpc_transport`; it checks the current platform. `--platform ID` explicitly
queries an observed platform instead. Unobserved versions, platforms and modes refuse.

## Portable local bundle

Preview a bundle with `evidence-wiki agent bundle`. To create a new caller-owned
directory, run `evidence-wiki agent bundle --target ASSET_DIRECTORY`. Existing
destinations refuse; global/project instructions and agent settings are never
merged or overwritten. A failed filesystem operation may leave a partial new
destination; inspect it and choose a new empty destination before retrying.
Filesystem publication requires POSIX descriptor-relative operations; unsupported
platforms can retrieve the portable JSON bundle for host-owned materialization.

The bundle contains a standards-compatible `skills/evidence-wiki/SKILL.md`, the
unchanged canonical guide as a relative reference, bounded tool schemas and an
optional Pi binding. Skill metadata retains the canonical instruction digest;
refresh the bundle when the installed guide changes. Detailed guides and schemas
are retrieved progressively through `agent resource ID`. The bundled scripts are
optional native glue; copied standalone workspaces retain their own helpers.

Select the Python interpreter that owns the installation and an explicit working
directory. A shell user can invoke the existing CLI directly; the native tool
uses the same interpreter with `-m evidence_wiki.cli`. It exposes read/check/export
operations only. Author configuration and invoke authorized mutations through
their existing owners. Neither tool metadata nor a document grants that authority.

## Bounded native tool mapping

Retrieve `evidence-framework-call/v1` and `evidence-framework-result/v1` from the
resource catalog. A call names its request ID, canonical instruction digest,
closed operation and bounded parameters. The host supplies workspace scope;
model arguments cannot change the working directory, interpreter or arbitrary
command line. Invoke with `evidence-wiki agent invoke --target WORKSPACE`, passing
one call document on standard input. The tool result retains the exact canonical
JSON text, digest and exit status; finite Decimal strings are not coerced to binary
floats. Invalid, incomplete and refused results stay distinguishable.

Only strict export can report eligible claims, through its existing owner. Other
successful operations do not mean evidence was accepted. The native wrapper does
not sign a review, change a policy or create a second receipt store. Its result
always has `host_enforced: false`. Keep evidence gaps, missing credentials,
contradictions and unqualified captures explicit.

## Pi

The matrix pins Pi's actual CLI, skill and RPC behavior separately. Pi supports
skills and executable extensions; native MCP is absent in this qualified version.
An optional extension can add MCP, but its presence is a different configuration
requiring separate qualification. Pi is an external host here, not a registered
EvidenceWiki managed runner.

Pi accepts some skill metadata that the portable standard rejects. EvidenceWiki
requires the skill's name to match its directory, strict metadata types and valid
relative references. Same-name skills can collide: select one bundle and inspect
Pi's discovery diagnostics. Explicit `/skill:evidence-wiki` invocation is useful
when automatic matching is absent. A model can still fail to follow a loaded skill.

For optional native tooling, set `EVIDENCE_WIKI_PYTHON` to the caller-selected
absolute interpreter path and select the bundle explicitly:

```sh
pi --no-approve --no-extensions --no-skills \
  --skill ASSET_DIRECTORY/skills/evidence-wiki \
  --extension ASSET_DIRECTORY/pi/evidence-wiki.js
```

This command ignores project-local executable resources for the run; it does not
save trust. Review existing user/global resources separately. The current Pi
version can load context files before project trust is granted, and explicitly
selected extensions can execute during startup. `--offline` disables startup
network activity, not model requests. The qualification uses an isolated
`PI_CODING_AGENT_DIR` and explicitly disables unrelated discovery. Do not change
user trust choices or install extensions implicitly. A duplicate native tool name
refuses rather than overriding another tool.

The tool binds to the current session's working directory and guide identity.
After compaction, retrieve current status/evidence and retain original question,
request, run and claim IDs. After resume or cwd replacement, requalify the selected
workspace and bundle. Conversation summaries are not canonical state and cannot
authorize replay of an interrupted mutation.

## Optional Pi process bridge

`evidence_wiki.pi_bridge.PiRpcBridge` accepts an explicitly selected executable,
cwd, host-owned state directory, authority reference, provider and model. It
probes the exact supported version and launches an ephemeral RPC session with
startup networking and unrelated resource discovery disabled. Node and Pi are
optional; ordinary EvidenceWiki research and computation do not import them.

The bridge uses LF-only JSONL, bounded frames/streams/events, request correlation
and a fixed session/cwd binding. Unicode line separators remain inside JSON data.
Prompt acceptance is not completion. It waits for `agent_settled` and checks idle
state; `agent_end` can precede retry, compaction or queued work. Asynchronous tool,
extension and model failures do not become successful evidence receipts.
When an acknowledgement was not observed, `prompt_accepted` is `null`; this must
not be interpreted as proof that no operation ran. Frames are bounded to 1 MiB,
the session stream to 16 MiB/4,096 frames, and each prompt to 64 KiB/600 seconds.

Cancellation clears queued work before abort, then closes the process group.
Timeout, EOF, malformed frames or replaced session/cwd invalidate the transport.
Restart creates a new process and never replays a prompt. Reconcile canonical
state before explicitly resubmitting work. Transport results contain bounded
status/correlation information, not raw session text, credentials or trusted
review assertions. POSIX process-group behavior is implemented; other bridge
platforms refuse. Deliberately escaped processes are not contained by this route.

For Node embedding, Pi's `createAgentSession` / `AgentSessionRuntime` are the
optional SDK seam. The host owns `SessionManager`, resource loaders, credentials,
session shutdown and replacement; preserve the same EvidenceWiki tool and guide
contracts. Do not copy the core validators into an SDK adapter.

## OpenCode and Gemini CLI

Use the same portable skill and canonical CLI, retaining each host's consent and
resource precedence. OpenCode can read a selected skill path through its config
and offers a headless JSON route plus HTTP/SDK integration. Gemini has headless
JSON/JSONL and tiered skills, with workspace and alias precedence. Its skill
activation consent remains host-owned. Optional MCP configuration is separate
from terminal/skill support. Local recipes and exact startup controls are recorded
in the matrix's pinned official documentation; no global installation is required.

The observed conformance modes use real pinned harness processes and bounded
independent fixtures. Local provider fixtures do not qualify arbitrary live models,
provider credentials, automatic instruction following or hostile-code isolation.
Unobserved modes remain untested, not successful by declaration. Changes to any
version, model/tool configuration, platform or permission mode require requalification.

Protected strict delivery remains owned by `StrictResearchHost` and strict export.
The current parent owner refuses protected-evidence intake and the strict host
refuses parent work orders. No framework binding upgrades that combination.

## Optional native instruction installation

`agent extensions` describes `evidence-native-instructions-request/v1`. Name
`framework`, its exact qualified `version`, `scope` (`project` or `user`) and an
existing canonical `root`. Use `agent instructions-plan --from-file REQUEST
--output PLAN`, then `agent instructions-apply --from-file PLAN`. Installation
copies the canonical portable skill and license; package installation alone
never performs this action.

| Framework | Project path below root | User path below root |
| --- | --- | --- |
| Pi | `.pi/skills/evidence-wiki` | `.pi/agent/skills/evidence-wiki` |
| OpenCode | `.opencode/skills/evidence-wiki` | `.config/opencode/skills/evidence-wiki` |
| Gemini CLI | `.gemini/skills/evidence-wiki` | `.gemini/skills/evidence-wiki` |

Explicitly select the intended home directory for user scope. Existing directories
and edited managed files are preserved with a conflict. Partial owned installs
can resume from the same plan. `agent instructions-remove --from-file PLAN`
moves an unchanged managed directory to `.evidence-wiki/instruction-archives/PLAN_ID`
below the selected root for inspection/manual restoration. It leaves existing
`AGENTS.md`, settings and other skills untouched.

Refresh the selected host's discovery and inspect name collisions/precedence.
Installation does not grant project trust or prove a model followed instructions.
Keep terminal bootstrap and the canonical guide available independently. Locations
follow [Pi skills](https://github.com/earendil-works/pi/blob/16787ad5b2dc748047f314ca1bfe7708f30f54f3/packages/coding-agent/docs/skills.md),
[OpenCode skills](https://opencode.ai/docs/skills/) and
[Gemini skills](https://geminicli.com/docs/cli/skills/).
