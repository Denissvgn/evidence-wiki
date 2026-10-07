"""Finite research tool declarations; only the host selects runtime and grants."""

from copy import deepcopy

from .research_command_definitions import DEFINITIONS, READ_OPERATIONS, WRITE_OPERATIONS

CAPABILITY = "scoped-research-tools/v1"


def manifest(allowed=READ_OPERATIONS):
    result = []
    for operation, row in DEFINITIONS.items():
        if operation not in allowed:
            continue
        schema = deepcopy(row["schema"])
        schema["properties"]["target"] = {"type": "string", "minLength": 1, "maxLength": 4096}
        schema["required"].insert(0, "target")
        effect = "Explicit canonical writes through the frozen host policy" if row["write"] else "Bounded current/retained reads"
        result.append({"name": "research_" + operation.replace("-", "_"), "inputSchema": schema,
            "description": f"{operation}: {effect}. Uses public research.command; no client-selected runtime, credentials, grants or qualification. No elicitation or routine approval."})
    return result


def contract():
    return {"capability": CAPABILITY, "method": "research.command", "default_grants": sorted(READ_OPERATIONS),
        "mutation_grants": sorted(WRITE_OPERATIONS), "tools": manifest(READ_OPERATIONS | WRITE_OPERATIONS),
        "runtime": "startup-selected HostRuntime services or private runtime file; never tool arguments",
        "reconnect": "same roots, runtime identities and business request IDs; retained native budgets never reset",
        "cancellation": "cooperative safe checkpoints; transport cancellation suppresses response, never rolls back effects; pending/cancel/reconcile tools retain native custody",
        "interactivity": "no sampling, elicitation, credential provisioning or per-call signoff"}
