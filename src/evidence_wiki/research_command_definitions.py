"""Closed public journey operations and argument shapes shared by transports."""

from copy import deepcopy


def definitions():
    text = {"type": "string", "minLength": 1, "maxLength": 4096}
    identifier = {**text, "maxLength": 128}
    number = {"type": "integer", "minimum": 0, "maximum": 100000000}
    opaque = {"type": "object", "maxProperties": 128, "additionalProperties": True}
    values = {"type": "array", "maxItems": 128}
    rows = {}

    def add(name, required=(), optional=(), *, write=False, shapes=None, method=None):
        fields = {key: deepcopy(identifier) for key in (*required, *optional)}
        for key in fields:
            if key in {"max_steps", "after_sequence", "after_index", "max_records", "max_effects", "page_chars"}:
                fields[key] = deepcopy(number)
            elif key in {"instruction", "metadata", "proposal"}:
                fields[key] = deepcopy(opaque)
            elif key in {"source_updates", "source_ids", "alternative_ids", "pairs"}:
                fields[key] = deepcopy(values)
            elif key == "path":
                fields[key] = deepcopy(text)
        fields.update(shapes or {})
        for key in optional:
            if key in {"run_id", "expected_basis_id", "planner_config_ref", "before_requirements_revision_id", "kind",
                    "expected_history_basis_id", "pairs", "original_id", "as_of"}:
                fields[key] = {"anyOf": [fields[key], {"type": "null"}]}
        rows[name] = {"write": write, "method": method or name.replace("-", "_"), "schema": {"type": "object", "properties": fields,
            "required": list(required), "additionalProperties": False}}

    run = ("run_id",)
    request = (*run, "request_id")
    basis = (*request, "expected_basis_id")
    add("run-cycle", request, ("expected_basis_id", "max_steps", "planner_config_ref"), write=True)
    add("refresh", (*request, "previous_run_id"), ("max_steps", "planner_config_ref", "source_updates"), write=True)
    for name in ("current", "next-work"):
        add(name, run)
    add("brief", run, ("format",), shapes={"format": {"enum": ["structured", "markdown"]}})
    add("resolution", run)
    page = {"type": "integer", "minimum": 1, "maximum": 64}
    add("recovery-status", run, ("after_sequence", "max_effects"), shapes={"max_effects": page})
    add("history", run, ("kind", "after_index", "max_records", "expected_history_basis_id"))
    add("changes", ("pairs",), ("before_requirements_revision_id",))
    add("impact", optional=("pairs", "before_requirements_revision_id"))
    add("advance", basis, write=True)
    add("run-until-terminal", basis, ("max_steps",), write=True)
    add("pending", run)
    for name in ("cancel-pending", "reconcile-pending"):
        add(name, request, write=True)
    add("reconcile-recovery", basis, write=True)
    add("report", run, ("after_sequence", "max_records"), shapes={"max_records": page})
    add("semantic-report", run, ("after_index", "max_records", "expected_snapshot_id"), shapes={
        "after_index": {"type": "integer", "minimum": 0, "maximum": 1024},
        "max_records": {"type": "integer", "minimum": 1, "maximum": 16},
        "expected_snapshot_id": {"type": "string", "pattern": r"^sha256:[a-f0-9]{64}$(?![\s\S])"}})
    add("repair-progress", run, ("original_id", "after_sequence", "max_records"))
    add("review-services", run)
    add("prepare-acquisition", request, ("alternative_ids",))
    add("choose-acquisition", (*request, "instruction", "config_ref"), write=True)
    add("run-acquisition", (*request, "instruction", "config_ref", "metadata", "path"), write=True)
    add("acquire-evidence", (*request, "source_request_id", "expected_request_basis", "metadata", "path"), write=True)
    add("dispatch-acquisition", (*request, "source_request_id", "expected_request_basis"), write=True, method="acquire")
    add("examine-coverage", (*request, "question_slug", "config_ref"), ("source_ids",), write=True)
    add("examine-counterevidence", (*request, "question_slug", "claim_id", "config_ref"), ("source_ids",), write=True)
    add("calculate", request, ("as_of",), write=True)
    add("propose-calculation", (*request, "original_id", "config_ref"), ("as_of",), write=True)
    add("assemble-review-packet", (*request, "claim_id"), ("task_role", "page_chars"), write=True)
    add("prepare-delivery", run)
    add("deliver", (*request, "proposal"), write=True)
    add("delivery-status", request)
    add("reconcile-delivery", request, write=True)
    return rows


DEFINITIONS = definitions()
OPERATIONS = tuple(DEFINITIONS)
WRITE_OPERATIONS = frozenset(name for name, row in DEFINITIONS.items() if row["write"])
READ_OPERATIONS = frozenset(OPERATIONS) - WRITE_OPERATIONS
