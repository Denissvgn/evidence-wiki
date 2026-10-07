"""Transport-independent dispatch through public facades with exact typed errors."""

from .errors import ConfigError, EvidenceWikiError, UsageError
from .research_command_contracts import OPERATIONS, REQUEST, RESULT, checked, error_document
from .runtime_configuration import HostRuntime

ARGUMENTS = {
    "run-cycle": {"run_id", "request_id", "expected_basis_id", "max_steps", "planner_config_ref"},
    "refresh": {"run_id", "request_id", "previous_run_id", "max_steps", "planner_config_ref", "source_updates"},
    "current": {"run_id"}, "next-work": {"run_id"}, "brief": {"run_id", "format"}, "resolution": {"run_id"},
    "recovery-status": {"run_id", "after_sequence", "max_effects"},
    "history": {"run_id", "kind", "after_index", "max_records", "expected_history_basis_id"},
    "changes": {"pairs", "before_requirements_revision_id"}, "impact": {"pairs", "before_requirements_revision_id"},
}


def invoke(workspace, request, *, runtime=None):
    workspace._check_open()
    if type(request) is not dict or request.get("operation") not in OPERATIONS:
        checked(REQUEST, request)
    operation = request["operation"]
    try:
        request = checked(REQUEST, request)
        arguments = request["arguments"]
        if set(arguments)-ARGUMENTS[operation]:
            raise UsageError("RESEARCH_COMMAND_ARGUMENTS_INVALID", "Command arguments do not match the public operation.", recoverable=False)
        if type(runtime) is not HostRuntime:
            raise ConfigError("RESEARCH_EXECUTION_CONFIG_INVALID", "An explicit host runtime is required for this operation.",
                recoverable=False, remediation="Provide configured host policy, grants and execution services; the command never provisions credentials or prompts.")
        with runtime.context.activate():
            root = runtime.context.require_path(workspace.root, write=operation in {"run-cycle", "refresh"})
            if root.resolve() != workspace.root:
                raise ConfigError("RESEARCH_COMMAND_TARGET_INVALID", "The command target differs from its public workspace handle.", recoverable=False)
            namespace = workspace.research
            shared = {"execution": runtime.execution}
            if operation == "run-cycle":
                result = namespace.run_cycle(runtime.policy, **shared, **arguments)
            elif operation == "refresh":
                result = namespace.refresh(runtime.policy, **shared, **arguments)
            elif operation in {"current", "next-work"}:
                result = getattr(namespace, "current" if operation == "current" else "next")(runtime.policy, **shared, **arguments)
            elif operation in {"changes", "impact"}:
                result = getattr(namespace, operation)(**arguments)
            elif operation == "history":
                result = namespace.history(**shared, **arguments)
            else:
                selected = dict(arguments)
                run_id = selected.pop("run_id")
                with namespace.session(runtime.policy, run_id=run_id, **shared) as session:
                    result = getattr(session, operation.replace("-", "_"))(**selected)
        exit_code = 3 if (result.get("outcome") in {"abstained", "rejected", "stop_budget", "blocked_external", "policy_incompatible"}
            or result.get("verdict") in {"abstain", "reject"}) else 0
        return checked(RESULT, {"schema_version": RESULT, "operation": operation, "terminal": True, "status": "returned",
            "result": result, "error": None, "exit_code": exit_code, "authorized": False, "evidence_accepted": False})
    except EvidenceWikiError as error:
        return refusal(operation, error)
    except (TypeError, KeyError):
        return refusal(operation, UsageError("RESEARCH_COMMAND_ARGUMENTS_INVALID", "Required public operation arguments are missing or invalid.", recoverable=False))


def refusal(operation, error):
    return checked(RESULT, {"schema_version": RESULT, "operation": operation, "terminal": True, "status": "refused",
        "result": None, "error": error_document(error), "exit_code": error.exit_code, "authorized": False, "evidence_accepted": False})
