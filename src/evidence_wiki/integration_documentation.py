"""Canonical public integration material and bounded, inert repair proposals."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy

from ._contract import LIBRARY_API_VERSION
from ._operations import OPERATION_MATRIX_VERSION, operation_matrix
from .errors import UsageError
from .research_command_contracts import REQUEST, RESULT, schemas
from .research_command_definitions import DEFINITIONS
from .run_policy import TERMINAL_OUTCOMES

SECTIONS = {
    "journey": """Fetch `guide/integration/v1` and `example/integration-docs/v1` with public
`Onboarding.open().resource` or MCP `onboarding_resource`. Open a read-only handle
with `with Onboarding.open() as onboarding:` before retrieving resources; the
Onboarding constructor itself is not a zero-argument factory. The installed example generates
this guide, dispatches a Documentation agent and independent critic, and returns
terminal structured data. Host recipes and source provisioning are separately
available as `example/agent-host/v1` and `example/unattended-host/v1`.

Use the same installed interpreter for parent and native children. Provision the
workspace, admitted sources and an autonomous HostRuntime before research.
Generating agents receive native question/evidence projections; the host retains
credentials, signing services and held-out qualification labels.

```python
from evidence_wiki import Workspace, research_request

def run_journey(target, runtime, run_id="original"):
    with Workspace.open(target) as workspace:
        result = workspace.research.command(
            research_request("run-cycle", run_id=run_id, request_id=run_id+"-cycle", max_steps=64),
            runtime=runtime,
        )
        brief = None
        if result["status"] == "returned":
            brief = workspace.research.command(research_request("brief", run_id=run_id), runtime=runtime)
        return {"cycle": result, "brief": brief}
```

Call run_journey with the host-provisioned target and runtime. Hosts can select
HostRuntime.from_file for compatible owner-private native runtime files, or
HostRuntime.from_services for explicitly provided public services.

For current outputs, use brief with format="structured". Its lifecycle has
terminal/outcome fields; verdict is current evidence eligibility. Require a
returned accepted/partial terminal cycle and current accept/partial brief before
requesting refresh with a new run_id/request_id and the previous_run_id.
Independent installation/route trials should use separately provisioned targets.
For continued work on an existing terminal target, use the native refresh path
and retained parent identity. Preserve any provenance refusal even if an earlier
cycle returned accepted; current output eligibility is checked separately.
After an uncertain write, reconcile the original IDs instead of creating a
replacement. report max_records and recovery-status max_effects accept 1..64;
retain their paging/truncation flags. Start refresh with an explicit small step
ceiling appropriate to the selected host budget. A trusted host enforces a
finite outer deadline covering preparation and all calls, in addition to native
per-run budgets. Record its numeric bound and unknown cleanup/effect observations.

`research_request` builds and validates the exact native envelope; callers do not
author schema documents. Inspect the domain outcome even when transport status
is returned. Missing runtime, capability or incompatible policy is terminal data;
there is no stdin prompt, credential discovery or human review task.""",
    "policy": """The host selects immutable RunPolicy, roots, grants, finite usage/step/repair
ceilings and allowed effects. Native owners check actual current permissions,
source closure and policy at execution and closing boundaries. Declared refs and
tool discovery are not authority. Human-required profiles cannot be silently
converted to autonomous approval; incompatibility terminates without an operator
request. Do not reset original budgets or business IDs after a timeout.""",
    "executor": """Wire an explicit AgentExecutor with profile plus invoke, poll, cancel and
authenticate methods, and use HostRuntime.from_services(policy, context,
execution) for an embedding host. ResearchExecution wraps the selected executor,
protected qualifications and host signing/observation services. The native
BoundedSubprocessExecutor is a bounded reference adapter; host-owned commands and
usage meters remain explicit selections. Native scoped worker_document input
withholds parent correlation and keys. A receipt proves host-observed execution;
it does not prove provider identity, OS isolation, billing or semantic truth.
Unmetered usage remains unknown. A second model CLI is optional.""",
    "qualification": """Qualified evaluators need authenticated, current, purpose/domain/rubric-bound
qualification under the native policy, independent configured controllers and
complete original evidence context. Host-protected reference labels stay outside
worker inputs. Calibration, source/usage admission and review signing belong to
native host owners; generator output and agent agreement cannot qualify them.
Scripted fixture mechanics, live host execution and production semantic
qualification are separate evidence classes. The installed synthetic examples
demonstrate mechanics and cannot certify production accuracy.""",
    "effects": """Operation effects and required arguments below derive from the public shared
declarations. Research writes require both startup host grants and native policy/
source permissions. Current/retained reads may expose only permitted material;
instruction/schema discovery never admits a source or approves publication.
Prepare/deliver/reconcile use native destination and receipt owners. Reports
preserve truncation and unknowns; a bounded missing result is not proof of absence.""",
    "concurrency": """Native owners serialize protected workspace/request writes and reserve work
before releasing gates for agents. The scoped MCP stdio server has one research
worker and bounded request buffering; concurrent business work may return busy.
Use explicit original run/request IDs and retained pending/recovery status.
Cancellation is cooperative at safe checkpoints, suppresses a transport response
and preserves effects. Reconnect cannot roll back a write or renew budgets.
Resolve uncertain dispatch through native pending/cancel/reconcile operations
before starting another attempt. Hosts retain an outer finite operation deadline.""",
    "mcp": """Start serve-onboarding-mcp with allowed roots, explicit research runtime and
write-operation grants. Initialize, send notifications/initialized and discover
tools/list on startup and reconnect. Research tools accept target and declared
operation arguments; clients cannot select runtime, credentials, grants or
qualification. Build arguments with research_request, then send its arguments
plus target to the corresponding tool name below. Onboarding protocol scope and
the research runtime are selected separately. No elicitation or sampling is
required. Legacy serve-mcp remains a separate workspace protocol.""",
    "migration": """Negotiate the installed library API and operation matrix at startup; compare
the documentation basis rather than assuming package version alone identifies
the surface. Rebuild documentation when public declarations change. Existing
Workspace methods and legacy MCP retain their separate meanings. Native runtime
files support only their declared versions; use HostRuntime.from_services for
advanced public profiles instead of downgrading them. Migrate source/workspace
configuration only through explicit public plan/apply owners. Preserve native
history, authority and original budgets; missing capability is a terminal result.""",
}
MAX_BYTES = 65536


def canonical(value):
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (ValueError, TypeError, UnicodeError, RecursionError):
        invalid("document_encoding")
    if len(raw) > MAX_BYTES:
        invalid("document_bound")
    return raw


def invalid(reason):
    raise UsageError("INTEGRATION_DOCUMENTATION_INVALID", "Invalid bounded integration documentation.",
                     recoverable=False, details={"reason": reason})


class IntegrationDocumentation:
    """Derive documentation from current public declarations, never private state."""

    def __init__(self):
        boundaries = {row["operation"]: row for row in operation_matrix()["operations"]}
        self._reference = {"schema_version": "integration-reference/v1", "library_api_version": LIBRARY_API_VERSION,
            "matrix_version": OPERATION_MATRIX_VERSION, "request_schema": REQUEST, "result_schema": RESULT,
            "command_boundary": boundaries["research.command"], "terminal_outcomes": list(TERMINAL_OUTCOMES),
            "transport_statuses": schemas()[RESULT]["properties"]["status"]["enum"],
            "section_order": list(SECTIONS),
            "operations": [{"operation": name, "method": row["method"], "write": row["write"],
                "arguments": deepcopy(row["schema"]), "mcp_tool": "research_"+name.replace("-", "_")}
                for name, row in DEFINITIONS.items()], "sections": deepcopy(SECTIONS),
            "limitations": ["Declared capability is not execution authority.", "Scripted results do not qualify production semantics.",
                             "External intervention and unmetered usage remain unknown."]}
        self._basis = hashlib.sha256(canonical(self._reference)).hexdigest()

    def reference(self):
        return {**deepcopy(self._reference), "basis_id": self._basis}

    def render(self):
        """Generate exact public prose, argument reference and route mappings."""
        value = self._reference
        text = (f"# Integration journey\n\nLibrary API {value['library_api_version']}; operation matrix {value['matrix_version']}. "
                f"Documentation basis: `{self._basis}`.\n\n")
        for name, content in value["sections"].items():
            text += f"## {name.capitalize()}\n\n{content}\n\n"
        text += "## Terminal outcomes\n\n" + ", ".join(f"`{item}`" for item in value["terminal_outcomes"]) + ".\n\n"
        text += "Transport `returned` reports domain data, including negative outcomes. `refused` contains a typed error. "
        text += "Neither status grants external-action authority or evidence acceptance.\n\n"
        text += "## Operation reference\n\n| Operation | Effect | Required arguments | Optional arguments | MCP tool |\n| --- | --- | --- | --- | --- |\n"
        for row in value["operations"]:
            required = row["arguments"]["required"]
            optional = [key for key in row["arguments"]["properties"] if key not in required]
            text += f"| `{row['operation']}` | {'native write' if row['write'] else 'read'} | {', '.join(required) or 'none'} | {', '.join(optional) or 'none'} | `{row['mcp_tool']}` |\n"
        text += "\nWire argument types and bounds are in `IntegrationDocumentation().reference()`. Native owners impose additional policy, custody and budget constraints.\n"
        if len(text.encode()) > MAX_BYTES:
            invalid("render_bound")
        return text

    def proposal(self):
        """Return the canonical bounded assembly template without schema authoring."""
        return {"schema_version": "integration-documentation-proposal/v1", "basis_id": self._basis,
                "section_ids": list(SECTIONS), "operation_ids": list(DEFINITIONS)}

    def _issues(self, proposal):
        expected = {"schema_version", "basis_id", "section_ids", "operation_ids"}
        if type(proposal) is not dict or set(proposal) != expected:
            return ["proposal_shape"]
        try:
            canonical(proposal)
        except UsageError:
            return ["proposal_bound_or_encoding"]
        if proposal["schema_version"] != "integration-documentation-proposal/v1" or proposal["basis_id"] != self._basis:
            return ["basis_or_version"]
        if proposal["section_ids"] != list(SECTIONS) or proposal["operation_ids"] != list(DEFINITIONS):
            return ["contract_coverage"]
        current = IntegrationDocumentation()
        return ["declarations_changed"] if current._basis != self._basis else []

    def agent_input(self, *, purpose="documentation", proposal=None):
        if type(purpose) is not str or purpose not in {"documentation", "critic"}:
            invalid("purpose")
        result = {"schema_version": "integration-documentation-input/v1", "purpose": purpose,
            "basis_id": self._basis, "reference": self.reference(), "proposal_template": self.proposal(),
            "instructions": "Assemble only canonical public sections and operation IDs. Do not invent prose, paths, credentials or qualification. Return the supplied closed proposal template; critics return only the bounded pass/fail contract.",
            "semantic_qualification": "unknown"}
        if purpose == "critic":
            issues = self._issues(proposal)
            result.update(native_issues=issues,
                markdown=self.render() if not issues else None,
                output_contract={"schema_version": "integration-documentation-critique/v1", "basis_id": self._basis, "verdict": "pass or fail"})
        canonical(result)
        return result

    def resolve(self, proposal, critique):
        issues = self._issues(proposal)
        if (type(critique) is not dict or set(critique) != {"schema_version", "basis_id", "verdict"}
                or critique["schema_version"] != "integration-documentation-critique/v1"
                or critique["basis_id"] != self._basis or type(critique["verdict"]) is not str
                or critique["verdict"] not in {"pass", "fail"}):
            issues.append("critique_shape")
        elif critique["verdict"] != "pass":
            issues.append("critic_refused")
        return {"schema_version": "integration-documentation-result/v1", "basis_id": self._basis,
            "status": "refused" if issues else "ready", "terminal": True, "issues": issues,
            "markdown": None if issues else self.render(), "operator_request": False,
            "semantic_qualification": "unknown", "authorized": False, "evidence_accepted": False}

    def repair(self, *, failure_kind, attempt, basis_id):
        """Propose a bounded repair; no repository path, source edit or callback."""
        if (type(failure_kind) is not str or failure_kind not in {"documentation_mismatch", "integration_failure", "capability_missing"}
                or type(attempt) is not int or not 1 <= attempt <= 2 or basis_id != self._basis):
            invalid("repair_selection")
        return {"schema_version": "integration-repair-proposal/v1", "basis_id": self._basis,
            "kind": "documentation" if failure_kind == "documentation_mismatch" else "api_investigation",
            "reason": failure_kind, "attempt": attempt, "max_attempts": 2,
            "next_action": "stop_bound" if attempt == 2 else "regenerate_and_reverify",
            "proposal": self.proposal() if failure_kind == "documentation_mismatch" else None,
            "effects": "proposal_only", "operator_request": False, "authorized": False, "evidence_accepted": False}
